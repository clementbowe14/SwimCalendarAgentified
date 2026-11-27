from typing import Any, Optional

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.google_calendar_oauth import (
    GOOGLE_CALENDAR_AUTHORIZE_PATH,
    GOOGLE_CALENDAR_CALLBACK_PATH,
    google_calendar_oauth_route_path,
)


class ApiException(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: Optional[dict[str, Any]] = None,
    ):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


class ResourceNotFound(ApiException):
    def __init__(self, message: str = "The requested resource could not be found."):
        super().__init__("RESOURCE_NOT_FOUND", message, status_code=404)


class AuthenticationRequired(ApiException):
    def __init__(self):
        super().__init__(
            "AUTHENTICATION_REQUIRED",
            "Coach authentication is required.",
            status_code=401,
        )


class InvalidCredentials(ApiException):
    def __init__(self):
        super().__init__(
            "INVALID_CREDENTIALS",
            "The coach credentials are invalid.",
            status_code=401,
        )


class InvalidAuthToken(ApiException):
    def __init__(self):
        super().__init__(
            "INVALID_AUTH_TOKEN",
            "The coach session is invalid or expired.",
            status_code=401,
        )


class AuthorizationForbidden(ApiException):
    def __init__(self):
        super().__init__(
            "AUTHORIZATION_FORBIDDEN",
            "The authenticated coach is not allowed to access this resource.",
            status_code=403,
        )


class CsrfValidationFailed(ApiException):
    def __init__(self):
        super().__init__(
            "CSRF_VALIDATION_FAILED",
            "The request origin or CSRF protection header is invalid.",
            status_code=403,
        )


class InviteNotFound(ApiException):
    def __init__(self):
        super().__init__("INVITE_NOT_FOUND", "The invite could not be found.", status_code=404)


class InviteExpired(ApiException):
    def __init__(self):
        super().__init__("INVITE_EXPIRED", "The invite has expired.", status_code=410)


class LessonRequestNotFound(ApiException):
    def __init__(self):
        super().__init__(
            "LESSON_REQUEST_NOT_FOUND",
            "The lesson request could not be found.",
            status_code=404,
        )


class SlotOfferNotFound(ApiException):
    def __init__(self):
        super().__init__(
            "SLOT_OFFER_NOT_FOUND",
            "The selected lesson time is no longer available.",
            status_code=404,
        )


class CalendarLinksUnavailable(ApiException):
    def __init__(self):
        super().__init__(
            "CALENDAR_LINKS_UNAVAILABLE",
            "Calendar links are unavailable for this appointment.",
            status_code=409,
        )


class CalendarAvailabilityUnavailable(ApiException):
    def __init__(self):
        super().__init__(
            "CALENDAR_AVAILABILITY_UNAVAILABLE",
            "Calendar availability could not be verified.",
            status_code=503,
        )


class CalendarOAuthUnavailable(ApiException):
    def __init__(self):
        super().__init__(
            "CALENDAR_OAUTH_UNAVAILABLE",
            "Google Calendar authorization is unavailable.",
            status_code=503,
        )


class SchedulingConflict(ApiException):
    def __init__(
        self,
        message: str = "The requested time is unavailable.",
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__("SCHEDULING_CONFLICT", message, status_code=409, details=details)


class SchedulingPolicyStale(ApiException):
    def __init__(self):
        super().__init__(
            "SCHEDULING_POLICY_STALE",
            "The scheduling policy changed. Reload it before saving again.",
            status_code=409,
        )


class BlockerStale(ApiException):
    def __init__(self):
        super().__init__(
            "BLOCKER_STALE",
            "This schedule event changed. Reload it before saving again.",
            status_code=409,
        )


class SchedulingPolicyEndpointMoved(ApiException):
    def __init__(self):
        super().__init__(
            "SCHEDULING_POLICY_ENDPOINT_MOVED",
            "Scheduling policy updates must use /api/coach/scheduling-policy.",
            status_code=410,
        )


class SchedulingPolicyValidationError(ApiException):
    def __init__(self, message: str):
        super().__init__(
            "VALIDATION_ERROR",
            message,
            status_code=422,
            details={"field": "distance_buffer_tiers"},
        )


class ProviderSetupRequired(ApiException):
    def __init__(
        self,
        missing_fields: Optional[list[str]] = None,
        *,
        public: bool = False,
    ):
        super().__init__(
            "PROVIDER_SETUP_REQUIRED",
            (
                "Scheduling is temporarily unavailable."
                if public
                else "Complete provider settings before scheduling."
            ),
            status_code=409,
            details=(
                None
                if public or not missing_fields
                else {"missing_fields": missing_fields}
            ),
        )


class IdempotencyConflict(ApiException):
    def __init__(self):
        super().__init__(
            "IDEMPOTENCY_KEY_CONFLICT",
            "The idempotency key was already used for a different mutation.",
            status_code=409,
        )


class LessonRequestExpired(ApiException):
    def __init__(self):
        super().__init__(
            "LESSON_REQUEST_EXPIRED",
            "The lesson request is no longer actionable.",
            status_code=409,
        )


class LessonRequestConfirmConflict(ApiException):
    def __init__(self):
        super().__init__(
            "LESSON_REQUEST_CONFIRM_CONFLICT",
            "The lesson request was already confirmed with a different slot.",
            status_code=409,
        )


class AlternativeSlotContextMismatch(ApiException):
    def __init__(self):
        super().__init__(
            "ALTERNATIVE_SLOT_CONTEXT_MISMATCH",
            "One or more alternative slots do not match this lesson request.",
            status_code=409,
        )


class InvalidSchedulingRequest(ApiException):
    def __init__(
        self,
        message: str = "The scheduling request is invalid.",
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__("INVALID_SCHEDULING_REQUEST", message, status_code=400, details=details)


class GeocodingFailed(ApiException):
    def __init__(self, details: Optional[dict[str, Any]] = None):
        super().__init__(
            "GEOCODING_FAILED",
            "The address could not be geocoded.",
            status_code=400,
            details=details,
        )


class RoutingEstimateFailed(ApiException):
    def __init__(self):
        super().__init__(
            "ROUTING_ESTIMATE_FAILED",
            "Travel time could not be estimated.",
            status_code=503,
        )


class RoutingTimeout(ApiException):
    def __init__(self):
        super().__init__(
            "ROUTING_TIMEOUT",
            "Travel time estimation timed out.",
            status_code=503,
        )


class RoutingProviderUnavailable(ApiException):
    def __init__(self):
        super().__init__(
            "ROUTING_PROVIDER_UNAVAILABLE",
            "The routing provider is unavailable.",
            status_code=503,
        )


class RoutingConfigurationError(ApiException):
    def __init__(self):
        super().__init__(
            "ROUTING_CONFIGURATION_ERROR",
            "The routing provider is not configured.",
            status_code=503,
        )


def error_payload(code: str, message: str, details: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {"error": error}


async def api_exception_handler(request: Request, exc: ApiException) -> JSONResponse:
    response = JSONResponse(
        status_code=exc.status_code,
        content=error_payload(exc.code, exc.message, exc.details),
    )
    if request.url.path.startswith(("/api/auth/coach", "/api/coach")):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    if isinstance(exc, InvalidAuthToken):
        from app.auth import clear_session_cookie

        clear_session_cookie(response)
    return response


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    redact_inputs = request.url.path.startswith("/api/auth/coach")
    details = {
        "fields": [
            _serializable_validation_error(error, redact_input=redact_inputs)
            for error in exc.errors()
        ]
    }
    return JSONResponse(
        status_code=422,
        content=error_payload("VALIDATION_ERROR", "Request validation failed.", details),
    )


def _serializable_validation_error(
    error: dict[str, Any],
    *,
    redact_input: bool = False,
) -> dict[str, Any]:
    serialized = dict(error)
    if redact_input or "password" in {
        str(item).casefold() for item in serialized.get("loc", ())
    }:
        serialized.pop("input", None)
    if "ctx" in serialized:
        serialized["ctx"] = {key: str(value) for key, value in serialized["ctx"].items()}
    return serialized


async def unexpected_exception_handler(request: Request, __: Exception) -> JSONResponse:
    route_path = google_calendar_oauth_route_path(
        request.scope["path"], request.scope.get("root_path", "")
    )
    private = request.url.path.startswith("/api/public/") or route_path.rstrip("/") in (
        GOOGLE_CALENDAR_AUTHORIZE_PATH,
        GOOGLE_CALENDAR_CALLBACK_PATH,
    )
    return JSONResponse(
        status_code=500,
        content=error_payload("INTERNAL_SERVER_ERROR", "An unexpected error occurred."),
        headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}
        if private else None,
    )
