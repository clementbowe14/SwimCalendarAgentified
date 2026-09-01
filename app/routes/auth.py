from fastapi import APIRouter, Depends

from app.api_schemas import (
    CoachAuthResponse,
    CoachContext as CoachContextResponse,
    CoachLoginRequest,
    CoachLogoutResponse,
    CoachSessionResponse,
)
from app.auth import authenticate_coach, issue_access_token
from app.dependencies import CoachContext, get_current_coach


router = APIRouter(prefix="/api/auth/coach", tags=["coach-auth"])


@router.post("/login", response_model=CoachAuthResponse)
@router.post("/sign-in", response_model=CoachAuthResponse)
def login_coach(request: CoachLoginRequest) -> CoachAuthResponse:
    principal = authenticate_coach(request.email, request.password)
    token = issue_access_token(principal)
    return CoachAuthResponse(
        access_token=token,
        expires_at=principal.expires_at,
        coach=CoachContextResponse(
            instructor_id=principal.instructor_id,
            display_name=principal.display_name,
        ),
    )


@router.get("/session", response_model=CoachSessionResponse)
def read_coach_session(coach: CoachContext = Depends(get_current_coach)) -> CoachSessionResponse:
    return CoachSessionResponse(
        authenticated=True,
        coach=CoachContextResponse(
            instructor_id=coach.instructor_id,
            display_name=coach.display_name,
        ),
    )


@router.post("/logout", response_model=CoachLogoutResponse)
@router.post("/sign-out", response_model=CoachLogoutResponse)
def logout_coach() -> CoachLogoutResponse:
    return CoachLogoutResponse(success=True, message="Coach session cleared.")
