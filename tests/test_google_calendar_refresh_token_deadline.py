from __future__ import annotations

import asyncio
import dis
import inspect
import json
import sys
import traceback

import httpx
import pytest

from app import (
    google_calendar_token_deadline,
    google_calendar_token_httpx,
)
from app.google_calendar_authorization import GOOGLE_CALENDAR_FREEBUSY_SCOPE
from app.google_calendar_credential_claim import _new_claim
from app.google_calendar_oauth import (
    GOOGLE_CALENDAR_CALLBACK_PATH,
    GoogleCalendarOAuthConfiguration,
)
from app.google_calendar_token_deadline import (
    exchange_google_calendar_refresh_token_with_deadline,
)
from app.google_calendar_token_exchange import (
    GOOGLE_CALENDAR_TOKEN_ENDPOINT,
    GoogleCalendarRefreshAuthorizationRequired,
    GoogleCalendarRefreshTokenExchangeResult,
    GoogleCalendarTokenExchangeConfiguration,
    GoogleCalendarTokenExchangeError,
    GoogleCalendarTokenTransportResponse,
)


CLIENT_ID = "123456789-dayweave.apps.googleusercontent.com"
REDIRECT_URI = "https://api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH
CLIENT_SECRET = "client+secret/private&value="
ACCOUNT_ID = "acct_refresh_deadline_private"
INSTRUCTOR_ID = "inst_refresh_deadline_private"
CONNECTION_ID = "calconn_" + "a" * 32
REFRESH_TOKEN = "refresh-token-private-value"
ACCESS_TOKEN = "access-token-private-value"
PROVIDER_DETAIL = "provider-private-error-detail"


class PrivateBaseException(BaseException):
    pass


class HostileSystemExit(SystemExit):
    pass


class AsyncRecordingStream(httpx.AsyncByteStream):
    def __init__(
        self,
        chunks=(),
        *,
        stall_after_chunks=False,
        error=None,
        stall_close=False,
    ):
        self.chunks = tuple(chunks)
        self.stall_after_chunks = stall_after_chunks
        self.error = error
        self.stall_close = stall_close
        self.iteration_count = 0
        self.close_count = 0
        self.cancelled = False
        self.close_cancelled = False

    async def __aiter__(self):
        self.iteration_count += 1
        try:
            for chunk in self.chunks:
                yield chunk
            if self.error is not None:
                raise self.error
            if self.stall_after_chunks:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self):
        self.close_count += 1
        if self.stall_close:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.close_cancelled = True
                raise


class AsyncRecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler, *, stall_close=False):
        self.handler = handler
        self.stall_close = stall_close
        self.requests = []
        self.close_count = 0
        self.cancelled = False
        self.started = None
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
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.close_cancelled = True
                raise


def _configuration():
    return GoogleCalendarTokenExchangeConfiguration(
        authorization_configuration=GoogleCalendarOAuthConfiguration(
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
        ),
        client_secret=CLIENT_SECRET,
    )


def _claim(
    *,
    account_id=ACCOUNT_ID,
    instructor_id=INSTRUCTOR_ID,
    connection_id=CONNECTION_ID,
    revision=7,
    refresh_token=REFRESH_TOKEN,
):
    return _new_claim(
        account_id=account_id,
        instructor_id=instructor_id,
        connection_id=connection_id,
        expected_calendar_connection_revision=revision,
        refresh_token=refresh_token,
    )


def _payload_bytes():
    return json.dumps(
        {
            "access_token": ACCESS_TOKEN,
            "expires_in": 3600,
            "scope": GOOGLE_CALENDAR_FREEBUSY_SCOPE,
            "token_type": "Bearer",
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _response(*, status_code=200, body=None, stream=None, headers=None):
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


def _transport_response(*, status_code=200, body=None):
    return GoogleCalendarTokenTransportResponse(
        status_code=status_code,
        content_type="application/json",
        body=_payload_bytes() if body is None else body,
    )


def _install_transport(monkeypatch, handler, *, stall_close=False):
    transport = AsyncRecordingTransport(
        handler,
        stall_close=stall_close,
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
    configuration = overrides.get("configuration")
    if configuration is None:
        configuration = _configuration()
    claim = overrides.get("claim")
    if claim is None:
        claim = _claim()
    return await exchange_google_calendar_refresh_token_with_deadline(
        configuration=configuration,
        claim=claim,
    )


def _run(awaitable):
    return asyncio.run(awaitable)


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


def test_public_refresh_surface_is_async_and_keyword_only():
    assert inspect.iscoroutinefunction(
        exchange_google_calendar_refresh_token_with_deadline
    )
    signature = inspect.signature(
        exchange_google_calendar_refresh_token_with_deadline
    )
    assert list(signature.parameters) == ["configuration", "claim"]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )


def test_success_uses_one_exact_request_and_preserves_claim_binding(
    monkeypatch,
):
    stream = AsyncRecordingStream((_payload_bytes(),))
    transport, factory_calls = _install_transport(
        monkeypatch,
        lambda _request: _response(stream=stream),
    )

    result = _run(_exchange())

    assert type(result) is GoogleCalendarRefreshTokenExchangeResult
    assert result.account_id == ACCOUNT_ID
    assert result.instructor_id == INSTRUCTOR_ID
    assert result.connection_id == CONNECTION_ID
    assert result.expected_calendar_connection_revision == 7
    assert result.access_token == ACCESS_TOKEN
    assert result.expires_in_seconds == 3600
    assert factory_calls == [True]
    assert transport.close_count == 1
    assert stream.iteration_count == 1
    assert stream.close_count == 1
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.method == "POST"
    assert str(request.url) == GOOGLE_CALENDAR_TOKEN_ENDPOINT
    assert request.url.query == b""
    assert request.content == (
        b"client_id=123456789-dayweave.apps.googleusercontent.com"
        b"&client_secret=client%2Bsecret%2Fprivate%26value%3D"
        b"&grant_type=refresh_token"
        b"&refresh_token=refresh-token-private-value"
    )
    assert "authorization" not in request.headers
    assert "cookie" not in request.headers


def test_invalid_grant_returns_exact_claim_bound_recovery(monkeypatch):
    body = b'{"error":"invalid_grant"}'
    stream = AsyncRecordingStream((body,))
    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(
            status_code=400,
            body=body,
            stream=stream,
        ),
    )

    result = _run(_exchange())

    assert type(result) is GoogleCalendarRefreshAuthorizationRequired
    assert result.account_id == ACCOUNT_ID
    assert result.instructor_id == INSTRUCTOR_ID
    assert result.connection_id == CONNECTION_ID
    assert result.expected_calendar_connection_revision == 7
    assert stream.iteration_count == 1
    assert stream.close_count == 1
    assert transport.close_count == 1
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"configuration": object()},
        {"claim": object()},
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


def test_complete_claim_substitution_during_request_fails_closed(monkeypatch):
    original_claim = _claim()
    replacement_claim = _claim(
        account_id="acct_refresh_deadline_replacement",
        instructor_id="inst_refresh_deadline_replacement",
        connection_id="calconn_" + "b" * 32,
        revision=11,
        refresh_token="replacement-refresh-token-private",
    )

    def handler(_request):
        for field in (
            "account_id",
            "instructor_id",
            "connection_id",
            "expected_calendar_connection_revision",
            "refresh_token",
            "_integrity_tag",
        ):
            object.__setattr__(
                original_claim,
                field,
                getattr(replacement_claim, field),
            )
        return _response()

    transport, _ = _install_transport(monkeypatch, handler)

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _run(_exchange(claim=original_claim))

    assert len(transport.requests) == 1
    assert transport.close_count == 1


def test_overall_deadline_cancels_stalled_request_and_closes(monkeypatch):
    async def stall(_request):
        await asyncio.Event().wait()

    transport, factory_calls = _install_transport(monkeypatch, stall)
    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_TOKEN_EXCHANGE_DEADLINE_SECONDS",
        0.02,
    )

    with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
        _run(_exchange())

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert factory_calls == [True]
    assert len(transport.requests) == 1
    assert transport.cancelled is True
    assert transport.close_count == 1


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (200, _payload_bytes()),
        (400, b'{"error":"invalid_grant"}'),
    ],
)
def test_late_outcome_after_suppressed_cancellation_is_rejected(
    monkeypatch,
    status_code,
    body,
):
    cancellation_was_suppressed = []

    async def suppress_cancellation(**_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_was_suppressed.append(True)
            return _transport_response(status_code=status_code, body=body)

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
def test_final_monotonic_check_controls_refresh_outcome_acceptance(
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
        result = _run(_exchange())
        assert result.access_token == ACCESS_TOKEN
    assert deadlines == [105.0]


def test_external_cancellation_is_sanitized_and_secret_free(monkeypatch):
    async def stall(_request):
        await asyncio.Event().wait()

    transport, _ = _install_transport(monkeypatch, stall)

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
        "exchange_google_calendar_refresh_token_with_deadline",
        "_raise_cancelled",
    ]
    for private in (
        CLIENT_SECRET,
        REFRESH_TOKEN,
        ACCESS_TOKEN,
        ACCOUNT_ID,
        INSTRUCTOR_ID,
        CONNECTION_ID,
        PROVIDER_DETAIL,
    ):
        assert private not in values
    assert transport.cancelled is True
    assert transport.close_count == 1


def test_suppressed_external_cancellation_cannot_return_token(monkeypatch):
    request_started = asyncio.Event()
    cancellation_was_suppressed = []

    async def suppress_cancellation(**_kwargs):
        request_started.set()
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

    async def scenario():
        task = asyncio.create_task(_exchange())
        await request_started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError as error:
            return error
        raise AssertionError("suppressed cancellation must reject the token")

    raised = _run(scenario())

    assert cancellation_was_suppressed == [True]
    assert raised.__cause__ is None
    assert raised.__context__ is None
    assert raised.__suppress_context__ is True
    names, values = _module_traceback(raised)
    assert names == [
        "exchange_google_calendar_refresh_token_with_deadline",
        "_raise_cancelled",
    ]
    for private in (
        CLIENT_SECRET,
        REFRESH_TOKEN,
        ACCESS_TOKEN,
        ACCOUNT_ID,
        INSTRUCTOR_ID,
        CONNECTION_ID,
    ):
        assert private not in values


@pytest.mark.parametrize("failure_kind", ["malformed_response", "exception"])
def test_suppressed_external_cancellation_precedes_followup_failure(
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
                return _transport_response(body=b"{}")
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
        "exchange_google_calendar_refresh_token_with_deadline",
        "_raise_cancelled",
    ]
    for private in (
        CLIENT_SECRET,
        REFRESH_TOKEN,
        ACCOUNT_ID,
        INSTRUCTOR_ID,
        CONNECTION_ID,
        PROVIDER_DETAIL,
    ):
        assert private not in values


@pytest.mark.parametrize(
    (
        "exception_factory",
        "expected_type",
        "expected_helper",
        "expected_exit_code",
    ),
    [
        (
            lambda: KeyboardInterrupt(PROVIDER_DETAIL),
            KeyboardInterrupt,
            "_raise_control_exception",
            None,
        ),
        (
            lambda: SystemExit(7),
            SystemExit,
            "_raise_control_exception",
            7,
        ),
        (
            lambda: SystemExit(PROVIDER_DETAIL),
            SystemExit,
            "_raise_control_exception",
            1,
        ),
        (
            lambda: HostileSystemExit(PROVIDER_DETAIL),
            SystemExit,
            "_raise_control_exception",
            1,
        ),
        (
            lambda: GeneratorExit(PROVIDER_DETAIL),
            GeneratorExit,
            "_raise_control_exception",
            None,
        ),
        (
            lambda: PrivateBaseException(PROVIDER_DETAIL),
            GoogleCalendarTokenExchangeError,
            "_raise_token_exchange_error",
            None,
        ),
    ],
)
def test_public_refresh_boundary_sanitizes_every_base_exception_path(
    monkeypatch,
    exception_factory,
    expected_type,
    expected_helper,
    expected_exit_code,
):
    async def fail_transport(**_kwargs):
        raise exception_factory()

    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_post_form_async",
        fail_transport,
    )

    with pytest.raises(expected_type) as raised:
        _run(_exchange())

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    if expected_type is SystemExit:
        assert raised.value.code == expected_exit_code
    names, values = _module_traceback(raised.value)
    assert names == [
        "exchange_google_calendar_refresh_token_with_deadline",
        expected_helper,
    ]
    output = " ".join(
        (
            repr(raised.value),
            "".join(traceback.format_exception(raised.value)),
            values,
        )
    )
    for private in (
        CLIENT_SECRET,
        REFRESH_TOKEN,
        ACCESS_TOKEN,
        ACCOUNT_ID,
        INSTRUCTOR_ID,
        CONNECTION_ID,
        PROVIDER_DETAIL,
    ):
        assert private not in output


def test_refresh_boundary_entry_interruption_is_sanitized():
    guarded = exchange_google_calendar_refresh_token_with_deadline
    interrupted = False

    def interrupt_entry(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is guarded.__code__
        ):
            interrupted = True
            raise KeyboardInterrupt("refresh-entry-control-private")
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
        "exchange_google_calendar_refresh_token_with_deadline",
        "_raise_control_exception",
    ]
    output = " ".join(
        (
            repr(raised.value),
            "".join(traceback.format_exception(raised.value)),
            values,
        )
    )
    for private in (
        "refresh-entry-control-private",
        CLIENT_SECRET,
        REFRESH_TOKEN,
        ACCOUNT_ID,
        INSTRUCTOR_ID,
        CONNECTION_ID,
    ):
        assert private not in output


