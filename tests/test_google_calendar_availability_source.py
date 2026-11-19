from __future__ import annotations

import asyncio
import copy
import datetime
import inspect
import pickle
import sys

import pytest

from app import db, google_calendar_availability_source as source_module
from app.calendar_connections import (
    disconnect_google_calendar,
    mark_google_calendar_authorization_required,
    replace_google_calendar_connection,
)
from app.calendar_credentials import CalendarCredentialCipher
from app.external_calendar import (
    MAX_BUSY_QUERY_WINDOW,
    CalendarBusyInterval,
    CalendarBusySnapshot,
    ExternalCalendarAuthorizationRequired,
    ExternalCalendarInvalidResponse,
    ExternalCalendarTimeout,
    ExternalCalendarUnavailable,
)
from app.google_calendar_availability_source import (
    GoogleCalendarAvailabilitySource,
)
from app.models import (
    InstructorDB,
    ProviderAccountDB,
    ProviderCalendarConnectionDB,
)


UTC = datetime.timezone.utc
WINDOW_START = datetime.datetime(2026, 8, 20, 14, 0, tzinfo=UTC)
WINDOW_END = WINDOW_START + datetime.timedelta(hours=8)
CIPHER = CalendarCredentialCipher(
    active_key_id="availability-source-primary",
    keys={"availability-source-primary": b"s" * 32},
)


class RecordingBusySource:
    def __init__(
        self,
        *,
        snapshot: CalendarBusySnapshot | object,
        failure: BaseException | None = None,
        before_return=None,
    ) -> None:
        self.snapshot = snapshot
        self.failure = failure
        self.before_return = before_return
        self.calls: list[tuple[str, datetime.datetime, datetime.datetime]] = []

    def query_busy_periods(
        self,
        *,
        instructor_id: str,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> CalendarBusySnapshot:
        self.calls.append((instructor_id, window_start, window_end))
        if self.failure is not None:
            raise self.failure
        if self.before_return is not None:
            self.before_return()
        return self.snapshot


class StubFence:
    def __init__(self, state: str) -> None:
        self.state = state


class RaisingSourceFactory:
    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.session_factories: list[object] = []

    def __call__(self, session_factory):
        self.session_factories.append(session_factory)
        raise self.failure


def _source(
    *,
    instructor_id: str = "inst_availability_source",
    session_factory=lambda: None,
    busy_source_factory=lambda _session_factory: None,
) -> GoogleCalendarAvailabilitySource:
    return GoogleCalendarAvailabilitySource(
        session_factory,
        instructor_id=instructor_id,
        busy_source_factory=busy_source_factory,
    )


def _stub_fence(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: str,
) -> tuple[StubFence, list[StubFence]]:
    fence = StubFence(state)
    revalidated: list[StubFence] = []

    def claim(_session_factory, *, instructor_id):
        assert instructor_id == "inst_availability_source"
        return fence

    def revalidate(_session_factory, *, fence):
        revalidated.append(fence)

    monkeypatch.setattr(
        source_module,
        "claim_google_calendar_availability_fence",
        claim,
    )
    monkeypatch.setattr(
        source_module,
        "revalidate_google_calendar_availability_fence",
        revalidate,
    )
    return fence, revalidated


def _query(source: GoogleCalendarAvailabilitySource) -> CalendarBusySnapshot:
    return source.query_busy_periods(
        instructor_id="inst_availability_source",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )


def _add_owner(suffix: str) -> tuple[str, str]:
    instructor_id = f"inst_availability_source_{suffix}"
    account_id = f"acct_availability_source_{suffix}"
    with db.SessionLocal.begin() as session:
        session.add(
            InstructorDB(
                id=instructor_id,
                name=f"Availability Source {suffix}",
                main_address="1 Source Way",
                lat=30.2,
                lng=-97.2,
                work_start_time="08:00",
                work_end_time="17:00",
                timezone="America/Chicago",
            )
        )
        session.add(
            ProviderAccountDB(
                id=account_id,
                instructor_id=instructor_id,
                email_normalized=f"availability-source-{suffix}@example.com",
                password_hash="not-used-by-availability-source-test",
                is_demo=False,
                session_version=1,
            )
        )
    return account_id, instructor_id


def _connect(account_id: str) -> None:
    with db.SessionLocal.begin() as session:
        replace_google_calendar_connection(
            session,
            account_id=account_id,
            refresh_token="availability-source-refresh-private",
            cipher=CIPHER,
            now=WINDOW_START,
        )


def _connection_id(account_id: str) -> str:
    with db.SessionLocal() as session:
        value = session.query(ProviderCalendarConnectionDB.id).filter_by(
            account_id=account_id
        ).scalar()
    assert isinstance(value, str)
    return value


def _mark_authorization_required(account_id: str) -> None:
    with db.SessionLocal.begin() as session:
        mark_google_calendar_authorization_required(
            session,
            account_id=account_id,
            expected_connection_id=_connection_id(account_id),
            now=WINDOW_START + datetime.timedelta(minutes=1),
        )


def _module_frame_locals(error: BaseException) -> list[dict]:
    frames = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith(
            "/app/google_calendar_availability_source.py"
        ):
            frames.append(current.tb_frame.f_locals)
        current = current.tb_next
    return frames


def _contains_identity(value: object, target: object) -> bool:
    if value is target:
        return True
    if isinstance(value, dict):
        return any(
            _contains_identity(item, target)
            for pair in value.items()
            for item in pair
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_identity(item, target) for item in value)
    return False


def _contains_calendar_payload(
    value: object,
    seen: set[int] | None = None,
) -> bool:
    if isinstance(value, (CalendarBusySnapshot, CalendarBusyInterval)):
        return True
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return False
    seen.add(identity)
    if isinstance(value, dict):
        return any(
            _contains_calendar_payload(item, seen)
            for pair in value.items()
            for item in pair
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_calendar_payload(item, seen) for item in value)
    return False


def test_disconnected_fence_returns_empty_without_creating_provider_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence, revalidated = _stub_fence(monkeypatch, state="disconnected")
    factory_calls: list[str] = []

    def forbidden_factory(_session_factory):
        factory_calls.append("called")
        raise AssertionError("disconnected calendars must not load secrets")

    result = _query(_source(busy_source_factory=forbidden_factory))

    assert result == CalendarBusySnapshot(status="disconnected", periods=())
    assert factory_calls == []
    assert revalidated == [fence]


def test_authorization_required_fence_fails_before_provider_source_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence, revalidated = _stub_fence(
        monkeypatch,
        state="authorization_required",
    )
    factory_calls: list[str] = []

    def forbidden_factory(_session_factory):
        factory_calls.append("called")
        raise AssertionError("authorization-required calendars must not load secrets")

    with pytest.raises(ExternalCalendarAuthorizationRequired) as captured:
        _query(_source(busy_source_factory=forbidden_factory))

    assert factory_calls == []
    assert revalidated == [fence]
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_connected_query_uses_one_fresh_source_and_revalidates_after_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = lambda: None
    fence = StubFence("connected")
    interval = CalendarBusyInterval(
        start=WINDOW_START + datetime.timedelta(hours=1),
        end=WINDOW_START + datetime.timedelta(hours=2),
    )
    concrete = RecordingBusySource(
        snapshot=CalendarBusySnapshot(status="verified", periods=(interval,)),
    )
    factory_calls: list[str] = []
    revalidation_calls: list[StubFence] = []

    monkeypatch.setattr(
        source_module,
        "claim_google_calendar_availability_fence",
        lambda *_args, **_kwargs: fence,
    )

    def revalidate(_session_factory, *, fence):
        caller = inspect.currentframe().f_back
        assert caller is not None
        assert caller.f_locals["concrete_source"] is None
        revalidation_calls.append(fence)

    monkeypatch.setattr(
        source_module,
        "revalidate_google_calendar_availability_fence",
        revalidate,
    )

    def factory(captured_session_factory):
        assert captured_session_factory is session_factory
        factory_calls.append("called")
        return concrete

    result = _query(
        _source(
            session_factory=session_factory,
            busy_source_factory=factory,
        )
    )

    assert result == CalendarBusySnapshot(status="verified", periods=(interval,))
    assert factory_calls == ["called"]
    assert concrete.calls == [
        ("inst_availability_source", WINDOW_START, WINDOW_END),
    ]
    assert revalidation_calls == [fence]


def test_connected_query_rejects_a_disconnected_provider_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_fence(monkeypatch, state="connected")
    concrete = RecordingBusySource(
        snapshot=CalendarBusySnapshot(status="disconnected", periods=()),
    )

    with pytest.raises(ExternalCalendarUnavailable) as captured:
        _query(_source(busy_source_factory=lambda _session_factory: concrete))

    assert type(captured.value) is ExternalCalendarUnavailable
    assert captured.value.reason_code == "provider_unavailable"


def test_each_query_uses_a_fresh_source_and_failures_are_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = StubFence("connected")
    claims: list[str] = []
    revalidations: list[StubFence] = []
    first_interval = CalendarBusyInterval(
        start=WINDOW_START + datetime.timedelta(minutes=15),
        end=WINDOW_START + datetime.timedelta(minutes=45),
    )
    second_interval = CalendarBusyInterval(
        start=WINDOW_START + datetime.timedelta(hours=3),
        end=WINDOW_START + datetime.timedelta(hours=4),
    )
    sources = [
        RecordingBusySource(
            snapshot=CalendarBusySnapshot(
                status="verified",
                periods=(first_interval,),
            )
        ),
        RecordingBusySource(
            snapshot=CalendarBusySnapshot(status="verified", periods=()),
            failure=ExternalCalendarTimeout(),
        ),
        RecordingBusySource(
            snapshot=CalendarBusySnapshot(
                status="verified",
                periods=(second_interval,),
            )
        ),
    ]
    created: list[RecordingBusySource] = []

    def claim(_session_factory, *, instructor_id):
        claims.append(instructor_id)
        return fence

    def revalidate(_session_factory, *, fence):
        revalidations.append(fence)

    def factory(_session_factory):
        source = sources[len(created)]
        created.append(source)
        return source

    monkeypatch.setattr(
        source_module,
        "claim_google_calendar_availability_fence",
        claim,
    )
    monkeypatch.setattr(
        source_module,
        "revalidate_google_calendar_availability_fence",
        revalidate,
    )
    source = _source(busy_source_factory=factory)

    first = _query(source)
    with pytest.raises(ExternalCalendarTimeout):
        _query(source)
    second = _query(source)

    assert first.periods == (first_interval,)
    assert second.periods == (second_interval,)
    assert created == sources
    assert claims == ["inst_availability_source"] * 3
    assert revalidations == [fence, fence]
    assert [len(item.calls) for item in sources] == [1, 1, 1]


@pytest.mark.parametrize(
    ("failure", "expected_type", "expected_reason"),
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
            RuntimeError("provider-collaborator-private"),
            ExternalCalendarUnavailable,
            "provider_unavailable",
        ),
    ],
)
def test_provider_failures_are_canonical_cause_free_and_do_not_retain_source(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    expected_type: type[ExternalCalendarUnavailable],
    expected_reason: str,
) -> None:
    _stub_fence(monkeypatch, state="connected")
    concrete = RecordingBusySource(
        snapshot=CalendarBusySnapshot(status="verified", periods=()),
        failure=failure,
    )

    with pytest.raises(expected_type) as captured:
        _query(_source(busy_source_factory=lambda _session_factory: concrete))

    assert type(captured.value) is expected_type
    assert captured.value.reason_code == expected_reason
    assert str(captured.value) == (
        "External calendar availability could not be verified."
    )
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True
    assert all(
        not any(
            _contains_identity(value, concrete)
            for value in frame_locals.values()
        )
        for frame_locals in _module_frame_locals(captured.value)
    )


