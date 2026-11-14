"""Strict, transport-injected Google Calendar authorization-code exchange.

This module is deliberately database-, environment-, and HTTP-client-free. A
later callback service supplies a trusted transport after committing one-time
OAuth state consumption and releasing every database lock.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import NoReturn, Protocol, final

from app.calendar_credentials import MAX_REFRESH_TOKEN_BYTES
from app.calendar_oauth_pkce import derive_s256_code_challenge
from app.google_calendar_authorization import GOOGLE_CALENDAR_FREEBUSY_SCOPE
from app.google_calendar_oauth import (
    GoogleCalendarOAuthConfiguration as _GoogleCalendarOAuthConfiguration,
    GoogleCalendarTokenExchangeError,
)


__all__ = (
    "GOOGLE_CALENDAR_TOKEN_ENDPOINT",
    "GoogleCalendarTokenExchangeConfiguration",
    "GoogleCalendarTokenExchangeError",
    "GoogleCalendarTokenExchangeResult",
    "GoogleCalendarTokenTransport",
    "GoogleCalendarTokenTransportResponse",
    "exchange_google_calendar_authorization_code",
)


GOOGLE_CALENDAR_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_TOKEN_EXCHANGE_TIMEOUT_SECONDS = 5.0
_MAX_CLIENT_SECRET_BYTES = 4 * 1024
_MAX_AUTHORIZATION_CODE_BYTES = 8 * 1024
_MAX_ACCESS_TOKEN_BYTES = 16 * 1024
_MAX_SCOPE_BYTES = 4 * 1024
_MAX_RESPONSE_BODY_BYTES = 64 * 1024
_MAX_EXPIRES_IN_SECONDS = 2_147_483_647


class _SealedSecretValue:
    """Immutable, non-serializable base for values that carry OAuth secrets."""

    __slots__ = ()

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("OAuth secret values are immutable.")

    def __delattr__(self, _name: str) -> None:
        raise AttributeError("OAuth secret values are immutable.")

    def __reduce__(self) -> NoReturn:
        raise TypeError("OAuth secret values cannot be serialized.")

    def __reduce_ex__(self, _protocol: int) -> NoReturn:
        raise TypeError("OAuth secret values cannot be serialized.")

    def __getstate__(self) -> NoReturn:
        raise TypeError("OAuth secret values cannot be serialized.")


@final
class GoogleCalendarTokenExchangeConfiguration(_SealedSecretValue):
    """Validated confidential-client values with redacted representations."""

    __slots__ = ("_client_id", "_redirect_uri", "_client_secret")

    _client_id: str
    _redirect_uri: str
    _client_secret: str

    def __init_subclass__(cls, **_kwargs: object) -> NoReturn:
        raise TypeError("OAuth secret values cannot be subclassed.")

    def __init__(
        self,
        *,
        authorization_configuration: _GoogleCalendarOAuthConfiguration,
        client_secret: str,
    ) -> None:
        try:
            client_id, redirect_uri, copied_secret = _validated_configuration_values(
                authorization_configuration=authorization_configuration,
                client_secret=client_secret,
            )
        except Exception:
            pass
        else:
            object.__setattr__(self, "_client_id", client_id)
            object.__setattr__(self, "_redirect_uri", redirect_uri)
            object.__setattr__(self, "_client_secret", copied_secret)
            return
        del authorization_configuration, client_secret
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
        raise TypeError("OAuth secret values cannot be subclassed.")

    def __init__(
        self,
        *,
        status_code: int,
        content_type: str | None,
        body: bytes,
    ) -> None:
        object.__setattr__(self, "status_code", status_code)
        object.__setattr__(self, "content_type", content_type)
        object.__setattr__(self, "body", body)

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
    """Validated offline credential; access-token material is discarded."""

    __slots__ = ("refresh_token",)

    refresh_token: str

    def __init_subclass__(cls, **_kwargs: object) -> NoReturn:
        raise TypeError("OAuth secret values cannot be subclassed.")

    def __init__(self, *, refresh_token: object) -> None:
        del refresh_token
        _raise_token_exchange_error()

    def __repr__(self) -> str:
        return "GoogleCalendarTokenExchangeResult(<redacted>)"

    __str__ = __repr__


def exchange_google_calendar_authorization_code(
    *,
    configuration: GoogleCalendarTokenExchangeConfiguration,
    authorization_code: str,
    code_verifier: str,
    transport: GoogleCalendarTokenTransport,
) -> GoogleCalendarTokenExchangeResult:
    """Exchange one consumed code/verifier pair without retries or persistence."""

    try:
        return _exchange_google_calendar_authorization_code(
            configuration=configuration,
            authorization_code=authorization_code,
            code_verifier=code_verifier,
            transport=transport,
        )
    except Exception:
        pass
    del configuration, authorization_code, code_verifier, transport
    _raise_token_exchange_error()


def _exchange_google_calendar_authorization_code(
    *,
    configuration: object,
    authorization_code: object,
    code_verifier: object,
    transport: object,
) -> GoogleCalendarTokenExchangeResult:
    client_id, redirect_uri, client_secret = _revalidated_exchange_configuration(
        configuration
    )
    code = _visible_ascii(
        authorization_code,
        maximum_bytes=_MAX_AUTHORIZATION_CODE_BYTES,
    )
    if type(code_verifier) is not str:
        raise ValueError("invalid verifier")
    derive_s256_code_challenge(code_verifier)

    post_form = getattr(transport, "post_form", None)
    if not callable(post_form):
        raise ValueError("invalid transport")
    fields = (
        ("code", code),
        ("client_id", client_id),
        ("client_secret", client_secret),
        ("redirect_uri", redirect_uri),
        ("grant_type", "authorization_code"),
        ("code_verifier", code_verifier),
    )
    response = post_form(
        url=GOOGLE_CALENDAR_TOKEN_ENDPOINT,
        fields=fields,
        timeout_seconds=_TOKEN_EXCHANGE_TIMEOUT_SECONDS,
    )
    refresh_token = _validated_refresh_token(response)
    return _new_exchange_result(refresh_token=refresh_token)


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
    if type(response) is not GoogleCalendarTokenTransportResponse:
        raise ValueError("invalid transport response")
    status_code = response.status_code
    content_type = response.content_type
    body = response.body
    if type(status_code) is not int or status_code != 200:
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
    if type(payload) is not dict or "error" in payload or "id_token" in payload:
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


def _new_exchange_result(*, refresh_token: str) -> GoogleCalendarTokenExchangeResult:
    result = object.__new__(GoogleCalendarTokenExchangeResult)
    object.__setattr__(result, "refresh_token", refresh_token)
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
