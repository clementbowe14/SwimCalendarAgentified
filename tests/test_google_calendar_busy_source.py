import asyncio
import copy
import datetime
import inspect
import pickle
import sys
import threading
import time
from collections.abc import Coroutine

import pytest

from app import google_calendar_busy_source
from app.calendar_credentials import CalendarCredentialCipher
from app.external_calendar import (
    CalendarBusyInterval,
    CalendarBusySnapshot,
    CalendarBusySource,
    ExternalCalendarAuthorizationRequired,
    ExternalCalendarInvalidResponse,
    ExternalCalendarTimeout,
    ExternalCalendarUnavailable,
)
from app.google_calendar_busy_source import GoogleCalendarBusySource
from app.google_calendar_oauth import (
    GOOGLE_CALENDAR_CALLBACK_PATH,
    GoogleCalendarOAuthConfiguration,
)
from app.google_calendar_token_exchange import (
    GoogleCalendarTokenExchangeConfiguration,
)


UTC = datetime.timezone.utc
WINDOW_START = datetime.datetime(2026, 11, 20, 14, tzinfo=UTC)
WINDOW_END = WINDOW_START + datetime.timedelta(hours=8)
AUTHORIZATION_CONFIGURATION = GoogleCalendarOAuthConfiguration(
    client_id="123456789-dayweave.apps.googleusercontent.com",
    redirect_uri=(
        "https://api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH
    ),
)
TOKEN_CONFIGURATION = GoogleCalendarTokenExchangeConfiguration(
    authorization_configuration=AUTHORIZATION_CONFIGURATION,
    client_secret="client-secret-private",
)
CIPHER = CalendarCredentialCipher(
    active_key_id="primary",
    keys={"primary": b"k" * 32},
)


def _session_factory():
    raise AssertionError("database should be mocked at the service boundary")


def _source() -> GoogleCalendarBusySource:
    return GoogleCalendarBusySource(
        _session_factory,
        configuration=TOKEN_CONFIGURATION,
        credential_cipher=CIPHER,
    )


def _module_frame_locals(error):
    traceback = error.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_filename.endswith(
            "app/google_calendar_busy_source.py"
        ):
            yield traceback.tb_frame.f_locals
        traceback = traceback.tb_next


def test_source_satisfies_provider_neutral_runtime_contract():
    assert isinstance(_source(), CalendarBusySource)


@pytest.mark.parametrize(
    "snapshot",
    [
        CalendarBusySnapshot(status="disconnected", periods=()),
        CalendarBusySnapshot(status="verified", periods=()),
        CalendarBusySnapshot(
            status="verified",
            periods=(
                CalendarBusyInterval(
                    start=WINDOW_START,
                    end=WINDOW_START + datetime.timedelta(hours=1),
                ),
            ),
        ),
    ],
)
def test_source_runs_async_service_and_forwards_exact_inputs(monkeypatch, snapshot):
    calls = []

    async def query(session_factory, **kwargs):
        calls.append((session_factory, kwargs, threading.get_ident()))
        return snapshot

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    result = _source().query_busy_periods(
        instructor_id="inst_001",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert result == snapshot
    assert result is not snapshot
    assert calls == [
        (
            _session_factory,
            {
                "instructor_id": "inst_001",
                "window_start": WINDOW_START,
                "window_end": WINDOW_END,
                "configuration": TOKEN_CONFIGURATION,
                "credential_cipher": CIPHER,
            },
            threading.get_ident(),
        )
    ]


def test_source_fails_closed_inside_running_event_loop(monkeypatch):
    calls = []

    async def forbidden_query(*_args, **_kwargs):
        calls.append(True)

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        forbidden_query,
    )

    async def scenario():
        with pytest.raises(ExternalCalendarUnavailable) as captured:
            _source().query_busy_periods(
                instructor_id="inst_001",
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )
        assert captured.value.reason_code == "provider_unavailable"
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None

    asyncio.run(scenario())
    assert calls == []


