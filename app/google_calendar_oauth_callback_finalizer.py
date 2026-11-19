"""Fresh-transaction finalization for a claimed Google Calendar callback."""

from __future__ import annotations

import base64
import datetime
import hashlib
import re
import secrets
from collections.abc import Callable
from typing import NoReturn

from sqlalchemy.engine import Engine as _Engine
from sqlalchemy.orm import Session as _Session

from app.calendar_connections import (
    CalendarConnectionStatus,
    replace_google_calendar_connection,
)
from app.calendar_credentials import (
    MAX_REFRESH_TOKEN_BYTES,
    CalendarCredentialCipher,
)
from app.calendar_oauth_pkce import derive_s256_code_challenge
from app.google_calendar_oauth import GoogleCalendarOAuthError
from app.google_calendar_oauth_callback_claim import (
    GoogleCalendarOAuthCallbackClaim,
)
from app.google_calendar_token_exchange import (
    GoogleCalendarTokenExchangeResult,
)
from app.models import (
    ProviderAccountDB,
    ProviderCalendarConnectionDB,
    ProviderCalendarOAuthPKCEVerifierDB,
    ProviderCalendarOAuthStateDB,
    ProviderSessionDB,
)


__all__ = (
    "GoogleCalendarOAuthCallbackFinalizationError",
    "finalize_google_calendar_oauth_callback",
)


UTC = datetime.timezone.utc
_CANONICAL_BASE64URL_32_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_MAX_IDENTIFIER_BYTES = 255
_POSTGRESQL_BIGINT_MAX = 9_223_372_036_854_775_807
_FINALIZATION_MODELS = (
    ProviderAccountDB,
    ProviderSessionDB,
    ProviderCalendarConnectionDB,
    ProviderCalendarOAuthStateDB,
    ProviderCalendarOAuthPKCEVerifierDB,
)


class GoogleCalendarOAuthCallbackFinalizationError(GoogleCalendarOAuthError):
    """A claimed callback could not install its credential safely."""

    def __init__(self) -> None:
        super().__init__("Google Calendar authorization could not be finalized.")


def finalize_google_calendar_oauth_callback(
    session_factory: Callable[[], _Session],
    *,
    claim: GoogleCalendarOAuthCallbackClaim,
    token_result: GoogleCalendarTokenExchangeResult,
    credential_cipher: CalendarCredentialCipher,
) -> CalendarConnectionStatus:
    """Install one claim-bound refresh credential in a fresh transaction."""

    try:
        return _finalize_google_calendar_oauth_callback(
            session_factory,
            claim=claim,
            token_result=token_result,
            credential_cipher=credential_cipher,
        )
    except Exception:
        pass
    del session_factory, claim, token_result, credential_cipher
    _raise_finalization_error()


def _finalize_google_calendar_oauth_callback(
    session_factory: object,
    *,
    claim: object,
    token_result: object,
    credential_cipher: object,
) -> CalendarConnectionStatus:
    (
        account_id,
        session_id,
        expected_revision,
        refresh_token,
    ) = _validated_handoff(
        claim=claim,
        token_result=token_result,
        credential_cipher=credential_cipher,
    )
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
        for model in _FINALIZATION_MODELS:
            if database_session.get_bind(mapper=model) is not database_bind:
                raise ValueError("invalid database session")

        with database_session.begin():
            _require_transactional_connection(database_session)
            finalized_at = _lock_and_validate_owner(
                database_session,
                account_id=account_id,
                session_id=session_id,
                expected_revision=expected_revision,
            )
            status = replace_google_calendar_connection(
                database_session,
                account_id=account_id,
                refresh_token=refresh_token,
                cipher=credential_cipher,
                now=finalized_at,
            )
            if type(status) is not CalendarConnectionStatus:
                raise ValueError("invalid connection status")
    finally:
        database_session.close()
    return status


