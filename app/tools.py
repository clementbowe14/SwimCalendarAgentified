import datetime
from typing import List, Optional

from app import db, timezones
from app.engine import BufferPolicy, SwimSchedulerConstraintEngine
from app.models import LocationInfo, TimeSlotDTO
from app.provider_settings import (
    ProviderNotFound,
    ProviderSetupIncomplete,
    load_provider_configuration,
    require_provider_setup,
    working_interval_for_date,
)
from app.routing import BaseRoutingProvider, RoutingProviderNotConfigured
from app.routing_service import build_routing_provider
from app.scheduler_settings import load_buffer_policy


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


def configure_routing_provider(provider):
    global routing_provider
    routing_provider = provider


def reset_routing_provider():
    configure_routing_provider(_build_default_routing_provider())


def find_available_swim_slots(
    instructor_id: str,
    date_str: str,
    client_address: str,
    is_instructor_facility: bool = False,
    duration_mins: int = 60,
    buffer_policy: Optional[BufferPolicy] = None,
) -> List[TimeSlotDTO]:
    """
    Finds all valid swim lesson slots for a given client request on a specific date.
    Handles geocoding, instructor facility location preference, existing bookings, and distance buffers.
    """
    try:
        provider = require_provider_setup(instructor_id)
    except ProviderNotFound as exc:
        raise ValueError(f"Instructor {instructor_id} not found.") from exc

    # A complete provider always has both locations, but retaining the guard
    # keeps this boundary fail-closed if malformed legacy data is introduced.
    if provider.base_location is None or provider.service_location is None:
        raise ProviderSetupIncomplete(provider.setup_missing_fields)

    target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    working_interval = working_interval_for_date(provider, target_date)
    if working_interval is None:
        return []

    instructor_base_loc = LocationInfo(
        address=provider.base_location.address,
        lat=provider.base_location.lat,
        lng=provider.base_location.lng,
        is_instructor_facility=False,
    )
    instructor_service_loc = LocationInfo(
        address=provider.service_location.address,
        lat=provider.service_location.lat,
        lng=provider.service_location.lng,
        is_instructor_facility=True,
    )

    if is_instructor_facility:
        target_loc = instructor_service_loc
    else:
        lat, lng = routing_provider.geocode(client_address)
        target_loc = LocationInfo(
            address=client_address,
            lat=lat,
            lng=lng,
            is_instructor_facility=False,
        )

    start_label, end_label = working_interval
    try:
        day_start_local = timezones.parse_local_date_time(
            target_date,
            start_label,
            provider.timezone,
        )
        day_end_local = timezones.parse_local_date_time(
            target_date,
            end_label,
            provider.timezone,
        )
    except (timezones.AmbiguousLocalTimeError, timezones.NonexistentLocalTimeError):
        return []
    if day_end_local <= day_start_local:
        # Overnight windows are rejected at the API and database boundaries.
        # Fail closed if invalid rows somehow reach the scheduler.
        return []

    day_start = timezones.ensure_aware_utc(day_start_local)
    day_end = timezones.ensure_aware_utc(day_end_local)

    existing_events = _clip_events_to_working_interval(
        db.get_utc_appointment_events_for_date(instructor_id, date_str),
        day_start=day_start,
        day_end=day_end,
    )
    policy = buffer_policy or load_buffer_policy(instructor_id, operation="availability")
    constraint_engine = SwimSchedulerConstraintEngine(
        routing_provider,
        policy,
        timezone_name=provider.timezone,
    )
    utc_slots = constraint_engine.find_available_slots_for_day(
        day_start=day_start,
        day_end=day_end,
        instructor_main_location=instructor_base_loc,
        existing_events=existing_events,
        target_location=target_loc,
        duration_mins=duration_mins,
    )

    return [
        slot.model_copy(
            update={
                "start_time": timezones.aware_to_local(slot.start_time, provider.timezone),
                "end_time": timezones.aware_to_local(slot.end_time, provider.timezone),
            }
        )
        for slot in utc_slots
        if slot.start_time >= day_start and slot.end_time <= day_end
    ]


def _clip_events_to_working_interval(
    events: List[dict],
    *,
    day_start: datetime.datetime,
    day_end: datetime.datetime,
) -> List[dict]:
    """Keep scheduler inputs inside the provider's configured working interval."""
    bounded_events: List[dict] = []
    for event in events:
        event_start = event["start_time"]
        event_end = event["end_time"]
        if event_end <= day_start or event_start >= day_end:
            continue

        bounded_event = event.copy()
        bounded_event["start_time"] = max(event_start, day_start)
        bounded_event["end_time"] = min(event_end, day_end)
        if bounded_event["end_time"] > bounded_event["start_time"]:
            bounded_events.append(bounded_event)
    return bounded_events


def book_swim_lesson(
    instructor_id: str,
    client_id: str,
    client_name: str,
    start_time_iso: str,
    duration_mins: int,
    location_address: str,
    idempotency_key: Optional[str] = None,
    request_id: Optional[str] = None,
    is_instructor_facility: bool = False,
    buffer_policy: Optional[BufferPolicy] = None,
) -> dict:
    """Books an appointment after revalidating the authoritative schedule."""
    try:
        provider = load_provider_configuration(instructor_id)
    except ProviderNotFound as exc:
        raise ValueError(f"Instructor {instructor_id} not found.") from exc
    parsed_start = datetime.datetime.fromisoformat(start_time_iso)
    if parsed_start.tzinfo is None or parsed_start.utcoffset() is None:
        try:
            start_time = timezones.validate_local_datetime(parsed_start, provider.timezone)
        except timezones.InvalidTimezoneError as exc:
            raise ProviderSetupIncomplete(provider.setup_missing_fields) from exc
    else:
        start_time = parsed_start
    start_time_storage = timezones.to_utc_storage(start_time)
    end_time_storage = start_time_storage + datetime.timedelta(minutes=duration_mins)

    existing = db.get_appointment_by_idempotency_key(instructor_id, idempotency_key)
    if existing is not None:
        if (
            existing["instructor_id"] != instructor_id
            or existing["client_id"] != client_id
            or timezones.to_utc_storage(existing["start_time"]) != start_time_storage
            or timezones.to_utc_storage(existing["end_time"]) != end_time_storage
        ):
            raise db.IdempotencyKeyConflictError("The idempotency key was used for another booking.")
        return {
            "success": True,
            "appointment_id": existing["id"],
            "message": f"Successfully booked swim lesson for {client_name} on {timezones.aware_to_local(start_time, provider.timezone).strftime('%b %d, %Y at %I:%M %p')}!",
        }

    if not provider.setup_complete:
        raise ProviderSetupIncomplete(provider.setup_missing_fields)

    policy = buffer_policy or load_buffer_policy(instructor_id, operation="direct_booking")
    available_slots = find_available_swim_slots(
        instructor_id=instructor_id,
        date_str=timezones.requested_local_date(start_time, provider.timezone).isoformat(),
        client_address=location_address,
        is_instructor_facility=is_instructor_facility,
        duration_mins=duration_mins,
        buffer_policy=policy,
    )
    if not any(timezones.to_utc_storage(slot.start_time) == start_time_storage for slot in available_slots):
        raise db.ScheduleConflictError("The requested start is no longer available.")

    if is_instructor_facility:
        if provider.service_location is None:
            raise ProviderSetupIncomplete(provider.setup_missing_fields)
        lat, lng = provider.service_location.lat, provider.service_location.lng
        location_address = provider.service_location.address
    else:
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
        "message": f"Successfully booked swim lesson for {client_name} on {timezones.aware_to_local(start_time, provider.timezone).strftime('%b %d, %Y at %I:%M %p')}!"
    }
