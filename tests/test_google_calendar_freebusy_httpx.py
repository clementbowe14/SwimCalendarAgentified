from __future__ import annotations

import asyncio
import datetime
import inspect

import httpcore
import httpx
import pytest

from app import google_calendar_freebusy_httpx, google_calendar_token_httpx
from app.google_calendar_freebusy import (
    GOOGLE_CALENDAR_FREEBUSY_ENDPOINT,
    GoogleCalendarFreeBusyTransportResponse,
)
from app.google_calendar_freebusy_httpx import _post_json_async


ACCESS_TOKEN = "access-token-freebusy-httpx-private"
PROVIDER_DETAIL = "provider-freebusy-httpx-private"
REQUEST_BODY = (
    b'{"timeMin":"2026-11-19T10:00:00Z",'
    b'"timeMax":"2026-11-19T16:00:00Z",'
    b'"timeZone":"UTC","calendarExpansionMax":1,'
    b'"items":[{"id":"primary"}]}'
)
MAX_BODY_BYTES = 1_048_576


class AsyncRecordingStream(httpx.AsyncByteStream):
    def __init__(self, chunks=(), *, error=None, close_error=None):
        self.chunks = tuple(chunks)
        self.error = error
        self.close_error = close_error
        self.iteration_count = 0
        self.close_count = 0

    async def __aiter__(self):
        self.iteration_count += 1
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self):
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


class AsyncRecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler, *, close_error=None):
        self.handler = handler
        self.close_error = close_error
        self.requests = []
        self.request_bodies = []
        self.close_count = 0

    async def handle_async_request(self, request):
        self.requests.append(request)
        self.request_bodies.append(await request.aread())
        response = self.handler(request)
        if inspect.isawaitable(response):
            response = await response
        return response

    async def aclose(self):
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


def _response(
    *,
    status_code=200,
    body=b"{}",
    headers=None,
    stream=None,
):
    if stream is None:
        stream = AsyncRecordingStream((body,))
    if headers is None:
        headers = [
            ("content-type", "application/json"),
            ("content-length", str(len(body))),
        ]
    return httpx.Response(
        status_code,
        headers=headers,
        stream=stream,
    )


def _install_transport(monkeypatch, handler, *, close_error=None):
    transport = AsyncRecordingTransport(
        handler,
        close_error=close_error,
    )
    factory_calls = []

    def factory(**kwargs):
        factory_calls.append(kwargs)
        return transport

    monkeypatch.setattr(
        google_calendar_token_httpx,
        "_new_async_http_transport",
        factory,
    )
    return transport, factory_calls


async def _post(**overrides):
    deadline = overrides.get("deadline")
    if deadline is None:
        deadline = asyncio.get_running_loop().time() + 5.0
    return await _post_json_async(
        url=overrides.get("url", GOOGLE_CALENDAR_FREEBUSY_ENDPOINT),
        access_token=overrides.get("access_token", ACCESS_TOKEN),
        body=overrides.get("body", REQUEST_BODY),
        timeout_seconds=overrides.get("timeout_seconds", 5.0),
        deadline=deadline,
    )


def _run(awaitable):
    return asyncio.run(awaitable)


def test_module_surface_is_private_and_keyword_only():
    assert google_calendar_freebusy_httpx.__all__ == ()
    signature = inspect.signature(_post_json_async)
    assert list(signature.parameters) == [
        "url",
        "access_token",
        "body",
        "timeout_seconds",
        "deadline",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )


def test_exact_single_post_and_raw_success_response(monkeypatch):
    stream = AsyncRecordingStream((b'{"ok":true}',))
    transport, factory_calls = _install_transport(
        monkeypatch,
        lambda _request: _response(
            body=b'{"ok":true}',
            headers=[
                ("content-type", "application/json; charset=UTF-8"),
                ("content-encoding", "Identity"),
                ("content-length", "11"),
            ],
            stream=stream,
        ),
    )

    async def invoke_with_known_deadline():
        deadline = asyncio.get_running_loop().time() + 5.0
        return await _post(deadline=deadline), deadline

    result, deadline = _run(invoke_with_known_deadline())

    assert type(result) is GoogleCalendarFreeBusyTransportResponse
    assert result.status_code == 200
    assert result.content_type == "application/json; charset=UTF-8"
    assert result.body == b'{"ok":true}'
    assert len(factory_calls) == 1
    assert factory_calls == [{"deadline": deadline}]
    assert len(transport.requests) == 1
    assert transport.request_bodies == [REQUEST_BODY]
    assert transport.close_count == 1
    assert stream.iteration_count == 1
    assert stream.close_count == 1
    request = transport.requests[0]
    assert request.method == "POST"
    assert str(request.url) == GOOGLE_CALENDAR_FREEBUSY_ENDPOINT
    assert request.url.query == b""
    assert request.headers["accept"] == "application/json"
    assert request.headers["accept-encoding"] == "identity"
    assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert request.headers["content-type"] == "application/json"
    assert "cookie" not in request.headers
    assert "proxy-authorization" not in request.headers
    assert ACCESS_TOKEN.encode() not in REQUEST_BODY