@pytest.mark.parametrize(
    ("raised", "expected_type", "reason"),
    [
        (
            ExternalCalendarAuthorizationRequired(),
            ExternalCalendarAuthorizationRequired,
            "authorization_required",
        ),
        (ExternalCalendarTimeout(), ExternalCalendarTimeout, "timeout"),
        (
            ExternalCalendarInvalidResponse(),
            ExternalCalendarInvalidResponse,
            "invalid_response",
        ),
        (
            ExternalCalendarUnavailable("provider_unavailable"),
            ExternalCalendarUnavailable,
            "provider_unavailable",
        ),
        (RuntimeError("private-provider-detail"), ExternalCalendarUnavailable, "provider_unavailable"),
        (asyncio.CancelledError("private-cancel-detail"), ExternalCalendarUnavailable, "provider_unavailable"),
    ],
)
def test_source_canonicalizes_failures_without_private_chains(
    monkeypatch,
    raised,
    expected_type,
    reason,
):
    async def query(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    with pytest.raises(expected_type) as captured:
        _source().query_busy_periods(
            instructor_id="inst_private",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
    assert captured.value.reason_code == reason
    assert str(captured.value) == (
        "External calendar availability could not be verified."
    )
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "private" not in repr(captured.value)


@pytest.mark.parametrize(
    ("raised", "expected_type", "expected_code"),
    [
        (KeyboardInterrupt("private"), KeyboardInterrupt, None),
        (SystemExit(23), SystemExit, 23),
        (GeneratorExit("private"), GeneratorExit, None),
    ],
)
def test_source_reemits_sanitized_controls(
    monkeypatch,
    raised,
    expected_type,
    expected_code,
):
    async def query(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    with pytest.raises(expected_type) as captured:
        _source().query_busy_periods(
            instructor_id="inst_private",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
    if expected_type is SystemExit:
        assert captured.value.code == expected_code
    else:
        assert str(captured.value) == ""
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_source_rejects_malformed_service_result(monkeypatch):
    async def query(*_args, **_kwargs):
        return {"status": "verified", "periods": []}

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    with pytest.raises(ExternalCalendarInvalidResponse):
        _source().query_busy_periods(
            instructor_id="inst_001",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )


@pytest.mark.parametrize(
    "snapshot",
    [
        CalendarBusySnapshot(status="forged", periods=()),
        CalendarBusySnapshot(status="verified", periods=[]),
        CalendarBusySnapshot(
            status="disconnected",
            periods=(
                CalendarBusyInterval(
                    start=WINDOW_START,
                    end=WINDOW_START + datetime.timedelta(minutes=30),
                ),
            ),
        ),
        CalendarBusySnapshot(status="verified", periods=("private-event",)),
        CalendarBusySnapshot(
            status="verified",
            periods=(
                CalendarBusyInterval(
                    start=WINDOW_START,
                    end=WINDOW_START,
                ),
            ),
        ),
    ],
)
def test_source_rejects_forged_exact_snapshots(monkeypatch, snapshot):
    async def query(*_args, **_kwargs):
        return snapshot

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    with pytest.raises(ExternalCalendarInvalidResponse) as captured:
        _source().query_busy_periods(
            instructor_id="inst_001",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_source_rejects_datetime_subclasses_in_busy_intervals(monkeypatch):
    class PrivateDateTime(datetime.datetime):
        provider_metadata = "private-datetime-metadata"

        def astimezone(self, _timezone=None):
            return self

    snapshot = CalendarBusySnapshot(
        status="verified",
        periods=(
            CalendarBusyInterval(
                start=PrivateDateTime(
                    2026,
                    11,
                    20,
                    14,
                    tzinfo=UTC,
                ),
                end=PrivateDateTime(
                    2026,
                    11,
                    20,
                    15,
                    tzinfo=UTC,
                ),
            ),
        ),
    )

    async def query(*_args, **_kwargs):
        return snapshot

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    with pytest.raises(ExternalCalendarInvalidResponse) as captured:
        _source().query_busy_periods(
            instructor_id="inst_private",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    for frame_locals in _module_frame_locals(captured.value):
        assert "private-datetime-metadata" not in repr(frame_locals)


@pytest.mark.parametrize(
    ("window_start", "window_end"),
    [
        (object(), WINDOW_END),
        (WINDOW_START, object()),
        (WINDOW_END, WINDOW_START),
        (datetime.datetime(2026, 11, 20, 14), WINDOW_END),
    ],
)
def test_disconnected_snapshot_still_requires_a_valid_window(
    monkeypatch,
    window_start,
    window_end,
):
    async def query(*_args, **_kwargs):
        return CalendarBusySnapshot(status="disconnected", periods=())

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    with pytest.raises(ExternalCalendarInvalidResponse) as captured:
        _source().query_busy_periods(
            instructor_id="inst_private",
            window_start=window_start,
            window_end=window_end,
        )
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_source_clips_and_copies_verified_periods(monkeypatch):
    snapshot = CalendarBusySnapshot(
        status="verified",
        periods=(
            CalendarBusyInterval(
                start=WINDOW_START - datetime.timedelta(hours=1),
                end=WINDOW_START + datetime.timedelta(minutes=30),
            ),
            CalendarBusyInterval(
                start=WINDOW_START + datetime.timedelta(minutes=20),
                end=WINDOW_START + datetime.timedelta(hours=1),
            ),
        ),
    )

    async def query(*_args, **_kwargs):
        return snapshot

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    result = _source().query_busy_periods(
        instructor_id="inst_001",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert result == CalendarBusySnapshot(
        status="verified",
        periods=(
            CalendarBusyInterval(
                start=WINDOW_START,
                end=WINDOW_START + datetime.timedelta(hours=1),
            ),
        ),
    )
    assert result is not snapshot


@pytest.mark.parametrize(
    ("nested", "expected_type", "expected_code"),
    [
        (KeyboardInterrupt("private"), KeyboardInterrupt, None),
        (SystemExit(37), SystemExit, 37),
        (GeneratorExit("private"), GeneratorExit, None),
    ],
)
def test_source_preserves_sanitized_controls_nested_in_groups(
    monkeypatch,
    nested,
    expected_type,
    expected_code,
):
    raised = BaseExceptionGroup(
        "private-group",
        [RuntimeError("private-detail"), nested],
    )

    async def query(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    with pytest.raises(expected_type) as captured:
        _source().query_busy_periods(
            instructor_id="inst_private",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
    if expected_type is SystemExit:
        assert captured.value.code == expected_code
    else:
        assert str(captured.value) == ""
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_source_fails_closed_for_nested_cancellation(monkeypatch):
    async def query(*_args, **_kwargs):
        raise BaseExceptionGroup(
            "private-group",
            [asyncio.CancelledError("private-detail")],
        )

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    with pytest.raises(ExternalCalendarUnavailable) as captured:
        _source().query_busy_periods(
            instructor_id="inst_private",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
    assert captured.value.reason_code == "provider_unavailable"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize("hostile_reason", [[], {}, object()])
def test_source_fails_closed_for_mutated_failure_reason(
    monkeypatch,
    hostile_reason,
):
    raised = ExternalCalendarUnavailable("provider_unavailable")
    raised.reason_code = hostile_reason

    async def query(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    with pytest.raises(ExternalCalendarUnavailable) as captured:
        _source().query_busy_periods(
            instructor_id="inst_private",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
    assert type(captured.value) is ExternalCalendarUnavailable
    assert captured.value.reason_code == "provider_unavailable"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_source_does_not_trust_failure_subclasses(monkeypatch):
    class HostileUnavailable(ExternalCalendarUnavailable):
        def __getattribute__(self, name):
            if name == "reason_code":
                raise RuntimeError("private-classifier-detail")
            return super().__getattribute__(name)

    raised = HostileUnavailable("provider_unavailable")

    async def query(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    with pytest.raises(ExternalCalendarUnavailable) as captured:
        _source().query_busy_periods(
            instructor_id="inst_private",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
    assert type(captured.value) is ExternalCalendarUnavailable
    assert captured.value.reason_code == "provider_unavailable"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_runner_rejection_does_not_allocate_the_service_coroutine(monkeypatch):
    created_drivers = []
    service_calls = []

    async def query(*_args, **_kwargs):
        service_calls.append(True)
        return CalendarBusySnapshot(status="verified", periods=())

    def reject_run(_runner, driver):
        created_drivers.append(driver)
        raise RuntimeError("private-runner-detail")

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )
    monkeypatch.setattr(asyncio.Runner, "run", reject_run)

    with pytest.raises(ExternalCalendarUnavailable) as captured:
        _source().query_busy_periods(
            instructor_id="inst_private",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )

    assert service_calls == []
    assert len(created_drivers) == 1
    assert created_drivers[0].cr_frame is None
    assert captured.value.reason_code == "provider_unavailable"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_interrupted_driver_close_retains_only_a_redacted_handoff(monkeypatch):
    created_drivers = []
    service_calls = []

    async def query(*_args, **_kwargs):
        service_calls.append(True)
        return CalendarBusySnapshot(status="verified", periods=())

    def reject_run(_runner, driver):
        created_drivers.append(driver)
        raise RuntimeError("private-runner-detail")

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )
    monkeypatch.setattr(asyncio.Runner, "run", reject_run)
    guarded = google_calendar_busy_source._run_service_in_owned_runner
    source_lines, first_line = inspect.getsourcelines(guarded)
    target_line = first_line + next(
        offset
        for offset, line in enumerate(source_lines)
        if "types.CoroutineType.close(driver)" in line
    )
    interrupted = False

    def interrupt_close(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is guarded.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt("driver-close-control-private")
        return interrupt_close

    try:
        sys.settrace(interrupt_close)
        with pytest.raises(KeyboardInterrupt) as captured:
            _source().query_busy_periods(
                instructor_id="inst_private",
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )
    finally:
        sys.settrace(None)

    assert interrupted is True
    assert service_calls == []
    assert len(created_drivers) == 1
    driver = created_drivers[0]
    assert driver.cr_frame is not None
    assert driver.cr_frame.f_locals == {"handoff": []}
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True
    for frame_locals in _module_frame_locals(captured.value):
        assert TOKEN_CONFIGURATION not in frame_locals.values()
        assert CIPHER not in frame_locals.values()
        assert "driver-close-control-private" not in repr(frame_locals)
        assert "private-runner-detail" not in repr(frame_locals)
    driver.close()


def test_service_coroutine_is_created_inside_the_owned_runner(monkeypatch):
    running_loops = []

    async def query(*_args, **_kwargs):
        running_loops.append(asyncio.get_running_loop())
        return CalendarBusySnapshot(status="verified", periods=())

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    result = _source().query_busy_periods(
        instructor_id="inst_private",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert result == CalendarBusySnapshot(status="verified", periods=())
    assert len(running_loops) == 1
    assert running_loops[0].is_closed() is True


def test_source_closes_coroutine_returned_as_malformed_result(monkeypatch):
    created = []

    async def malformed_result():
        return "private-result"

    async def query(*_args, **_kwargs):
        result = malformed_result()
        created.append(result)
        return result

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    with pytest.raises(ExternalCalendarInvalidResponse):
        _source().query_busy_periods(
            instructor_id="inst_private",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
    assert len(created) == 1
    assert created[0].cr_frame is None


def test_source_closes_non_native_coroutine_returned_as_malformed_result(
    monkeypatch,
):
    class NonNativeCoroutine(Coroutine):
        def __init__(self):
            self.closed = False

        def __await__(self):
            return self

        def send(self, _value):
            raise StopIteration(None)

        def throw(self, raised, *_args):
            raise raised

        def close(self):
            self.closed = True

    result = NonNativeCoroutine()

    async def query(*_args, **_kwargs):
        return result

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    with pytest.raises(ExternalCalendarInvalidResponse):
        _source().query_busy_periods(
            instructor_id="inst_private",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
    assert result.closed is True


def test_malformed_coroutine_reemits_control_after_confirmed_close(monkeypatch):
    class InterruptingCoroutine(Coroutine):
        def __init__(self):
            self.close_calls = 0
            self.closed = False

        def __await__(self):
            return self

        def send(self, _value):
            raise StopIteration(None)

        def throw(self, raised, *_args):
            raise raised

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise KeyboardInterrupt("private-close-control")
            self.closed = True

    result = InterruptingCoroutine()

    async def query(*_args, **_kwargs):
        return result

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    with pytest.raises(KeyboardInterrupt) as captured:
        _source().query_busy_periods(
            instructor_id="inst_private",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )

    assert result.close_calls == 2
    assert result.closed is True
    assert str(captured.value) == ""
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True


def test_interrupted_caller_lease_return_keeps_unique_pool_capacity(
    monkeypatch,
):
    async def malformed_result():
        return "private-result"

    async def query(*_args, **_kwargs):
        return malformed_result()

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )
    original_return = (
        google_calendar_busy_source
        ._return_malformed_coroutine_cleanup_lease
    )
    return_calls = 0

    def publish_then_interrupt(lease):
        nonlocal return_calls
        original_return(lease)
        return_calls += 1
        if return_calls == 1:
            raise KeyboardInterrupt("lease-return-control-private")

    monkeypatch.setattr(
        google_calendar_busy_source,
        "_return_malformed_coroutine_cleanup_lease",
        publish_then_interrupt,
    )

    with pytest.raises(KeyboardInterrupt) as captured:
        _source().query_busy_periods(
            instructor_id="inst_private",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )

    assert return_calls == 1
    assert str(captured.value) == ""
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True
    acquired_leases = []
    while True:
        lease = (
            google_calendar_busy_source
            ._acquire_malformed_coroutine_cleanup_lease()
        )
        if lease is None:
            break
        acquired_leases.append(lease)
    try:
        assert len(acquired_leases) == (
            google_calendar_busy_source._MALFORMED_COROUTINE_CLEANUP_CAPACITY
        )
        assert len({id(lease) for lease in acquired_leases}) == len(
            acquired_leases
        )
    finally:
        for lease in acquired_leases:
            lease.release_caller()


def test_completed_worker_lease_cannot_be_reborrowed_before_caller_release():
    leases = []
    while True:
        lease = (
            google_calendar_busy_source
            ._acquire_malformed_coroutine_cleanup_lease()
        )
        if lease is None:
            break
        leases.append(lease)
    assert len(leases) == (
        google_calendar_busy_source._MALFORMED_COROUTINE_CLEANUP_CAPACITY
    )
    victim = leases.pop()
    candidate = object()
    reacquired = None
    try:
        assert victim.quarantine(candidate) is True
        assert victim.claim_worker() is candidate
        assert victim.release_worker() is True
        assert victim._state == "worker_done"
        assert victim.quarantine(candidate) is False
        assert (
            google_calendar_busy_source
            ._acquire_malformed_coroutine_cleanup_lease()
        ) is None

        victim.release_caller()
        reacquired = (
            google_calendar_busy_source
            ._acquire_malformed_coroutine_cleanup_lease()
        )
        assert reacquired is victim
        victim = None
    finally:
        if reacquired is not None:
            reacquired.release_caller()
        if victim is not None:
            victim.release_caller()
        for lease in leases:
            lease.release_caller()


def test_worker_entry_interruption_is_sanitized_without_excepthook(
    monkeypatch,
):
    created = []

    async def malformed_result():
        return "private-result"

    async def query(*_args, **_kwargs):
        result = malformed_result()
        created.append(result)
        return result

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )
    target = google_calendar_busy_source._close_malformed_coroutine_worker
    source_lines, source_start = inspect.getsourcelines(target)
    entry_line = source_start + next(
        index
        for index, source_line in enumerate(source_lines)
        if source_line.lstrip().startswith("try: return")
    )
    active_before = set(
        google_calendar_busy_source._ACTIVE_MALFORMED_COROUTINE_LEASES
    )
    interrupted = False
    uncaught_thread_failures = []
    previous_trace = threading.gettrace()

    def interrupt_worker_entry(frame, event, _arg):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is target.__code__
            and frame.f_lineno == entry_line
        ):
            interrupted = True
            raise KeyboardInterrupt("worker-entry-control-private")
        return interrupt_worker_entry

    def capture_thread_failure(args):
        uncaught_thread_failures.append(args)

    monkeypatch.setattr(threading, "excepthook", capture_thread_failure)
    threading.settrace(interrupt_worker_entry)
    try:
        with pytest.raises(KeyboardInterrupt) as captured:
            _source().query_busy_periods(
                instructor_id="inst_private",
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )
    finally:
        threading.settrace(previous_trace)

    assert interrupted is True
    assert uncaught_thread_failures == []
    assert str(captured.value) == ""
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True
    active_after = set(
        google_calendar_busy_source._ACTIVE_MALFORMED_COROUTINE_LEASES
    )
    new_leases = active_after - active_before
    assert len(new_leases) == 1
    lease = google_calendar_busy_source._ACTIVE_MALFORMED_COROUTINE_LEASES[
        new_leases.pop()
    ]
    retained_result = lease.claim_worker()
    assert retained_result is created[0]
    retained_result.close()
    assert lease.release_worker() is True


def test_worker_retries_interrupted_outcome_publication_without_excepthook(
    monkeypatch,
):
    async def malformed_result():
        return "private-result"

    async def query(*_args, **_kwargs):
        return malformed_result()

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )
    target = (
        google_calendar_busy_source
        ._publish_malformed_coroutine_cleanup_outcome
    )
    source_lines, source_start = inspect.getsourcelines(target)
    publication_line = source_start + next(
        index
        for index, source_line in enumerate(source_lines)
        if "outcome[0] =" in source_line
    )
    interrupted = False
    uncaught_thread_failures = []
    previous_trace = threading.gettrace()

    def interrupt_first_publication(frame, event, _arg):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is target.__code__
            and frame.f_lineno == publication_line
        ):
            interrupted = True
            raise KeyboardInterrupt("outcome-publication-control-private")
        return interrupt_first_publication

    def capture_thread_failure(args):
        uncaught_thread_failures.append(args)

    monkeypatch.setattr(threading, "excepthook", capture_thread_failure)
    threading.settrace(interrupt_first_publication)
    try:
        with pytest.raises(KeyboardInterrupt) as captured:
            _source().query_busy_periods(
                instructor_id="inst_private",
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )
    finally:
        threading.settrace(previous_trace)

    assert interrupted is True
    assert uncaught_thread_failures == []
    assert str(captured.value) == ""
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True
    acquired_leases = []
    while True:
        lease = (
            google_calendar_busy_source
            ._acquire_malformed_coroutine_cleanup_lease()
        )
        if lease is None:
            break
        acquired_leases.append(lease)
    try:
        assert len(acquired_leases) == (
            google_calendar_busy_source._MALFORMED_COROUTINE_CLEANUP_CAPACITY
        )
        assert len({id(lease) for lease in acquired_leases}) == len(
            acquired_leases
        )
    finally:
        for lease in acquired_leases:
            lease.release_caller()


def test_malformed_coroutine_cannot_hold_cleanup_open_forever(monkeypatch):
    class Pause:
        def __await__(self):
            yield None
            return None

    ignore_generator_exit = True

    async def stubborn_result():
        nonlocal ignore_generator_exit
        while True:
            try:
                await Pause()
            except GeneratorExit:
                if ignore_generator_exit:
                    continue
                raise

    async def query(*_args, **_kwargs):
        result = stubborn_result()
        result.send(None)
        return result

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    active_before = set(
        google_calendar_busy_source._ACTIVE_MALFORMED_COROUTINE_LEASES
    )
    started = time.monotonic()
    try:
        with pytest.raises(ExternalCalendarUnavailable) as captured:
            _source().query_busy_periods(
                instructor_id="inst_private",
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )
    finally:
        ignore_generator_exit = False
        active_after = set(
            google_calendar_busy_source._ACTIVE_MALFORMED_COROUTINE_LEASES
        )
        new_leases = active_after - active_before
        assert len(new_leases) == 1
        lease = google_calendar_busy_source._ACTIVE_MALFORMED_COROUTINE_LEASES[
            new_leases.pop()
        ]
        retained_result = lease._candidate
        retained_result.close()
        lease.release_worker()

    assert time.monotonic() - started < 2
    assert type(captured.value) is ExternalCalendarUnavailable
    assert captured.value.reason_code == "provider_unavailable"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_malformed_coroutine_close_call_is_time_bounded(monkeypatch):
    class Pause:
        def __await__(self):
            yield None
            return None

    release_close = threading.Event()
    coroutine_finished = threading.Event()

    async def blocking_close_result():
        try:
            await Pause()
        except GeneratorExit:
            release_close.wait()
        finally:
            coroutine_finished.set()

    result = blocking_close_result()
    result.send(None)

    async def query(*_args, **_kwargs):
        return result

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    started = time.monotonic()
    try:
        with pytest.raises(ExternalCalendarUnavailable) as captured:
            _source().query_busy_periods(
                instructor_id="inst_private",
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )
    finally:
        release_close.set()

    assert time.monotonic() - started < 1
    assert type(captured.value) is ExternalCalendarUnavailable
    assert captured.value.reason_code == "provider_unavailable"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert coroutine_finished.wait(1) is True


def test_malformed_coroutine_cleanup_has_global_bounded_admission(monkeypatch):
    class Pause:
        def __await__(self):
            yield None
            return None

    releases = []
    finished = []
    provider_calls = 0

    async def query(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        release_close = threading.Event()
        coroutine_finished = threading.Event()

        async def blocking_result(
            release=release_close,
            completed=coroutine_finished,
        ):
            try:
                await Pause()
            except GeneratorExit:
                release.wait()
            finally:
                completed.set()

        result = blocking_result()
        result.send(None)
        releases.append(release_close)
        finished.append(coroutine_finished)
        return result

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    try:
        for _request in range(
            google_calendar_busy_source._MALFORMED_COROUTINE_CLEANUP_CAPACITY
            + 1
        ):
            with pytest.raises(ExternalCalendarUnavailable) as captured:
                _source().query_busy_periods(
                    instructor_id="inst_private",
                    window_start=WINDOW_START,
                    window_end=WINDOW_END,
                )
            assert captured.value.reason_code == "provider_unavailable"
        live_cleanup_workers = [
            thread
            for thread in threading.enumerate()
            if thread.name == "calendar-malformed-coroutine-cleanup"
        ]
        assert len(live_cleanup_workers) == (
            google_calendar_busy_source._MALFORMED_COROUTINE_CLEANUP_CAPACITY
        )
        assert provider_calls == (
            google_calendar_busy_source._MALFORMED_COROUTINE_CLEANUP_CAPACITY
        )
    finally:
        for release in releases:
            release.set()

    assert all(completed.wait(1) for completed in finished)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and any(
        thread.name == "calendar-malformed-coroutine-cleanup"
        for thread in threading.enumerate()
    ):
        time.sleep(0.01)
    assert all(
        thread.name != "calendar-malformed-coroutine-cleanup"
        for thread in threading.enumerate()
    )


def test_cleanup_permit_has_one_owner_when_start_launches_then_raises(
    monkeypatch,
):
    class Pause:
        def __await__(self):
            yield None
            return None

    releases = []
    finished = []
    all_results = []
    for _index in range(
        google_calendar_busy_source._MALFORMED_COROUTINE_CLEANUP_CAPACITY + 1
    ):
        release_close = threading.Event()
        coroutine_finished = threading.Event()

        async def blocking_result(
            release=release_close,
            completed=coroutine_finished,
        ):
            try:
                await Pause()
            except GeneratorExit:
                release.wait()
            finally:
                completed.set()

        result = blocking_result()
        result.send(None)
        releases.append(release_close)
        finished.append(coroutine_finished)
        all_results.append(result)
    pending_results = list(all_results)

    async def query(*_args, **_kwargs):
        return pending_results.pop(0)

    original_start = threading.Thread.start
    start_calls = 0

    def start_then_raise(worker):
        nonlocal start_calls
        original_start(worker)
        start_calls += 1
        raise RuntimeError("private-post-start-detail")

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )
    monkeypatch.setattr(threading.Thread, "start", start_then_raise)

    try:
        for _result in all_results:
            with pytest.raises(ExternalCalendarUnavailable) as captured:
                _source().query_busy_periods(
                    instructor_id="inst_private",
                    window_start=WINDOW_START,
                    window_end=WINDOW_END,
                )
            assert captured.value.reason_code == "provider_unavailable"
        assert start_calls == (
            google_calendar_busy_source._MALFORMED_COROUTINE_CLEANUP_CAPACITY
        )
        assert sum(
            thread.name == "calendar-malformed-coroutine-cleanup"
            for thread in threading.enumerate()
        ) == google_calendar_busy_source._MALFORMED_COROUTINE_CLEANUP_CAPACITY
    finally:
        for release in releases:
            release.set()
        all_results[-1].close()

    assert all(completed.wait(1) for completed in finished)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and any(
        thread.name == "calendar-malformed-coroutine-cleanup"
        for thread in threading.enumerate()
    ):
        time.sleep(0.01)
    acquired_leases = []
    while True:
        lease = (
            google_calendar_busy_source
            ._acquire_malformed_coroutine_cleanup_lease()
        )
        if lease is None:
            break
        acquired_leases.append(lease)
    try:
        assert len(acquired_leases) == (
            google_calendar_busy_source._MALFORMED_COROUTINE_CLEANUP_CAPACITY
        )
    finally:
        for lease in acquired_leases:
            lease.release_caller()


def test_rejection_tracebacks_do_not_retain_secret_boundary_values(monkeypatch):
    marker = "provider-secret-marker"
    malformed = {"access_token": marker}

    async def query(*_args, **_kwargs):
        return malformed

    monkeypatch.setattr(
        google_calendar_busy_source,
        "query_google_calendar_busy_snapshot",
        query,
    )

    with pytest.raises(ExternalCalendarInvalidResponse) as captured:
        _source().query_busy_periods(
            instructor_id="inst_private",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )

    traceback = captured.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_filename.endswith(
            "app/google_calendar_busy_source.py"
        ):
            assert marker not in repr(traceback.tb_frame.f_locals)
            assert TOKEN_CONFIGURATION not in traceback.tb_frame.f_locals.values()
            assert CIPHER not in traceback.tb_frame.f_locals.values()
        traceback = traceback.tb_next


@pytest.mark.parametrize("boundary", ["constructor", "query"])
def test_public_boundary_entry_interruptions_are_sanitized(boundary):
    if boundary == "constructor":
        guarded = GoogleCalendarBusySource.__init__
        operation = _source
    else:
        guarded = GoogleCalendarBusySource.query_busy_periods
        source = _source()
        operation = lambda: source.query_busy_periods(
            instructor_id="inst_entry_private",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
    source_lines, first_line = inspect.getsourcelines(guarded)
    target_line = first_line + next(
        offset
        for offset, line in enumerate(source_lines)
        if line.lstrip().startswith("try: return")
    )
    interrupted = False

    def interrupt_entry(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is guarded.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt("entry-control-private")
        return interrupt_entry

    try:
        sys.settrace(interrupt_entry)
        with pytest.raises(KeyboardInterrupt) as captured:
            operation()
    finally:
        sys.settrace(None)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True
    module_locals = list(_module_frame_locals(captured.value))
    assert module_locals
    for frame_locals in module_locals:
        assert "entry-control-private" not in repr(frame_locals)
        assert TOKEN_CONFIGURATION not in frame_locals.values()
        assert CIPHER not in frame_locals.values()
        assert all(
            type(value) is not GoogleCalendarBusySource
            for value in frame_locals.values()
        )


@pytest.mark.parametrize("boundary", ["constructor", "query"])
def test_public_boundary_cleanup_interruptions_are_sanitized(
    monkeypatch,
    boundary,
):
    if boundary == "constructor":
        guarded = GoogleCalendarBusySource.__init__
        operation = lambda: GoogleCalendarBusySource(
            None,
            configuration=TOKEN_CONFIGURATION,
            credential_cipher=CIPHER,
        )
    else:
        guarded = GoogleCalendarBusySource.query_busy_periods
        source = _source()

        def fail_guarded(*_args, **_kwargs):
            raise RuntimeError("ordinary-cleanup-private")

        monkeypatch.setattr(
            google_calendar_busy_source,
            "_guarded_query_busy_periods",
            fail_guarded,
        )
        operation = lambda: source.query_busy_periods(
            instructor_id="inst_cleanup_private",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
    source_lines, first_line = inspect.getsourcelines(guarded)
    target_line = first_line + next(
        offset
        for offset, line in enumerate(source_lines)
        if line.lstrip().startswith("try:") and " = None" in line
    )
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
            raise KeyboardInterrupt("cleanup-control-private")
        return interrupt_cleanup

    try:
        sys.settrace(interrupt_cleanup)
        with pytest.raises(KeyboardInterrupt) as captured:
            operation()
    finally:
        sys.settrace(None)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True
    module_locals = list(_module_frame_locals(captured.value))
    assert module_locals
    for frame_locals in module_locals:
        output = repr(frame_locals)
        assert "cleanup-control-private" not in output
        assert "ordinary-cleanup-private" not in output
        assert TOKEN_CONFIGURATION not in frame_locals.values()
        assert CIPHER not in frame_locals.values()
        assert all(
            type(value) is not GoogleCalendarBusySource
            for value in frame_locals.values()
        )


@pytest.mark.parametrize("method", ["__reduce__", "__getstate__"])
def test_serialization_rejections_do_not_retain_source(method):
    source = _source()

    with pytest.raises(TypeError) as captured:
        getattr(source, method)()

    for frame_locals in _module_frame_locals(captured.value):
        assert source not in frame_locals.values()
        assert TOKEN_CONFIGURATION not in frame_locals.values()
        assert CIPHER not in frame_locals.values()


def test_source_is_redacted_immutable_and_not_serializable():
    source = _source()

    assert repr(source) == "GoogleCalendarBusySource(<redacted>)"
    assert str(source) == "GoogleCalendarBusySource(<redacted>)"
    assert "client-secret-private" not in repr(source)
    with pytest.raises(AttributeError):
        source._configuration = None
    with pytest.raises(AttributeError):
        del source._credential_cipher
    with pytest.raises(TypeError):
        pickle.dumps(source)
    with pytest.raises(TypeError):
        copy.copy(source)
    with pytest.raises(TypeError):
        type("SourceSubclass", (GoogleCalendarBusySource,), {})


def test_source_cannot_be_reinitialized():
    source = _source()

    def replacement_factory():
        raise AssertionError("replacement factory must not be installed")

    with pytest.raises(ValueError):
        source.__init__(
            replacement_factory,
            configuration=TOKEN_CONFIGURATION,
            credential_cipher=CIPHER,
        )

    assert source._session_factory is _session_factory
    assert source._configuration is TOKEN_CONFIGURATION
    assert source._credential_cipher is CIPHER


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "session_factory": None,
            "configuration": TOKEN_CONFIGURATION,
            "credential_cipher": CIPHER,
        },
        {
            "session_factory": _session_factory,
            "configuration": object(),
            "credential_cipher": CIPHER,
        },
        {
            "session_factory": _session_factory,
            "configuration": TOKEN_CONFIGURATION,
            "credential_cipher": object(),
        },
    ],
)
def test_source_rejects_unsafe_construction(kwargs):
    with pytest.raises(ValueError):
        GoogleCalendarBusySource(**kwargs)
