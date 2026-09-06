from fastapi import APIRouter, Response
from fastapi.responses import PlainTextResponse

from app.api_schemas import (
    ClientInviteAvailabilityRequest,
    ClientInviteAvailabilityResponse,
    ClientInviteCalendarLinks,
    ClientInviteEnvelopeResponse,
    ClientInviteIntake,
    ClientInviteAlternativeSelectionRequest,
    ClientInviteLessonRequestCreate,
    ClientInviteLessonRequestResponse,
    PublicAvailabilitySearchRequest,
    PublicAvailabilitySearchResponse,
    PublicInviteResponse,
    PublicLessonRequestCreate,
    PublicLessonRequestResponse,
    PublicRequestStatusResponse,
)
from app.api_services import (
    create_public_lesson_request,
    get_public_request_status,
    public_invite_response,
    search_public_availability,
)
from app.client_invites import (
    get_client_calendar_ics,
    get_client_calendar_links,
    get_client_invite_envelope,
    get_client_lesson_request_status,
    request_client_invite_lesson_slot,
    search_client_invite_slots,
    select_client_lesson_request_alternative,
    submit_client_invite_intake,
)
from app.public_tokens import resolve_invite


router = APIRouter(prefix="/api/public", tags=["public"])


@router.get("/invites/{token}", response_model=PublicInviteResponse)
def read_invite(token: str) -> PublicInviteResponse:
    return public_invite_response(resolve_invite(token))


@router.post("/invites/{token}/availability", response_model=PublicAvailabilitySearchResponse)
def find_invite_availability(
    token: str,
    request: PublicAvailabilitySearchRequest,
) -> PublicAvailabilitySearchResponse:
    return search_public_availability(resolve_invite(token), request)


@router.post("/invites/{token}/requests", response_model=PublicLessonRequestResponse)
def create_invite_lesson_request(
    token: str,
    request: PublicLessonRequestCreate,
) -> PublicLessonRequestResponse:
    return create_public_lesson_request(resolve_invite(token), request)


@router.get("/requests/{request_token}", response_model=PublicRequestStatusResponse)
def read_request_status(request_token: str) -> PublicRequestStatusResponse:
    return get_public_request_status(request_token)


@router.get("/client-invites/{token}", response_model=ClientInviteEnvelopeResponse)
def read_client_invite(token: str) -> ClientInviteEnvelopeResponse:
    return get_client_invite_envelope(token)


@router.post("/client-invites/{token}/intake", response_model=ClientInviteEnvelopeResponse)
def submit_client_intake(token: str, intake: ClientInviteIntake) -> ClientInviteEnvelopeResponse:
    return submit_client_invite_intake(token, intake)


@router.post("/client-invites/{token}/slots", response_model=ClientInviteAvailabilityResponse)
def find_client_invite_slots(
    token: str,
    request: ClientInviteAvailabilityRequest,
) -> ClientInviteAvailabilityResponse:
    return search_client_invite_slots(token, request)


@router.post("/client-invites/{token}/lesson-requests", response_model=ClientInviteLessonRequestResponse)
def create_client_invite_lesson_request(
    token: str,
    request: ClientInviteLessonRequestCreate,
) -> ClientInviteLessonRequestResponse:
    return request_client_invite_lesson_slot(token, request)


@router.get("/lesson-requests/{request_token}", response_model=ClientInviteLessonRequestResponse)
def read_client_lesson_request_status(request_token: str) -> ClientInviteLessonRequestResponse:
    return get_client_lesson_request_status(request_token)


@router.post("/lesson-requests/{request_token}/select-alternative", response_model=ClientInviteLessonRequestResponse)
def select_client_lesson_alternative(
    request_token: str,
    request: ClientInviteAlternativeSelectionRequest,
) -> ClientInviteLessonRequestResponse:
    return select_client_lesson_request_alternative(request_token, request)


@router.get("/lesson-requests/{request_token}/calendar-links", response_model=ClientInviteCalendarLinks)
def read_client_calendar_links(request_token: str) -> ClientInviteCalendarLinks:
    return get_client_calendar_links(request_token)


@router.get("/calendar/{request_token}.ics", response_class=PlainTextResponse)
def read_client_calendar_ics(request_token: str) -> Response:
    return PlainTextResponse(
        get_client_calendar_ics(request_token),
        media_type="text/calendar",
    )
