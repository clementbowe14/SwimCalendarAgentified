from __future__ import annotations

import asyncio
import datetime
import inspect
import itertools
import json
import pickle
import traceback

import httpx
import pytest

from app import (
    google_calendar_freebusy,
    google_calendar_freebusy_deadline,
    google_calendar_token_httpx,
)
from app.external_calendar import (
    CalendarBusyInterval,
    ExternalCalendarAuthorizationRequired,
    ExternalCalendarInvalidResponse,
    ExternalCalendarTimeout,
    ExternalCalendarUnavailable,
)
from app.google_calendar_credential_handoff import _new_claim
from app.google_calendar_freebusy import (
    GOOGLE_CALENDAR_FREEBUSY_ENDPOINT,
    GoogleCalendarFreeBusyQueryResult,
    GoogleCalendarFreeBusyTransportResponse,
)
from app.google_calendar_freebusy_deadline import (
    query_google_calendar_freebusy_with_deadline,
)
from app.google_calendar_token_exchange import _new_refresh_exchange_result


UTC = datetime.timezone.utc
ACCOUNT_ID = "acct_freebusy_deadline_private"
INSTRUCTOR_ID = "inst_freebusy_deadline_private"
CONNECTION_ID = "calconn_" + "a" * 32
REFRESH_TOKEN = "refresh-freebusy-deadline-private"
ACCESS_TOKEN = "access-freebusy-deadline-private"
PROVIDER_DETAIL = "provider-freebusy-deadline-private"
WINDOW_START = datetime.datetime(2026, 11, 19, 10, tzinfo=UTC)
WINDOW_END = datetime.datetime(2026, 11, 19, 16, tzinfo=UTC)


class HybridTimeoutCancellation(TimeoutError, asyncio.CancelledError):
    pass


class PassiveScope:
    def __init__(self, *, cancel_called=False):
        self.cancel_called = cancel_called

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return False


class TrackingScope(PassiveScope):
    def __init__(self, *, expire_on_exit=False):
        super().__init__()
        self.active = False
        self.expire_on_exit = expire_on_exit

    def __enter__(self):
        self.active = True
        return self

    def __exit__(self, _type, _value, _traceback):
        self.active = False
        if self.expire_on_exit:
            self.cancel_called = True
        return False


class DeadlineAsyncStream(httpx.AsyncByteStream):
    def __init__(self, body, *, stall_iteration=False, stall_close=False):
        self.body = body
        self.stall_iteration = stall_iteration
        self.stall_close = stall_close
        self.iteration_count = 0
        self.close_count = 0
        self.iteration_cancelled = False
        self.close_cancelled = False

    async def __aiter__(self):
        self.iteration_count += 1
        if self.stall_iteration:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.iteration_cancelled = True
                raise
        yield self.body

    async def aclose(self):
        self.close_count += 1
        if self.stall_close:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.close_cancelled = True
                raise


class DeadlineAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, stream, *, stall_close=False):
        self.stream = stream
        self.stall_close = stall_close
        self.requests = []
        self.request_bodies = []
        self.close_count = 0
        self.close_cancelled = False

    async def handle_async_request(self, request):
        self.requests.append(request)
        self.request_bodies.append(await request.aread())
        return httpx.Response(
            200,
            headers=[
                ("content-type", "application/json"),
                ("content-length", str(len(self.stream.body))),
            ],
            stream=self.stream,
        )

    async def aclose(self):
        self.close_count += 1
        if self.stall_close:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.close_cancelled = True
                raise


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


def _access_result(
    *,
    account_id=ACCOUNT_ID,
    instructor_id=INSTRUCTOR_ID,
    connection_id=CONNECTION_ID,
    revision=7,
    access_token=ACCESS_TOKEN,
    expires_in_seconds=3600,
):
    return _new_refresh_exchange_result(
        account_id=account_id,
        instructor_id=instructor_id,
        connection_id=connection_id,
        expected_calendar_connection_revision=revision,
        access_token=access_token,
        expires_in_seconds=expires_in_seconds,
    )


def _payload(*, start=WINDOW_START, end=WINDOW_END, busy=None):
    if busy is None:
        busy = [
            {
                "start": "2026-11-19T10:30:00Z",
                "end": "2026-11-19T11:00:00Z",
            }
        ]
    canonical = lambda value: value.isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    return {
        "kind": "calendar#freeBusy",
        "timeMin": canonical(start),
        "timeMax": canonical(end),
        "calendars": {"resolved@example.com": {"busy": busy}},
    }


def _response(*, status_code=200, payload=None, body=None):
    if body is None:
        body = json.dumps(
            _payload() if payload is None else payload,
            separators=(",", ":"),
        ).encode("utf-8")
    return GoogleCalendarFreeBusyTransportResponse(
        status_code=status_code,
        content_type="application/json" if status_code == 200 else None,
        body=body if status_code == 200 else b"",
    )


async def _query(**overrides):
    claim = overrides.get("claim")
    if claim is None:
        claim = _claim()
    access_result = overrides.get("access_result")
    if access_result is None:
        access_result = _access_result()
    return await query_google_calendar_freebusy_with_deadline(
        claim=claim,
        access_result=access_result,
        window_start=overrides.get("window_start", WINDOW_START),
        window_end=overrides.get("window_end", WINDOW_END),
    )


def _run(awaitable):
    return asyncio.run(awaitable)


def _formatted(error):
    return "".join(traceback.format_exception(error))


def _module_traceback_values(error):
    values = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith(
            "/app/google_calendar_freebusy_deadline.py"
        ):
            values.extend(current.tb_frame.f_locals.values())
        current = current.tb_next
    return repr(values)