@pytest.mark.parametrize(
    ("failure", "expected_type", "expected_reason"),
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
            RuntimeError("factory-collaborator-private"),
            ExternalCalendarUnavailable,
            "provider_unavailable",
        ),
    ],
)
def test_factory_failures_are_canonical_and_do_not_retain_factory(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    expected_type: type[ExternalCalendarUnavailable],
    expected_reason: str,
) -> None:
    _stub_fence(monkeypatch, state="connected")
    session_factory = lambda: None
    factory = RaisingSourceFactory(failure)
    source = _source(
        session_factory=session_factory,
        busy_source_factory=factory,
    )

    with pytest.raises(expected_type) as captured:
        _query(source)

    assert type(captured.value) is expected_type
    assert captured.value.reason_code == expected_reason
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert factory.session_factories == [session_factory]
    assert all(
        not any(
            _contains_identity(value, factory)
            for value in frame_locals.values()
        )
        for frame_locals in _module_frame_locals(captured.value)
    )


@pytest.mark.parametrize(
    ("window_start", "window_end"),
    [
        (WINDOW_START.replace(tzinfo=None), WINDOW_END),
        (WINDOW_START, WINDOW_END.replace(tzinfo=None)),
        (WINDOW_END, WINDOW_START),
        (WINDOW_START, WINDOW_START),
        (WINDOW_START, WINDOW_START + MAX_BUSY_QUERY_WINDOW + datetime.timedelta(microseconds=1)),
        ("2026-08-20T14:00:00Z", WINDOW_END),
    ],
)
def test_invalid_windows_fail_before_database_or_factory_access(
    window_start,
    window_end,
) -> None:
    touched: list[str] = []

    def session_factory():
        touched.append("database")
        raise AssertionError("invalid windows must fail before database access")

    def source_factory(_session_factory):
        touched.append("factory")
        raise AssertionError("invalid windows must fail before source creation")

    source = _source(
        session_factory=session_factory,
        busy_source_factory=source_factory,
    )
    with pytest.raises(ValueError) as captured:
        source.query_busy_periods(
            instructor_id="inst_availability_source",
            window_start=window_start,
            window_end=window_end,
        )

    assert str(captured.value) == (
        "Google Calendar availability query is invalid."
    )
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert touched == []


def test_cross_instructor_query_fails_before_database_or_factory_access() -> None:
    touched: list[str] = []

    def forbidden(kind: str):
        def operation(*_args):
            touched.append(kind)
            raise AssertionError("cross-instructor queries must not perform I/O")

        return operation

    source = _source(
        session_factory=forbidden("database"),
        busy_source_factory=forbidden("factory"),
    )

    with pytest.raises(ExternalCalendarUnavailable) as captured:
        source.query_busy_periods(
            instructor_id="inst_other",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )

    assert type(captured.value) is ExternalCalendarUnavailable
    assert captured.value.reason_code == "provider_unavailable"
    assert touched == []


@pytest.mark.parametrize(
    ("control", "expected_type", "expected_args"),
    [
        (KeyboardInterrupt("control-private"), KeyboardInterrupt, ()),
        (SystemExit("control-private"), SystemExit, (1,)),
        (GeneratorExit("control-private"), GeneratorExit, ()),
        (asyncio.CancelledError("control-private"), asyncio.CancelledError, ()),
    ],
)
def test_process_controls_are_reemitted_fresh_after_source_release(
    monkeypatch: pytest.MonkeyPatch,
    control: BaseException,
    expected_type: type[BaseException],
    expected_args: tuple,
) -> None:
    _stub_fence(monkeypatch, state="connected")
    concrete = RecordingBusySource(
        snapshot=CalendarBusySnapshot(status="verified", periods=()),
        failure=control,
    )

    with pytest.raises(expected_type) as captured:
        _query(_source(busy_source_factory=lambda _session_factory: concrete))

    assert captured.value is not control
    assert captured.value.args == expected_args
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert all(
        not any(
            _contains_identity(value, concrete)
            for value in frame_locals.values()
        )
        for frame_locals in _module_frame_locals(captured.value)
    )