def test_refresh_boundary_cleanup_interruption_is_sanitized():
    guarded = exchange_google_calendar_refresh_token_with_deadline
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
    ) >= 2
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
            raise KeyboardInterrupt("refresh-cleanup-control-private")
        return interrupt_cleanup

    try:
        sys.settrace(interrupt_cleanup)
        with pytest.raises(KeyboardInterrupt) as raised:
            _run(_exchange(claim=object()))
    finally:
        sys.settrace(None)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, values = _module_traceback(raised.value)
    assert names == [
        "exchange_google_calendar_refresh_token_with_deadline",
        "_raise_control_exception",
    ]
    output = " ".join(
        (
            repr(raised.value),
            "".join(traceback.format_exception(raised.value)),
            values,
        )
    )
    for private in (
        "refresh-cleanup-control-private",
        CLIENT_SECRET,
        REFRESH_TOKEN,
        ACCOUNT_ID,
        INSTRUCTOR_ID,
        CONNECTION_ID,
    ):
        assert private not in output


def test_refresh_boundary_exception_entry_interruption_is_sanitized():
    guarded = (
        google_calendar_token_deadline
        ._guarded_google_calendar_refresh_token_with_deadline
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
            raise KeyboardInterrupt("refresh-except-entry-private")
        return interrupt_exception_entry

    try:
        sys.settrace(interrupt_exception_entry)
        with pytest.raises(KeyboardInterrupt) as raised:
            _run(_exchange(claim=object()))
    finally:
        sys.settrace(None)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, values = _module_traceback(raised.value)
    assert names == [
        "exchange_google_calendar_refresh_token_with_deadline",
        "_raise_control_exception",
    ]
    output = " ".join(
        (
            repr(raised.value),
            "".join(traceback.format_exception(raised.value)),
            values,
        )
    )
    for private in (
        "refresh-except-entry-private",
        CLIENT_SECRET,
        REFRESH_TOKEN,
        ACCOUNT_ID,
        INSTRUCTOR_ID,
        CONNECTION_ID,
    ):
        assert private not in output


def test_reentrant_public_refresh_handler_interruption_is_secret_free(
    monkeypatch,
):
    guarded = exchange_google_calendar_refresh_token_with_deadline
    configuration = _configuration()
    claim = _claim()
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

    async def return_token_response(**_kwargs):
        return _transport_response()

    def fail_after_response(*_args, **_kwargs):
        raise RuntimeError(f"{ACCESS_TOKEN}:{PROVIDER_DETAIL}")

    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_post_form_async",
        return_token_response,
    )
    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_complete_google_calendar_refresh_token_exchange",
        fail_after_response,
    )

    try:
        sys.settrace(interrupt_public_handler)
        with pytest.raises(KeyboardInterrupt) as raised:
            _run(_exchange(configuration=configuration, claim=claim))
    finally:
        sys.settrace(None)

    names, values = _module_traceback(raised.value)
    assert names == [
        "exchange_google_calendar_refresh_token_with_deadline"
    ]
    chained_module_locals = []
    pending_errors = [raised.value]
    seen_errors = set()
    while pending_errors:
        current_error = pending_errors.pop()
        if id(current_error) in seen_errors:
            continue
        seen_errors.add(id(current_error))
        current_frame = current_error.__traceback__
        while current_frame is not None:
            if current_frame.tb_frame.f_code.co_filename.endswith(
                "/app/google_calendar_token_deadline.py"
            ):
                chained_module_locals.extend(
                    current_frame.tb_frame.f_locals.values()
                )
                assert all(
                    value is not configuration and value is not claim
                    for value in current_frame.tb_frame.f_locals.values()
                )
            current_frame = current_frame.tb_next
        for related_error in (
            current_error.__cause__,
            current_error.__context__,
        ):
            if (
                related_error is not None
                and id(related_error) not in seen_errors
            ):
                pending_errors.append(related_error)
    output = " ".join(
        (
            repr(raised.value),
            "".join(traceback.format_exception(raised.value)),
            values,
            repr(chained_module_locals),
        )
    )
    for private in (
        CLIENT_SECRET,
        REFRESH_TOKEN,
        ACCESS_TOKEN,
        ACCOUNT_ID,
        INSTRUCTOR_ID,
        CONNECTION_ID,
        PROVIDER_DETAIL,
    ):
        assert private not in output


def test_refresh_http_400_rejects_compressed_or_oversized_body(monkeypatch):
    compressed_stream = AsyncRecordingStream((b'{"error":"invalid_grant"}',))
    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(
            status_code=400,
            stream=compressed_stream,
            headers=[
                ("content-type", "application/json"),
                ("content-encoding", "gzip"),
            ],
        ),
    )

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _run(_exchange())

    assert compressed_stream.close_count == 1
    assert transport.close_count == 1

    oversized_stream = AsyncRecordingStream((b"x" * (64 * 1024 + 1),))
    transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(
            status_code=400,
            stream=oversized_stream,
            headers=[("content-type", "application/json")],
        ),
    )

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _run(_exchange())

    assert oversized_stream.close_count == 1
    assert transport.close_count == 1
