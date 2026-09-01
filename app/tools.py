import datetime
from typing import List, Optional
from app import timezones
from app.routing import BaseRoutingProvider, RoutingProviderNotConfigured
from app.engine import SwimSchedulerConstraintEngine
from app.models import LocationInfo, TimeSlotDTO
from app import db
from app.routing_service import build_routing_provider


class UnconfiguredRoutingProvider(BaseRoutingProvider):
    provider_name = "unconfigured"

    def __init__(self, error: RoutingProviderNotConfigured):
        self.error = error

    def estimate_route(self, origin_coords, dest_coords, departure_time=None, traffic_mode="none"):
        raise self.error

    def geocode(self, address: str):
        raise self.error


def _build_default_routing_provider():
    try:
        return build_routing_provider()
    except RoutingProviderNotConfigured as exc:
        return UnconfiguredRoutingProvider(exc)


routing_provider = _build_default_routing_provider()
constraint_engine = SwimSchedulerConstraintEngine(routing_provider)


def configure_routing_provider(provider):
    global routing_provider, constraint_engine
    routing_provider = provider
    constraint_engine = SwimSchedulerConstraintEngine(routing_provider)


def reset_routing_provider():
    configure_routing_provider(_build_default_routing_provider())

def find_available_swim_slots(
    instructor_id: str,
    date_str: str,
    client_address: str,
    is_instructor_facility: bool = False,
    duration_mins: int = 60
) -> List[TimeSlotDTO]:
    """
    Finds all valid swim lesson slots for a given client request on a specific date.
    Handles geocoding, instructor facility location preference, existing bookings, and distance buffers.
    """
    # 1. Fetch instructor info
    instructor = db.get_instructor_info(instructor_id)
    if not instructor:
        raise ValueError(f"Instructor {instructor_id} not found.")

    instructor_main_loc = LocationInfo(
        address=instructor.main_address,
        lat=instructor.lat,
        lng=instructor.lng,
        is_instructor_facility=True
    )

    # 2. Determine target location coords
    if is_instructor_facility:
        target_loc = instructor_main_loc
    else:
        lat, lng = routing_provider.geocode(client_address)
        target_loc = LocationInfo(
            address=client_address,
            lat=lat,
            lng=lng,
            is_instructor_facility=False
        )

    # 3. Parse instructor-local working bounds, then run the engine in UTC.
    target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    day_start_local = timezones.parse_local_date_time(
        target_date,
        instructor.work_start_time,
        instructor.timezone,
    )
    day_end_local = timezones.parse_local_date_time(
        target_date,
        instructor.work_end_time,
        instructor.timezone,
    )
    if day_end_local <= day_start_local:
        day_end_local = day_end_local + datetime.timedelta(days=1)
    day_start = timezones.ensure_aware_utc(day_start_local)
    day_end = timezones.ensure_aware_utc(day_end_local)

    # 4. Fetch existing appointments & breaks
    existing_events = db.get_utc_appointment_events_for_date(instructor_id, date_str)

    # 5. Run constraint engine
    utc_slots = constraint_engine.find_available_slots_for_day(
        day_start=day_start,
        day_end=day_end,
        instructor_main_location=instructor_main_loc,
        existing_events=existing_events,
        target_location=target_loc,
        duration_mins=duration_mins
    )

    return [
        slot.model_copy(
            update={
                "start_time": timezones.aware_to_local(slot.start_time, instructor.timezone),
                "end_time": timezones.aware_to_local(slot.end_time, instructor.timezone),
            }
        )
        for slot in utc_slots
    ]

def book_swim_lesson(
    instructor_id: str,
    client_id: str,
    client_name: str,
    start_time_iso: str,
    duration_mins: int,
    location_address: str,
    idempotency_key: Optional[str] = None,
    request_id: Optional[str] = None,
) -> dict:
    """
    Books an appointment slot after validating inputs.
    """
    instructor = db.get_instructor_info(instructor_id)
    if not instructor:
        raise ValueError(f"Instructor {instructor_id} not found.")
    parsed_start = datetime.datetime.fromisoformat(start_time_iso)
    if parsed_start.tzinfo is None or parsed_start.utcoffset() is None:
        start_time = timezones.validate_local_datetime(parsed_start, instructor.timezone)
    else:
        start_time = parsed_start
    start_time_storage = timezones.to_utc_storage(start_time)
    end_time_storage = start_time_storage + datetime.timedelta(minutes=duration_mins)

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
        "message": f"Successfully booked swim lesson for {client_name} on {timezones.aware_to_local(start_time, instructor.timezone).strftime('%b %d, %Y at %I:%M %p')}!"
    }
