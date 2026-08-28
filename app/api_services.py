from datetime import date, datetime
import re

from app import db, tools
from app.api_errors import InvalidSchedulingRequest, ResourceNotFound, SchedulingConflict
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
    resolve_lesson_request,
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
    _require_available_start(
        instructor_id=instructor_id,
        requested_date=request.start_time.date(),
        client_address=request.client_address,
        is_instructor_facility=request.is_instructor_facility,
        duration_mins=request.duration_mins,
        requested_start=request.start_time,
    )
    location = _resolve_location(instructor_id, request.client_address, request.is_instructor_facility)
    result = tools.book_swim_lesson(
        instructor_id=instructor_id,
        client_id=_client_id_from_name(request.client_name),
        client_name=request.client_name,
        start_time_iso=request.start_time.isoformat(),
        duration_mins=request.duration_mins,
        location_address=location.address,
    )
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
    is_facility = request.location_type == "facility"
    selected_slot = _require_available_start(
        instructor_id=invite.instructor_id,
        requested_date=request.requested_date,
        client_address=request.client_address,
        is_instructor_facility=is_facility,
        duration_mins=request.duration_mins,
        requested_start=request.slot_start,
    )
    record = create_lesson_request(
        invite_token=invite.token,
        client_name=request.client_name,
        selected_slot_start=request.slot_start,
    )
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
    lat, lng = tools.routing_provider.geocode(client_address)
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


def _client_id_from_name(client_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", client_name.strip().lower()).strip("_")
    return f"client_{slug or 'guest'}"
