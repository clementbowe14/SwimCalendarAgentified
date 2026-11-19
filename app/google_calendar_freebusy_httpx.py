"""Bounded async HTTPX transport for one Google Calendar FreeBusy query."""

from __future__ import annotations

import asyncio
import re

import httpcore
import httpx

from app.external_calendar import MAX_BUSY_QUERY_WINDOW
from app.google_calendar_freebusy import (
    GOOGLE_CALENDAR_FREEBUSY_ENDPOINT,
    GoogleCalendarFreeBusyTransportResponse,
    _canonical_rfc3339,
    _parse_rfc3339,
)
from app.google_calendar_token_httpx import (
    _new_async_http_client,
    _validated_operation_deadline,
)


__all__ = ()


_FREEBUSY_TIMEOUT_SECONDS = 5.0
_MAX_ACCESS_TOKEN_BYTES = 16 * 1024
_MAX_RESPONSE_BODY_BYTES = 1024 * 1024
_RESPONSE_CHUNK_BYTES = 8 * 1024
_CANONICAL_TIMESTAMP = (
    rb"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    rb"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    rb"(?:\.[0-9]{6})?Z"
)
_REQUEST_BODY_PATTERN = re.compile(
    rb'\{"timeMin":"('
    + _CANONICAL_TIMESTAMP
    + rb')","timeMax":"('
    + _CANONICAL_TIMESTAMP
    + rb')","timeZone":"UTC","calendarExpansionMax":1,'
    + rb'"items":\[\{"id":"primary"\}\]\}\Z'
)


async def _post_json_async(
    *,
    url: object,
    access_token: object,
    body: object,
    timeout_seconds: object,
    deadline: object,
) -> GoogleCalendarFreeBusyTransportResponse:
    """Perform one fixed, raw, non-redirecting FreeBusy POST."""

    trusted_token, trusted_body = _validated_request(
        url=url,
        access_token=access_token,
        body=body,
        timeout_seconds=timeout_seconds,
    )
    operation_deadline = _validated_operation_deadline(deadline)
    try:
        client = await _new_async_http_client(
            timeout_seconds=_FREEBUSY_TIMEOUT_SECONDS,
            deadline=operation_deadline,
        )
        try:
            result = await _stream_response_async(
                client=client,
                access_token=trusted_token,
                body=trusted_body,
            )
        except BaseException:
            try:
                await client.aclose()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            raise
        await client.aclose()
        return result
    except (httpcore.TimeoutException, httpx.TimeoutException):
        raise TimeoutError("FreeBusy transport timed out") from None


def _validated_request(
    *,
    url: object,
    access_token: object,
    body: object,
    timeout_seconds: object,
) -> tuple[str, bytes]:
    if type(url) is not str or url != GOOGLE_CALENDAR_FREEBUSY_ENDPOINT:
        raise ValueError("invalid FreeBusy endpoint")
    if (
        type(timeout_seconds) is not float
        or timeout_seconds != _FREEBUSY_TIMEOUT_SECONDS
    ):
        raise ValueError("invalid FreeBusy timeout")
    token = _validated_access_token(access_token)
    request_body = _validated_request_body(body)
    return token, request_body


def _validated_access_token(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("invalid access token")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("invalid access token") from None
    if len(encoded) > _MAX_ACCESS_TOKEN_BYTES or any(
        byte < 0x21 or byte > 0x7E for byte in encoded
    ):
        raise ValueError("invalid access token")
    return value


def _validated_request_body(value: object) -> bytes:
    if type(value) is not bytes:
        raise ValueError("invalid FreeBusy request body")
    matched = _REQUEST_BODY_PATTERN.fullmatch(value)
    if matched is None:
        raise ValueError("invalid FreeBusy request body")
    start_text = matched.group(1).decode("ascii")
    end_text = matched.group(2).decode("ascii")
    start = _parse_rfc3339(start_text)
    end = _parse_rfc3339(end_text)
    if (
        _canonical_rfc3339(start) != start_text
        or _canonical_rfc3339(end) != end_text
        or end <= start
        or end - start > MAX_BUSY_QUERY_WINDOW
    ):
        raise ValueError("invalid FreeBusy request body")
    return value


async def _stream_response_async(
    *,
    client: httpx.AsyncClient,
    access_token: str,
    body: bytes,
) -> GoogleCalendarFreeBusyTransportResponse:
    response_context = client.stream(
        "POST",
        GOOGLE_CALENDAR_FREEBUSY_ENDPOINT,
        content=body,
        params=None,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        cookies=None,
        auth=None,
        follow_redirects=False,
        timeout=httpx.Timeout(_FREEBUSY_TIMEOUT_SECONDS),
    )
    response = await response_context.__aenter__()
    try:
        result = await _bounded_response_async(response)
    except BaseException as failure:
        try:
            await response_context.__aexit__(
                type(failure),
                failure,
                failure.__traceback__,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        raise
    await response_context.__aexit__(None, None, None)
    return result


async def _bounded_response_async(
    response: httpx.Response,
) -> GoogleCalendarFreeBusyTransportResponse:
    status_code = response.status_code
    if type(status_code) is not int:
        raise ValueError("invalid response status")
    if status_code != 200:
        return GoogleCalendarFreeBusyTransportResponse(
            status_code=status_code,
            content_type=None,
            body=b"",
        )

    _validate_identity_encoding(response.headers)
    declared_length = _validated_content_length(response.headers)
    body = bytearray()
    async for chunk in response.aiter_raw(
        chunk_size=_RESPONSE_CHUNK_BYTES
    ):
        if type(chunk) is not bytes:
            raise ValueError("invalid response chunk")
        if len(chunk) > _MAX_RESPONSE_BODY_BYTES - len(body):
            raise ValueError("response body is too large")
        body.extend(chunk)

    if declared_length is not None and declared_length != len(body):
        raise ValueError("response length mismatch")
    return GoogleCalendarFreeBusyTransportResponse(
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
    if not value or not value.isascii() or not value.isdecimal():
        raise ValueError("invalid response length")
    parsed = int(value, 10)
    if value != str(parsed) or parsed > _MAX_RESPONSE_BODY_BYTES:
        raise ValueError("invalid response length")
    return parsed
