"""Committed, route-inert claim boundary for Google Calendar callbacks."""

from __future__ import annotations

import base64
import datetime
import re
from collections.abc import Callable
from typing import NoReturn, final

from sqlalchemy.engine import Engine as _Engine
from sqlalchemy.orm import Session as _Session

from app.calendar_oauth_pkce import CalendarOAuthPKCECipher
from app.calendar_oauth_states import (
    ConsumedGoogleCalendarOAuthPKCEState,
    consume_google_calendar_oauth_state_with_pkce_and_browser_binding,
)
from app.google_calendar_oauth import GoogleCalendarOAuthError
from app.models import (
    ProviderAccountDB,
    ProviderCalendarOAuthPKCEVerifierDB,
    ProviderCalendarOAuthStateDB,
    ProviderSessionDB,
)


__all__ = (
    "GoogleCalendarOAuthCallbackClaim",
    "GoogleCalendarOAuthCallbackClaimError",
    "claim_google_calendar_oauth_callback",
)


UTC = datetime.timezone.utc
_CANONICAL_BASE64URL_32_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_MAX_IDENTIFIER_BYTES = 255
_POSTGRESQL_BIGINT_MAX = 9_223_372_036_854_775_807
_CLAIM_MODELS = (
    ProviderAccountDB,
    ProviderSessionDB,
    ProviderCalendarOAuthStateDB,
    ProviderCalendarOAuthPKCEVerifierDB,
)


class GoogleCalendarOAuthCallbackClaimError(GoogleCalendarOAuthError):
    """A Google Calendar callback could not be claimed safely."""

    def __init__(self) -> None:
        super().__init__("Google Calendar authorization could not be claimed.")


class _SealedClaimValue:
    """Immutable, non-serializable base for a secret-bearing claim handoff."""

    __slots__ = ()

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("Google Calendar callback claims are immutable.")

    def __delattr__(self, _name: str) -> None:
        raise AttributeError("Google Calendar callback claims are immutable.")

    def __reduce__(self) -> NoReturn:
        raise TypeError("Google Calendar callback claims cannot be serialized.")

    def __reduce_ex__(self, _protocol: int) -> NoReturn:
        raise TypeError("Google Calendar callback claims cannot be serialized.")

    def __getstate__(self) -> NoReturn:
        raise TypeError("Google Calendar callback claims cannot be serialized.")


@final
class GoogleCalendarOAuthCallbackClaim(_SealedClaimValue):
    """Committed callback owner, verifier, and expected revision."""

    __slots__ = (
        "account_id",
        "session_id",
        "code_verifier",
        "expected_calendar_connection_revision",
    )

    account_id: str
    session_id: str
    code_verifier: str
    expected_calendar_connection_revision: int

    def __init_subclass__(cls, **_kwargs: object) -> NoReturn:
        raise TypeError("Google Calendar callback claims cannot be subclassed.")

    def __init__(
        self,
        *,
        account_id: object,
        session_id: object,
        code_verifier: object,
        expected_calendar_connection_revision: object,
    ) -> None:
        del (
            account_id,
            session_id,
            code_verifier,
            expected_calendar_connection_revision,
        )
        _raise_claim_error()

    def __repr__(self) -> str:
        return "GoogleCalendarOAuthCallbackClaim(<redacted>)"

    __str__ = __repr__


def claim_google_calendar_oauth_callback(
    session_factory: Callable[[], _Session],
    *,
    raw_state: str,
    raw_browser_binding: str,
    pkce_cipher: CalendarOAuthPKCECipher,
    now: datetime.datetime | None = None,
) -> GoogleCalendarOAuthCallbackClaim:
    """Commit one state claim before returning its network handoff."""

    try:
        return _claim_google_calendar_oauth_callback(
            session_factory,
            raw_state=raw_state,
            raw_browser_binding=raw_browser_binding,
            pkce_cipher=pkce_cipher,
            now=now,
        )
    except Exception:
        pass
    del session_factory, raw_state, raw_browser_binding, pkce_cipher, now
    _raise_claim_error()


