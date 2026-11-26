"""Database-free sealed handoff for one Google refresh credential."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
from typing import NoReturn, final

from app.calendar_credentials import MAX_REFRESH_TOKEN_BYTES
from app.google_calendar_oauth import GoogleCalendarOAuthError


__all__ = (
    "GoogleCalendarRefreshCredentialClaim",
    "GoogleCalendarRefreshCredentialClaimError",
    "GoogleCalendarValidatedRefreshCredentialClaim",
    "validate_google_calendar_refresh_credential_claim",
)


_CONNECTION_ID_PATTERN = re.compile(r"calconn_[0-9a-f]{32}\Z")
_MAX_IDENTIFIER_BYTES = 255
_POSTGRESQL_BIGINT_MAX = 9_223_372_036_854_775_807
_CLAIM_INTEGRITY_KEY = secrets.token_bytes(32)


class GoogleCalendarRefreshCredentialClaimError(GoogleCalendarOAuthError):
    """A stored Google credential could not be claimed safely."""

    def __init__(self) -> None:
        super().__init__(
            "Google Calendar refresh credential could not be claimed safely."
        )


class _SealedClaimValue:
    """Immutable, non-serializable base for a secret-bearing handoff."""

    __slots__ = ()

    def __setattr__(self, _name: str, _value: object) -> None:
        del self, _name, _value
        _raise_immutable_error()

    def __delattr__(self, _name: str) -> None:
        del self, _name
        _raise_immutable_error()

    def __reduce__(self) -> NoReturn:
        del self
        _raise_serialization_error()

    def __reduce_ex__(self, _protocol: int) -> NoReturn:
        del self, _protocol
        _raise_serialization_error()

    def __getstate__(self) -> NoReturn:
        del self
        _raise_serialization_error()


@final
class GoogleCalendarRefreshCredentialClaim(_SealedClaimValue):
    """One decrypted credential generation awaiting freshness validation."""

    __slots__ = (
        "account_id",
        "instructor_id",
        "connection_id",
        "expected_calendar_connection_revision",
        "refresh_token",
        "_integrity_tag",
    )

    account_id: str
    instructor_id: str
    connection_id: str
    expected_calendar_connection_revision: int
    refresh_token: str
    _integrity_tag: bytes

    def __init_subclass__(cls, **_kwargs: object) -> NoReturn:
        del cls, _kwargs
        _raise_subclass_error()

    def __init__(
        self,
        *,
        account_id: object,
        instructor_id: object,
        connection_id: object,
        expected_calendar_connection_revision: object,
        refresh_token: object,
    ) -> None:
        del (
            account_id,
            instructor_id,
            connection_id,
            expected_calendar_connection_revision,
            refresh_token,
        )
        _raise_claim_error()

    def __repr__(self) -> str:
        return "GoogleCalendarRefreshCredentialClaim(<redacted>)"

    __str__ = __repr__


@final
class GoogleCalendarValidatedRefreshCredentialClaim(_SealedClaimValue):
    """Copied, integrity-checked values for one immediate operation."""

    __slots__ = (
        "account_id",
        "instructor_id",
        "connection_id",
        "expected_calendar_connection_revision",
        "refresh_token",
    )

    account_id: str
    instructor_id: str
    connection_id: str
    expected_calendar_connection_revision: int
    refresh_token: str

    def __init_subclass__(cls, **_kwargs: object) -> NoReturn:
        del cls, _kwargs
        _raise_subclass_error()

    def __init__(
        self,
        *,
        account_id: object,
        instructor_id: object,
        connection_id: object,
        expected_calendar_connection_revision: object,
        refresh_token: object,
    ) -> None:
        del (
            account_id,
            instructor_id,
            connection_id,
            expected_calendar_connection_revision,
            refresh_token,
        )
        _raise_claim_error()

    def __repr__(self) -> str:
        return "GoogleCalendarValidatedRefreshCredentialClaim(<redacted>)"

    __str__ = __repr__


def validate_google_calendar_refresh_credential_claim(
    value: object,
) -> GoogleCalendarValidatedRefreshCredentialClaim:
    """Copy one exact, intact claim or reject it without exposing detail."""

    control_exception: str | None = None
    system_exit_code: int | None = None
    try:
        return _validated_google_calendar_refresh_credential_claim(value)
    except asyncio.CancelledError:
        control_exception = "cancelled"
    except KeyboardInterrupt:
        control_exception = "keyboard_interrupt"
    except SystemExit as raised:
        control_exception = "system_exit"
        if type(raised) is SystemExit:
            system_exit_code = (
                raised.code
                if type(raised.code) is int or raised.code is None
                else 1
            )
        else:
            system_exit_code = 1
    except GeneratorExit:
        control_exception = "generator_exit"
    except BaseException:
        pass
    del value
    if control_exception is not None:
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    _raise_claim_error()


def _validated_google_calendar_refresh_credential_claim(
    value: object,
) -> GoogleCalendarValidatedRefreshCredentialClaim:
    if type(value) is not GoogleCalendarRefreshCredentialClaim:
        raise ValueError("invalid credential claim")
    account_id = _validated_identifier(value.account_id)
    instructor_id = _validated_instructor_id(value.instructor_id)
    connection_id = _validated_connection_id(value.connection_id)
    expected_revision = _validated_revision(
        value.expected_calendar_connection_revision,
        allow_zero=False,
    )
    refresh_token = _validated_refresh_token(value.refresh_token)
    integrity_tag = value._integrity_tag
    if type(integrity_tag) is not bytes or len(integrity_tag) != 32:
        raise ValueError("invalid credential claim integrity")
    expected_tag = _claim_integrity_tag(
        account_id=account_id,
        instructor_id=instructor_id,
        connection_id=connection_id,
        expected_revision=expected_revision,
        refresh_token=refresh_token,
    )
    if not hmac.compare_digest(integrity_tag, expected_tag):
        raise ValueError("invalid credential claim integrity")
    return _new_validated_claim(
        account_id=account_id,
        instructor_id=instructor_id,
        connection_id=connection_id,
        expected_revision=expected_revision,
        refresh_token=refresh_token,
    )


def _validated_claim_fence(value: object) -> tuple[str, str, str, int]:
    snapshot = validate_google_calendar_refresh_credential_claim(value)
    return (
        snapshot.account_id,
        snapshot.instructor_id,
        snapshot.connection_id,
        snapshot.expected_calendar_connection_revision,
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


def _validated_instructor_id(value: object) -> str:
    # This is an existing database key, separate from Google's identifiers.
    if (
        type(value) is not str
        or not 1 <= len(value) <= _MAX_IDENTIFIER_BYTES
        or value != value.strip()
        or not value.isprintable()
        or len(value.encode("utf-8")) > _MAX_IDENTIFIER_BYTES
    ):
        raise ValueError("invalid instructor identifier")
    return value


def _validated_connection_id(value: object) -> str:
    if (
        type(value) is not str
        or _CONNECTION_ID_PATTERN.fullmatch(value) is None
    ):
        raise ValueError("invalid connection identifier")
    return value


def _validated_revision(value: object, *, allow_zero: bool) -> int:
    minimum = 0 if allow_zero else 1
    if (
        type(value) is not int
        or not minimum <= value <= _POSTGRESQL_BIGINT_MAX
    ):
        raise ValueError("invalid connection revision")
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


def _claim_integrity_tag(
    *,
    account_id: str,
    instructor_id: str,
    connection_id: str,
    expected_revision: int,
    refresh_token: str,
) -> bytes:
    parts = (
        account_id.encode("ascii"),
        instructor_id.encode("utf-8"),
        connection_id.encode("ascii"),
        expected_revision.to_bytes(8, "big", signed=False),
        refresh_token.encode("ascii"),
    )
    message = b"".join(
        len(part).to_bytes(4, "big", signed=False) + part
        for part in parts
    )
    return hmac.new(
        _CLAIM_INTEGRITY_KEY,
        message,
        hashlib.sha256,
    ).digest()


def _new_claim(
    *,
    account_id: str,
    instructor_id: str,
    connection_id: str,
    expected_calendar_connection_revision: int,
    refresh_token: str,
) -> GoogleCalendarRefreshCredentialClaim:
    trusted_account_id = _validated_identifier(account_id)
    trusted_instructor_id = _validated_instructor_id(instructor_id)
    trusted_connection_id = _validated_connection_id(connection_id)
    trusted_revision = _validated_revision(
        expected_calendar_connection_revision,
        allow_zero=False,
    )
    trusted_refresh_token = _validated_refresh_token(refresh_token)
    integrity_tag = _claim_integrity_tag(
        account_id=trusted_account_id,
        instructor_id=trusted_instructor_id,
        connection_id=trusted_connection_id,
        expected_revision=trusted_revision,
        refresh_token=trusted_refresh_token,
    )
    result = object.__new__(GoogleCalendarRefreshCredentialClaim)
    object.__setattr__(result, "account_id", trusted_account_id)
    object.__setattr__(result, "instructor_id", trusted_instructor_id)
    object.__setattr__(result, "connection_id", trusted_connection_id)
    object.__setattr__(
        result,
        "expected_calendar_connection_revision",
        trusted_revision,
    )
    object.__setattr__(result, "refresh_token", trusted_refresh_token)
    object.__setattr__(result, "_integrity_tag", integrity_tag)
    return result


def _new_validated_claim(
    *,
    account_id: str,
    instructor_id: str,
    connection_id: str,
    expected_revision: int,
    refresh_token: str,
) -> GoogleCalendarValidatedRefreshCredentialClaim:
    result = object.__new__(GoogleCalendarValidatedRefreshCredentialClaim)
    object.__setattr__(result, "account_id", account_id)
    object.__setattr__(result, "instructor_id", instructor_id)
    object.__setattr__(result, "connection_id", connection_id)
    object.__setattr__(
        result,
        "expected_calendar_connection_revision",
        expected_revision,
    )
    object.__setattr__(result, "refresh_token", refresh_token)
    return result


def _raise_claim_error() -> NoReturn:
    error = GoogleCalendarRefreshCredentialClaimError()
    try:
        raise error from None
    except GoogleCalendarRefreshCredentialClaimError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_immutable_error() -> NoReturn:
    error = AttributeError(
        "Google Calendar refresh credential claims are immutable."
    )
    try:
        raise error from None
    except AttributeError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_serialization_error() -> NoReturn:
    error = TypeError(
        "Google Calendar refresh credential claims cannot be serialized."
    )
    try:
        raise error from None
    except TypeError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_subclass_error() -> NoReturn:
    error = TypeError(
        "Google Calendar refresh credential claims cannot be subclassed."
    )
    try:
        raise error from None
    except TypeError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_control_exception(
    kind: str,
    *,
    system_exit_code: int | None,
) -> NoReturn:
    if kind == "cancelled":
        error: BaseException = asyncio.CancelledError()
    elif kind == "keyboard_interrupt":
        error = KeyboardInterrupt()
    elif kind == "system_exit":
        error = SystemExit(system_exit_code)
    elif kind == "generator_exit":
        error = GeneratorExit()
    else:
        _raise_claim_error()
    try:
        raise error from None
    except BaseException as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise
