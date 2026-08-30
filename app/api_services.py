from datetime import date, datetime, timedelta
import re

from app import db, tools
from app.api_errors import (
    GeocodingFailed as GeocodingFailedApiError,
    IdempotencyConflict,
    InvalidSchedulingRequest,
    ResourceNotFound,
    RoutingConfigurationError,
    RoutingEstimateFailed as RoutingEstimateFailedApiError,
    RoutingProviderUnavailable as RoutingProviderUnavailableApiError,
    RoutingTimeout as RoutingTimeoutApiError,
    SchedulingConflict,
)
from app.api_schemas import (
    AppointmentResponse,
    AvailabilitySearchRequest,
    AvailabilitySearchResponse,
    BookLessonResponse,
    CoachBookLessonRequest,
    CoachScheduleResponse,
    InstructorProfileResponse,
    LocationResponse,
    PublicAvailabilitySearchRequest,
    PublicAvailabilitySearchResponse,
    PublicInviteResponse,
    PublicLessonRequestCreate,
    PublicLessonRequestResponse,
    PublicRequestStatusResponse,
    TimeSlotResponse,
)
from app.models import TimeSlotDTO
from app.public_tokens import (
    InviteContext,
    create_lesson_request,
    resolve_lesson_request_by_idempotency_key,
    resolve_lesson_request,
)
from app.routing import (
    GeocodeFailed,
    RouteEstimateFailed,
    RoutingProviderNotConfigured,
    RoutingProviderUnavailable,
    RoutingTimeout,
)


def get_instructor_profile(instructor_id: str) -> InstructorProfileResponse:
    instructor = db.get_instructor_info(instructor_id)
    if not instructor:
        raise ResourceNotFound("The instructor could not be found.")
    return InstructorProfileResponse(
        id=instructor.id,
        name=instructor.name,
        main_address=instructor.main_address,
        lat=instructor.lat,
        lng=instructor.lng,
        work_start_time=instructor.work_start_time,
        work_end_time=instructor.work_end_time,
    )


def get_coach_schedule(instructor_id: str, schedule_date: date) -> CoachScheduleResponse:
    instructor = get_instructor_profile(instructor_id)
    appointments = db.get_appointments_for_date(instructor_id, schedule_date.isoformat())
    return CoachScheduleResponse(
        instructor=instructor,
        date=schedule_date,
        appointments=[
            AppointmentResponse(
                id=appointment["id"],
                title=appointment["title"],
                start_time=appointment["start_time"],
                end_time=appointment["end_time"],
                location_address=appointment["location_address"],
                lat=appointment["lat"],
                lng=appointment["lng"],
                is_break=appointment["is_break"],
            )
            for appointment in appointments
        ],
    )


def search_coach_availability(
    instructor_id: str,
    request: AvailabilitySearchRequest,
) -> AvailabilitySearchResponse:
    slots = _find_slots(
        instructor_id=instructor_id,
        requested_date=request.requested_date,
        client_address=request.client_address,
        is_instructor_facility=request.is_instructor_facility,
        duration_mins=request.duration_mins,
    )
    return AvailabilitySearchResponse(
        status="PRESENTING",
        client_name=request.client_name,
        requested_date=request.requested_date,
        duration_mins=request.duration_mins,
        location=_resolve_location(instructor_id, request.client_address, request.is_instructor_facility),
        available_slots=[_slot_response(slot) for slot in slots],
    )


def book_coach_lesson(instructor_id: str, request: CoachBookLessonRequest) -> BookLessonResponse:
    existing = db.get_appointment_by_idempotency_key(request.idempotency_key)
    if existing:
        if (
            existing["instructor_id"] != instructor_id
            or existing["client_id"] != _client_id_from_name(request.client_name)
            or existing["start_time"] != request.start_time
            or existing["end_time"] != request.start_time + _duration_delta(request.duration_mins)
        ):
            raise IdempotencyConflict()
        return BookLessonResponse(
            success=True,
            appointment_id=existing["id"],
            message=f"Successfully booked swim lesson for {request.client_name} on {existing['start_time'].strftime('%b %d, %Y at %I:%M %p')}!",
        )

    _require_available_start(
        instructor_id=instructor_id,
        requested_date=request.start_time.date(),
        client_address=request.client_address,
        is_instructor_facility=request.is_instructor_facility,
        duration_mins=request.duration_mins,
        requested_start=request.start_time,
    )
    location = _resolve_location(instructor_id, request.client_address, request.is_instructor_facility)
    try:
        result = tools.book_swim_lesson(
            instructor_id=instructor_id,
            client_id=_client_id_from_name(request.client_name),
            client_name=request.client_name,
            start_time_iso=request.start_time.isoformat(),
            duration_mins=request.duration_mins,
            location_address=location.address,
            idempotency_key=request.idempotency_key,
        )
    except db.ScheduleConflictError as exc:
        raise SchedulingConflict() from exc
    except db.IdempotencyKeyConflictError as exc:
        raise IdempotencyConflict() from exc
    return BookLessonResponse(**result)


def public_invite_response(invite: InviteContext) -> PublicInviteResponse:
    return PublicInviteResponse(
        invite_token=invite.token,
        instructor_name=invite.instructor_name,
        business_name=invite.business_name,
        location_label=invite.location_label,
        preferred_duration_mins=invite.preferred_duration_mins,
        allowed_durations_mins=list(invite.allowed_durations_mins),
        expires_at=invite.expires_at,
    )


def search_public_availability(
    invite: InviteContext,
    request: PublicAvailabilitySearchRequest,
) -> PublicAvailabilitySearchResponse:
    is_facility = request.location_type == "facility"
    slots = _find_slots(
        instructor_id=invite.instructor_id,
        requested_date=request.requested_date,
        client_address=request.client_address,
        is_instructor_facility=is_facility,
        duration_mins=request.duration_mins,
    )
    location = _resolve_location(invite.instructor_id, request.client_address, is_facility)
    return PublicAvailabilitySearchResponse(
        status="PRESENTING",
        requested_date=request.requested_date,
        duration_mins=request.duration_mins,
        location_label=location.address if is_facility else "Client pool",
        available_slots=[_slot_response(slot) for slot in slots],
    )


def create_public_lesson_request(
    invite: InviteContext,
    request: PublicLessonRequestCreate,
) -> PublicLessonRequestResponse:
    existing = resolve_lesson_request_by_idempotency_key(request.idempotency_key)
    if existing:
        if (
            existing.invite_token != invite.token
            or existing.client_name != request.client_name
            or existing.client_address != request.client_address
            or existing.requested_date != request.requested_date
            or existing.selected_slot_start != request.slot_start
            or existing.duration_mins != request.duration_mins
            or existing.location_type != request.location_type
        ):
            raise IdempotencyConflict()
        return PublicLessonRequestResponse(
            request_token=existing.request_token,
            status="pending",
            selected_slot=TimeSlotResponse(
                start_iso=existing.selected_slot_start,
                end_iso=existing.selected_slot_end,
                display=_format_slot_display(
                    existing.selected_slot_start,
                    existing.selected_slot_end,
                    existing.travel_time_mins,
                    existing.buffer_mins,
                    existing.distance_miles,
                ),
                travel_time_mins=existing.travel_time_mins,
                buffer_mins=existing.buffer_mins,
                distance_miles=existing.distance_miles,
            ),
            message="Your lesson request was sent to the instructor for confirmation.",
        )

    is_facility = request.location_type == "facility"
    selected_slot = _require_available_start(
        instructor_id=invite.instructor_id,
        requested_date=request.requested_date,
        client_address=request.client_address,
        is_instructor_facility=is_facility,
        duration_mins=request.duration_mins,
        requested_start=request.slot_start,
    )
    location = _resolve_location(invite.instructor_id, request.client_address, is_facility)
    try:
        record = create_lesson_request(
            invite_token=invite.token,
            instructor_id=invite.instructor_id,
            client_name=request.client_name,
            client_address=request.client_address,
            requested_date=request.requested_date,
            selected_slot_start=request.slot_start,
            selected_slot_end=selected_slot.end_time,
            duration_mins=request.duration_mins,
            travel_time_mins=selected_slot.travel_time_mins,
            buffer_mins=selected_slot.buffer_mins,
            distance_miles=selected_slot.distance_miles,
            location_type=request.location_type,
            lat=location.lat,
            lng=location.lng,
            idempotency_key=request.idempotency_key,
        )
    except db.IdempotencyKeyConflictError as exc:
        raise IdempotencyConflict() from exc
    return PublicLessonRequestResponse(
        request_token=record.request_token,
        status="pending",
        selected_slot=_slot_response(selected_slot),
        message="Your lesson request was sent to the instructor for confirmation.",
    )


def get_public_request_status(request_token: str) -> PublicRequestStatusResponse:
    record = resolve_lesson_request(request_token)
    return PublicRequestStatusResponse(
        request_token=record.request_token,
        status="pending",
        message="Your lesson request is waiting for instructor confirmation.",
    )


def _find_slots(
    instructor_id: str,
    requested_date: date,
    client_address: str,
    is_instructor_facility: bool,
    duration_mins: int,
) -> list[TimeSlotDTO]:
    try:
        return tools.find_available_swim_slots(
            instructor_id=instructor_id,
            date_str=requested_date.isoformat(),
            client_address=client_address,
            is_instructor_facility=is_instructor_facility,
            duration_mins=duration_mins,
        )
    except GeocodeFailed as exc:
        raise GeocodingFailedApiError({"field": "client_address"}) from exc
    except RoutingTimeout as exc:
        raise RoutingTimeoutApiError() from exc
    except RoutingProviderUnavailable as exc:
        raise RoutingProviderUnavailableApiError() from exc
    except RoutingProviderNotConfigured as exc:
        raise RoutingConfigurationError() from exc
    except RouteEstimateFailed as exc:
        raise RoutingEstimateFailedApiError() from exc
    except ValueError as exc:
        message = str(exc)
        if "not found" in message.lower():
            raise ResourceNotFound("The instructor could not be found.") from exc
        raise InvalidSchedulingRequest("The scheduling request could not be evaluated.") from exc


def _require_available_start(
    instructor_id: str,
    requested_date: date,
    client_address: str,
    is_instructor_facility: bool,
    duration_mins: int,
    requested_start: datetime,
) -> TimeSlotDTO:
    slots = _find_slots(
        instructor_id=instructor_id,
        requested_date=requested_date,
        client_address=client_address,
        is_instructor_facility=is_instructor_facility,
        duration_mins=duration_mins,
    )
    for slot in slots:
        if slot.start_time == requested_start:
            return slot
    raise SchedulingConflict(
        details={
            "requested_start": requested_start.isoformat(),
            "requested_date": requested_date.isoformat(),
        }
    )


def _resolve_location(
    instructor_id: str,
    client_address: str,
    is_instructor_facility: bool,
) -> LocationResponse:
    instructor = db.get_instructor_info(instructor_id)
    if not instructor:
        raise ResourceNotFound("The instructor could not be found.")
    if is_instructor_facility:
        return LocationResponse(
            address=instructor.main_address,
            lat=instructor.lat,
            lng=instructor.lng,
            is_instructor_facility=True,
        )
    try:
        lat, lng = tools.routing_provider.geocode(client_address)
    except GeocodeFailed as exc:
        raise GeocodingFailedApiError({"field": "client_address"}) from exc
    except RoutingProviderNotConfigured as exc:
        raise RoutingConfigurationError() from exc
    return LocationResponse(
        address=client_address,
        lat=lat,
        lng=lng,
        is_instructor_facility=False,
    )


def _slot_response(slot: TimeSlotDTO) -> TimeSlotResponse:
    return TimeSlotResponse(
        start_iso=slot.start_time,
        end_iso=slot.end_time,
        display=slot.format_display(),
        travel_time_mins=slot.travel_time_mins,
        buffer_mins=slot.buffer_mins,
        distance_miles=slot.distance_miles,
    )


def _format_slot_display(
    start_time: datetime,
    end_time: datetime,
    travel_time_mins: int,
    buffer_mins: int,
    distance_miles: float,
) -> str:
    return (
        f"{start_time.strftime('%I:%M %p')} - {end_time.strftime('%I:%M %p')} "
        f"(Travel: {travel_time_mins}m, Buffer: {buffer_mins}m, Dist: {distance_miles:.1f}mi)"
    )


def _client_id_from_name(client_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", client_name.strip().lower()).strip("_")
    return f"client_{slug or 'guest'}"


def _duration_delta(duration_mins: int) -> timedelta:
    return timedelta(minutes=duration_mins)
