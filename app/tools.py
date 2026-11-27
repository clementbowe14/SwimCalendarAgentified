import datetime
import threading
from typing import List, Optional

from app import db, timezones
from app.engine import BufferPolicy, SchedulePosition, SwimSchedulerConstraintEngine
from app.external_calendar import (
    CalendarBusySnapshot,
    CalendarBusySource,
    query_calendar_busy_snapshot,
)
from app.models import LocationInfo, TimeSlotDTO
from app.provider_settings import (
    ProviderConfiguration,
    ProviderNotFound,
    ProviderSetupIncomplete,
    load_provider_configuration,
    require_provider_setup,
    working_interval_for_date,
)
from app.routing import BaseRoutingProvider, RoutingProviderNotConfigured
from app.routing_service import build_routing_provider
from app.scheduler_settings import load_buffer_policy


class UnconfiguredRoutingProvider(BaseRoutingProvider):
    provider_name = "unconfigured"

    def __init__(self, error: RoutingProviderNotConfigured):
        self.error = error

    def estimate_route(self, origin_coords, dest_coords, departure_time=None, traffic_mode="none"):
        raise self.error

    def geocode(self, address: str):
        raise self.error


class _OneShotCalendarBusySource:
    """Delegate one query without retaining its secret-bearing source."""

    __slots__ = ("_source_handoff",)

    def __init__(
        self,
        source_handoff: list[Optional[CalendarBusySource]],
    ) -> None:
        self._source_handoff = source_handoff

    def query_busy_periods(
        self,
        *,
        instructor_id: str,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> CalendarBusySnapshot:
        try: source: Optional[CalendarBusySource] = _take_calendar_busy_source(self._source_handoff); return _query_calendar_busy_source(source, instructor_id=instructor_id, window_start=window_start, window_end=window_end, _cleared=(source := None,))
        finally:
            try: source = None; _clear_calendar_busy_source_handoff(self._source_handoff)
            except BaseException:
                source = None
                _clear_calendar_busy_source_handoff(self._source_handoff)
                raise


def _clear_calendar_busy_source_handoff(
    source_handoff: list[Optional[CalendarBusySource]],
) -> None:
    first_failure: Optional[BaseException] = None
    cleared = False
    for _attempt in range(3):
        try:
            list.clear(source_handoff)
            cleared = True
            break
        except BaseException as raised:
            if first_failure is None:
                first_failure = raised
            raised = None
    if not cleared:
        assert first_failure is not None
        raise first_failure
    if first_failure is not None:
        raise first_failure


def _clear_optional_calendar_busy_source_handoff(
    source_handoff: Optional[list[Optional[CalendarBusySource]]],
) -> None:
    if source_handoff is not None:
        _clear_calendar_busy_source_handoff(source_handoff)


def _query_calendar_busy_source(
    source: Optional[CalendarBusySource],
    *,
    instructor_id: str,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
    _cleared: object,
) -> CalendarBusySnapshot:
    try: return source.query_busy_periods(instructor_id=instructor_id, window_start=window_start, window_end=(source := None, window_end)[1])  # type: ignore[union-attr]
    finally:
        try: source = None
        except BaseException:
            source = None
            raise


def _build_default_routing_provider():
    try:
        return build_routing_provider()
    except RoutingProviderNotConfigured as exc:
        return UnconfiguredRoutingProvider(exc)


_routing_provider_lock = threading.RLock()
_default_routing_provider = _build_default_routing_provider()
routing_provider = _default_routing_provider
_routing_provider_overridden = False


def configure_routing_provider(provider):
    global routing_provider, _routing_provider_overridden
    with _routing_provider_lock:
        routing_provider = provider
        _routing_provider_overridden = True


def configure_default_routing_provider(provider):
    global routing_provider, _default_routing_provider
    with _routing_provider_lock:
        _default_routing_provider = provider
        if not _routing_provider_overridden:
            routing_provider = provider


def reset_routing_provider():
    global routing_provider, _routing_provider_overridden
    with _routing_provider_lock:
        _routing_provider_overridden = False
        routing_provider = _default_routing_provider


def find_available_swim_slots(
    instructor_id: str,
    date_str: str,
    client_address: str,
    is_instructor_facility: bool = False,
    duration_mins: int = 60,
    buffer_policy: Optional[BufferPolicy] = None,
    *,
    calendar_busy_source: Optional[CalendarBusySource] = None,
) -> List[TimeSlotDTO]:
    """Find valid slots without retaining a secret-bearing busy source."""

    try: source_handoff: list[Optional[CalendarBusySource]] = [calendar_busy_source]; calendar_busy_source = None; return _find_available_swim_slots(instructor_id, date_str, client_address, is_instructor_facility, duration_mins, buffer_policy, source_handoff)
    finally:
        try: calendar_busy_source = None; _clear_optional_calendar_busy_source_handoff(locals().get("source_handoff"))
        except BaseException:
            calendar_busy_source = None
            _clear_optional_calendar_busy_source_handoff(
                locals().get("source_handoff")
            )
            raise


def _find_available_swim_slots(
    instructor_id: str,
    date_str: str,
    client_address: str,
    is_instructor_facility: bool,
    duration_mins: int,
    buffer_policy: Optional[BufferPolicy],
    calendar_busy_source_handoff: list[Optional[CalendarBusySource]],
) -> List[TimeSlotDTO]:
    """
    Finds all valid swim lesson slots for a given client request on a specific date.
    Handles geocoding, instructor facility location preference, existing bookings, and distance buffers.
    """
    try:
        provider = require_provider_setup(instructor_id)
    except ProviderNotFound as exc:
        raise ValueError(f"Instructor {instructor_id} not found.") from exc

    # A complete provider always has both locations, but retaining the guard
    # keeps this boundary fail-closed if malformed legacy data is introduced.
    if provider.base_location is None or provider.service_location is None:
        raise ProviderSetupIncomplete(provider.setup_missing_fields)

    target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    working_window = working_window_utc(provider, target_date)
    if working_window is None:
        return []
    day_start, day_end = working_window

    instructor_base_loc = LocationInfo(
        address=provider.base_location.address,
        lat=provider.base_location.lat,
        lng=provider.base_location.lng,
        is_instructor_facility=False,
    )
    instructor_service_loc = LocationInfo(
        address=provider.service_location.address,
        lat=provider.service_location.lat,
        lng=provider.service_location.lng,
        is_instructor_facility=True,
    )

    if is_instructor_facility:
        target_loc = instructor_service_loc
    else:
        lat, lng = routing_provider.geocode(client_address)
        target_loc = LocationInfo(
            address=client_address,
            lat=lat,
            lng=lng,
            is_instructor_facility=False,
        )

    internal_events = db.get_utc_appointment_events_for_date(
        instructor_id,
        date_str,
        include_day_edges=True,
    )
    bounded_internal_events = clip_schedule_events(
        internal_events,
        day_start=day_start,
        day_end=day_end,
    )
    external_events = _external_busy_events(
        calendar_busy_source_handoff,
        instructor_id=instructor_id,
        day_start=day_start,
        day_end=day_end,
        internal_events=bounded_internal_events,
    )
    position, existing_events = working_schedule_context(
        day_start, instructor_base_loc, [*internal_events, *external_events]
    )
    policy = buffer_policy or load_buffer_policy(instructor_id, operation="availability")
    constraint_engine = SwimSchedulerConstraintEngine(
        routing_provider,
        policy,
        timezone_name=provider.timezone,
    )
    utc_slots = constraint_engine.find_available_slots_for_day(
        day_start=day_start,
        day_end=day_end,
        instructor_main_location=instructor_base_loc,
        existing_events=existing_events,
        target_location=target_loc,
        duration_mins=duration_mins,
        initial_position=position,
    )

    return [
        slot.model_copy(
            update={
                "start_time": timezones.aware_to_local(slot.start_time, provider.timezone),
                "end_time": timezones.aware_to_local(slot.end_time, provider.timezone),
            }
        )
        for slot in utc_slots
        if slot.start_time >= day_start and slot.end_time <= day_end
    ]


def working_window_utc(
    provider: ProviderConfiguration,
    target_date: datetime.date,
) -> Optional[tuple[datetime.datetime, datetime.datetime]]:
    working_interval = working_interval_for_date(provider, target_date)
    if working_interval is None:
        return None
    start_label, end_label = working_interval
    try:
        start = timezones.parse_local_date_time(target_date, start_label, provider.timezone)
        end = timezones.parse_local_date_time(target_date, end_label, provider.timezone)
    except (timezones.AmbiguousLocalTimeError, timezones.NonexistentLocalTimeError):
        return None
    if end <= start:
        return None
    return timezones.ensure_aware_utc(start), timezones.ensure_aware_utc(end)


def working_schedule_context(
    work_start: datetime.datetime,
    base: LocationInfo,
    events: List[dict],
) -> tuple[SchedulePosition, List[dict]]:
    """Keep the known location before work and travel anchors after it."""
    position = SchedulePosition(work_start, (base.lat, base.lng))
    working_events = []
    for event in sorted(events, key=lambda event: (event["start_time"], event["end_time"])):
        if event["end_time"] <= work_start:
            if event.get("blocker_kind") != "external_calendar":
                position = position.after_event(event)
        else:
            working_events.append({**event, "start_time": max(event["start_time"], work_start)})
    return position, working_events


def _external_busy_events(
    source_handoff: list[Optional[CalendarBusySource]],
    *,
    instructor_id: str,
    day_start: datetime.datetime,
    day_end: datetime.datetime,
    internal_events: List[dict],
) -> List[dict]:
    try:
        one_shot_source: Optional[_OneShotCalendarBusySource] = None
        if len(source_handoff) != 1:
            raise RuntimeError("Calendar busy source handoff is invalid.")
        if source_handoff[0] is None:
            _clear_calendar_busy_source_handoff(source_handoff)
            return []
        one_shot_source = _OneShotCalendarBusySource(source_handoff)
        snapshot = query_calendar_busy_snapshot(
            one_shot_source,
            instructor_id=instructor_id,
            window_start=day_start,
            window_end=day_end,
        )
        return external_busy_events_from_snapshot(
            snapshot,
            internal_events=internal_events,
        )
    finally:
        try: one_shot_source = None; _clear_calendar_busy_source_handoff(source_handoff)
        except BaseException:
            one_shot_source = None
            _clear_calendar_busy_source_handoff(source_handoff)
            raise


def external_busy_events_from_snapshot(
    snapshot: CalendarBusySnapshot,
    *,
    internal_events: List[dict],
) -> List[dict]:
    if snapshot.status == "disconnected":
        return []
    internal_coverage = _merged_event_coverage(internal_events)
    external_events: List[dict] = []
    for interval in snapshot.periods:
        cursor = interval.start
        for coverage_start, coverage_end in internal_coverage:
            if coverage_end <= cursor:
                continue
            if coverage_start >= interval.end:
                break
            if coverage_start > cursor:
                external_events.append(
                    _external_busy_event(
                        cursor,
                        min(coverage_start, interval.end),
                    )
                )
            cursor = max(cursor, coverage_end)
            if cursor >= interval.end:
                break
        if cursor < interval.end:
            external_events.append(
                _external_busy_event(cursor, interval.end)
            )
    return external_events


def _merged_event_coverage(
    events: List[dict],
) -> List[tuple[datetime.datetime, datetime.datetime]]:
    coverage: List[tuple[datetime.datetime, datetime.datetime]] = []
    for event in sorted(events, key=lambda item: item["start_time"]):
        start = event["start_time"]
        end = event["end_time"]
        if not coverage or start > coverage[-1][1]:
            coverage.append((start, end))
            continue
        previous_start, previous_end = coverage[-1]
        coverage[-1] = (previous_start, max(previous_end, end))
    return coverage


def _external_busy_event(
    start: datetime.datetime,
    end: datetime.datetime,
) -> dict:
    return {
        "start_time": start,
        "end_time": end,
        "event_type": "block",
        "blocker_kind": "external_calendar",
    }


def clip_schedule_events(
    events: List[dict],
    *,
    day_start: datetime.datetime,
    day_end: datetime.datetime,
) -> List[dict]:
    """Clip event copies to a half-open schedule window."""
    bounded_events: List[dict] = []
    for event in events:
        event_start = event["start_time"]
        event_end = event["end_time"]
        if event_end <= day_start or event_start >= day_end:
            continue

        bounded_event = event.copy()
        bounded_event["start_time"] = max(event_start, day_start)
        bounded_event["end_time"] = min(event_end, day_end)
        if bounded_event["end_time"] > bounded_event["start_time"]:
            bounded_events.append(bounded_event)
    return bounded_events


def _take_calendar_busy_source(
    source_handoff: list[Optional[CalendarBusySource]],
) -> Optional[CalendarBusySource]:
    try:
        if len(source_handoff) != 1:
            raise RuntimeError("Calendar busy source handoff is invalid.")
        return source_handoff.pop()
    finally:
        _clear_calendar_busy_source_handoff(source_handoff)


def book_swim_lesson(
    instructor_id: str,
    client_id: str,
    client_name: str,
    start_time_iso: str,
    duration_mins: int,
    location_address: str,
    idempotency_key: Optional[str] = None,
    request_id: Optional[str] = None,
    is_instructor_facility: bool = False,
    buffer_policy: Optional[BufferPolicy] = None,
    *,
    calendar_busy_source: Optional[CalendarBusySource] = None,
) -> dict:
    """Book a lesson without retaining a secret-bearing busy source."""

    try: source_handoff: list[Optional[CalendarBusySource]] = [calendar_busy_source]; calendar_busy_source = None; return _book_swim_lesson(instructor_id, client_id, client_name, start_time_iso, duration_mins, location_address, idempotency_key, request_id, is_instructor_facility, buffer_policy, source_handoff)
    finally:
        try: calendar_busy_source = None; _clear_optional_calendar_busy_source_handoff(locals().get("source_handoff"))
        except BaseException:
            calendar_busy_source = None
            _clear_optional_calendar_busy_source_handoff(
                locals().get("source_handoff")
            )
            raise


def _book_swim_lesson(
    instructor_id: str,
    client_id: str,
    client_name: str,
    start_time_iso: str,
    duration_mins: int,
    location_address: str,
    idempotency_key: Optional[str],
    request_id: Optional[str],
    is_instructor_facility: bool,
    buffer_policy: Optional[BufferPolicy],
    calendar_busy_source_handoff: list[Optional[CalendarBusySource]],
) -> dict:
    """Books an appointment after revalidating the authoritative schedule."""
    try:
        provider = load_provider_configuration(instructor_id)
    except ProviderNotFound as exc:
        raise ValueError(f"Instructor {instructor_id} not found.") from exc
    parsed_start = datetime.datetime.fromisoformat(start_time_iso)
    if parsed_start.tzinfo is None or parsed_start.utcoffset() is None:
        try:
            start_time = timezones.validate_local_datetime(parsed_start, provider.timezone)
        except timezones.InvalidTimezoneError as exc:
            raise ProviderSetupIncomplete(provider.setup_missing_fields) from exc
    else:
        start_time = parsed_start
    start_time_storage = timezones.to_utc_storage(start_time)
    end_time_storage = start_time_storage + datetime.timedelta(minutes=duration_mins)

    existing = db.get_appointment_by_idempotency_key(instructor_id, idempotency_key)
    if existing is not None:
        if (
            existing["instructor_id"] != instructor_id
            or existing["client_id"] != client_id
            or timezones.to_utc_storage(existing["start_time"]) != start_time_storage
            or timezones.to_utc_storage(existing["end_time"]) != end_time_storage
        ):
            raise db.IdempotencyKeyConflictError("The idempotency key was used for another booking.")
        return {
            "success": True,
            "appointment_id": existing["id"],
            "message": f"Successfully booked swim lesson for {client_name} on {timezones.aware_to_local(start_time, provider.timezone).strftime('%b %d, %Y at %I:%M %p')}!",
        }

    if not provider.setup_complete:
        raise ProviderSetupIncomplete(provider.setup_missing_fields)

    policy = buffer_policy or load_buffer_policy(instructor_id, operation="direct_booking")
    available_slots = find_available_swim_slots(
        instructor_id=instructor_id,
        date_str=timezones.requested_local_date(start_time, provider.timezone).isoformat(),
        client_address=location_address,
        is_instructor_facility=is_instructor_facility,
        duration_mins=duration_mins,
        buffer_policy=policy,
        calendar_busy_source=_take_calendar_busy_source(
            calendar_busy_source_handoff
        ),
    )
    if not any(timezones.to_utc_storage(slot.start_time) == start_time_storage for slot in available_slots):
        raise db.ScheduleConflictError("The requested start is no longer available.")

    if is_instructor_facility:
        if provider.service_location is None:
            raise ProviderSetupIncomplete(provider.setup_missing_fields)
        lat, lng = provider.service_location.lat, provider.service_location.lng
        location_address = provider.service_location.address
    else:
        lat, lng = routing_provider.geocode(location_address)

    app_id = db.save_appointment(
        instructor_id=instructor_id,
        client_id=client_id,
        title=f"Lesson with {client_name}",
        start_time=start_time_storage,
        end_time=end_time_storage,
        location_address=location_address,
        lat=lat,
        lng=lng,
        idempotency_key=idempotency_key,
        request_id=request_id,
    )

    return {
        "success": True,
        "appointment_id": app_id,
        "message": f"Successfully booked swim lesson for {client_name} on {timezones.aware_to_local(start_time, provider.timezone).strftime('%b %d, %Y at %I:%M %p')}!"
    }
