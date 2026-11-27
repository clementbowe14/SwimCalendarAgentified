"""Strict, transport-injected Google Calendar OAuth token exchanges.

This module is deliberately database-, environment-, and HTTP-client-free.
Callers supply a trusted transport only after committing state or credential
claims and releasing every database lock.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
from decimal import Decimal
from typing import NoReturn, Protocol, TypeAlias, final

from app.calendar_credentials import MAX_REFRESH_TOKEN_BYTES
from app.calendar_oauth_pkce import derive_s256_code_challenge
from app.google_calendar_credential_handoff import (
    GoogleCalendarRefreshCredentialClaim,
    GoogleCalendarValidatedRefreshCredentialClaim,
    validate_google_calendar_refresh_credential_claim,
)
from app.google_calendar_oauth import (
    GoogleCalendarOAuthConfiguration as _GoogleCalendarOAuthConfiguration,
    GoogleCalendarTokenExchangeError,
)
from app.google_calendar_protocol import (
    GOOGLE_CALENDAR_AUTHORIZATION_CODE_MAX_BYTES,
    GOOGLE_CALENDAR_FREEBUSY_SCOPE,
)


__all__ = (
    "GOOGLE_CALENDAR_TOKEN_ENDPOINT",
    "GoogleCalendarTokenExchangeConfiguration",
    "GoogleCalendarTokenExchangeError",
    "GoogleCalendarTokenExchangeResult",
    "GoogleCalendarTokenTransport",
    "GoogleCalendarTokenTransportResponse",
    "exchange_google_calendar_authorization_code",
    "GoogleCalendarRefreshTokenExchangeOutcome",
    "GoogleCalendarRefreshAuthorizationRequired",
    "GoogleCalendarRefreshTokenExchangeResult",
    "exchange_google_calendar_refresh_token",
)


GOOGLE_CALENDAR_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_TOKEN_EXCHANGE_TIMEOUT_SECONDS = 5.0
_MAX_CLIENT_SECRET_BYTES = 4 * 1024
_MAX_ACCESS_TOKEN_BYTES = 16 * 1024
_MAX_SCOPE_BYTES = 4 * 1024
_MAX_RESPONSE_BODY_BYTES = 64 * 1024
_MAX_EXPIRES_IN_SECONDS = 2_147_483_647
_PKCE_BASE64URL_32_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_SUCCESS_TOKEN_RESPONSE_STATUSES = frozenset({200})
_REFRESH_TOKEN_RESPONSE_STATUSES = frozenset({200, 400})
_REFRESH_RESULT_INTEGRITY_KEY = secrets.token_bytes(32)


class _SealedSecretValue:
    """Immutable, non-serializable base for values that carry OAuth secrets."""

    __slots__ = ()

    def __setattr__(self, _name: str, _value: object) -> None:
        del self, _name, _value
        _raise_secret_immutable_error()

    def __delattr__(self, _name: str) -> None:
        del self, _name
        _raise_secret_immutable_error()

    def __reduce__(self) -> NoReturn:
        del self
        _raise_secret_serialization_error()

    def __reduce_ex__(self, _protocol: int) -> NoReturn:
        del self, _protocol
        _raise_secret_serialization_error()

    def __getstate__(self) -> NoReturn:
        del self
        _raise_secret_serialization_error()


@final
class GoogleCalendarTokenExchangeConfiguration(_SealedSecretValue):
    """Validated confidential-client values with redacted representations."""

    __slots__ = ("_client_id", "_redirect_uri", "_client_secret")

    _client_id: str
    _redirect_uri: str
    _client_secret: str

    def __init_subclass__(cls, **_kwargs: object) -> NoReturn:
        del cls, _kwargs
        _raise_secret_subclass_error()

    def __init__(
        self,
        *,
        authorization_configuration: _GoogleCalendarOAuthConfiguration,
        client_secret: str,
    ) -> None:
        # The first traced instruction must already be inside the handler.
        try: return _install_validated_exchange_configuration(self, authorization_configuration, client_secret)
        except asyncio.CancelledError:
            control_exception = "cancelled"
            system_exit_code = None
        except KeyboardInterrupt:
            control_exception = "keyboard_interrupt"
            system_exit_code = None
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
            system_exit_code = None
        except BaseException:
            control_exception = None
            system_exit_code = None
        try: self = authorization_configuration = client_secret = None
        except BaseException as cleanup_raised: self = authorization_configuration = client_secret = None; control_exception, system_exit_code = _classified_control_exception(cleanup_raised)
        if control_exception is not None:
            _raise_control_exception(
                control_exception,
                system_exit_code=system_exit_code,
            )
        _raise_token_exchange_error()

    def __repr__(self) -> str:
        return "GoogleCalendarTokenExchangeConfiguration(<redacted>)"

    __str__ = __repr__


@final
class GoogleCalendarTokenTransportResponse(_SealedSecretValue):
    """Untrusted bounded-response candidate returned by an injected transport."""

    __slots__ = ("status_code", "content_type", "body")

    status_code: int
    content_type: str | None
    body: bytes

    def __init_subclass__(cls, **_kwargs: object) -> NoReturn:
        del cls, _kwargs
        _raise_secret_subclass_error()

    def __init__(
        self,
        *,
        status_code: int,
        content_type: str | None,
        body: bytes,
    ) -> None:
        # The first traced instruction must already be inside the handler.
        try: return _install_validated_transport_response(self, status_code, content_type, body)
        except asyncio.CancelledError:
            control_exception = "cancelled"
            system_exit_code = None
        except KeyboardInterrupt:
            control_exception = "keyboard_interrupt"
            system_exit_code = None
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
            system_exit_code = None
        except BaseException:
            control_exception = None
            system_exit_code = None
        try: self = status_code = content_type = body = None
        except BaseException as cleanup_raised: self = status_code = content_type = body = None; control_exception, system_exit_code = _classified_control_exception(cleanup_raised)
        if control_exception is not None:
            _raise_control_exception(
                control_exception,
                system_exit_code=system_exit_code,
            )
        _raise_token_exchange_error()

    def __repr__(self) -> str:
        return "GoogleCalendarTokenTransportResponse(<redacted>)"

    __str__ = __repr__


class GoogleCalendarTokenTransport(Protocol):
    """Port for one non-redirecting, non-retrying form POST."""

    def post_form(
        self,
        *,
        url: str,
        fields: tuple[tuple[str, str], ...],
        timeout_seconds: float,
    ) -> GoogleCalendarTokenTransportResponse: ...


@final
class GoogleCalendarTokenExchangeResult(_SealedSecretValue):
    """Validated offline credential bound to its exact PKCE exchange."""

    __slots__ = ("refresh_token", "pkce_code_challenge")

    refresh_token: str
    pkce_code_challenge: str
    __init__ = None

    def __init_subclass__(cls, **_kwargs: object) -> NoReturn:
        del cls, _kwargs
        _raise_secret_subclass_error()

    def __repr__(self) -> str:
        return "GoogleCalendarTokenExchangeResult(<redacted>)"

    __str__ = __repr__


@final
class GoogleCalendarRefreshTokenExchangeResult(_SealedSecretValue):
    """Validated access token bound to one exact credential generation."""

    __slots__ = (
        "account_id",
        "instructor_id",
        "connection_id",
        "expected_calendar_connection_revision",
        "access_token",
        "expires_in_seconds",
        "_integrity_tag",
    )

    account_id: str
    instructor_id: str
    connection_id: str
    expected_calendar_connection_revision: int
    access_token: str
    expires_in_seconds: int
    __init__ = None

    def __init_subclass__(cls, **_kwargs: object) -> NoReturn:
        del cls, _kwargs
        _raise_secret_subclass_error()

    def __repr__(self) -> str:
        return "GoogleCalendarRefreshTokenExchangeResult(<redacted>)"

    __str__ = __repr__


@final
class GoogleCalendarRefreshAuthorizationRequired(_SealedSecretValue):
    """Claim-bound recovery outcome for one invalid refresh grant."""

    __slots__ = (
        "account_id",
        "instructor_id",
        "connection_id",
        "expected_calendar_connection_revision",
    )

    account_id: str
    instructor_id: str
    connection_id: str
    expected_calendar_connection_revision: int
    __init__ = None

    def __init_subclass__(cls, **_kwargs: object) -> NoReturn:
        del cls, _kwargs
        _raise_secret_subclass_error()

    def __repr__(self) -> str:
        return "GoogleCalendarRefreshAuthorizationRequired(<redacted>)"

    __str__ = __repr__


GoogleCalendarRefreshTokenExchangeOutcome: TypeAlias = (
    GoogleCalendarRefreshTokenExchangeResult
    | GoogleCalendarRefreshAuthorizationRequired
)


def exchange_google_calendar_authorization_code(
    *,
    configuration: GoogleCalendarTokenExchangeConfiguration,
    authorization_code: str,
    code_verifier: str,
    transport: GoogleCalendarTokenTransport,
) -> GoogleCalendarTokenExchangeResult:
    """Exchange one consumed code/verifier pair without retries or persistence."""

    # The first traced instruction must already be inside the handler.
    try: return _exchange_google_calendar_authorization_code(configuration, authorization_code, code_verifier, transport)
    except asyncio.CancelledError:
        control_exception = "cancelled"
        system_exit_code = None
    except KeyboardInterrupt:
        control_exception = "keyboard_interrupt"
        system_exit_code = None
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
        system_exit_code = None
    except BaseException:
        control_exception = None
        system_exit_code = None
    try: configuration = authorization_code = code_verifier = transport = None
    except BaseException as cleanup_raised: configuration = authorization_code = code_verifier = transport = None; control_exception, system_exit_code = _classified_control_exception(cleanup_raised)
    if control_exception is not None:
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    _raise_token_exchange_error()


def exchange_google_calendar_refresh_token(
    *,
    configuration: GoogleCalendarTokenExchangeConfiguration,
    claim: GoogleCalendarRefreshCredentialClaim,
    transport: GoogleCalendarTokenTransport,
) -> GoogleCalendarRefreshTokenExchangeOutcome:
    """Exchange one claim-bound refresh credential without persistence."""

    # The first traced instruction must already be inside the handler.
    try: return _exchange_google_calendar_refresh_token(configuration, claim, transport)
    except asyncio.CancelledError:
        control_exception = "cancelled"
        system_exit_code = None
    except KeyboardInterrupt:
        control_exception = "keyboard_interrupt"
        system_exit_code = None
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
        system_exit_code = None
    except BaseException:
        control_exception = None
        system_exit_code = None
    try: configuration = claim = transport = None
    except BaseException as cleanup_raised: configuration = claim = transport = None; control_exception, system_exit_code = _classified_control_exception(cleanup_raised)
    if control_exception is not None:
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    _raise_token_exchange_error()


def _exchange_google_calendar_authorization_code(
    configuration: object,
    authorization_code: object,
    code_verifier: object,
    transport: object,
) -> GoogleCalendarTokenExchangeResult:
    fields, pkce_code_challenge = (
        _prepare_google_calendar_authorization_code_exchange(
            configuration=configuration,
            authorization_code=authorization_code,
            code_verifier=code_verifier,
        )
    )
    post_form = getattr(transport, "post_form", None)
    if not callable(post_form):
        raise ValueError("invalid transport")
    response = post_form(
        url=GOOGLE_CALENDAR_TOKEN_ENDPOINT,
        fields=fields,
        timeout_seconds=_TOKEN_EXCHANGE_TIMEOUT_SECONDS,
    )
    return _complete_google_calendar_authorization_code_exchange(
        response,
        pkce_code_challenge=pkce_code_challenge,
    )


def _exchange_google_calendar_refresh_token(
    configuration: object,
    claim: object,
    transport: object,
) -> GoogleCalendarRefreshTokenExchangeOutcome:
    (
        fields,
        original_claim,
        expected_claim,
    ) = _prepare_google_calendar_refresh_token_exchange(
        configuration=configuration,
        claim=claim,
    )
    post_form = getattr(transport, "post_form", None)
    if not callable(post_form):
        raise ValueError("invalid transport")
    response = post_form(
        url=GOOGLE_CALENDAR_TOKEN_ENDPOINT,
        fields=fields,
        timeout_seconds=_TOKEN_EXCHANGE_TIMEOUT_SECONDS,
    )
    return _complete_google_calendar_refresh_token_exchange(
        response,
        claim=original_claim,
        expected_claim=expected_claim,
    )


def _prepare_google_calendar_authorization_code_exchange(
    *,
    configuration: object,
    authorization_code: object,
    code_verifier: object,
) -> tuple[tuple[tuple[str, str], ...], str]:
    client_id, redirect_uri, client_secret = _revalidated_exchange_configuration(
        configuration
    )
    code = _visible_ascii(
        authorization_code,
        maximum_bytes=GOOGLE_CALENDAR_AUTHORIZATION_CODE_MAX_BYTES,
    )
    trusted_verifier = _validated_code_verifier(code_verifier)
    pkce_code_challenge = _verified_pkce_code_challenge(
        derive_s256_code_challenge(trusted_verifier),
        code_verifier=trusted_verifier,
    )
    return (
        (
            ("code", code),
            ("client_id", client_id),
            ("client_secret", client_secret),
            ("redirect_uri", redirect_uri),
            ("grant_type", "authorization_code"),
            ("code_verifier", trusted_verifier),
        ),
        pkce_code_challenge,
    )


def _prepare_google_calendar_refresh_token_exchange(
    *,
    configuration: object,
    claim: object,
) -> tuple[
    tuple[tuple[str, str], ...],
    GoogleCalendarRefreshCredentialClaim,
    GoogleCalendarValidatedRefreshCredentialClaim,
]:
    client_id, _redirect_uri, client_secret = (
        _revalidated_exchange_configuration(configuration)
    )
    if type(claim) is not GoogleCalendarRefreshCredentialClaim:
        raise ValueError("invalid refresh credential claim")
    snapshot = validate_google_calendar_refresh_credential_claim(claim)
    refresh_token = _visible_ascii(
        snapshot.refresh_token,
        maximum_bytes=MAX_REFRESH_TOKEN_BYTES,
    )
    return (
        (
            ("client_id", client_id),
            ("client_secret", client_secret),
            ("grant_type", "refresh_token"),
            ("refresh_token", refresh_token),
        ),
        claim,
        snapshot,
    )


def _complete_google_calendar_authorization_code_exchange(
    response: object,
    *,
    pkce_code_challenge: object,
) -> GoogleCalendarTokenExchangeResult:
    trusted_challenge = _validated_pkce_code_challenge(pkce_code_challenge)
    refresh_token = _validated_refresh_token(response)
    return _new_exchange_result(
        refresh_token=refresh_token,
        pkce_code_challenge=trusted_challenge,
    )


def _complete_google_calendar_refresh_token_exchange(
    response: object,
    *,
    claim: object,
    expected_claim: object,
) -> GoogleCalendarRefreshTokenExchangeOutcome:
    trusted_claim = validate_google_calendar_refresh_credential_claim(claim)
    _require_same_refresh_claim_snapshot(
        expected_claim,
        trusted_claim,
    )
    status_code, payload = _validated_token_response_payload(
        response,
        accepted_statuses=_REFRESH_TOKEN_RESPONSE_STATUSES,
    )
    if status_code == 400:
        if (
            payload.get("error") == "invalid_grant"
            and not _TOKEN_BEARING_RESPONSE_MEMBERS.intersection(payload)
        ):
            return _new_refresh_authorization_required(
                account_id=trusted_claim.account_id,
                instructor_id=trusted_claim.instructor_id,
                connection_id=trusted_claim.connection_id,
                expected_calendar_connection_revision=(
                    trusted_claim.expected_calendar_connection_revision
                ),
            )
        raise ValueError("invalid refresh grant response")
    if (
        status_code != 200
        or "error" in payload
        or "id_token" in payload
        or "refresh_token" in payload
    ):
        raise ValueError("invalid refresh response")

    access_token = _token_value(
        payload.get("access_token"),
        maximum_bytes=_MAX_ACCESS_TOKEN_BYTES,
    )
    if payload.get("token_type") != "Bearer":
        raise ValueError("invalid token type")
    expires_in = payload.get("expires_in")
    if (
        type(expires_in) is not int
        or not 1 <= expires_in <= _MAX_EXPIRES_IN_SECONDS
    ):
        raise ValueError("invalid expiry")
    if "scope" in payload:
        scope = payload["scope"]
        if (
            type(scope) is not str
            or not 1 <= len(scope.encode("utf-8")) <= _MAX_SCOPE_BYTES
            or tuple(scope.split(" "))
            != (GOOGLE_CALENDAR_FREEBUSY_SCOPE,)
        ):
            raise ValueError("invalid granted scope")
    return _new_refresh_exchange_result(
        account_id=trusted_claim.account_id,
        instructor_id=trusted_claim.instructor_id,
        connection_id=trusted_claim.connection_id,
        expected_calendar_connection_revision=(
            trusted_claim.expected_calendar_connection_revision
        ),
        access_token=access_token,
        expires_in_seconds=expires_in,
    )


def _require_same_refresh_claim_snapshot(
    expected: object,
    actual: object,
) -> None:
    if (
        type(expected) is not GoogleCalendarValidatedRefreshCredentialClaim
        or type(actual) is not GoogleCalendarValidatedRefreshCredentialClaim
        or expected.account_id != actual.account_id
        or expected.instructor_id != actual.instructor_id
        or expected.connection_id != actual.connection_id
        or expected.expected_calendar_connection_revision
        != actual.expected_calendar_connection_revision
        or not secrets.compare_digest(
            expected.refresh_token,
            actual.refresh_token,
        )
    ):
        raise ValueError("refresh credential claim changed")


_TOKEN_BEARING_RESPONSE_MEMBERS = frozenset(
    {
        "access_token",
        "id_token",
        "refresh_token",
    }
)


def _validated_token_response_payload(
    response: object,
    *,
    accepted_statuses: frozenset[int],
) -> tuple[int, dict[str, object]]:
    if type(response) is not GoogleCalendarTokenTransportResponse:
        raise ValueError("invalid transport response")
    status_code = response.status_code
    content_type = response.content_type
    body = response.body
    if type(status_code) is not int or status_code not in accepted_statuses:
        raise ValueError("unsuccessful transport response")
    if not _json_content_type(content_type):
        raise ValueError("invalid content type")
    if (
        type(body) is not bytes
        or not 1 <= len(body) <= _MAX_RESPONSE_BODY_BYTES
    ):
        raise ValueError("invalid response body")
    payload = json.loads(
        body.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_json_constant,
        parse_float=_finite_json_decimal,
    )
    if type(payload) is not dict:
        raise ValueError("invalid response payload")
    return status_code, payload


def _validated_code_verifier(value: object) -> str:
    if (
        type(value) is not str
        or _PKCE_BASE64URL_32_PATTERN.fullmatch(value) is None
    ):
        raise ValueError("invalid verifier")
    return value


def _validated_pkce_code_challenge(value: object) -> str:
    if (
        type(value) is not str
        or _PKCE_BASE64URL_32_PATTERN.fullmatch(value) is None
    ):
        raise ValueError("invalid PKCE code challenge")
    return value


def _verified_pkce_code_challenge(
    value: object,
    *,
    code_verifier: str,
) -> str:
    challenge = _validated_pkce_code_challenge(value)
    expected = (
        base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    if challenge == code_verifier or not secrets.compare_digest(
        challenge,
        expected,
    ):
        raise ValueError("invalid PKCE code challenge")
    return challenge


def _validated_configuration_values(
    *,
    authorization_configuration: object,
    client_secret: object,
) -> tuple[str, str, str]:
    if type(authorization_configuration) is not _GoogleCalendarOAuthConfiguration:
        raise ValueError("invalid authorization configuration")
    client_id = authorization_configuration.client_id
    redirect_uri = authorization_configuration.redirect_uri
    if type(client_id) is not str or type(redirect_uri) is not str:
        raise ValueError("invalid authorization configuration")
    _GoogleCalendarOAuthConfiguration(
        client_id=client_id,
        redirect_uri=redirect_uri,
    )
    secret = _visible_ascii(
        client_secret,
        maximum_bytes=_MAX_CLIENT_SECRET_BYTES,
    )
    return client_id, redirect_uri, secret


def _install_validated_exchange_configuration(
    target: object,
    authorization_configuration: object,
    client_secret: object,
) -> None:
    if type(target) is not GoogleCalendarTokenExchangeConfiguration:
        raise ValueError("invalid exchange configuration target")
    client_id, redirect_uri, copied_secret = _validated_configuration_values(
        authorization_configuration=authorization_configuration,
        client_secret=client_secret,
    )
    object.__setattr__(target, "_client_id", client_id)
    object.__setattr__(target, "_redirect_uri", redirect_uri)
    object.__setattr__(target, "_client_secret", copied_secret)


def _install_validated_transport_response(
    target: object,
    status_code: object,
    content_type: object,
    body: object,
) -> None:
    if type(target) is not GoogleCalendarTokenTransportResponse:
        raise ValueError("invalid transport response target")
    object.__setattr__(target, "status_code", status_code)
    object.__setattr__(target, "content_type", content_type)
    object.__setattr__(target, "body", body)


def _revalidated_exchange_configuration(
    configuration: object,
) -> tuple[str, str, str]:
    if type(configuration) is not GoogleCalendarTokenExchangeConfiguration:
        raise ValueError("invalid exchange configuration")
    client_id = configuration._client_id
    redirect_uri = configuration._redirect_uri
    client_secret = configuration._client_secret
    if (
        type(client_id) is not str
        or type(redirect_uri) is not str
        or type(client_secret) is not str
    ):
        raise ValueError("invalid exchange configuration")
    authorization_configuration = _GoogleCalendarOAuthConfiguration(
        client_id=client_id,
        redirect_uri=redirect_uri,
    )
    return _validated_configuration_values(
        authorization_configuration=authorization_configuration,
        client_secret=client_secret,
    )


def _validated_refresh_token(response: object) -> str:
    _status_code, payload = _validated_token_response_payload(
        response,
        accepted_statuses=_SUCCESS_TOKEN_RESPONSE_STATUSES,
    )
    if "error" in payload or "id_token" in payload:
        raise ValueError("invalid response payload")

    _token_value(payload.get("access_token"), maximum_bytes=_MAX_ACCESS_TOKEN_BYTES)
    refresh_token = _token_value(
        payload.get("refresh_token"),
        maximum_bytes=MAX_REFRESH_TOKEN_BYTES,
    )
    if payload.get("token_type") != "Bearer":
        raise ValueError("invalid token type")
    expires_in = payload.get("expires_in")
    if (
        type(expires_in) is not int
        or not 1 <= expires_in <= _MAX_EXPIRES_IN_SECONDS
    ):
        raise ValueError("invalid expiry")
    scope = payload.get("scope")
    if (
        type(scope) is not str
        or not 1 <= len(scope.encode("utf-8")) <= _MAX_SCOPE_BYTES
        or tuple(scope.split(" ")) != (GOOGLE_CALENDAR_FREEBUSY_SCOPE,)
    ):
        raise ValueError("invalid granted scope")
    return refresh_token


def _visible_ascii(value: object, *, maximum_bytes: int) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("invalid text")
    encoded = value.encode("ascii")
    if len(encoded) > maximum_bytes or any(
        byte < 0x21 or byte > 0x7E for byte in encoded
    ):
        raise ValueError("invalid text")
    return value


def _token_value(value: object, *, maximum_bytes: int) -> str:
    return _visible_ascii(value, maximum_bytes=maximum_bytes)


def _json_content_type(value: object) -> bool:
    if type(value) is not str or not value or len(value) > 256:
        return False
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    if any((byte < 0x20 and byte != 0x09) or byte == 0x7F for byte in encoded):
        return False
    parts = tuple(part.strip() for part in value.split(";"))
    if not parts or parts[0].lower() != "application/json":
        return False
    parameters = tuple(part.lower() for part in parts[1:])
    return parameters in ((), ("charset=utf-8",), ('charset="utf-8"',))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("invalid JSON constant")


def _finite_json_decimal(value: str) -> Decimal:
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise ValueError("invalid JSON number")
    return parsed


def _new_exchange_result(
    *,
    refresh_token: str,
    pkce_code_challenge: str,
) -> GoogleCalendarTokenExchangeResult:
    result = object.__new__(GoogleCalendarTokenExchangeResult)
    object.__setattr__(result, "refresh_token", refresh_token)
    object.__setattr__(result, "pkce_code_challenge", pkce_code_challenge)
    return result


def _new_refresh_exchange_result(
    *,
    account_id: str,
    instructor_id: str,
    connection_id: str,
    expected_calendar_connection_revision: int,
    access_token: str,
    expires_in_seconds: int,
) -> GoogleCalendarRefreshTokenExchangeResult:
    integrity_tag = _refresh_exchange_result_integrity_tag(
        account_id=account_id,
        instructor_id=instructor_id,
        connection_id=connection_id,
        expected_revision=expected_calendar_connection_revision,
        access_token=access_token,
        expires_in_seconds=expires_in_seconds,
    )
    result = object.__new__(GoogleCalendarRefreshTokenExchangeResult)
    object.__setattr__(result, "account_id", account_id)
    object.__setattr__(result, "instructor_id", instructor_id)
    object.__setattr__(result, "connection_id", connection_id)
    object.__setattr__(
        result,
        "expected_calendar_connection_revision",
        expected_calendar_connection_revision,
    )
    object.__setattr__(result, "access_token", access_token)
    object.__setattr__(result, "expires_in_seconds", expires_in_seconds)
    object.__setattr__(result, "_integrity_tag", integrity_tag)
    return result


def _refresh_exchange_result_integrity_matches(
    value: object,
    *,
    account_id: str,
    instructor_id: str,
    connection_id: str,
    expected_revision: int,
    access_token: str,
    expires_in_seconds: int,
) -> bool:
    """Authenticate one validated field snapshot without exposing its tag."""

    if type(value) is not GoogleCalendarRefreshTokenExchangeResult:
        return False
    try:
        integrity_tag = value._integrity_tag
        expected_tag = _refresh_exchange_result_integrity_tag(
            account_id=account_id,
            instructor_id=instructor_id,
            connection_id=connection_id,
            expected_revision=expected_revision,
            access_token=access_token,
            expires_in_seconds=expires_in_seconds,
        )
    except (AttributeError, OverflowError, TypeError, UnicodeEncodeError):
        return False
    return (
        type(integrity_tag) is bytes
        and len(integrity_tag) == hashlib.sha256().digest_size
        and hmac.compare_digest(integrity_tag, expected_tag)
    )


def _refresh_exchange_result_integrity_tag(
    *,
    account_id: str,
    instructor_id: str,
    connection_id: str,
    expected_revision: int,
    access_token: str,
    expires_in_seconds: int,
) -> bytes:
    parts = (
        account_id.encode("ascii"),
        instructor_id.encode("utf-8"),
        connection_id.encode("ascii"),
        expected_revision.to_bytes(8, "big", signed=False),
        access_token.encode("ascii"),
        expires_in_seconds.to_bytes(8, "big", signed=False),
    )
    message = b"".join(
        len(part).to_bytes(4, "big", signed=False) + part
        for part in parts
    )
    return hmac.new(
        _REFRESH_RESULT_INTEGRITY_KEY,
        message,
        hashlib.sha256,
    ).digest()


def _new_refresh_authorization_required(
    *,
    account_id: str,
    instructor_id: str,
    connection_id: str,
    expected_calendar_connection_revision: int,
) -> GoogleCalendarRefreshAuthorizationRequired:
    result = object.__new__(GoogleCalendarRefreshAuthorizationRequired)
    object.__setattr__(result, "account_id", account_id)
    object.__setattr__(result, "instructor_id", instructor_id)
    object.__setattr__(result, "connection_id", connection_id)
    object.__setattr__(
        result,
        "expected_calendar_connection_revision",
        expected_calendar_connection_revision,
    )
    return result


def _raise_token_exchange_error() -> NoReturn:
    error = GoogleCalendarTokenExchangeError()
    try:
        raise error from None
    except GoogleCalendarTokenExchangeError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_secret_immutable_error() -> NoReturn:
    error = AttributeError("OAuth secret values are immutable.")
    try:
        raise error from None
    except AttributeError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_secret_serialization_error() -> NoReturn:
    error = TypeError("OAuth secret values cannot be serialized.")
    try:
        raise error from None
    except TypeError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_secret_subclass_error() -> NoReturn:
    error = TypeError("OAuth secret values cannot be subclassed.")
    try:
        raise error from None
    except TypeError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _classified_control_exception(
    raised: BaseException,
) -> tuple[str | None, int | None]:
    if isinstance(raised, asyncio.CancelledError):
        return "cancelled", None
    if isinstance(raised, KeyboardInterrupt):
        return "keyboard_interrupt", None
    if isinstance(raised, SystemExit):
        if type(raised) is not SystemExit:
            return "system_exit", 1
        code = raised.code
        return "system_exit", code if type(code) is int or code is None else 1
    if isinstance(raised, GeneratorExit):
        return "generator_exit", None
    return None, None


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
        _raise_token_exchange_error()
    try:
        raise error from None
    except BaseException as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise
