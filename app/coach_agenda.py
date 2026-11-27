from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import db, timezones, tools
from app.api_errors import CalendarAvailabilityUnavailable, InvalidSchedulingRequest, ResourceNotFound
from app.api_schemas import (
    CoachAgendaNextUp,
    CoachAgendaOpeningScheduleItemResponse,
    CoachAgendaStatus,
    CoachAgendaTransitionScheduleItemResponse,
    CoachCalendarBusyScheduleItemResponse,
    CoachDailyAgendaResponse,
    CoachLessonScheduleItemResponse,
)
from app.api_services import coach_schedule_item
from app.engine import BufferPolicy, InboundTransition, SchedulePosition, SwimSchedulerConstraintEngine, event_coordinates
from app.external_calendar import CalendarBusySource, ExternalCalendarUnavailable, query_calendar_busy_snapshot
from app.google_calendar_availability_fence import (
    check_current_google_calendar_availability_fence,
    claim_google_calendar_availability_fence,
    revalidate_google_calendar_availability_fence,
)
from app.models import LocationInfo, TimeSlotDTO
from app.provider_settings import (
    ProviderConfiguration,
    ProviderLocation,
    ProviderNotFound,
    ProviderService,
    load_provider_configuration,
)
from app.routing import RoutingError
from app.routing_cache import CachingRoutingProvider
from app.scheduler_settings import InvalidSchedulerPolicy, load_buffer_policy


@dataclass(frozen=True)
class AgendaDay:
    provider: ProviderConfiguration
    date: date
    timezone: str
    start: datetime
    end: datetime
    working_window: tuple[datetime, datetime] | None
    events: list[dict]


@dataclass(frozen=True)
class WorkingPlan:
    policy: BufferPolicy
    service: ProviderService
    transitions: list[CoachAgendaTransitionScheduleItemResponse]
    arrivals: dict[str, CoachAgendaNextUp]
    slots: list[TimeSlotDTO]


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

    def current_time() -> datetime:
        return timezones.ensure_aware_utc(now) if now is not None else datetime.now(timezones.UTC)

    selected_date = agenda_date or timezones.requested_local_date(current_time(), _agenda_timezone(provider))
    day = _read_agenda_day(provider, selected_date)
    status: CoachAgendaStatus = "ready" if provider.setup_complete else "setup_required"
    if "timezone" in provider.setup_missing_fields or day.end <= day.start:
        return _known_agenda(day, current_time(), status)

    routing_window = (
        tools.routing_calendar_window(*day.working_window, day.events)
        if day.working_window is not None
        else None
    )
    calendar_start = min(day.start, routing_window[0]) if routing_window is not None else day.start
    calendar_end = max(day.end, routing_window[1]) if routing_window is not None else day.end
    try:
        fence = claim_google_calendar_availability_fence(db.SessionLocal, instructor_id=instructor_id)
        snapshot = query_calendar_busy_snapshot(
            build_calendar_source(),
            instructor_id=instructor_id,
            window_start=calendar_start,
            window_end=calendar_end,
        )
    except (CalendarAvailabilityUnavailable, ExternalCalendarUnavailable):
        return _known_agenda(day, current_time(), "calendar_unavailable")

    external = tools.external_busy_events_from_snapshot(snapshot, internal_events=day.events)
    plan = None
    try:
        plan = _working_plan(day, external, current_time(), routing_window)
    except (InvalidSchedulerPolicy, RoutingError, SQLAlchemyError):
        status = "routing_unavailable"

    # Lifecycle revalidation can wait for a writer. Take the final snapshot after it.
    try:
        revalidate_google_calendar_availability_fence(db.SessionLocal, fence=fence)
    except ExternalCalendarUnavailable:
        status = "calendar_unavailable"
        plan = None

    # Compare the local inputs and credential-free calendar generation in one short read.
    try:
        with db.SessionLocal() as session:
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            latest = _read_agenda_day(
                load_provider_configuration(instructor_id, session=session),
                selected_date,
                session=session,
            )
            if latest != day:
                return _known_agenda(latest, current_time(), "schedule_changed")
            if plan is not None:
                try:
                    latest_policy = load_buffer_policy(instructor_id, operation="coach_agenda", session=session)
                except InvalidSchedulerPolicy:
                    status = "routing_unavailable"
                    plan = None
                else:
                    if latest_policy != plan.policy:
                        return _known_agenda(latest, current_time(), "schedule_changed")
            check_current_google_calendar_availability_fence(session, fence=fence)
    except (ProviderNotFound, SQLAlchemyError):
        return _known_agenda(day, current_time(), "schedule_changed")
    except ExternalCalendarUnavailable:
        return _known_agenda(latest, current_time(), "calendar_unavailable")

    instant = current_time()
    agenda = _known_agenda(latest, instant, status)
    if status == "calendar_unavailable":
        return agenda
    agenda.items.extend(
        CoachCalendarBusyScheduleItemResponse(
            id=f"agenda-calendar-busy-{index}",
            start_time=event["start_time"],
            end_time=event["end_time"],
        )
        for index, event in enumerate(tools.clip_schedule_events(external, day_start=day.start, day_end=day.end))
    )
    if plan is not None:
        for item in plan.transitions:
            start = max(item.start_time, day.start)
            end = min(item.end_time, day.end)
            if start < end:
                agenda.items.append(item.model_copy(update={"start_time": start, "end_time": end}))
        agenda.items.extend(_bookable_windows(plan.slots, plan.transitions, instant, plan.service))
        if agenda.next_up is not None:
            agenda.next_up = plan.arrivals.get(agenda.next_up.appointment_id, agenda.next_up)
    agenda.items.sort(key=lambda item: (item.start_time, item.end_time, item.id))
    return agenda


def _agenda_timezone(provider: ProviderConfiguration) -> str:
    if "timezone" in provider.setup_missing_fields:
        return timezones.DEFAULT_INSTRUCTOR_TIMEZONE
    return provider.timezone


def _read_agenda_day(
    provider: ProviderConfiguration, selected_date: date, *, session: Session | None = None
) -> AgendaDay:
    timezone_name = _agenda_timezone(provider)
    try:
        start, end = timezones.local_day_window_utc_storage(selected_date, timezone_name)
    except OverflowError:
        raise InvalidSchedulingRequest("Choose a supported calendar date.") from None
    working_window = tools.working_window_utc(provider, selected_date) if provider.setup_complete else None
    return AgendaDay(
        provider=provider,
        date=selected_date,
        timezone=timezone_name,
        start=timezones.storage_to_aware_utc(start),
        end=timezones.storage_to_aware_utc(end),
        working_window=working_window,
        events=tools.routing_events_for_date(
            provider.id, selected_date, timezone_name,
            include_adjacent_days=working_window is not None,
            session=session,
        ),
    )


def _known_agenda(day: AgendaDay, now: datetime, status: CoachAgendaStatus) -> CoachDailyAgendaResponse:
    service_location = day.provider.service_location
    items = [
        coach_schedule_item(event, service_address=service_location.address if service_location else None)
        for event in tools.clip_schedule_events(day.events, day_start=day.start, day_end=day.end)
    ]
    featured = next((
        item for item in items
        if isinstance(item, CoachLessonScheduleItemResponse) and item.end_time > now
    ), None)
    return CoachDailyAgendaResponse(
        instructor_id=day.provider.id,
        date=day.date,
        today=timezones.requested_local_date(now, day.timezone),
        timezone=day.timezone,
        status=status,
        items=items,
        next_up=CoachAgendaNextUp(appointment_id=featured.id) if featured is not None else None,
    )