def test_public_surface_is_exact_async_and_keyword_only():
    assert google_calendar_freebusy_deadline.__all__ == (
        "query_google_calendar_freebusy_with_deadline",
    )
    assert inspect.iscoroutinefunction(
        query_google_calendar_freebusy_with_deadline
    )
    signature = inspect.signature(
        query_google_calendar_freebusy_with_deadline
    )
    assert list(signature.parameters) == [
        "claim",
        "access_result",
        "window_start",
        "window_end",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )


def test_owned_transport_outcome_rejects_slot_mutation_and_deletion():
    outcome = google_calendar_freebusy_deadline._new_owned_transport_outcome(
        value=object()
    )

    with pytest.raises(AttributeError):
        outcome.value = object()
    with pytest.raises(AttributeError):
        del outcome.value
    with pytest.raises(TypeError):
        pickle.dumps(outcome)

    assert repr(outcome) == "_OwnedTransportOutcome(<redacted>)"


def test_success_composes_exact_transport_request_and_claim_bound_result(
    monkeypatch,
):
    calls = []

    async def post(**kwargs):
        calls.append(kwargs)
        return _response()

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        post,
    )

    result = _run(_query())

    assert type(result) is GoogleCalendarFreeBusyQueryResult
    assert result.account_id == ACCOUNT_ID
    assert result.instructor_id == INSTRUCTOR_ID
    assert result.connection_id == CONNECTION_ID
    assert result.expected_calendar_connection_revision == 7
    assert result.periods == (
        CalendarBusyInterval(
            start=datetime.datetime(2026, 11, 19, 10, 30, tzinfo=UTC),
            end=datetime.datetime(2026, 11, 19, 11, tzinfo=UTC),
        ),
    )
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == GOOGLE_CALENDAR_FREEBUSY_ENDPOINT
    assert call["access_token"] == ACCESS_TOKEN
    assert call["timeout_seconds"] == 5.0
    assert type(call["deadline"]) is float
    assert json.loads(call["body"]) == {
        "timeMin": "2026-11-19T10:00:00Z",
        "timeMax": "2026-11-19T16:00:00Z",
        "timeZone": "UTC",
        "calendarExpansionMax": 1,
        "items": [{"id": "primary"}],
    }
    assert ACCESS_TOKEN.encode() not in call["body"]
    assert REFRESH_TOKEN.encode() not in call["body"]


@pytest.mark.parametrize(
    ("overrides", "expected_type"),
    [
        ({"claim": object()}, ExternalCalendarInvalidResponse),
        ({"access_result": object()}, ExternalCalendarInvalidResponse),
        (
            {
                "access_result": _access_result(
                    instructor_id="inst_other_private"
                )
            },
            ExternalCalendarInvalidResponse,
        ),
        (
            {"window_start": datetime.datetime(2026, 11, 19, 10)},
            ExternalCalendarInvalidResponse,
        ),
        ({"window_end": WINDOW_START}, ExternalCalendarInvalidResponse),
    ],
)
def test_invalid_inputs_fail_before_clock_scope_or_transport(
    monkeypatch,
    overrides,
    expected_type,
):
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("deadline/network must not be created")

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_monotonic_time",
        forbidden,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_new_deadline_scope",
        forbidden,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        forbidden,
    )

    with pytest.raises(expected_type):
        _run(_query(**overrides))

    assert calls == []


@pytest.mark.parametrize(
    ("failure", "expected_type", "reason"),
    [
        (TimeoutError(PROVIDER_DETAIL), ExternalCalendarTimeout, "timeout"),
        (
            ValueError(PROVIDER_DETAIL),
            ExternalCalendarInvalidResponse,
            "invalid_response",
        ),
        (
            RuntimeError(PROVIDER_DETAIL),
            ExternalCalendarUnavailable,
            "provider_unavailable",
        ),
    ],
)
def test_transport_failures_are_phase_mapped_and_sanitized(
    monkeypatch,
    failure,
    expected_type,
    reason,
):
    async def post(**_kwargs):
        raise failure

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        post,
    )

    with pytest.raises(expected_type) as raised:
        _run(_query())

    assert raised.value.reason_code == reason
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)


@pytest.mark.parametrize(
    ("failure_factory", "expected_type", "expected_code"),
    [
        (
            lambda: asyncio.CancelledError(PROVIDER_DETAIL),
            asyncio.CancelledError,
            None,
        ),
        (
            lambda: KeyboardInterrupt(PROVIDER_DETAIL),
            KeyboardInterrupt,
            None,
        ),
        (
            lambda: SystemExit(PROVIDER_DETAIL),
            SystemExit,
            1,
        ),
        (
            lambda: GeneratorExit(PROVIDER_DETAIL),
            GeneratorExit,
            None,
        ),
    ],
)
def test_direct_child_controls_are_reemitted_fresh_from_parent(
    monkeypatch,
    failure_factory,
    expected_type,
    expected_code,
):
    failure = failure_factory()

    async def post(**_kwargs):
        raise failure

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        post,
    )

    with pytest.raises(expected_type) as raised:
        _run(_query())

    assert type(raised.value) is expected_type
    assert raised.value is not failure
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    if expected_type is SystemExit:
        assert raised.value.code == expected_code
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_secondary_control_during_child_failure_classification_is_preserved(
    monkeypatch,
):
    original_classifier = (
        google_calendar_freebusy_deadline
        ._classified_non_cancel_control_exception
    )
    secondary = KeyboardInterrupt(PROVIDER_DETAIL)
    calls = []

    async def post(**_kwargs):
        raise RuntimeError(PROVIDER_DETAIL)

    def interrupt_once(raised):
        calls.append(raised)
        if len(calls) == 1:
            raise secondary
        return original_classifier(raised)

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        post,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_classified_non_cancel_control_exception",
        interrupt_once,
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        _run(_query())

    assert len(calls) == 1
    assert type(raised.value) is KeyboardInterrupt
    assert raised.value is not secondary
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_original_child_control_survives_ordinary_classifier_failure(
    monkeypatch,
):
    original = KeyboardInterrupt(PROVIDER_DETAIL)

    async def post(**_kwargs):
        raise original

    def fail_classifier(_raised):
        raise RuntimeError(PROVIDER_DETAIL)

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        post,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_classified_non_cancel_control_exception",
        fail_classifier,
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        _run(_query())

    assert type(raised.value) is KeyboardInterrupt
    assert raised.value is not original
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_original_child_control_survives_emergency_traversal_failure(
    monkeypatch,
):
    original = KeyboardInterrupt(PROVIDER_DETAIL)
    original_graph = (
        google_calendar_freebusy_deadline._bounded_unique_exception_graph
    )
    graph_calls = []

    async def post(**_kwargs):
        raise original

    def fail_classifier(_raised):
        raise RuntimeError(PROVIDER_DETAIL)

    def fail_first_graph(raised):
        graph_calls.append(raised)
        if len(graph_calls) == 1:
            raise RuntimeError(PROVIDER_DETAIL)
        return original_graph(raised)

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        post,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_classified_non_cancel_control_exception",
        fail_classifier,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_bounded_unique_exception_graph",
        fail_first_graph,
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        _run(_query())

    assert len(graph_calls) >= 2
    assert type(raised.value) is KeyboardInterrupt
    assert raised.value is not original
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_secondary_non_cancel_control_outranks_original_child_cancel(
    monkeypatch,
):
    original = asyncio.CancelledError(PROVIDER_DETAIL)
    secondary = KeyboardInterrupt(PROVIDER_DETAIL)

    async def post(**_kwargs):
        raise original

    def interrupt_classifier(_raised):
        raise secondary

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        post,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_classified_non_cancel_control_exception",
        interrupt_classifier,
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        _run(_query())

    assert type(raised.value) is KeyboardInterrupt
    assert raised.value is not secondary
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_control_during_child_success_outcome_factory_is_preserved(
    monkeypatch,
):
    original_factory = (
        google_calendar_freebusy_deadline._new_owned_transport_outcome
    )
    secondary = KeyboardInterrupt(PROVIDER_DETAIL)
    calls = []

    def interrupt_once(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise secondary
        return original_factory(**kwargs)

    async def post(**_kwargs):
        return _response()

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        post,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_new_owned_transport_outcome",
        interrupt_once,
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        _run(_query())

    assert len(calls) == 2
    assert type(raised.value) is KeyboardInterrupt
    assert raised.value is not secondary
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_outer_child_control_survives_ordinary_classifier_failure(
    monkeypatch,
):
    original_factory = (
        google_calendar_freebusy_deadline._new_owned_transport_outcome
    )
    original = KeyboardInterrupt(PROVIDER_DETAIL)
    factory_calls = []

    async def post(**_kwargs):
        return _response()

    def interrupt_first_factory(**kwargs):
        factory_calls.append(kwargs)
        if len(factory_calls) == 1:
            raise original
        return original_factory(**kwargs)

    def fail_classifier(_raised):
        raise RuntimeError(PROVIDER_DETAIL)

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        post,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_new_owned_transport_outcome",
        interrupt_first_factory,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_classified_non_cancel_control_exception",
        fail_classifier,
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        _run(_query())

    assert len(factory_calls) >= 3
    assert type(raised.value) is KeyboardInterrupt
    assert raised.value is not original
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_outcome_creation_bypasses_repeated_constructor_controls(
    monkeypatch,
):
    constructor_calls = []

    async def post(**_kwargs):
        return _response()

    def always_interrupt(*_args, **_kwargs):
        constructor_calls.append(True)
        raise KeyboardInterrupt(PROVIDER_DETAIL)

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        post,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline._OwnedTransportOutcome,
        "__init__",
        always_interrupt,
    )

    result = _run(_query())

    assert type(result) is GoogleCalendarFreeBusyQueryResult
    assert constructor_calls == []


@pytest.mark.parametrize("phase", ["preparation", "transport", "completion"])
def test_hybrid_timeout_cancellation_wins_in_every_phase(monkeypatch, phase):
    if phase == "preparation":
        def fail_preparation(**_kwargs):
            raise HybridTimeoutCancellation(PROVIDER_DETAIL)

        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_prepare_google_calendar_freebusy_query",
            fail_preparation,
        )
    else:
        async def post(**_kwargs):
            if phase == "transport":
                raise HybridTimeoutCancellation(PROVIDER_DETAIL)
            return _response()

        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_post_json_async",
            post,
        )
        if phase == "completion":
            def fail_completion(*_args, **_kwargs):
                raise HybridTimeoutCancellation(PROVIDER_DETAIL)

            monkeypatch.setattr(
                google_calendar_freebusy_deadline,
                "_complete_google_calendar_freebusy_query",
                fail_completion,
            )

    with pytest.raises(asyncio.CancelledError) as raised:
        _run(_query())

    assert type(raised.value) is asyncio.CancelledError
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)


@pytest.mark.parametrize(
    ("response", "expected_type", "reason"),
    [
        (
            _response(status_code=401),
            ExternalCalendarAuthorizationRequired,
            "authorization_required",
        ),
        (
            _response(status_code=403),
            ExternalCalendarUnavailable,
            "provider_unavailable",
        ),
        (
            _response(status_code=429),
            ExternalCalendarUnavailable,
            "provider_unavailable",
        ),
        (
            _response(status_code=503),
            ExternalCalendarUnavailable,
            "provider_unavailable",
        ),
        (
            _response(status_code=302),
            ExternalCalendarInvalidResponse,
            "invalid_response",
        ),
        (
            _response(body=b"not-json"),
            ExternalCalendarInvalidResponse,
            "invalid_response",
        ),
        (
            object(),
            ExternalCalendarInvalidResponse,
            "invalid_response",
        ),
        (
            None,
            ExternalCalendarInvalidResponse,
            "invalid_response",
        ),
    ],
)
def test_provider_and_protocol_outcomes_keep_existing_semantics(
    monkeypatch,
    response,
    expected_type,
    reason,
):
    async def post(**_kwargs):
        return response

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        post,
    )

    with pytest.raises(expected_type) as raised:
        _run(_query())

    assert raised.value.reason_code == reason
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize("target", ["claim", "access_result", "both"])
def test_mutation_during_transport_fails_after_one_request(monkeypatch, target):
    claim = _claim()
    access_result = _access_result()
    calls = []

    async def post(**kwargs):
        calls.append(kwargs)
        if target in {"claim", "both"}:
            replacement = _claim(
                account_id="acct_mutated_private",
                instructor_id="inst_mutated_private",
                connection_id="calconn_" + "b" * 32,
                revision=11,
                refresh_token="refresh-mutated-private",
            )
            for name in (
                "account_id",
                "instructor_id",
                "connection_id",
                "expected_calendar_connection_revision",
                "refresh_token",
                "_integrity_tag",
            ):
                object.__setattr__(claim, name, getattr(replacement, name))
        if target in {"access_result", "both"}:
            if target == "both":
                replacement = _access_result(
                    account_id="acct_mutated_private",
                    instructor_id="inst_mutated_private",
                    connection_id="calconn_" + "b" * 32,
                    revision=11,
                    access_token="access-mutated-private",
                    expires_in_seconds=1800,
                )
            else:
                replacement = _access_result(
                    access_token="access-mutated-private",
                    expires_in_seconds=1800,
                )
            for name in (
                "account_id",
                "instructor_id",
                "connection_id",
                "expected_calendar_connection_revision",
                "access_token",
                "expires_in_seconds",
                "_integrity_tag",
            ):
                object.__setattr__(
                    access_result,
                    name,
                    getattr(replacement, name),
                )
        return _response()

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        post,
    )

    with pytest.raises(ExternalCalendarInvalidResponse):
        _run(_query(claim=claim, access_result=access_result))

    assert len(calls) == 1


def test_real_stalled_transport_is_cancelled_by_overall_deadline(monkeypatch):
    cancelled = []

    async def stall(**_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_FREEBUSY_DEADLINE_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        stall,
    )

    async def scenario():
        async with asyncio.timeout(1.0):
            return await _query()

    with pytest.raises(ExternalCalendarTimeout) as raised:
        _run(scenario())

    assert raised.value.reason_code == "timeout"
    assert cancelled == [True]


@pytest.mark.parametrize("after_cancel", ["return", "raise"])
def test_suppressed_internal_cancellation_still_times_out(
    monkeypatch,
    after_cancel,
):
    async def suppress(**_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if after_cancel == "raise":
                raise RuntimeError(PROVIDER_DETAIL)
            return _response()

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_FREEBUSY_DEADLINE_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        suppress,
    )

    async def scenario():
        return await asyncio.wait_for(_query(), timeout=1.0)

    with pytest.raises(ExternalCalendarTimeout) as raised:
        _run(scenario())

    assert raised.value.reason_code == "timeout"
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_repeated_child_cancellation_suppression_cannot_extend_deadline(
    monkeypatch,
):
    state = {"cancellations": 0}

    async def scenario():
        release = asyncio.Event()
        cleaned = asyncio.Event()
        consumed = asyncio.Event()
        original_consume = (
            google_calendar_freebusy_deadline._consume_owned_task_result
        )

        async def suppress_repeated_cancellation(**_kwargs):
            task = asyncio.current_task()
            state["task"] = task
            try:
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        task.uncancel()
                        state["cancellations"] += 1
                raise RuntimeError(PROVIDER_DETAIL)
            finally:
                cleaned.set()

        def record_consumed(task):
            original_consume(task)
            consumed.set()

        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_post_json_async",
            suppress_repeated_cancellation,
        )
        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_consume_owned_task_result",
            record_consumed,
        )
        started_at = asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(_query(), timeout=1.0)
        except ExternalCalendarTimeout:
            state["elapsed"] = (
                asyncio.get_running_loop().time() - started_at
            )
            state["done_before_release"] = state["task"].done()
            release.set()
            done, _pending = await asyncio.wait(
                {state["task"]},
                timeout=0.5,
            )
            assert state["task"] in done
            await asyncio.wait_for(consumed.wait(), timeout=0.5)
            state["cleaned_before_runner_shutdown"] = cleaned.is_set()
            state["consumed_before_runner_shutdown"] = consumed.is_set()
            raise

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_FREEBUSY_DEADLINE_SECONDS",
        0.02,
    )

    with pytest.raises(ExternalCalendarTimeout) as raised:
        _run(scenario())

    assert raised.value.reason_code == "timeout"
    assert state["elapsed"] < 0.15
    assert state["cancellations"] >= 1
    assert state["done_before_release"] is False
    assert state["cleaned_before_runner_shutdown"] is True
    assert state["consumed_before_runner_shutdown"] is True
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_grouped_internal_deadline_cancellation_is_timeout(monkeypatch):
    async def group_internal_cancel(**_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as cancelled:
            raise BaseExceptionGroup(
                PROVIDER_DETAIL,
                [cancelled, RuntimeError(PROVIDER_DETAIL)],
            )

    async def scenario():
        return await asyncio.wait_for(_query(), timeout=1.0)

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_FREEBUSY_DEADLINE_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        group_internal_cancel,
    )

    with pytest.raises(ExternalCalendarTimeout) as raised:
        _run(scenario())

    assert raised.value.reason_code == "timeout"
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_non_cancel_control_grouped_before_deadline_wins(monkeypatch):
    async def group_controls(**_kwargs):
        raise BaseExceptionGroup(
            PROVIDER_DETAIL,
            [
                KeyboardInterrupt(PROVIDER_DETAIL),
                asyncio.CancelledError(PROVIDER_DETAIL),
            ],
        )

    async def scenario():
        async with asyncio.timeout(1.0):
            return await _query()

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        group_controls,
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        _run(scenario())

    assert type(raised.value) is KeyboardInterrupt
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)


@pytest.mark.parametrize(
    "phase",
    ["response_stream", "response_close", "client_close"],
)
def test_public_deadline_governs_real_adapter_io_and_cleanup(
    monkeypatch,
    phase,
):
    body = json.dumps(_payload(), separators=(",", ":")).encode("utf-8")
    stream = DeadlineAsyncStream(
        body,
        stall_iteration=phase == "response_stream",
        stall_close=phase == "response_close",
    )
    transport = DeadlineAsyncTransport(
        stream,
        stall_close=phase == "client_close",
    )
    factory_calls = []

    def factory(**kwargs):
        factory_calls.append(kwargs)
        return transport

    async def scenario():
        return await asyncio.wait_for(_query(), timeout=1.0)

    monkeypatch.setattr(
        google_calendar_token_httpx,
        "_new_async_http_transport",
        factory,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_FREEBUSY_DEADLINE_SECONDS",
        0.01,
    )

    with pytest.raises(ExternalCalendarTimeout):
        _run(scenario())

    assert len(factory_calls) == 1
    assert len(transport.requests) == 1
    assert transport.close_count == 1
    assert stream.close_count == 1
    if phase == "response_stream":
        assert stream.iteration_cancelled is True
    elif phase == "response_close":
        assert stream.close_cancelled is True
    else:
        assert transport.close_cancelled is True


@pytest.mark.parametrize(
    ("final_time", "expected_type"),
    [
        (104.999999, GoogleCalendarFreeBusyQueryResult),
        (105.0, ExternalCalendarTimeout),
    ],
)
def test_final_monotonic_check_enforces_exact_deadline(
    monkeypatch,
    final_time,
    expected_type,
):
    times = iter((100.0, final_time))
    scope_deadlines = []

    async def post(**kwargs):
        assert kwargs["deadline"] == 105.0
        return _response()

    def new_scope(*, deadline):
        scope_deadlines.append(deadline)
        return PassiveScope()

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_monotonic_time",
        lambda: next(times),
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_new_deadline_scope",
        new_scope,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        post,
    )

    if expected_type is GoogleCalendarFreeBusyQueryResult:
        assert type(_run(_query())) is expected_type
    else:
        with pytest.raises(expected_type):
            _run(_query())
    assert scope_deadlines == [105.0]


def test_completion_and_mutation_fence_run_inside_deadline_scope(monkeypatch):
    scope = TrackingScope()
    original_complete = (
        google_calendar_freebusy_deadline
        ._complete_google_calendar_freebusy_query
    )

    async def post(**_kwargs):
        return _response()

    def complete(*args, **kwargs):
        assert scope.active is True
        return original_complete(*args, **kwargs)

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_new_deadline_scope",
        lambda **_kwargs: scope,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        post,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_complete_google_calendar_freebusy_query",
        complete,
    )

    result = _run(_query())

    assert type(result) is GoogleCalendarFreeBusyQueryResult
    assert scope.active is False


def test_deadline_expiration_during_completion_beats_protocol_failure(
    monkeypatch,
):
    scope = TrackingScope(expire_on_exit=True)

    async def post(**_kwargs):
        return _response()

    def fail_completion(*_args, **_kwargs):
        assert scope.active is True
        raise ValueError(PROVIDER_DETAIL)

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_new_deadline_scope",
        lambda **_kwargs: scope,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        post,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_complete_google_calendar_freebusy_query",
        fail_completion,
    )

    with pytest.raises(ExternalCalendarTimeout) as raised:
        _run(_query())

    assert raised.value.reason_code == "timeout"
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_real_external_task_cancellation_is_fresh_and_cause_free(monkeypatch):
    started = None

    async def scenario():
        nonlocal started
        started = asyncio.Event()

        async def stall(**_kwargs):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_post_json_async",
            stall,
        )
        task = asyncio.create_task(_query())
        await started.wait()
        task.cancel(PROVIDER_DETAIL)
        await task

    with pytest.raises(asyncio.CancelledError) as raised:
        _run(scenario())

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_suppressed_external_cancellation_wins_over_malformed_response(
    monkeypatch,
):
    started = None

    async def scenario():
        nonlocal started
        started = asyncio.Event()

        async def suppress(**_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return _response(body=b"malformed-private-body")

        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_post_json_async",
            suppress,
        )
        task = asyncio.create_task(_query())
        await started.wait()
        task.cancel(PROVIDER_DETAIL)
        await task

    with pytest.raises(asyncio.CancelledError) as raised:
        _run(scenario())

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_dependency_uncancel_cannot_erase_caller_cancellation(monkeypatch):
    started = None

    async def scenario():
        nonlocal started
        started = asyncio.Event()

        async def suppress_and_uncancel(**_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                asyncio.current_task().uncancel()
                return _response()

        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_post_json_async",
            suppress_and_uncancel,
        )
        task = asyncio.create_task(_query())
        await started.wait()
        task.cancel(PROVIDER_DETAIL)
        await task

    with pytest.raises(asyncio.CancelledError) as raised:
        _run(asyncio.wait_for(scenario(), timeout=1.0))

    assert type(raised.value) is asyncio.CancelledError
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_parent_control_cancels_and_awaits_owned_transport_task(monkeypatch):
    state = {}

    async def scenario():
        started = asyncio.Event()
        cleaned = asyncio.Event()
        state["cleaned"] = cleaned

        async def post(**_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        async def interrupt_after_start(task):
            state["task"] = task
            await started.wait()
            raise KeyboardInterrupt(PROVIDER_DETAIL)

        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_post_json_async",
            post,
        )
        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_await_owned_transport_task",
            interrupt_after_start,
        )
        try:
            async with asyncio.timeout(1.0):
                await _query()
        except KeyboardInterrupt as raised:
            assert type(raised) is KeyboardInterrupt
            assert cleaned.is_set()
            assert state["task"].done()
            state["checked_before_runner_shutdown"] = True
            raise

    with pytest.raises(KeyboardInterrupt) as raised:
        _run(scenario())

    assert type(raised.value) is KeyboardInterrupt
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)
    assert state["checked_before_runner_shutdown"] is True
    assert state["cleaned"].is_set()
    assert state["task"].done()


def test_cleanup_preserves_child_non_cancel_control_before_cutoff(
    monkeypatch,
):
    async def scenario():
        started = asyncio.Event()

        async def control_during_cleanup(**_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise KeyboardInterrupt(PROVIDER_DETAIL)

        async def parent_failure(_task):
            await started.wait()
            raise RuntimeError(PROVIDER_DETAIL)

        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_post_json_async",
            control_during_cleanup,
        )
        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_await_owned_transport_task",
            parent_failure,
        )
        return await _query()

    with pytest.raises(KeyboardInterrupt) as raised:
        _run(scenario())

    assert type(raised.value) is KeyboardInterrupt
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_owned_task_cleanup_is_bounded_by_original_deadline(monkeypatch):
    state = {}

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        cleaned = asyncio.Event()
        consumed = asyncio.Event()
        original_consume = (
            google_calendar_freebusy_deadline._consume_owned_task_result
        )

        async def suppress_repeated_cancellation(**_kwargs):
            started.set()
            try:
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        asyncio.current_task().uncancel()
                        state["cancellations"] = (
                            state.get("cancellations", 0) + 1
                        )
                raise RuntimeError(PROVIDER_DETAIL)
            finally:
                cleaned.set()

        async def interrupt_after_start(task):
            state["task"] = task
            await started.wait()
            raise KeyboardInterrupt(PROVIDER_DETAIL)

        def record_consumed(task):
            original_consume(task)
            consumed.set()

        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_FREEBUSY_DEADLINE_SECONDS",
            0.05,
        )
        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_post_json_async",
            suppress_repeated_cancellation,
        )
        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_await_owned_transport_task",
            interrupt_after_start,
        )
        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_consume_owned_task_result",
            record_consumed,
        )
        started_at = asyncio.get_running_loop().time()
        try:
            async with asyncio.timeout(1.0):
                await _query()
        except KeyboardInterrupt:
            state["elapsed"] = (
                asyncio.get_running_loop().time() - started_at
            )
            state["cleaned_before_release"] = cleaned.is_set()
            state["done_before_runner_shutdown"] = state["task"].done()
            release.set()
            done, _pending = await asyncio.wait(
                {state["task"]},
                timeout=0.5,
            )
            assert state["task"] in done
            await asyncio.wait_for(consumed.wait(), timeout=0.5)
            state["cleaned_before_runner_shutdown"] = cleaned.is_set()
            state["consumed_before_runner_shutdown"] = consumed.is_set()
            raise

    with pytest.raises(KeyboardInterrupt):
        _run(scenario())

    assert state["elapsed"] < 0.15
    assert state["cancellations"] >= 2
    assert state["cleaned_before_release"] is False
    assert state["cleaned_before_runner_shutdown"] is True
    assert state["consumed_before_runner_shutdown"] is True
    assert state["done_before_runner_shutdown"] is False


def test_cleanup_failure_observed_after_cutoff_cannot_replace_timeout(
    monkeypatch,
):
    real_wait = asyncio.wait

    async def scenario():
        started = asyncio.Event()

        async def late_control(**_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as cancelled:
                raise BaseExceptionGroup(
                    PROVIDER_DETAIL,
                    [KeyboardInterrupt(PROVIDER_DETAIL), cancelled],
                )

        async def transport_timeout(_task):
            await started.wait()
            raise TimeoutError(PROVIDER_DETAIL)

        async def delayed_wait(
            awaitables,
            *,
            timeout=None,
            return_when=asyncio.ALL_COMPLETED,
        ):
            assert timeout is not None
            await asyncio.sleep(0.05)
            return await real_wait(
                awaitables,
                timeout=0.0,
                return_when=return_when,
            )

        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_post_json_async",
            late_control,
        )
        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_await_owned_transport_task",
            transport_timeout,
        )
        monkeypatch.setattr(
            google_calendar_freebusy_deadline.asyncio,
            "wait",
            delayed_wait,
        )
        return await _query()

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_FREEBUSY_DEADLINE_SECONDS",
        0.02,
    )

    with pytest.raises(ExternalCalendarTimeout) as raised:
        _run(scenario())

    assert raised.value.reason_code == "timeout"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_two_phase_start_blocks_transport_when_task_ownership_is_interrupted(
    monkeypatch,
):
    state = {"network_started": False}
    original_create_task = asyncio.create_task

    async def scenario():
        async def post(**_kwargs):
            state["network_started"] = True
            return _response()

        def interrupt_after_creation(coroutine):
            task = original_create_task(coroutine)
            state["task"] = task
            raise GeneratorExit(PROVIDER_DETAIL)

        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_FREEBUSY_DEADLINE_SECONDS",
            0.01,
        )
        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_post_json_async",
            post,
        )
        monkeypatch.setattr(
            google_calendar_freebusy_deadline.asyncio,
            "create_task",
            interrupt_after_creation,
        )
        try:
            async with asyncio.timeout(1.0):
                await _query()
        except GeneratorExit:
            assert state["network_started"] is False
            await asyncio.sleep(0.03)
            assert state["task"].done()
            assert state["network_started"] is False
            state["checked_before_runner_shutdown"] = True
            raise

    with pytest.raises(GeneratorExit) as raised:
        _run(scenario())

    assert type(raised.value) is GeneratorExit
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)
    assert state["checked_before_runner_shutdown"] is True


@pytest.mark.parametrize(
    ("error_factory", "expected_type", "expected_code"),
    [
        (
            lambda: BaseExceptionGroup(
                PROVIDER_DETAIL,
                [RuntimeError(PROVIDER_DETAIL), KeyboardInterrupt(PROVIDER_DETAIL)],
            ),
            KeyboardInterrupt,
            None,
        ),
        (
            lambda: BaseExceptionGroup(
                PROVIDER_DETAIL,
                [SystemExit(PROVIDER_DETAIL)],
            ),
            SystemExit,
            1,
        ),
    ],
)
def test_grouped_controls_are_sanitized(
    monkeypatch,
    error_factory,
    expected_type,
    expected_code,
):
    async def fail(**_kwargs):
        raise error_factory()

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        fail,
    )

    with pytest.raises(expected_type) as raised:
        _run(_query())

    assert type(raised.value) is expected_type
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    if expected_type is SystemExit:
        assert raised.value.code == expected_code
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_subclass_hidden_deep_grouped_cancellation_is_sanitized(monkeypatch):
    class HidingGroup(BaseExceptionGroup):
        @property
        def exceptions(self):
            return (RuntimeError(PROVIDER_DETAIL),)

    grouped: BaseException = asyncio.CancelledError(PROVIDER_DETAIL)
    for _ in range(1_500):
        grouped = BaseExceptionGroup(PROVIDER_DETAIL, [grouped])
    grouped = HidingGroup(PROVIDER_DETAIL, [grouped])

    async def fail(**_kwargs):
        raise grouped

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        fail,
    )

    with pytest.raises(asyncio.CancelledError) as raised:
        _run(_query())

    assert type(raised.value) is asyncio.CancelledError
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_exception_graph_traversal_deduplicates_shared_nodes():
    shared: BaseException = RuntimeError(PROVIDER_DETAIL)
    for _ in range(20):
        shared = BaseExceptionGroup(PROVIDER_DETAIL, [shared, shared])

    nodes = tuple(
        itertools.islice(
            google_calendar_freebusy._iter_unique_exception_graph(shared),
            100,
        )
    )

    assert len(nodes) == 21
    assert len({id(node) for node in nodes}) == 21


def test_exception_graph_traversal_has_a_unique_node_limit():
    nested: BaseException = RuntimeError(PROVIDER_DETAIL)
    for _ in range(
        google_calendar_freebusy._MAX_CONTROL_EXCEPTION_NODES + 10
    ):
        nested = BaseExceptionGroup(PROVIDER_DETAIL, [nested])

    nodes = tuple(
        itertools.islice(
            google_calendar_freebusy._iter_unique_exception_graph(nested),
            google_calendar_freebusy._MAX_CONTROL_EXCEPTION_NODES + 1,
        )
    )

    assert len(nodes) == 4_096


def test_exception_graph_overflow_conservatively_cancels(monkeypatch):
    nested: BaseException = KeyboardInterrupt(PROVIDER_DETAIL)
    for _ in range(google_calendar_freebusy._MAX_CONTROL_EXCEPTION_NODES):
        nested = BaseExceptionGroup(PROVIDER_DETAIL, [nested])

    async def fail(**_kwargs):
        raise nested

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        fail,
    )

    with pytest.raises(asyncio.CancelledError) as raised:
        _run(_query())

    assert type(raised.value) is asyncio.CancelledError
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_failure_traceback_and_locals_do_not_retain_secrets(monkeypatch, caplog):
    async def fail(**_kwargs):
        raise RuntimeError(PROVIDER_DETAIL)

    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        fail,
    )

    with pytest.raises(ExternalCalendarUnavailable) as raised:
        _run(_query())

    output = " ".join(
        (
            str(raised.value),
            repr(raised.value),
            _formatted(raised.value),
            _module_traceback_values(raised.value),
            caplog.text,
        )
    )
    for private in (
        PROVIDER_DETAIL,
        ACCESS_TOKEN,
        REFRESH_TOKEN,
        ACCOUNT_ID,
        INSTRUCTOR_ID,
        CONNECTION_ID,
    ):
        assert private not in output