def _validated_handoff(
    *,
    claim: object,
    token_result: object,
    credential_cipher: object,
) -> tuple[str, str, int, str]:
    if (
        type(claim) is not GoogleCalendarOAuthCallbackClaim
        or type(token_result) is not GoogleCalendarTokenExchangeResult
        or type(credential_cipher) is not CalendarCredentialCipher
    ):
        raise ValueError("invalid callback handoff")

    account_id = _validated_identifier(claim.account_id)
    session_id = _validated_identifier(claim.session_id)
    code_verifier = _validated_base64url_32(claim.code_verifier)
    expected_revision = claim.expected_calendar_connection_revision
    if (
        type(expected_revision) is not int
        or not 1 <= expected_revision <= _POSTGRESQL_BIGINT_MAX
    ):
        raise ValueError("invalid callback revision")

    refresh_token = _validated_refresh_token(token_result.refresh_token)
    result_challenge = _validated_base64url_32(
        token_result.pkce_code_challenge
    )
    canonical_challenge = _verified_pkce_code_challenge(code_verifier)
    if not secrets.compare_digest(result_challenge, canonical_challenge):
        raise ValueError("crossed callback handoff")
    return account_id, session_id, expected_revision, refresh_token


def _verified_pkce_code_challenge(code_verifier: str) -> str:
    derived = _validated_base64url_32(
        derive_s256_code_challenge(code_verifier)
    )
    expected = (
        base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    if derived == code_verifier or not secrets.compare_digest(
        derived,
        expected,
    ):
        raise ValueError("invalid PKCE derivation")
    return derived


def _lock_and_validate_owner(
    session: _Session,
    *,
    account_id: str,
    session_id: str,
    expected_revision: int,
) -> datetime.datetime:
    account = (
        session.query(ProviderAccountDB)
        .filter(ProviderAccountDB.id == account_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if account is None:
        raise ValueError("missing callback owner")
    provider_session = (
        session.query(ProviderSessionDB)
        .filter(
            ProviderSessionDB.account_id == account.id,
            ProviderSessionDB.id == session_id,
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if provider_session is None:
        raise ValueError("missing callback session")

    finalized_at = _validated_utc_time(_utc_now())
    created_at = _validated_utc_time(provider_session.created_at)
    expires_at = _validated_utc_time(provider_session.expires_at)
    account_session_version = account.session_version
    provider_session_version = provider_session.session_version
    current_revision = account.calendar_connection_revision
    if (
        account.disabled_at is not None
        or provider_session.revoked_at is not None
        or type(account_session_version) is not int
        or type(provider_session_version) is not int
        or account_session_version < 1
        or provider_session_version != account_session_version
        or created_at > finalized_at
        or expires_at <= finalized_at
        or type(current_revision) is not int
        or current_revision != expected_revision
        or current_revision >= _POSTGRESQL_BIGINT_MAX
    ):
        raise ValueError("inactive or stale callback owner")
    return finalized_at


def _require_transactional_connection(session: _Session) -> None:
    connection = session.connection()
    configured_isolation = connection.get_execution_options().get(
        "isolation_level"
    )
    proxied_connection = connection.connection
    driver_connection = getattr(
        proxied_connection,
        "driver_connection",
        proxied_connection,
    )
    driver_autocommit = getattr(driver_connection, "autocommit", None)
    if (
        type(configured_isolation) is str
        and configured_isolation.upper() == "AUTOCOMMIT"
    ) or driver_autocommit is True:
        raise ValueError("autocommit database session")


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


def _validated_base64url_32(value: object) -> str:
    if (
        type(value) is not str
        or _CANONICAL_BASE64URL_32_PATTERN.fullmatch(value) is None
    ):
        raise ValueError("invalid PKCE value")
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(
            encoded + b"=",
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, ValueError):
        raise ValueError("invalid PKCE value") from None
    if (
        len(decoded) != 32
        or base64.urlsafe_b64encode(decoded).rstrip(b"=") != encoded
    ):
        raise ValueError("invalid PKCE value")
    return value


def _validated_refresh_token(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("invalid refresh token")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("invalid refresh token") from None
    if len(encoded) > MAX_REFRESH_TOKEN_BYTES or any(
        byte < 0x21 or byte > 0x7E for byte in encoded
    ):
        raise ValueError("invalid refresh token")
    return value


def _validated_utc_time(value: object) -> datetime.datetime:
    if (
        type(value) is not datetime.datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("invalid operation time")
    return value.astimezone(UTC)


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def _raise_finalization_error() -> NoReturn:
    error = GoogleCalendarOAuthCallbackFinalizationError()
    try:
        raise error from None
    except GoogleCalendarOAuthCallbackFinalizationError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise
