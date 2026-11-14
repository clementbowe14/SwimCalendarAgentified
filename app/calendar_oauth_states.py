"""Transactional issue/consume boundary for Google Calendar OAuth state.

Callers own the SQLAlchemy session and command transaction. This module never
commits, logs, loads deployment configuration, or persists a raw bearer value.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Literal, NoReturn, Optional

from sqlalchemy import delete as _delete
from sqlalchemy import exists as _exists
from sqlalchemy.exc import (
    IntegrityError as _IntegrityError,
    SQLAlchemyError as _SQLAlchemyError,
)
from sqlalchemy.orm import (
    Session as _Session,
    SessionTransactionOrigin as _SessionTransactionOrigin,
)

from app.calendar_oauth_pkce import (
    CalendarOAuthPKCECipher as _CalendarOAuthPKCECipher,
    CalendarOAuthPKCEScope as _CalendarOAuthPKCEScope,
    EncryptedCalendarOAuthPKCEVerifier as _EncryptedCalendarOAuthPKCEVerifier,
)
from app.models import (
    ProviderAccountDB as _ProviderAccountDB,
    ProviderCalendarOAuthPKCEVerifierDB as _ProviderCalendarOAuthPKCEVerifierDB,
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
    "ConsumedGoogleCalendarOAuthPKCEState",
    "ConsumedGoogleCalendarOAuthState",
    "IssuedGoogleCalendarOAuthBrowserBoundPKCEState",
    "IssuedGoogleCalendarOAuthPKCEState",
    "IssuedGoogleCalendarOAuthState",
    "consume_google_calendar_oauth_state",
    "consume_google_calendar_oauth_state_with_pkce_and_browser_binding",
    "consume_google_calendar_oauth_state_with_pkce",
    "issue_google_calendar_oauth_state_with_pkce_and_browser_binding",
    "issue_google_calendar_oauth_state_with_pkce",
    "issue_google_calendar_oauth_state",
)


UTC = datetime.timezone.utc
_STATE_ENTROPY_BYTES = 32
_STATE_COLLISION_RETRIES = 3
_STATE_LIFETIME = datetime.timedelta(minutes=10)
_STATE_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_BROWSER_BINDING_ENTROPY_BYTES = 32
_BROWSER_BINDING_DIGEST_DOMAIN = (
    b"dayweave.calendar-oauth-browser-binding.sha256.v1\x00"
)


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
class IssuedGoogleCalendarOAuthPKCEState:
    """Public authorization material for one verifier-backed state."""

    raw_state: str
    expires_at: datetime.datetime
    code_challenge: str

    @property
    def code_challenge_method(self) -> Literal["S256"]:
        return "S256"

    def __repr__(self) -> str:
        return "IssuedGoogleCalendarOAuthPKCEState(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class IssuedGoogleCalendarOAuthBrowserBoundPKCEState:
    """Authorization material plus one browser-held binding secret."""

    issued_state: IssuedGoogleCalendarOAuthPKCEState
    raw_browser_binding: str

    def __repr__(self) -> str:
        return "IssuedGoogleCalendarOAuthBrowserBoundPKCEState(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class ConsumedGoogleCalendarOAuthState:
    """The persisted owner authenticated by one successful state deletion."""

    account_id: str
    session_id: str

    def __repr__(self) -> str:
        return "ConsumedGoogleCalendarOAuthState(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class ConsumedGoogleCalendarOAuthPKCEState:
    """One authenticated owner and the verifier released for token exchange."""

    account_id: str
    session_id: str
    code_verifier: str

    def __repr__(self) -> str:
        return "ConsumedGoogleCalendarOAuthPKCEState(<redacted>)"

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
        if _owner_has_pkce_state(
            session,
            account_id=account.id,
            session_id=provider_session.id,
        ) or _locked_owner_has_browser_bound_state(
            session,
            account_id=account.id,
            session_id=provider_session.id,
        ):
            _raise_sanitized_persistence_error()

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
                            _ProviderCalendarOAuthStateDB.browser_binding_digest
                            .is_(None),
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
                if _locked_owner_has_browser_bound_state(
                    session,
                    account_id=account.id,
                    session_id=provider_session.id,
                ):
                    break
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


def issue_google_calendar_oauth_state_with_pkce(
    session: _Session,
    *,
    account_id: str,
    session_id: str,
    pkce_cipher: _CalendarOAuthPKCECipher,
    now: Optional[datetime.datetime] = None,
) -> IssuedGoogleCalendarOAuthPKCEState:
    """Atomically replace one pending state and encrypted PKCE verifier."""

    error_type = None
    try:
        return _issue_google_calendar_oauth_state_with_pkce(
            session,
            account_id=account_id,
            session_id=session_id,
            pkce_cipher=pkce_cipher,
            now=now,
        )
    except CalendarOAuthStateTransactionRequired:
        error_type = CalendarOAuthStateTransactionRequired
    except CalendarOAuthStateInputError:
        error_type = CalendarOAuthStateInputError
    except CalendarOAuthStateSessionInvalid:
        error_type = CalendarOAuthStateSessionInvalid
    except Exception:
        error_type = CalendarOAuthStatePersistenceError

    del session, account_id, session_id, pkce_cipher, now
    raise error_type() from None


def _issue_google_calendar_oauth_state_with_pkce(
    session: _Session,
    *,
    account_id: str,
    session_id: str,
    pkce_cipher: _CalendarOAuthPKCECipher,
    now: Optional[datetime.datetime],
) -> IssuedGoogleCalendarOAuthPKCEState:
    """Perform issuance below the public traceback-redaction boundary."""

    _require_command_transaction(session)
    supplied_at = _aware_utc(now) if now is not None else None
    if not isinstance(pkce_cipher, _CalendarOAuthPKCECipher):
        raise CalendarOAuthStateInputError()
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
        if _locked_owner_has_browser_bound_state(
            session,
            account_id=account.id,
            session_id=provider_session.id,
        ):
            _raise_sanitized_persistence_error()

        for _ in range(_STATE_COLLISION_RETRIES):
            raw_state = _candidate_raw_state()
            if raw_state is None:
                continue
            state_digest = _state_digest(raw_state)
            if _digest_exists(session, state_digest):
                continue

            generated = pkce_cipher.generate_google_calendar_pkce(
                scope=_CalendarOAuthPKCEScope(
                    state_digest=state_digest,
                    account_id=account.id,
                    session_id=provider_session.id,
                )
            )
            encrypted_verifier = generated.encrypted_verifier
            if _pkce_nonce_exists(
                session,
                key_id=encrypted_verifier.key_id,
                nonce=encrypted_verifier.nonce,
            ):
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
                            _ProviderCalendarOAuthStateDB.browser_binding_digest
                            .is_(None),
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
                    session.execute(
                        _ProviderCalendarOAuthPKCEVerifierDB.__table__
                        .insert()
                        .values(
                            state_digest=state_digest,
                            verifier_version=encrypted_verifier.version,
                            verifier_key_id=encrypted_verifier.key_id,
                            verifier_nonce=encrypted_verifier.nonce,
                            verifier_ciphertext=encrypted_verifier.ciphertext,
                        )
                    )
            except _IntegrityError:
                if _locked_owner_has_browser_bound_state(
                    session,
                    account_id=account.id,
                    session_id=provider_session.id,
                ):
                    break
                if _digest_exists(
                    session,
                    state_digest,
                ) or _pkce_nonce_exists(
                    session,
                    key_id=encrypted_verifier.key_id,
                    nonce=encrypted_verifier.nonce,
                ):
                    continue
                break
            return IssuedGoogleCalendarOAuthPKCEState(
                raw_state=raw_state,
                expires_at=expires_at,
                code_challenge=generated.code_challenge,
            )
    except CalendarOAuthStateError:
        raise
    except Exception:
        pass
    _raise_sanitized_persistence_error()


def issue_google_calendar_oauth_state_with_pkce_and_browser_binding(
    session: _Session,
    *,
    account_id: str,
    session_id: str,
    pkce_cipher: _CalendarOAuthPKCECipher,
    now: Optional[datetime.datetime] = None,
) -> IssuedGoogleCalendarOAuthBrowserBoundPKCEState:
    """Atomically issue PKCE state and an independent browser binding."""

    error_type = None
    try:
        return _issue_google_calendar_oauth_state_with_pkce_and_browser_binding(
            session,
            account_id=account_id,
            session_id=session_id,
            pkce_cipher=pkce_cipher,
            now=now,
        )
    except CalendarOAuthStateTransactionRequired:
        error_type = CalendarOAuthStateTransactionRequired
    except CalendarOAuthStateInputError:
        error_type = CalendarOAuthStateInputError
    except CalendarOAuthStateSessionInvalid:
        error_type = CalendarOAuthStateSessionInvalid
    except Exception:
        error_type = CalendarOAuthStatePersistenceError

    del session, account_id, session_id, pkce_cipher, now
    raise error_type() from None


def _issue_google_calendar_oauth_state_with_pkce_and_browser_binding(
    session: _Session,
    *,
    account_id: str,
    session_id: str,
    pkce_cipher: _CalendarOAuthPKCECipher,
    now: Optional[datetime.datetime],
) -> IssuedGoogleCalendarOAuthBrowserBoundPKCEState:
    """Perform browser-bound issuance below the redaction boundary."""

    _require_command_transaction(session)
    supplied_at = _aware_utc(now) if now is not None else None
    if not isinstance(pkce_cipher, _CalendarOAuthPKCECipher):
        raise CalendarOAuthStateInputError()
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

            browser_binding = _candidate_raw_browser_binding()
            if browser_binding is None:
                continue
            raw_browser_binding, browser_binding_digest = browser_binding
            if (
                raw_browser_binding == raw_state
                or browser_binding_digest == state_digest
                or _browser_binding_digest_exists(
                    session,
                    browser_binding_digest,
                )
            ):
                continue

            generated = pkce_cipher.generate_google_calendar_pkce(
                scope=_CalendarOAuthPKCEScope(
                    state_digest=state_digest,
                    account_id=account.id,
                    session_id=provider_session.id,
                )
            )
            encrypted_verifier = generated.encrypted_verifier
            if _pkce_nonce_exists(
                session,
                key_id=encrypted_verifier.key_id,
                nonce=encrypted_verifier.nonce,
            ):
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
                            browser_binding_digest=browser_binding_digest,
                        )
                    )
                    session.execute(
                        _ProviderCalendarOAuthPKCEVerifierDB.__table__
                        .insert()
                        .values(
                            state_digest=state_digest,
                            verifier_version=encrypted_verifier.version,
                            verifier_key_id=encrypted_verifier.key_id,
                            verifier_nonce=encrypted_verifier.nonce,
                            verifier_ciphertext=encrypted_verifier.ciphertext,
                        )
                    )
            except _IntegrityError:
                if _digest_exists(
                    session,
                    state_digest,
                ) or _browser_binding_digest_exists(
                    session,
                    browser_binding_digest,
                ) or _pkce_nonce_exists(
                    session,
                    key_id=encrypted_verifier.key_id,
                    nonce=encrypted_verifier.nonce,
                ):
                    continue
                break
            return IssuedGoogleCalendarOAuthBrowserBoundPKCEState(
                issued_state=IssuedGoogleCalendarOAuthPKCEState(
                    raw_state=raw_state,
                    expires_at=expires_at,
                    code_challenge=generated.code_challenge,
                ),
                raw_browser_binding=raw_browser_binding,
            )
    except CalendarOAuthStateError:
        raise
    except Exception:
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
                _ProviderCalendarOAuthStateDB.browser_binding_digest.is_(
                    None
                ),
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
                _ProviderCalendarOAuthStateDB.browser_binding_digest.is_(
                    None
                ),
                _ProviderCalendarOAuthStateDB.created_at <= consumed_at,
                _ProviderCalendarOAuthStateDB.expires_at > consumed_at,
                ~_exists().where(
                    _ProviderCalendarOAuthPKCEVerifierDB.state_digest
                    == _ProviderCalendarOAuthStateDB.state_digest
                ),
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


def consume_google_calendar_oauth_state_with_pkce(
    session: _Session,
    *,
    raw_state: str,
    pkce_cipher: _CalendarOAuthPKCECipher,
    now: Optional[datetime.datetime] = None,
) -> ConsumedGoogleCalendarOAuthPKCEState:
    """Consume one verifier-backed state after authenticating its envelope."""

    error_type = None
    try:
        return _consume_google_calendar_oauth_state_with_pkce(
            session,
            raw_state=raw_state,
            pkce_cipher=pkce_cipher,
            now=now,
        )
    except CalendarOAuthStateTransactionRequired:
        error_type = CalendarOAuthStateTransactionRequired
    except CalendarOAuthStateInputError:
        error_type = CalendarOAuthStateInputError
    except CalendarOAuthStateInvalid:
        error_type = CalendarOAuthStateInvalid
    except Exception:
        error_type = CalendarOAuthStatePersistenceError

    del session, raw_state, pkce_cipher, now
    raise error_type() from None


def _consume_google_calendar_oauth_state_with_pkce(
    session: _Session,
    *,
    raw_state: str,
    pkce_cipher: _CalendarOAuthPKCECipher,
    now: Optional[datetime.datetime],
) -> ConsumedGoogleCalendarOAuthPKCEState:
    """Perform PKCE consumption below the public redaction boundary."""

    _require_command_transaction(session)
    supplied_at = _aware_utc(now) if now is not None else None
    if not isinstance(pkce_cipher, _CalendarOAuthPKCECipher):
        raise CalendarOAuthStateInputError()
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
                _ProviderCalendarOAuthStateDB.browser_binding_digest.is_(
                    None
                ),
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

        locked_state = (
            session.query(
                _ProviderCalendarOAuthStateDB.state_digest,
                _ProviderCalendarOAuthStateDB.account_id,
                _ProviderCalendarOAuthStateDB.session_id,
            )
            .filter(
                _ProviderCalendarOAuthStateDB.state_digest == state_digest,
                _ProviderCalendarOAuthStateDB.provider == "google",
                _ProviderCalendarOAuthStateDB.account_id == account.id,
                _ProviderCalendarOAuthStateDB.session_id
                == provider_session.id,
                _ProviderCalendarOAuthStateDB.browser_binding_digest.is_(
                    None
                ),
                _ProviderCalendarOAuthStateDB.created_at <= consumed_at,
                _ProviderCalendarOAuthStateDB.expires_at > consumed_at,
            )
            .with_for_update()
            .one_or_none()
        )
        if locked_state is None:
            raise CalendarOAuthStateInvalid()

        locked_verifier = (
            session.query(
                _ProviderCalendarOAuthPKCEVerifierDB.verifier_version,
                _ProviderCalendarOAuthPKCEVerifierDB.verifier_key_id,
                _ProviderCalendarOAuthPKCEVerifierDB.verifier_nonce,
                _ProviderCalendarOAuthPKCEVerifierDB.verifier_ciphertext,
            )
            .filter(
                _ProviderCalendarOAuthPKCEVerifierDB.state_digest
                == locked_state.state_digest
            )
            .with_for_update()
            .one_or_none()
        )
        if locked_verifier is None:
            raise CalendarOAuthStateInvalid()

        code_verifier = pkce_cipher.decrypt_google_calendar_verifier(
            _EncryptedCalendarOAuthPKCEVerifier(
                version=locked_verifier.verifier_version,
                key_id=locked_verifier.verifier_key_id,
                nonce=locked_verifier.verifier_nonce,
                ciphertext=locked_verifier.verifier_ciphertext,
            ),
            scope=_CalendarOAuthPKCEScope(
                state_digest=locked_state.state_digest,
                account_id=locked_state.account_id,
                session_id=locked_state.session_id,
            ),
        )

        with session.begin_nested():
            deleted = session.execute(
                _delete(_ProviderCalendarOAuthStateDB)
                .where(
                    _ProviderCalendarOAuthStateDB.state_digest
                    == locked_state.state_digest,
                    _ProviderCalendarOAuthStateDB.provider == "google",
                    _ProviderCalendarOAuthStateDB.account_id
                    == locked_state.account_id,
                    _ProviderCalendarOAuthStateDB.session_id
                    == locked_state.session_id,
                    _ProviderCalendarOAuthStateDB.browser_binding_digest.is_(
                        None
                    ),
                    _ProviderCalendarOAuthStateDB.created_at <= consumed_at,
                    _ProviderCalendarOAuthStateDB.expires_at > consumed_at,
                    _exists().where(
                        _ProviderCalendarOAuthPKCEVerifierDB.state_digest
                        == _ProviderCalendarOAuthStateDB.state_digest
                    ),
                )
                .returning(
                    _ProviderCalendarOAuthStateDB.account_id,
                    _ProviderCalendarOAuthStateDB.session_id,
                )
                .execution_options(synchronize_session=False)
            ).one_or_none()
            if deleted is None:
                raise CalendarOAuthStateInvalid()
            consumed = ConsumedGoogleCalendarOAuthPKCEState(
                account_id=deleted.account_id,
                session_id=deleted.session_id,
                code_verifier=code_verifier,
            )
        return consumed
    except CalendarOAuthStateError:
        raise
    except Exception:
        pass
    _raise_sanitized_persistence_error()


def consume_google_calendar_oauth_state_with_pkce_and_browser_binding(
    session: _Session,
    *,
    raw_state: str,
    raw_browser_binding: str,
    pkce_cipher: _CalendarOAuthPKCECipher,
    now: Optional[datetime.datetime] = None,
) -> ConsumedGoogleCalendarOAuthPKCEState:
    """Consume one PKCE state only with its independent browser binding."""

    error_type = None
    try:
        return (
            _consume_google_calendar_oauth_state_with_pkce_and_browser_binding(
                session,
                raw_state=raw_state,
                raw_browser_binding=raw_browser_binding,
                pkce_cipher=pkce_cipher,
                now=now,
            )
        )
    except CalendarOAuthStateTransactionRequired:
        error_type = CalendarOAuthStateTransactionRequired
    except CalendarOAuthStateInputError:
        error_type = CalendarOAuthStateInputError
    except CalendarOAuthStateInvalid:
        error_type = CalendarOAuthStateInvalid
    except Exception:
        error_type = CalendarOAuthStatePersistenceError

    del session, raw_state, raw_browser_binding, pkce_cipher, now
    raise error_type() from None


def _consume_google_calendar_oauth_state_with_pkce_and_browser_binding(
    session: _Session,
    *,
    raw_state: str,
    raw_browser_binding: str,
    pkce_cipher: _CalendarOAuthPKCECipher,
    now: Optional[datetime.datetime],
) -> ConsumedGoogleCalendarOAuthPKCEState:
    """Perform browser-bound consumption below the redaction boundary."""

    _require_command_transaction(session)
    supplied_at = _aware_utc(now) if now is not None else None
    if not isinstance(pkce_cipher, _CalendarOAuthPKCECipher):
        raise CalendarOAuthStateInputError()
    if not _valid_raw_state(raw_state):
        raise CalendarOAuthStateInvalid()
    browser_binding_digest = _browser_binding_digest_from_raw(
        raw_browser_binding
    )
    if (
        browser_binding_digest is None
        or raw_browser_binding == raw_state
    ):
        raise CalendarOAuthStateInvalid()
    state_digest = _state_digest(raw_state)
    if secrets.compare_digest(browser_binding_digest, state_digest):
        raise CalendarOAuthStateInvalid()

    try:
        owner = (
            session.query(
                _ProviderCalendarOAuthStateDB.account_id,
                _ProviderCalendarOAuthStateDB.session_id,
            )
            .filter(
                _ProviderCalendarOAuthStateDB.state_digest == state_digest,
                _ProviderCalendarOAuthStateDB.provider == "google",
                _ProviderCalendarOAuthStateDB.browser_binding_digest
                == browser_binding_digest,
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

        locked_state = (
            session.query(
                _ProviderCalendarOAuthStateDB.state_digest,
                _ProviderCalendarOAuthStateDB.account_id,
                _ProviderCalendarOAuthStateDB.session_id,
            )
            .filter(
                _ProviderCalendarOAuthStateDB.state_digest == state_digest,
                _ProviderCalendarOAuthStateDB.provider == "google",
                _ProviderCalendarOAuthStateDB.account_id == account.id,
                _ProviderCalendarOAuthStateDB.session_id
                == provider_session.id,
                _ProviderCalendarOAuthStateDB.browser_binding_digest
                == browser_binding_digest,
                _ProviderCalendarOAuthStateDB.created_at <= consumed_at,
                _ProviderCalendarOAuthStateDB.expires_at > consumed_at,
            )
            .with_for_update()
            .one_or_none()
        )
        if locked_state is None:
            raise CalendarOAuthStateInvalid()

        locked_verifier = (
            session.query(
                _ProviderCalendarOAuthPKCEVerifierDB.verifier_version,
                _ProviderCalendarOAuthPKCEVerifierDB.verifier_key_id,
                _ProviderCalendarOAuthPKCEVerifierDB.verifier_nonce,
                _ProviderCalendarOAuthPKCEVerifierDB.verifier_ciphertext,
            )
            .filter(
                _ProviderCalendarOAuthPKCEVerifierDB.state_digest
                == locked_state.state_digest
            )
            .with_for_update()
            .one_or_none()
        )
        if locked_verifier is None:
            raise CalendarOAuthStateInvalid()

        code_verifier = pkce_cipher.decrypt_google_calendar_verifier(
            _EncryptedCalendarOAuthPKCEVerifier(
                version=locked_verifier.verifier_version,
                key_id=locked_verifier.verifier_key_id,
                nonce=locked_verifier.verifier_nonce,
                ciphertext=locked_verifier.verifier_ciphertext,
            ),
            scope=_CalendarOAuthPKCEScope(
                state_digest=locked_state.state_digest,
                account_id=locked_state.account_id,
                session_id=locked_state.session_id,
            ),
        )

        with session.begin_nested():
            deleted = session.execute(
                _delete(_ProviderCalendarOAuthStateDB)
                .where(
                    _ProviderCalendarOAuthStateDB.state_digest
                    == locked_state.state_digest,
                    _ProviderCalendarOAuthStateDB.provider == "google",
                    _ProviderCalendarOAuthStateDB.account_id
                    == locked_state.account_id,
                    _ProviderCalendarOAuthStateDB.session_id
                    == locked_state.session_id,
                    _ProviderCalendarOAuthStateDB.browser_binding_digest
                    == browser_binding_digest,
                    _ProviderCalendarOAuthStateDB.created_at <= consumed_at,
                    _ProviderCalendarOAuthStateDB.expires_at > consumed_at,
                    _exists().where(
                        _ProviderCalendarOAuthPKCEVerifierDB.state_digest
                        == _ProviderCalendarOAuthStateDB.state_digest
                    ),
                )
                .returning(
                    _ProviderCalendarOAuthStateDB.account_id,
                    _ProviderCalendarOAuthStateDB.session_id,
                )
                .execution_options(synchronize_session=False)
            ).one_or_none()
            if deleted is None:
                raise CalendarOAuthStateInvalid()
            consumed = ConsumedGoogleCalendarOAuthPKCEState(
                account_id=deleted.account_id,
                session_id=deleted.session_id,
                code_verifier=code_verifier,
            )
        return consumed
    except CalendarOAuthStateError:
        raise
    except Exception:
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


def _browser_binding_digest_exists(
    session: _Session,
    browser_binding_digest: bytes,
) -> bool:
    return (
        session.query(_ProviderCalendarOAuthStateDB.state_digest)
        .filter(
            _ProviderCalendarOAuthStateDB.browser_binding_digest
            == browser_binding_digest
        )
        .first()
        is not None
    )


def _pkce_nonce_exists(
    session: _Session,
    *,
    key_id: str,
    nonce: bytes,
) -> bool:
    return (
        session.query(_ProviderCalendarOAuthPKCEVerifierDB.state_digest)
        .filter(
            _ProviderCalendarOAuthPKCEVerifierDB.verifier_key_id == key_id,
            _ProviderCalendarOAuthPKCEVerifierDB.verifier_nonce == nonce,
        )
        .first()
        is not None
    )


def _owner_has_pkce_state(
    session: _Session,
    *,
    account_id: str,
    session_id: str,
) -> bool:
    return (
        session.query(_ProviderCalendarOAuthPKCEVerifierDB.state_digest)
        .join(
            _ProviderCalendarOAuthStateDB,
            _ProviderCalendarOAuthStateDB.state_digest
            == _ProviderCalendarOAuthPKCEVerifierDB.state_digest,
        )
        .filter(
            _ProviderCalendarOAuthStateDB.account_id == account_id,
            _ProviderCalendarOAuthStateDB.session_id == session_id,
        )
        .first()
        is not None
    )


def _locked_owner_has_browser_bound_state(
    session: _Session,
    *,
    account_id: str,
    session_id: str,
) -> bool:
    state = (
        session.query(
            _ProviderCalendarOAuthStateDB.browser_binding_digest,
        )
        .filter(
            _ProviderCalendarOAuthStateDB.account_id == account_id,
            _ProviderCalendarOAuthStateDB.session_id == session_id,
        )
        .with_for_update()
        .one_or_none()
    )
    return state is not None and state.browser_binding_digest is not None


def _candidate_raw_state() -> Optional[str]:
    try:
        candidate = secrets.token_urlsafe(_STATE_ENTROPY_BYTES)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate if _valid_raw_state(candidate) else None


def _candidate_raw_browser_binding() -> Optional[tuple[str, bytes]]:
    try:
        entropy = secrets.token_bytes(_BROWSER_BINDING_ENTROPY_BYTES)
        if (
            not isinstance(entropy, bytes)
            or len(entropy) != _BROWSER_BINDING_ENTROPY_BYTES
        ):
            return None
        candidate = (
            base64.urlsafe_b64encode(entropy)
            .rstrip(b"=")
            .decode("ascii")
        )
        digest = hashlib.sha256(
            _BROWSER_BINDING_DIGEST_DOMAIN + entropy
        ).digest()
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError):
        return None
    if not _valid_raw_state(candidate):
        return None
    return candidate, digest


def _browser_binding_digest_from_raw(value: object) -> Optional[bytes]:
    if type(value) is not str or _STATE_PATTERN.fullmatch(value) is None:
        return None
    try:
        encoded = value.encode("ascii")
        entropy = base64.b64decode(
            encoded + b"=",
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, ValueError):
        return None
    if (
        len(entropy) != _BROWSER_BINDING_ENTROPY_BYTES
        or base64.urlsafe_b64encode(entropy).rstrip(b"=") != encoded
    ):
        return None
    return hashlib.sha256(
        _BROWSER_BINDING_DIGEST_DOMAIN + entropy
    ).digest()


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
