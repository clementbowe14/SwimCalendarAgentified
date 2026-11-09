from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from fastapi import Request, Response, Security
from fastapi.security import APIKeyCookie

from app.api_errors import AuthenticationRequired, CsrfValidationFailed
from app.auth import (
    CSRF_HEADER_NAME,
    CSRF_HEADER_VALUE,
    SESSION_COOKIE_NAME,
    validate_coach_session,
)
from app.config import application_settings_from_env


session_cookie_scheme = APIKeyCookie(
    name=SESSION_COOKIE_NAME,
    scheme_name="CoachSessionCookie",
    description="Opaque revocable provider session cookie.",
    auto_error=False,
)
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True)
class CoachContext:
    account_id: str
    session_id: str
    instructor_id: str
    display_name: str
    expires_at: datetime


def get_current_coach(
    request: Request,
    session_token: Optional[str] = Security(session_cookie_scheme),
) -> CoachContext:
    principal = getattr(request.state, "coach_principal", None)
    if principal is None:
        if session_token is None:
            raise AuthenticationRequired()
        principal = validate_coach_session(session_token)
    return CoachContext(
        account_id=principal.account_id,
        session_id=principal.session_id,
        instructor_id=principal.instructor_id,
        display_name=principal.display_name,
        expires_at=principal.expires_at,
    )


def require_csrf_protection(request: Request) -> None:
    if request.method.upper() not in UNSAFE_METHODS:
        return
    if request.headers.get(CSRF_HEADER_NAME) != CSRF_HEADER_VALUE:
        raise CsrfValidationFailed()

    origin = request.headers.get("Origin")
    if origin is None:
        return
    settings = application_settings_from_env()
    own_origin = f"{request.url.scheme}://{request.url.netloc}"
    trusted_origins = set(settings.cors_origins)
    trusted_origins.add(own_origin)
    if origin == "null" or origin not in trusted_origins:
        raise CsrfValidationFailed()
