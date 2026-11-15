"""Commit-before-redirect orchestration for Google Calendar OAuth start."""

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
    IssuedGoogleCalendarOAuthBrowserBoundPKCEState,
    IssuedGoogleCalendarOAuthPKCEState,
    issue_google_calendar_oauth_state_with_pkce_and_browser_binding,
)
from app.google_calendar_authorization import (
    GoogleCalendarAuthorizationRequest,
    build_google_calendar_authorization_request,
)
from app.google_calendar_oauth import (
    GoogleCalendarOAuthConfiguration,
    GoogleCalendarOAuthError,
)
from app.models import (
    ProviderAccountDB,
    ProviderCalendarOAuthPKCEVerifierDB,
    ProviderCalendarOAuthStateDB,
    ProviderSessionDB,
)


__all__ = (
    "GoogleCalendarAuthorizationStart",
    "GoogleCalendarAuthorizationStartError",
    "prepare_google_calendar_authorization_start",
)


UTC = datetime.timezone.utc
_MAX_IDENTIFIER_BYTES = 255
_CANONICAL_BASE64URL_32_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_ISSUANCE_MODELS = (
    ProviderAccountDB,
    ProviderSessionDB,
    ProviderCalendarOAuthStateDB,
    ProviderCalendarOAuthPKCEVerifierDB,
)


class GoogleCalendarAuthorizationStartError(GoogleCalendarOAuthError):
    """A browser-bound Google authorization could not be started safely."""

    def __init__(self) -> None:
        super().__init__("Google Calendar authorization could not be started.")


class _SealedStartValue:
    """Immutable, non-serializable base for a secret-bearing start handoff."""

    __slots__ = ()

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("Google Calendar authorization starts are immutable.")

    def __delattr__(self, _name: str) -> None:
        raise AttributeError("Google Calendar authorization starts are immutable.")

    def __reduce__(self) -> NoReturn:
        raise TypeError("Google Calendar authorization starts cannot be serialized.")

    def __reduce_ex__(self, _protocol: int) -> NoReturn:
        raise TypeError("Google Calendar authorization starts cannot be serialized.")

    def __getstate__(self) -> NoReturn:
        raise TypeError("Google Calendar authorization starts cannot be serialized.")


@final
class GoogleCalendarAuthorizationStart(_SealedStartValue):
    """Committed authorization request and its browser-only binding value."""

    __slots__ = ("authorization_request", "raw_browser_binding", "expires_at")

    authorization_request: GoogleCalendarAuthorizationRequest
    raw_browser_binding: str
    expires_at: datetime.datetime

    def __init_subclass__(cls, **_kwargs: object) -> NoReturn:
        raise TypeError("Google Calendar authorization starts cannot be subclassed.")

    def __init__(
        self,
        *,
        authorization_request: object,
        raw_browser_binding: object,
        expires_at: object,
    ) -> None:
        del authorization_request, raw_browser_binding, expires_at
        _raise_start_error()

    def __repr__(self) -> str:
        return "GoogleCalendarAuthorizationStart(<redacted>)"

    __str__ = __repr__


def prepare_google_calendar_authorization_start(
    session_factory: Callable[[], _Session],
    *,
    account_id: str,
    session_id: str,
    configuration: GoogleCalendarOAuthConfiguration,
    pkce_cipher: CalendarOAuthPKCECipher,
    now: datetime.datetime | None = None,
) -> GoogleCalendarAuthorizationStart:
    """Commit browser-bound PKCE state before building its redirect request."""

    try:
        return _prepare_google_calendar_authorization_start(
            session_factory,
            account_id=account_id,
            session_id=session_id,
            configuration=configuration,
            pkce_cipher=pkce_cipher,
            now=now,
        )
    except Exception:
        pass
    del session_factory, account_id, session_id, configuration, pkce_cipher, now
    _raise_start_error()


def _prepare_google_calendar_authorization_start(
    session_factory: object,
    *,
    account_id: object,
    session_id: object,
    configuration: object,
    pkce_cipher: object,
    now: object,
) -> GoogleCalendarAuthorizationStart:
    trusted_account_id = _validated_identifier(account_id)
    trusted_session_id = _validated_identifier(session_id)
    trusted_configuration = _validated_configuration(configuration)
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
        for model in _ISSUANCE_MODELS:
            if database_session.get_bind(mapper=model) is not database_bind:
                raise ValueError("invalid database session")
        with database_session.begin():
            issued = (
                issue_google_calendar_oauth_state_with_pkce_and_browser_binding(
                    database_session,
                    account_id=trusted_account_id,
                    session_id=trusted_session_id,
                    pkce_cipher=pkce_cipher,
                    now=operation_time,
                )
            )
            issued_state, raw_browser_binding, expires_at = (
                _validated_issuance_snapshot(
                    issued,
                    operation_time=operation_time,
                )
            )
    finally:
        database_session.close()

    authorization_request = build_google_calendar_authorization_request(
        configuration=trusted_configuration,
        issued_state=issued_state,
    )
    if type(authorization_request) is not GoogleCalendarAuthorizationRequest:
        raise ValueError("invalid authorization request")
    return _new_start(
        authorization_request=authorization_request,
        raw_browser_binding=raw_browser_binding,
        expires_at=expires_at,
    )


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


def _validated_configuration(
    value: object,
) -> GoogleCalendarOAuthConfiguration:
    if type(value) is not GoogleCalendarOAuthConfiguration:
        raise ValueError("invalid OAuth configuration")
    client_id = value.client_id
    redirect_uri = value.redirect_uri
    if type(client_id) is not str or type(redirect_uri) is not str:
        raise ValueError("invalid OAuth configuration")
    return GoogleCalendarOAuthConfiguration(
        client_id=client_id,
        redirect_uri=redirect_uri,
    )


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


def _validated_issuance_snapshot(
    value: object,
    *,
    operation_time: datetime.datetime | None,
) -> tuple[IssuedGoogleCalendarOAuthPKCEState, str, datetime.datetime]:
    if type(value) is not IssuedGoogleCalendarOAuthBrowserBoundPKCEState:
        raise ValueError("invalid state issuance")
    issued_state = value.issued_state
    raw_browser_binding = value.raw_browser_binding
    if type(issued_state) is not IssuedGoogleCalendarOAuthPKCEState:
        raise ValueError("invalid state issuance")

    raw_state = issued_state.raw_state
    code_challenge = issued_state.code_challenge
    expires_at = issued_state.expires_at
    if (
        not _canonical_base64url_32(raw_state)
        or not _canonical_base64url_32(code_challenge)
        or not _canonical_base64url_32(raw_browser_binding)
        or raw_state == raw_browser_binding
        or issued_state.code_challenge_method != "S256"
        or type(expires_at) is not datetime.datetime
        or expires_at.tzinfo is not UTC
        or expires_at.utcoffset() != datetime.timedelta(0)
        or (operation_time is not None and expires_at <= operation_time)
    ):
        raise ValueError("invalid state issuance")
    return (
        IssuedGoogleCalendarOAuthPKCEState(
            raw_state=raw_state,
            expires_at=expires_at,
            code_challenge=code_challenge,
        ),
        raw_browser_binding,
        expires_at,
    )


def _canonical_base64url_32(value: object) -> bool:
    if (
        type(value) is not str
        or _CANONICAL_BASE64URL_32_PATTERN.fullmatch(value) is None
    ):
        return False
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(
            encoded + b"=",
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, ValueError):
        return False
    return (
        len(decoded) == 32
        and base64.urlsafe_b64encode(decoded).rstrip(b"=") == encoded
    )


def _new_start(
    *,
    authorization_request: GoogleCalendarAuthorizationRequest,
    raw_browser_binding: str,
    expires_at: datetime.datetime,
) -> GoogleCalendarAuthorizationStart:
    result = object.__new__(GoogleCalendarAuthorizationStart)
    object.__setattr__(result, "authorization_request", authorization_request)
    object.__setattr__(result, "raw_browser_binding", raw_browser_binding)
    object.__setattr__(result, "expires_at", expires_at)
    return result


def _raise_start_error() -> NoReturn:
    error = GoogleCalendarAuthorizationStartError()
    try:
        raise error from None
    except GoogleCalendarAuthorizationStartError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise
