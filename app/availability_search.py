"""Bounded, provider-local search using the existing one-day scheduler."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from time import monotonic

from sqlalchemy.orm import Session

from app import db, timezones, tools
from app.api_errors import AvailabilitySearchBudgetExceeded, InvalidSchedulingRequest
from app.api_schemas import (
    PILOT_AVAILABILITY_DAYS,
    PROVIDER_WEEKDAYS,
    AvailabilityEmptyReason,
    OneTimeAvailabilityCriteria,
)
from app.engine import BufferPolicy
from app.external_calendar import (
    MAX_BUSY_QUERY_WINDOW,
    CalendarBusyInterval,
    CalendarBusySnapshot,
    CalendarBusySource,
    normalize_busy_intervals,
    query_calendar_busy_snapshot,
)
from app.google_calendar_availability_fence import (
    claim_google_calendar_availability_fence,
    revalidate_google_calendar_availability_fence,
)
from app.models import LocationInfo, TimeSlotDTO
from app.provider_settings import ProviderConfiguration, require_provider_setup
from app.routing import BaseRoutingProvider, RouteEstimate
from app.scheduler_settings import load_buffer_policy


_SEARCH_SECONDS = 15
_MAX_ROUTING_CALLS = 96
_MAX_SEARCH_EVENTS = 768


class _BoundedRouting(BaseRoutingProvider):
    def __init__(self, provider: BaseRoutingProvider):
        self.provider = provider
        self.deadline = monotonic() + _SEARCH_SECONDS
        self.remaining = _MAX_ROUTING_CALLS

    def check_deadline(self) -> None:
        if monotonic() >= self.deadline:
            raise AvailabilitySearchBudgetExceeded()

    def _start_call(self) -> None:
        self.check_deadline()
        if self.remaining == 0:
            raise AvailabilitySearchBudgetExceeded()
        self.remaining -= 1

    def geocode(self, address: str) -> tuple[float, float]:
        self._start_call()
        coords = self.provider.geocode(address)
        self.check_deadline()
        return coords

    def estimate_route(
        self,
        origin_coords: tuple[float, float],
        dest_coords: tuple[float, float],
        departure_time: datetime | None = None,
        traffic_mode: str = "none",
    ) -> RouteEstimate:
        self._start_call()
        estimate = self.provider.estimate_route(
            origin_coords, dest_coords, departure_time=departure_time, traffic_mode=traffic_mode,
        )
        self.check_deadline()
        return estimate


@dataclass(frozen=True)
class AvailabilityRange:
    timezone: str
    location: LocationInfo
    slots: list[TimeSlotDTO]
    empty_reason: AvailabilityEmptyReason | None


@dataclass(frozen=True)
class _SearchDay:
    date: date
    working_window: tuple[datetime, datetime]
    calendar_window: tuple[datetime, datetime]
    events: list[dict]
    client_windows: list[tuple[time, time]]


def search_one_time_availability(
    instructor_id: str,
    criteria: OneTimeAvailabilityCriteria,
    *,
    client_address: str,
    is_instructor_facility: bool,
    calendar_busy_source: CalendarBusySource | None,
    now: datetime | None = None,
) -> AvailabilityRange:
    routing = _BoundedRouting(tools.routing_provider)
    try:
        fence = None
        if calendar_busy_source is not None:
            fence = claim_google_calendar_availability_fence(db.SessionLocal, instructor_id=instructor_id)
        routing.check_deadline()

        with db.SessionLocal() as session:
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            provider = require_provider_setup(instructor_id, session=session)
            today = timezones.requested_local_date(now or datetime.now(timezones.UTC), provider.timezone)
            if criteria.start_date < today or (criteria.end_date - today).days >= PILOT_AVAILABILITY_DAYS:
                raise InvalidSchedulingRequest("Choose dates within the next 14 days, including today.")
            days, empty_reason = _search_days(provider, criteria, routing, session)
            policy = load_buffer_policy(instructor_id, operation="availability", session=session) if days else None

        location = tools.resolve_availability_location(
            provider, client_address, is_instructor_facility, routing=routing,
        )
        busy_periods: list[CalendarBusyInterval] = []
        if days and calendar_busy_source is not None:
            busy_periods = _read_busy_periods(calendar_busy_source, instructor_id, days, routing)
    finally:
        calendar_busy_source = None

    slots: list[TimeSlotDTO] = []
    if policy is not None:
        slots = _find_range_slots(provider, criteria, location, days, busy_periods, routing, policy, now=now)

    routing.check_deadline()
    if fence is not None:
        revalidate_google_calendar_availability_fence(db.SessionLocal, fence=fence)
    routing.check_deadline()
    return AvailabilityRange(provider.timezone, location, slots, None if slots else empty_reason)


def _find_range_slots(
    provider: ProviderConfiguration,
    criteria: OneTimeAvailabilityCriteria,
    location: LocationInfo,
    days: list[_SearchDay],
    busy_periods: list[CalendarBusyInterval],
    routing: _BoundedRouting,
    policy: BufferPolicy,
    *,
    now: datetime | None,
) -> list[TimeSlotDTO]:
    earliest = timezones.ensure_aware_utc(now or datetime.now(timezones.UTC))
    zone = timezones.get_zone(provider.timezone)
    slots: list[TimeSlotDTO] = []
    event_count = sum(len(day.events) for day in days)
    for day in days:
        routing.check_deadline()
        calendar_start, calendar_end = day.calendar_window
        snapshot = CalendarBusySnapshot("verified", normalize_busy_intervals(
            (
                period for period in busy_periods
                if period.start < calendar_end and period.end > calendar_start
            ),
            window_start=calendar_start, window_end=calendar_end,
        ))
        external_events = tools.external_busy_events_from_snapshot(snapshot, internal_events=day.events)
        event_count += len(external_events)
        if event_count > _MAX_SEARCH_EVENTS:
            raise AvailabilitySearchBudgetExceeded()
        slots.extend(tools.slots_for_working_day(
            provider, day.date, day.working_window, location, [*day.events, *external_events],
            policy=policy, duration_mins=criteria.duration_mins,
            routing=routing,
            slot_filter=lambda start, end: start >= earliest and _fits_client_windows(
                start.astimezone(zone), end, day.client_windows
            ),
            slot_limit=criteria.limit - len(slots),
        ))
        if len(slots) == criteria.limit:
            break
    return slots


def _search_days(
    provider: ProviderConfiguration,
    criteria: OneTimeAvailabilityCriteria,
    routing: _BoundedRouting,
    session: Session,
) -> tuple[list[_SearchDay], AvailabilityEmptyReason]:
    days: list[_SearchDay] = []
    has_working_day = False
    remaining_events = _MAX_SEARCH_EVENTS
    zone = timezones.get_zone(provider.timezone)
    for offset in range((criteria.end_date - criteria.start_date).days + 1):
        routing.check_deadline()
        day = criteria.start_date + timedelta(days=offset)
        working = tools.working_window_utc(provider, day)
        if working is None:
            continue
        has_working_day = True
        work_start, work_end = (edge.astimezone(zone).time() for edge in working)
        windows = [
            (start, end)
            for start, end in _windows_for_weekday(criteria, PROVIDER_WEEKDAYS[day.weekday()])
            if start < work_end and end > work_start
        ]
        if criteria.windows and not windows:
            continue
        events = tools.routing_events_for_date(
            provider.id, day, provider.timezone, session=session, limit=remaining_events + 1,
        )
        remaining_events -= len(events)
        if remaining_events < 0:
            raise AvailabilitySearchBudgetExceeded()
        days.append(_SearchDay(day, working, tools.routing_calendar_window(*working, events), events, windows))
    if days:
        return days, "no_safe_slots"
    return [], "outside_client_windows" if has_working_day else "no_working_hours"


def _read_busy_periods(
    source: CalendarBusySource,
    instructor_id: str,
    days: list[_SearchDay],
    routing: _BoundedRouting,
) -> list[CalendarBusyInterval]:
    try:
        start = min(day.calendar_window[0] for day in days)
        end = max(day.calendar_window[1] for day in days)
        periods: list[CalendarBusyInterval] = []
        while start < end:
            routing.check_deadline()
            # Adjacent routing anchors can extend beyond the 14 local search days.
            chunk_end = min(end, start + MAX_BUSY_QUERY_WINDOW)
            snapshot = query_calendar_busy_snapshot(
                source, instructor_id=instructor_id, window_start=start, window_end=chunk_end,
            )
            routing.check_deadline()
            periods.extend(snapshot.periods)
            if len(periods) > _MAX_SEARCH_EVENTS:
                raise AvailabilitySearchBudgetExceeded()
            start = chunk_end
        return periods
    finally:
        source = None


def _windows_for_weekday(
    criteria: OneTimeAvailabilityCriteria,
    weekday: str,
) -> list[tuple[time, time]]:
    windows = sorted(
        (time.fromisoformat(window.start_time), time.fromisoformat(window.end_time))
        for window in criteria.windows
        if weekday in window.days
    )
    merged: list[tuple[time, time]] = []
    for start, end in windows:
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def _fits_client_windows(first: datetime, end_utc: datetime, windows: list[tuple[time, time]]) -> bool:
    if not windows:
        return True
    last = (end_utc - timedelta(microseconds=1)).astimezone(first.tzinfo)
    if first.date() != last.date():
        return False
    for start, end in windows:
        if not (start <= first.time() < end and start <= last.time() < end):
            continue
        if first.utcoffset() == last.utcoffset():
            return True
        # Across a clock change, both endpoints can fit while part of the lesson falls outside.
        first_utc = timezones.ensure_aware_utc(first)
        minutes = int((end_utc - first_utc).total_seconds() // 60)
        if all(
            start <= (first_utc + timedelta(minutes=minute)).astimezone(first.tzinfo).time() < end
            for minute in range(1, minutes)
        ):
            return True
    return False
