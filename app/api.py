from contextlib import asynccontextmanager
from copy import deepcopy
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from app import db, tools
from app.api_errors import (
    ApiException,
    AuthenticationRequired,
    api_exception_handler,
    unexpected_exception_handler,
    validation_exception_handler,
)
from app.auth import (
    CSRF_HEADER_NAME,
    CSRF_HEADER_VALUE,
    SESSION_COOKIE_NAME,
    validate_coach_session,
)
from app.config import application_settings_from_env, routing_settings_from_env
from app.dependencies import require_csrf_protection
from app.routes import auth, coach, health, public
from app.routing_service import build_routing_provider


UNSAFE_PROVIDER_METHODS = {"post", "put", "patch", "delete"}
PROVIDER_PATH_PREFIXES = ("/api/auth/coach", "/api/coach")
CSRF_OPENAPI_PARAMETER = {
    "name": CSRF_HEADER_NAME,
    "in": "header",
    "required": True,
    "description": "Required anti-CSRF marker for provider mutations. The value must be `1`.",
    "schema": {
        "type": "string",
        "const": CSRF_HEADER_VALUE,
        "enum": [CSRF_HEADER_VALUE],
    },
    "example": CSRF_HEADER_VALUE,
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db.init_db(settings=app.state.settings)
    yield


def create_app() -> FastAPI:
    settings = application_settings_from_env()
    routing_settings = routing_settings_from_env(settings.environment)
    routing_provider = build_routing_provider(routing_settings)
    tools.configure_default_routing_provider(routing_provider)
    app = FastAPI(
        title="Swim Command Local API",
        version="0.1.0",
        description="Local HTTP API for the swim scheduling prototype.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.routing_readiness = {
        "provider": routing_provider.provider_name,
        "mode": "external" if routing_provider.provider_name == "mapbox" else "synthetic",
        "configured": True,
    }

    app.add_exception_handler(ApiException, api_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unexpected_exception_handler)

    @app.middleware("http")
    async def protect_sensitive_responses(request, call_next):
        security_failure = await _provider_security_failure(request)
        if security_failure is not None:
            return security_failure
        response = await call_next(request)
        path = request.url.path
        is_coach_calendar_handoff = (
            path.startswith("/api/coach/appointments/")
            and path.endswith(("/calendar-links", "/calendar.ics"))
        )
        if (
            path.startswith("/api/public/")
            or path.startswith("/api/auth/coach")
            or path.startswith("/api/coach")
            or is_coach_calendar_handoff
        ):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    # CORS must wrap the provider-security middleware so browser clients can
    # read stable 401/403 responses returned before request-body decoding.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Accept",
            "Idempotency-Key",
            "X-CSRF-Protection",
        ],
    )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(coach.router)
    app.include_router(public.router)
    _document_provider_csrf_header(app)
    return app


async def _provider_security_failure(request: Request):
    """Authenticate provider requests before FastAPI attempts body decoding."""

    if request.method.upper() == "OPTIONS":
        return None
    path = request.url.path
    coach_namespace = path == "/api/coach" or path.startswith("/api/coach/")
    auth_namespace = path == "/api/auth/coach" or path.startswith("/api/auth/coach/")
    if not coach_namespace and not auth_namespace:
        return None

    try:
        if coach_namespace or path == "/api/auth/coach/session":
            session_token = request.cookies.get(SESSION_COOKIE_NAME)
            if session_token is None:
                raise AuthenticationRequired()
            request.state.coach_principal = await run_in_threadpool(
                validate_coach_session,
                session_token,
            )
        if request.method.lower() in UNSAFE_PROVIDER_METHODS:
            require_csrf_protection(request)
    except ApiException as exc:
        return await api_exception_handler(request, exc)
    return None


def _document_provider_csrf_header(app: FastAPI) -> None:
    """Expose the runtime CSRF contract to generated API clients."""

    default_openapi = app.openapi

    def provider_openapi():
        if app.openapi_schema is not None:
            return app.openapi_schema

        schema = default_openapi()
        for path, path_item in schema.get("paths", {}).items():
            if not path.startswith(PROVIDER_PATH_PREFIXES):
                continue
            for method in UNSAFE_PROVIDER_METHODS:
                operation = path_item.get(method)
                if operation is None:
                    continue
                parameters = operation.setdefault("parameters", [])
                parameters.append(deepcopy(CSRF_OPENAPI_PARAMETER))
        app.openapi_schema = schema
        return schema

    app.openapi = provider_openapi


app = create_app()
