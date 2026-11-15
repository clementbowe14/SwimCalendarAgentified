"""Bounded HTTPX transport for one Google Calendar token exchange."""

from __future__ import annotations

import asyncio
import math
import ssl
from collections.abc import AsyncIterator, Iterable
from typing import NoReturn, final
from urllib.parse import quote_plus, urlencode

import certifi
import anyio
import httpcore
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


def _remaining_timeout(
    deadline: float,
    timeout: float | None,
) -> float:
    remaining = max(0.0, deadline - anyio.current_time())
    if timeout is None:
        return remaining
    return min(remaining, timeout)


@final
class _DeadlineNetworkStream(httpcore.AsyncNetworkStream):
    """Clamp every network operation and close to one absolute deadline."""

    __slots__ = ("_stream", "_deadline")

    def __init__(
        self,
        stream: httpcore.AsyncNetworkStream,
        *,
        deadline: float,
    ) -> None:
        self._stream = stream
        self._deadline = deadline

    async def read(
        self,
        max_bytes: int,
        timeout: float | None = None,
    ) -> bytes:
        return await self._stream.read(
            max_bytes,
            timeout=_remaining_timeout(self._deadline, timeout),
        )

    async def write(
        self,
        buffer: bytes,
        timeout: float | None = None,
    ) -> None:
        await self._stream.write(
            buffer,
            timeout=_remaining_timeout(self._deadline, timeout),
        )

    async def aclose(self) -> None:
        remaining = max(0.0, self._deadline - anyio.current_time())
        if remaining == 0.0:
            await anyio.aclose_forcefully(self._stream)
            return
        with anyio.move_on_after(remaining) as close_scope:
            await self._stream.aclose()
        if close_scope.cancel_called:
            await anyio.aclose_forcefully(self._stream)

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            stream = await self._stream.start_tls(
                ssl_context,
                server_hostname=server_hostname,
                timeout=_remaining_timeout(self._deadline, timeout),
            )
        except BaseException as failure:
            try:
                with anyio.CancelScope(shield=True):
                    if isinstance(failure, asyncio.CancelledError):
                        await anyio.aclose_forcefully(self._stream)
                    else:
                        await self.aclose()
            except Exception:
                pass
            raise
        return _DeadlineNetworkStream(stream, deadline=self._deadline)

    def get_extra_info(self, info: str) -> object:
        return self._stream.get_extra_info(info)


@final
class _DeadlineNetworkBackend(httpcore.AsyncNetworkBackend):
    """Wrap HTTPcore's AnyIO backend with deadline-aware streams."""

    __slots__ = ("_backend", "_deadline")

    def __init__(self, *, deadline: float) -> None:
        self._backend = httpcore.AnyIOBackend()
        self._deadline = deadline

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        stream = await self._backend.connect_tcp(
            host,
            port,
            timeout=_remaining_timeout(self._deadline, timeout),
            local_address=local_address,
            socket_options=socket_options,
        )
        return _DeadlineNetworkStream(stream, deadline=self._deadline)

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        stream = await self._backend.connect_unix_socket(
            path,
            timeout=_remaining_timeout(self._deadline, timeout),
            socket_options=socket_options,
        )
        return _DeadlineNetworkStream(stream, deadline=self._deadline)

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(
            min(seconds, max(0.0, self._deadline - anyio.current_time()))
        )


@final
class _DeadlineHttpcoreResponseStream(httpx.AsyncByteStream):
    __slots__ = ("_stream",)

    def __init__(self, stream: object) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for part in self._stream:
            yield part

    async def aclose(self) -> None:
        close = getattr(self._stream, "aclose", None)
        if callable(close):
            await close()


@final
class _DeadlineHttpxTransport(httpx.AsyncBaseTransport):
    """One HTTP/1.1 pool whose sockets cannot outlive the operation."""

    __slots__ = ("_pool",)

    def __init__(
        self,
        *,
        ssl_context: ssl.SSLContext,
        deadline: float,
    ) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=1,
            max_keepalive_connections=0,
            keepalive_expiry=None,
            http1=True,
            http2=False,
            retries=0,
            network_backend=_DeadlineNetworkBackend(deadline=deadline),
        )

    async def handle_async_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        if not isinstance(request.stream, httpx.AsyncByteStream):
            raise ValueError("invalid async request stream")
        response = await self._pool.handle_async_request(
            httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
        )
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_DeadlineHttpcoreResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


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


async def _post_form_async(
    *,
    url: object,
    fields: object,
    timeout_seconds: object,
    deadline: object,
) -> GoogleCalendarTokenTransportResponse:
    """Perform the same bounded request through cancellation-capable I/O."""

    encoded_form = _validated_encoded_form(
        url=url,
        fields=fields,
        timeout_seconds=timeout_seconds,
    )
    operation_deadline = _validated_operation_deadline(deadline)
    client = await _new_async_http_client(
        timeout_seconds=_TOKEN_EXCHANGE_TIMEOUT_SECONDS,
        deadline=operation_deadline,
    )
    try:
        response = await _stream_response_async(
            client=client,
            encoded_form=encoded_form,
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
    return response


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


def _validated_operation_deadline(value: object) -> float:
    if (
        type(value) is not float
        or not math.isfinite(value)
        or value <= anyio.current_time()
    ):
        raise ValueError("invalid operation deadline")
    return value


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


def _new_async_http_transport(*, deadline: float) -> httpx.AsyncBaseTransport:
    return _DeadlineHttpxTransport(
        ssl_context=_new_ssl_context(),
        deadline=deadline,
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


async def _new_async_http_client(
    *,
    timeout_seconds: float,
    deadline: float,
) -> httpx.AsyncClient:
    transport = _new_async_http_transport(deadline=deadline)
    try:
        return httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            proxy=None,
            http1=True,
            http2=False,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds),
        )
    except BaseException:
        try:
            await transport.aclose()
        except asyncio.CancelledError:
            raise
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


async def _stream_response_async(
    *,
    client: httpx.AsyncClient,
    encoded_form: bytes,
) -> GoogleCalendarTokenTransportResponse:
    response_context = client.stream(
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


async def _bounded_response_async(
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
    async for chunk in response.aiter_raw(chunk_size=_RESPONSE_CHUNK_BYTES):
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
