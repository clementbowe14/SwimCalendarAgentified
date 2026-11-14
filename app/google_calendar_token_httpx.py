"""Bounded HTTPX transport for one Google Calendar token exchange."""

from __future__ import annotations

import ssl
from typing import NoReturn, final
from urllib.parse import quote_plus, urlencode

import certifi
import httpx

from app.google_calendar_oauth import GoogleCalendarTokenExchangeError
from app.google_calendar_token_exchange import (
    GOOGLE_CALENDAR_TOKEN_ENDPOINT,
    GoogleCalendarTokenTransportResponse,
)


__all__ = ("HttpxGoogleCalendarTokenTransport",)


_TOKEN_EXCHANGE_TIMEOUT_SECONDS = 5.0
_MAX_REQUEST_BODY_BYTES = 64 * 1024
_MAX_RESPONSE_BODY_BYTES = 64 * 1024
_RESPONSE_CHUNK_BYTES = 8 * 1024
_FIELD_NAMES = (
    "code",
    "client_id",
    "client_secret",
    "redirect_uri",
    "grant_type",
    "code_verifier",
)
_FIELD_MAXIMUM_BYTES = (
    8 * 1024,
    512,
    4 * 1024,
    2 * 1024,
    len("authorization_code"),
    43,
)


@final
class HttpxGoogleCalendarTokenTransport:
    """Perform one fixed, non-redirecting and non-retrying token POST."""

    __slots__ = ()

    def __init_subclass__(cls, **_kwargs: object) -> NoReturn:
        raise TypeError("Google Calendar token transports cannot be subclassed.")

    def post_form(
        self,
        *,
        url: str,
        fields: tuple[tuple[str, str], ...],
        timeout_seconds: float,
    ) -> GoogleCalendarTokenTransportResponse:
        try:
            return _post_form(
                url=url,
                fields=fields,
                timeout_seconds=timeout_seconds,
            )
        except Exception:
            pass
        del url, fields, timeout_seconds
        _raise_token_exchange_error()


def _post_form(
    *,
    url: object,
    fields: object,
    timeout_seconds: object,
) -> GoogleCalendarTokenTransportResponse:
    encoded_form = _validated_encoded_form(
        url=url,
        fields=fields,
        timeout_seconds=timeout_seconds,
    )
    client = _new_http_client(timeout_seconds=_TOKEN_EXCHANGE_TIMEOUT_SECONDS)
    try:
        return _stream_response(client=client, encoded_form=encoded_form)
    finally:
        client.close()


def _validated_encoded_form(
    *,
    url: object,
    fields: object,
    timeout_seconds: object,
) -> bytes:
    if type(url) is not str or url != GOOGLE_CALENDAR_TOKEN_ENDPOINT:
        raise ValueError("invalid token endpoint")
    if (
        type(timeout_seconds) is not float
        or timeout_seconds != _TOKEN_EXCHANGE_TIMEOUT_SECONDS
    ):
        raise ValueError("invalid timeout")
    if type(fields) is not tuple or len(fields) != len(_FIELD_NAMES):
        raise ValueError("invalid token fields")

    validated_fields: list[tuple[str, str]] = []
    for expected_name, maximum_bytes, field in zip(
        _FIELD_NAMES,
        _FIELD_MAXIMUM_BYTES,
        fields,
        strict=True,
    ):
        if type(field) is not tuple or len(field) != 2:
            raise ValueError("invalid token field")
        name, value = field
        if (
            type(name) is not str
            or name != expected_name
            or type(value) is not str
            or not 1 <= len(value) <= maximum_bytes
            or not _visible_ascii(value)
        ):
            raise ValueError("invalid token field")
        validated_fields.append((name, value))

    if validated_fields[4][1] != "authorization_code":
        raise ValueError("invalid grant type")
    encoded = urlencode(
        tuple(validated_fields),
        doseq=False,
        safe="",
        encoding="ascii",
        errors="strict",
        quote_via=quote_plus,
    ).encode("ascii")
    if not 1 <= len(encoded) <= _MAX_REQUEST_BODY_BYTES:
        raise ValueError("invalid token form size")
    return encoded


def _visible_ascii(value: object) -> bool:
    if type(value) is not str or not value or value != value.strip():
        return False
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return all(0x21 <= byte <= 0x7E for byte in encoded)


def _new_http_transport() -> httpx.BaseTransport:
    return httpx.HTTPTransport(
        verify=_new_ssl_context(),
        trust_env=False,
        proxy=None,
        http1=True,
        http2=False,
        retries=0,
    )


def _new_ssl_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=certifi.where())
    if hasattr(context, "keylog_filename"):
        context.keylog_filename = None
    return context


def _new_http_client(*, timeout_seconds: float) -> httpx.Client:
    transport = _new_http_transport()
    try:
        return httpx.Client(
            transport=transport,
            trust_env=False,
            proxy=None,
            http1=True,
            http2=False,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds),
        )
    except Exception:
        try:
            transport.close()
        except Exception:
            pass
        raise


def _stream_response(
    *,
    client: httpx.Client,
    encoded_form: bytes,
) -> GoogleCalendarTokenTransportResponse:
    with client.stream(
        "POST",
        GOOGLE_CALENDAR_TOKEN_ENDPOINT,
        content=encoded_form,
        params=None,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        cookies=None,
        auth=None,
        follow_redirects=False,
        timeout=httpx.Timeout(_TOKEN_EXCHANGE_TIMEOUT_SECONDS),
    ) as response:
        return _bounded_response(response)


def _bounded_response(
    response: httpx.Response,
) -> GoogleCalendarTokenTransportResponse:
    status_code = response.status_code
    if type(status_code) is not int:
        raise ValueError("invalid response status")
    if status_code != 200:
        return GoogleCalendarTokenTransportResponse(
            status_code=status_code,
            content_type=None,
            body=b"",
        )

    _validate_identity_encoding(response.headers)
    declared_length = _validated_content_length(response.headers)
    body = bytearray()
    for chunk in response.iter_raw(chunk_size=_RESPONSE_CHUNK_BYTES):
        if type(chunk) is not bytes:
            raise ValueError("invalid response chunk")
        if len(chunk) > _MAX_RESPONSE_BODY_BYTES - len(body):
            raise ValueError("response body is too large")
        body.extend(chunk)

    if declared_length is not None and declared_length != len(body):
        raise ValueError("response length mismatch")
    return GoogleCalendarTokenTransportResponse(
        status_code=status_code,
        content_type=response.headers.get("content-type"),
        body=bytes(body),
    )


def _validate_identity_encoding(headers: httpx.Headers) -> None:
    values = headers.get_list("content-encoding")
    if values and (len(values) != 1 or values[0].lower() != "identity"):
        raise ValueError("unsupported response encoding")


def _validated_content_length(headers: httpx.Headers) -> int | None:
    values = headers.get_list("content-length")
    if not values:
        return None
    if len(values) != 1:
        raise ValueError("ambiguous response length")
    value = values[0]
    if (
        not value
        or len(value) > 6
        or not value.isascii()
        or not value.isdecimal()
    ):
        raise ValueError("invalid response length")
    parsed = int(value, 10)
    if parsed > _MAX_RESPONSE_BODY_BYTES:
        raise ValueError("response body is too large")
    return parsed


def _raise_token_exchange_error() -> NoReturn:
    error = GoogleCalendarTokenExchangeError()
    try:
        raise error from None
    except GoogleCalendarTokenExchangeError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise
