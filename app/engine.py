import datetime
import math
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from app import timezones
from app.models import LocationInfo, TimeSlotDTO
from app.routing import BaseRoutingProvider


SLOT_INCREMENT_MINUTES = 15


@dataclass(frozen=True)
class DistanceBufferTier:
    min_miles: float
    max_miles: Optional[float]
    buffer_mins: int


@dataclass(frozen=True)
class BufferPolicy:
    distance_buffers_enabled: bool
    post_break_recovery_mins: int
    distance_buffer_tiers: tuple[DistanceBufferTier, ...]

    def distance_buffer_minutes(self, distance_miles: float) -> int:
        if not math.isfinite(distance_miles) or distance_miles < 0:
            raise ValueError("Distance must be a finite non-negative number.")
        if not self.distance_buffers_enabled:
            return 0
        normalized = round(distance_miles, 2)
        for tier in self.distance_buffer_tiers:
            if normalized >= tier.min_miles and (tier.max_miles is None or normalized <= tier.max_miles):
                return tier.buffer_mins
        raise ValueError("Distance does not match a configured buffer tier.")

    def inbound_buffer_minutes(
        self,
        distance_miles: float,
        *,
        is_after_break: bool,
        is_shift_start: bool,
    ) -> int:
        if is_shift_start:
            return 0
        if is_after_break:
            return self.post_break_recovery_mins
        return self.distance_buffer_minutes(distance_miles)

    def ceil_datetime(
        self,
        value: datetime.datetime,
        timezone_name: Optional[str] = None,
        *,
        allow_repeated_hour: bool = False,
    ) -> datetime.datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return _ceil_naive_label(value, SLOT_INCREMENT_MINUTES)

        zone = timezones.get_zone(timezone_name)
        local = value.astimezone(zone)
        local_label = local.replace(tzinfo=None)
        label = _ceil_naive_label(local_label, SLOT_INCREMENT_MINUTES)
        if allow_repeated_hour:
            # Slot evaluation follows real instants through a repeated local hour.
            candidate = timezones.ensure_aware_utc(value) + (label - local_label)
            rounded_label = candidate.astimezone(zone).replace(tzinfo=None)
            if _ceil_naive_label(rounded_label, SLOT_INCREMENT_MINUTES) == rounded_label:
                return candidate.astimezone(value.tzinfo)
        while True:
            resolved = _resolve_local_label(label, zone, local.fold)
            if resolved is not None:
                return resolved.astimezone(value.tzinfo)
            label += datetime.timedelta(minutes=SLOT_INCREMENT_MINUTES)


def _ceil_naive_label(value: datetime.datetime, increment_mins: int) -> datetime.datetime:
    midnight = value.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_seconds = (value - midnight).total_seconds()
    increment_seconds = increment_mins * 60
    boundary_seconds = math.ceil(elapsed_seconds / increment_seconds) * increment_seconds
    return midnight + datetime.timedelta(seconds=boundary_seconds)


def _resolve_local_label(
    label: datetime.datetime,
    zone: datetime.tzinfo,
    preferred_fold: int,
) -> Optional[datetime.datetime]:
    candidates = [
        label.replace(tzinfo=zone, fold=preferred_fold),
        label.replace(tzinfo=zone, fold=1 - preferred_fold),
    ]
    for candidate in candidates:
        roundtrip = candidate.astimezone(timezones.UTC).astimezone(zone)
        if roundtrip.replace(tzinfo=None) == label and roundtrip.fold == candidate.fold:
            return candidate
    return None


@dataclass(frozen=True)
class SchedulePosition:
    free_at: datetime.datetime
    coordinates: Tuple[float, float]
    previous_break_end: Optional[datetime.datetime] = None
    is_shift_start: bool = True

    def after_event(self, event: dict) -> "SchedulePosition":
        end = event["end_time"]
        blocker_kind = event.get("blocker_kind")
        if blocker_kind == "external_calendar":
            break_end = self.previous_break_end
        else:
            is_break = (
                blocker_kind == "break"
                if event.get("event_type") == "block" or blocker_kind is not None
                else bool(event.get("is_break", False))
            )
            break_end = end if is_break else None
        return SchedulePosition(
            free_at=end,
            coordinates=event_coordinates(event) or self.coordinates,
            previous_break_end=break_end,
            is_shift_start=False,
        )


@dataclass(frozen=True)
class InboundTransition:
    distance_miles: float
    travel_mins: int
    buffer: datetime.timedelta

    @property
    def duration(self) -> datetime.timedelta:
        return datetime.timedelta(minutes=self.travel_mins) + self.buffer

    @property
    def buffer_mins(self) -> int:
        return math.ceil(self.buffer.total_seconds() / 60)


class SwimSchedulerConstraintEngine:
    """Deterministic scheduling constraint solver."""

    def __init__(
        self,
        routing_provider: BaseRoutingProvider,
        buffer_policy: BufferPolicy,
        timezone_name: Optional[str] = None,
    ):
        self.routing = routing_provider
        self.buffer_policy = buffer_policy
        self.timezone_name = timezone_name

    def get_inter_lesson_buffer_minutes(self, distance_miles: float) -> int:
        return self.buffer_policy.distance_buffer_minutes(distance_miles)

    def apply_15min_ceiling(self, dt: datetime.datetime) -> datetime.datetime:
        return self.buffer_policy.ceil_datetime(dt, self.timezone_name)

    def compute_earliest_start_time(
        self,
        prev_end_time: datetime.datetime,
        travel_time_mins: int,
        distance_miles: float,
        is_after_break: bool = False,
    ) -> datetime.datetime:
        buffer_mins = self.buffer_policy.inbound_buffer_minutes(
            distance_miles,
            is_after_break=is_after_break,
            is_shift_start=False,
        )
        raw_earliest = prev_end_time + datetime.timedelta(minutes=travel_time_mins + buffer_mins)
        return self.buffer_policy.ceil_datetime(raw_earliest, self.timezone_name)

    def find_available_slots_for_day(
        self,
        day_start: datetime.datetime,
        day_end: datetime.datetime,
        instructor_main_location: LocationInfo,
        existing_events: List[dict],
        target_location: LocationInfo,
        duration_mins: int = 60,
        *,
        initial_position: SchedulePosition | None = None,
        next_shift_start: datetime.datetime | None = None,
        slot_filter: Callable[[datetime.datetime, datetime.datetime], bool] | None = None,
        slot_limit: int | None = None,
    ) -> List[TimeSlotDTO]:
        valid_slots: List[TimeSlotDTO] = []
        base_coordinates = (instructor_main_location.lat, instructor_main_location.lng)
        target_coords = (target_location.lat, target_location.lng)
        lesson_delta = datetime.timedelta(minutes=duration_mins)
        sorted_events = sorted(existing_events, key=lambda item: item["start_time"])
        position = initial_position or SchedulePosition(
            day_start, base_coordinates
        )

        for index, event in enumerate(sorted_events):
            if slot_limit is not None and len(valid_slots) == slot_limit:
                return valid_slots
            next_coords, outbound_departure, outbound_deadline = _outbound_route_anchor(
                sorted_events,
                index,
            )
            valid_slots.extend(
                self._evaluate_gap(
                    position=position,
                    next_start_time=event["start_time"],
                    next_coords=next_coords,
                    outbound_departure_time=outbound_departure,
                    outbound_deadline=outbound_deadline,
                    target_coords=target_coords,
                    base_coordinates=base_coordinates,
                    next_shift_start=next_shift_start,
                    day_start=day_start,
                    day_end=day_end,
                    lesson_delta=lesson_delta,
                    slot_filter=slot_filter,
                    slot_limit=None if slot_limit is None else slot_limit - len(valid_slots),
                )
            )
            position = position.after_event(event)

        valid_slots.extend(
            self._evaluate_gap(
                position=position,
                next_start_time=day_end,
                next_coords=None,
                outbound_departure_time=None,
                outbound_deadline=None,
                target_coords=target_coords,
                base_coordinates=base_coordinates,
                next_shift_start=next_shift_start,
                day_start=day_start,
                day_end=day_end,
                lesson_delta=lesson_delta,
                slot_filter=slot_filter,
                slot_limit=None if slot_limit is None else slot_limit - len(valid_slots),
            )
        )
        return valid_slots

    def inbound_transition(
        self,
        position: SchedulePosition,
        target_coords: Tuple[float, float],
        *,
        departure_time: datetime.datetime | None = None,
        route_estimator: Callable[..., Tuple[float, int]] | None = None,
    ) -> InboundTransition:
        estimate = route_estimator or self.routing.get_distance_and_travel_time
        distance, travel = estimate(
            position.coordinates,
            target_coords,
            departure_time=departure_time if departure_time is not None else position.free_at,
        )
        buffer = datetime.timedelta(minutes=self.buffer_policy.inbound_buffer_minutes(
            distance,
            is_after_break=position.free_at == position.previous_break_end,
            is_shift_start=position.is_shift_start,
        ))
        if position.previous_break_end is not None:
            recovery_end = position.previous_break_end + datetime.timedelta(
                minutes=self.buffer_policy.post_break_recovery_mins
            )
            # Keep any recovery time that remains after an external busy period.
            buffer = max(buffer, recovery_end - position.free_at)
        return InboundTransition(distance, travel, buffer)

    def first_matching_start(
        self,
        begin: datetime.datetime,
        end: datetime.datetime,
        duration: datetime.timedelta,
        accepts: Callable[[datetime.datetime, datetime.datetime], bool],
    ) -> datetime.datetime | None:
        start = self.buffer_policy.ceil_datetime(begin, self.timezone_name, allow_repeated_hour=True)
        increment = datetime.timedelta(minutes=SLOT_INCREMENT_MINUTES)
        while start + duration <= end:
            if accepts(start, start + duration):
                return start
            start += increment
        return None

    def _evaluate_gap(
        self,
        position: SchedulePosition,
        next_start_time: datetime.datetime,
        next_coords: Optional[Tuple[float, float]],
        outbound_departure_time: Optional[datetime.datetime],
        outbound_deadline: Optional[datetime.datetime],
        target_coords: Tuple[float, float],
        base_coordinates: Tuple[float, float],
        next_shift_start: datetime.datetime | None,
        day_start: datetime.datetime,
        day_end: datetime.datetime,
        lesson_delta: datetime.timedelta,
        slot_filter: Callable[[datetime.datetime, datetime.datetime], bool] | None = None,
        slot_limit: int | None = None,
    ) -> List[TimeSlotDTO]:
        slots = []
        # Skip occupied gaps before asking the routing provider for an estimate.
        latest_gap_end = min(next_start_time, day_end)
        gap_start = max(day_start, position.free_at)
        if slot_limit == 0 or gap_start + lesson_delta > latest_gap_end:
            return slots
        increment = datetime.timedelta(minutes=SLOT_INCREMENT_MINUTES)
        if slot_filter is not None:
            matching_start = self.first_matching_start(gap_start, latest_gap_end, lesson_delta, slot_filter)
            if matching_start is None:
                return slots
            gap_start = matching_start
        transition = self.inbound_transition(position, target_coords)
        raw_start = max(gap_start, position.free_at + transition.duration)
        current_candidate = self.buffer_policy.ceil_datetime(raw_start, self.timezone_name, allow_repeated_hour=True)
        # A locationless blocker is still a temporal boundary. Location only
        # controls whether outbound travel to the next event must fit.
        while current_candidate + lesson_delta <= latest_gap_end:
            candidate_end = current_candidate + lesson_delta
            if slot_filter is not None and not slot_filter(current_candidate, candidate_end):
                current_candidate += increment
                continue
            if next_coords is not None:
                route_departure = outbound_departure_time or candidate_end
                route_deadline = outbound_deadline or next_start_time
                fits = self._outbound_fits(target_coords, next_coords, route_departure, route_deadline)
                if (
                    not fits and outbound_departure_time is not None
                    and next_shift_start is not None and route_deadline >= next_shift_start
                    and next_coords == base_coordinates
                ):
                    # The return can finish before the first locationless block.
                    fits = self._outbound_fits(
                        target_coords, base_coordinates, candidate_end, min(next_start_time, next_shift_start),
                    )
                if not fits:
                    current_candidate += increment
                    continue

            slots.append(
                TimeSlotDTO(
                    start_time=current_candidate,
                    end_time=candidate_end,
                    travel_time_mins=transition.travel_mins,
                    buffer_mins=transition.buffer_mins,
                    distance_miles=transition.distance_miles,
                )
            )
            if slot_limit is not None and len(slots) == slot_limit:
                break
            current_candidate += increment

        return slots

    def _outbound_fits(
        self,
        origin: Tuple[float, float],
        destination: Tuple[float, float],
        departure: datetime.datetime,
        deadline: datetime.datetime,
    ) -> bool:
        if departure > deadline:
            return False
        distance, travel = self.routing.get_distance_and_travel_time(
            origin, destination, departure_time=departure,
        )
        buffer = self.buffer_policy.distance_buffer_minutes(distance)
        return departure + datetime.timedelta(minutes=travel + buffer) <= deadline


def event_coordinates(event: dict) -> Optional[Tuple[float, float]]:
    lat = event.get("lat")
    lng = event.get("lng")
    if lat is None or lng is None:
        return None
    return float(lat), float(lng)


def _outbound_route_anchor(
    events: List[dict],
    index: int,
) -> tuple[
    Optional[Tuple[float, float]],
    Optional[datetime.datetime],
    Optional[datetime.datetime],
]:
    immediate = events[index]
    immediate_coords = event_coordinates(immediate)
    if immediate_coords is not None:
        return immediate_coords, None, immediate["start_time"]

    # A candidate before a locationless blocker remains the last physical
    # location. Travel to the next located event can begin only after every
    # intervening locationless blocker has ended.
    departure = immediate["end_time"]
    for event in events[index + 1 :]:
        coords = event_coordinates(event)
        if coords is not None:
            return coords, departure, event["start_time"]
        departure = max(departure, event["end_time"])
    return None, None, None
