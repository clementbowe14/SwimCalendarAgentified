from datetime import date
from functools import partial

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.responses import PlainTextResponse

from app.api_errors import (
    ApiException,
    SchedulingPolicyEndpointMoved,
)
from app.calendar_connection_access import (
    EXPECTED_CALENDAR_INSTRUCTOR_HEADER,
    get_google_calendar_coach,
    require_expected_instructor,
)
from app.coach_agenda import get_coach_agenda
from app.notifications import (
    correct_coach_email_delivery_contact,
    enqueue_invite_for_coach,
    list_coach_email_deliveries,
    resend_coach_email_delivery,
)

from app.api_schemas import (
    AvailabilitySearchRequest,
    AvailabilitySearchResponse,
    BookLessonResponse,
    ClientInviteCalendarLinks,
    CoachBlockerMutation,
    CoachBlockerResponse,
    CoachBlockerUpdate,
    CoachBookLessonRequest,
    CoachCalendarConnectionResponse,
    CoachClientListResponse,
    CoachClientProfileResponse,
    CoachCommandCenterSnapshot,
    CoachContext as CoachContextResponse,
    CoachDailyAgendaResponse,
    CoachEmailContactUpdateRequest,
    CoachEmailDeliveryResponse,
    CoachLessonRequestAlternativesRequest,
    CoachLessonRequestActionResponse,
    CoachLessonRequestConfirmResponse,
    CoachLessonRequestConfirmRequest,
    CoachProviderSettingsResponse,
    CoachProviderSettingsUpdate,
    CoachScheduleResponse,
    CoachSchedulerRules,
    CoachSchedulingPolicyResponse,
    CoachSchedulingPolicyUpdate,
    ErrorResponse,
    InviteNotificationResponse,
)
from app.api_services import (
    approve_coach_lesson_request,
    book_coach_lesson,
    create_coach_blocker,
    confirm_coach_lesson_request,
    decline_coach_lesson_request,
    delete_coach_blocker,
    disconnect_coach_google_calendar,
    get_coach_command_center_snapshot,
    get_coach_calendar_ics,
    get_coach_calendar_links,
    get_coach_google_calendar_connection_status,
    get_coach_blockers,
    get_coach_client,
    get_coach_provider_settings,
    get_coach_schedule,
    get_coach_scheduler_settings,
    get_coach_scheduling_policy,
    list_coach_clients,
    request_alternatives_for_coach_lesson_request,
    search_coach_availability,
    update_coach_provider_settings,
    update_coach_blocker,
    update_coach_scheduling_policy,
)
from app.dependencies import (
    CoachContext,
    get_current_coach,
    google_calendar_availability_source as _google_calendar_availability_source,
    require_csrf_protection,
)


router = APIRouter(
    prefix="/api/coach",
    tags=["coach"],
    dependencies=[Depends(get_current_coach), Depends(require_csrf_protection)],
)

BLOCKER_ERROR_RESPONSES = {
    404: {
        "model": ErrorResponse,
        "description": "The provider or schedule event could not be found.",
    },
    409: {
        "model": ErrorResponse,
        "description": "The schedule, revision, setup, or idempotency key conflicts.",
    },
    422: {
        "model": ErrorResponse,
        "description": "The blocker input, local time, or location is invalid.",
    },
}


CLIENT_READ_ERROR_RESPONSES = {
    404: {
        "model": ErrorResponse,
        "description": "The provider or client could not be found.",
    },
}


class CalendarTextResponse(PlainTextResponse):
    media_type = "text/calendar"


def _private_response(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


@router.post("/invites/{invite_token}/notifications", response_model=InviteNotificationResponse, status_code=202)
async def send_invite_notification(
    invite_token: str,
    request: Request,
    response: Response,
    idempotency_key: str = Header(min_length=1, max_length=200),
    coach: CoachContext = Depends(get_current_coach),
) -> InviteNotificationResponse:
    if await request.body():
        raise ApiException("VALIDATION_ERROR", "This endpoint does not accept a request body.", 422)
    _private_response(response)
    result = await run_in_threadpool(
        enqueue_invite_for_coach,
        coach.instructor_id,
        invite_token,
        idempotency_key,
    )
    return InviteNotificationResponse(
        notifications=result.notifications,
        email_delivery=result.email_delivery,
    )


@router.get("/email-deliveries", response_model=list[CoachEmailDeliveryResponse])
def read_email_deliveries(
    response: Response,
    coach: CoachContext = Depends(get_current_coach),
) -> list[CoachEmailDeliveryResponse]:
    _private_response(response)
    return list_coach_email_deliveries(coach.instructor_id)


@router.post(
    "/email-deliveries/{delivery_id}/resend",
    response_model=CoachEmailDeliveryResponse,
)
async def resend_email_delivery(
    delivery_id: str,
    request: Request,
    response: Response,
    coach: CoachContext = Depends(get_current_coach),
) -> CoachEmailDeliveryResponse:
    if await request.body():
        raise ApiException("VALIDATION_ERROR", "This endpoint does not accept a request body.", 422)
    _private_response(response)
    return await run_in_threadpool(
        resend_coach_email_delivery,
        coach.instructor_id,
        delivery_id,
    )


@router.put(
    "/email-deliveries/{delivery_id}/contact",
    response_model=CoachEmailDeliveryResponse,
)
def update_email_delivery_contact(
    delivery_id: str,
    request: CoachEmailContactUpdateRequest,
    response: Response,
    coach: CoachContext = Depends(get_current_coach),
) -> CoachEmailDeliveryResponse:
    _private_response(response)
    return correct_coach_email_delivery_contact(
        coach.instructor_id,
        delivery_id,
        request.email,
    )


@router.get("/me", response_model=CoachContextResponse)
def read_current_coach(coach: CoachContext = Depends(get_current_coach)) -> CoachContextResponse:
    return CoachContextResponse(instructor_id=coach.instructor_id, display_name=coach.display_name)


@router.get("/provider", response_model=CoachProviderSettingsResponse)
def read_provider_settings(
    response: Response,
    coach: CoachContext = Depends(get_current_coach),
) -> CoachProviderSettingsResponse:
    _private_response(response)
    return get_coach_provider_settings(coach.instructor_id)


@router.get(
    "/calendar-connections/google",
    response_model=CoachCalendarConnectionResponse,
    responses={409: {"model": ErrorResponse, "description": "The signed-in provider changed."}},
)
def read_google_calendar_connection_status(
    response: Response,
    coach: CoachContext = Depends(get_google_calendar_coach),
) -> CoachCalendarConnectionResponse:
    _private_response(response)
    return get_coach_google_calendar_connection_status(coach.account_id)


@router.delete(
    "/calendar-connections/google",
    response_model=CoachCalendarConnectionResponse,
    responses={409: {"model": ErrorResponse, "description": "The signed-in provider changed."}},
)
def delete_google_calendar_connection(
    response: Response,
    coach: CoachContext = Depends(get_google_calendar_coach),
) -> CoachCalendarConnectionResponse:
    _private_response(response)
    return disconnect_coach_google_calendar(coach.account_id)


@router.put("/provider", response_model=CoachProviderSettingsResponse)
def save_provider_settings(
    settings: CoachProviderSettingsUpdate,
    response: Response,
    coach: CoachContext = Depends(get_current_coach),
) -> CoachProviderSettingsResponse:
    _private_response(response)
    return update_coach_provider_settings(coach.instructor_id, settings)


@router.get(
    "/clients",
    response_model=CoachClientListResponse,
    responses=CLIENT_READ_ERROR_RESPONSES,
)
def read_clients(
    response: Response,
    coach: CoachContext = Depends(get_current_coach),
) -> CoachClientListResponse:
    _private_response(response)
    return list_coach_clients(coach.instructor_id)


@router.get(
    "/clients/{client_id}",
    response_model=CoachClientProfileResponse,
    responses=CLIENT_READ_ERROR_RESPONSES,
)
def read_client(
    client_id: str,
    response: Response,
    coach: CoachContext = Depends(get_current_coach),
) -> CoachClientProfileResponse:
    _private_response(response)
    return get_coach_client(coach.instructor_id, client_id)


@router.get("/schedule", response_model=CoachScheduleResponse)
def read_schedule(
    date_: date = Query(alias="date"),
    coach: CoachContext = Depends(get_current_coach),
) -> CoachScheduleResponse:
    return get_coach_schedule(coach.instructor_id, date_)


@router.get("/agenda", response_model=CoachDailyAgendaResponse)
def read_agenda(
    http_request: Request,
    response: Response,
    expected_instructor: str = Header(alias=EXPECTED_CALENDAR_INSTRUCTOR_HEADER),
    date_: date | None = Query(default=None, alias="date"),
    coach: CoachContext = Depends(get_current_coach),
) -> CoachDailyAgendaResponse:
    _private_response(response)
    try:
        require_expected_instructor(coach, expected_instructor)
        return get_coach_agenda(
            coach.instructor_id,
            date_,
            build_calendar_source=partial(
                _google_calendar_availability_source, http_request.app.state, coach.instructor_id
            ),
        )
    finally:
        del http_request, coach, expected_instructor


@router.get(
    "/blockers",
    response_model=list[CoachBlockerResponse],
    responses=BLOCKER_ERROR_RESPONSES,
)
def read_blockers(
    date_: date = Query(alias="date"),
    coach: CoachContext = Depends(get_current_coach),
) -> list[CoachBlockerResponse]:
    return get_coach_blockers(coach.instructor_id, date_)


@router.post(
    "/blockers",
    response_model=CoachBlockerResponse,
    status_code=201,
    responses=BLOCKER_ERROR_RESPONSES,
)
def create_blocker(
    blocker: CoachBlockerMutation,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=160),
    coach: CoachContext = Depends(get_current_coach),
) -> CoachBlockerResponse:
    return create_coach_blocker(coach.instructor_id, blocker, idempotency_key)


@router.put(
    "/blockers/{blocker_id}",
    response_model=CoachBlockerResponse,
    responses=BLOCKER_ERROR_RESPONSES,
)
def update_blocker(
    blocker_id: str,
    blocker: CoachBlockerUpdate,
    coach: CoachContext = Depends(get_current_coach),
) -> CoachBlockerResponse:
    return update_coach_blocker(coach.instructor_id, blocker_id, blocker)


@router.delete(
    "/blockers/{blocker_id}",
    status_code=204,
    responses=BLOCKER_ERROR_RESPONSES,
)
def remove_blocker(
    blocker_id: str,
    expected_revision: int = Query(ge=1),
    coach: CoachContext = Depends(get_current_coach),
) -> Response:
    delete_coach_blocker(coach.instructor_id, blocker_id, expected_revision)
    return Response(status_code=204)


@router.get(
    "/command-center/snapshot",
    response_model=CoachCommandCenterSnapshot,
)
def read_command_center_snapshot(
    date_: date = Query(alias="date"),
    coach: CoachContext = Depends(get_current_coach),
) -> CoachCommandCenterSnapshot:
    return get_coach_command_center_snapshot(coach.instructor_id, date_)


@router.get("/settings", response_model=CoachSchedulerRules, deprecated=True)
def read_scheduler_settings(
    coach: CoachContext = Depends(get_current_coach),
) -> CoachSchedulerRules:
    return get_coach_scheduler_settings(coach.instructor_id)


@router.put(
    "/settings",
    deprecated=True,
    status_code=410,
    response_model=ErrorResponse,
    response_description="Scheduling policy endpoint moved.",
)
def reject_legacy_scheduler_settings_write(
    coach: CoachContext = Depends(get_current_coach),
) -> None:
    del coach
    raise SchedulingPolicyEndpointMoved()


@router.get("/scheduling-policy", response_model=CoachSchedulingPolicyResponse)
def read_scheduling_policy(
    coach: CoachContext = Depends(get_current_coach),
) -> CoachSchedulingPolicyResponse:
    return get_coach_scheduling_policy(coach.instructor_id)


@router.put(
    "/scheduling-policy",
    response_model=CoachSchedulingPolicyResponse,
    responses={
        409: {
            "model": ErrorResponse,
            "description": "The expected policy revision is stale.",
        },
        422: {
            "model": ErrorResponse,
            "description": "The policy update is invalid.",
        },
    },
)
def save_scheduling_policy(
    policy: CoachSchedulingPolicyUpdate,
    coach: CoachContext = Depends(get_current_coach),
) -> CoachSchedulingPolicyResponse:
    return update_coach_scheduling_policy(coach.instructor_id, policy)


@router.post("/availability", response_model=AvailabilitySearchResponse)
def find_availability(
    request: AvailabilitySearchRequest,
    http_request: Request,
    coach: CoachContext = Depends(get_current_coach),
) -> AvailabilitySearchResponse:
    try:
        return search_coach_availability(
            coach.instructor_id,
            request,
            calendar_busy_source=_google_calendar_availability_source(
                http_request.app.state, coach.instructor_id
            ),
        )
    finally:
        # Keep request data and the authenticated principal out of route tracebacks.
        del request, http_request, coach


@router.post("/bookings", response_model=BookLessonResponse)
def create_booking(
    request: CoachBookLessonRequest,
    http_request: Request,
    coach: CoachContext = Depends(get_current_coach),
) -> BookLessonResponse:
    try:
        return book_coach_lesson(
            coach.instructor_id,
            request,
            build_calendar_source=partial(
                _google_calendar_availability_source, http_request.app.state, coach.instructor_id
            ),
        )
    finally:
        del request, http_request, coach


@router.get(
    "/appointments/{appointment_id}/calendar-links",
    response_model=ClientInviteCalendarLinks,
)
def read_appointment_calendar_links(
    appointment_id: str,
    response: Response,
    coach: CoachContext = Depends(get_current_coach),
) -> ClientInviteCalendarLinks:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return get_coach_calendar_links(coach.instructor_id, appointment_id)


@router.get(
    "/appointments/{appointment_id}/calendar.ics",
    response_class=CalendarTextResponse,
)
def download_appointment_calendar(
    appointment_id: str,
    coach: CoachContext = Depends(get_current_coach),
) -> Response:
    return CalendarTextResponse(
        get_coach_calendar_ics(coach.instructor_id, appointment_id),
        headers={
            "Content-Disposition": 'attachment; filename="appointment.ics"',
            "Cache-Control": "private, no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.post(
    "/lesson-requests/{request_token}/approve",
    response_model=CoachLessonRequestActionResponse,
    deprecated=True,
)
async def approve_lesson_request(
    request_token: str,
    request: Request,
    coach: CoachContext = Depends(get_current_coach),
) -> CoachLessonRequestActionResponse:
    if await request.body():
        raise ApiException("VALIDATION_ERROR", "This endpoint does not accept a request body.", 422)
    try:
        return await run_in_threadpool(
            approve_coach_lesson_request,
            coach.instructor_id,
            request_token,
            build_calendar_source=partial(
                _google_calendar_availability_source, request.app.state, coach.instructor_id
            ),
        )
    finally:
        del request, coach


@router.post("/lesson-requests/{request_token}/confirm", response_model=CoachLessonRequestConfirmResponse)
def confirm_lesson_request(
    request_token: str,
    http_request: Request,
    request: CoachLessonRequestConfirmRequest = CoachLessonRequestConfirmRequest(),
    coach: CoachContext = Depends(get_current_coach),
) -> CoachLessonRequestConfirmResponse:
    try:
        return confirm_coach_lesson_request(
            coach.instructor_id,
            request_token,
            request,
            build_calendar_source=partial(
                _google_calendar_availability_source, http_request.app.state, coach.instructor_id
            ),
        )
    finally:
        del request, http_request, coach


@router.post("/lesson-requests/{request_token}/alternatives", response_model=CoachLessonRequestActionResponse)
def request_lesson_alternatives(
    request_token: str,
    request: CoachLessonRequestAlternativesRequest = CoachLessonRequestAlternativesRequest(),
    coach: CoachContext = Depends(get_current_coach),
) -> CoachLessonRequestActionResponse:
    return request_alternatives_for_coach_lesson_request(coach.instructor_id, request_token, request)


@router.post("/lesson-requests/{request_token}/decline", response_model=CoachLessonRequestActionResponse)
def decline_lesson_request(
    request_token: str,
    coach: CoachContext = Depends(get_current_coach),
) -> CoachLessonRequestActionResponse:
    return decline_coach_lesson_request(coach.instructor_id, request_token)
