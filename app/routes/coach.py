from datetime import date

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.responses import PlainTextResponse

from app.api_errors import ApiException
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
    CoachBookLessonRequest,
    CoachCommandCenterSnapshot,
    CoachContext as CoachContextResponse,
    CoachEmailContactUpdateRequest,
    CoachEmailDeliveryResponse,
    CoachLessonRequestAlternativesRequest,
    CoachLessonRequestActionResponse,
    CoachLessonRequestConfirmResponse,
    CoachLessonRequestConfirmRequest,
    CoachScheduleResponse,
    CoachSchedulerRules,
    InviteNotificationResponse,
)
from app.api_services import (
    approve_coach_lesson_request,
    book_coach_lesson,
    confirm_coach_lesson_request,
    decline_coach_lesson_request,
    get_coach_command_center_snapshot,
    get_coach_calendar_ics,
    get_coach_calendar_links,
    get_coach_schedule,
    get_coach_scheduler_settings,
    request_alternatives_for_coach_lesson_request,
    search_coach_availability,
    update_coach_scheduler_settings,
)
from app.dependencies import CoachContext, get_current_coach


router = APIRouter(prefix="/api/coach", tags=["coach"])


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


@router.get("/schedule", response_model=CoachScheduleResponse)
def read_schedule(
    date_: date = Query(alias="date"),
    coach: CoachContext = Depends(get_current_coach),
) -> CoachScheduleResponse:
    return get_coach_schedule(coach.instructor_id, date_)


@router.get(
    "/command-center/snapshot",
    response_model=CoachCommandCenterSnapshot,
    response_model_exclude_none=True,
)
def read_command_center_snapshot(
    date_: date = Query(alias="date"),
    coach: CoachContext = Depends(get_current_coach),
) -> CoachCommandCenterSnapshot:
    return get_coach_command_center_snapshot(coach.instructor_id, date_)


@router.get("/settings", response_model=CoachSchedulerRules)
def read_scheduler_settings(
    coach: CoachContext = Depends(get_current_coach),
) -> CoachSchedulerRules:
    return get_coach_scheduler_settings(coach.instructor_id)


@router.put("/settings", response_model=CoachSchedulerRules)
def save_scheduler_settings(
    rules: CoachSchedulerRules,
    coach: CoachContext = Depends(get_current_coach),
) -> CoachSchedulerRules:
    return update_coach_scheduler_settings(coach.instructor_id, rules)


@router.post("/availability", response_model=AvailabilitySearchResponse)
def find_availability(
    request: AvailabilitySearchRequest,
    coach: CoachContext = Depends(get_current_coach),
) -> AvailabilitySearchResponse:
    return search_coach_availability(coach.instructor_id, request)


@router.post("/bookings", response_model=BookLessonResponse)
def create_booking(
    request: CoachBookLessonRequest,
    coach: CoachContext = Depends(get_current_coach),
) -> BookLessonResponse:
    return book_coach_lesson(coach.instructor_id, request)


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


@router.post("/lesson-requests/{request_token}/approve", response_model=CoachLessonRequestActionResponse)
def approve_lesson_request(
    request_token: str,
    coach: CoachContext = Depends(get_current_coach),
) -> CoachLessonRequestActionResponse:
    return approve_coach_lesson_request(coach.instructor_id, request_token)


@router.post("/lesson-requests/{request_token}/confirm", response_model=CoachLessonRequestConfirmResponse)
def confirm_lesson_request(
    request_token: str,
    request: CoachLessonRequestConfirmRequest = CoachLessonRequestConfirmRequest(),
    coach: CoachContext = Depends(get_current_coach),
) -> CoachLessonRequestConfirmResponse:
    return confirm_coach_lesson_request(coach.instructor_id, request_token, request)


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
