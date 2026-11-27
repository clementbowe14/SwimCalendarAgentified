import datetime
import hashlib
from typing import Optional

from sqlalchemy.exc import IntegrityError

from app import db, timezones, tools
from app.calendar_handoff import calendar_ics, calendar_links
from app.notifications import public_notifications
from app.api_errors import (
    CalendarAvailabilityUnavailable,
    CalendarLinksUnavailable,
    GeocodingFailed as GeocodingFailedApiError,
    IdempotencyConflict,
    InvalidSchedulingRequest,
    LessonRequestExpired,
    LessonRequestNotFound,
    ProviderSetupRequired,
    ResourceNotFound,
    RoutingConfigurationError,
    RoutingEstimateFailed as RoutingEstimateFailedApiError,
    RoutingProviderUnavailable as RoutingProviderUnavailableApiError,
    RoutingTimeout as RoutingTimeoutApiError,
    SlotOfferNotFound,
)
from app.external_calendar import CalendarBusySource, ExternalCalendarUnavailable
from app.google_calendar_availability_fence import (
    GoogleCalendarAvailabilityFence,
    claim_google_calendar_availability_fence,
    lock_current_google_calendar_availability_fence,
)
from app.api_schemas import (
    ClientInviteAvailabilityRequest,
    ClientInviteAvailabilityResponse,
    ClientInviteCalendarLinks,
    ClientInviteEnvelopeResponse,
    ClientInviteIntake,
    ClientInviteLessonConfirmation,
    ClientInviteAlternativeSelectionRequest,
    ClientInviteLessonRequestCreate,
    ClientInviteLessonRequestResponse,
    ClientInviteLocation,
    ClientInviteSchedulerSlot,
    ClientInviteUnavailableWindow,
)
from app.models import AppointmentDB, InviteIntakeDB, InviteSlotOfferDB, LessonRequestDB, TimeSlotDTO
from app.provider_settings import (
    ProviderConfiguration,
    ProviderNotFound,
    ProviderSetupIncomplete,
    require_provider_setup,
    service_location_matches,
)
from app.public_tokens import (
    InviteContext,
    create_lesson_request,
    demo_invite_is_enabled,
    require_active_invite,
    resolve_invite,
    resolve_lesson_request_by_idempotency_key,
)
from app.routing import (
    GeocodeFailed,
    RouteEstimateFailed,
    RoutingProviderNotConfigured,
    RoutingProviderUnavailable,
    RoutingTimeout,
)
from app.engine import BufferPolicy
from app.scheduler_settings import load_buffer_policy


SLOT_OFFER_TTL_MINUTES = 30
WEEKLY_OCCURRENCE_COUNT = 4
DAY_KEYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
HOME_POOL_ADDRESS_PROMPT = "Enter home pool address"


def get_client_invite_envelope(invite_token: str) -> ClientInviteEnvelopeResponse:
    invite = resolve_invite(invite_token)
    return _envelope_response(invite, _load_or_default_intake(invite))


def submit_client_invite_intake(
    invite_token: str,
    intake: ClientInviteIntake,
) -> ClientInviteEnvelopeResponse:
    invite = resolve_invite(invite_token)
    intake = _project_authoritative_facility_intake(invite, intake)
    if (
        intake.location_type == "home"
        and intake.location.address.strip().casefold()
        == HOME_POOL_ADDRESS_PROMPT.casefold()
    ):
        raise InvalidSchedulingRequest("Enter a home pool address.")
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
    invite: InviteContext,
    request: ClientInviteAvailabilityRequest,
    *,
    calendar_busy_source: CalendarBusySource | None = None,
) -> ClientInviteAvailabilityResponse:
    is_facility = request.location.is_instructor_facility or request.location.address.lower() == "facility"
    location = _resolve_location(invite.instructor_id, request.location, is_facility)
    timezone_name = _instructor_timezone(invite.instructor_id)
    buffer_policy = (
        load_buffer_policy(invite.instructor_id, operation="availability")
        if request.recurrence == "weekly"
        else None
    )

    def slots_on(day: datetime.date) -> list[TimeSlotDTO]:
        with db.SessionLocal() as session:
            require_active_invite(session, invite.token, instructor_id=invite.instructor_id)
        return _find_slots(
            instructor_id=invite.instructor_id,
            requested_date=day,
            client_address=location.address,
            is_instructor_facility=is_facility,
            duration_mins=request.duration_mins,
            buffer_policy=buffer_policy,
            calendar_busy_source=calendar_busy_source,
        )

    try:
        fence = None
        if calendar_busy_source is not None:
            fence = claim_google_calendar_availability_fence(db.SessionLocal, instructor_id=invite.instructor_id)
        slots = slots_on(request.requested_date)
        filtered_slots = [slot for slot in slots if _slot_matches_windows(slot, request.windows)]
        if request.recurrence == "weekly" and filtered_slots:
            for week in range(WEEKLY_OCCURRENCE_COUNT):
                offset = datetime.timedelta(weeks=week)
                week_slots = slots if week == 0 else slots_on(request.requested_date + offset)
                available_intervals = {
                    (timezones.to_utc_storage(slot.start_time), timezones.to_utc_storage(slot.end_time))
                    for slot in week_slots
                }
                filtered_slots = [
                    slot for slot in filtered_slots
                    if _weekly_interval_available(slot, offset, timezone_name, available_intervals)
                ]
                if not filtered_slots:
                    break
        return _present_slot_offers(invite, request, location, is_facility, filtered_slots, timezone_name, fence)
    except ExternalCalendarUnavailable:
        pass
    raise CalendarAvailabilityUnavailable() from None


def _present_slot_offers(
    invite: InviteContext,
    request: ClientInviteAvailabilityRequest,
    location: ClientInviteLocation,
    is_facility: bool,
    slots: list[TimeSlotDTO],
    timezone_name: str,
    fence: GoogleCalendarAvailabilityFence | None,
) -> ClientInviteAvailabilityResponse:
    response_slots = [
        _shape_slot(invite, request, location, slot, timezone_name)
        for slot in slots
    ]
    _save_slot_offers(invite, request, location, is_facility, list(zip(slots, response_slots)), fence)
    return ClientInviteAvailabilityResponse(
        status="PRESENTING",
        dialogue_response="Here are the appointment times available to request.",
        slots=response_slots,
        unavailable_windows=_unavailable_windows(request, response_slots),
    )


def request_client_invite_lesson_slot(
    invite_token: str,
    request: ClientInviteLessonRequestCreate,
) -> ClientInviteLessonRequestResponse:
    invite = resolve_invite(invite_token)
    offer = _load_active_offer(invite.token, request.slot_id)
    if offer.recurrence == "weekly" and not offer.recurrence_available:
        raise InvalidSchedulingRequest("The selected lesson time is not available for weekly booking.")
    idempotency_key = request.idempotency_key or f"public-slot:{invite.token}:{request.slot_id}"
    existing = resolve_lesson_request_by_idempotency_key(idempotency_key)
    if existing is not None:
        if existing.invite_token != invite.token or existing.original_slot_offer_id != offer.id:
            raise IdempotencyConflict()
        return _lesson_request_response(existing.request_token)

    provider = _require_public_provider_setup(offer.instructor_id)
    _validate_current_facility_offer(offer, provider)
    policy = load_buffer_policy(invite.instructor_id, operation="client_invite_request_creation")
    _validate_offer_occurrences(offer, policy, provider.timezone)
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
            recurrence=offer.recurrence,
            recurrence_count=offer.recurrence_count,
            original_slot_offer_id=offer.id,
            expires_at=min(datetime.datetime.utcnow() + datetime.timedelta(hours=24), invite.expires_at),
        )
    except db.IdempotencyKeyConflictError as exc:
        raise IdempotencyConflict() from exc
    return _lesson_request_response(record.request_token)


def get_client_lesson_request_status(request_token: str) -> ClientInviteLessonRequestResponse:
    return _lesson_request_response(request_token)


def select_client_lesson_request_alternative(
    request_token: str,
    request: ClientInviteAlternativeSelectionRequest,
) -> ClientInviteLessonRequestResponse:
    session = db.SessionLocal()
    try:
        lesson_request = (
            session.query(LessonRequestDB)
            .filter_by(request_token=request_token)
            .with_for_update()
            .first()
        )
        if (
            lesson_request is None
            or lesson_request.invite is None
            or not demo_invite_is_enabled(lesson_request.invite)
            or lesson_request.invite.revoked_at is not None
        ):
            raise LessonRequestNotFound()
        has_appointment = (
            session.query(AppointmentDB.id)
            .filter_by(request_id=lesson_request.id)
            .first()
            is not None
        )
        if lesson_request.status not in {"pending", "alternatives_sent"} or has_appointment:
            raise InvalidSchedulingRequest("Confirmed lesson requests cannot select another time.")
        if _is_expired(lesson_request):
            raise LessonRequestExpired()
        offer = (
            session.query(InviteSlotOfferDB)
            .filter(
                InviteSlotOfferDB.id == request.slot_id,
                InviteSlotOfferDB.lesson_request_id == lesson_request.id,
                InviteSlotOfferDB.invite_token == lesson_request.invite_token,
                InviteSlotOfferDB.instructor_id == lesson_request.instructor_id,
                InviteSlotOfferDB.offer_kind == "alternative",
                InviteSlotOfferDB.expires_at > datetime.datetime.utcnow(),
            )
            .first()
        )
        if offer is None:
            raise SlotOfferNotFound()
        provider = _require_public_provider_setup(lesson_request.instructor_id)
        _validate_current_facility_offer(offer, provider)
        policy = load_buffer_policy(
            lesson_request.instructor_id,
            operation="client_invite_alternative_selection",
        )
        _validate_offer_occurrences(offer, policy, provider.timezone)
        lesson_request.slot_start = offer.slot_start
        lesson_request.slot_end = offer.slot_end
        lesson_request.requested_date = offer.requested_date
        lesson_request.travel_time_mins = offer.travel_time_mins
        lesson_request.buffer_mins = offer.buffer_mins
        lesson_request.distance_miles = offer.distance_miles
        lesson_request.original_slot_offer_id = offer.id
        lesson_request.confirmed_slot_offer_id = None
        lesson_request.status = "pending"
        lesson_request.validation_failure_code = None
        lesson_request.validation_failure_details = None
        lesson_request.validation_failed_at = None
        lesson_request.expires_at = min(
            datetime.datetime.utcnow() + datetime.timedelta(hours=24),
            lesson_request.invite.expires_at,
        )
        lesson_request.updated_at = datetime.datetime.utcnow()
        session.commit()
    finally:
        session.close()
    return _lesson_request_response(request_token)


def get_client_calendar_links(request_token: str) -> ClientInviteCalendarLinks:
    request, appointments = _load_request_and_appointments(request_token)
    return calendar_links(request.request_token, appointments)


def get_client_calendar_ics(request_token: str) -> str:
    request, appointments = _load_request_and_appointments(request_token)
    if not appointments:
        raise CalendarLinksUnavailable()
    return calendar_ics(appointments)


def _find_slots(
    instructor_id: str,
    requested_date: datetime.date,
    client_address: str,
    is_instructor_facility: bool,
    duration_mins: int,
    buffer_policy: Optional[BufferPolicy] = None,
    *,
    calendar_busy_source: CalendarBusySource | None = None,
) -> list[TimeSlotDTO]:
    try:
        return tools.find_available_swim_slots(
            instructor_id=instructor_id,
            date_str=requested_date.isoformat(),
            client_address=client_address,
            is_instructor_facility=is_instructor_facility,
            duration_mins=duration_mins,
            buffer_policy=buffer_policy,
            calendar_busy_source=calendar_busy_source,
        )
    except ExternalCalendarUnavailable:
        pass
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
    except ProviderSetupIncomplete as exc:
        raise ProviderSetupRequired(public=True) from exc
    except ValueError as exc:
        message = str(exc)
        if "not found" in message.lower():
            raise ResourceNotFound("The instructor could not be found.") from exc
        raise InvalidSchedulingRequest("The scheduling request could not be evaluated.") from exc
    raise CalendarAvailabilityUnavailable() from None


def _validate_offer_occurrences(
    offer: InviteSlotOfferDB,
    buffer_policy: BufferPolicy,
    timezone_name: str,
) -> None:
    base_local = timezones.storage_to_local(offer.slot_start, timezone_name)
    count = offer.recurrence_count if offer.recurrence == "weekly" else 1
    for index in range(count):
        local_label = base_local.replace(tzinfo=None) + datetime.timedelta(days=7 * index)
        try:
            occurrence = timezones.validate_local_datetime(local_label, timezone_name)
        except (
            timezones.AmbiguousLocalTimeError,
            timezones.NonexistentLocalTimeError,
        ) as exc:
            raise InvalidSchedulingRequest(
                "The selected lesson time is no longer available."
            ) from exc
        slots = _find_slots(
            instructor_id=offer.instructor_id,
            requested_date=occurrence.date(),
            client_address=offer.client_address,
            is_instructor_facility=offer.location_type == "facility",
            duration_mins=offer.duration_mins,
            buffer_policy=buffer_policy,
        )
        occurrence_storage = timezones.to_utc_storage(occurrence)
        if not any(timezones.to_utc_storage(slot.start_time) == occurrence_storage for slot in slots):
            raise InvalidSchedulingRequest("The selected lesson time is no longer available.")


def _validate_current_facility_offer(
    offer: InviteSlotOfferDB,
    provider: ProviderConfiguration,
) -> None:
    if offer.location_type != "facility":
        return
    if not service_location_matches(
        provider,
        address=offer.location_address,
        lat=offer.lat,
        lng=offer.lng,
    ):
        raise InvalidSchedulingRequest("The selected lesson time is no longer available.")


def _require_public_provider_setup(instructor_id: str) -> ProviderConfiguration:
    try:
        return require_provider_setup(instructor_id)
    except ProviderNotFound as exc:
        raise ResourceNotFound("The instructor could not be found.") from exc
    except ProviderSetupIncomplete as exc:
        raise ProviderSetupRequired(public=True) from exc


def _resolve_location(
    instructor_id: str,
    location: ClientInviteLocation,
    is_facility: bool,
) -> ClientInviteLocation:
    try:
        provider = require_provider_setup(instructor_id)
    except ProviderNotFound as exc:
        raise ResourceNotFound("The instructor could not be found.")
    except ProviderSetupIncomplete as exc:
        raise ProviderSetupRequired(public=True) from exc
    if is_facility:
        if provider.service_location is None:
            raise ProviderSetupRequired(public=True)
        return ClientInviteLocation(
            address=provider.service_location.address,
            lat=provider.service_location.lat,
            lng=provider.service_location.lng,
            is_instructor_facility=True,
        )
    if location.lat is not None and location.lng is not None:
        return location
    try:
        lat, lng = tools.routing_provider.geocode(location.address)
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
            address=HOME_POOL_ADDRESS_PROMPT,
            lat=None,
            lng=None,
            is_instructor_facility=False,
        ),
    )


def _envelope_response(invite: InviteContext, intake: ClientInviteIntake) -> ClientInviteEnvelopeResponse:
    intake = _project_authoritative_facility_intake(invite, intake)
    return ClientInviteEnvelopeResponse(
        invite_token=invite.token,
        instructor_name=invite.instructor_name,
        business_name=invite.business_name,
        service_name=invite.service_name,
        service_location=(
            {
                "address": invite.service_location.address,
                "lat": invite.service_location.lat,
                "lng": invite.service_location.lng,
            }
            if invite.service_location is not None
            else None
        ),
        expires_at_iso=invite.expires_at,
        intake=intake,
        preferred_duration_mins=invite.preferred_duration_mins,
        allowed_durations_mins=list(invite.allowed_durations_mins),
    )


def _project_authoritative_facility_intake(
    invite: InviteContext,
    intake: ClientInviteIntake,
) -> ClientInviteIntake:
    if intake.location_type != "facility" and not intake.location.is_instructor_facility:
        return intake
    service_location = invite.service_location
    return intake.model_copy(
        update={
            "location_type": "facility",
            "location": ClientInviteLocation(
                address=(
                    service_location.address
                    if service_location is not None
                    else invite.location_label
                ),
                lat=service_location.lat if service_location is not None else None,
                lng=service_location.lng if service_location is not None else None,
                is_instructor_facility=True,
            ),
        }
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


def _shape_slot(
    invite: InviteContext,
    request: ClientInviteAvailabilityRequest,
    location: ClientInviteLocation,
    slot: TimeSlotDTO,
    timezone_name: str,
) -> ClientInviteSchedulerSlot:
    local_start = timezones.aware_to_local(slot.start_time, timezone_name)
    local_end = timezones.aware_to_local(slot.end_time, timezone_name)
    slot_id = _slot_id(invite.token, request, slot.start_time)
    return ClientInviteSchedulerSlot(
        id=slot_id,
        start_iso=local_start,
        end_iso=local_end,
        display=_display_slot(local_start),
        location_address=location.address,
        recurrence_available=True,
        recurrence=request.recurrence,
        recurrence_count=WEEKLY_OCCURRENCE_COUNT if request.recurrence == "weekly" else 1,
    )


def _save_slot_offers(
    invite: InviteContext,
    request: ClientInviteAvailabilityRequest,
    location: ClientInviteLocation,
    is_facility: bool,
    offers: list[tuple[TimeSlotDTO, ClientInviteSchedulerSlot]],
    fence: GoogleCalendarAvailabilityFence | None,
) -> None:
    with db.SessionLocal() as session:
        for attempt in range(2):
            try:
                with session.begin():
                    require_active_invite(session, invite.token, instructor_id=invite.instructor_id, lock=True)
                    for slot, shaped in offers:
                        _upsert_slot_offer(session, invite, request, location, slot, shaped, is_facility)
                    if fence is not None:
                        session.flush()
                        lock_current_google_calendar_availability_fence(session, fence=fence)
                    require_active_invite(session, invite.token, instructor_id=invite.instructor_id)
                return
            except IntegrityError:
                if attempt:
                    raise


def _upsert_slot_offer(
    session,
    invite: InviteContext,
    request: ClientInviteAvailabilityRequest,
    location: ClientInviteLocation,
    slot: TimeSlotDTO,
    shaped: ClientInviteSchedulerSlot,
    is_facility: bool,
) -> None:
    existing = session.query(InviteSlotOfferDB).filter_by(id=shaped.id).first()
    if existing is None:
        existing = InviteSlotOfferDB(id=shaped.id)
        session.add(existing)
    existing.invite_token = invite.token
    existing.instructor_id = invite.instructor_id
    existing.client_name = request.client_name
    existing.client_address = location.address
    existing.requested_date = request.requested_date
    existing.slot_start = timezones.to_utc_storage(slot.start_time)
    existing.slot_end = timezones.to_utc_storage(slot.end_time)
    existing.duration_mins = request.duration_mins
    existing.location_type = "facility" if is_facility else "home"
    existing.location_address = location.address
    existing.lat = location.lat or 0.0
    existing.lng = location.lng or 0.0
    existing.travel_time_mins = slot.travel_time_mins
    existing.buffer_mins = slot.buffer_mins
    existing.distance_miles = slot.distance_miles
    existing.recurrence = request.recurrence
    existing.recurrence_count = shaped.recurrence_count
    existing.recurrence_available = shaped.recurrence_available
    existing.lesson_request_id = None
    existing.offer_kind = "search"
    existing.message = None
    existing.notified_at = None
    existing.expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=SLOT_OFFER_TTL_MINUTES)
    existing.updated_at = datetime.datetime.utcnow()


