"""Transactional issue/consume boundary for Google Calendar OAuth state.

Callers own the SQLAlchemy session and command transaction. This module never
commits, logs, loads deployment configuration, or persists a raw bearer value.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import NoReturn, Optional

from sqlalchemy import delete as _delete
from sqlalchemy.exc import (
    IntegrityError as _IntegrityError,
    SQLAlchemyError as _SQLAlchemyError,
)
from sqlalchemy.orm import (
    Session as _Session,
    SessionTransactionOrigin as _SessionTransactionOrigin,
)

from app.models import (
    ProviderAccountDB as _ProviderAccountDB,
    ProviderCalendarOAuthStateDB as _ProviderCalendarOAuthStateDB,
    ProviderSessionDB as _ProviderSessionDB,
)


__all__ = (
    "CalendarOAuthStateError",
    "CalendarOAuthStateInputError",
    "CalendarOAuthStateInvalid",
    "CalendarOAuthStatePersistenceError",
    "CalendarOAuthStateSessionInvalid",
    "CalendarOAuthStateTransactionRequired",
    "ConsumedGoogleCalendarOAuthState",
    "IssuedGoogleCalendarOAuthState",
    "consume_google_calendar_oauth_state",
    "issue_google_calendar_oauth_state",
)


UTC = datetime.timezone.utc
_STATE_ENTROPY_BYTES = 32
_STATE_COLLISION_RETRIES = 3
_STATE_LIFETIME = datetime.timedelta(minutes=10)
_STATE_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class CalendarOAuthStateError(RuntimeError):
    """Base error for calendar OAuth-state persistence."""


class CalendarOAuthStateInputError(CalendarOAuthStateError):
    """An internal clock or command value is malformed."""

    def __init__(self) -> None:
        super().__init__("Calendar authorization input is invalid.")


class CalendarOAuthStateSessionInvalid(CalendarOAuthStateError):
    """The trusted account/session pair cannot begin authorization."""

    def __init__(self) -> None:
        super().__init__("Calendar authorization session is not active.")


class CalendarOAuthStateInvalid(CalendarOAuthStateError):
    """The callback bearer cannot authenticate an active authorization."""

    def __init__(self) -> None:
        super().__init__("Calendar authorization state is invalid.")


class CalendarOAuthStatePersistenceError(CalendarOAuthStateError):
    """A state transition could not complete safely."""

    def __init__(self) -> None:
        super().__init__("Calendar authorization state could not be persisted.")


class CalendarOAuthStateTransactionRequired(CalendarOAuthStateError):
    """A command was called without a caller-owned transaction."""

    def __init__(self) -> None:
        super().__init__("Calendar authorization command requires a transaction.")


@dataclass(frozen=True, slots=True, repr=False)
class IssuedGoogleCalendarOAuthState:
    """The one raw bearer returned only to the later authorization service."""

    raw_state: str
    expires_at: datetime.datetime

    def __repr__(self) -> str:
        return "IssuedGoogleCalendarOAuthState(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class ConsumedGoogleCalendarOAuthState:
    """The persisted owner authenticated by one successful state deletion."""

    account_id: str
    session_id: str

    def __repr__(self) -> str:
        return "ConsumedGoogleCalendarOAuthState(<redacted>)"

    __str__ = __repr__


def issue_google_calendar_oauth_state(
    session: _Session,
    *,
    account_id: str,
    session_id: str,
    now: Optional[datetime.datetime] = None,
) -> IssuedGoogleCalendarOAuthState:
    """Replace one active session's pending Google OAuth bearer."""

    _require_command_transaction(session)
    supplied_at = _aware_utc(now) if now is not None else None
    try:
        account, provider_session = _locked_owner(
            session,
            account_id=account_id,
            session_id=session_id,
            invalid_error=CalendarOAuthStateSessionInvalid,
        )
        issued_at = supplied_at or datetime.datetime.now(UTC)
        session_expires_at = _validate_active_owner(
            account,
            provider_session,
            effective_at=issued_at,
            invalid_error=CalendarOAuthStateSessionInvalid,
        )
        expires_at = min(issued_at + _STATE_LIFETIME, session_expires_at)

        for _ in range(_STATE_COLLISION_RETRIES):
            raw_state = _candidate_raw_state()
            if raw_state is None:
                continue
            state_digest = _state_digest(raw_state)
            if _digest_exists(session, state_digest):
                continue
            try:
                with session.begin_nested():
                    session.execute(
                        _delete(_ProviderCalendarOAuthStateDB)
                        .where(
                            _ProviderCalendarOAuthStateDB.account_id
                            == account.id,
                            _ProviderCalendarOAuthStateDB.session_id
                            == provider_session.id,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    session.execute(
                        _ProviderCalendarOAuthStateDB.__table__.insert().values(
                            state_digest=state_digest,
                            account_id=account.id,
                            session_id=provider_session.id,
                            provider="google",
                            created_at=issued_at,
                            expires_at=expires_at,
                        )
                    )
            except _IntegrityError:
                continue
            return IssuedGoogleCalendarOAuthState(
                raw_state=raw_state,
                expires_at=expires_at,
            )
    except CalendarOAuthStateError:
        raise
    except _SQLAlchemyError:
        pass
    _raise_sanitized_persistence_error()


def consume_google_calendar_oauth_state(
    session: _Session,
    *,
    raw_state: str,
    now: Optional[datetime.datetime] = None,
) -> ConsumedGoogleCalendarOAuthState:
    """Consume one valid bearer and return only its persisted owner pair."""

    _require_command_transaction(session)
    supplied_at = _aware_utc(now) if now is not None else None
    if not _valid_raw_state(raw_state):
        raise CalendarOAuthStateInvalid()
    state_digest = _state_digest(raw_state)

    try:
        owner = (
            session.query(
                _ProviderCalendarOAuthStateDB.account_id,
                _ProviderCalendarOAuthStateDB.session_id,
            )
            .filter(
                _ProviderCalendarOAuthStateDB.state_digest == state_digest,
                _ProviderCalendarOAuthStateDB.provider == "google",
            )
            .one_or_none()
        )
        if owner is None:
            raise CalendarOAuthStateInvalid()

        account, provider_session = _locked_owner(
            session,
            account_id=owner.account_id,
            session_id=owner.session_id,
            invalid_error=CalendarOAuthStateInvalid,
        )
        consumed_at = supplied_at or datetime.datetime.now(UTC)
        _validate_active_owner(
            account,
            provider_session,
            effective_at=consumed_at,
            invalid_error=CalendarOAuthStateInvalid,
        )

        deleted = session.execute(
            _delete(_ProviderCalendarOAuthStateDB)
            .where(
                _ProviderCalendarOAuthStateDB.state_digest == state_digest,
                _ProviderCalendarOAuthStateDB.provider == "google",
                _ProviderCalendarOAuthStateDB.account_id == account.id,
                _ProviderCalendarOAuthStateDB.session_id
                == provider_session.id,
                _ProviderCalendarOAuthStateDB.created_at <= consumed_at,
                _ProviderCalendarOAuthStateDB.expires_at > consumed_at,
            )
            .returning(
                _ProviderCalendarOAuthStateDB.account_id,
                _ProviderCalendarOAuthStateDB.session_id,
            )
            .execution_options(synchronize_session=False)
        ).one_or_none()
        if deleted is None:
            raise CalendarOAuthStateInvalid()
        return ConsumedGoogleCalendarOAuthState(
            account_id=deleted.account_id,
            session_id=deleted.session_id,
        )
    except CalendarOAuthStateError:
        raise
    except _SQLAlchemyError:
        pass
    _raise_sanitized_persistence_error()


def _locked_owner(
    session: _Session,
    *,
    account_id: object,
    session_id: object,
    invalid_error: type[CalendarOAuthStateError],
) -> tuple[_ProviderAccountDB, _ProviderSessionDB]:
    if not _valid_identifier(account_id) or not _valid_identifier(session_id):
        raise invalid_error()
    account = (
        session.query(_ProviderAccountDB)
        .filter(_ProviderAccountDB.id == account_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if account is None:
        raise invalid_error()
    provider_session = (
        session.query(_ProviderSessionDB)
        .filter(
            _ProviderSessionDB.account_id == account.id,
            _ProviderSessionDB.id == session_id,
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if provider_session is None:
        raise invalid_error()
    return account, provider_session


def _validate_active_owner(
    account: _ProviderAccountDB,
    provider_session: _ProviderSessionDB,
    *,
    effective_at: datetime.datetime,
    invalid_error: type[CalendarOAuthStateError],
) -> datetime.datetime:
    session_created_at = _stored_utc(provider_session.created_at)
    session_expires_at = _stored_utc(provider_session.expires_at)
    if (
        account.disabled_at is not None
        or provider_session.revoked_at is not None
        or provider_session.session_version != account.session_version
        or session_created_at > effective_at
        or session_expires_at <= effective_at
    ):
        raise invalid_error()
    return session_expires_at


def _digest_exists(session: _Session, state_digest: bytes) -> bool:
    return (
        session.query(_ProviderCalendarOAuthStateDB.state_digest)
        .filter(_ProviderCalendarOAuthStateDB.state_digest == state_digest)
        .first()
        is not None
    )


def _candidate_raw_state() -> Optional[str]:
    try:
        candidate = secrets.token_urlsafe(_STATE_ENTROPY_BYTES)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate if _valid_raw_state(candidate) else None


def _valid_raw_state(value: object) -> bool:
    return isinstance(value, str) and _STATE_PATTERN.fullmatch(value) is not None


def _state_digest(raw_state: str) -> bytes:
    return hashlib.sha256(raw_state.encode("ascii")).digest()


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _require_command_transaction(session: _Session) -> None:
    transaction = (
        session.get_transaction() if isinstance(session, _Session) else None
    )
    if (
        transaction is None
        or transaction.origin is not _SessionTransactionOrigin.BEGIN
    ):
        raise CalendarOAuthStateTransactionRequired()


def _aware_utc(value: object) -> datetime.datetime:
    if (
        not isinstance(value, datetime.datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise CalendarOAuthStateInputError()
    return value.astimezone(UTC)


def _stored_utc(value: object) -> datetime.datetime:
    try:
        return _aware_utc(value)
    except CalendarOAuthStateInputError:
        pass
    _raise_sanitized_persistence_error()


def _raise_sanitized_persistence_error() -> NoReturn:
    """Raise outside an exception handler so private parameters stay severed."""

    raise CalendarOAuthStatePersistenceError()
