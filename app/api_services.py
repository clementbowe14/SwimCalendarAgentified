from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import math
import re
from typing import NoReturn, Optional
from urllib.parse import quote
import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import db, timezones, tools
from app.api_errors import (
    AlternativeSlotContextMismatch,
    ApiException,
    BlockerStale,
    CalendarAvailabilityUnavailable,
    CalendarLinksUnavailable,
    GeocodingFailed as GeocodingFailedApiError,
    IdempotencyConflict,
    InvalidSchedulingRequest,
    LessonRequestConfirmConflict,
    LessonRequestExpired,
    LessonRequestNotFound,
    ProviderSetupRequired,
    ResourceNotFound,
    RoutingConfigurationError,
    RoutingEstimateFailed as RoutingEstimateFailedApiError,
    RoutingProviderUnavailable as RoutingProviderUnavailableApiError,
    RoutingTimeout as RoutingTimeoutApiError,
    SchedulingConflict,
    SchedulingPolicyStale,
    SchedulingPolicyValidationError,
)
from app.api_schemas import (
    AppointmentResponse,
    AvailabilitySearchRequest,
    AvailabilitySearchResponse,
    BookLessonResponse,
    CoachAvailabilityRangeRequest,
    CoachAvailabilityRangeResponse,
    CoachBookLessonRequest,
    CoachBlockerScheduleItemResponse,
    CoachBlockerMutation,
    CoachBlockerResponse,
    CoachBlockerUpdate,
    CoachCalendarConnectionResponse,
    CoachClientAvailabilityWindowResponse,
    CoachClientListResponse,
    CoachClientLocationResponse,
    CoachClientProfileResponse,
    CoachClientResponse,
    CoachCommandCenterSnapshot,
    CoachEmailDeliveryResponse,
    CoachLessonRequestActionResponse,
    CoachLessonRequestAlternativesRequest,
    CoachLessonRequestConfirmResponse,
    CoachLessonRequestConfirmRequest,
    CoachLessonRequestResponse,
    CoachLessonScheduleItemResponse,
    CoachScheduleItemResponse,
    CoachScheduleResponse,
    CoachSchedulerRules,
    CoachSchedulingPolicyResponse,
    CoachSchedulingPolicyUpdate,
    CoachTimeSlotResponse,
    CoachInstructorProfileResponse,
    CoachProviderLocationResponse,
    CoachProviderServiceResponse,
    CoachProviderSettingsResponse,
    CoachProviderSettingsUpdate,
    CoachProviderWorkingHours,
    InstructorProfileResponse,
    LocationResponse,
    PublicAvailabilitySearchRequest,
    PublicAvailabilitySearchResponse,
    PublicInviteResponse,
    PublicLessonRequestCreate,
    PublicLessonRequestResponse,
    PublicRequestStatusResponse,
    PublicTimeSlotResponse,
    TimeSlotResponse,
    ClientInviteCalendarLinks,
)
from app.availability_search import search_one_time_availability
from app.calendar_connections import (
    CalendarConnectionOwnerNotFound,
    disconnect_google_calendar,
    get_google_calendar_connection_status,
)
from app.calendar_handoff import calendar_ics, calendar_links
from app.config import demo_data_access_enabled_from_env
from app.external_calendar import (
    CalendarBusySnapshot,
    CalendarBusySource,
    ExternalCalendarUnavailable,
    query_calendar_busy_snapshot,
)
from app.google_calendar_availability_fence import (
    GoogleCalendarAvailabilityFence,
    claim_google_calendar_availability_fence,
    lock_current_google_calendar_availability_fence,
)
from app.models import (
    AppointmentDB,
    InviteSlotOfferDB,
    InviteTokenDB,
    LessonRequestDB,
    LocationInfo,
    TimeSlotDTO,
)
from app.notifications import coach_email_delivery_for_request, list_coach_email_deliveries
from app.provider_settings import (
    ProviderConfiguration,
    ProviderLocation,
    ProviderNotFound,
    ProviderSetupIncomplete,
    WEEKDAY_NAMES,
    load_provider_configuration,
    require_provider_setup,
    save_provider_configuration,
    service_location_matches,
)
from app.public_tokens import (
    InviteContext,
    LessonRequestRecord,
    create_lesson_request,
    is_range_slot_offer,
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
from app.engine import BufferPolicy
from app.scheduler_settings import (
    load_buffer_policy,
    load_scheduler_rules,
    load_scheduler_settings_projection,
    load_scheduling_policy,
    save_scheduling_policy,
)


def get_instructor_profile(instructor_id: str) -> InstructorProfileResponse:
    instructor = db.get_instructor_info(instructor_id)
    if not instructor:
        raise ResourceNotFound("The instructor could not be found.")
    try:
        configuration = load_provider_configuration(instructor_id)
    except ProviderNotFound:
        raise ResourceNotFound("The instructor could not be found.")
    service_location = configuration.service_location
    first_enabled = next(
        (row for row in configuration.working_hours if row.enabled),
        None,
    )
    return InstructorProfileResponse(
        id=instructor.id,
        name=configuration.display_name,
        main_address=service_location.address if service_location else "",
        lat=service_location.lat if service_location else 0.0,
        lng=service_location.lng if service_location else 0.0,
        work_start_time=(first_enabled.start_time if first_enabled else None) or "",
        work_end_time=(first_enabled.end_time if first_enabled else None) or "",
        timezone=configuration.timezone,
    )


def get_coach_provider_settings(instructor_id: str) -> CoachProviderSettingsResponse:
    try:
        return _provider_settings_response(load_provider_configuration(instructor_id))
    except ProviderNotFound as exc:
        raise ResourceNotFound("The instructor could not be found.") from exc


def get_coach_google_calendar_connection_status(
    account_id: str,
) -> CoachCalendarConnectionResponse:
    try:
        with db.SessionLocal() as session:
            status = get_google_calendar_connection_status(
                session,
                account_id=account_id,
            )
    except CalendarConnectionOwnerNotFound as exc:
        raise ResourceNotFound("The provider could not be found.") from exc
    return CoachCalendarConnectionResponse.model_validate(status, from_attributes=True)


def disconnect_coach_google_calendar(account_id: str) -> CoachCalendarConnectionResponse:
    try:
        with db.SessionLocal.begin() as session:
            status = disconnect_google_calendar(session, account_id=account_id)
    except CalendarConnectionOwnerNotFound as exc:
        raise ResourceNotFound("The provider could not be found.") from exc
    return CoachCalendarConnectionResponse.model_validate(status, from_attributes=True)


def list_coach_clients(instructor_id: str) -> CoachClientListResponse:
    configuration = _client_read_provider_configuration(instructor_id)
    clients = db.list_client_profiles_for_instructor(instructor_id)
    return CoachClientListResponse(
        clients=[
            _client_profile_response(client, configuration)
            for client in clients
        ]
    )


def get_coach_client(
    instructor_id: str,
    client_id: str,
) -> CoachClientProfileResponse:
    configuration = _client_read_provider_configuration(instructor_id)
    client = db.get_client_profile_for_instructor(instructor_id, client_id)
    if client is None:
        raise ResourceNotFound("The client could not be found.")
    return _client_profile_response(client, configuration)


def _client_read_provider_configuration(
    instructor_id: str,
) -> ProviderConfiguration:
    try:
        return load_provider_configuration(instructor_id)
    except ProviderNotFound as exc:
        raise ResourceNotFound("The instructor could not be found.") from exc


def _client_profile_response(
    client: dict,
    configuration: ProviderConfiguration,
) -> CoachClientProfileResponse:
    client_location = _client_profile_location_response(client)
    effective_location = client_location
    if client["location_mode"] == "service_location":
        effective_location = _client_service_location_response(
            configuration.service_location
        )
    return CoachClientProfileResponse(
        id=client["id"],
        name=client["name"],
        phone=_optional_contact(client["phone"]),
        email=_optional_contact(client["email"]),
        notes=client["notes"],
        location_mode=client["location_mode"],
        client_location=client_location,
        effective_location=effective_location,
        default_duration_mins=client["default_duration_mins"],
        availability=[
            CoachClientAvailabilityWindowResponse(
                id=window["id"],
                days=[
                    weekday
                    for index, weekday in enumerate(WEEKDAY_NAMES)
                    if window["day_mask"] & (1 << index)
                ],
                start_time=window["start_time"].strftime("%H:%M"),
                end_time=window["end_time"].strftime("%H:%M"),
            )
            for window in client["availability"]
        ],
        revision=client["revision"],
    )


def _client_profile_location_response(
    client: dict,
) -> Optional[CoachClientLocationResponse]:
    if (
        client["default_location_address"] is None
        or client["default_lat"] is None
        or client["default_lng"] is None
    ):
        return None
    return CoachClientLocationResponse(
        address=client["default_location_address"],
        lat=client["default_lat"],
        lng=client["default_lng"],
    )


def _client_service_location_response(
    location: Optional[ProviderLocation],
) -> Optional[CoachClientLocationResponse]:
    if location is None:
        return None
    return CoachClientLocationResponse(
        address=location.address,
        lat=location.lat,
        lng=location.lng,
    )


def _optional_contact(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def update_coach_provider_settings(
    instructor_id: str,
    request: CoachProviderSettingsUpdate,
) -> CoachProviderSettingsResponse:
    try:
        current = load_provider_configuration(instructor_id)
    except ProviderNotFound as exc:
        raise ResourceNotFound("The instructor could not be found.") from exc

    geocode_cache: dict[str, tuple[float, float]] = {}
    base_coords = _provider_location_coordinates(
        request.base_address,
        current.base_location,
        "base_address",
        geocode_cache,
    )
    service_coords = _provider_location_coordinates(
        request.service_address,
        current.service_location,
        "service_address",
        geocode_cache,
    )
    try:
        saved = save_provider_configuration(
            instructor_id,
            display_name=request.display_name,
            business_name=request.business_name,
            timezone_name=request.timezone,
            base_address=request.base_address,
            base_coords=base_coords,
            service_address=request.service_address,
            service_coords=service_coords,
            service_name=request.service_name,
            default_duration_mins=request.default_duration_mins,
            working_hours=request.working_hours,
        )
    except ProviderNotFound as exc:
        raise ResourceNotFound("The instructor could not be found.") from exc
    return _provider_settings_response(saved)


def _provider_settings_response(
    configuration: ProviderConfiguration,
) -> CoachProviderSettingsResponse:
    return CoachProviderSettingsResponse(
        id=configuration.id,
        display_name=configuration.display_name,
        business_name=configuration.business_name,
        timezone=configuration.timezone,
        base_location=_provider_location_response(configuration.base_location),
        service=CoachProviderServiceResponse(
            name=configuration.service_name,
            default_duration_mins=configuration.default_duration_mins,
            location=_provider_location_response(configuration.service_location),
        ),
        working_hours=[
            CoachProviderWorkingHours(**hours.__dict__)
            for hours in configuration.working_hours
        ],
        setup_complete=configuration.setup_complete,
        setup_missing_fields=list(configuration.setup_missing_fields),
    )


def _provider_location_response(
    location: Optional[ProviderLocation],
) -> Optional[CoachProviderLocationResponse]:
    if location is None:
        return None
    return CoachProviderLocationResponse(
        address=location.address,
        lat=location.lat,
        lng=location.lng,
    )


def _provider_location_coordinates(
    address: str,
    current: Optional[ProviderLocation],
    field: str,
    cache: dict[str, tuple[float, float]],
) -> tuple[float, float]:
    normalized = address.strip()
    if current is not None and normalized == current.address:
        return current.lat, current.lng
    cache_key = normalized.casefold()
    if cache_key in cache:
        return cache[cache_key]
    try:
        lat, lng = tools.routing_provider.geocode(normalized)
    except GeocodeFailed as exc:
        raise GeocodingFailedApiError({"field": field}) from exc
    except RoutingTimeout as exc:
        raise RoutingTimeoutApiError() from exc
    except RoutingProviderUnavailable as exc:
        raise RoutingProviderUnavailableApiError() from exc
    except RoutingProviderNotConfigured as exc:
        raise RoutingConfigurationError() from exc
    except RouteEstimateFailed as exc:
        raise RoutingEstimateFailedApiError() from exc
    if (
        not math.isfinite(lat)
        or not math.isfinite(lng)
        or not -90 <= lat <= 90
        or not -180 <= lng <= 180
    ):
        raise GeocodingFailedApiError({"field": field})
    cache[cache_key] = (lat, lng)
    return cache[cache_key]


def get_coach_command_center_snapshot(
    instructor_id: str,
    snapshot_date: date,
) -> CoachCommandCenterSnapshot:
    try:
        configuration = load_provider_configuration(instructor_id)
    except ProviderNotFound as exc:
        raise ResourceNotFound("The instructor could not be found.") from exc
    service_location = configuration.service_location
    first_enabled = next(
        (row for row in configuration.working_hours if row.enabled),
        None,
    )
    coach_instructor = CoachInstructorProfileResponse(
        id=configuration.id,
        name=configuration.display_name,
        main_address=service_location.address if service_location else "",
        lat=service_location.lat if service_location else 0.0,
        lng=service_location.lng if service_location else 0.0,
        work_start_time=(first_enabled.start_time if first_enabled else None) or "",
        work_end_time=(first_enabled.end_time if first_enabled else None) or "",
        timezone=configuration.timezone,
        business_name=configuration.presentation_business_name,
        base_location=_provider_location_response(configuration.base_location),
        service=CoachProviderServiceResponse(
            name=configuration.service_name,
            default_duration_mins=configuration.default_duration_mins,
            location=_provider_location_response(configuration.service_location),
        ),
        working_hours=[
            CoachProviderWorkingHours(**hours.__dict__)
            for hours in configuration.working_hours
        ],
        setup_complete=configuration.setup_complete,
        setup_missing_fields=list(configuration.setup_missing_fields),
    )
    clients = [
        _client_response(client, coach_instructor)
        for client in db.get_clients_for_instructor(instructor_id)
    ]
    schedule = [
        coach_schedule_item(appointment, service_address=coach_instructor.main_address)
        for appointment in db.get_local_appointments_for_date(
            instructor_id, snapshot_date.isoformat(), timezone_name=configuration.timezone
        )
    ]
    requests = [
        _coach_lesson_request_response(request)
        for request in db.get_pending_lesson_requests_for_instructor(instructor_id)
    ]
    rules, scheduling_policy = load_scheduler_settings_projection(
        instructor_id,
        operation="command_center_snapshot",
    )
    return CoachCommandCenterSnapshot(
        instructor=coach_instructor,
        date=snapshot_date,
        clients=clients,
        schedule=schedule,
        requests=requests,
        email_deliveries=list_coach_email_deliveries(instructor_id),
        rules=rules,
        scheduling_policy=scheduling_policy,
    )


def get_coach_scheduler_settings(instructor_id: str) -> CoachSchedulerRules:
    return load_scheduler_rules(instructor_id, operation="read_settings")


def get_coach_scheduling_policy(
    instructor_id: str,
) -> CoachSchedulingPolicyResponse:
    return load_scheduling_policy(instructor_id, operation="read_scheduling_policy")


def update_coach_scheduling_policy(
    instructor_id: str,
    update: CoachSchedulingPolicyUpdate,
) -> CoachSchedulingPolicyResponse:
    current = load_scheduling_policy(
        instructor_id,
        operation="update_scheduling_policy",
    )
    if update.expected_revision != current.revision:
        raise SchedulingPolicyStale()

    current_ids = [tier.id for tier in current.distance_buffer_tiers]
    update_ids = [tier.id for tier in update.distance_buffer_tiers]
    if update_ids != current_ids:
        raise SchedulingPolicyValidationError(
            "Distance buffer tier ids must exactly match the saved server order."
        )

    buffer_minutes_by_id = {
        tier.id: tier.buffer_mins for tier in update.distance_buffer_tiers
    }
    merged_tiers = [
        tier.model_copy(update={"buffer_mins": buffer_minutes_by_id[tier.id]})
        for tier in current.distance_buffer_tiers
    ]
    candidate = CoachSchedulingPolicyResponse(
        revision=current.revision,
        distance_buffers_enabled=update.distance_buffers_enabled,
        post_break_recovery_mins=update.post_break_recovery_mins,
        distance_buffer_tiers=merged_tiers,
    )
    saved = save_scheduling_policy(
        instructor_id,
        expected_revision=update.expected_revision,
        policy=candidate,
    )
    if saved is None:
        raise SchedulingPolicyStale()
    return saved


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
                blocker_kind=appointment["blocker_kind"],
                revision=appointment["revision"],
            )
            for appointment in appointments
        ],
    )


