import pytest
import datetime
from app.routing import BaseRoutingProvider, HaversineRoutingProvider, RouteEstimate
from app.engine import SwimSchedulerConstraintEngine
from app.models import LocationInfo

@pytest.fixture
def routing():
    return HaversineRoutingProvider()

@pytest.fixture
def engine(routing):
    return SwimSchedulerConstraintEngine(routing)

def test_distance_buffer_tiers(engine):
    # Tier 1: x < 5 miles -> 10 mins
    assert engine.get_inter_lesson_buffer_minutes(2.5) == 10
    assert engine.get_inter_lesson_buffer_minutes(4.99) == 10

    # Tier 2: 5 <= x <= 10 miles -> 20 mins
    assert engine.get_inter_lesson_buffer_minutes(5.0) == 20
    assert engine.get_inter_lesson_buffer_minutes(7.5) == 20
    assert engine.get_inter_lesson_buffer_minutes(10.0) == 20

    # Tier 3: x > 10 miles -> 30 mins
    assert engine.get_inter_lesson_buffer_minutes(10.1) == 30
    assert engine.get_inter_lesson_buffer_minutes(15.0) == 30

def test_15min_ceiling_rounding(engine):
    # Exactly on boundary -> unchanged
    t1 = datetime.datetime(2026, 8, 20, 10, 0, 0)
    assert engine.apply_15min_ceiling(t1) == t1

    # 10:01 AM -> 10:15 AM
    t2 = datetime.datetime(2026, 8, 20, 10, 1, 0)
    assert engine.apply_15min_ceiling(t2) == datetime.datetime(2026, 8, 20, 10, 15, 0)

    # 10:07 AM -> 10:15 AM
    t3 = datetime.datetime(2026, 8, 20, 10, 7, 30)
    assert engine.apply_15min_ceiling(t3) == datetime.datetime(2026, 8, 20, 10, 15, 0)

    # 10:46 AM -> 11:00 AM
    t4 = datetime.datetime(2026, 8, 20, 10, 46, 0)
    assert engine.apply_15min_ceiling(t4) == datetime.datetime(2026, 8, 20, 11, 0, 0)

def test_post_break_buffer(engine):
    break_end = datetime.datetime(2026, 8, 20, 13, 0, 0) # 1:00 PM
    travel_mins = 18
    distance_miles = 6.5

    # After break rule: break_end + travel_mins + 15 min break buffer
    # 1:00 PM + 18m travel + 15m buffer = 1:33 PM
    # 1:33 PM ceiling rounded to 15m -> 1:45 PM
    earliest = engine.compute_earliest_start_time(
        prev_end_time=break_end,
        travel_time_mins=travel_mins,
        distance_miles=distance_miles,
        is_after_break=True
    )
    assert earliest == datetime.datetime(2026, 8, 20, 13, 45, 0)

def test_first_lesson_start_location(engine, routing):
    main_loc = LocationInfo(address="Base", lat=30.2672, lng=-97.7431, is_instructor_facility=True)
    target_loc = LocationInfo(address="North Pool", lat=30.3300, lng=-97.7200, is_instructor_facility=False)

    day_start = datetime.datetime(2026, 8, 20, 8, 0, 0)
    day_end = datetime.datetime(2026, 8, 20, 17, 0, 0)

    slots = engine.find_available_slots_for_day(
        day_start=day_start,
        day_end=day_end,
        instructor_main_location=main_loc,
        existing_events=[],
        target_location=target_loc,
        duration_mins=60
    )

    # Must return slots starting on or after day_start + travel_mins (ceiling rounded)
    assert len(slots) > 0
    first_slot = slots[0]
    dist, travel = routing.get_distance_and_travel_time((main_loc.lat, main_loc.lng), (target_loc.lat, target_loc.lng))
    expected_earliest = engine.apply_15min_ceiling(day_start + datetime.timedelta(minutes=travel))
    assert first_slot.start_time == expected_earliest


class RecordingRoutingProvider(BaseRoutingProvider):
    provider_name = "recording"

    def __init__(self):
        self.calls = []

    def estimate_route(self, origin_coords, dest_coords, departure_time=None, traffic_mode="none"):
        self.calls.append(
            {
                "origin": origin_coords,
                "destination": dest_coords,
                "departure_time": departure_time,
                "traffic_mode": traffic_mode,
            }
        )
        return RouteEstimate(
            distance_miles=1.0,
            travel_time_mins=5,
            duration_seconds=300,
            provider=self.provider_name,
        )

    def geocode(self, address: str):
        return (30.2672, -97.7431)


def test_engine_passes_departure_context_to_routing_provider():
    routing = RecordingRoutingProvider()
    engine = SwimSchedulerConstraintEngine(routing)
    main_loc = LocationInfo(address="Base", lat=30.0, lng=-97.0, is_instructor_facility=True)
    target_loc = LocationInfo(address="Target", lat=30.1, lng=-97.1, is_instructor_facility=False)
    next_loc = LocationInfo(address="Next", lat=30.2, lng=-97.2, is_instructor_facility=False)

    slots = engine.find_available_slots_for_day(
        day_start=datetime.datetime(2026, 8, 20, 8, 0),
        day_end=datetime.datetime(2026, 8, 20, 11, 0),
        instructor_main_location=main_loc,
        existing_events=[
            {
                "id": "app_next",
                "start_time": datetime.datetime(2026, 8, 20, 10, 0),
                "end_time": datetime.datetime(2026, 8, 20, 10, 45),
                "lat": next_loc.lat,
                "lng": next_loc.lng,
                "is_break": False,
            }
        ],
        target_location=target_loc,
        duration_mins=30,
    )

    assert slots
    assert all(call["departure_time"] is not None for call in routing.calls)
    assert datetime.datetime(2026, 8, 20, 8, 0) in {
        call["departure_time"] for call in routing.calls
    }
    assert slots[0].end_time in {call["departure_time"] for call in routing.calls}


class ReturnLegTrafficProvider(RecordingRoutingProvider):
    def estimate_route(self, origin_coords, dest_coords, departure_time=None, traffic_mode="none"):
        self.calls.append(
            {
                "origin": origin_coords,
                "destination": dest_coords,
                "departure_time": departure_time,
                "traffic_mode": traffic_mode,
            }
        )
        travel_time = 40 if departure_time == datetime.datetime(2026, 8, 20, 9, 30) else 5
        return RouteEstimate(
            distance_miles=1.0,
            travel_time_mins=travel_time,
            duration_seconds=travel_time * 60,
            provider=self.provider_name,
        )


def test_engine_uses_candidate_end_time_for_return_leg_routing():
    routing = ReturnLegTrafficProvider()
    engine = SwimSchedulerConstraintEngine(routing)
    main_loc = LocationInfo(address="Base", lat=30.0, lng=-97.0, is_instructor_facility=True)
    target_loc = LocationInfo(address="Target", lat=30.1, lng=-97.1, is_instructor_facility=False)

    slots = engine.find_available_slots_for_day(
        day_start=datetime.datetime(2026, 8, 20, 8, 0),
        day_end=datetime.datetime(2026, 8, 20, 11, 0),
        instructor_main_location=main_loc,
        existing_events=[
            {
                "id": "app_next",
                "start_time": datetime.datetime(2026, 8, 20, 10, 0),
                "end_time": datetime.datetime(2026, 8, 20, 10, 45),
                "lat": 30.2,
                "lng": -97.2,
                "is_break": False,
            }
        ],
        target_location=target_loc,
        duration_mins=30,
    )

    assert datetime.datetime(2026, 8, 20, 9, 30) in {
        call["departure_time"] for call in routing.calls
    }
    assert datetime.datetime(2026, 8, 20, 9, 0) not in {slot.start_time for slot in slots}
