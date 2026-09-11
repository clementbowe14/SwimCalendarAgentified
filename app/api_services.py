from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import re
from typing import Optional
import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import db, timezones, tools
from app.api_errors import (
    AlternativeSlotContextMismatch,
    GeocodingFailed as GeocodingFailedApiError,
    IdempotencyConflict,
    InvalidSchedulingRequest,
    LessonRequestConfirmConflict,
    LessonRequestExpired,
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
    CoachLessonRequestAlternativesRequest,
    CoachLessonRequestConfirmResponse,
    CoachLessonRequestConfirmRequest,
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
from app.models import AppointmentDB, InviteSlotOfferDB, LessonRequestDB, TimeSlotDTO
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
        timezone=instructor.timezone,
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
    instructor = db.get_instructor_info(request["instructor_id"])
    timezone_name = instructor.timezone if instructor else timezones.DEFAULT_INSTRUCTOR_TIMEZONE
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
            start_time=timezones.storage_to_local(request["slot_start"], timezone_name),
            end_time=timezones.storage_to_local(request["slot_end"], timezone_name),
            travel_time_mins=request["travel_time_mins"],
            buffer_mins=request["buffer_mins"],
            distance_miles=request["distance_miles"],
            target_location=location,
        ),
        source="client_invite",
        status=_request_status(request["status"], request.get("computed_expired", False)),
        risk="at_risk" if request.get("validation_failure_code") else "clear",
        submitted_at=request["created_at"],
        note="Client selected this time from an invite.",
        alternative_slots=[
            CoachTimeSlotResponse(
                id=offer["id"],
                start_time=timezones.storage_to_local(offer["slot_start"], timezone_name),
                end_time=timezones.storage_to_local(offer["slot_end"], timezone_name),
                travel_time_mins=offer["travel_time_mins"],
                buffer_mins=offer["buffer_mins"],
                distance_miles=offer["distance_miles"],
                target_location=LocationResponse(
                    address=offer["client_address"],
                    lat=offer["lat"],
                    lng=offer["lng"],
                    is_instructor_facility=offer["location_type"] == "facility",
                ),
            )
            for offer in request.get("alternative_slots", [])
        ],
    )


def _lesson_status(status: str) -> str:
    if status == "cancelled":
        return "canceled"
    return "scheduled"


def _request_status(status: str, computed_expired: bool = False) -> str:
    if computed_expired:
        return "expired"
    if status in {"accepted", "confirmed"}:
        return "approved"
    if status == "alternatives_sent":
        return "needs_alternatives"
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
    instructor = db.get_instructor_info(instructor_id)
    if not instructor:
        raise ResourceNotFound("The instructor could not be found.")
    try:
        requested_start = _canonical_request_time(request.start_time, instructor.timezone)
    except ValueError as exc:
        raise InvalidSchedulingRequest("The requested local time is invalid for the instructor timezone.") from exc
    requested_start_storage = timezones.to_utc_storage(requested_start)
    requested_end_storage = requested_start_storage + _duration_delta(request.duration_mins)
    existing = db.get_appointment_by_idempotency_key(request.idempotency_key)
    if existing:
        if (
            existing["instructor_id"] != instructor_id
            or existing["client_id"] != _client_id_from_name(request.client_name)
            or timezones.to_utc_storage(existing["start_time"]) != requested_start_storage
            or timezones.to_utc_storage(existing["end_time"]) != requested_end_storage
        ):
            raise IdempotencyConflict()
        return BookLessonResponse(
            success=True,
            appointment_id=existing["id"],
            message=f"Successfully booked swim lesson for {request.client_name} on {timezones.storage_to_local(existing['start_time'], instructor.timezone).strftime('%b %d, %Y at %I:%M %p')}!",
        )

    _require_available_start(
        instructor_id=instructor_id,
        requested_date=timezones.requested_local_date(requested_start, instructor.timezone),
        client_address=request.client_address,
        is_instructor_facility=request.is_instructor_facility,
        duration_mins=request.duration_mins,
        requested_start=requested_start,
    )
    location = _resolve_location(instructor_id, request.client_address, request.is_instructor_facility)
    try:
        result = tools.book_swim_lesson(
            instructor_id=instructor_id,
            client_id=_client_id_from_name(request.client_name),
            client_name=request.client_name,
            start_time_iso=requested_start.isoformat(),
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
) -> CoachLessonRequestActionResponse | CoachLessonRequestConfirmResponse:
    return confirm_coach_lesson_request(
        instructor_id,
        request_token,
        CoachLessonRequestConfirmRequest(),
        legacy_response=True,
    )


def confirm_coach_lesson_request(
    instructor_id: str,
    request_token: str,
    request: CoachLessonRequestConfirmRequest,
    *,
    legacy_response: bool = False,
) -> CoachLessonRequestActionResponse:
    session = db.SessionLocal()
    try:
        lesson_request = _query_coach_lesson_request(session, instructor_id, request_token).with_for_update().first()
        if lesson_request is None:
            raise LessonRequestNotFound()
        if _is_request_expired(lesson_request):
            raise LessonRequestExpired()

        existing_appointments = _appointments_for_request(session, lesson_request.id)
        if lesson_request.status == "confirmed" or existing_appointments:
            appointment_ids = [appointment.id for appointment in existing_appointments]
            if not _confirmed_slot_matches(lesson_request, request.slot_id):
                raise LessonRequestConfirmConflict()
            return _lesson_request_action_response(
                lesson_request,
                appointment_ids,
                status="approved" if legacy_response else "confirmed",
            )
        if lesson_request.status not in {"pending", "alternatives_sent"}:
            raise InvalidSchedulingRequest("Only pending lesson requests can be confirmed.")

        selected_offer = _resolve_confirm_offer(session, lesson_request, request.slot_id)
        if selected_offer is not None:
            _apply_offer_to_request(lesson_request, selected_offer)
        occurrences = _request_occurrences(lesson_request, selected_offer)
        _acquire_confirmation_locks(session, lesson_request.instructor_id, occurrences)

        try:
            _validate_occurrences(lesson_request, occurrences)
            appointments = _insert_occurrence_appointments(session, lesson_request, occurrences)
        except SchedulingConflict:
            session.rollback()
            _persist_validation_failure(
                lesson_request.id,
                "SLOT_CONFLICT",
                {"request_token": request_token},
            )
            raise
        except IntegrityError as exc:
            session.rollback()
            _persist_validation_failure(
                lesson_request.id,
                "SLOT_CONFLICT",
                {"request_token": request_token},
            )
            raise SchedulingConflict() from exc

        now = datetime.utcnow()
        lesson_request.status = "confirmed"
        lesson_request.confirmed_at = now
        lesson_request.confirmed_slot_offer_id = selected_offer.id if selected_offer is not None else None
        lesson_request.validation_failure_code = None
        lesson_request.validation_failure_details = None
        lesson_request.validation_failed_at = None
        lesson_request.client_notification_status = "queued"
        lesson_request.client_notification_message = "Your lesson request was confirmed."
        lesson_request.client_notified_at = now
        lesson_request.updated_at = now
        session.commit()
        return _lesson_request_action_response(
            lesson_request,
            [appointment.id for appointment in appointments],
            status="approved" if legacy_response else "confirmed",
        )
    finally:
        session.close()


def request_alternatives_for_coach_lesson_request(
    instructor_id: str,
    request_token: str,
    request: Optional[CoachLessonRequestAlternativesRequest] = None,
) -> CoachLessonRequestActionResponse:
    body = request or CoachLessonRequestAlternativesRequest()
    session = db.SessionLocal()
    try:
        lesson_request = _query_coach_lesson_request(session, instructor_id, request_token).with_for_update().first()
        if lesson_request is None:
            raise LessonRequestNotFound()
        if _is_request_expired(lesson_request):
            raise LessonRequestExpired()
        if lesson_request.status == "confirmed" or _appointments_for_request(session, lesson_request.id):
            raise InvalidSchedulingRequest("Confirmed lesson requests cannot be marked for new times.")
        promoted = _promote_alternative_offers(session, lesson_request, body)
        now = datetime.utcnow()
        lesson_request.status = "alternatives_sent"
        lesson_request.alternatives_sent_at = now
        lesson_request.validation_failure_code = None
        lesson_request.validation_failure_details = None
        lesson_request.validation_failed_at = None
        lesson_request.client_notification_status = "queued"
        lesson_request.client_notification_message = body.message or "Please choose a new lesson time."
        lesson_request.client_notified_at = now if promoted or body.message else None
        lesson_request.updated_at = now
        session.commit()
    finally:
        session.close()
    return CoachLessonRequestActionResponse(
        id=request_token,
        status="needs_alternatives",
        appointment_id=None,
        message="Client should choose new lesson times.",
    )


def decline_coach_lesson_request(
    instructor_id: str,
    request_token: str,
) -> CoachLessonRequestActionResponse:
    session = db.SessionLocal()
    try:
        lesson_request = _query_coach_lesson_request(session, instructor_id, request_token).with_for_update().first()
        if lesson_request is None:
            raise LessonRequestNotFound()
        if _is_request_expired(lesson_request):
            raise LessonRequestExpired()
        if lesson_request.status == "confirmed" or _appointments_for_request(session, lesson_request.id):
            raise InvalidSchedulingRequest("Confirmed lesson requests cannot be declined.")
        lesson_request.status = "declined"
        lesson_request.declined_at = datetime.utcnow()
        lesson_request.updated_at = datetime.utcnow()
        session.commit()
        return CoachLessonRequestActionResponse(
            id=lesson_request.request_token,
            status="declined",
            appointment_id=None,
            message="Lesson request was declined.",
        )
    finally:
        session.close()


def _query_coach_lesson_request(session, instructor_id: str, request_token: str):
    return session.query(LessonRequestDB).filter(
        LessonRequestDB.instructor_id == instructor_id,
        LessonRequestDB.request_token == request_token,
    )


def _appointments_for_request(session, request_id: str) -> list[AppointmentDB]:
    return (
        session.query(AppointmentDB)
        .filter_by(request_id=request_id)
        .order_by(AppointmentDB.start_time.asc())
        .all()
    )


def _lesson_request_action_response(
    lesson_request: LessonRequestDB,
    appointment_ids: list[str],
    *,
    status: str,
) -> CoachLessonRequestActionResponse | CoachLessonRequestConfirmResponse:
    if status == "confirmed":
        return CoachLessonRequestConfirmResponse(
            id=lesson_request.request_token,
            status="confirmed",
            appointment_id=appointment_ids[0] if appointment_ids else None,
            appointment_ids=appointment_ids,
            message="Lesson request is confirmed.",
        )
    return CoachLessonRequestActionResponse(
        id=lesson_request.request_token,
        status=status,
        appointment_id=appointment_ids[0] if appointment_ids else None,
        message="Lesson request is confirmed.",
    )


def _is_request_expired(lesson_request: LessonRequestDB) -> bool:
    return lesson_request.status not in {"confirmed"} and lesson_request.expires_at <= datetime.utcnow()


def _instructor_timezone(instructor_id: str) -> str:
    instructor = db.get_instructor_info(instructor_id)
    return instructor.timezone if instructor else timezones.DEFAULT_INSTRUCTOR_TIMEZONE


def _confirmed_slot_matches(lesson_request: LessonRequestDB, slot_id: Optional[str]) -> bool:
    if slot_id is None:
        return True
    return lesson_request.confirmed_slot_offer_id == slot_id


def _resolve_confirm_offer(
    session,
    lesson_request: LessonRequestDB,
    slot_id: Optional[str],
) -> Optional[InviteSlotOfferDB]:
    if slot_id is None:
        if lesson_request.original_slot_offer_id is None:
            return None
        return session.query(InviteSlotOfferDB).filter_by(id=lesson_request.original_slot_offer_id).first()
    offer = (
        session.query(InviteSlotOfferDB)
        .filter(
            InviteSlotOfferDB.id == slot_id,
            InviteSlotOfferDB.lesson_request_id == lesson_request.id,
            InviteSlotOfferDB.invite_token == lesson_request.invite_token,
            InviteSlotOfferDB.instructor_id == lesson_request.instructor_id,
            InviteSlotOfferDB.offer_kind == "alternative",
            InviteSlotOfferDB.expires_at > datetime.utcnow(),
        )
        .first()
    )
    if offer is None:
        raise InvalidSchedulingRequest("The selected confirmation slot is not valid for this request.")
    return offer


def _request_occurrences(
    lesson_request: LessonRequestDB,
    selected_offer: Optional[InviteSlotOfferDB],
) -> list[tuple[datetime, datetime]]:
    instructor = db.get_instructor_info(lesson_request.instructor_id)
    timezone_name = instructor.timezone if instructor else timezones.DEFAULT_INSTRUCTOR_TIMEZONE
    source_start = selected_offer.slot_start if selected_offer is not None else lesson_request.slot_start
    source_end = selected_offer.slot_end if selected_offer is not None else lesson_request.slot_end
    base_local_start = timezones.storage_to_local(source_start, timezone_name).replace(tzinfo=None)
    base_local_end = timezones.storage_to_local(source_end, timezone_name).replace(tzinfo=None)
    count = lesson_request.recurrence_count if lesson_request.recurrence == "weekly" else 1
    occurrences: list[tuple[datetime, datetime]] = []
    for index in range(count):
        local_start = base_local_start + timedelta(days=7 * index)
        local_end = base_local_end + timedelta(days=7 * index)
        start = timezones.validate_local_datetime(local_start, timezone_name)
        end = timezones.validate_local_datetime(local_end, timezone_name)
        occurrences.append((timezones.to_utc_storage(start), timezones.to_utc_storage(end)))
    return occurrences


def _apply_offer_to_request(lesson_request: LessonRequestDB, offer: InviteSlotOfferDB) -> None:
    lesson_request.slot_start = offer.slot_start
    lesson_request.slot_end = offer.slot_end
    lesson_request.requested_date = offer.requested_date
    lesson_request.travel_time_mins = offer.travel_time_mins
    lesson_request.buffer_mins = offer.buffer_mins
    lesson_request.distance_miles = offer.distance_miles
    lesson_request.original_slot_offer_id = offer.id


def _acquire_confirmation_locks(
    session,
    instructor_id: str,
    occurrences: list[tuple[datetime, datetime]],
) -> None:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    instructor = db.get_instructor_info(instructor_id)
    timezone_name = instructor.timezone if instructor else timezones.DEFAULT_INSTRUCTOR_TIMEZONE
    local_dates = set()
    for start, _ in occurrences:
        local_date = timezones.storage_to_local(start, timezone_name).date()
        local_dates.update({local_date - timedelta(days=1), local_date, local_date + timedelta(days=1)})
    for local_date in sorted(local_dates):
        raw = f"{instructor_id}:{local_date.isoformat()}".encode("utf-8")
        key = int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _validate_occurrences(
    lesson_request: LessonRequestDB,
    occurrences: list[tuple[datetime, datetime]],
) -> None:
    for start, _ in occurrences:
        requested_start = timezones.storage_to_aware_utc(start)
        requested_date = timezones.requested_local_date(requested_start, _instructor_timezone(lesson_request.instructor_id))
        _require_available_start(
            instructor_id=lesson_request.instructor_id,
            requested_date=requested_date,
            client_address=lesson_request.client_address,
            is_instructor_facility=lesson_request.location_type == "facility",
            duration_mins=lesson_request.duration_mins,
            requested_start=requested_start,
        )


def _insert_occurrence_appointments(
    session,
    lesson_request: LessonRequestDB,
    occurrences: list[tuple[datetime, datetime]],
) -> list[AppointmentDB]:
    if lesson_request.original_slot_offer_id:
        lesson_request.confirmed_slot_offer_id = lesson_request.original_slot_offer_id
    appointments: list[AppointmentDB] = []
    for index, (start, end) in enumerate(occurrences):
        idempotency_key = f"lesson-request-confirm:{lesson_request.id}:{index}"
        existing = session.query(AppointmentDB).filter_by(idempotency_key=idempotency_key).first()
        if existing is not None:
            appointments.append(existing)
            continue
        conflict = (
            session.query(AppointmentDB)
            .filter(
                AppointmentDB.instructor_id == lesson_request.instructor_id,
                AppointmentDB.status.in_(db.ACTIVE_APPOINTMENT_STATUSES),
                AppointmentDB.cancelled_at.is_(None),
                AppointmentDB.start_time < end,
                AppointmentDB.end_time > start,
            )
            .first()
        )
        if conflict is not None:
            raise SchedulingConflict()
        appointment = AppointmentDB(
            id=f"app_{uuid.uuid4().hex[:8]}",
            instructor_id=lesson_request.instructor_id,
            client_id=lesson_request.client_id or _client_id_from_name(lesson_request.client_name),
            request_id=lesson_request.id,
            title=f"Lesson with {lesson_request.client_name}",
            start_time=start,
            end_time=end,
            location_address=lesson_request.client_address,
            lat=lesson_request.lat,
            lng=lesson_request.lng,
            is_break=False,
            event_type="lesson",
            status="scheduled",
            idempotency_key=idempotency_key,
        )
        session.add(appointment)
        appointments.append(appointment)
    return appointments


def _persist_validation_failure(
    request_id: str,
    code: str,
    details: dict,
) -> None:
    session = db.SessionLocal()
    try:
        lesson_request = session.query(LessonRequestDB).filter_by(id=request_id).first()
        if lesson_request is None:
            return
        lesson_request.validation_failure_code = code
        lesson_request.validation_failure_details = details
        lesson_request.validation_failed_at = datetime.utcnow()
        lesson_request.updated_at = datetime.utcnow()
        session.commit()
    finally:
        session.close()


def _promote_alternative_offers(
    session,
    lesson_request: LessonRequestDB,
    request: CoachLessonRequestAlternativesRequest,
) -> list[InviteSlotOfferDB]:
    if not request.slot_ids:
        return []
    offers = (
        session.query(InviteSlotOfferDB)
        .filter(
            InviteSlotOfferDB.id.in_(request.slot_ids),
            InviteSlotOfferDB.offer_kind == "search",
            InviteSlotOfferDB.lesson_request_id.is_(None),
            InviteSlotOfferDB.expires_at > datetime.utcnow(),
        )
        .all()
    )
    if {offer.id for offer in offers} != set(request.slot_ids):
        raise AlternativeSlotContextMismatch()
    for offer in offers:
        if not _offer_matches_request_context(offer, lesson_request):
            raise AlternativeSlotContextMismatch()
    promoted: list[InviteSlotOfferDB] = []
    now = datetime.utcnow()
    for offer in offers:
        existing = (
            session.query(InviteSlotOfferDB)
            .filter_by(
                id=_alternative_offer_id(lesson_request.id, offer.id),
                lesson_request_id=lesson_request.id,
                offer_kind="alternative",
            )
            .first()
        )
        if existing is not None:
            promoted.append(existing)
            continue
        alternative = InviteSlotOfferDB(
            id=_alternative_offer_id(lesson_request.id, offer.id),
            invite_token=offer.invite_token,
            instructor_id=offer.instructor_id,
            client_name=offer.client_name,
            client_address=offer.client_address,
            requested_date=offer.requested_date,
            slot_start=offer.slot_start,
            slot_end=offer.slot_end,
            duration_mins=offer.duration_mins,
            location_type=offer.location_type,
            location_address=offer.location_address,
            lat=offer.lat,
            lng=offer.lng,
            travel_time_mins=offer.travel_time_mins,
            buffer_mins=offer.buffer_mins,
            distance_miles=offer.distance_miles,
            recurrence=offer.recurrence,
            recurrence_count=offer.recurrence_count,
            recurrence_available=offer.recurrence_available,
            lesson_request_id=lesson_request.id,
            offer_kind="alternative",
            message=request.message,
            notified_at=now,
            expires_at=offer.expires_at,
        )
        session.add(alternative)
        promoted.append(alternative)
    return promoted


def _offer_matches_request_context(offer: InviteSlotOfferDB, lesson_request: LessonRequestDB) -> bool:
    return (
        offer.invite_token == lesson_request.invite_token
        and offer.instructor_id == lesson_request.instructor_id
        and offer.client_name == lesson_request.client_name
        and offer.client_address == lesson_request.client_address
        and offer.location_type == lesson_request.location_type
        and offer.location_address == lesson_request.client_address
        and abs(offer.lat - lesson_request.lat) < 0.000001
        and abs(offer.lng - lesson_request.lng) < 0.000001
        and offer.duration_mins == lesson_request.duration_mins
        and offer.recurrence == lesson_request.recurrence
        and offer.recurrence_count == lesson_request.recurrence_count
        and offer.requested_date == lesson_request.requested_date
    )


def _alternative_offer_id(request_id: str, source_slot_id: str) -> str:
    raw = f"{request_id}:{source_slot_id}".encode("utf-8")
    return f"alt_{hashlib.sha256(raw).hexdigest()[:16]}"


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
    instructor = db.get_instructor_info(invite.instructor_id)
    if not instructor:
        raise ResourceNotFound("The instructor could not be found.")
    try:
        requested_start = _canonical_request_time(request.slot_start, instructor.timezone)
    except ValueError as exc:
        raise InvalidSchedulingRequest("The requested local time is invalid for the instructor timezone.") from exc
    requested_date = timezones.requested_local_date(requested_start, instructor.timezone)
    existing = resolve_lesson_request_by_idempotency_key(request.idempotency_key)
    if existing:
        if (
            existing.invite_token != invite.token
            or existing.client_name != request.client_name
            or existing.client_address != request.client_address
            or existing.requested_date != requested_date
            or timezones.to_utc_storage(existing.selected_slot_start) != timezones.to_utc_storage(requested_start)
            or existing.duration_mins != request.duration_mins
            or existing.location_type != request.location_type
        ):
            raise IdempotencyConflict()
        return PublicLessonRequestResponse(
            request_token=existing.request_token,
            status="pending",
            selected_slot=TimeSlotResponse(
                start_iso=timezones.storage_to_local(existing.selected_slot_start, instructor.timezone),
                end_iso=timezones.storage_to_local(existing.selected_slot_end, instructor.timezone),
                display=_format_slot_display(
                    timezones.storage_to_local(existing.selected_slot_start, instructor.timezone),
                    timezones.storage_to_local(existing.selected_slot_end, instructor.timezone),
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
        requested_date=requested_date,
        client_address=request.client_address,
        is_instructor_facility=is_facility,
        duration_mins=request.duration_mins,
        requested_start=requested_start,
    )
    location = _resolve_location(invite.instructor_id, request.client_address, is_facility)
    try:
        record = create_lesson_request(
            invite_token=invite.token,
            instructor_id=invite.instructor_id,
            client_name=request.client_name,
            client_address=request.client_address,
            requested_date=requested_date,
            selected_slot_start=timezones.to_utc_storage(requested_start),
            selected_slot_end=timezones.to_utc_storage(selected_slot.end_time),
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
    if record.status != "confirmed" and record.expires_at <= datetime.utcnow():
        status = "expired"
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
    if status in {"accepted", "confirmed"}:
        return "confirmed"
    if status in {"alternatives_sent", "declined", "cancelled"}:
        return "needs_new_times"
    return "pending"


def _legacy_public_request_message(status: str) -> str:
    if status == "confirmed":
        return "Your lesson request was confirmed by the instructor."
    if status == "needs_new_times":
        return "The instructor needs you to choose new lesson times."
    if status == "expired":
        return "This lesson request has expired. Please choose a new time."
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
    requested_storage = timezones.to_utc_storage(requested_start)
    for slot in slots:
        if timezones.to_utc_storage(slot.start_time) == requested_storage:
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
    except RoutingTimeout as exc:
        raise RoutingTimeoutApiError() from exc
    except RoutingProviderUnavailable as exc:
        raise RoutingProviderUnavailableApiError() from exc
    except RoutingProviderNotConfigured as exc:
        raise RoutingConfigurationError() from exc
    except RouteEstimateFailed as exc:
        raise RoutingEstimateFailedApiError() from exc
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


def _canonical_request_time(value: datetime, timezone_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return timezones.validate_local_datetime(value, timezone_name)
    return value