def _claim_google_calendar_oauth_callback(
    session_factory: object,
    *,
    raw_state: object,
    raw_browser_binding: object,
    pkce_cipher: object,
    now: object,
) -> GoogleCalendarOAuthCallbackClaim:
    trusted_state = _validated_bearer(raw_state)
    trusted_browser_binding = _validated_bearer(raw_browser_binding)
    if trusted_state == trusted_browser_binding:
        raise ValueError("callback bearers must differ")
    if type(pkce_cipher) is not CalendarOAuthPKCECipher:
        raise ValueError("invalid PKCE cipher")
    operation_time = _validated_operation_time(now)
    if not callable(session_factory):
        raise ValueError("invalid session factory")

    database_session = session_factory()
    if not isinstance(database_session, _Session):
        raise ValueError("invalid database session")
    try:
        database_bind = database_session.get_bind()
        if database_session.get_transaction() is not None or not isinstance(
            database_bind,
            _Engine,
        ):
            raise ValueError("invalid database session")
        for model in _CLAIM_MODELS:
            if database_session.get_bind(mapper=model) is not database_bind:
                raise ValueError("invalid database session")

        with database_session.begin():
            consumed = (
                consume_google_calendar_oauth_state_with_pkce_and_browser_binding(
                    database_session,
                    raw_state=trusted_state,
                    raw_browser_binding=trusted_browser_binding,
                    pkce_cipher=pkce_cipher,
                    now=operation_time,
                )
            )
            account_id, session_id, code_verifier = (
                _validated_consumed_snapshot(consumed)
            )
            expected_revision = _advance_claim_revision(
                database_session,
                account_id=account_id,
            )
    finally:
        database_session.close()

    return _new_claim(
        account_id=account_id,
        session_id=session_id,
        code_verifier=code_verifier,
        expected_calendar_connection_revision=expected_revision,
    )


def _validated_bearer(value: object) -> str:
    if (
        type(value) is not str
        or _CANONICAL_BASE64URL_32_PATTERN.fullmatch(value) is None
    ):
        raise ValueError("invalid callback bearer")
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(
            encoded + b"=",
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, ValueError):
        raise ValueError("invalid callback bearer") from None
    if (
        len(decoded) != 32
        or base64.urlsafe_b64encode(decoded).rstrip(b"=") != encoded
    ):
        raise ValueError("invalid callback bearer")
    return value


def _validated_operation_time(value: object) -> datetime.datetime | None:
    if value is None:
        return None
    if (
        type(value) is not datetime.datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("invalid operation time")
    return value.astimezone(UTC)


def _validated_consumed_snapshot(
    value: object,
) -> tuple[str, str, str]:
    if type(value) is not ConsumedGoogleCalendarOAuthPKCEState:
        raise ValueError("invalid consumed state")
    account_id = _validated_identifier(value.account_id)
    session_id = _validated_identifier(value.session_id)
    code_verifier = _validated_bearer(value.code_verifier)
    return account_id, session_id, code_verifier


def _advance_claim_revision(
    session: _Session,
    *,
    account_id: str,
) -> int:
    account = (
        session.query(ProviderAccountDB)
        .filter(ProviderAccountDB.id == account_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if account is None:
        raise ValueError("missing callback owner")
    current_revision = account.calendar_connection_revision
    if (
        type(current_revision) is not int
        or current_revision < 0
        or current_revision >= _POSTGRESQL_BIGINT_MAX
    ):
        raise ValueError("invalid callback revision")
    expected_revision = current_revision + 1
    account.calendar_connection_revision = expected_revision
    session.flush()
    return expected_revision


def _validated_identifier(value: object) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= _MAX_IDENTIFIER_BYTES
        or value != value.strip()
    ):
        raise ValueError("invalid owner identifier")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("invalid owner identifier") from None
    if any(byte < 0x21 or byte > 0x7E for byte in encoded):
        raise ValueError("invalid owner identifier")
    return value


def _new_claim(
    *,
    account_id: str,
    session_id: str,
    code_verifier: str,
    expected_calendar_connection_revision: int,
) -> GoogleCalendarOAuthCallbackClaim:
    result = object.__new__(GoogleCalendarOAuthCallbackClaim)
    object.__setattr__(result, "account_id", account_id)
    object.__setattr__(result, "session_id", session_id)
    object.__setattr__(result, "code_verifier", code_verifier)
    object.__setattr__(
        result,
        "expected_calendar_connection_revision",
        expected_calendar_connection_revision,
    )
    return result


def _raise_claim_error() -> NoReturn:
    error = GoogleCalendarOAuthCallbackClaimError()
    try:
        raise error from None
    except GoogleCalendarOAuthCallbackClaimError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise
