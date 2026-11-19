from __future__ import annotations

import datetime as dt
import inspect
import sys

import pytest

from app import provider_settings, tools
from app.external_calendar import (
    CalendarBusyInterval,
    CalendarBusySnapshot,
    ExternalCalendarTimeout,
)
from app.models import TimeSlotDTO
from app.provider_settings import (
    ProviderConfiguration,
    ProviderLocation,
    ProviderWorkingHours,
)
from app.routing import BaseRoutingProvider, RouteEstimate
from app.scheduler_settings import default_scheduling_policy, to_buffer_policy


UTC = dt.timezone.utc
THURSDAY = dt.date(2026, 8, 20)


class ZeroRoutingProvider(BaseRoutingProvider):
    provider_name = "external-calendar-zero-routing"

    def estimate_route(
        self,
        origin_coords,
        dest_coords,
        departure_time=None,
        traffic_mode="none",
    ) -> RouteEstimate:
        return RouteEstimate(
            distance_miles=0,
            travel_time_mins=0,
            duration_seconds=0,
            provider=self.provider_name,
        )

    def geocode(self, address: str) -> tuple[float, float]:
        return (30.0, -97.0)


class RecordingBusySource:
    def __init__(
        self,
        snapshot: CalendarBusySnapshot | None = None,
        error: Exception | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.error = error
        self.calls: list[tuple[str, dt.datetime, dt.datetime]] = []

    def query_busy_periods(
        self,
        *,
        instructor_id: str,
        window_start: dt.datetime,
        window_end: dt.datetime,
    ) -> CalendarBusySnapshot:
        self.calls.append((instructor_id, window_start, window_end))
        if self.error is not None:
            raise self.error
        assert self.snapshot is not None
        return self.snapshot


def _contains_identity(value: object, target: object) -> bool:
    if value is target:
        return True
    if isinstance(value, tools._OneShotCalendarBusySource):
        return _contains_identity(value._source_handoff, target)
    if isinstance(value, dict):
        return any(
            _contains_identity(item, target)
            for pair in value.items()
            for item in pair
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_identity(item, target) for item in value)
    return False


def _assert_source_not_retained(
    error: BaseException,
    source: object,
) -> None:
    application_frames = []
    traceback = error.__traceback__
    while traceback is not None:
        if "/app/" in traceback.tb_frame.f_code.co_filename:
            application_frames.append(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next
    assert application_frames
    assert all(
        not any(
            _contains_identity(value, source)
            for value in frame_locals.values()
        )
        for frame_locals in application_frames
    )


def _provider(
    *,
    instructor_id: str = "inst_external",
    timezone: str = "UTC",
    enabled_weekday: str = "thursday",
    start_time: str = "10:00",
    end_time: str = "17:00",
) -> ProviderConfiguration:
    location = ProviderLocation(
        address="Provider location",
        lat=30.0,
        lng=-97.0,
    )
    hours = tuple(
        ProviderWorkingHours(
            weekday=weekday,
            enabled=weekday == enabled_weekday,
            start_time=start_time if weekday == enabled_weekday else None,
            end_time=end_time if weekday == enabled_weekday else None,
        )
        for weekday in provider_settings.WEEKDAY_NAMES
    )
    return ProviderConfiguration(
        id=instructor_id,
        display_name="External Calendar Provider",
        business_name=None,
        timezone=timezone,
        base_location=location,
        service_location=location,
        service_name="Provider service",
        default_duration_mins=60,
        working_hours=hours,
        setup_complete=True,
        setup_missing_fields=(),
    )


def _find_slots(
    monkeypatch: pytest.MonkeyPatch,
    source: RecordingBusySource,
    *,
    provider: ProviderConfiguration | None = None,
    requested_date: dt.date = THURSDAY,
    internal_events: list[dict] | None = None,
) -> list[TimeSlotDTO]:
    active_provider = provider or _provider()
    monkeypatch.setattr(
        tools,
        "require_provider_setup",
        lambda _instructor_id: active_provider,
    )
    monkeypatch.setattr(
        tools.db,
        "get_utc_appointment_events_for_date",
        lambda *_args: list(internal_events or []),
    )
    monkeypatch.setattr(tools, "routing_provider", ZeroRoutingProvider())
    return tools.find_available_swim_slots(
        instructor_id=active_provider.id,
        date_str=requested_date.isoformat(),
        client_address="Provider location",
        is_instructor_facility=True,
        duration_mins=60,
        buffer_policy=to_buffer_policy(default_scheduling_policy()),
        calendar_busy_source=source,
    )


@pytest.mark.parametrize("status", ["disconnected", "verified"])
def test_empty_external_calendar_preserves_availability_and_exact_scope(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    source = RecordingBusySource(
        CalendarBusySnapshot(status=status, periods=()),
    )

    slots = _find_slots(monkeypatch, source)

    assert slots
    assert slots[0].start_time == dt.datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    assert source.calls == [
        (
            "inst_external",
            dt.datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
            dt.datetime(2026, 8, 20, 17, 0, tzinfo=UTC),
        )
    ]


def test_verified_external_busy_period_removes_overlapping_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    busy_start = dt.datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    busy_end = dt.datetime(2026, 8, 20, 13, 0, tzinfo=UTC)
    source = RecordingBusySource(
        CalendarBusySnapshot(
            status="verified",
            periods=(CalendarBusyInterval(start=busy_start, end=busy_end),),
        )
    )

    slots = _find_slots(monkeypatch, source)

    assert slots
    assert all(
        slot.end_time <= busy_start or slot.start_time >= busy_end
        for slot in slots
    )


def test_external_blocker_cannot_move_occupied_frontier_backward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = RecordingBusySource(
        CalendarBusySnapshot(
            status="verified",
            periods=(
                CalendarBusyInterval(
                    start=dt.datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
                    end=dt.datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
                ),
            ),
        )
    )
    contained_internal_event = {
        "start_time": dt.datetime(2026, 8, 20, 10, 30, tzinfo=UTC),
        "end_time": dt.datetime(2026, 8, 20, 11, 0, tzinfo=UTC),
        "lat": 30.0,
        "lng": -97.0,
        "is_break": False,
    }
    original_internal_event = dict(contained_internal_event)
    captured_events: list[dict] = []
    engine_type = tools.SwimSchedulerConstraintEngine

    class CapturingEngine(engine_type):
        def find_available_slots_for_day(self, *, existing_events, **kwargs):
            captured_events.extend(existing_events)
            return super().find_available_slots_for_day(
                existing_events=existing_events,
                **kwargs,
            )

    monkeypatch.setattr(tools, "SwimSchedulerConstraintEngine", CapturingEngine)

    slots = _find_slots(
        monkeypatch,
        source,
        internal_events=[contained_internal_event],
    )

    assert slots
    assert slots[0].start_time == dt.datetime(2026, 8, 20, 12, 15, tzinfo=UTC)
    assert all(
        slot.start_time >= dt.datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
        for slot in slots
    )
    assert contained_internal_event == original_internal_event
    internal = [event for event in captured_events if "lat" in event]
    external = [event for event in captured_events if "lat" not in event]
    assert internal == [contained_internal_event]
    assert external == [
        {
            "start_time": dt.datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
            "end_time": dt.datetime(2026, 8, 20, 10, 30, tzinfo=UTC),
            "event_type": "block",
            "blocker_kind": "external_calendar",
        },
        {
            "start_time": dt.datetime(2026, 8, 20, 11, 0, tzinfo=UTC),
            "end_time": dt.datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
            "event_type": "block",
            "blocker_kind": "external_calendar",
        },
    ]


@pytest.mark.parametrize(
    ("requested_date", "expected_start", "expected_end"),
    [
        (
            dt.date(2026, 3, 8),
            dt.datetime(2026, 3, 8, 5, 0, tzinfo=UTC),
            dt.datetime(2026, 3, 8, 8, 0, tzinfo=UTC),
        ),
        (
            dt.date(2026, 11, 1),
            dt.datetime(2026, 11, 1, 4, 0, tzinfo=UTC),
            dt.datetime(2026, 11, 1, 9, 0, tzinfo=UTC),
        ),
    ],
)
def test_external_query_uses_timezone_correct_dst_working_window(
    monkeypatch: pytest.MonkeyPatch,
    requested_date: dt.date,
    expected_start: dt.datetime,
    expected_end: dt.datetime,
) -> None:
    source = RecordingBusySource(
        CalendarBusySnapshot(status="disconnected", periods=()),
    )
    provider = _provider(
        timezone="America/New_York",
        enabled_weekday="sunday",
        start_time="00:00",
        end_time="04:00",
    )

    _find_slots(
        monkeypatch,
        source,
        provider=provider,
        requested_date=requested_date,
    )

    assert source.calls == [
        (provider.id, expected_start, expected_end),
    ]


def test_external_calendar_failure_produces_no_unverified_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = RecordingBusySource(error=ExternalCalendarTimeout())

    with pytest.raises(ExternalCalendarTimeout) as captured:
        _find_slots(monkeypatch, source)

    assert str(captured.value) == (
        "External calendar availability could not be verified."
    )
    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__ is True
    _assert_source_not_retained(captured.value, source)


@pytest.mark.parametrize(
    ("target", "line_fragment"),
    [
        (
            tools.find_available_swim_slots,
            "source_handoff: list[Optional[CalendarBusySource]] = [",
        ),
        (
            tools._external_busy_events,
            "one_shot_source = _OneShotCalendarBusySource",
        ),
    ],
)
def test_source_is_cleared_when_handoff_setup_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    target,
    line_fragment: str,
) -> None:
    source = RecordingBusySource(
        CalendarBusySnapshot(status="disconnected", periods=()),
    )
    source_lines, source_start = inspect.getsourcelines(target)
    target_line = source_start + next(
        index
        for index, source_line in enumerate(source_lines)
        if line_fragment in source_line
    )
    interrupted = False

    def interrupt_handoff(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is target.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt("calendar-source-handoff-private")
        return interrupt_handoff

    try:
        sys.settrace(interrupt_handoff)
        with pytest.raises(KeyboardInterrupt) as captured:
            _find_slots(monkeypatch, source)
    finally:
        sys.settrace(None)

    assert interrupted is True
    assert source.calls == []
    _assert_source_not_retained(captured.value, source)


@pytest.mark.parametrize(
    "interruption_type",
    [KeyboardInterrupt, UnboundLocalError],
    ids=["keyboard-interrupt", "unbound-local-error"],
)
@pytest.mark.parametrize(
    "line_fragment",
    [
        "try: source_handoff: list[Optional[CalendarBusySource]]",
        "try: calendar_busy_source = None;",
    ],
    ids=["boundary-entry", "cleanup-entry"],
)
def test_booking_source_is_cleared_when_boundary_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    interruption_type: type[BaseException],
    line_fragment: str,
) -> None:
    source = RecordingBusySource(
        CalendarBusySnapshot(status="disconnected", periods=()),
    )

    def missing_provider(_instructor_id: str) -> ProviderConfiguration:
        raise provider_settings.ProviderNotFound()

    monkeypatch.setattr(tools, "load_provider_configuration", missing_provider)
    target = tools.book_swim_lesson
    source_lines, source_start = inspect.getsourcelines(target)
    target_line = source_start + next(
        index
        for index, source_line in enumerate(source_lines)
        if line_fragment in source_line
    )
    interrupted = False

    def interrupt_cleanup(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is target.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise interruption_type("calendar-source-booking-cleanup")
        return interrupt_cleanup

    try:
        sys.settrace(interrupt_cleanup)
        with pytest.raises(interruption_type) as captured:
            tools.book_swim_lesson(
                instructor_id="missing_instructor",
                client_id="client_external",
                client_name="External Client",
                start_time_iso="2026-08-20T12:00:00+00:00",
                duration_mins=60,
                location_address="Provider location",
                calendar_busy_source=source,
            )
    finally:
        sys.settrace(None)

    assert interrupted is True
    assert source.calls == []
    _assert_source_not_retained(captured.value, source)


@pytest.mark.parametrize(
    ("line_fragment", "expected_calls"),
    [
        ("try: source: Optional[CalendarBusySource]", 0),
        ("try: source = None;", 1),
    ],
    ids=["boundary-entry", "cleanup-entry"],
)
def test_one_shot_source_is_cleared_when_query_is_interrupted(
    line_fragment: str,
    expected_calls: int,
) -> None:
    source = RecordingBusySource(
        CalendarBusySnapshot(status="disconnected", periods=()),
    )
    one_shot_source = tools._OneShotCalendarBusySource([source])
    target = tools._OneShotCalendarBusySource.query_busy_periods
    source_lines, source_start = inspect.getsourcelines(target)
    target_line = source_start + next(
        index
        for index, source_line in enumerate(source_lines)
        if line_fragment in source_line
    )
    interrupted = False

    def interrupt_query(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is target.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt("calendar-source-query-cleanup")
        return interrupt_query

    try:
        sys.settrace(interrupt_query)
        with pytest.raises(KeyboardInterrupt) as captured:
            one_shot_source.query_busy_periods(
                instructor_id="inst_external",
                window_start=dt.datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
                window_end=dt.datetime(2026, 8, 20, 17, 0, tzinfo=UTC),
            )
    finally:
        sys.settrace(None)

    assert interrupted is True
    assert len(source.calls) == expected_calls
    assert one_shot_source._source_handoff == []
    _assert_source_not_retained(captured.value, source)


def test_final_booking_revalidation_forwards_the_same_busy_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    source = RecordingBusySource(
        CalendarBusySnapshot(status="disconnected", periods=()),
    )
    captured_source = None

    def no_available_slots(**kwargs):
        nonlocal captured_source
        captured_source = kwargs["calendar_busy_source"]
        return []

    monkeypatch.setattr(
        tools,
        "load_provider_configuration",
        lambda _instructor_id: provider,
    )
    monkeypatch.setattr(
        tools.db,
        "get_appointment_by_idempotency_key",
        lambda *_args: None,
    )
    monkeypatch.setattr(tools, "find_available_swim_slots", no_available_slots)
    monkeypatch.setattr(
        tools.db,
        "save_appointment",
        lambda **_kwargs: pytest.fail("an unavailable slot must not be saved"),
    )

    with pytest.raises(tools.db.ScheduleConflictError):
        tools.book_swim_lesson(
            instructor_id=provider.id,
            client_id="client_external",
            client_name="External Client",
            start_time_iso="2026-08-20T12:00:00+00:00",
            duration_mins=60,
            location_address="Provider location",
            is_instructor_facility=True,
            buffer_policy=to_buffer_policy(default_scheduling_policy()),
            calendar_busy_source=source,
        )

    assert captured_source is source


def test_booking_calendar_failure_retains_no_source_and_saves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    source = RecordingBusySource(error=ExternalCalendarTimeout())
    monkeypatch.setattr(
        tools,
        "load_provider_configuration",
        lambda _instructor_id: provider,
    )
    monkeypatch.setattr(
        tools,
        "require_provider_setup",
        lambda _instructor_id: provider,
    )
    monkeypatch.setattr(
        tools.db,
        "get_appointment_by_idempotency_key",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        tools.db,
        "get_utc_appointment_events_for_date",
        lambda *_args: [],
    )
    monkeypatch.setattr(tools, "routing_provider", ZeroRoutingProvider())
    monkeypatch.setattr(
        tools.db,
        "save_appointment",
        lambda **_kwargs: pytest.fail("an unverified booking must not be saved"),
    )

    with pytest.raises(ExternalCalendarTimeout) as captured:
        tools.book_swim_lesson(
            instructor_id=provider.id,
            client_id="client_external",
            client_name="External Client",
            start_time_iso="2026-08-20T12:00:00+00:00",
            duration_mins=60,
            location_address="Provider location",
            is_instructor_facility=True,
            buffer_policy=to_buffer_policy(default_scheduling_policy()),
            calendar_busy_source=source,
        )

    assert len(source.calls) == 1
    _assert_source_not_retained(captured.value, source)


def test_idempotent_booking_replay_does_not_requery_external_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    source = RecordingBusySource(error=ExternalCalendarTimeout())
    start = dt.datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        tools,
        "load_provider_configuration",
        lambda _instructor_id: provider,
    )
    monkeypatch.setattr(
        tools.db,
        "get_appointment_by_idempotency_key",
        lambda *_args: {
            "id": "appointment_existing",
            "instructor_id": provider.id,
            "client_id": "client_external",
            "start_time": start,
            "end_time": start + dt.timedelta(hours=1),
        },
    )
    monkeypatch.setattr(
        tools,
        "find_available_swim_slots",
        lambda **_kwargs: pytest.fail("a committed replay must not be revalidated"),
    )

    result = tools.book_swim_lesson(
        instructor_id=provider.id,
        client_id="client_external",
        client_name="External Client",
        start_time_iso=start.isoformat(),
        duration_mins=60,
        location_address="Provider location",
        idempotency_key="idempotency_external",
        is_instructor_facility=True,
        calendar_busy_source=source,
    )

    assert result["appointment_id"] == "appointment_existing"
    assert source.calls == []
