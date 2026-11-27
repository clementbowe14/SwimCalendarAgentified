import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import config, db
from app.api_errors import ApiException, CalendarOAuthUnavailable
from app.api_schemas import CoachGoogleCalendarAuthorizationResponse, ErrorResponse
from app.calendar_connection_access import get_google_calendar_coach
from app.calendar_credentials import CalendarCredentialConfigurationError
from app.calendar_oauth_pkce import CalendarOAuthPKCEConfigurationError
from app.dependencies import CoachContext, require_csrf_protection
from app.google_calendar_oauth import (
    GOOGLE_CALENDAR_AUTHORIZE_PATH,
    GOOGLE_CALENDAR_CALLBACK_PATH,
    GOOGLE_CALENDAR_CONNECTION_PATH,
    GoogleCalendarOAuthError,
    google_calendar_oauth_origin,
    google_calendar_oauth_route_path,
)
from app.google_calendar_oauth_callback_service import complete_google_calendar_oauth_callback
from app.google_calendar_oauth_start import prepare_google_calendar_authorization_start
from app.google_calendar_protocol import GOOGLE_CALENDAR_AUTHORIZATION_CODE_MAX_BYTES


router = APIRouter()
_CALLBACK_QUERY_KEY = "google_calendar_callback_query"
_PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}
_STATE_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
_INVALID_ESCAPE = re.compile(rb"%(?![0-9A-Fa-f]{2})")
_MAX_CALLBACK_QUERY_BYTES = 32 * 1024
_OAUTH_ERRORS = (
    GoogleCalendarOAuthError,
    CalendarCredentialConfigurationError,
    CalendarOAuthPKCEConfigurationError,
)


@dataclass(frozen=True, slots=True, repr=False)
class _CallbackQuery:
    state: str
    code: str


def _parse_callback_query(query: bytes) -> _CallbackQuery | None:
    if len(query) > _MAX_CALLBACK_QUERY_BYTES or _INVALID_ESCAPE.search(query):
        return None
    try:
        fields = parse_qsl(
            query.decode("ascii"),
            keep_blank_values=True,
            strict_parsing=True,
            errors="strict",
            max_num_fields=32,
        )
    except ValueError:
        return None

    values = {}
    for name, value in fields:
        if name == "error" or name in values:
            return None
        if name in ("code", "state"):
            values[name] = value

    state, code = values.get("state", ""), values.get("code", "")
    if (
        _STATE_PATTERN.fullmatch(state) is None
        or len(code) > GOOGLE_CALENDAR_AUTHORIZATION_CODE_MAX_BYTES
        or re.fullmatch(r"[!-~]+", code) is None
    ):
        return None
    return _CallbackQuery(state=state, code=code)


class GoogleCalendarOAuthPrivacyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = google_calendar_oauth_route_path(scope["path"], scope.get("root_path", ""))
        canonical_path = path.rstrip("/")
        is_callback = canonical_path == GOOGLE_CALENDAR_CALLBACK_PATH
        if not is_callback and canonical_path not in (
            GOOGLE_CALENDAR_AUTHORIZE_PATH, GOOGLE_CALENDAR_CONNECTION_PATH
        ):
            await self.app(scope, receive, send)
            return

        async def send_private(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).update(_PRIVATE_HEADERS)
            await send(message)

        if not is_callback:
            await self.app(scope, receive, send_private)
            return

        # Uvicorn formats its access entry from this same scope at response start.
        raw_query = scope["query_string"]
        scope["query_string"] = b""
        if path != GOOGLE_CALENDAR_CALLBACK_PATH:
            del raw_query
            await Response(status_code=404, headers=_PRIVATE_HEADERS)(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        if scope["method"] == "GET":
            state[_CALLBACK_QUERY_KEY] = _parse_callback_query(raw_query)
        del raw_query

        try:
            await self.app(scope, receive, send_private)
        finally:
            state.pop(_CALLBACK_QUERY_KEY, None)


@router.post(
    GOOGLE_CALENDAR_AUTHORIZE_PATH,
    tags=["coach"],
    dependencies=[Depends(require_csrf_protection)],
    response_model=CoachGoogleCalendarAuthorizationResponse,
    responses={
        401: {"model": ErrorResponse, "description": "An active coach session is required."},
        403: {"model": ErrorResponse, "description": "CSRF protection failed."},
        409: {"model": ErrorResponse, "description": "The signed-in provider changed."},
        422: {"model": ErrorResponse, "description": "The request body must be empty."},
        503: {"model": ErrorResponse, "description": "Google Calendar authorization is unavailable."},
    },
)
async def start_google_calendar_authorization(
    request: Request,
    response: Response,
    coach: CoachContext = Depends(get_google_calendar_coach),
) -> CoachGoogleCalendarAuthorizationResponse:
    if await request.body():
        raise ApiException("VALIDATION_ERROR", "This endpoint does not accept a request body.", 422)

    settings = request.app.state.settings
    try:
        configuration = config.google_calendar_oauth_configuration_from_env()
        callback_origin = _callback_origin(settings, configuration.redirect_uri)
        _require_callback_cookie_origin(request, callback_origin)
        _coach_app_origin(settings, callback_origin)
        # A start requires the callback's exchange configuration and encryption keys.
        config.google_calendar_token_exchange_configuration_from_env()
        config.calendar_credential_cipher_from_env()
        start = await run_in_threadpool(
            prepare_google_calendar_authorization_start,
            db.SessionLocal,
            account_id=coach.account_id,
            session_id=coach.session_id,
            configuration=configuration,
            pkce_cipher=config.calendar_oauth_pkce_cipher_from_env(),
        )
    except _OAUTH_ERRORS:
        raise CalendarOAuthUnavailable() from None

    max_age = int((start.expires_at - datetime.now(timezone.utc)).total_seconds())
    if max_age <= 0:
        raise CalendarOAuthUnavailable()
    response.set_cookie(
        value=start.raw_browser_binding,
        max_age=max_age,
        expires=start.expires_at,
        **_flow_cookie_options(callback_origin),
    )
    return CoachGoogleCalendarAuthorizationResponse(
        authorization_url=start.authorization_request.authorization_url
    )


@router.get(
    GOOGLE_CALENDAR_CALLBACK_PATH,
    tags=["oauth"],
    response_class=RedirectResponse,
    status_code=303,
    responses={
        303: {
            "description": "Redirects to the coach app with the calendar connection result.",
            "headers": {"Location": {"schema": {"type": "string"}}},
        },
        503: {"model": ErrorResponse, "description": "Google Calendar authorization is unavailable."},
    },
)
async def google_calendar_callback(request: Request) -> RedirectResponse:
    query = request.scope.get("state", {}).pop(_CALLBACK_QUERY_KEY, None)
    binding = None
    try:
        settings = request.app.state.settings
        callback_origin = _callback_origin(
            settings, os.getenv(config.GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV)
        )
        try:
            coach_origin = _coach_app_origin(settings, callback_origin)
        except CalendarOAuthUnavailable:
            return _callback_redirect(callback_origin, "failed")

        cookie_options = _flow_cookie_options(callback_origin)
        binding = request.cookies.get(cookie_options["key"])
        if query is None or not binding:
            return _callback_redirect(coach_origin, "failed")

        try:
            connection = await complete_google_calendar_oauth_callback(
                db.SessionLocal,
                raw_state=query.state,
                raw_browser_binding=binding,
                authorization_code=query.code,
                configuration=config.google_calendar_token_exchange_configuration_from_env(),
                pkce_cipher=config.calendar_oauth_pkce_cipher_from_env(),
                credential_cipher=config.calendar_credential_cipher_from_env(),
            )
        except _OAUTH_ERRORS:
            return _callback_redirect(coach_origin, "failed")
        if connection.state != "connected":
            return _callback_redirect(coach_origin, "failed")

        # The state was consumed. Leave the short-lived cookie alone so an older
        # response cannot clear the binding for a newer authorization.
        return _callback_redirect(coach_origin, "connected")
    finally:
        del request, query, binding


def _callback_origin(settings: config.ApplicationSettings, redirect_uri: str | None) -> str:
    origin = google_calendar_oauth_origin(redirect_uri, path=GOOGLE_CALENDAR_CALLBACK_PATH)
    if origin is None or (settings.cookie_secure and not origin.startswith("https://")):
        raise CalendarOAuthUnavailable()
    return origin


def _require_callback_cookie_origin(request: Request, callback_origin: str) -> None:
    callback = urlsplit(callback_origin)
    try:
        request_url = request.url
        if request_url.scheme == callback.scheme and request_url.hostname == callback.hostname:
            return
    except ValueError:
        pass
    raise CalendarOAuthUnavailable() from None


def _coach_app_origin(settings: config.ApplicationSettings, callback_origin: str) -> str:
    configured_origin = os.getenv(config.GOOGLE_CALENDAR_COACH_APP_ORIGIN_ENV)
    if configured_origin is None:
        return callback_origin

    origin = google_calendar_oauth_origin(configured_origin, path="")
    if (
        origin is None
        or configured_origin not in settings.cors_origins
        or origin.partition("://")[0] != callback_origin.partition("://")[0]
    ):
        raise CalendarOAuthUnavailable()
    return origin


def _flow_cookie_options(callback_origin: str) -> dict:
    secure = callback_origin.startswith("https://")
    return {
        "key": "__Host-swim_google_calendar_flow" if secure else "swim_google_calendar_flow",
        "path": "/" if secure else GOOGLE_CALENDAR_CALLBACK_PATH,
        "secure": secure,
        "httponly": True,
        "samesite": "lax",
    }


def _callback_redirect(origin: str, result: Literal["connected", "failed"]) -> RedirectResponse:
    return RedirectResponse(f"{origin}/?calendar={result}", status_code=303)