@pytest.mark.parametrize(
    ("overrides", "expected_message"),
    [
        ({"url": "https://attacker.invalid/freeBusy"}, "endpoint"),
        ({"access_token": ""}, "access token"),
        ({"access_token": " token"}, "access token"),
        ({"access_token": "token\nprivate"}, "access token"),
        ({"access_token": "x" * (16 * 1024 + 1)}, "access token"),
        ({"body": bytearray(REQUEST_BODY)}, "request body"),
        ({"body": b"{}"}, "request body"),
        ({"timeout_seconds": 4.9}, "timeout"),
        ({"timeout_seconds": 5}, "timeout"),
        ({"deadline": 0.0}, "deadline"),
    ],
)
def test_invalid_inputs_fail_before_client_creation(
    monkeypatch,
    overrides,
    expected_message,
):
    factory_calls = []
    monkeypatch.setattr(
        google_calendar_token_httpx,
        "_new_async_http_transport",
        lambda **kwargs: factory_calls.append(kwargs),
    )

    with pytest.raises(ValueError, match=expected_message):
        _run(_post(**overrides))

    assert factory_calls == []


@pytest.mark.parametrize(
    "body",
    [
        REQUEST_BODY.replace(b'"timeMin"', b'"timeMinimum"'),
        REQUEST_BODY.replace(b'"timeZone":"UTC",', b""),
        REQUEST_BODY.replace(b'"calendarExpansionMax":1', b'"calendarExpansionMax":2'),
        REQUEST_BODY.replace(b'"primary"', b'"other"'),
        REQUEST_BODY.replace(
            b"2026-11-19T16:00:00Z",
            b"2026-12-04T10:00:00.000001Z",
        ),
        REQUEST_BODY.replace(
            b"2026-11-19T10:00:00Z",
            b"2026-11-19T10:00:00.1Z",
        ),
        REQUEST_BODY.replace(
            b"2026-11-19T16:00:00Z",
            b"2026-11-19T10:00:00Z",
        ),
        REQUEST_BODY.replace(
            b"2026-11-19T16:00:00Z",
            b"2026-11-19T09:59:59Z",
        ),
    ],
)
def test_request_body_must_be_exact_canonical_primary_query(monkeypatch, body):
    factory_calls = []
    monkeypatch.setattr(
        google_calendar_token_httpx,
        "_new_async_http_transport",
        lambda **kwargs: factory_calls.append(kwargs),
    )

    with pytest.raises(ValueError, match="request body"):
        _run(_post(body=body))

    assert factory_calls == []


def test_canonical_microsecond_request_is_accepted(monkeypatch):
    body = REQUEST_BODY.replace(
        b"2026-11-19T10:00:00Z",
        b"2026-11-19T10:00:00.123456Z",
    ).replace(
        b"2026-11-19T16:00:00Z",
        b"2026-11-19T16:00:00.654321Z",
    )
    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(),
    )

    result = _run(_post(body=body))

    assert result.status_code == 200
    assert transport.request_bodies == [body]


def test_exact_fifteen_day_request_window_is_accepted(monkeypatch):
    exact_window = datetime.timedelta(days=15)
    assert google_calendar_freebusy_httpx.MAX_BUSY_QUERY_WINDOW == exact_window
    body = REQUEST_BODY.replace(
        b"2026-11-19T16:00:00Z",
        b"2026-12-04T10:00:00Z",
    )
    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(),
    )

    result = _run(_post(body=body))

    assert result.status_code == 200
    assert transport.request_bodies == [body]


@pytest.mark.parametrize("status_code", [302, 400, 401, 403, 408, 429, 500, 503])
def test_non_success_bodies_are_never_read(monkeypatch, status_code):
    stream = AsyncRecordingStream(
        error=AssertionError("non-success body must not be read")
    )
    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(
            status_code=status_code,
            headers=[
                ("content-type", "application/json"),
                ("content-encoding", "gzip"),
                ("location", "https://attacker.invalid/private"),
            ],
            stream=stream,
        ),
    )

    result = _run(_post())

    assert result.status_code == status_code
    assert result.content_type is None
    assert result.body == b""
    assert stream.iteration_count == 0
    assert stream.close_count == 1
    assert len(transport.requests) == 1
    assert transport.close_count == 1


