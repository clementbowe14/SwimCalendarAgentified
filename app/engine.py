import datetime
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from app import timezones
from app.models import LocationInfo, TimeSlotDTO
from app.routing import BaseRoutingProvider


@dataclass(frozen=True)
class DistanceBufferTier:
    min_miles: float
    max_miles: Optional[float]
    buffer_mins: int


@dataclass(frozen=True)
class BufferPolicy:
    buffers_enabled: bool
    apply_distance_buffer_at_shift_start: bool
    rounding_increment_mins: int
    post_break_buffer_mins: int
    distance_buffer_tiers: tuple[DistanceBufferTier, ...]

    @classmethod
    def default(cls) -> "BufferPolicy":
        return cls(
            buffers_enabled=True,
            apply_distance_buffer_at_shift_start=False,
            rounding_increment_mins=15,
            post_break_buffer_mins=15,
            distance_buffer_tiers=(
                DistanceBufferTier(0, 4.999, 10),
                DistanceBufferTier(5, 10, 20),
                DistanceBufferTier(10.001, None, 30),
            ),
        )

    def distance_buffer_minutes(self, distance_miles: float) -> int:
        if not self.buffers_enabled:
            return 0
        if not math.isfinite(distance_miles) or distance_miles < 0:
            raise ValueError("Distance must be a finite non-negative number.")
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
        if not self.buffers_enabled:
            return 0
        if is_shift_start and not self.apply_distance_buffer_at_shift_start:
            return 0
        if is_after_break:
            return self.post_break_buffer_mins
        return self.distance_buffer_minutes(distance_miles)

    def ceil_datetime(
        self,
        value: datetime.datetime,
        timezone_name: Optional[str] = None,
    ) -> datetime.datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return _ceil_naive_label(value, self.rounding_increment_mins)

        zone = timezones.get_zone(timezone_name)
        local = value.astimezone(zone)
        label = _ceil_naive_label(local.replace(tzinfo=None), self.rounding_increment_mins)
        while True:
            resolved = _resolve_local_label(label, zone, local.fold)
            if resolved is not None:
                return resolved.astimezone(value.tzinfo)
            label += datetime.timedelta(minutes=self.rounding_increment_mins)


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
        buffer_policy: Optional[BufferPolicy] = None,
        timezone_name: Optional[str] = None,
    ):
        self.routing = routing_provider
        self.buffer_policy = buffer_policy or BufferPolicy.default()
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

        for event in sorted_events:
            valid_slots.extend(
                self._evaluate_gap(
                    prev_end_time=prev_end_time,
                    prev_coords=prev_location_coords,
                    prev_is_break=prev_is_break,
                    is_shift_start=is_shift_start,
                    next_start_time=event["start_time"],
                    next_coords=(event["lat"], event["lng"]),
                    target_coords=target_coords,
                    day_end=day_end,
                    lesson_delta=lesson_delta,
                )
            )
            prev_end_time = event["end_time"]
            prev_location_coords = (event["lat"], event["lng"])
            prev_is_break = event.get("is_break", False)
            is_shift_start = False

        valid_slots.extend(
            self._evaluate_gap(
                prev_end_time=prev_end_time,
                prev_coords=prev_location_coords,
                prev_is_break=prev_is_break,
                is_shift_start=is_shift_start,
                next_start_time=day_end,
                next_coords=None,
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
        latest_gap_end = next_start_time if next_coords is not None else day_end
        increment = datetime.timedelta(minutes=self.buffer_policy.rounding_increment_mins)

        while current_candidate + lesson_delta <= latest_gap_end:
            candidate_end = current_candidate + lesson_delta
            if next_coords is not None:
                dist_to_next, travel_to_next = self.routing.get_distance_and_travel_time(
                    target_coords,
                    next_coords,
                    departure_time=candidate_end,
                )
                buffer_to_next = self.buffer_policy.distance_buffer_minutes(dist_to_next)
                if candidate_end + datetime.timedelta(minutes=travel_to_next + buffer_to_next) > next_start_time:
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