def get_coach_blockers(
    instructor_id: str,
    blocker_date: date,
) -> list[CoachBlockerResponse]:
    configuration = _blocker_provider_configuration(instructor_id)
    return [
        _blocker_response(row, configuration.timezone)
        for row in db.list_blockers_for_date(
            instructor_id,
            blocker_date.isoformat(),
            configuration.timezone,
        )
    ]


def create_coach_blocker(
    instructor_id: str,
    request: CoachBlockerMutation,
    idempotency_key: str,
) -> CoachBlockerResponse:
    replay = _blocker_creation_replay(instructor_id, idempotency_key, request)
    if replay is not None:
        return replay
    configuration = _blocker_provider_configuration(instructor_id)
    _require_blocker_timezone(request, configuration.timezone)
    start_time, end_time = _blocker_instants(request, configuration.timezone)
    address, lat, lng = _blocker_location(request)
    try:
        row = db.create_blocker(
            instructor_id,
            kind=request.kind,
            title=request.title,
            start_time=start_time,
            end_time=end_time,
            location_address=address,
            lat=lat,
            lng=lng,
            idempotency_key=idempotency_key,
            timezone_name=configuration.timezone,
        )
    except db.IdempotencyKeyConflictError as exc:
        replay = _blocker_creation_replay(instructor_id, idempotency_key, request)
        if replay is not None:
            return replay
        raise IdempotencyConflict() from exc
    except db.ScheduleConflictError as exc:
        raise SchedulingConflict(
            "That event overlaps another active schedule item."
        ) from exc
    return _blocker_response(
        row,
        row.get("creation_timezone", configuration.timezone),
    )


def _blocker_creation_replay(
    instructor_id: str,
    idempotency_key: str,
    request: CoachBlockerMutation,
) -> Optional[CoachBlockerResponse]:
    try:
        existing = db.get_blocker_creation_by_idempotency_key(
            instructor_id,
            idempotency_key,
        )
    except db.IdempotencyKeyConflictError as exc:
        raise IdempotencyConflict() from exc
    if existing is None:
        return None
    try:
        start_time, end_time = _blocker_instants(
            request,
            existing["creation_timezone"],
        )
    except ApiException as exc:
        raise IdempotencyConflict() from exc
    if not _blocker_request_matches_row(existing, request, start_time, end_time):
        raise IdempotencyConflict()
    return _blocker_response(existing, existing["creation_timezone"])


def update_coach_blocker(
    instructor_id: str,
    blocker_id: str,
    request: CoachBlockerUpdate,
) -> CoachBlockerResponse:
    configuration = _blocker_provider_configuration(instructor_id)
    existing = db.get_active_blocker(instructor_id, blocker_id)
    if existing is None:
        raise ResourceNotFound("The schedule event could not be found.")
    if existing["revision"] != request.expected_revision:
        raise BlockerStale()
    _require_blocker_timezone(request, configuration.timezone)
    start_time, end_time = _blocker_instants(request, configuration.timezone)
    address, lat, lng = _blocker_location(request)
    try:
        row = db.update_blocker(
            instructor_id,
            blocker_id,
            expected_revision=request.expected_revision,
            kind=request.kind,
            title=request.title,
            start_time=start_time,
            end_time=end_time,
            location_address=address,
            lat=lat,
            lng=lng,
            timezone_name=configuration.timezone,
        )
    except db.BlockerNotFoundError as exc:
        raise ResourceNotFound("The schedule event could not be found.") from exc
    except db.BlockerStaleError as exc:
        raise BlockerStale() from exc
    except db.ScheduleConflictError as exc:
        raise SchedulingConflict(
            "That event overlaps another active schedule item."
        ) from exc
    return _blocker_response(row, configuration.timezone)


def delete_coach_blocker(
    instructor_id: str,
    blocker_id: str,
    expected_revision: int,
) -> None:
    try:
        db.delete_blocker(
            instructor_id,
            blocker_id,
            expected_revision=expected_revision,
        )
    except db.BlockerNotFoundError as exc:
        raise ResourceNotFound("The schedule event could not be found.") from exc
    except db.BlockerStaleError as exc:
        raise BlockerStale() from exc


def _blocker_provider_configuration(instructor_id: str) -> ProviderConfiguration:
    try:
        configuration = load_provider_configuration(instructor_id)
    except ProviderNotFound as exc:
        raise ResourceNotFound("The instructor could not be found.") from exc
    try:
        timezones.get_zone(configuration.timezone)
    except timezones.InvalidTimezoneError as exc:
        raise ProviderSetupRequired(["timezone"]) from exc
    return configuration


def _require_blocker_timezone(request: CoachBlockerMutation, timezone_name: str) -> None:
    if request.expected_timezone is not None and request.expected_timezone != timezone_name:
        raise ApiException(
            "BLOCKER_TIMEZONE_CHANGED",
            "Your provider time zone changed. Reload Schedule before saving this event.",
            409,
        )


def _blocker_instants(
    request: CoachBlockerMutation,
    timezone_name: str,
) -> tuple[datetime, datetime]:
    start_time = _blocker_instant(
        request.date,
        request.start_time,
        timezone_name,
        "start_time",
    )
    end_time = _blocker_instant(
        request.date,
        request.end_time,
        timezone_name,
        "end_time",
    )
    if end_time <= start_time:
        raise ApiException(
            "VALIDATION_ERROR",
            "End time must be later than start time.",
            422,
            {"field": "end_time"},
        )
    return start_time, end_time


def _blocker_instant(
    blocker_date: date,
    value: str,
    timezone_name: str,
    field: str,
) -> datetime:
    try:
        return timezones.parse_local_date_time(blocker_date, value, timezone_name)
    except timezones.NonexistentLocalTimeError as exc:
        raise ApiException(
            "VALIDATION_ERROR",
            "That local time does not exist because of daylight saving time.",
            422,
            {"field": field, "reason": "nonexistent_local_time"},
        ) from exc
    except timezones.AmbiguousLocalTimeError as exc:
        raise ApiException(
            "VALIDATION_ERROR",
            "That local time occurs twice because of daylight saving time. Choose another time.",
            422,
            {"field": field, "reason": "ambiguous_local_time"},
        ) from exc


def _blocker_location(
    request: CoachBlockerMutation,
) -> tuple[Optional[str], Optional[float], Optional[float]]:
    if request.location_address is None:
        return None, None, None
    lat, lng = _provider_location_coordinates(
        request.location_address,
        None,
        "location_address",
        {},
    )
    return request.location_address, lat, lng


def _blocker_request_matches_row(
    row: dict,
    request: CoachBlockerMutation,
    start_time: datetime,
    end_time: datetime,
) -> bool:
    return (
        row["event_type"] == "block"
        and row["blocker_kind"] == request.kind
        and row["title"] == request.title
        and timezones.to_utc_storage(row["start_time"])
        == timezones.to_utc_storage(start_time)
        and timezones.to_utc_storage(row["end_time"])
        == timezones.to_utc_storage(end_time)
        and row["location_address"] == request.location_address
    )


def _blocker_response(
    row: dict,
    timezone_name: str,
) -> CoachBlockerResponse:
    location = None
    if (
        row["location_address"] is not None
        and row["lat"] is not None
        and row["lng"] is not None
    ):
        location = LocationResponse(
            address=row["location_address"],
            lat=row["lat"],
            lng=row["lng"],
            is_instructor_facility=False,
        )
    start_time = row["start_time"]
    end_time = row["end_time"]
    return CoachBlockerResponse(
        id=row["id"],
        kind=row["blocker_kind"] or "break",
        title=row["title"],
        start_time=start_time,
        end_time=end_time,
        local_date=start_time.date(),
        local_start_time=start_time.strftime("%H:%M"),
        local_end_time=end_time.strftime("%H:%M"),
        timezone=timezone_name,
        revision=row["revision"],
        location=location,
    )


def _client_response(client: dict, instructor: CoachInstructorProfileResponse) -> CoachClientResponse:
    service_location = instructor.service.location
    fallback_address = service_location.address if service_location else instructor.main_address
    fallback_lat = service_location.lat if service_location else instructor.lat
    fallback_lng = service_location.lng if service_location else instructor.lng
    address = client["default_location_address"] or fallback_address
    lat = client["default_lat"] if client["default_lat"] is not None else fallback_lat
    lng = client["default_lng"] if client["default_lng"] is not None else fallback_lng
    is_facility = (
        service_location is not None
        and address == service_location.address
        and lat == service_location.lat
        and lng == service_location.lng
    )
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


def coach_schedule_item(
    appointment: dict,
    *,
    service_address: str | None,
) -> CoachScheduleItemResponse:
    is_blocker = appointment["event_type"] == "block"
    blocker_kind = appointment["blocker_kind"] or "break"
    has_location = (
        appointment["location_address"] is not None
        and appointment["lat"] is not None
        and appointment["lng"] is not None
    )
    is_facility = has_location and appointment["location_address"] == service_address
    title = appointment["title"]
    if not is_blocker:
        title = title.replace("Lesson with ", "", 1)
    location = None
    if has_location:
        location = LocationResponse(
            address=appointment["location_address"],
            lat=appointment["lat"],
            lng=appointment["lng"],
            is_instructor_facility=is_facility,
        )
    if is_blocker:
        return CoachBlockerScheduleItemResponse(
            id=appointment["id"],
            kind=blocker_kind,
            title=title,
            start_time=appointment["start_time"],
            end_time=appointment["end_time"],
            location=location,
            revision=appointment["revision"],
        )
    if location is None or appointment["client_id"] is None:
        raise RuntimeError("Active lessons require a client and complete location.")
    return CoachLessonScheduleItemResponse(
        id=appointment["id"],
        kind="lesson",
        client_id=appointment["client_id"],
        title=title,
        start_time=appointment["start_time"],
        end_time=appointment["end_time"],
        location=location,
        status=_lesson_status(appointment["status"]),
    )


def _coach_lesson_request_response(request: dict) -> CoachLessonRequestResponse:
    timezone_name = _instructor_timezone(request["instructor_id"])
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
            id=request["original_slot_offer_id"] or f"slot_{request['request_token']}",
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


def search_coach_availability(
    instructor_id: str,
    request: AvailabilitySearchRequest,
    *,
    calendar_busy_source: Optional[CalendarBusySource] = None,
) -> AvailabilitySearchResponse:
    try: slots = _find_slots(
            calendar_busy_source=(
                calendar_busy_source,
                (calendar_busy_source := None),
            )[0],
            instructor_id=instructor_id,
            requested_date=request.requested_date,
            client_address=request.client_address,
            is_instructor_facility=request.is_instructor_facility,
            duration_mins=request.duration_mins,
        )
    finally:
        try: calendar_busy_source = None
        except BaseException:
            calendar_busy_source = None
            raise
    return AvailabilitySearchResponse(
        status="PRESENTING",
        client_name=request.client_name,
        requested_date=request.requested_date,
        duration_mins=request.duration_mins,
        location=_resolve_location(instructor_id, request.client_address, request.is_instructor_facility),
        available_slots=[_slot_response(slot) for slot in slots],
    )


def search_coach_availability_range(
    instructor_id: str,
    request: CoachAvailabilityRangeRequest,
    *,
    build_calendar_source: Callable[[Callable[[], Session]], CalendarBusySource],
) -> CoachAvailabilityRangeResponse:
    try:
        result = search_one_time_availability(
            instructor_id, request,
            client_address=request.client_address or "",
            is_instructor_facility=request.is_instructor_facility,
            build_calendar_source=build_calendar_source,
        )
        return CoachAvailabilityRangeResponse(
            start_date=request.start_date,
            end_date=request.end_date,
            duration_mins=request.duration_mins,
            timezone=result.timezone,
            location=LocationResponse(**result.location.model_dump()),
            available_slots=[_slot_response(slot) for slot in result.slots],
            empty_reason=result.empty_reason,
        )
    except ExternalCalendarUnavailable:
        pass
    except GeocodeFailed:
        raise GeocodingFailedApiError({"field": "client_address"}) from None
    except RoutingTimeout:
        raise RoutingTimeoutApiError() from None
    except RoutingProviderUnavailable:
        raise RoutingProviderUnavailableApiError() from None
    except RoutingProviderNotConfigured:
        raise RoutingConfigurationError() from None
    except RouteEstimateFailed:
        raise RoutingEstimateFailedApiError() from None
    except ProviderSetupIncomplete as exc:
        raise ProviderSetupRequired(list(exc.missing_fields)) from None
    except ProviderNotFound:
        raise ResourceNotFound("The instructor could not be found.") from None
    finally:
        del build_calendar_source
    raise CalendarAvailabilityUnavailable() from None


INSTRUCTOR_CALENDAR_TITLE = "Appointment"
INSTRUCTOR_CALENDAR_DESCRIPTION = "Confirmed appointment."


def get_coach_calendar_links(
    instructor_id: str,
    appointment_id: str,
) -> ClientInviteCalendarLinks:
    appointments = _load_coach_calendar_appointments(instructor_id, appointment_id)
    download_url = _coach_calendar_download_url(appointment_id)
    return calendar_links(
        appointment_id,
        appointments,
        download_url=download_url,
        title=INSTRUCTOR_CALENDAR_TITLE,
        description=INSTRUCTOR_CALENDAR_DESCRIPTION,
    )


def get_coach_calendar_ics(instructor_id: str, appointment_id: str) -> str:
    appointments = _load_coach_calendar_appointments(instructor_id, appointment_id)
    return calendar_ics(
        appointments,
        title=INSTRUCTOR_CALENDAR_TITLE,
        description=INSTRUCTOR_CALENDAR_DESCRIPTION,
    )


def _coach_calendar_download_url(appointment_id: str) -> str:
    encoded_id = quote(appointment_id, safe="")
    return f"/api/coach/appointments/{encoded_id}/calendar.ics"


def _load_coach_calendar_appointments(
    instructor_id: str,
    appointment_id: str,
) -> list[AppointmentDB]:
    session = db.SessionLocal()
    try:
        anchor = (
            session.query(AppointmentDB)
            .filter(
                AppointmentDB.id == appointment_id,
                AppointmentDB.instructor_id == instructor_id,
            )
            .first()
        )
        if anchor is None:
            raise ResourceNotFound("The appointment could not be found.")

        is_lesson_anchor = anchor.event_type == "lesson" and not anchor.is_break
        if not is_lesson_anchor:
            raise CalendarLinksUnavailable()

        query = session.query(AppointmentDB).filter(
            AppointmentDB.instructor_id == instructor_id,
            AppointmentDB.event_type == "lesson",
            AppointmentDB.status == "scheduled",
            AppointmentDB.is_break.is_(False),
            AppointmentDB.cancelled_at.is_(None),
        )
        if anchor.request_id is None:
            query = query.filter(AppointmentDB.id == anchor.id)
        else:
            parent_query = session.query(LessonRequestDB).join(
                InviteTokenDB,
                LessonRequestDB.invite_token == InviteTokenDB.token,
            )
            if not demo_data_access_enabled_from_env():
                parent_query = parent_query.filter(InviteTokenDB.is_demo.is_(False))
            parent = (
                parent_query
                .filter(
                    LessonRequestDB.id == anchor.request_id,
                    LessonRequestDB.instructor_id == instructor_id,
                )
                .first()
            )
            if parent is None:
                raise ResourceNotFound("The appointment could not be found.")
            if parent.status != "confirmed":
                raise CalendarLinksUnavailable()
            query = query.filter(AppointmentDB.request_id == anchor.request_id)

        appointments = query.order_by(
            AppointmentDB.start_time.asc(),
            AppointmentDB.calendar_uid.asc(),
        ).all()
        if not appointments:
            raise CalendarLinksUnavailable()
        for appointment in appointments:
            session.expunge(appointment)
        return appointments
    finally:
        session.close()


def _optional_coach_calendar_links(
    instructor_id: str,
    appointment_ids: list[str],
) -> Optional[ClientInviteCalendarLinks]:
    if not appointment_ids:
        return None
    try:
        return get_coach_calendar_links(instructor_id, appointment_ids[0])
    except Exception:
        # Calendar handoff is secondary to an already committed booking. Never
        # turn an artifact failure into a misleading persistence failure.
        return None


def book_coach_lesson(
    instructor_id: str,
    request: CoachBookLessonRequest,
    *,
    build_calendar_source: Optional[Callable[[], CalendarBusySource]] = None,
) -> BookLessonResponse:
    client_id = request.client_id or _client_id_from_name(request.client_name)
    try:
        provider = load_provider_configuration(instructor_id)
    except ProviderNotFound as exc:
        raise ResourceNotFound("The instructor could not be found.") from exc
    try:
        existing = db.get_appointment_by_idempotency_key(
            instructor_id,
            request.idempotency_key,
        )
    except db.IdempotencyKeyConflictError as exc:
        raise IdempotencyConflict() from exc
    try:
        requested_start = _canonical_request_time(request.start_time, provider.timezone)
    except timezones.InvalidTimezoneError as exc:
        if existing is not None and _is_aware(request.start_time):
            requested_start = request.start_time
            _validate_coach_booking_replay(existing, instructor_id, client_id, request, requested_start)
            return _coach_booking_replay_response(existing, request, provider.timezone)
        raise ProviderSetupRequired(list(provider.setup_missing_fields)) from exc
    except (timezones.AmbiguousLocalTimeError, timezones.NonexistentLocalTimeError) as exc:
        raise InvalidSchedulingRequest("The requested local time is invalid for the instructor timezone.") from exc
    if existing:
        _validate_coach_booking_replay(existing, instructor_id, client_id, request, requested_start)
        return _coach_booking_replay_response(existing, request, provider.timezone)

    if not provider.setup_complete:
        raise ProviderSetupRequired(list(provider.setup_missing_fields))

    client_name = request.client_name
    client_address = request.client_address
    if request.client_id is not None:
        client = db.get_client_profile_for_instructor(instructor_id, client_id)
        if client is None:
            raise ResourceNotFound("The client could not be found.")
        client_address = client_address.strip()
        at_facility = client["location_mode"] == "service_location"
        saved_location = (
            _client_service_location_response(provider.service_location)
            if at_facility else _client_profile_location_response(client)
        )
        if (
            at_facility != request.is_instructor_facility
            or saved_location is None
            or saved_location.address.strip() != client_address
        ):
            raise SchedulingConflict("The saved client location changed. Find new times for this client.")
        client_name = client["name"]

    policy = load_buffer_policy(instructor_id, operation="coach_booking")
    location = _resolve_location(instructor_id, client_address, request.is_instructor_facility)
    try:
        result = tools.book_swim_lesson(
            instructor_id=instructor_id,
            client_id=client_id,
            client_name=client_name,
            start_time_iso=requested_start.isoformat(),
            duration_mins=request.duration_mins,
            location_address=location.address,
            expected_location_address=client_address if request.client_id is not None else None,
            target_location=LocationInfo(**location.model_dump()),
            idempotency_key=request.idempotency_key,
            is_instructor_facility=request.is_instructor_facility,
            buffer_policy=policy,
            calendar_busy_source=(
                build_calendar_source() if build_calendar_source is not None else None
            ),
        )
    except ExternalCalendarUnavailable:
        result = None
    except db.ScheduleConflictError as exc:
        raise SchedulingConflict() from exc
    except db.IdempotencyKeyConflictError as exc:
        raise IdempotencyConflict() from exc
    except ProviderSetupIncomplete as exc:
        raise ProviderSetupRequired(list(exc.missing_fields)) from exc
    finally:
        del build_calendar_source
    if result is None:
        _raise_calendar_availability_unavailable()
    return BookLessonResponse(
        **result,
        calendar_links=_optional_coach_calendar_links(instructor_id, [result["appointment_id"]]),
    )


def _validate_coach_booking_replay(
    existing: dict,
    instructor_id: str,
    client_id: str,
    request: CoachBookLessonRequest,
    requested_start: datetime,
) -> None:
    requested_start_storage = timezones.to_utc_storage(requested_start)
    requested_end_storage = requested_start_storage + _duration_delta(request.duration_mins)
    if (
        existing["instructor_id"] != instructor_id
        or existing["client_id"] != client_id
        or (
            request.client_id is not None
            and (existing["location_address"] or "").strip() != request.client_address.strip()
        )
        or timezones.to_utc_storage(existing["start_time"]) != requested_start_storage
        or timezones.to_utc_storage(existing["end_time"]) != requested_end_storage
    ):
        raise IdempotencyConflict()


def _coach_booking_replay_response(
    existing: dict,
    request: CoachBookLessonRequest,
    timezone_name: str,
) -> BookLessonResponse:
    readable_start, used_authoritative_timezone = _stored_time_for_response(
        existing["start_time"],
        timezone_name,
    )
    timezone_suffix = "" if used_authoritative_timezone else " UTC"
    client_name = (
        existing["title"].removeprefix("Lesson with ")
        if request.client_id is not None
        else request.client_name
    )
    return BookLessonResponse(
        success=True,
        appointment_id=existing["id"],
        message=(
            f"Successfully booked swim lesson for {client_name} on "
            f"{readable_start.strftime('%b %d, %Y at %I:%M %p')}"
            f"{timezone_suffix}!"
        ),
        calendar_links=_optional_coach_calendar_links(
            existing["instructor_id"],
            [existing["id"]],
        ),
    )


def approve_coach_lesson_request(
    instructor_id: str,
    request_token: str,
    *,
    build_calendar_source: Callable[[], CalendarBusySource] | None = None,
) -> CoachLessonRequestActionResponse:
    confirmation = confirm_coach_lesson_request(
        instructor_id,
        request_token,
        CoachLessonRequestConfirmRequest(),
        build_calendar_source=build_calendar_source,
    )
    return CoachLessonRequestActionResponse(
        id=confirmation.id,
        status="approved",
        appointment_id=confirmation.appointment_id,
        message=confirmation.message,
    )


def confirm_coach_lesson_request(
    instructor_id: str,
    request_token: str,
    request: CoachLessonRequestConfirmRequest,
    *,
    build_calendar_source: Callable[[], CalendarBusySource] | None = None,
) -> CoachLessonRequestConfirmResponse:
    calendar = None
    calendar_check_failed = False
    if build_calendar_source is not None:
        try:
            calendar = _prepare_confirmation_calendar(
                instructor_id, request_token, request.slot_id, build_calendar_source
            )
        except CalendarAvailabilityUnavailable:
            calendar_check_failed = True
    session = db.SessionLocal()
    try:
        lesson_request = _load_request_for_confirmation(session, instructor_id, request_token, lock=True)

        existing_appointments = _appointments_for_request(
            session,
            instructor_id,
            lesson_request.id,
        )
        if lesson_request.status == "confirmed" or existing_appointments:
            appointment_ids = [appointment.id for appointment in existing_appointments]
            if not _confirmed_slot_matches(lesson_request, request.slot_id):
                raise LessonRequestConfirmConflict()
            return _lesson_request_confirmation_response(
                lesson_request,
                appointment_ids,
                calendar_links=_optional_coach_calendar_links(instructor_id, appointment_ids),
                email_delivery=coach_email_delivery_for_request(
                    session,
                    instructor_id,
                    lesson_request.id,
                ),
            )
        if lesson_request.status not in {"pending", "alternatives_sent"}:
            raise InvalidSchedulingRequest("Only pending lesson requests can be confirmed.")
        if calendar_check_failed:
            _raise_calendar_availability_unavailable()

        _require_coach_provider_setup(lesson_request.instructor_id)
        explicit_offer = _resolve_confirm_offer(session, lesson_request, request.slot_id)
        if explicit_offer is not None:
            _apply_offer_to_request(lesson_request, explicit_offer)
        confirmed_alternative = explicit_offer or _selected_alternative_offer(session, lesson_request)
        occurrences = _request_occurrences(lesson_request)
        _acquire_confirmation_locks(session, lesson_request.instructor_id, occurrences)

        policy = load_buffer_policy(instructor_id, operation="confirm_lesson_request")
        try:
            try:
                _validate_current_facility_request(lesson_request)
                location = _validate_occurrences(lesson_request, occurrences, policy, calendar_busy_source=calendar)
            except SchedulingConflict:
                _lock_confirmation_calendar(session, calendar)
                raise
            _lock_confirmation_calendar(session, calendar)
            lesson_request.lat, lesson_request.lng = location.lat, location.lng
            appointments = _insert_occurrence_appointments(session, lesson_request, occurrences)
            # Force database overlap and integrity checks while they can still
            # be translated into the canonical scheduling-conflict contract.
            session.flush()
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
        lesson_request.confirmed_slot_offer_id = (
            confirmed_alternative.id if confirmed_alternative is not None else None
        )
        lesson_request.validation_failure_code = None
        lesson_request.validation_failure_details = None
        lesson_request.validation_failed_at = None
        lesson_request.updated_at = now
        from app.notifications import enqueue_confirmation
        enqueue_confirmation(session, lesson_request, appointments)
        email_delivery = coach_email_delivery_for_request(
            session,
            instructor_id,
            lesson_request.id,
        )
        session.commit()
        appointment_ids = [appointment.id for appointment in appointments]
        return _lesson_request_confirmation_response(
            lesson_request,
            appointment_ids,
            calendar_links=_optional_coach_calendar_links(instructor_id, appointment_ids),
            email_delivery=email_delivery,
        )
    finally:
        calendar = None
        session.close()


@dataclass(frozen=True, repr=False)
class _ConfirmationCalendar:
    instructor_id: str
    snapshots: dict[tuple[datetime, datetime], CalendarBusySnapshot]
    fence: GoogleCalendarAvailabilityFence | None = None

    def query_busy_periods(
        self, *, instructor_id: str, window_start: datetime, window_end: datetime
    ) -> CalendarBusySnapshot:
        if instructor_id == self.instructor_id:
            for (start, end), snapshot in self.snapshots.items():
                if start <= window_start and window_end <= end:
                    return snapshot
        raise ExternalCalendarUnavailable("provider_unavailable")


def _lock_confirmation_calendar(session, calendar: _ConfirmationCalendar | None) -> None:
    if calendar is not None and calendar.fence is not None:
        try:
            lock_current_google_calendar_availability_fence(session, fence=calendar.fence)
        except ExternalCalendarUnavailable:
            calendar = None
        else:
            return
        _raise_calendar_availability_unavailable()


def _prepare_confirmation_calendar(
    instructor_id: str,
    request_token: str,
    slot_id: str | None,
    build_calendar_source: Callable[[], CalendarBusySource],
) -> _ConfirmationCalendar:
    with db.SessionLocal() as session:
        lesson_request = _load_request_for_confirmation(session, instructor_id, request_token)
        if lesson_request.status == "confirmed" or _appointments_for_request(
            session, instructor_id, lesson_request.id
        ):
            return _ConfirmationCalendar(instructor_id, {})
        if lesson_request.status not in {"pending", "alternatives_sent"}:
            raise InvalidSchedulingRequest("Only pending lesson requests can be confirmed.")
        provider = _require_coach_provider_setup(instructor_id)
        offer = _resolve_confirm_offer(session, lesson_request, slot_id)
        if offer is not None:
            session.expunge(lesson_request)
            _apply_offer_to_request(lesson_request, offer)
        occurrences = _request_occurrences(lesson_request, timezone_name=provider.timezone)
        dates = {timezones.storage_to_local(start, provider.timezone).date() for start, _ in occurrences}
        windows = set()
        for local_date in dates:
            working = tools.working_window_utc(provider, local_date)
            if working is None:
                continue
            events = tools.routing_events_for_date(instructor_id, local_date, provider.timezone, session=session)
            windows.add(tools.routing_calendar_window(*working, events))
    if not windows:
        return _ConfirmationCalendar(instructor_id, {})
    try:
        fence = claim_google_calendar_availability_fence(db.SessionLocal, instructor_id=instructor_id)
        source = build_calendar_source()
        snapshots = {
            (start, end): query_calendar_busy_snapshot(
                source, instructor_id=instructor_id, window_start=start, window_end=end
            )
            for start, end in sorted(windows)
        }
    except ExternalCalendarUnavailable:
        pass
    else:
        return _ConfirmationCalendar(instructor_id, snapshots, fence)
    _raise_calendar_availability_unavailable()


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
        if lesson_request.status == "confirmed" or _appointments_for_request(
            session,
            instructor_id,
            lesson_request.id,
        ):
            raise InvalidSchedulingRequest("Confirmed lesson requests cannot be marked for new times.")
        provider = _require_coach_provider_setup(lesson_request.instructor_id)
        policy = load_buffer_policy(
            lesson_request.instructor_id,
            operation="send_lesson_request_alternatives",
        )
        promoted = _promote_alternative_offers(
            session,
            lesson_request,
            body,
            provider,
            policy,
        )
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
        if lesson_request.status == "confirmed" or _appointments_for_request(
            session,
            instructor_id,
            lesson_request.id,
        ):
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
    query = session.query(LessonRequestDB).join(
        InviteTokenDB,
        LessonRequestDB.invite_token == InviteTokenDB.token,
    )
    if not demo_data_access_enabled_from_env():
        query = query.filter(InviteTokenDB.is_demo.is_(False))
    return query.filter(
        LessonRequestDB.instructor_id == instructor_id,
        LessonRequestDB.request_token == request_token,
    )


def _load_request_for_confirmation(
    session, instructor_id: str, request_token: str, *, lock: bool = False
) -> LessonRequestDB:
    query = _query_coach_lesson_request(session, instructor_id, request_token)
    lesson_request = (query.with_for_update() if lock else query).first()
    if lesson_request is None:
        raise LessonRequestNotFound()
    if _is_request_expired(lesson_request):
        raise LessonRequestExpired()
    return lesson_request


def _appointments_for_request(
    session,
    instructor_id: str,
    request_id: str,
) -> list[AppointmentDB]:
    return (
        session.query(AppointmentDB)
        .filter_by(instructor_id=instructor_id, request_id=request_id)
        .order_by(AppointmentDB.start_time.asc())
        .all()
    )


def _lesson_request_confirmation_response(
    lesson_request: LessonRequestDB,
    appointment_ids: list[str],
    *,
    calendar_links: Optional[ClientInviteCalendarLinks] = None,
    email_delivery: Optional[CoachEmailDeliveryResponse] = None,
) -> CoachLessonRequestConfirmResponse:
    return CoachLessonRequestConfirmResponse(
        id=lesson_request.request_token,
        status="confirmed",
        appointment_id=appointment_ids[0] if appointment_ids else None,
        appointment_ids=appointment_ids,
        message="Lesson request is confirmed.",
        calendar_links=calendar_links,
        email_delivery=email_delivery,
    )


def _is_request_expired(lesson_request: LessonRequestDB) -> bool:
    return lesson_request.status not in {"confirmed"} and lesson_request.expires_at <= datetime.utcnow()


def _instructor_timezone(instructor_id: str) -> str:
    instructor = db.get_instructor_info(instructor_id)
    if instructor is None or not instructor.timezone or not instructor.timezone.strip():
        return "UTC"
    try:
        return timezones.get_zone(instructor.timezone if instructor else None).key
    except timezones.InvalidTimezoneError:
        # Read-only responses still need to expose committed instants when a
        # legacy row contains an invalid zone. UTC is an explicit, truthful
        # representation; using the product default would imply provider intent.
        return "UTC"


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
        return None
    offer = (
        session.query(InviteSlotOfferDB)
        .filter(
            InviteSlotOfferDB.id == slot_id,
            InviteSlotOfferDB.lesson_request_id == lesson_request.id,
            InviteSlotOfferDB.invite_token == lesson_request.invite_token,
            InviteSlotOfferDB.instructor_id == lesson_request.instructor_id,
            InviteSlotOfferDB.offer_kind == "alternative",
            (
                (InviteSlotOfferDB.expires_at > datetime.utcnow())
                | (InviteSlotOfferDB.id == lesson_request.original_slot_offer_id)
            ),
        )
        .first()
    )
    if offer is None:
        raise InvalidSchedulingRequest("The selected confirmation slot is not valid for this request.")
    return offer


def _selected_alternative_offer(
    session,
    lesson_request: LessonRequestDB,
) -> Optional[InviteSlotOfferDB]:
    if lesson_request.original_slot_offer_id is None:
        return None
    return (
        session.query(InviteSlotOfferDB)
        .filter(
            InviteSlotOfferDB.id == lesson_request.original_slot_offer_id,
            InviteSlotOfferDB.lesson_request_id == lesson_request.id,
            InviteSlotOfferDB.invite_token == lesson_request.invite_token,
            InviteSlotOfferDB.instructor_id == lesson_request.instructor_id,
            InviteSlotOfferDB.offer_kind == "alternative",
        )
        .first()
    )


def _request_occurrences(
    lesson_request: LessonRequestDB,
    *,
    timezone_name: str | None = None,
) -> list[tuple[datetime, datetime]]:
    if lesson_request.recurrence != "weekly":
        return [(lesson_request.slot_start, lesson_request.slot_end)]
    timezone_name = timezone_name or _instructor_timezone(lesson_request.instructor_id)
    base_local_start = timezones.storage_to_local(lesson_request.slot_start, timezone_name).replace(tzinfo=None)
    base_local_end = timezones.storage_to_local(lesson_request.slot_end, timezone_name).replace(tzinfo=None)
    occurrences: list[tuple[datetime, datetime]] = []
    for index in range(lesson_request.recurrence_count):
        local_start = base_local_start + timedelta(days=7 * index)
        local_end = base_local_end + timedelta(days=7 * index)
        try:
            start = timezones.validate_local_datetime(local_start, timezone_name)
            end = timezones.validate_local_datetime(local_end, timezone_name)
        except (
            timezones.AmbiguousLocalTimeError,
            timezones.NonexistentLocalTimeError,
        ) as exc:
            raise SchedulingConflict(
                "The recurring lesson crosses an unavailable local time. "
                "Send the client new times."
            ) from exc
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
    timezone_name = _instructor_timezone(instructor_id)
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
    buffer_policy: BufferPolicy,
    *,
    calendar_busy_source: CalendarBusySource | None = None,
) -> LocationInfo:
    try:
        timezone_name = _instructor_timezone(lesson_request.instructor_id)
        at_facility = lesson_request.location_type == "facility"
        if at_facility or is_range_slot_offer(lesson_request.original_slot_offer_id):
            location = LocationInfo(
                address=lesson_request.client_address, lat=lesson_request.lat, lng=lesson_request.lng,
                is_instructor_facility=at_facility,
            )
        else:
            resolved = _resolve_location(lesson_request.instructor_id, lesson_request.client_address, False)
            location = LocationInfo(**resolved.model_dump())
        for start, end in occurrences:
            requested_start = timezones.storage_to_aware_utc(start)
            _require_available_start(
                instructor_id=lesson_request.instructor_id,
                requested_date=timezones.requested_local_date(requested_start, timezone_name),
                client_address=lesson_request.client_address,
                is_instructor_facility=lesson_request.location_type == "facility",
                duration_mins=lesson_request.duration_mins,
                requested_start=requested_start,
                requested_end=timezones.storage_to_aware_utc(end),
                buffer_policy=buffer_policy,
                calendar_busy_source=calendar_busy_source,
                target_location=location,
            )
        return location
    finally:
        calendar_busy_source = None


def _validate_current_facility_request(lesson_request: LessonRequestDB) -> None:
    if lesson_request.location_type != "facility":
        return
    try:
        provider = require_provider_setup(lesson_request.instructor_id)
    except ProviderNotFound as exc:
        raise ResourceNotFound("The instructor could not be found.") from exc
    except ProviderSetupIncomplete as exc:
        raise ProviderSetupRequired(list(exc.missing_fields)) from exc
    if not service_location_matches(
        provider,
        address=lesson_request.client_address,
        lat=lesson_request.lat,
        lng=lesson_request.lng,
    ):
        raise SchedulingConflict(
            "The provider service location changed. Send the client new times."
        )


def _require_coach_provider_setup(instructor_id: str) -> ProviderConfiguration:
    try:
        return require_provider_setup(instructor_id)
    except ProviderNotFound as exc:
        raise ResourceNotFound("The instructor could not be found.") from exc
    except ProviderSetupIncomplete as exc:
        raise ProviderSetupRequired(list(exc.missing_fields)) from exc


def _insert_occurrence_appointments(
    session,
    lesson_request: LessonRequestDB,
    occurrences: list[tuple[datetime, datetime]],
) -> list[AppointmentDB]:
    appointments: list[AppointmentDB] = []
    for index, (start, end) in enumerate(occurrences):
        idempotency_key = f"lesson-request-confirm:{lesson_request.id}:{index}"
        existing = (
            session.query(AppointmentDB)
            .filter_by(
                instructor_id=lesson_request.instructor_id,
                idempotency_key=idempotency_key,
            )
            .first()
        )
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
    provider: ProviderConfiguration,
    buffer_policy: BufferPolicy,
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
        _validate_alternative_offer(offer, provider, buffer_policy)
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


def _validate_alternative_offer(
    offer: InviteSlotOfferDB,
    provider: ProviderConfiguration,
    buffer_policy: BufferPolicy,
) -> None:
    if offer.location_type == "facility" and not service_location_matches(
        provider,
        address=offer.location_address,
        lat=offer.lat,
        lng=offer.lng,
    ):
        raise SchedulingConflict(
            "The provider service location changed. Find current times and try again."
        )
    if offer.recurrence == "weekly" and not offer.recurrence_available:
        raise SchedulingConflict(
            "The recurring lesson time is no longer available. Find current times and try again."
        )

    base_local = timezones.storage_to_local(offer.slot_start, provider.timezone).replace(
        tzinfo=None
    )
    if base_local.date() != offer.requested_date:
        raise SchedulingConflict(
            "The provider timezone changed. Find current times and try again."
        )
    count = offer.recurrence_count if offer.recurrence == "weekly" else 1
    for index in range(count):
        local_label = base_local + timedelta(days=7 * index)
        try:
            requested_start = timezones.validate_local_datetime(
                local_label,
                provider.timezone,
            )
        except (
            timezones.AmbiguousLocalTimeError,
            timezones.NonexistentLocalTimeError,
        ) as exc:
            raise SchedulingConflict(
                "The recurring lesson crosses an unavailable local time. "
                "Find current times and try again."
            ) from exc
        _require_available_start(
            instructor_id=offer.instructor_id,
            requested_date=requested_start.date(),
            client_address=offer.client_address,
            is_instructor_facility=offer.location_type == "facility",
            duration_mins=offer.duration_mins,
            requested_start=requested_start,
            buffer_policy=buffer_policy,
        )


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
        service_name=invite.service_name,
        service_location=_provider_location_response(invite.service_location),
        preferred_duration_mins=invite.preferred_duration_mins,
        allowed_durations_mins=list(invite.allowed_durations_mins),
        expires_at=invite.expires_at,
    )


def search_public_availability(
    invite: InviteContext,
    request: PublicAvailabilitySearchRequest,
    *,
    calendar_busy_source: Optional[CalendarBusySource] = None,
) -> PublicAvailabilitySearchResponse:
    try: slots = _find_slots(
            calendar_busy_source=(
                calendar_busy_source,
                (calendar_busy_source := None),
            )[0],
            instructor_id=invite.instructor_id,
            requested_date=request.requested_date,
            client_address=request.client_address,
            is_instructor_facility=(
                is_facility := request.location_type == "facility"
            ),
            duration_mins=request.duration_mins,
            public=True,
        )
    finally:
        try: calendar_busy_source = None
        except BaseException:
            calendar_busy_source = None
            raise
    location = _resolve_location(invite.instructor_id, request.client_address, is_facility)
    return PublicAvailabilitySearchResponse(
        status="PRESENTING",
        requested_date=request.requested_date,
        duration_mins=request.duration_mins,
        location_label=location.address if is_facility else "Client pool",
        available_slots=[_public_slot_response(slot) for slot in slots],
    )


def create_public_lesson_request(
    invite: InviteContext,
    request: PublicLessonRequestCreate,
) -> PublicLessonRequestResponse:
    try:
        provider = load_provider_configuration(invite.instructor_id)
    except ProviderNotFound as exc:
        raise ResourceNotFound("The instructor could not be found.") from exc
    existing = resolve_lesson_request_by_idempotency_key(request.idempotency_key)
    try:
        requested_start = _canonical_request_time(request.slot_start, provider.timezone)
        requested_date = timezones.requested_local_date(requested_start, provider.timezone)
    except timezones.InvalidTimezoneError as exc:
        if existing is not None and _is_aware(request.slot_start):
            requested_start = request.slot_start
            _validate_public_lesson_request_replay(
                existing,
                invite,
                request,
                requested_start,
                request.requested_date,
            )
            return _public_lesson_request_replay_response(
                existing,
                provider.timezone,
            )
        raise ProviderSetupRequired(public=True) from exc
    except (timezones.AmbiguousLocalTimeError, timezones.NonexistentLocalTimeError) as exc:
        raise InvalidSchedulingRequest("The requested local time is invalid for the instructor timezone.") from exc
    if existing:
        _validate_public_lesson_request_replay(
            existing,
            invite,
            request,
            requested_start,
            requested_date,
        )
        return _public_lesson_request_replay_response(existing, provider.timezone)

    if not provider.setup_complete:
        raise ProviderSetupRequired(public=True)

    is_facility = request.location_type == "facility"
    policy = load_buffer_policy(invite.instructor_id, operation="public_request_creation")
    selected_slot = _require_available_start(
        instructor_id=invite.instructor_id,
        requested_date=requested_date,
        client_address=request.client_address,
        is_instructor_facility=is_facility,
        duration_mins=request.duration_mins,
        requested_start=requested_start,
        buffer_policy=policy,
        public=True,
    )
    location = _resolve_location(invite.instructor_id, request.client_address, is_facility)
    try:
        record = create_lesson_request(
            invite_token=invite.token,
            instructor_id=invite.instructor_id,
            client_name=request.client_name,
            client_address=location.address,
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
        selected_slot=_public_slot_response(selected_slot),
        message="Your lesson request was sent to the instructor for confirmation.",
    )


def _validate_public_lesson_request_replay(
    existing: LessonRequestRecord,
    invite: InviteContext,
    request: PublicLessonRequestCreate,
    requested_start: datetime,
    requested_date: date,
) -> None:
    if (
        existing.invite_token != invite.token
        or existing.client_name != request.client_name
        or (
            request.location_type != "facility"
            and existing.client_address != request.client_address
        )
        or existing.requested_date != requested_date
        or timezones.to_utc_storage(existing.selected_slot_start)
        != timezones.to_utc_storage(requested_start)
        or existing.duration_mins != request.duration_mins
        or existing.location_type != request.location_type
    ):
        raise IdempotencyConflict()


def _public_lesson_request_replay_response(
    existing: LessonRequestRecord,
    timezone_name: str,
) -> PublicLessonRequestResponse:
    start_time, start_uses_authoritative_timezone = _stored_time_for_response(
        existing.selected_slot_start,
        timezone_name,
    )
    end_time, end_uses_authoritative_timezone = _stored_time_for_response(
        existing.selected_slot_end,
        timezone_name,
    )
    timezone_label = (
        None
        if start_uses_authoritative_timezone and end_uses_authoritative_timezone
        else "UTC"
    )
    from app.notifications import public_notifications

    with db.SessionLocal() as session:
        notifications = public_notifications(session, existing.id)
    return PublicLessonRequestResponse(
        notifications=notifications,
        request_token=existing.request_token,
        status="pending",
        selected_slot=PublicTimeSlotResponse(
            start_iso=start_time,
            end_iso=end_time,
            display=_format_public_slot_display(
                start_time,
                end_time,
                timezone_label=timezone_label,
            ),
        ),
        message="Your lesson request was sent to the instructor for confirmation.",
    )


def get_public_request_status(request_token: str) -> PublicRequestStatusResponse:
    record = resolve_lesson_request(request_token)
    from app.notifications import public_notifications
    with db.SessionLocal() as session:
        notifications = public_notifications(session, record.id)
    status = _legacy_public_request_status(record.status)
    if record.status != "confirmed" and record.expires_at <= datetime.utcnow():
        status = "expired"
    return PublicRequestStatusResponse(
        request_token=record.request_token,
        status=status,
        message=_legacy_public_request_message(status),
        notifications=notifications,
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
    buffer_policy: Optional[BufferPolicy] = None,
    public: bool = False,
    *,
    calendar_busy_source: Optional[CalendarBusySource] = None,
    requested_start: datetime | None = None,
    target_location: LocationInfo | None = None,
) -> list[TimeSlotDTO]:
    try: control_kind = None; system_exit_code = None; cleanup_failure = False; return _find_slots_evaluated(
            calendar_busy_source=(
                calendar_busy_source,
                (calendar_busy_source := None),
            )[0],
            instructor_id=instructor_id,
            requested_date=requested_date,
            client_address=client_address,
            is_instructor_facility=is_instructor_facility,
            duration_mins=duration_mins,
            buffer_policy=buffer_policy,
            public=public,
            requested_start=requested_start,
            target_location=target_location,
        )
    except asyncio.CancelledError:
        control_kind = "cancelled"
        system_exit_code = None
        cleanup_failure = False
    except KeyboardInterrupt:
        control_kind = "keyboard_interrupt"
        system_exit_code = None
        cleanup_failure = False
    except SystemExit as interrupted:
        try:
            control_kind = "system_exit"
            cleanup_failure = False
            system_exit_code = (
                interrupted.code
                if type(interrupted) is SystemExit
                and (
                    type(interrupted.code) is int
                    or interrupted.code is None
                )
                else 1
            )
            interrupted = None
        except BaseException as cleanup_raised:
            interrupted = None
            if isinstance(cleanup_raised, asyncio.CancelledError):
                control_kind = "cancelled"
                system_exit_code = None
                cleanup_failure = False
            elif isinstance(cleanup_raised, KeyboardInterrupt):
                control_kind = "keyboard_interrupt"
                system_exit_code = None
                cleanup_failure = False
            elif isinstance(cleanup_raised, SystemExit):
                control_kind = "system_exit"
                cleanup_failure = False
                system_exit_code = (
                    cleanup_raised.code
                    if type(cleanup_raised) is SystemExit
                    and (
                        type(cleanup_raised.code) is int
                        or cleanup_raised.code is None
                    )
                    else 1
                )
            elif isinstance(cleanup_raised, GeneratorExit):
                control_kind = "generator_exit"
                system_exit_code = None
                cleanup_failure = False
            else:
                control_kind = None
                system_exit_code = None
                cleanup_failure = True
            cleanup_raised = None
    except GeneratorExit:
        control_kind = "generator_exit"
        system_exit_code = None
        cleanup_failure = False
    finally:
        try: calendar_busy_source = None
        except BaseException:
            calendar_busy_source = None
            raise
    if cleanup_failure:
        _raise_calendar_cleanup_failure()
    _raise_calendar_control(
        control_kind,
        system_exit_code=system_exit_code,
    )


def _find_slots_evaluated(
    instructor_id: str,
    requested_date: date,
    client_address: str,
    is_instructor_facility: bool,
    duration_mins: int,
    buffer_policy: Optional[BufferPolicy] = None,
    public: bool = False,
    *,
    calendar_busy_source: Optional[CalendarBusySource] = None,
    requested_start: datetime | None = None,
    target_location: LocationInfo | None = None,
) -> list[TimeSlotDTO]:
    try: calendar_failure = False; return tools.find_available_swim_slots(
            calendar_busy_source=(
                calendar_busy_source,
                (calendar_busy_source := None),
            )[0],
            instructor_id=instructor_id,
            date_str=requested_date.isoformat(),
            client_address=client_address,
            is_instructor_facility=is_instructor_facility,
            duration_mins=duration_mins,
            buffer_policy=buffer_policy,
            requested_start=requested_start,
            target_location=target_location,
        )
    except ExternalCalendarUnavailable:
        try: calendar_failure = True
        except BaseException as interrupted:
            interrupted.__cause__ = None
            interrupted.__context__ = None
            interrupted.__suppress_context__ = True
            raise
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
    except ProviderSetupIncomplete as exc:
        raise ProviderSetupRequired(
            list(exc.missing_fields),
            public=public,
        ) from exc
    except ValueError as exc:
        message = str(exc)
        if "not found" in message.lower():
            raise ResourceNotFound("The instructor could not be found.") from exc
        raise InvalidSchedulingRequest("The scheduling request could not be evaluated.") from exc
    finally:
        try: calendar_busy_source = None
        except (
            asyncio.CancelledError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            calendar_busy_source = None
            raise
        except BaseException:
            calendar_busy_source = None
            _raise_calendar_cleanup_failure()
    if calendar_failure:
        _raise_calendar_availability_unavailable()
    raise RuntimeError("Calendar availability error mapping is invalid.")


def _raise_calendar_control(
    kind: str,
    *,
    system_exit_code: int | None,
) -> NoReturn:
    if kind == "cancelled":
        error: BaseException = asyncio.CancelledError()
    elif kind == "keyboard_interrupt":
        error = KeyboardInterrupt()
    elif kind == "system_exit":
        error = SystemExit(system_exit_code)
    else:
        error = GeneratorExit()
    error.__cause__ = None
    error.__context__ = None
    error.__suppress_context__ = True
    error.__traceback__ = None
    try:
        raise error.with_traceback(None) from None
    except BaseException as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_calendar_cleanup_failure() -> NoReturn:
    error = RuntimeError("Calendar availability control cleanup failed.")
    error.__cause__ = None
    error.__context__ = None
    error.__suppress_context__ = True
    error.__traceback__ = None
    try:
        raise error.with_traceback(None) from None
    except RuntimeError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_calendar_availability_unavailable() -> NoReturn:
    error = CalendarAvailabilityUnavailable()
    error.__cause__ = None
    error.__context__ = None
    error.__suppress_context__ = True
    error.__traceback__ = None
    try:
        raise error.with_traceback(None) from None
    except CalendarAvailabilityUnavailable as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _require_available_start(
    instructor_id: str,
    requested_date: date,
    client_address: str,
    is_instructor_facility: bool,
    duration_mins: int,
    requested_start: datetime,
    buffer_policy: Optional[BufferPolicy] = None,
    public: bool = False,
    *,
    requested_end: datetime | None = None,
    calendar_busy_source: CalendarBusySource | None = None,
    target_location: LocationInfo | None = None,
) -> TimeSlotDTO:
    try:
        slots = _find_slots(
            instructor_id=instructor_id,
            requested_date=requested_date,
            client_address=client_address,
            is_instructor_facility=is_instructor_facility,
            duration_mins=duration_mins,
            buffer_policy=buffer_policy,
            public=public,
            calendar_busy_source=calendar_busy_source,
            requested_start=requested_start if target_location is not None else None,
            target_location=target_location,
        )
    finally:
        calendar_busy_source = None
    requested_storage = timezones.to_utc_storage(requested_start)
    end_storage = timezones.to_utc_storage(requested_end) if requested_end is not None else None
    for slot in slots:
        if timezones.to_utc_storage(slot.start_time) == requested_storage and (
            end_storage is None or timezones.to_utc_storage(slot.end_time) == end_storage
        ):
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
    try:
        configuration = load_provider_configuration(instructor_id)
    except ProviderNotFound:
        raise ResourceNotFound("The instructor could not be found.")
    if is_instructor_facility:
        if configuration.service_location is None:
            raise ProviderSetupRequired(
                list(configuration.setup_missing_fields),
            )
        return LocationResponse(
            address=configuration.service_location.address,
            lat=configuration.service_location.lat,
            lng=configuration.service_location.lng,
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


def _public_slot_response(slot: TimeSlotDTO) -> PublicTimeSlotResponse:
    return PublicTimeSlotResponse(
        start_iso=slot.start_time,
        end_iso=slot.end_time,
        display=_format_public_slot_display(slot.start_time, slot.end_time),
    )


def _format_public_slot_display(
    start_time: datetime,
    end_time: datetime,
    *,
    timezone_label: Optional[str] = None,
) -> str:
    timezone_suffix = f" {timezone_label}" if timezone_label else ""
    return (
        f"{start_time.strftime('%I:%M %p')} - {end_time.strftime('%I:%M %p')}"
        f"{timezone_suffix}"
    )


def _client_id_from_name(client_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", client_name.strip().lower()).strip("_")
    return f"client_{slug or 'guest'}"


def _duration_delta(duration_mins: int) -> timedelta:
    return timedelta(minutes=duration_mins)


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _stored_time_for_response(
    value: datetime,
    timezone_name: str,
) -> tuple[datetime, bool]:
    try:
        zone = _authoritative_zone(timezone_name)
        return timezones.storage_to_aware_utc(value).astimezone(zone), True
    except timezones.InvalidTimezoneError:
        return timezones.storage_to_aware_utc(value), False


def _authoritative_zone(timezone_name: str):
    if not timezone_name or not timezone_name.strip():
        raise timezones.InvalidTimezoneError("Provider timezone is blank.")
    return timezones.get_zone(timezone_name)


def _canonical_request_time(value: datetime, timezone_name: str) -> datetime:
    if not _is_aware(value):
        _authoritative_zone(timezone_name)
        return timezones.validate_local_datetime(value, timezone_name)
    return value.astimezone(_authoritative_zone(timezone_name))
