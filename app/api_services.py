from datetime import date, datetime, timedelta
import re
from typing import Optional

from app import db, tools
from app.api_errors import (
    GeocodingFailed as GeocodingFailedApiError,
    IdempotencyConflict,
    InvalidSchedulingRequest,
    LessonRequestNotFound,
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
    CoachClientResponse,
    CoachCommandCenterSnapshot,
    CoachDistanceBufferTier,
    CoachLessonRequestActionResponse,
    CoachLessonRequestResponse,
    CoachScheduleItemResponse,
    CoachScheduleResponse,
    CoachSchedulerPayments,
    CoachSchedulerRules,
    CoachTimeSlotResponse,
    CoachInstructorProfileResponse,
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
from app.models import AppointmentDB, LessonRequestDB, TimeSlotDTO
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


def get_coach_command_center_snapshot(
    instructor_id: str,
    snapshot_date: date,
) -> CoachCommandCenterSnapshot:
    instructor = get_instructor_profile(instructor_id)
    coach_instructor = CoachInstructorProfileResponse(
        **instructor.model_dump(),
        business_name=_business_name_for_instructor(instructor_id),
    )
    clients = [
        _client_response(client, coach_instructor)
        for client in db.get_clients_for_instructor(instructor_id)
    ]
    schedule = [
        _schedule_item_response(appointment, coach_instructor)
        for appointment in db.get_appointments_for_date(instructor_id, snapshot_date.isoformat())
    ]
    requests = [
        _coach_lesson_request_response(request)
        for request in db.get_pending_lesson_requests_for_instructor(instructor_id)
    ]
    return CoachCommandCenterSnapshot(
        instructor=coach_instructor,
        date=snapshot_date,
        clients=clients,
        schedule=schedule,
        requests=requests,
        rules=get_coach_scheduler_settings(instructor_id),
    )


def get_coach_scheduler_settings(instructor_id: str) -> CoachSchedulerRules:
    saved = db.get_scheduler_settings(instructor_id)
    if saved is None:
        return _default_scheduler_rules()
    return CoachSchedulerRules.model_validate(saved)


def update_coach_scheduler_settings(
    instructor_id: str,
    rules: CoachSchedulerRules,
) -> CoachSchedulerRules:
    saved = db.save_scheduler_settings(instructor_id, rules.model_dump(mode="json"))
    return CoachSchedulerRules.model_validate(saved)


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


def _business_name_for_instructor(instructor_id: str) -> str:
    session = db.SessionLocal()
    try:
        invite = (
            session.query(db.InviteTokenDB)
            .filter_by(instructor_id=instructor_id, business_name="Swim Command")
            .first()
        )
        if invite:
            return invite.business_name
        invite = (
            session.query(db.InviteTokenDB)
            .filter_by(instructor_id=instructor_id)
            .order_by(db.InviteTokenDB.created_at.desc())
            .first()
        )
        return invite.business_name if invite else "Swim Command"
    finally:
        session.close()


def _client_response(client: dict, instructor: CoachInstructorProfileResponse) -> CoachClientResponse:
    address = client["default_location_address"] or instructor.main_address
    lat = client["default_lat"] if client["default_lat"] is not None else instructor.lat
    lng = client["default_lng"] if client["default_lng"] is not None else instructor.lng
    is_facility = address == instructor.main_address
    return CoachClientResponse(
        id=client["id"],
        name=client["name"],
        phone=client["phone"] or "",
        email=client["email"] or "",
        default_location=LocationResponse(
            address=address,
            lat=lat,
            lng=lng,
            is_instructor_facility=is_facility,
        ),
        location_mode="instructor_facility" if is_facility else "client_home",
        lesson_duration_mins=45,
        history_summary="Loaded from client profile",
    )


def _schedule_item_response(
    appointment: dict,
    instructor: CoachInstructorProfileResponse,
) -> CoachScheduleItemResponse:
    is_break = appointment["is_break"] or appointment["event_type"] == "block"
    is_facility = is_break or appointment["location_address"] == instructor.main_address
    title = appointment["title"]
    if not is_break:
        title = title.replace("Lesson with ", "", 1)
    return CoachScheduleItemResponse(
        id=appointment["id"],
        kind="break" if is_break else "lesson",
        client_id=None if is_break else appointment["client_id"],
        title=title,
        start_time=appointment["start_time"],
        end_time=appointment["end_time"],
        location=LocationResponse(
            address=appointment["location_address"],
            lat=appointment["lat"],
            lng=appointment["lng"],
            is_instructor_facility=is_facility,
        ),
        status=None if is_break else _lesson_status(appointment["status"]),
    )


def _coach_lesson_request_response(request: dict) -> CoachLessonRequestResponse:
    location = LocationResponse(
        address=request["client_address"],
        lat=request["lat"],
        lng=request["lng"],
        is_instructor_facility=request["location_type"] == "facility",
    )
    return CoachLessonRequestResponse(
        id=request["request_token"],
        client_id=request["client_id"] or _client_id_from_name(request["client_name"]),
        client_name=request["client_name"],
        requested_slot=CoachTimeSlotResponse(
            id=f"slot_{request['request_token']}",
            start_time=request["slot_start"],
            end_time=request["slot_end"],
            travel_time_mins=request["travel_time_mins"],
            buffer_mins=request["buffer_mins"],
            distance_miles=request["distance_miles"],
            target_location=location,
        ),
        source="client_invite",
        status=_request_status(request["status"]),
        risk="clear",
        submitted_at=request["created_at"],
        note="Client selected this time from an invite.",
        alternative_slots=[],
    )


def _lesson_status(status: str) -> str:
    if status == "cancelled":
        return "canceled"
    return "scheduled"


def _request_status(status: str) -> str:
    if status == "accepted":
        return "approved"
    if status in {"declined", "cancelled"}:
        return "declined"
    return "pending"


def _default_scheduler_rules() -> CoachSchedulerRules:
    return CoachSchedulerRules(
        rounding_increment_mins=15,
        post_break_buffer_mins=15,
        distance_buffer_tiers=[
            CoachDistanceBufferTier(
                id="near",
                label="Under 5 mi",
                min_miles=0,
                max_miles=4.999,
                buffer_mins=10,
            ),
            CoachDistanceBufferTier(
                id="medium",
                label="5-10 mi",
                min_miles=5,
                max_miles=10,
                buffer_mins=20,
            ),
            CoachDistanceBufferTier(
                id="far",
                label="Over 10 mi",
                min_miles=10.001,
                max_miles=None,
                buffer_mins=30,
            ),
        ],
        payments=CoachSchedulerPayments(
            enabled_methods=["card", "ach", "cash"],
            cancellation_window_hours=12,
            require_payment_method_for_new_clients=True,
        ),
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


def approve_coach_lesson_request(
    instructor_id: str,
    request_token: str,
) -> CoachLessonRequestActionResponse:
    lesson_request, appointment = _load_coach_lesson_request(instructor_id, request_token)
    if appointment is not None:
        _mark_lesson_request_status(lesson_request.id, "accepted")
        return CoachLessonRequestActionResponse(
            id=lesson_request.request_token,
            status="approved",
            appointment_id=appointment.id,
            message="Lesson request is confirmed.",
        )
    if lesson_request.status in {"declined", "cancelled"}:
        raise InvalidSchedulingRequest("Only pending lesson requests can be approved.")
    if db.has_active_schedule_conflict(
        instructor_id=lesson_request.instructor_id,
        start_time=lesson_request.slot_start,
        end_time=lesson_request.slot_end,
    ):
        raise SchedulingConflict()
    try:
        appointment_id = db.save_appointment(
            instructor_id=lesson_request.instructor_id,
            client_id=lesson_request.client_id or _client_id_from_name(lesson_request.client_name),
            title=f"Lesson with {lesson_request.client_name}",
            start_time=lesson_request.slot_start,
            end_time=lesson_request.slot_end,
            location_address=lesson_request.client_address,
            lat=lesson_request.lat,
            lng=lesson_request.lng,
            idempotency_key=f"lesson-request-approval:{lesson_request.id}",
            request_id=lesson_request.id,
        )
    except db.ScheduleConflictError as exc:
        raise SchedulingConflict() from exc
    except db.IdempotencyKeyConflictError as exc:
        raise IdempotencyConflict() from exc
    _mark_lesson_request_status(lesson_request.id, "accepted")
    return CoachLessonRequestActionResponse(
        id=lesson_request.request_token,
        status="approved",
        appointment_id=appointment_id,
        message="Lesson request is confirmed.",
    )


def request_alternatives_for_coach_lesson_request(
    instructor_id: str,
    request_token: str,
) -> CoachLessonRequestActionResponse:
    lesson_request, appointment = _load_coach_lesson_request(instructor_id, request_token)
    if appointment is not None or lesson_request.status == "accepted":
        raise InvalidSchedulingRequest("Confirmed lesson requests cannot be marked for new times.")
    _mark_lesson_request_status(lesson_request.id, "declined")
    return CoachLessonRequestActionResponse(
        id=lesson_request.request_token,
        status="needs_alternatives",
        appointment_id=None,
        message="Client should choose new lesson times.",
    )


def decline_coach_lesson_request(
    instructor_id: str,
    request_token: str,
) -> CoachLessonRequestActionResponse:
    lesson_request, appointment = _load_coach_lesson_request(instructor_id, request_token)
    if appointment is not None or lesson_request.status == "accepted":
        raise InvalidSchedulingRequest("Confirmed lesson requests cannot be declined.")
    _mark_lesson_request_status(lesson_request.id, "declined")
    return CoachLessonRequestActionResponse(
        id=lesson_request.request_token,
        status="declined",
        appointment_id=None,
        message="Lesson request was declined.",
    )


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
    status = _legacy_public_request_status(record.status)
    return PublicRequestStatusResponse(
        request_token=record.request_token,
        status=status,
        message=_legacy_public_request_message(status),
    )


def _load_coach_lesson_request(
    instructor_id: str,
    request_token: str,
) -> tuple[LessonRequestDB, Optional[AppointmentDB]]:
    session = db.SessionLocal()
    try:
        lesson_request = (
            session.query(LessonRequestDB)
            .filter(
                LessonRequestDB.instructor_id == instructor_id,
                LessonRequestDB.request_token == request_token,
            )
            .first()
        )
        if lesson_request is None:
            raise LessonRequestNotFound()
        appointment = session.query(AppointmentDB).filter_by(request_id=lesson_request.id).first()
        session.expunge(lesson_request)
        if appointment is not None:
            session.expunge(appointment)
        return lesson_request, appointment
    finally:
        session.close()


def _mark_lesson_request_status(request_id: str, status: str) -> None:
    session = db.SessionLocal()
    try:
        lesson_request = session.query(LessonRequestDB).filter_by(id=request_id).one()
        lesson_request.status = status
        lesson_request.updated_at = datetime.utcnow()
        session.commit()
    finally:
        session.close()


def _legacy_public_request_status(status: str) -> str:
    if status == "accepted":
        return "confirmed"
    if status in {"declined", "cancelled"}:
        return "needs_new_times"
    return "pending"


def _legacy_public_request_message(status: str) -> str:
    if status == "confirmed":
        return "Your lesson request was confirmed by the instructor."
    if status == "needs_new_times":
        return "The instructor needs you to choose new lesson times."
    return "Your lesson request is waiting for instructor confirmation."


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
