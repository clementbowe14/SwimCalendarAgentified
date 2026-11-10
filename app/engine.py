import datetime
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

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
    ) -> datetime.datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return _ceil_naive_label(value, SLOT_INCREMENT_MINUTES)

        zone = timezones.get_zone(timezone_name)
        local = value.astimezone(zone)
        label = _ceil_naive_label(local.replace(tzinfo=None), SLOT_INCREMENT_MINUTES)
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
    ) -> List[TimeSlotDTO]:
        valid_slots: List[TimeSlotDTO] = []
        target_coords = (target_location.lat, target_location.lng)
        lesson_delta = datetime.timedelta(minutes=duration_mins)
        sorted_events = sorted(existing_events, key=lambda item: item["start_time"])
        prev_location_coords = (instructor_main_location.lat, instructor_main_location.lng)
        prev_end_time = day_start
        prev_is_break = False
        is_shift_start = True

        for index, event in enumerate(sorted_events):
            event_coords = _event_coordinates(event)
            next_coords, outbound_departure, outbound_deadline = _outbound_route_anchor(
                sorted_events,
                index,
            )
            valid_slots.extend(
                self._evaluate_gap(
                    prev_end_time=prev_end_time,
                    prev_coords=prev_location_coords,
                    prev_is_break=prev_is_break,
                    is_shift_start=is_shift_start,
                    next_start_time=event["start_time"],
                    next_coords=next_coords,
                    outbound_departure_time=outbound_departure,
                    outbound_deadline=outbound_deadline,
                    target_coords=target_coords,
                    day_end=day_end,
                    lesson_delta=lesson_delta,
                )
            )
            prev_end_time = event["end_time"]
            if event_coords is not None:
                prev_location_coords = event_coords
            prev_is_break = (
                event.get("blocker_kind") == "break"
                if event.get("event_type") == "block" or event.get("blocker_kind") is not None
                else bool(event.get("is_break", False))
            )
            is_shift_start = False

        valid_slots.extend(
            self._evaluate_gap(
                prev_end_time=prev_end_time,
                prev_coords=prev_location_coords,
                prev_is_break=prev_is_break,
                is_shift_start=is_shift_start,
                next_start_time=day_end,
                next_coords=None,
                outbound_departure_time=None,
                outbound_deadline=None,
                target_coords=target_coords,
                day_end=day_end,
                lesson_delta=lesson_delta,
            )
        )
        return valid_slots

    def _evaluate_gap(
        self,
        prev_end_time: datetime.datetime,
        prev_coords: Tuple[float, float],
        prev_is_break: bool,
        is_shift_start: bool,
        next_start_time: datetime.datetime,
        next_coords: Optional[Tuple[float, float]],
        outbound_departure_time: Optional[datetime.datetime],
        outbound_deadline: Optional[datetime.datetime],
        target_coords: Tuple[float, float],
        day_end: datetime.datetime,
        lesson_delta: datetime.timedelta,
    ) -> List[TimeSlotDTO]:
        slots = []
        dist_from_prev, travel_from_prev = self.routing.get_distance_and_travel_time(
            prev_coords,
            target_coords,
            departure_time=prev_end_time,
        )
        buffer_mins = self.buffer_policy.inbound_buffer_minutes(
            dist_from_prev,
            is_after_break=prev_is_break,
            is_shift_start=is_shift_start,
        )
        raw_start = prev_end_time + datetime.timedelta(minutes=travel_from_prev + buffer_mins)
        current_candidate = self.buffer_policy.ceil_datetime(raw_start, self.timezone_name)
        # A locationless blocker is still a temporal boundary. Location only
        # controls whether outbound travel to the next event must fit.
        latest_gap_end = min(next_start_time, day_end)
        increment = datetime.timedelta(minutes=SLOT_INCREMENT_MINUTES)

        while current_candidate + lesson_delta <= latest_gap_end:
            candidate_end = current_candidate + lesson_delta
            if next_coords is not None:
                route_departure = outbound_departure_time or candidate_end
                route_deadline = outbound_deadline or next_start_time
                dist_to_next, travel_to_next = self.routing.get_distance_and_travel_time(
                    target_coords,
                    next_coords,
                    departure_time=route_departure,
                )
                buffer_to_next = self.buffer_policy.distance_buffer_minutes(dist_to_next)
                if route_departure + datetime.timedelta(
                    minutes=travel_to_next + buffer_to_next
                ) > route_deadline:
                    current_candidate += increment
                    continue

            slots.append(
                TimeSlotDTO(
                    start_time=current_candidate,
                    end_time=candidate_end,
                    travel_time_mins=travel_from_prev,
                    buffer_mins=buffer_mins,
                    distance_miles=dist_from_prev,
                )
            )
            current_candidate += increment

        return slots


def _event_coordinates(event: dict) -> Optional[Tuple[float, float]]:
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
    immediate_coords = _event_coordinates(immediate)
    if immediate_coords is not None:
        return immediate_coords, None, immediate["start_time"]

    # A candidate before a locationless blocker remains the last physical
    # location. Travel to the next located event can begin only after every
    # intervening locationless blocker has ended.
    departure = immediate["end_time"]
    for event in events[index + 1 :]:
        coords = _event_coordinates(event)
        if coords is not None:
            return coords, departure, event["start_time"]
        departure = max(departure, event["end_time"])
    return None, None, None
