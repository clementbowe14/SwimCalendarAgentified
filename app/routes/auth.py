from fastapi import APIRouter, Depends, Request, Response

from app.api_errors import ApiException
from app.api_schemas import (
    CoachAuthResponse,
    CoachContext as CoachContextResponse,
    CoachLoginRequest,
    CoachLogoutResponse,
    CoachSessionResponse,
)
from app.auth import (
    SESSION_COOKIE_NAME,
    authenticate_coach,
    clear_session_cookie,
    revoke_presented_session,
    set_session_cookie,
)
from app.dependencies import CoachContext, get_current_coach, require_csrf_protection


router = APIRouter(
    prefix="/api/auth/coach",
    tags=["coach-auth"],
    dependencies=[Depends(require_csrf_protection)],
)


@router.post("/login", response_model=CoachAuthResponse, deprecated=True)
@router.post("/sign-in", response_model=CoachAuthResponse)
def login_coach(
    credentials: CoachLoginRequest,
    request: Request,
    response: Response,
) -> CoachAuthResponse:
    issued = authenticate_coach(
        credentials.email,
        credentials.password,
        presented_token=request.cookies.get(SESSION_COOKIE_NAME),
    )
    set_session_cookie(response, issued)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return CoachAuthResponse(
        authenticated=True,
        expires_at=issued.principal.expires_at,
        coach=CoachContextResponse(
            instructor_id=issued.principal.instructor_id,
            display_name=issued.principal.display_name,
        ),
    )


@router.get("/session", response_model=CoachSessionResponse)
def read_coach_session(
    response: Response,
    coach: CoachContext = Depends(get_current_coach),
) -> CoachSessionResponse:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return CoachSessionResponse(
        authenticated=True,
        expires_at=coach.expires_at,
        coach=CoachContextResponse(
            instructor_id=coach.instructor_id,
            display_name=coach.display_name,
        ),
    )


@router.post("/logout", response_model=CoachLogoutResponse, deprecated=True)
@router.post("/sign-out", response_model=CoachLogoutResponse)
async def logout_coach(request: Request, response: Response) -> CoachLogoutResponse:
    if await request.body():
        raise ApiException(
            "VALIDATION_ERROR",
            "This endpoint does not accept a request body.",
            status_code=422,
        )
    revoke_presented_session(request.cookies.get(SESSION_COOKIE_NAME))
    clear_session_cookie(response)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return CoachLogoutResponse(success=True, message="Signed out.")