def test_exact_one_mebibyte_raw_body_is_accepted(monkeypatch):
    assert google_calendar_freebusy_httpx._MAX_RESPONSE_BODY_BYTES == MAX_BODY_BYTES
    body = b"x" * MAX_BODY_BYTES
    stream = AsyncRecordingStream((body,))
    _install_transport(
        monkeypatch,
        lambda _request: _response(
            headers=[
                ("content-type", "application/json"),
                ("content-length", str(MAX_BODY_BYTES)),
            ],
            stream=stream,
        ),
    )

    result = _run(_post())

    assert result.body == body
    assert stream.iteration_count == 1
    assert stream.close_count == 1


def test_chunked_body_over_limit_fails_and_closes(monkeypatch):
    stream = AsyncRecordingStream((b"x" * MAX_BODY_BYTES, b"y"))
    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(headers=[], stream=stream),
    )

    with pytest.raises(ValueError, match="too large"):
        _run(_post())

    assert stream.iteration_count == 1
    assert stream.close_count == 1
    assert transport.close_count == 1
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "headers",
    [
        [("content-length", str(MAX_BODY_BYTES + 1))],
        [("content-length", "-1")],
        [("content-length", "+1")],
        [("content-length", "01")],
        [("content-length", "1.0")],
        [("content-length", "1, 1")],
        [("content-length", "1"), ("content-length", "1")],
    ],
)
def test_invalid_declared_length_fails_before_iteration(monkeypatch, headers):
    stream = AsyncRecordingStream(
        error=AssertionError("invalid-length body must not be read")
    )
    _install_transport(
        monkeypatch,
        lambda _request: _response(headers=headers, stream=stream),
    )

    with pytest.raises(ValueError):
        _run(_post())

    assert stream.iteration_count == 0
    assert stream.close_count == 1


def test_declared_length_must_match_streamed_length(monkeypatch):
    stream = AsyncRecordingStream((b"x",))
    _install_transport(
        monkeypatch,
        lambda _request: _response(
            headers=[("content-length", "2")],
            stream=stream,
        ),
    )

    with pytest.raises(ValueError, match="length mismatch"):
        _run(_post())

    assert stream.iteration_count == 1
    assert stream.close_count == 1


@pytest.mark.parametrize(
    "headers",
    [
        [("content-encoding", "gzip")],
        [("content-encoding", "deflate")],
        [("content-encoding", "br")],
        [("content-encoding", "gzip, br")],
        [("content-encoding", "identity ")],
        [
            ("content-encoding", "identity"),
            ("content-encoding", "identity"),
        ],
    ],
)
def test_compressed_or_ambiguous_encoding_is_rejected(monkeypatch, headers):
    stream = AsyncRecordingStream(
        error=AssertionError("encoded body must not be read")
    )
    _install_transport(
        monkeypatch,
        lambda _request: _response(headers=headers, stream=stream),
    )

    with pytest.raises(ValueError, match="encoding"):
        _run(_post())

    assert stream.iteration_count == 0
    assert stream.close_count == 1


def test_non_byte_and_midstream_failures_close_every_resource(monkeypatch):
    for stream in (
        AsyncRecordingStream(("not-bytes",)),
        AsyncRecordingStream((b"partial",), error=RuntimeError(PROVIDER_DETAIL)),
    ):
        transport, _ = _install_transport(
            monkeypatch,
            lambda _request, value=stream: _response(
                headers=[],
                stream=value,
            ),
        )

        with pytest.raises((TypeError, ValueError, RuntimeError)):
            _run(_post())

        assert stream.iteration_count == 1
        assert stream.close_count == 1
        assert transport.close_count == 1
        assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda request: httpx.ReadTimeout(
            PROVIDER_DETAIL,
            request=request,
        ),
        lambda _request: httpcore.ReadTimeout(PROVIDER_DETAIL),
    ],
)
def test_transport_timeouts_are_normalized_and_never_retried(
    monkeypatch,
    error_factory,
):
    def fail(request):
        raise error_factory(request)

    transport, factory_calls = _install_transport(monkeypatch, fail)

    with pytest.raises(TimeoutError) as raised:
        _run(_post())

    assert raised.value.__cause__ is None
    assert len(factory_calls) == 1
    assert len(transport.requests) == 1
    assert transport.close_count == 1


def test_client_creation_timeout_is_normalized(monkeypatch):
    async def fail_client_creation(**_kwargs):
        raise httpcore.ConnectTimeout(PROVIDER_DETAIL)

    monkeypatch.setattr(
        google_calendar_freebusy_httpx,
        "_new_async_http_client",
        fail_client_creation,
    )

    with pytest.raises(TimeoutError) as raised:
        _run(_post())

    assert raised.value.__cause__ is None


def test_response_iteration_timeout_is_normalized_and_closes(monkeypatch):
    stream = AsyncRecordingStream(error=httpcore.ReadTimeout(PROVIDER_DETAIL))
    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(headers=[], stream=stream),
    )

    with pytest.raises(TimeoutError) as raised:
        _run(_post())

    assert raised.value.__cause__ is None
    assert stream.iteration_count == 1
    assert stream.close_count == 1
    assert transport.close_count == 1


def test_response_close_timeout_is_normalized(monkeypatch):
    stream = AsyncRecordingStream(
        (b"{}",),
        close_error=httpcore.ReadTimeout(PROVIDER_DETAIL),
    )
    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(stream=stream),
    )

    with pytest.raises(TimeoutError) as raised:
        _run(_post())

    assert raised.value.__cause__ is None
    assert stream.iteration_count == 1
    assert stream.close_count == 1
    assert transport.close_count == 1


def test_primary_failure_survives_response_and_client_cleanup(monkeypatch):
    stream = AsyncRecordingStream(
        (b"partial",),
        error=ValueError("primary-body-failure"),
        close_error=RuntimeError("response-close-failure"),
    )
    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(headers=[], stream=stream),
        close_error=RuntimeError("client-close-failure"),
    )

    with pytest.raises(ValueError, match="primary-body-failure"):
        _run(_post())

    assert stream.iteration_count == 1
    assert stream.close_count == 1
    assert transport.close_count == 1
    assert len(transport.requests) == 1


def test_primary_timeout_survives_ordinary_client_cleanup_failure(monkeypatch):
    def fail(_request):
        raise httpcore.ReadTimeout(PROVIDER_DETAIL)

    transport, _ = _install_transport(
        monkeypatch,
        fail,
        close_error=RuntimeError("client-close-failure"),
    )

    with pytest.raises(TimeoutError):
        _run(_post())

    assert len(transport.requests) == 1
    assert transport.close_count == 1


def test_primary_cancellation_survives_ordinary_cleanup_failure(monkeypatch):
    def cancel(_request):
        raise asyncio.CancelledError(PROVIDER_DETAIL)

    transport, _ = _install_transport(
        monkeypatch,
        cancel,
        close_error=RuntimeError("client-close-failure"),
    )

    with pytest.raises(asyncio.CancelledError):
        _run(_post())

    assert len(transport.requests) == 1
    assert transport.close_count == 1


def test_cleanup_cancellation_precedes_primary_body_failure(monkeypatch):
    stream = AsyncRecordingStream(
        error=ValueError("primary-body-failure"),
        close_error=asyncio.CancelledError(PROVIDER_DETAIL),
    )
    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(headers=[], stream=stream),
    )

    with pytest.raises(asyncio.CancelledError):
        _run(_post())

    assert stream.iteration_count == 1
    assert stream.close_count == 1
    assert transport.close_count == 1


def test_client_cleanup_cancellation_precedes_primary_failure(monkeypatch):
    stream = AsyncRecordingStream(error=ValueError("primary-body-failure"))
    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(headers=[], stream=stream),
        close_error=asyncio.CancelledError(PROVIDER_DETAIL),
    )

    with pytest.raises(asyncio.CancelledError):
        _run(_post())

    assert stream.iteration_count == 1
    assert stream.close_count == 1
    assert transport.close_count == 1


def test_httpx_timeout_during_client_cleanup_is_normalized(monkeypatch):
    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(),
        close_error=httpx.ReadTimeout(PROVIDER_DETAIL),
    )

    with pytest.raises(TimeoutError) as raised:
        _run(_post())

    assert raised.value.__cause__ is None
    assert len(transport.requests) == 1
    assert transport.close_count == 1


def test_response_and_client_cleanup_failures_fail_closed(monkeypatch):
    stream = AsyncRecordingStream((b"{}",), close_error=RuntimeError("response-close"))
    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(stream=stream),
    )

    with pytest.raises(RuntimeError, match="response-close"):
        _run(_post())

    assert stream.close_count == 1
    assert transport.close_count == 1

    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(),
        close_error=RuntimeError("client-close"),
    )
    with pytest.raises(RuntimeError, match="client-close"):
        _run(_post())
    assert transport.close_count == 1
    assert len(transport.requests) == 1