def _load_active_offer(invite_token: str, slot_id: str) -> InviteSlotOfferDB:
    session = db.SessionLocal()
    try:
        offer = (
            session.query(InviteSlotOfferDB)
            .filter(
                InviteSlotOfferDB.id == slot_id,
                InviteSlotOfferDB.invite_token == invite_token,
                InviteSlotOfferDB.offer_kind == "search",
                InviteSlotOfferDB.lesson_request_id.is_(None),
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
    request, appointments = _load_request_and_appointments(request_token)
    timezone_name, uses_authoritative_timezone = _instructor_response_timezone(
        request.instructor_id
    )
    local_start = timezones.storage_to_local(request.slot_start, timezone_name)
    local_end = timezones.storage_to_local(request.slot_end, timezone_name)
    slot = ClientInviteSchedulerSlot(
        id=request.original_slot_offer_id or _slot_id_from_request(request),
        start_iso=local_start,
        end_iso=local_end,
        display=_display_slot(
            local_start,
            timezone_label=None if uses_authoritative_timezone else "UTC",
        ),
        location_address=request.client_address,
        recurrence_available=True,
        recurrence=request.recurrence,
        recurrence_count=request.recurrence_count,
    )
    status = _public_request_status(request.status)
    if _is_expired(request):
        status = "expired"
    confirmation = _confirmation_response(request, appointments) if appointments else None
    with db.SessionLocal() as session:
        notifications = public_notifications(session, request.id)
    return ClientInviteLessonRequestResponse(
        id=request.request_token,
        invite_token=request.invite_token,
        slot=slot,
        status=status,
        requested_at_iso=request.created_at,
        alternative_slots=_alternative_slot_responses(request),
        confirmation=confirmation,
        notifications=notifications,
    )


def _load_request_and_appointments(request_token: str) -> tuple[LessonRequestDB, list[AppointmentDB]]:
    session = db.SessionLocal()
    try:
        request = session.query(LessonRequestDB).filter_by(request_token=request_token).first()
        if (
            request is None
            or request.invite is None
            or not demo_invite_is_enabled(request.invite)
            or request.invite.revoked_at is not None
        ):
            raise LessonRequestNotFound()
        appointments = (
            session.query(AppointmentDB)
            .filter_by(request_id=request.id, instructor_id=request.instructor_id,
                       event_type="lesson", status="scheduled", is_break=False)
            .filter(AppointmentDB.cancelled_at.is_(None))
            .order_by(AppointmentDB.start_time.asc(), AppointmentDB.calendar_uid.asc())
            .all()
        ) if request.status == "confirmed" else []
        session.expunge(request)
        for appointment in appointments:
            session.expunge(appointment)
        return request, appointments
    finally:
        session.close()


def _confirmation_response(
    request: LessonRequestDB,
    appointments: list[AppointmentDB],
) -> ClientInviteLessonConfirmation:
    first = appointments[0]
    links = calendar_links(request.request_token, appointments)
    timezone_name, uses_authoritative_timezone = _instructor_response_timezone(
        request.instructor_id
    )
    local_start = timezones.storage_to_local(first.start_time, timezone_name)
    timezone_label = None if uses_authoritative_timezone else "UTC"
    return ClientInviteLessonConfirmation(
        appointment_id=request.request_token,
        appointment_ids=[
            request.request_token if len(appointments) == 1 else f"{request.request_token}-{index + 1}"
            for index, _ in enumerate(appointments)
        ],
        message=(
            "Your swim lesson is confirmed for "
            f"{_display_slot(local_start, timezone_label=timezone_label)}."
        ),
        confirmed_at_iso=first.created_at,
        calendar_links=links,
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
            timezones.canonical_instant_text(slot_start),
            str(request.duration_mins),
            request.recurrence,
            str(WEEKLY_OCCURRENCE_COUNT if request.recurrence == "weekly" else 1),
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
            timezones.canonical_instant_text(request.slot_start),
            str(request.duration_mins),
            request.recurrence,
            str(request.recurrence_count),
        ]
    )
    return f"slot_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _intake_id(invite_token: str) -> str:
    return f"intake_{hashlib.sha256(invite_token.encode('utf-8')).hexdigest()[:12]}"


def _display_slot(
    start_time: datetime.datetime,
    *,
    timezone_label: Optional[str] = None,
) -> str:
    hour = start_time.strftime("%I").lstrip("0") or "12"
    suffix = f" {timezone_label}" if timezone_label else ""
    return (
        f"{start_time.strftime('%a, %b')} {start_time.day} at "
        f"{hour}:{start_time.strftime('%M %p')}{suffix}"
    )


def _weekly_interval_available(
    slot: TimeSlotDTO,
    offset: datetime.timedelta,
    timezone_name: str,
    available_intervals: set[tuple[datetime.datetime, datetime.datetime]],
) -> bool:
    try:
        start = timezones.validate_local_datetime(slot.start_time.replace(tzinfo=None) + offset, timezone_name)
        end = timezones.validate_local_datetime(slot.end_time.replace(tzinfo=None) + offset, timezone_name)
    except (timezones.AmbiguousLocalTimeError, timezones.NonexistentLocalTimeError):
        return False
    return (timezones.to_utc_storage(start), timezones.to_utc_storage(end)) in available_intervals


def _instructor_timezone(instructor_id: str) -> str:
    return _instructor_response_timezone(instructor_id)[0]


def _instructor_response_timezone(instructor_id: str) -> tuple[str, bool]:
    instructor = db.get_instructor_info(instructor_id)
    if instructor is None or not instructor.timezone or not instructor.timezone.strip():
        return "UTC", False
    try:
        return timezones.get_zone(instructor.timezone if instructor else None).key, True
    except timezones.InvalidTimezoneError:
        # Existing requests remain readable even while setup is incomplete.
        # UTC preserves their exact instants without inventing a provider zone.
        return "UTC", False


def _public_request_status(status: str) -> str:
    if status in {"accepted", "confirmed"}:
        return "confirmed"
    if status in {"alternatives_sent", "declined", "cancelled"}:
        return "needs_new_times"
    return "requested"


def _alternative_slot_responses(request: LessonRequestDB) -> list[ClientInviteSchedulerSlot]:
    if request.status != "alternatives_sent":
        return []
    session = db.SessionLocal()
    try:
        offers = (
            session.query(InviteSlotOfferDB)
            .filter_by(lesson_request_id=request.id, offer_kind="alternative")
            .order_by(InviteSlotOfferDB.slot_start.asc())
            .all()
        )
        timezone_name, uses_authoritative_timezone = _instructor_response_timezone(
            request.instructor_id
        )
        return [
            _offer_slot_response(
                offer,
                timezone_name,
                timezone_label=None if uses_authoritative_timezone else "UTC",
            )
            for offer in offers
        ]
    finally:
        session.close()


def _offer_slot_response(
    offer: InviteSlotOfferDB,
    timezone_name: str,
    *,
    timezone_label: Optional[str] = None,
) -> ClientInviteSchedulerSlot:
    local_start = timezones.storage_to_local(offer.slot_start, timezone_name)
    local_end = timezones.storage_to_local(offer.slot_end, timezone_name)
    return ClientInviteSchedulerSlot(
        id=offer.id,
        start_iso=local_start,
        end_iso=local_end,
        display=_display_slot(local_start, timezone_label=timezone_label),
        location_address=offer.location_address,
        recurrence_available=offer.recurrence_available,
        recurrence=offer.recurrence,
        recurrence_count=offer.recurrence_count,
    )


def _is_expired(request: LessonRequestDB) -> bool:
    return request.status not in {"confirmed"} and request.expires_at <= datetime.datetime.utcnow()
