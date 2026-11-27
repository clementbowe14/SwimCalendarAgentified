from collections.abc import Callable
from datetime import date, datetime

from app import db, timezones, tools
from app.api_errors import CalendarAvailabilityUnavailable, InvalidSchedulingRequest, ResourceNotFound
from app.api_schemas import (
    CoachAgendaNextUp,
    CoachCalendarBusyScheduleItemResponse,
    CoachDailyAgendaResponse,
    CoachLessonScheduleItemResponse,
    CoachUtilityScheduleItemResponse,
)
from app.api_services import coach_schedule_item
from app.engine import SchedulePosition, SwimSchedulerConstraintEngine, event_coordinates
from app.external_calendar import CalendarBusySource, ExternalCalendarUnavailable, query_calendar_busy_snapshot
from app.google_calendar_availability_fence import (
    claim_google_calendar_availability_fence,
    revalidate_google_calendar_availability_fence,
)
from app.models import LocationInfo, TimeSlotDTO
from app.provider_settings import ProviderLocation, ProviderNotFound, load_provider_configuration
from app.routing import RoutingError
from app.scheduler_settings import load_buffer_policy


def get_coach_agenda(
    instructor_id: str,
    agenda_date: date | None,
    *,
    build_calendar_source: Callable[[], CalendarBusySource],
    now: datetime | None = None,
) -> CoachDailyAgendaResponse:
    try:
        provider = load_provider_configuration(instructor_id)
    except ProviderNotFound:
        raise ResourceNotFound("The instructor could not be found.") from None

    instant = timezones.ensure_aware_utc(now) if now is not None else datetime.now(timezones.UTC)
    has_timezone = "timezone" not in provider.setup_missing_fields
    timezone_name = provider.timezone if has_timezone else timezones.DEFAULT_INSTRUCTOR_TIMEZONE
    today = timezones.requested_local_date(instant, timezone_name)
    selected_date = agenda_date or today
    try:
        storage_start, storage_end = timezones.local_day_window_utc_storage(selected_date, timezone_name)
    except OverflowError:
        raise InvalidSchedulingRequest("Choose a supported calendar date.") from None
    day_start = timezones.storage_to_aware_utc(storage_start)
    day_end = timezones.storage_to_aware_utc(storage_end)
    internal = db.get_utc_appointment_events_for_date(
        instructor_id, selected_date.isoformat(), timezone_name=timezone_name
    )
    service = provider.service
    service_address = provider.service_location.address if provider.service_location else None
    items = [
        coach_schedule_item(event, service_address=service_address)
        for event in tools.clip_schedule_events(internal, day_start=day_start, day_end=day_end)
    ]
    featured = next((
        item for item in items
        if isinstance(item, CoachLessonScheduleItemResponse) and item.end_time > instant
    ), None)
    agenda = CoachDailyAgendaResponse(
        instructor_id=instructor_id,
        date=selected_date,
        today=today,
        timezone=timezone_name,
        status="ready" if provider.setup_complete else "setup_required",
        items=items,
        next_up=CoachAgendaNextUp(appointment_id=featured.id) if featured is not None else None,
    )
    if not has_timezone or day_end <= day_start:
        return agenda

    try:
        fence = claim_google_calendar_availability_fence(db.SessionLocal, instructor_id=instructor_id)
        snapshot = query_calendar_busy_snapshot(
            build_calendar_source(),
            instructor_id=instructor_id,
            window_start=day_start,
            window_end=day_end,
        )
    except (CalendarAvailabilityUnavailable, ExternalCalendarUnavailable):
        agenda.status = "calendar_unavailable"
        return agenda

    external = tools.external_busy_events_from_snapshot(snapshot, internal_events=internal)
    planned_items = list(agenda.items)
    planned_next_up = agenda.next_up
    planned_items.extend(
        CoachCalendarBusyScheduleItemResponse(
            id=f"agenda-calendar-busy-{index}",
            start_time=event["start_time"],
            end_time=event["end_time"],
        )
        for index, event in enumerate(external)
    )

    if provider.setup_complete and provider.base_location is not None and service is not None:
        window = tools.working_window_utc(provider, selected_date)
        if window is not None:
            work_start, work_end = window
            working_events = sorted(
                tools.clip_schedule_events([*internal, *external], day_start=work_start, day_end=work_end),
                key=lambda event: event["start_time"],
            )
            base = _scheduler_location(provider.base_location, is_facility=False)
            target = _scheduler_location(service.location, is_facility=True)
            engine = SwimSchedulerConstraintEngine(
                tools.routing_provider,
                load_buffer_policy(instructor_id, operation="coach_agenda"),
                timezone_name=timezone_name,
            )
            try:
                transitions, arrivals = _planned_transitions(
                    engine,
                    working_events,
                    original_starts={event["id"]: event["start_time"] for event in internal},
                    shift_start=work_start,
                    base=base,
                )
                openings = []
                if work_end > instant:
                    slots = engine.find_available_slots_for_day(
                        day_start=work_start,
                        day_end=work_end,
                        instructor_main_location=base,
                        existing_events=working_events,
                        target_location=target,
                        duration_mins=service.default_duration_mins,
                    )
                    openings = _bookable_windows(
                        slots, transitions, instant, service.default_duration_mins, target.address
                    )
            except RoutingError:
                agenda.status = "routing_unavailable"
            else:
                planned_items.extend(transitions)
                planned_items.extend(openings)
                if featured is not None and featured.id in arrivals:
                    planned_next_up = arrivals[featured.id]

    try:
        revalidate_google_calendar_availability_fence(db.SessionLocal, fence=fence)
    except ExternalCalendarUnavailable:
        agenda.status = "calendar_unavailable"
        return agenda
    agenda.items = sorted(planned_items, key=lambda item: (item.start_time, item.end_time, item.id))
    agenda.next_up = planned_next_up
    return agenda


