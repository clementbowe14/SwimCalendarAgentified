"""Transactional persistence for one encrypted Google Calendar connection.

Callers own the SQLAlchemy session and command transaction. This module never
loads deployment configuration, commits, logs, or returns credential material.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Literal, NoReturn, Optional
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError as _SQLAlchemyError
from sqlalchemy.orm import (
    Session as _Session,
    SessionTransactionOrigin as _SessionTransactionOrigin,
)

from app.calendar_credentials import (
    CalendarCredentialCipher as _CalendarCredentialCipher,
    CalendarCredentialScope as _CalendarCredentialScope,
    EncryptedCalendarCredential as _EncryptedCalendarCredential,
)
from app.models import (
    ProviderAccountDB as _ProviderAccountDB,
    ProviderCalendarConnectionDB as _ProviderCalendarConnectionDB,
)


__all__ = (
    "CalendarConnectionError",
    "CalendarConnectionInputError",
    "CalendarConnectionOwnerNotFound",
    "CalendarConnectionPersistenceError",
    "CalendarConnectionState",
    "CalendarConnectionStatus",
    "CalendarConnectionTransactionRequired",
    "disconnect_google_calendar",
    "get_google_calendar_connection_status",
    "mark_google_calendar_authorization_required",
    "replace_google_calendar_connection",
)


UTC = datetime.timezone.utc
_CONNECTION_ID_PATTERN = re.compile(r"calconn_[0-9a-f]{32}\Z")
_POSTGRESQL_BIGINT_MAX = 9_223_372_036_854_775_807

CalendarConnectionState = Literal[
    "disconnected",
    "connected",
    "authorization_required",
]


class CalendarConnectionError(RuntimeError):
    """Base error for calendar connection persistence."""


class CalendarConnectionOwnerNotFound(CalendarConnectionError):
    """The trusted provider account no longer exists."""

    def __init__(self) -> None:
        super().__init__("Calendar connection owner was not found.")


class CalendarConnectionTransactionRequired(CalendarConnectionError):
    """A command was called without a caller-owned transaction."""

    def __init__(self) -> None:
        super().__init__("Calendar connection command requires a transaction.")


class CalendarConnectionInputError(CalendarConnectionError):
    """A non-secret command value is malformed or stale in time."""

    def __init__(self) -> None:
        super().__init__("Calendar connection input is invalid.")


class CalendarConnectionPersistenceError(CalendarConnectionError):
    """The requested persistence transition could not complete safely."""

    def __init__(self) -> None:
        super().__init__("Calendar connection could not be persisted.")


@dataclass(frozen=True, slots=True)
class CalendarConnectionStatus:
    """Credential-free lifecycle projection suitable for later API mapping."""

    provider: Literal["google"]
    state: CalendarConnectionState
    connected_at: Optional[datetime.datetime]
    updated_at: Optional[datetime.datetime]
    authorization_required_at: Optional[datetime.datetime]


def get_google_calendar_connection_status(
    session: _Session,
    *,
    account_id: str,
) -> CalendarConnectionStatus:
    """Read one provider account's credential-free Google connection state."""

    try:
        account = _provider_account(session, account_id=account_id, lock=False)
        row = _connection_for_account(
            session,
            account.id,
            refresh=False,
        )
        return _status(row)
    except CalendarConnectionError:
        raise
    except _SQLAlchemyError:
        pass
    _raise_sanitized_persistence_error()


def replace_google_calendar_connection(
    session: _Session,
    *,
    account_id: str,
    refresh_token: str,
    cipher: _CalendarCredentialCipher,
    now: Optional[datetime.datetime] = None,
) -> CalendarConnectionStatus:
    """Atomically replace the current connection with a fresh encrypted row."""

    _require_command_transaction(session)
    _require_no_pending_lifecycle_mutations(session)
    supplied_at = _aware_utc(now) if now is not None else None
    try:
        account = _provider_account(session, account_id=account_id, lock=True)
        next_revision = _next_calendar_connection_revision(account)
        existing = _connection_for_account(
            session,
            account.id,
            refresh=True,
        )
        replaced_at = supplied_at or datetime.datetime.now(UTC)
        if existing is not None and replaced_at < _aware_utc(existing.updated_at):
            raise CalendarConnectionInputError()

        connection_id = f"calconn_{uuid4().hex}"
        scope = _CalendarCredentialScope(
            instructor_id=account.instructor_id,
            connection_id=connection_id,
            provider="google",
        )
        envelope = _unused_envelope(
            session,
            refresh_token=refresh_token,
            cipher=cipher,
            scope=scope,
        )

        if existing is not None:
            session.delete(existing)
            session.flush()

        account.calendar_connection_revision = next_revision
        replacement = _ProviderCalendarConnectionDB(
            id=connection_id,
            account_id=account.id,
            provider="google",
            credential_version=envelope.version,
            credential_key_id=envelope.key_id,
            credential_nonce=envelope.nonce,
            credential_ciphertext=envelope.ciphertext,
            authorization_required_at=None,
            created_at=replaced_at,
            updated_at=replaced_at,
        )
        session.add(replacement)
        session.flush()
        return _status(replacement)
    except CalendarConnectionError:
        raise
    except _SQLAlchemyError:
        pass
    _raise_sanitized_persistence_error()


