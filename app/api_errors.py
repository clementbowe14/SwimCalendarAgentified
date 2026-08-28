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