def _scheduler_location(location: ProviderLocation, *, is_facility: bool) -> LocationInfo:
    return LocationInfo(
        address=location.address,
        lat=location.lat,
        lng=location.lng,
        is_instructor_facility=is_facility,
    )


def _planned_transitions(
    engine: SwimSchedulerConstraintEngine,
    events: list[dict],
    *,
    original_starts: dict[str, datetime],
    shift_start: datetime,
    base: LocationInfo,
) -> tuple[list[CoachUtilityScheduleItemResponse], dict[str, CoachAgendaNextUp]]:
    rows = []
    arrivals = {}
    position = SchedulePosition(shift_start, (base.lat, base.lng))
    for event in events:
        coordinates = event_coordinates(event)
        appointment_id = event.get("id")
        start = event["start_time"]
        if coordinates is not None and original_starts.get(appointment_id) == start:
            transition = engine.inbound_transition(position, coordinates)
            leave_by = start - transition.duration
            if leave_by >= position.free_at:
                ready_at = start - transition.buffer
                arrivals[appointment_id] = CoachAgendaNextUp(
                    appointment_id=appointment_id,
                    leave_by=leave_by,
                    travel_time_mins=transition.travel_mins,
                    buffer_mins=transition.buffer_mins,
                )
                if leave_by < ready_at:
                    rows.append(CoachUtilityScheduleItemResponse(
                        id=f"agenda-travel-{appointment_id}",
                        kind="travel",
                        title="Travel to scheduled location",
                        start_time=leave_by,
                        end_time=ready_at,
                        travel_time_mins=transition.travel_mins,
                    ))
                if ready_at < start:
                    rows.append(CoachUtilityScheduleItemResponse(
                        id=f"agenda-buffer-{appointment_id}",
                        kind="buffer",
                        title="Transition buffer",
                        start_time=ready_at,
                        end_time=start,
                        buffer_mins=transition.buffer_mins,
                    ))
        position = position.after_event(event)
    return rows, arrivals


def _bookable_windows(
    slots: list[TimeSlotDTO],
    transitions: list[CoachUtilityScheduleItemResponse],
    now: datetime,
    duration_mins: int,
    service_address: str,
) -> list[CoachUtilityScheduleItemResponse]:
    windows: list[tuple[datetime, datetime]] = []
    for slot in slots:
        if slot.start_time < now or any(
            item.start_time < slot.end_time and item.end_time > slot.start_time
            for item in transitions
        ):
            continue
        if windows and slot.start_time <= windows[-1][1]:
            start, end = windows[-1]
            windows[-1] = start, max(end, slot.end_time)
        else:
            windows.append((slot.start_time, slot.end_time))
    return [
        CoachUtilityScheduleItemResponse(
            id=f"agenda-open-{index}",
            kind="open_gap",
            title="Bookable at your service location",
            start_time=start,
            end_time=end,
            note=f"{duration_mins} min appointments at {service_address}",
        )
        for index, (start, end) in enumerate(windows)
    ]