def _working_plan(
    day: AgendaDay,
    external: list[dict],
    now: datetime,
    routing_window: tuple[datetime, datetime] | None,
) -> WorkingPlan | None:
    provider = day.provider
    service = provider.service
    if day.working_window is None or routing_window is None or provider.base_location is None or service is None:
        return None
    work_start, work_end = day.working_window
    busy_start, busy_end = routing_window
    base = _scheduler_location(provider.base_location, is_facility=False)
    target = _scheduler_location(service.location, is_facility=True)
    bounded_busy = tools.clip_schedule_events(external, day_start=busy_start, day_end=busy_end)
    policy = load_buffer_policy(provider.id, operation="coach_agenda")
    engine = SwimSchedulerConstraintEngine(tools.routing_provider, policy, timezone_name=day.timezone)
    position, working_events = tools.working_schedule_context(
        work_start, base, [*day.events, *bounded_busy], local_day_start=day.start, engine=engine,
    )
    transition_position, transition_events = tools.working_schedule_context(
        work_start, base, [*day.events, *external], local_day_start=day.start, engine=engine,
    )
    calendar_end = max(day.end, busy_end)
    transitions, arrivals = _planned_transitions(
        engine,
        transition_events,
        original_starts={
            event["id"]: event["start_time"] for event in day.events if event["start_time"] <= calendar_end
        },
        position=transition_position,
    )
    slots = []
    if work_end > now:
        slots = engine.find_available_slots_for_day(
            day_start=work_start,
            day_end=work_end,
            instructor_main_location=base,
            existing_events=working_events,
            target_location=target,
            duration_mins=service.default_duration_mins,
            initial_position=position,
        )
    return WorkingPlan(policy, service, transitions, arrivals, slots)


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
    position: SchedulePosition,
) -> tuple[list[CoachAgendaTransitionScheduleItemResponse], dict[str, CoachAgendaNextUp]]:
    rows = []
    arrivals = {}
    for event in events:
        coordinates = event_coordinates(event)
        appointment_id = event.get("id")
        start = event["start_time"]
        if coordinates is not None and appointment_id is not None and original_starts.get(appointment_id) == start:
            departure = _verified_departure(engine, position, coordinates, start)
            if departure is not None:
                leave_by, transition = departure
                arrives_at = leave_by + timedelta(minutes=transition.travel_mins)
                ready_at = arrives_at + transition.buffer
                arrivals[appointment_id] = CoachAgendaNextUp(
                    appointment_id=appointment_id,
                    leave_by=leave_by,
                    travel_time_mins=transition.travel_mins,
                    buffer_mins=transition.buffer_mins,
                )
                if leave_by < arrives_at:
                    rows.append(CoachAgendaTransitionScheduleItemResponse(
                        id=f"agenda-travel-{appointment_id}",
                        kind="travel",
                        title="Travel to scheduled location",
                        start_time=leave_by,
                        end_time=arrives_at,
                        travel_time_mins=transition.travel_mins,
                    ))
                if arrives_at < ready_at:
                    rows.append(CoachAgendaTransitionScheduleItemResponse(
                        id=f"agenda-buffer-{appointment_id}",
                        kind="buffer",
                        title="Transition buffer",
                        start_time=arrives_at,
                        end_time=ready_at,
                        buffer_mins=transition.buffer_mins,
                    ))
        position = position.after_event(event)
    return rows, arrivals


def _verified_departure(
    engine: SwimSchedulerConstraintEngine,
    position: SchedulePosition,
    coordinates: tuple[float, float],
    appointment_start: datetime,
) -> tuple[datetime, InboundTransition] | None:
    route = engine.routing
    exact_estimate = route.get_distance_and_travel_time_at_exact_departure if isinstance(route, CachingRoutingProvider) else None
    earliest = engine.inbound_transition(position, coordinates, route_estimator=exact_estimate)
    departure = appointment_start - earliest.duration
    # Traffic can differ between the first free instant and a later departure.
    # Try at most two later quotes, then keep the earliest verified departure.
    for _ in range(2):
        if departure <= position.free_at:
            break
        estimate = engine.inbound_transition(
            position, coordinates, departure_time=departure, route_estimator=exact_estimate
        )
        if departure + estimate.duration <= appointment_start:
            return departure, estimate
        departure = appointment_start - estimate.duration
    if position.free_at + earliest.duration <= appointment_start:
        return position.free_at, earliest
    return None


def _bookable_windows(
    slots: list[TimeSlotDTO],
    transitions: list[CoachAgendaTransitionScheduleItemResponse],
    now: datetime,
    service: ProviderService,
) -> list[CoachAgendaOpeningScheduleItemResponse]:
    windows: list[list[TimeSlotDTO]] = []
    for slot in slots:
        if slot.start_time < now or any(
            item.start_time < slot.end_time and item.end_time > slot.start_time
            for item in transitions
        ):
            continue
        if windows and slot.start_time <= windows[-1][-1].end_time:
            windows[-1].append(slot)
        else:
            windows.append([slot])
    return [
        CoachAgendaOpeningScheduleItemResponse(
            id=f"agenda-open-{index}",
            title="Bookable at your service location",
            start_time=window[0].start_time,
            end_time=window[-1].end_time,
            start_times=[slot.start_time for slot in window],
            note=f"{service.default_duration_mins} min appointments at {service.location.address}",
        )
        for index, window in enumerate(windows)
    ]
