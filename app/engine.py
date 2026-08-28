import datetime
from typing import List, Tuple, Optional
from app.models import TimeSlotDTO, LocationInfo
from app.routing import BaseRoutingProvider

class SwimSchedulerConstraintEngine:
    """
    Deterministic constraint solver for swim lesson scheduling.
    Enforces inter-lesson distance buffers, post-break buffers, 
    initial location baseline, and 15-minute ceiling rounding.
    """
    def __init__(self, routing_provider: BaseRoutingProvider):
        self.routing = routing_provider

    @staticmethod
    def get_inter_lesson_buffer_minutes(distance_miles: float) -> int:
        """
        Inter-lesson buffer tier rules:
        - x < 5.0 miles -> 10 minutes
        - 5.0 <= x <= 10.0 miles -> 20 minutes
        - x > 10.0 miles -> 30 minutes
        """
        if distance_miles < 5.0:
            return 10
        elif 5.0 <= distance_miles <= 10.0:
            return 20
        else:
            return 30

    @staticmethod
    def apply_15min_ceiling(dt: datetime.datetime) -> datetime.datetime:
        """
        Rounds datetime UP to the nearest 15-minute boundary.
        Guarantees that calculated travel and buffer times are strictly preserved.
        """
        discard = datetime.timedelta(
            minutes=dt.minute % 15,
            seconds=dt.second,
            microseconds=dt.microsecond
        )
        if discard.total_seconds() == 0:
            return dt
        return dt + datetime.timedelta(minutes=15) - discard

    def compute_earliest_start_time(
        self,
        prev_end_time: datetime.datetime,
        travel_time_mins: int,
        distance_miles: float,
        is_after_break: bool = False
    ) -> datetime.datetime:
        """
        Calculates the earliest valid start time for a lesson given the previous
        event end time, travel time, and buffer rules.
        """
        if is_after_break:
            buffer_mins = 15 # Fixed post-break recovery buffer rule
        else:
            buffer_mins = self.get_inter_lesson_buffer_minutes(distance_miles)

        total_offset_mins = travel_time_mins + buffer_mins
        raw_earliest = prev_end_time + datetime.timedelta(minutes=total_offset_mins)
        return self.apply_15min_ceiling(raw_earliest)

    def find_available_slots_for_day(
        self,
        day_start: datetime.datetime,
        day_end: datetime.datetime,
        instructor_main_location: LocationInfo,
        existing_events: List[dict], # List of dicts with start_time, end_time, lat, lng, is_break
        target_location: LocationInfo,
        duration_mins: int = 60
    ) -> List[TimeSlotDTO]:
        """
        Scans the day schedule to find all valid, non-overlapping 15-minute aligned time slots.
        
        existing_events format:
        [
            {
                "id": str,
                "start_time": datetime,
                "end_time": datetime,
                "lat": float,
                "lng": float,
                "is_break": bool
            },
            ...
        ]
        """
        valid_slots: List[TimeSlotDTO] = []
        target_coords = (target_location.lat, target_location.lng)
        lesson_delta = datetime.timedelta(minutes=duration_mins)

        # Ensure events are sorted by start_time
        sorted_events = sorted(existing_events, key=lambda x: x["start_time"])

        # Build gaps timeline: list of (prev_event, next_event)
        # We start from day_start (where previous location is instructor main address)
        timeline_checkpoints = []

        # Virtual initial checkpoint at start of day from instructor's main address
        prev_location_coords = (instructor_main_location.lat, instructor_main_location.lng)
        prev_end_time = day_start
        prev_is_break = False

        for i, event in enumerate(sorted_events):
            next_start_time = event["start_time"]
            next_location_coords = (event["lat"], event["lng"])

            # Evaluate candidate slots in the gap [prev_end_time, next_start_time]
            gap_slots = self._evaluate_gap(
                prev_end_time=prev_end_time,
                prev_coords=prev_location_coords,
                prev_is_break=prev_is_break,
                next_start_time=next_start_time,
                next_coords=next_location_coords,
                target_coords=target_coords,
                day_start=day_start,
                day_end=day_end,
                lesson_delta=lesson_delta,
                duration_mins=duration_mins
            )
            valid_slots.extend(gap_slots)

            # Update prev checkpoint
            prev_end_time = event["end_time"]
            prev_location_coords = (event["lat"], event["lng"])
            prev_is_break = event.get("is_break", False)

        # Final gap from last event end (or day_start if no events) to day_end
        final_slots = self._evaluate_gap(
            prev_end_time=prev_end_time,
            prev_coords=prev_location_coords,
            prev_is_break=prev_is_break,
            next_start_time=day_end,
            next_coords=None, # No subsequent event to travel to
            target_coords=target_coords,
            day_start=day_start,
            day_end=day_end,
            lesson_delta=lesson_delta,
            duration_mins=duration_mins
        )
        valid_slots.extend(final_slots)

        return valid_slots

    def _evaluate_gap(
        self,
        prev_end_time: datetime.datetime,
        prev_coords: Tuple[float, float],
        prev_is_break: bool,
        next_start_time: datetime.datetime,
        next_coords: Optional[Tuple[float, float]],
        target_coords: Tuple[float, float],
        day_start: datetime.datetime,
        day_end: datetime.datetime,
        lesson_delta: datetime.timedelta,
        duration_mins: int
    ) -> List[TimeSlotDTO]:
        slots = []

        # 1. Travel from previous location to target location
        dist_from_prev, travel_from_prev = self.routing.get_distance_and_travel_time(
            prev_coords,
            target_coords,
            departure_time=prev_end_time,
        )

        if prev_is_break:
            buffer_mins = 15
        else:
            buffer_mins = self.get_inter_lesson_buffer_minutes(dist_from_prev)

        # Earliest candidate start time
        if prev_end_time == day_start:
            # First lesson of the day: starts after travel from main address
            raw_start = day_start + datetime.timedelta(minutes=travel_from_prev)
            candidate_start = self.apply_15min_ceiling(raw_start)
        else:
            candidate_start = self.compute_earliest_start_time(
                prev_end_time=prev_end_time,
                travel_time_mins=travel_from_prev,
                distance_miles=dist_from_prev,
                is_after_break=prev_is_break
            )

        # Loop through 15-minute increments in the candidate window
        current_candidate = candidate_start
        latest_gap_end = next_start_time if next_coords is not None else day_end
        while current_candidate + lesson_delta <= latest_gap_end:
            candidate_end = current_candidate + lesson_delta
            if next_coords is not None:
                dist_to_next, travel_to_next = self.routing.get_distance_and_travel_time(
                    target_coords,
                    next_coords,
                    departure_time=candidate_end,
                )
                buffer_to_next = self.get_inter_lesson_buffer_minutes(dist_to_next)
                arrives_before_next = (
                    candidate_end + datetime.timedelta(minutes=travel_to_next + buffer_to_next)
                    <= next_start_time
                )
                if not arrives_before_next:
                    current_candidate += datetime.timedelta(minutes=15)
                    continue

            slot = TimeSlotDTO(
                start_time=current_candidate,
                end_time=candidate_end,
                travel_time_mins=travel_from_prev,
                buffer_mins=buffer_mins,
                distance_miles=dist_from_prev
            )
            slots.append(slot)
            current_candidate += datetime.timedelta(minutes=15)

        return slots
