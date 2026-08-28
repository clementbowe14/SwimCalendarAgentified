from typing import Any, Optional

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


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
            "Calendar links are unavailable until the lesson is confirmed.",
            status_code=409,
        )


class SchedulingConflict(ApiException):
    def __init__(
        self,
        message: str = "The requested time is unavailable.",
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__("SCHEDULING_CONFLICT", message, status_code=409, details=details)


class IdempotencyConflict(ApiException):
    def __init__(self):
        super().__init__(
            "IDEMPOTENCY_KEY_CONFLICT",
            "The idempotency key was already used for a different mutation.",
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


async def api_exception_handler(_: Request, exc: ApiException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(exc.code, exc.message, exc.details),
    )


async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = {"fields": exc.errors()}
    return JSONResponse(
        status_code=422,
        content=error_payload("VALIDATION_ERROR", "Request validation failed.", details),
    )


async def unexpected_exception_handler(_: Request, __: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=error_payload("INTERNAL_SERVER_ERROR", "An unexpected error occurred."),
    )
