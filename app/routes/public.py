from fastapi import APIRouter

from app.api_schemas import (
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