@pytest.mark.parametrize(
    ("control", "expected_type", "expected_args"),
    [
        (KeyboardInterrupt("factory-control-private"), KeyboardInterrupt, ()),
        (SystemExit("factory-control-private"), SystemExit, (1,)),
        (GeneratorExit("factory-control-private"), GeneratorExit, ()),
        (
            asyncio.CancelledError("factory-control-private"),
            asyncio.CancelledError,
            (),
        ),
    ],
)
def test_factory_process_controls_are_reemitted_after_factory_release(
    monkeypatch: pytest.MonkeyPatch,
    control: BaseException,
    expected_type: type[BaseException],
    expected_args: tuple,
) -> None:
    _stub_fence(monkeypatch, state="connected")
    factory = RaisingSourceFactory(control)

    with pytest.raises(expected_type) as captured:
        _query(_source(busy_source_factory=factory))

    assert captured.value is not control
    assert captured.value.args == expected_args
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert all(
        not any(
            _contains_identity(value, factory)
            for value in frame_locals.values()
        )
        for frame_locals in _module_frame_locals(captured.value)
    )


@pytest.mark.parametrize("boundary", ["constructor", "query"])
def test_public_boundary_entry_interruptions_are_sanitized(boundary: str) -> None:
    factory_marker = lambda _session_factory: None
    if boundary == "constructor":
        guarded = GoogleCalendarAvailabilitySource.__init__
        operation = lambda: GoogleCalendarAvailabilitySource(
            lambda: None,
            instructor_id="inst_entry_private",
            busy_source_factory=factory_marker,
        )
    else:
        guarded = GoogleCalendarAvailabilitySource.query_busy_periods
        source = _source(
            instructor_id="inst_entry_private",
            busy_source_factory=factory_marker,
        )
        operation = lambda: source.query_busy_periods(
            instructor_id="inst_entry_private",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
    source_lines, source_start = inspect.getsourcelines(guarded)
    target_line = source_start + next(
        index
        for index, source_line in enumerate(source_lines)
        if source_line.lstrip().startswith("try: return")
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

    assert interrupted is True
    assert captured.value.args == ()
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    frames = _module_frame_locals(captured.value)
    assert frames
    for frame_locals in frames:
        output = repr(frame_locals)
        assert "entry-control-private" not in output
        assert "inst_entry_private" not in output
        assert factory_marker not in frame_locals.values()
        assert all(
            type(value) is not GoogleCalendarAvailabilitySource
            for value in frame_locals.values()
        )


@pytest.mark.parametrize("boundary", ["constructor", "query"])
def test_public_boundary_cleanup_interruptions_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    factory_marker = lambda _session_factory: None
    if boundary == "constructor":
        guarded = GoogleCalendarAvailabilitySource.__init__
        operation = lambda: GoogleCalendarAvailabilitySource(
            None,
            instructor_id="inst_cleanup_private",
            busy_source_factory=factory_marker,
        )
    else:
        guarded = GoogleCalendarAvailabilitySource.query_busy_periods
        source = _source(
            instructor_id="inst_cleanup_private",
            busy_source_factory=factory_marker,
        )

        def fail_guarded(*_args, **_kwargs):
            raise RuntimeError("ordinary-cleanup-private")

        monkeypatch.setattr(
            source_module,
            "_guarded_query_busy_periods",
            fail_guarded,
        )
        operation = lambda: source.query_busy_periods(
            instructor_id="inst_cleanup_private",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
    source_lines, source_start = inspect.getsourcelines(guarded)
    target_line = source_start + next(
        index
        for index, source_line in enumerate(source_lines)
        if source_line.lstrip().startswith("try:") and " = None" in source_line
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

    assert interrupted is True
    assert captured.value.args == ()
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    frames = _module_frame_locals(captured.value)
    assert frames
    for frame_locals in frames:
        output = repr(frame_locals)
        assert "cleanup-control-private" not in output
        assert "ordinary-cleanup-private" not in output
        assert "inst_cleanup_private" not in output
        assert factory_marker not in frame_locals.values()
        assert all(
            type(value) is not GoogleCalendarAvailabilitySource
            for value in frame_locals.values()
        )


def test_source_is_final_immutable_redacted_and_nonserializable() -> None:
    source = _source()

    assert repr(source) == "GoogleCalendarAvailabilitySource(<redacted>)"
    assert str(source) == repr(source)
    with pytest.raises(TypeError):
        class Child(GoogleCalendarAvailabilitySource):
            pass
    with pytest.raises(AttributeError):
        source._instructor_id = "inst_other"
    with pytest.raises(AttributeError):
        del source._instructor_id
    with pytest.raises(TypeError):
        pickle.dumps(source)
    with pytest.raises(TypeError):
        copy.copy(source)


@pytest.mark.parametrize(
    "state",
    ["disconnected", "authorization_required", "connected"],
)
def test_real_fence_states_drive_secret_free_or_verified_behavior(
    migrated_postgres_db,
    state: str,
) -> None:
    account_id, instructor_id = _add_owner(state)
    if state != "disconnected":
        _connect(account_id)
    if state == "authorization_required":
        _mark_authorization_required(account_id)
    concrete = RecordingBusySource(
        snapshot=CalendarBusySnapshot(status="verified", periods=()),
    )
    factory_calls: list[str] = []

    def factory(session_factory):
        assert session_factory is db.SessionLocal
        factory_calls.append("called")
        return concrete

    source = GoogleCalendarAvailabilitySource(
        db.SessionLocal,
        instructor_id=instructor_id,
        busy_source_factory=factory,
    )
    if state == "authorization_required":
        with pytest.raises(ExternalCalendarAuthorizationRequired):
            source.query_busy_periods(
                instructor_id=instructor_id,
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )
    else:
        result = source.query_busy_periods(
            instructor_id=instructor_id,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
        assert result.status == (
            "disconnected" if state == "disconnected" else "verified"
        )

    assert factory_calls == ([] if state != "connected" else ["called"])
    assert concrete.calls == (
        []
        if state != "connected"
        else [(instructor_id, WINDOW_START, WINDOW_END)]
    )


def test_final_revalidation_discards_snapshot_after_concurrent_disconnect(
    migrated_postgres_db,
) -> None:
    account_id, instructor_id = _add_owner("disconnect_race")
    _connect(account_id)

    def disconnect_during_query() -> None:
        with db.SessionLocal.begin() as session:
            disconnect_google_calendar(session, account_id=account_id)

    interval = CalendarBusyInterval(
        start=WINDOW_START + datetime.timedelta(hours=1),
        end=WINDOW_START + datetime.timedelta(hours=2),
    )
    concrete = RecordingBusySource(
        snapshot=CalendarBusySnapshot(status="verified", periods=(interval,)),
        before_return=disconnect_during_query,
    )
    source = GoogleCalendarAvailabilitySource(
        db.SessionLocal,
        instructor_id=instructor_id,
        busy_source_factory=lambda _session_factory: concrete,
    )

    with pytest.raises(ExternalCalendarUnavailable) as captured:
        source.query_busy_periods(
            instructor_id=instructor_id,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )

    assert type(captured.value) is ExternalCalendarUnavailable
    assert captured.value.reason_code == "provider_unavailable"
    assert concrete.calls == [(instructor_id, WINDOW_START, WINDOW_END)]
    assert all(
        not any(
            _contains_calendar_payload(value)
            for value in frame_locals.values()
        )
        for frame_locals in _module_frame_locals(captured.value)
    )
