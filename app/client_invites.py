import datetime
import hashlib
import urllib.parse
from typing import Optional

from app import db, tools
from app.api_errors import (
    CalendarLinksUnavailable,
    GeocodingFailed as GeocodingFailedApiError,
    IdempotencyConflict,
    InvalidSchedulingRequest,
    LessonRequestNotFound,
    ResourceNotFound,
    RoutingConfigurationError,
    RoutingEstimateFailed as RoutingEstimateFailedApiError,
    RoutingProviderUnavailable as RoutingProviderUnavailableApiError,
    RoutingTimeout as RoutingTimeoutApiError,
    SlotOfferNotFound,
)
from app.api_schemas import (
    ClientInviteAvailabilityRequest,
    ClientInviteAvailabilityResponse,
    ClientInviteCalendarLinks,
    ClientInviteEnvelopeResponse,
    ClientInviteIntake,
    ClientInviteLessonConfirmation,
    ClientInviteLessonRequestCreate,
    ClientInviteLessonRequestResponse,
    ClientInviteLocation,
    ClientInviteSchedulerSlot,
    ClientInviteUnavailableWindow,
)
from app.models import AppointmentDB, InviteIntakeDB, InviteSlotOfferDB, LessonRequestDB, TimeSlotDTO
from app.public_tokens import InviteContext, create_lesson_request, resolve_invite
from app.routing import (
    GeocodeFailed,
    RouteEstimateFailed,
    RoutingProviderNotConfigured,
    RoutingProviderUnavailable,
    RoutingTimeout,
)


SLOT_OFFER_TTL_MINUTES = 30
DAY_KEYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def get_client_invite_envelope(invite_token: str) -> ClientInviteEnvelopeResponse:
    invite = resolve_invite(invite_token)
    return _envelope_response(invite, _load_or_default_intake(invite))


def submit_client_invite_intake(
    invite_token: str,
    intake: ClientInviteIntake,
) -> ClientInviteEnvelopeResponse:
    invite = resolve_invite(invite_token)
    session = db.SessionLocal()
    try:
        existing = session.query(InviteIntakeDB).filter_by(invite_token=invite.token).first()
        if existing is None:
            existing = InviteIntakeDB(id=_intake_id(invite.token), invite_token=invite.token)
            session.add(existing)
        existing.client_name = intake.client_name
        existing.guardian_phone = intake.guardian_phone
        existing.guardian_email = intake.guardian_email
        existing.notes = intake.notes
        existing.location_type = intake.location_type
        existing.location_address = intake.location.address
        existing.lat = intake.location.lat
        existing.lng = intake.location.lng
        existing.is_instructor_facility = intake.location.is_instructor_facility
        existing.updated_at = datetime.datetime.utcnow()
        session.commit()
    finally:
        session.close()
    return _envelope_response(invite, intake)


def search_client_invite_slots(
    invite_token: str,
    request: ClientInviteAvailabilityRequest,
) -> ClientInviteAvailabilityResponse:
    invite = resolve_invite(invite_token)
    is_facility = request.location.is_instructor_facility or request.location.address.lower() == "facility"
    location = _resolve_location(invite.instructor_id, request.location, is_facility)
    slots = _find_slots(
        instructor_id=invite.instructor_id,
        requested_date=request.requested_date,
        client_address=location.address,
        is_instructor_facility=is_facility,
        duration_mins=request.duration_mins,
    )
    filtered_slots = [slot for slot in slots if _slot_matches_windows(slot, request.windows)]
    response_slots = [
        _store_and_shape_slot(invite, request, location, slot, is_facility)
        for slot in filtered_slots
    ]
    return ClientInviteAvailabilityResponse(
        status="PRESENTING",
        dialogue_response=(
            "Here are the safe lesson times that fit the instructor schedule, travel time, and buffers."
        ),
        slots=response_slots,
        unavailable_windows=_unavailable_windows(request, response_slots),
    )


def request_client_invite_lesson_slot(
    invite_token: str,
    request: ClientInviteLessonRequestCreate,
) -> ClientInviteLessonRequestResponse:
    invite = resolve_invite(invite_token)
    offer = _load_active_offer(invite.token, request.slot_id)
    idempotency_key = request.idempotency_key or f"public-slot:{invite.token}:{request.slot_id}"
    try:
        record = create_lesson_request(
            invite_token=invite.token,
            instructor_id=invite.instructor_id,
            client_name=offer.client_name,
            client_address=offer.client_address,
            requested_date=offer.requested_date,
            selected_slot_start=offer.slot_start,
            selected_slot_end=offer.slot_end,
            duration_mins=offer.duration_mins,
            travel_time_mins=offer.travel_time_mins,
            buffer_mins=offer.buffer_mins,
            distance_miles=offer.distance_miles,
            location_type=offer.location_type,
            lat=offer.lat,
            lng=offer.lng,
            idempotency_key=idempotency_key,
        )
    except db.IdempotencyKeyConflictError as exc:
        raise IdempotencyConflict() from exc
    return _lesson_request_response(record.request_token)


def get_client_lesson_request_status(request_token: str) -> ClientInviteLessonRequestResponse:
    return _lesson_request_response(request_token)


def get_client_calendar_links(request_token: str) -> ClientInviteCalendarLinks:
    response = _lesson_request_response(request_token)
    if response.confirmation is None:
        raise CalendarLinksUnavailable()
    return response.confirmation.calendar_links


def get_client_calendar_ics(request_token: str) -> str:
    request, appointment = _load_request_and_appointment(request_token)
    if appointment is None:
        raise CalendarLinksUnavailable()
    return _ics_content(request, appointment)


def _find_slots(
    instructor_id: str,
    requested_date: datetime.date,
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
        raise GeocodingFailedApiError({"field": "location.address"}) from exc
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


def _resolve_location(
    instructor_id: str,
    location: ClientInviteLocation,
    is_facility: bool,
) -> ClientInviteLocation:
    instructor = db.get_instructor_info(instructor_id)
    if not instructor:
        raise ResourceNotFound("The instructor could not be found.")
    if is_facility:
        return ClientInviteLocation(
            address=instructor.main_address,
            lat=instructor.lat,
            lng=instructor.lng,
            is_instructor_facility=True,
        )
    if location.lat is not None and location.lng is not None:
        return location
    try:
        lat, lng = tools.routing_provider.geocode(location.address)
    except GeocodeFailed as exc:
        raise GeocodingFailedApiError({"field": "location.address"}) from exc
    except RoutingProviderNotConfigured as exc:
        raise RoutingConfigurationError() from exc
    return ClientInviteLocation(
        address=location.address,
        lat=lat,
        lng=lng,
        is_instructor_facility=False,
    )


def _load_or_default_intake(invite: InviteContext) -> ClientInviteIntake:
    session = db.SessionLocal()
    try:
        intake = session.query(InviteIntakeDB).filter_by(invite_token=invite.token).first()
        if intake:
            return _intake_response(intake)
    finally:
        session.close()
    return ClientInviteIntake(
        client_name="New swimmer",
        guardian_phone="",
        guardian_email="",
        notes="",
        location_type="home",
        location=ClientInviteLocation(
            address=invite.location_label,
            lat=None,
            lng=None,
            is_instructor_facility=False,
        ),
    )


def _envelope_response(invite: InviteContext, intake: ClientInviteIntake) -> ClientInviteEnvelopeResponse:
    return ClientInviteEnvelopeResponse(
        invite_token=invite.token,
        instructor_name=invite.instructor_name,
        business_name=invite.business_name,
        expires_at_iso=invite.expires_at,
        intake=intake,
        preferred_duration_mins=invite.preferred_duration_mins,
        allowed_durations_mins=list(invite.allowed_durations_mins),
    )


def _intake_response(intake: InviteIntakeDB) -> ClientInviteIntake:
    return ClientInviteIntake(
        client_name=intake.client_name,
        guardian_phone=intake.guardian_phone or "",
        guardian_email=intake.guardian_email or "",
        notes=intake.notes or "",
        location_type=intake.location_type,
        location=ClientInviteLocation(
            address=intake.location_address,
            lat=intake.lat,
            lng=intake.lng,
            is_instructor_facility=intake.is_instructor_facility,
        ),
    )


def _store_and_shape_slot(
    invite: InviteContext,
    request: ClientInviteAvailabilityRequest,
    location: ClientInviteLocation,
    slot: TimeSlotDTO,
    is_facility: bool,
) -> ClientInviteSchedulerSlot:
    slot_id = _slot_id(invite.token, request, slot.start_time)
    shaped = ClientInviteSchedulerSlot(
        id=slot_id,
        start_iso=slot.start_time,
        end_iso=slot.end_time,
        display=_display_slot(slot.start_time),
        location_address=location.address,
        travel_time_mins=slot.travel_time_mins,
        buffer_mins=slot.buffer_mins,
        distance_miles=slot.distance_miles,
        previous_event="Prior commitment" if slot.start_time.hour > 8 else "Start of instructor day",
        next_event="Next commitment",
        safety_reason="Travel and buffer time fit before the next protected schedule item.",
        recurrence_available=request.recurrence == "single" or _weekly_candidate(slot.start_time),
    )
    session = db.SessionLocal()
    try:
        existing = session.query(InviteSlotOfferDB).filter_by(id=slot_id).first()
        if existing is None:
            existing = InviteSlotOfferDB(id=slot_id)
            session.add(existing)
        existing.invite_token = invite.token
        existing.instructor_id = invite.instructor_id
        existing.client_name = request.client_name
        existing.client_address = location.address
        existing.requested_date = request.requested_date
        existing.slot_start = slot.start_time
        existing.slot_end = slot.end_time
        existing.duration_mins = request.duration_mins
        existing.location_type = "facility" if is_facility else "home"
        existing.location_address = location.address
        existing.lat = location.lat or 0.0
        existing.lng = location.lng or 0.0
        existing.travel_time_mins = slot.travel_time_mins
        existing.buffer_mins = slot.buffer_mins
        existing.distance_miles = slot.distance_miles
        existing.recurrence = request.recurrence
        existing.recurrence_available = shaped.recurrence_available
        existing.expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=SLOT_OFFER_TTL_MINUTES)
        existing.updated_at = datetime.datetime.utcnow()
        session.commit()
    finally:
        session.close()
    return shaped


def _load_active_offer(invite_token: str, slot_id: str) -> InviteSlotOfferDB:
    session = db.SessionLocal()
    try:
        offer = (
            session.query(InviteSlotOfferDB)
            .filter(
                InviteSlotOfferDB.id == slot_id,
                InviteSlotOfferDB.invite_token == invite_token,
                InviteSlotOfferDB.expires_at > datetime.datetime.utcnow(),
            )
            .first()
        )
        if offer is None:
            raise SlotOfferNotFound()
        session.expunge(offer)
        return offer
    finally:
        session.close()


def _lesson_request_response(request_token: str) -> ClientInviteLessonRequestResponse:
    request, appointment = _load_request_and_appointment(request_token)
    slot = ClientInviteSchedulerSlot(
        id=_slot_id_from_request(request),
        start_iso=request.slot_start,
        end_iso=request.slot_end,
        display=_display_slot(request.slot_start),
        location_address=request.client_address,
        travel_time_mins=request.travel_time_mins,
        buffer_mins=request.buffer_mins,
        distance_miles=request.distance_miles,
        previous_event="Prior commitment",
        next_event="Next commitment",
        safety_reason="This requested time was created from a server-issued slot offer.",
        recurrence_available=True,
    )
    status = _public_request_status(request.status)
    confirmation = _confirmation_response(request, appointment) if appointment is not None else None
    return ClientInviteLessonRequestResponse(
        id=request.request_token,
        invite_token=request.invite_token,
        slot=slot,
        status=status,
        requested_at_iso=request.created_at,
        confirmation=confirmation,
    )


def _load_request_and_appointment(request_token: str) -> tuple[LessonRequestDB, Optional[AppointmentDB]]:
    session = db.SessionLocal()
    try:
        request = session.query(LessonRequestDB).filter_by(request_token=request_token).first()
        if request is None:
            raise LessonRequestNotFound()
        appointment = session.query(AppointmentDB).filter_by(request_id=request.id).first()
        session.expunge(request)
        if appointment is not None:
            session.expunge(appointment)
        return request, appointment
    finally:
        session.close()


