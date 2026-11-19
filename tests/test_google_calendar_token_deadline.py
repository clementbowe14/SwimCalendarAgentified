from __future__ import annotations

import asyncio
import dis
import inspect
import json
import ssl
import sys
import traceback

import anyio
import httpcore
import httpx
import pytest

from app import (
    api,
    google_calendar_token_deadline,
    google_calendar_token_httpx,
    tools,
)
from app.calendar_oauth_pkce import derive_s256_code_challenge
from app.google_calendar_authorization import GOOGLE_CALENDAR_FREEBUSY_SCOPE
from app.google_calendar_oauth import (
    GOOGLE_CALENDAR_CALLBACK_PATH,
    GoogleCalendarOAuthConfiguration,
)
from app.google_calendar_token_deadline import (
    exchange_google_calendar_authorization_code_with_deadline,
    exchange_google_calendar_refresh_token_with_deadline,
)
from app.google_calendar_token_exchange import (
    GOOGLE_CALENDAR_TOKEN_ENDPOINT,
    GoogleCalendarTokenExchangeConfiguration,
    GoogleCalendarTokenExchangeError,
    GoogleCalendarTokenTransportResponse,
)
from app.routing import HaversineRoutingProvider


CLIENT_ID = "123456789-dayweave.apps.googleusercontent.com"
REDIRECT_URI = "https://api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH
CLIENT_SECRET = "client+secret/private&value="
AUTHORIZATION_CODE = "4/code+private&value="
CODE_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
ACCESS_TOKEN = "access-token-private-value"
REFRESH_TOKEN = "refresh-token-private-value"
PROVIDER_DETAIL = "provider-private-error-detail"


class AsyncRecordingStream(httpx.AsyncByteStream):
    def __init__(
        self,
        chunks=(),
        *,
        delay_seconds=0.0,
        first_chunk_immediate=False,
        stall_after_chunks=False,
        error=None,
        stall_close=False,
        close_error=None,
    ):
        self.chunks = tuple(chunks)
        self.delay_seconds = delay_seconds
        self.first_chunk_immediate = first_chunk_immediate
        self.stall_after_chunks = stall_after_chunks
        self.error = error
        self.stall_close = stall_close
        self.close_error = close_error
        self.iteration_count = 0
        self.yield_count = 0
        self.close_count = 0
        self.cancelled = False
        self.stalled = None
        self.close_started = None
        self.close_cancelled = False

    async def __aiter__(self):
        self.iteration_count += 1
        try:
            for chunk in self.chunks:
                if self.delay_seconds and (
                    self.yield_count or not self.first_chunk_immediate
                ):
                    await asyncio.sleep(self.delay_seconds)
                self.yield_count += 1
                yield chunk
            if self.error is not None:
                raise self.error
            if self.stall_after_chunks:
                self.stalled = asyncio.Event()
                self.stalled.set()
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self):
        self.close_count += 1
        if self.stall_close:
            self.close_started = asyncio.Event()
            self.close_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.close_cancelled = True
                raise
        if self.close_error is not None:
            raise self.close_error


class AsyncRecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler, *, stall_close=False, close_error=None):
        self.handler = handler
        self.stall_close = stall_close
        self.close_error = close_error
        self.requests = []
        self.close_count = 0
        self.cancelled = False
        self.started = None
        self.close_started = None
        self.close_cancelled = False

    async def handle_async_request(self, request):
        self.requests.append(request)
        self.started = asyncio.Event()
        self.started.set()
        try:
            response = self.handler(request)
            if inspect.isawaitable(response):
                response = await response
            return response
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self):
        self.close_count += 1
        if self.stall_close:
            self.close_started = asyncio.Event()
            self.close_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.close_cancelled = True
                raise
        if self.close_error is not None:
            raise self.close_error


def _configuration():
    return GoogleCalendarTokenExchangeConfiguration(
        authorization_configuration=GoogleCalendarOAuthConfiguration(
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
        ),
        client_secret=CLIENT_SECRET,
    )


def _payload():
    return {
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "expires_in": 3600,
        "scope": GOOGLE_CALENDAR_FREEBUSY_SCOPE,
        "token_type": "Bearer",
    }


def _payload_bytes():
    return json.dumps(_payload(), separators=(",", ":")).encode("utf-8")


def _response(*, body=None, stream=None, headers=None, status_code=200):
    if body is None:
        body = _payload_bytes()
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


def _transport_response():
    return GoogleCalendarTokenTransportResponse(
        status_code=200,
        content_type="application/json",
        body=_payload_bytes(),
    )


def _install_transport(
    monkeypatch,
    handler,
    *,
    stall_close=False,
    close_error=None,
):
    transport = AsyncRecordingTransport(
        handler,
        stall_close=stall_close,
        close_error=close_error,
    )
    factory_calls = []

    def factory(**_kwargs):
        factory_calls.append(True)
        return transport

    monkeypatch.setattr(
        google_calendar_token_httpx,
        "_new_async_http_transport",
        factory,
    )
    return transport, factory_calls


async def _exchange(**overrides):
    return await exchange_google_calendar_authorization_code_with_deadline(
        configuration=overrides.get("configuration", _configuration()),
        authorization_code=overrides.get(
            "authorization_code",
            AUTHORIZATION_CODE,
        ),
        code_verifier=overrides.get("code_verifier", CODE_VERIFIER),
    )


def _run(awaitable):
    return asyncio.run(awaitable)


def _formatted_exception(error):
    return "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )


def _module_traceback(error):
    names = []
    values = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith(
            "/app/google_calendar_token_deadline.py"
        ):
            names.append(current.tb_frame.f_code.co_name)
            values.extend(current.tb_frame.f_locals.values())
        current = current.tb_next
    return names, repr(values)


def test_public_surface_is_one_async_keyword_only_service():
    assert google_calendar_token_deadline.__all__ == (
        "exchange_google_calendar_authorization_code_with_deadline",
        "exchange_google_calendar_refresh_token_with_deadline",
    )
    assert inspect.iscoroutinefunction(
        exchange_google_calendar_authorization_code_with_deadline
    )
    assert inspect.iscoroutinefunction(
        exchange_google_calendar_refresh_token_with_deadline
    )
    signature = inspect.signature(
        exchange_google_calendar_authorization_code_with_deadline
    )
    assert list(signature.parameters) == [
        "configuration",
        "authorization_code",
        "code_verifier",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )


def test_success_uses_one_exact_bounded_request(monkeypatch):
    stream = AsyncRecordingStream((_payload_bytes(),))
    transport, factory_calls = _install_transport(
        monkeypatch,
        lambda _request: _response(stream=stream),
    )

    result = _run(_exchange())

    assert result.refresh_token == REFRESH_TOKEN
    assert result.pkce_code_challenge == derive_s256_code_challenge(
        CODE_VERIFIER
    )
    assert factory_calls == [True]
    assert transport.close_count == 1
    assert stream.iteration_count == 1
    assert stream.close_count == 1
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.method == "POST"
    assert str(request.url) == GOOGLE_CALENDAR_TOKEN_ENDPOINT
    assert request.url.query == b""
    assert request.headers["accept"] == "application/json"
    assert request.headers["accept-encoding"] == "identity"
    assert request.headers["content-type"] == (
        "application/x-www-form-urlencoded"
    )
    assert "authorization" not in request.headers
    assert "proxy-authorization" not in request.headers
    assert "cookie" not in request.headers
    assert request.extensions["timeout"] == {
        "connect": 5.0,
        "read": 5.0,
        "write": 5.0,
        "pool": 5.0,
    }
    assert request.content == (
        b"code=4%2Fcode%2Bprivate%26value%3D"
        b"&client_id=123456789-dayweave.apps.googleusercontent.com"
        b"&client_secret=client%2Bsecret%2Fprivate%26value%3D"
        b"&redirect_uri=https%3A%2F%2Fapi.dayweave.example%2Fapi%2Foauth"
        b"%2Fgoogle-calendar%2Fcallback"
        b"&grant_type=authorization_code"
        b"&code_verifier=dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    )


def test_distinct_verifiers_preserve_distinct_bindings_through_deadline(
    monkeypatch,
):
    other_verifier = "A" * 43
    first_transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(),
    )

    first = _run(_exchange(code_verifier=CODE_VERIFIER))

    second_transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(),
    )
    second = _run(_exchange(code_verifier=other_verifier))

    assert first.pkce_code_challenge == derive_s256_code_challenge(
        CODE_VERIFIER
    )
    assert second.pkce_code_challenge == derive_s256_code_challenge(
        other_verifier
    )
    assert first.pkce_code_challenge != second.pkce_code_challenge
    assert len(first_transport.requests) == 1
    assert len(second_transport.requests) == 1


def test_async_network_policy_ignores_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-private.invalid:9999")
    monkeypatch.setenv("SSL_CERT_FILE", "/private/provider-ca-secret.pem")
    keylog_path = tmp_path / "provider-tls-secrets.log"
    monkeypatch.setenv("SSLKEYLOGFILE", str(keylog_path))
    network_transport = AsyncRecordingTransport(
        lambda _request: _response()
    )
    transport_kwargs = []
    client_kwargs = []
    real_client = httpx.AsyncClient

    def build_transport(*, ssl_context, deadline):
        transport_kwargs.append(
            {"ssl_context": ssl_context, "deadline": deadline}
        )
        return network_transport

    def build_client(**kwargs):
        client_kwargs.append(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr(
        google_calendar_token_httpx,
        "_DeadlineHttpxTransport",
        build_transport,
    )
    monkeypatch.setattr(
        google_calendar_token_httpx.httpx,
        "AsyncClient",
        build_client,
    )

    _run(_exchange())

    assert len(transport_kwargs) == 1
    transport_options = transport_kwargs[0]
    ssl_context = transport_options["ssl_context"]
    assert isinstance(ssl_context, ssl.SSLContext)
    assert ssl_context.check_hostname is True
    assert ssl_context.verify_mode is ssl.CERT_REQUIRED
    assert ssl_context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert ssl_context.keylog_filename is None
    assert not keylog_path.exists()
    assert type(transport_options["deadline"]) is float
    assert len(client_kwargs) == 1
    options = client_kwargs[0]
    assert options["transport"] is network_transport
    assert options["trust_env"] is False
    assert options["proxy"] is None
    assert options["http1"] is True
    assert options["http2"] is False
    assert options["follow_redirects"] is False
    assert options["timeout"].connect == 5.0
    assert options["timeout"].read == 5.0
    assert options["timeout"].write == 5.0
    assert options["timeout"].pool == 5.0


def test_deadline_transport_uses_one_non_reusing_http1_pool():
    async def scenario():
        deadline = asyncio.get_running_loop().time() + 5.0
        transport = google_calendar_token_httpx._DeadlineHttpxTransport(
            ssl_context=google_calendar_token_httpx._new_ssl_context(),
            deadline=deadline,
        )
        try:
            pool = transport._pool
            assert pool._proxy is None
            assert pool._max_connections == 1
            assert pool._max_keepalive_connections == 0
            assert pool._keepalive_expiry is None
            assert pool._http1 is True
            assert pool._http2 is False
            assert pool._retries == 0
            assert isinstance(
                pool._network_backend,
                google_calendar_token_httpx._DeadlineNetworkBackend,
            )
        finally:
            await transport.aclose()

    _run(scenario())


def test_deadline_stream_close_escapes_httpcore_style_shield():
    class StallingNetworkStream(httpcore.AsyncNetworkStream):
        def __init__(self):
            self.close_count = 0
            self.close_cancelled = False
            self.force_closed = False

        async def read(self, max_bytes, timeout=None):
            del max_bytes, timeout
            return b""

        async def write(self, buffer, timeout=None):
            del buffer, timeout

        async def aclose(self):
            self.close_count += 1
            if self.close_count > 1:
                self.force_closed = True
                await asyncio.sleep(0)
                return
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.close_cancelled = True
                raise

        async def start_tls(
            self,
            ssl_context,
            server_hostname=None,
            timeout=None,
        ):
            del ssl_context, server_hostname, timeout
            return self

        def get_extra_info(self, info):
            del info
            return None

    async def scenario():
        underlying = StallingNetworkStream()
        stream = google_calendar_token_httpx._DeadlineNetworkStream(
            underlying,
            deadline=anyio.current_time() + 0.02,
        )
        with anyio.CancelScope(shield=True):
            await stream.aclose()
        return underlying

    underlying = _run(asyncio.wait_for(scenario(), timeout=0.2))

    assert underlying.close_count == 2
    assert underlying.close_cancelled is True
    assert underlying.force_closed is True


def test_tls_cancellation_force_closes_pre_handshake_stream():
    class StallingTLSStream(httpcore.AsyncNetworkStream):
        def __init__(self):
            self.tls_cancelled = False
            self.close_count = 0
            self.force_closed = False

        async def read(self, max_bytes, timeout=None):
            del max_bytes, timeout
            return b""

        async def write(self, buffer, timeout=None):
            del buffer, timeout

        async def aclose(self):
            self.close_count += 1
            self.force_closed = True
            await asyncio.sleep(0)

        async def start_tls(
            self,
            ssl_context,
            server_hostname=None,
            timeout=None,
        ):
            del ssl_context, server_hostname, timeout
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.tls_cancelled = True
                raise

        def get_extra_info(self, info):
            del info
            return None

    async def scenario():
        underlying = StallingTLSStream()
        deadline = anyio.current_time() + 0.02
        stream = google_calendar_token_httpx._DeadlineNetworkStream(
            underlying,
            deadline=deadline,
        )
        with anyio.CancelScope(deadline=deadline) as scope:
            await stream.start_tls(
                ssl.create_default_context(),
                server_hostname="oauth2.googleapis.com",
                timeout=5.0,
            )
        assert scope.cancel_called is True
        return underlying

    underlying = _run(asyncio.wait_for(scenario(), timeout=0.2))

    assert underlying.tls_cancelled is True
    assert underlying.close_count == 1
    assert underlying.force_closed is True


def test_deadline_httpcore_transport_composes_without_live_network(monkeypatch):
    body = _payload_bytes()
    backend = httpcore.AsyncMockBackend(
        [
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"\r\n"
            + body
        ]
    )
    monkeypatch.setattr(
        google_calendar_token_httpx.httpcore,
        "AnyIOBackend",
        lambda: backend,
    )

    result = _run(_exchange())

    assert result.refresh_token == REFRESH_TOKEN


@pytest.mark.parametrize(
    "overrides",
    [
        {"configuration": object()},
        {"authorization_code": ""},
        {"authorization_code": " bad"},
        {"authorization_code": "café"},
        {"code_verifier": "not-a-canonical-verifier"},
        {"code_verifier": None},
    ],
)
def test_invalid_input_precedes_deadline_and_network(
    monkeypatch,
    overrides,
):
    deadline_calls = []
    network_calls = []

    def forbidden_deadline(*, deadline):
        del deadline
        deadline_calls.append(True)
        raise AssertionError("deadline must not be created")

    async def forbidden_network(**_kwargs):
        network_calls.append(True)
        raise AssertionError("network must not be created")

    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_new_deadline_scope",
        forbidden_deadline,
    )
    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_post_form_async",
        forbidden_network,
    )

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _run(_exchange(**overrides))

    assert deadline_calls == []
    assert network_calls == []


def test_overall_deadline_cancels_stalled_request_and_closes(monkeypatch):
    async def stall(_request):
        await asyncio.Event().wait()

    transport, factory_calls = _install_transport(
        monkeypatch,
        stall,
        close_error=RuntimeError("cleanup-private-detail"),
    )
    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_TOKEN_EXCHANGE_DEADLINE_SECONDS",
        0.02,
    )

    with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
        _run(_exchange())

    assert str(raised.value) == (
        "Google Calendar authorization could not be completed."
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert factory_calls == [True]
    assert len(transport.requests) == 1
    assert transport.cancelled is True
    assert transport.close_count == 1


def test_overall_deadline_cancels_slow_stream_and_closes(monkeypatch):
    stream = AsyncRecordingStream(
        (b"{", b'"partial"', b":"),
        delay_seconds=0.012,
        first_chunk_immediate=True,
        stall_after_chunks=True,
    )
    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(
            stream=stream,
            headers=[("content-type", "application/json")],
        ),
    )
    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_TOKEN_EXCHANGE_DEADLINE_SECONDS",
        0.018,
    )

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _run(_exchange())

    assert stream.iteration_count == 1
    assert 1 <= stream.yield_count < 3
    assert stream.cancelled is True
    assert stream.close_count == 1
    assert transport.close_count == 1
    assert len(transport.requests) == 1


def test_deadline_reinterrupts_hanging_transport_close(monkeypatch):
    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(),
        stall_close=True,
    )
    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_TOKEN_EXCHANGE_DEADLINE_SECONDS",
        0.02,
    )

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _run(asyncio.wait_for(_exchange(), timeout=0.2))

    assert transport.close_count == 1
    assert transport.close_started is not None
    assert transport.close_cancelled is True


def test_deadline_reinterrupts_hanging_response_close(monkeypatch):
    stream = AsyncRecordingStream(
        (_payload_bytes(),),
        stall_close=True,
    )
    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(stream=stream),
    )
    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_TOKEN_EXCHANGE_DEADLINE_SECONDS",
        0.02,
    )

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _run(asyncio.wait_for(_exchange(), timeout=0.2))

    assert stream.close_count == 1
    assert stream.close_started is not None
    assert stream.close_cancelled is True
    assert transport.close_count == 1


def test_late_result_after_suppressed_cancellation_is_rejected(monkeypatch):
    cancellation_was_suppressed = []

    async def suppress_cancellation(**_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_was_suppressed.append(True)
            return _transport_response()

    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_post_form_async",
        suppress_cancellation,
    )
    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_TOKEN_EXCHANGE_DEADLINE_SECONDS",
        0.01,
    )

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _run(_exchange())

    assert cancellation_was_suppressed == [True]


@pytest.mark.parametrize(
    ("completed_at", "should_fail"),
    [(104.999, False), (105.0, True)],
)
def test_final_monotonic_check_controls_result_acceptance(
    monkeypatch,
    completed_at,
    should_fail,
):
    class PassiveScope:
        cancel_called = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    times = iter((100.0, completed_at))
    deadlines = []

    async def immediate_response(**_kwargs):
        return _transport_response()

    def scope_factory(*, deadline):
        deadlines.append(deadline)
        return PassiveScope()

    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_monotonic_time",
        lambda: next(times),
    )
    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_new_deadline_scope",
        scope_factory,
    )
    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_post_form_async",
        immediate_response,
    )

    if should_fail:
        with pytest.raises(GoogleCalendarTokenExchangeError):
            _run(_exchange())
    else:
        assert _run(_exchange()).refresh_token == REFRESH_TOKEN
    assert deadlines == [105.0]


def test_external_cancellation_remains_sanitized_cancellation(monkeypatch):
    async def stall(_request):
        await asyncio.Event().wait()

    transport, _ = _install_transport(
        monkeypatch,
        stall,
        close_error=RuntimeError("cleanup-private-detail"),
    )

    async def scenario():
        task = asyncio.create_task(_exchange())
        while transport.started is None:
            await asyncio.sleep(0)
        await transport.started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError as error:
            return error
        raise AssertionError("external cancellation must propagate")

    raised = _run(scenario())

    assert raised.__cause__ is None
    assert raised.__context__ is None
    assert raised.__suppress_context__ is True
    names, values = _module_traceback(raised)
    assert names == [
        "exchange_google_calendar_authorization_code_with_deadline",
        "_raise_cancelled",
    ]
    for private in (CLIENT_SECRET, AUTHORIZATION_CODE, CODE_VERIFIER):
        assert private not in values
    assert transport.cancelled is True
    assert transport.close_count == 1


@pytest.mark.parametrize("failure_kind", ["malformed_response", "exception"])
def test_authorization_suppressed_cancellation_precedes_followup_failure(
    monkeypatch,
    failure_kind,
):
    request_started = asyncio.Event()
    cancellation_was_suppressed = []

    async def suppress_then_fail(**_kwargs):
        request_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_was_suppressed.append(True)
            if failure_kind == "malformed_response":
                return GoogleCalendarTokenTransportResponse(
                    status_code=500,
                    content_type=None,
                    body=b"",
                )
            raise RuntimeError(PROVIDER_DETAIL)

    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_post_form_async",
        suppress_then_fail,
    )

    async def scenario():
        task = asyncio.create_task(_exchange())
        await request_started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError as error:
            return error
        raise AssertionError("pending cancellation must win")

    raised = _run(scenario())

    assert cancellation_was_suppressed == [True]
    assert raised.__cause__ is None
    assert raised.__context__ is None
    assert raised.__suppress_context__ is True
    names, values = _module_traceback(raised)
    assert names == [
        "exchange_google_calendar_authorization_code_with_deadline",
        "_raise_cancelled",
    ]
    for private in (
        CLIENT_SECRET,
        AUTHORIZATION_CODE,
        CODE_VERIFIER,
        PROVIDER_DETAIL,
    ):
        assert private not in values


def test_authorization_boundary_entry_interruption_is_sanitized():
    guarded = exchange_google_calendar_authorization_code_with_deadline
    interrupted = False

    def interrupt_entry(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is guarded.__code__
        ):
            interrupted = True
            raise KeyboardInterrupt("authorization-entry-control-private")
        return interrupt_entry

    try:
        sys.settrace(interrupt_entry)
        with pytest.raises(KeyboardInterrupt) as raised:
            _run(_exchange())
    finally:
        sys.settrace(None)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, values = _module_traceback(raised.value)
    assert names == [
        "exchange_google_calendar_authorization_code_with_deadline",
        "_raise_control_exception",
    ]
    output = " ".join(
        (
            repr(raised.value),
            _formatted_exception(raised.value),
            values,
        )
    )
    for private in (
        "authorization-entry-control-private",
        CLIENT_SECRET,
        AUTHORIZATION_CODE,
        CODE_VERIFIER,
    ):
        assert private not in output


def test_authorization_boundary_cleanup_interruption_is_sanitized():
    guarded = exchange_google_calendar_authorization_code_with_deadline
    source_lines, first_line = inspect.getsourcelines(guarded)
    target_line = first_line + next(
        offset
        for offset, line in enumerate(source_lines)
        if line.lstrip().startswith("try:") and " = None" in line
    )
    cleanup_instructions = [
        instruction
        for instruction in dis.get_instructions(guarded)
        if instruction.positions.lineno == target_line
    ]
    assert "DELETE_FAST" not in {
        instruction.opname for instruction in cleanup_instructions
    }
    assert sum(
        instruction.opname == "STORE_FAST"
        for instruction in cleanup_instructions
    ) >= 3
    interrupted = False

    def interrupt_cleanup(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is guarded.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt("authorization-cleanup-control-private")
        return interrupt_cleanup

    try:
        sys.settrace(interrupt_cleanup)
        with pytest.raises(KeyboardInterrupt) as raised:
            _run(
                _exchange(
                    authorization_code=(
                        "invalid\nauthorization-cleanup-secret"
                    )
                )
            )
    finally:
        sys.settrace(None)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, values = _module_traceback(raised.value)
    assert names == [
        "exchange_google_calendar_authorization_code_with_deadline",
        "_raise_control_exception",
    ]
    output = " ".join(
        (
            repr(raised.value),
            _formatted_exception(raised.value),
            values,
        )
    )
    for private in (
        "authorization-cleanup-control-private",
        "authorization-cleanup-secret",
        CLIENT_SECRET,
        AUTHORIZATION_CODE,
        CODE_VERIFIER,
    ):
        assert private not in output


def test_authorization_boundary_exception_entry_interruption_is_sanitized():
    guarded = (
        google_calendar_token_deadline
        ._guarded_google_calendar_authorization_code_with_deadline
    )
    source_lines, first_line = inspect.getsourcelines(guarded)
    target_line = first_line + next(
        offset
        for offset, line in enumerate(source_lines)
        if line.lstrip().startswith("except BaseException as failure")
    )
    interrupted = False

    def interrupt_exception_entry(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is guarded.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt("authorization-except-entry-private")
        return interrupt_exception_entry

    try:
        sys.settrace(interrupt_exception_entry)
        with pytest.raises(KeyboardInterrupt) as raised:
            _run(
                _exchange(
                    authorization_code=(
                        "invalid\nauthorization-except-entry-secret"
                    )
                )
            )
    finally:
        sys.settrace(None)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, values = _module_traceback(raised.value)
    assert names == [
        "exchange_google_calendar_authorization_code_with_deadline",
        "_raise_control_exception",
    ]
    output = " ".join(
        (
            repr(raised.value),
            _formatted_exception(raised.value),
            values,
        )
    )
    for private in (
        "authorization-except-entry-private",
        "authorization-except-entry-secret",
        CLIENT_SECRET,
        AUTHORIZATION_CODE,
        CODE_VERIFIER,
    ):
        assert private not in output


def test_reentrant_public_authorization_handler_is_secret_free():
    guarded = exchange_google_calendar_authorization_code_with_deadline
    source_lines, first_line = inspect.getsourcelines(guarded)
    target_line = first_line + next(
        offset
        for offset, line in enumerate(source_lines)
        if line.lstrip().startswith(
            "except BaseException as boundary_raised"
        )
    )
    interrupted = False

    def interrupt_public_handler(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is guarded.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt("public-handler-private-detail")
        return interrupt_public_handler

    try:
        sys.settrace(interrupt_public_handler)
        with pytest.raises(KeyboardInterrupt) as raised:
            _run(
                _exchange(
                    authorization_code=(
                        "invalid\npublic-handler-secret"
                    )
                )
            )
    finally:
        sys.settrace(None)

    names, values = _module_traceback(raised.value)
    assert names == [
        "exchange_google_calendar_authorization_code_with_deadline"
    ]
    output = " ".join(
        (
            repr(raised.value),
            _formatted_exception(raised.value),
            values,
        )
    )
    for private in (
        "public-handler-secret",
        CLIENT_SECRET,
        AUTHORIZATION_CODE,
        CODE_VERIFIER,
    ):
        assert private not in output


def test_cancellation_during_client_cleanup_takes_precedence(monkeypatch):
    def fail_request(_request):
        raise RuntimeError("request-private-detail")

    transport, _ = _install_transport(
        monkeypatch,
        fail_request,
        stall_close=True,
    )

    async def scenario():
        task = asyncio.create_task(_exchange())
        while transport.close_started is None:
            await asyncio.sleep(0)
        await transport.close_started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError as error:
            return error
        raise AssertionError("cleanup cancellation must take precedence")

    raised = _run(scenario())

    assert raised.__cause__ is None
    assert raised.__context__ is None
    assert transport.close_cancelled is True


def test_cancellation_during_response_cleanup_takes_precedence(monkeypatch):
    stream = AsyncRecordingStream(
        error=RuntimeError("stream-private-detail"),
        stall_close=True,
    )
    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(
            stream=stream,
            headers=[("content-type", "application/json")],
        ),
    )

    async def scenario():
        task = asyncio.create_task(_exchange())
        while stream.close_started is None:
            await asyncio.sleep(0)
        await stream.close_started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError as error:
            return error
        raise AssertionError("response cleanup cancellation must take precedence")

    raised = _run(scenario())

    assert raised.__cause__ is None
    assert raised.__context__ is None
    assert stream.close_cancelled is True
    assert transport.close_count == 1


def test_response_close_failure_cannot_mask_external_cancellation(monkeypatch):
    stream = AsyncRecordingStream(
        stall_after_chunks=True,
        close_error=RuntimeError("response-close-private-detail"),
    )
    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(
            stream=stream,
            headers=[("content-type", "application/json")],
        ),
    )

    async def scenario():
        task = asyncio.create_task(_exchange())
        while stream.stalled is None:
            await asyncio.sleep(0)
        await stream.stalled.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError as error:
            return error
        raise AssertionError("response cleanup must preserve cancellation")

    raised = _run(scenario())

    assert raised.__cause__ is None
    assert raised.__context__ is None
    assert stream.cancelled is True
    assert stream.close_count == 1
    assert transport.close_count == 1


def test_concurrent_timeout_and_success_are_isolated(monkeypatch):
    transports = []

    async def handler(request):
        if b"slow-code-private" in request.content:
            await asyncio.Event().wait()
        return _response()

    def factory(**_kwargs):
        transport = AsyncRecordingTransport(handler)
        transports.append(transport)
        return transport

    monkeypatch.setattr(
        google_calendar_token_httpx,
        "_new_async_http_transport",
        factory,
    )
    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_TOKEN_EXCHANGE_DEADLINE_SECONDS",
        0.05,
    )

    async def scenario():
        slow = asyncio.create_task(
            _exchange(authorization_code="slow-code-private")
        )
        fast = asyncio.create_task(
            _exchange(authorization_code="fast-code-private")
        )
        return await asyncio.gather(slow, fast, return_exceptions=True)

    slow_result, fast_result = _run(scenario())

    assert type(slow_result) is GoogleCalendarTokenExchangeError
    assert fast_result.refresh_token == REFRESH_TOKEN
    assert len(transports) == 2
    assert sum(len(transport.requests) for transport in transports) == 2
    assert all(transport.close_count == 1 for transport in transports)
    assert sum(transport.cancelled for transport in transports) == 1


def test_failures_have_fixed_secret_free_diagnostics(monkeypatch, caplog):
    async def fail_with_secrets(**_kwargs):
        raise RuntimeError(
            " ".join(
                (
                    PROVIDER_DETAIL,
                    CLIENT_SECRET,
                    AUTHORIZATION_CODE,
                    CODE_VERIFIER,
                    ACCESS_TOKEN,
                    REFRESH_TOKEN,
                )
            )
        )

    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_post_form_async",
        fail_with_secrets,
    )

    with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
        _run(_exchange())

    assert str(raised.value) == (
        "Google Calendar authorization could not be completed."
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, frame_values = _module_traceback(raised.value)
    assert names == [
        "exchange_google_calendar_authorization_code_with_deadline",
        "_raise_token_exchange_error",
    ]
    output = " ".join(
        (
            str(raised.value),
            repr(raised.value),
            _formatted_exception(raised.value),
            frame_values,
            caplog.text,
        )
    )
    for private in (
        PROVIDER_DETAIL,
        CLIENT_SECRET,
        AUTHORIZATION_CODE,
        CODE_VERIFIER,
        ACCESS_TOKEN,
        REFRESH_TOKEN,
    ):
        assert private not in output


def test_failure_clears_secret_bearing_ambient_context():
    ambient_secret = "ambient-token-deadline-private-detail"

    async def scenario():
        try:
            raise RuntimeError(ambient_secret)
        except RuntimeError:
            await _exchange(configuration=object())

    with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
        _run(scenario())

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    _, frame_values = _module_traceback(raised.value)
    assert ambient_secret not in " ".join(
        (_formatted_exception(raised.value), frame_values)
    )


def test_import_construction_and_startup_remain_inert(monkeypatch):
    calls = []

    async def forbidden(**_kwargs):
        calls.append(True)
        raise AssertionError("token network must remain call-time only")

    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_post_form_async",
        forbidden,
    )

    try:
        app = api.create_app()
        assert not hasattr(app.state, "google_calendar_token_deadline")
        assert "google_calendar_token_deadline" not in app.state.routing_readiness
        assert "db" not in google_calendar_token_deadline.__dict__
        assert calls == []
    finally:
        tools.configure_default_routing_provider(HaversineRoutingProvider())
        tools.reset_routing_provider()