def mark_google_calendar_authorization_required(
    session: _Session,
    *,
    account_id: str,
    expected_connection_id: str,
    now: Optional[datetime.datetime] = None,
) -> CalendarConnectionStatus:
    """Mark only the connection observed by a failed credential use."""

    _require_command_transaction(session)
    _require_no_pending_lifecycle_mutations(session)
    if not _valid_connection_id(expected_connection_id):
        raise CalendarConnectionInputError()
    supplied_at = _aware_utc(now) if now is not None else None
    try:
        account = _provider_account(session, account_id=account_id, lock=True)
        row = _connection_for_account(
            session,
            account.id,
            refresh=True,
        )
        if row is None or row.id != expected_connection_id:
            return _status(row)
        if row.authorization_required_at is not None:
            return _status(row)
        marked_at = supplied_at or datetime.datetime.now(UTC)
        if marked_at < _aware_utc(row.updated_at):
            raise CalendarConnectionInputError()

        row.authorization_required_at = marked_at
        row.updated_at = marked_at
        session.flush()
        return _status(row)
    except CalendarConnectionError:
        raise
    except _SQLAlchemyError:
        pass
    _raise_sanitized_persistence_error()


def disconnect_google_calendar(
    session: _Session,
    *,
    account_id: str,
) -> CalendarConnectionStatus:
    """Idempotently remove one provider account's encrypted connection."""

    _require_command_transaction(session)
    _require_no_pending_lifecycle_mutations(session)
    try:
        account = _provider_account(session, account_id=account_id, lock=True)
        next_revision = _next_calendar_connection_revision(account)
        row = _connection_for_account(
            session,
            account.id,
            refresh=True,
        )
        account.calendar_connection_revision = next_revision
        if row is not None:
            session.delete(row)
        session.flush()
        return _status(None)
    except CalendarConnectionError:
        raise
    except _SQLAlchemyError:
        pass
    _raise_sanitized_persistence_error()


def _provider_account(
    session: _Session,
    *,
    account_id: str,
    lock: bool,
) -> _ProviderAccountDB:
    if not isinstance(account_id, str) or not account_id.strip():
        raise CalendarConnectionOwnerNotFound()
    query = (
        session.query(_ProviderAccountDB)
        .filter(_ProviderAccountDB.id == account_id)
    )
    if lock:
        query = query.populate_existing().with_for_update()
    with session.no_autoflush:
        account = query.one_or_none()
    if account is None:
        raise CalendarConnectionOwnerNotFound()
    return account


def _connection_for_account(
    session: _Session,
    account_id: str,
    *,
    refresh: bool,
) -> Optional[_ProviderCalendarConnectionDB]:
    query = session.query(_ProviderCalendarConnectionDB).filter(
        _ProviderCalendarConnectionDB.account_id == account_id
    )
    if refresh:
        query = query.populate_existing()
    with session.no_autoflush:
        return query.one_or_none()


def _next_calendar_connection_revision(
    account: _ProviderAccountDB,
) -> int:
    current = account.calendar_connection_revision
    if (
        type(current) is not int
        or current < 0
        or current >= _POSTGRESQL_BIGINT_MAX
    ):
        raise CalendarConnectionPersistenceError()
    return current + 1


def _unused_envelope(
    session: _Session,
    *,
    refresh_token: str,
    cipher: _CalendarCredentialCipher,
    scope: _CalendarCredentialScope,
) -> _EncryptedCalendarCredential:
    envelope = cipher.encrypt_refresh_token(refresh_token, scope=scope)
    collision = (
        session.query(_ProviderCalendarConnectionDB.id)
        .filter(
            _ProviderCalendarConnectionDB.credential_key_id == envelope.key_id,
            _ProviderCalendarConnectionDB.credential_nonce == envelope.nonce,
        )
        .first()
    )
    if collision is not None:
        raise CalendarConnectionPersistenceError()
    return envelope


def _status(
    row: Optional[_ProviderCalendarConnectionDB],
) -> CalendarConnectionStatus:
    if row is None:
        return CalendarConnectionStatus(
            provider="google",
            state="disconnected",
            connected_at=None,
            updated_at=None,
            authorization_required_at=None,
        )
    authorization_required_at = (
        _aware_utc(row.authorization_required_at)
        if row.authorization_required_at is not None
        else None
    )
    return CalendarConnectionStatus(
        provider="google",
        state=(
            "authorization_required"
            if authorization_required_at is not None
            else "connected"
        ),
        connected_at=_aware_utc(row.created_at),
        updated_at=_aware_utc(row.updated_at),
        authorization_required_at=authorization_required_at,
    )


def _require_command_transaction(session: _Session) -> None:
    transaction = (
        session.get_transaction() if isinstance(session, _Session) else None
    )
    if (
        transaction is None
        or transaction.origin is not _SessionTransactionOrigin.BEGIN
    ):
        raise CalendarConnectionTransactionRequired()


def _require_no_pending_lifecycle_mutations(session: _Session) -> None:
    for collection in (session.new, session.dirty, session.deleted):
        if any(
            isinstance(
                value,
                (_ProviderAccountDB, _ProviderCalendarConnectionDB),
            )
            for value in collection
        ):
            raise CalendarConnectionPersistenceError()


def _raise_sanitized_persistence_error() -> NoReturn:
    """Raise outside a database handler so raw parameters have no context."""

    raise CalendarConnectionPersistenceError()


def _valid_connection_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and _CONNECTION_ID_PATTERN.fullmatch(value) is not None
    )


def _aware_utc(value: object) -> datetime.datetime:
    if (
        not isinstance(value, datetime.datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise CalendarConnectionInputError()
    return value.astimezone(UTC)