def _confirmation_response(
    request: LessonRequestDB,
    appointment: AppointmentDB,
) -> ClientInviteLessonConfirmation:
    links = _calendar_links(request, appointment)
    return ClientInviteLessonConfirmation(
        appointment_id=request.request_token,
        message=f"Your swim lesson is confirmed for {_display_slot(appointment.start_time)}.",
        confirmed_at_iso=appointment.created_at,
        calendar_links=links,
    )


def _calendar_links(request: LessonRequestDB, appointment: AppointmentDB) -> ClientInviteCalendarLinks:
    start = _calendar_timestamp(appointment.start_time)
    end = _calendar_timestamp(appointment.end_time)
    title = urllib.parse.quote("Swim lesson")
    location = urllib.parse.quote(appointment.location_address)
    google = (
        "https://calendar.google.com/calendar/render?action=TEMPLATE"
        f"&text={title}&dates={start}/{end}&location={location}"
    )
    ics = f"/api/public/calendar/{request.request_token}.ics"
    return ClientInviteCalendarLinks(google=google, apple=ics, ics=ics)


def _ics_content(request: LessonRequestDB, appointment: AppointmentDB) -> str:
    return "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Swim Command//Client Invite//EN",
            "BEGIN:VEVENT",
            f"UID:{request.request_token}@swim-command.local",
            f"DTSTART:{_calendar_timestamp(appointment.start_time)}",
            f"DTEND:{_calendar_timestamp(appointment.end_time)}",
            "SUMMARY:Swim lesson",
            f"LOCATION:{appointment.location_address}",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )


def _slot_matches_windows(slot: TimeSlotDTO, windows) -> bool:
    if not windows:
        return True
    day_key = DAY_KEYS[slot.start_time.weekday()]
    slot_time = slot.start_time.time()
    for window in windows:
        if day_key not in window.days:
            continue
        start_hour, start_minute = map(int, window.start_time.split(":"))
        end_hour, end_minute = map(int, window.end_time.split(":"))
        if datetime.time(start_hour, start_minute) <= slot_time < datetime.time(end_hour, end_minute):
            return True
    return False


def _unavailable_windows(
    request: ClientInviteAvailabilityRequest,
    slots: list[ClientInviteSchedulerSlot],
) -> list[ClientInviteUnavailableWindow]:
    if slots:
        return []
    return [
        ClientInviteUnavailableWindow(
            id=window.id,
            label=f"{', '.join(window.days)} {window.start_time}-{window.end_time}",
            reason="No safe lesson time fit this preferred window.",
        )
        for window in request.windows
    ]


def _slot_id(invite_token: str, request: ClientInviteAvailabilityRequest, slot_start: datetime.datetime) -> str:
    raw = "|".join(
        [
            invite_token,
            request.client_name.strip().lower(),
            request.location.address.strip().lower(),
            request.requested_date.isoformat(),
            slot_start.isoformat(),
            str(request.duration_mins),
            request.recurrence,
        ]
    )
    return f"slot_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _slot_id_from_request(request: LessonRequestDB) -> str:
    raw = "|".join(
        [
            request.invite_token,
            request.client_name.strip().lower(),
            request.client_address.strip().lower(),
            request.requested_date.isoformat(),
            request.slot_start.isoformat(),
            str(request.duration_mins),
            "single",
        ]
    )
    return f"slot_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _intake_id(invite_token: str) -> str:
    return f"intake_{hashlib.sha256(invite_token.encode('utf-8')).hexdigest()[:12]}"


def _display_slot(start_time: datetime.datetime) -> str:
    hour = start_time.strftime("%I").lstrip("0") or "12"
    return f"{start_time.strftime('%a, %b')} {start_time.day} at {hour}:{start_time.strftime('%M %p')}"


def _calendar_timestamp(value: datetime.datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%S")


def _weekly_candidate(start_time: datetime.datetime) -> bool:
    return start_time.weekday() < 5 or start_time.hour < 17


def _public_request_status(status: str) -> str:
    if status == "accepted":
        return "confirmed"
    if status in {"declined", "cancelled"}:
        return "needs_new_times"
    return "requested"