def test_forced_concurrent_queries_remain_tenant_isolated(monkeypatch):
    calls = []

    async def scenario():
        both_started = asyncio.Event()
        release_first = asyncio.Event()

        async def post(**kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                both_started.set()
            await both_started.wait()
            request = json.loads(kwargs["body"])
            start = datetime.datetime.fromisoformat(
                request["timeMin"].replace("Z", "+00:00")
            )
            end = datetime.datetime.fromisoformat(
                request["timeMax"].replace("Z", "+00:00")
            )
            if kwargs["access_token"].endswith("-a"):
                await release_first.wait()
            else:
                release_first.set()
            busy_start = start + datetime.timedelta(minutes=30)
            busy_end = busy_start + datetime.timedelta(minutes=30)
            return _response(
                payload=_payload(
                    start=start,
                    end=end,
                    busy=[
                        {
                            "start": busy_start.isoformat().replace(
                                "+00:00", "Z"
                            ),
                            "end": busy_end.isoformat().replace(
                                "+00:00", "Z"
                            ),
                        }
                    ],
                )
            )

        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_post_json_async",
            post,
        )
        cases = []
        for index, suffix in enumerate(("a", "b"), start=1):
            start = WINDOW_START + datetime.timedelta(days=index)
            end = start + datetime.timedelta(hours=6)
            cases.append(
                _query(
                    claim=_claim(
                        account_id=f"acct_concurrent_{suffix}",
                        instructor_id=f"inst_concurrent_{suffix}",
                        connection_id="calconn_" + suffix * 32,
                        revision=index,
                        refresh_token=f"refresh-concurrent-{suffix}",
                    ),
                    access_result=_access_result(
                        account_id=f"acct_concurrent_{suffix}",
                        instructor_id=f"inst_concurrent_{suffix}",
                        connection_id="calconn_" + suffix * 32,
                        revision=index,
                        access_token=f"access-concurrent-{suffix}",
                    ),
                    window_start=start,
                    window_end=end,
                )
            )
        return await asyncio.gather(*cases)

    results = _run(scenario())

    assert len(calls) == 2
    assert {call["access_token"] for call in calls} == {
        "access-concurrent-a",
        "access-concurrent-b",
    }
    for result, suffix in zip(results, ("a", "b"), strict=True):
        assert result.account_id == f"acct_concurrent_{suffix}"
        assert len(result.periods) == 1
        assert not hasattr(result, "access_token")


def test_concurrent_timeout_does_not_cancel_or_contaminate_peer(monkeypatch):
    calls = {}

    async def scenario():
        both_started = asyncio.Event()

        async def post(**kwargs):
            request = json.loads(kwargs["body"])
            calls[kwargs["access_token"]] = request
            if len(calls) == 2:
                both_started.set()
            await both_started.wait()
            if kwargs["access_token"].endswith("-a"):
                await asyncio.Event().wait()
            start = datetime.datetime.fromisoformat(
                request["timeMin"].replace("Z", "+00:00")
            )
            end = datetime.datetime.fromisoformat(
                request["timeMax"].replace("Z", "+00:00")
            )
            busy_start = start + datetime.timedelta(minutes=45)
            busy_end = busy_start + datetime.timedelta(minutes=30)
            return _response(
                payload=_payload(
                    start=start,
                    end=end,
                    busy=[
                        {
                            "start": busy_start.isoformat().replace(
                                "+00:00", "Z"
                            ),
                            "end": busy_end.isoformat().replace(
                                "+00:00", "Z"
                            ),
                        }
                    ],
                )
            )

        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_post_json_async",
            post,
        )
        monkeypatch.setattr(
            google_calendar_freebusy_deadline,
            "_FREEBUSY_DEADLINE_SECONDS",
            0.02,
        )
        cases = []
        expected = []
        for index, suffix in enumerate(("a", "b"), start=1):
            start = WINDOW_START + datetime.timedelta(days=index)
            end = start + datetime.timedelta(hours=6)
            expected.append((start, end, index, suffix))
            cases.append(
                _query(
                    claim=_claim(
                        account_id=f"acct_timeout_{suffix}",
                        instructor_id=f"inst_timeout_{suffix}",
                        connection_id="calconn_" + suffix * 32,
                        revision=index,
                        refresh_token=f"refresh-timeout-{suffix}",
                    ),
                    access_result=_access_result(
                        account_id=f"acct_timeout_{suffix}",
                        instructor_id=f"inst_timeout_{suffix}",
                        connection_id="calconn_" + suffix * 32,
                        revision=index,
                        access_token=f"access-timeout-{suffix}",
                    ),
                    window_start=start,
                    window_end=end,
                )
            )
        results = await asyncio.gather(*cases, return_exceptions=True)
        return results, expected

    results, expected = _run(asyncio.wait_for(scenario(), timeout=1.0))

    first, second = results
    assert type(first) is ExternalCalendarTimeout
    assert first.reason_code == "timeout"
    assert type(second) is GoogleCalendarFreeBusyQueryResult
    start, end, revision, suffix = expected[1]
    assert second.account_id == f"acct_timeout_{suffix}"
    assert second.instructor_id == f"inst_timeout_{suffix}"
    assert second.connection_id == "calconn_" + suffix * 32
    assert second.expected_calendar_connection_revision == revision
    assert second.window_start == start
    assert second.window_end == end
    assert second.periods == (
        CalendarBusyInterval(
            start=start + datetime.timedelta(minutes=45),
            end=start + datetime.timedelta(minutes=75),
        ),
    )
    assert calls == {
        "access-timeout-a": {
            "timeMin": "2026-11-20T10:00:00Z",
            "timeMax": "2026-11-20T16:00:00Z",
            "timeZone": "UTC",
            "calendarExpansionMax": 1,
            "items": [{"id": "primary"}],
        },
        "access-timeout-b": {
            "timeMin": "2026-11-21T10:00:00Z",
            "timeMax": "2026-11-21T16:00:00Z",
            "timeZone": "UTC",
            "calendarExpansionMax": 1,
            "items": [{"id": "primary"}],
        },
    }
