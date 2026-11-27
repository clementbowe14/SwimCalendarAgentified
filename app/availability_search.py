"""Bounded, provider-local search using the existing one-day scheduler."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app import db, timezones, tools
from app.api_errors import InvalidSchedulingRequest
from app.api_schemas import AvailabilityEmptyReason, OneTimeAvailabilityCriteria, PILOT_AVAILABILITY_DAYS, PROVIDER_WEEKDAYS
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
from app.scheduler_settings import load_buffer_policy


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
    try:
        provider = require_provider_setup(instructor_id)
        today = timezones.requested_local_date(now or datetime.now(timezones.UTC), provider.timezone)
        if criteria.start_date < today or (criteria.end_date - today).days >= PILOT_AVAILABILITY_DAYS:
            raise InvalidSchedulingRequest("Choose dates within the next 14 days, including today.")

        location = tools.resolve_availability_location(provider, client_address, is_instructor_facility)
        days, empty_reason = _search_days(provider, criteria)
        if not days:
            return AvailabilityRange(provider.timezone, location, [], empty_reason)

        policy = load_buffer_policy(instructor_id, operation="availability")
        fence = None
        busy_periods: list[CalendarBusyInterval] = []
        if calendar_busy_source is not None:
            fence = claim_google_calendar_availability_fence(db.SessionLocal, instructor_id=instructor_id)
            busy_periods = _read_busy_periods(calendar_busy_source, instructor_id, days)
    finally:
        calendar_busy_source = None

    earliest = timezones.ensure_aware_utc(now or datetime.now(timezones.UTC))
    slots: list[TimeSlotDTO] = []
    for day in days:
        calendar_start, calendar_end = day.calendar_window
        snapshot = CalendarBusySnapshot("verified", normalize_busy_intervals(
            (
                period for period in busy_periods
                if period.start < calendar_end and period.end > calendar_start
            ),
            window_start=calendar_start, window_end=calendar_end,
        ))
        external_events = tools.external_busy_events_from_snapshot(snapshot, internal_events=day.events)
        candidates = tools.slots_for_working_day(
            provider, day.date, day.working_window, location, [*day.events, *external_events],
            policy=policy, duration_mins=criteria.duration_mins,
        )
        for slot in sorted(candidates, key=lambda candidate: timezones.ensure_aware_utc(candidate.start_time)):
            if timezones.ensure_aware_utc(slot.start_time) >= earliest and _fits_client_windows(slot, day.client_windows):
                slots.append(slot)
                if len(slots) == criteria.limit:
                    break
        if len(slots) == criteria.limit:
            break

    if fence is not None:
        revalidate_google_calendar_availability_fence(db.SessionLocal, fence=fence)
    return AvailabilityRange(provider.timezone, location, slots, None if slots else "no_safe_slots")


def _search_days(
    provider: ProviderConfiguration,
    criteria: OneTimeAvailabilityCriteria,
) -> tuple[list[_SearchDay], AvailabilityEmptyReason]:
    days: list[_SearchDay] = []
    has_working_day = False
    with db.SessionLocal() as session:
        for offset in range((criteria.end_date - criteria.start_date).days + 1):
            day = criteria.start_date + timedelta(days=offset)
            working = tools.working_window_utc(provider, day)
            if working is None:
                continue
            has_working_day = True
            windows = _windows_for_weekday(criteria, PROVIDER_WEEKDAYS[day.weekday()])
            if criteria.windows and not windows:
                continue
            events = tools.routing_events_for_date(provider.id, day, provider.timezone, session=session)
            days.append(_SearchDay(day, working, tools.routing_calendar_window(*working, events), events, windows))
    return days, "outside_client_windows" if has_working_day else "no_working_hours"


def _read_busy_periods(
    source: CalendarBusySource,
    instructor_id: str,
    days: list[_SearchDay],
) -> list[CalendarBusyInterval]:
    try:
        start = min(day.calendar_window[0] for day in days)
        end = max(day.calendar_window[1] for day in days)
        periods: list[CalendarBusyInterval] = []
        while start < end:
            # Adjacent routing anchors can extend beyond the 14 local search days.
            chunk_end = min(end, start + MAX_BUSY_QUERY_WINDOW)
            snapshot = query_calendar_busy_snapshot(
                source, instructor_id=instructor_id, window_start=start, window_end=chunk_end,
            )
            periods.extend(snapshot.periods)
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


def _fits_client_windows(slot: TimeSlotDTO, windows: list[tuple[time, time]]) -> bool:
    if not windows:
        return True
    first = slot.start_time
    end_utc = timezones.ensure_aware_utc(slot.end_time)
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
