import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from uvicorn.protocols.utils import get_path_with_query_string

from app import api, config, db, dependencies, google_calendar_token_deadline
from app.auth import (
    CSRF_HEADER_NAME,
    CSRF_HEADER_VALUE,
    DEMO_PROVIDER_EMAIL,
    DEMO_PROVIDER_INSTRUCTOR_ID,
    DEMO_PROVIDER_PASSWORD,
    SESSION_COOKIE_NAME,
    CoachPrincipal,
    revoke_presented_session,
)
from app.calendar_connection_access import EXPECTED_CALENDAR_INSTRUCTOR_HEADER
from app.calendar_credentials import CalendarCredentialScope, EncryptedCalendarCredential
from app.calendar_oauth_pkce import derive_s256_code_challenge
from app.google_calendar_oauth import GOOGLE_CALENDAR_AUTHORIZE_PATH, GOOGLE_CALENDAR_CALLBACK_PATH
from app.google_calendar_protocol import GOOGLE_CALENDAR_FREEBUSY_SCOPE
from app.google_calendar_token_exchange import GoogleCalendarTokenTransportResponse
from app.models import ProviderCalendarConnectionDB
from app.routes import google_calendar_oauth


AUTHORIZE = GOOGLE_CALENDAR_AUTHORIZE_PATH
CALLBACK = GOOGLE_CALENDAR_CALLBACK_PATH
API_ORIGIN = "https://api.dayweave.example"
COACH_ORIGIN = "https://coach.dayweave.example"
LOCAL_ORIGIN = "http://127.0.0.1:8000"
SECURE_FLOW_COOKIE = "__Host-swim_google_calendar_flow"
LOCAL_FLOW_COOKIE = "swim_google_calendar_flow"
CONNECTION_PATH = "/api/coach/calendar-connections/google"
EXPECTED_INSTRUCTOR = EXPECTED_CALENDAR_INSTRUCTOR_HEADER
CLIENT_ID = "123-dayweave-test.apps.googleusercontent.com"
CLIENT_SECRET = "fake-calendar-client-secret"
KEY_ID = "fake-calendar-root-key"
CODE = "fake-authorization/with+reserved"
STATE = "A" * 43
BINDING = base64.urlsafe_b64encode(b"b" * 32).decode().rstrip("=")
REFRESH_TOKEN = "fake-calendar-refresh-token"
CSRF = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}
AUTHENTICATED = {"Cookie": f"{SESSION_COOKIE_NAME}=fake-provider-session", **CSRF}


@pytest.fixture(autouse=True)
def oauth_environment(monkeypatch):
    monkeypatch.setenv("SWIM_COACH_COOKIE_SECURE", "true")
    monkeypatch.setenv("SWIM_API_CORS_ORIGINS", f"{COACH_ORIGIN},http://localhost:3001")
    monkeypatch.setenv(config.GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV, CLIENT_ID)
    monkeypatch.setenv(config.GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV, CLIENT_SECRET)
    monkeypatch.setenv(config.GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV, API_ORIGIN + CALLBACK)
    monkeypatch.setenv(config.CALENDAR_CREDENTIAL_ACTIVE_KEY_ID_ENV, KEY_ID)
    monkeypatch.setenv(
        config.CALENDAR_CREDENTIAL_KEYS_ENV,
        json.dumps({KEY_ID: base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")}),
    )
    monkeypatch.delenv(config.GOOGLE_CALENDAR_COACH_APP_ORIGIN_ENV, raising=False)
    monkeypatch.setattr(google_calendar_token_deadline, "_post_form_async", _unexpected_call)


@pytest.fixture
def stub_provider_session(monkeypatch):
    def validate(raw_token):
        assert raw_token == "fake-provider-session"
        return CoachPrincipal(
            account_id="fake-account-id",
            session_id="fake-session-id",
            instructor_id="fake-instructor-id",
            display_name="Coach Test",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    monkeypatch.setattr(api, "validate_coach_session", validate)
    monkeypatch.setattr(dependencies, "validate_coach_session", validate)


def _unexpected_call(*_args, **_kwargs):
    pytest.fail("Unexpected database or Google exchange call.")


def _assert_private(response):
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def _assert_redirect(response, result, *, origin=API_ORIGIN):
    assert response.status_code == 303
    assert response.headers["location"] == f"{origin}/?calendar={result}"
    assert response.content == b""
    _assert_private(response)


def _flow_header(binding=BINDING):
    return {"Cookie": f"{SECURE_FLOW_COOKIE}={binding}"}


def test_start_uses_provider_auth_csrf_and_empty_body(stub_provider_session, monkeypatch):
    monkeypatch.setattr(
        google_calendar_oauth, "prepare_google_calendar_authorization_start", _unexpected_call
    )
    client = TestClient(api.create_app(), base_url=API_ORIGIN)
    requests = (
        ({**CSRF}, b"", 401, "AUTHENTICATION_REQUIRED"),
        ({"Cookie": AUTHENTICATED["Cookie"]}, b"", 403, "CSRF_VALIDATION_FAILED"),
        (
            {**AUTHENTICATED, "Origin": "https://untrusted.example"},
            b"",
            403,
            "CSRF_VALIDATION_FAILED",
        ),
        (AUTHENTICATED, b"{}", 422, "VALIDATION_ERROR"),
    )
    for headers, content, status, error_code in requests:
        response = client.post(AUTHORIZE, content=content, headers=headers)
        assert response.status_code == status
        assert response.json()["error"]["code"] == error_code
        assert "set-cookie" not in response.headers
        _assert_private(response)


@pytest.mark.parametrize("start_origin", ["https://api-alias.dayweave.example", "http://api.dayweave.example"])
def test_start_requires_callback_cookie_host_and_scheme(stub_provider_session, monkeypatch, start_origin):
    monkeypatch.setattr(
        google_calendar_oauth, "prepare_google_calendar_authorization_start", _unexpected_call
    )
    client = TestClient(api.create_app(), base_url=start_origin)

    response = client.post(AUTHORIZE, headers=AUTHENTICATED)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CALENDAR_OAUTH_UNAVAILABLE"
    assert "set-cookie" not in response.headers
    _assert_private(response)


def test_mounted_authorize_response_is_private(stub_provider_session, monkeypatch):
    url = f"https://accounts.google.com/o/oauth2/v2/auth?state={STATE}"
    start = SimpleNamespace(
        raw_browser_binding=BINDING,
        authorization_request=SimpleNamespace(authorization_url=url),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    monkeypatch.setattr(
        google_calendar_oauth, "prepare_google_calendar_authorization_start", lambda *_args, **_kwargs: start
    )
    mounted = FastAPI()
    mounted.mount("/mounted", api.create_app())
    client = TestClient(mounted, base_url=API_ORIGIN)

    response = client.post(
        f"/mounted{AUTHORIZE}", headers={**AUTHENTICATED, EXPECTED_INSTRUCTOR: "fake-instructor-id"}
    )

    assert response.status_code == 200
    assert response.json() == {"authorization_url": url}
    _assert_private(response)


@pytest.mark.parametrize(("method", "path"), [
    ("GET", CONNECTION_PATH), ("POST", AUTHORIZE), ("DELETE", CONNECTION_PATH),
])
def test_stale_settings_cannot_access_new_providers_calendar(stub_provider_session, monkeypatch, method, path):
    monkeypatch.setattr(google_calendar_oauth, "prepare_google_calendar_authorization_start", _unexpected_call)
    monkeypatch.setattr("app.routes.coach.get_coach_google_calendar_connection_status", _unexpected_call)
    monkeypatch.setattr("app.routes.coach.disconnect_coach_google_calendar", _unexpected_call)
    client = TestClient(api.create_app(), base_url=API_ORIGIN)

    response = client.request(
        method, path, headers={**AUTHENTICATED, EXPECTED_INSTRUCTOR: "other-instructor-id"}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CALENDAR_CONNECTION_OWNER_CHANGED"
    assert "set-cookie" not in response.headers
    _assert_private(response)


def test_calendar_instructor_precondition_is_allowed_by_cors():
    client = TestClient(api.create_app(), base_url=API_ORIGIN)
    response = client.options(AUTHORIZE, headers={
        "Origin": COACH_ORIGIN,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": f"{CSRF_HEADER_NAME}, {EXPECTED_INSTRUCTOR}",
    })

    assert response.status_code == 200
    assert EXPECTED_INSTRUCTOR.lower() in response.headers["access-control-allow-headers"].lower()


@pytest.mark.parametrize(
    "missing_setting",
    (
        config.GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV,
        config.GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV,
        config.GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV,
        config.CALENDAR_CREDENTIAL_ACTIVE_KEY_ID_ENV,
        config.CALENDAR_CREDENTIAL_KEYS_ENV,
    ),
)
def test_unconfigured_start_is_generic_and_callback_has_only_trusted_target(
    missing_setting, stub_provider_session, monkeypatch
):
    monkeypatch.delenv(missing_setting)
    monkeypatch.setattr(
        google_calendar_oauth, "prepare_google_calendar_authorization_start", _unexpected_call
    )
    client = TestClient(api.create_app(), base_url=API_ORIGIN, follow_redirects=False)

    start = client.post(AUTHORIZE, headers=AUTHENTICATED)
    assert start.status_code == 503
    assert start.json() == {
        "error": {
            "code": "CALENDAR_OAUTH_UNAVAILABLE",
            "message": "Google Calendar authorization is unavailable.",
        }
    }
    assert "set-cookie" not in start.headers
    _assert_private(start)

    callback = client.get(CALLBACK, params={"state": STATE, "code": CODE}, headers=_flow_header())
    if missing_setting == config.GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV:
        assert callback.status_code == 503
        assert callback.json() == start.json()
        assert "location" not in callback.headers
        _assert_private(callback)
    else:
        _assert_redirect(callback, "failed")


@pytest.mark.parametrize(
    "override",
    ("https://unlisted.example", "https://coach%2fhost.example", "http://localhost:3001"),
)
def test_unsafe_coach_origin_cannot_redirect_or_start(override, stub_provider_session, monkeypatch):
    monkeypatch.setenv(
        "SWIM_API_CORS_ORIGINS", f"{COACH_ORIGIN},https://coach%2fhost.example,http://localhost:3001"
    )
    monkeypatch.setenv(config.GOOGLE_CALENDAR_COACH_APP_ORIGIN_ENV, override)
    monkeypatch.setattr(
        google_calendar_oauth, "prepare_google_calendar_authorization_start", _unexpected_call
    )
    client = TestClient(api.create_app(), base_url=API_ORIGIN, follow_redirects=False)

    start = client.post(AUTHORIZE, headers=AUTHENTICATED)
    assert start.status_code == 503
    assert "set-cookie" not in start.headers
    _assert_private(start)

    callback = client.get(CALLBACK, params={"state": STATE, "code": CODE}, headers=_flow_header())
    _assert_redirect(callback, "failed")


@pytest.mark.parametrize(
    "query",
    (
        "code=test",
        f"state={STATE}",
        f"state={STATE}&code=",
        f"state={STATE}&code=first&%63ode=second",
        f"state={STATE}&%73tate={STATE}&code=test",
        f"state={STATE}&code=bad%0Acode",
        f"state={STATE}&code=bad%FFcode",
        f"state={STATE}&code=bad%ZZcode",
        f"state={STATE}&code={'x' * 8193}",
        "state=malformed&code=test",
        f"state={STATE}&error=access_denied&code=test",
    ),
)
def test_callback_rejects_denial_and_malformed_queries_before_completion(query, monkeypatch):
    monkeypatch.setattr(google_calendar_oauth, "complete_google_calendar_oauth_callback", _unexpected_call)
    client = TestClient(api.create_app(), base_url=API_ORIGIN, follow_redirects=False)

    response = client.get(f"{CALLBACK}?{query}", headers=_flow_header())

    _assert_redirect(response, "failed")
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(("root_path", "prefix"), [("", ""), ("/a", ""), ("/a", "/a")])
def test_callback_query_is_scrubbed_on_the_server_scope_before_response(monkeypatch, root_path, prefix):
    requests = []
    access_targets = []
    application = api.create_app()

    async def fake_complete(_session_factory, *, raw_state, authorization_code, **_kwargs):
        requests.append((raw_state, authorization_code))
        raise google_calendar_oauth.GoogleCalendarOAuthError("internal exchange failure")

    async def server(scope, receive, send):
        async def send_with_access_log(message):
            if message["type"] == "http.response.start":
                access_targets.append(get_path_with_query_string(scope))
                assert not scope.get("state", {}).get("google_calendar_callback_query")
            await send(message)

        await application(scope, receive, send_with_access_log)

    monkeypatch.setattr(google_calendar_oauth, "complete_google_calendar_oauth_callback", fake_complete)
    client = TestClient(
        server, base_url="https://spoofed-host.example", follow_redirects=False, root_path=root_path
    )
    params = {"state": STATE, "code": CODE, "next": "https://untrusted.example"}

    response = client.get(f"{prefix}{CALLBACK}", params=params, headers=_flow_header())
    _assert_redirect(response, "failed")
    alias = client.get(f"{prefix}{CALLBACK}/", params=params)
    assert alias.status_code == 404
    assert "location" not in alias.headers
    _assert_private(alias)

    assert requests == [(STATE, CODE)]
    assert access_targets == [f"{prefix}{CALLBACK}", f"{prefix}{CALLBACK}/"]


def test_cancelled_callback_clears_sensitive_route_locals(monkeypatch):
    async def cancel_callback(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(google_calendar_oauth, "complete_google_calendar_oauth_callback", cancel_callback)
    scope = {
        "type": "http",
        "method": "GET",
        "path": CALLBACK,
        "headers": [(b"cookie", f"{SECURE_FLOW_COOKIE}={BINDING}".encode())],
        "app": api.create_app(),
        "state": {"google_calendar_callback_query": google_calendar_oauth._CallbackQuery(STATE, CODE)},
    }

    with pytest.raises(asyncio.CancelledError) as cancelled:
        asyncio.run(google_calendar_oauth.google_calendar_callback(Request(scope)))

    callback_locals = next(
        frame.frame.f_locals for frame in cancelled.traceback if frame.name == "google_calendar_callback"
    )
    assert {"request", "query", "binding"}.isdisjoint(callback_locals)
    assert scope["state"] == {}


def _sign_in(client):
    response = client.post(
        "/api/auth/coach/sign-in",
        headers=CSRF,
        json={"email": DEMO_PROVIDER_EMAIL, "password": DEMO_PROVIDER_PASSWORD},
    )
    assert response.status_code == 200
    return response


def _start(client, *, origin):
    response = client.post(AUTHORIZE, headers={**CSRF, "Origin": origin})
    assert response.status_code == 200
    assert set(response.json()) == {"authorization_url"}
    _assert_private(response)
    authorization = urlsplit(response.json()["authorization_url"])
    assert (authorization.scheme, authorization.netloc, authorization.path) == (
        "https",
        "accounts.google.com",
        "/o/oauth2/v2/auth",
    )
    return response, parse_qs(authorization.query)


def _fake_token_response():
    return GoogleCalendarTokenTransportResponse(
        status_code=200,
        content_type="application/json",
        body=json.dumps(
            {
                "access_token": "fake-calendar-access-token",
                "refresh_token": REFRESH_TOKEN,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": GOOGLE_CALENDAR_FREEBUSY_SCOPE,
            }
        ).encode(),
    )


@pytest.mark.integration
def test_bound_callback_exchanges_once_and_persists_encrypted_connection(
    migrated_postgres_db, monkeypatch
):
    monkeypatch.setenv(config.GOOGLE_CALENDAR_COACH_APP_ORIGIN_ENV, COACH_ORIGIN)
    exchanges = []

    async def fake_exchange(*, fields, **_kwargs):
        exchanges.append(dict(fields))
        return _fake_token_response()

    monkeypatch.setattr(google_calendar_token_deadline, "_post_form_async", fake_exchange)
    with TestClient(api.create_app(), base_url=API_ORIGIN, follow_redirects=False) as client:
        login = _sign_in(client)
        provider_cookie = SimpleCookie(login.headers["set-cookie"])[SESSION_COOKIE_NAME]
        assert provider_cookie["samesite"] == "strict"
        assert provider_cookie["secure"] and provider_cookie["httponly"]
        assert not provider_cookie["domain"]

        start, authorization = _start(client, origin=COACH_ORIGIN)
        assert authorization["redirect_uri"] == [API_ORIGIN + CALLBACK]
        assert authorization["scope"] == [GOOGLE_CALENDAR_FREEBUSY_SCOPE]
        assert authorization["code_challenge_method"] == ["S256"]
        assert "client_secret" not in authorization
        assert "code_verifier" not in authorization
        state = authorization["state"][0]
        flow_cookie = SimpleCookie(start.headers["set-cookie"])[SECURE_FLOW_COOKIE]
        assert flow_cookie.value != state
        assert flow_cookie["samesite"] == "lax"
        assert flow_cookie["secure"] and flow_cookie["httponly"]
        assert flow_cookie["path"] == "/" and not flow_cookie["domain"]
        assert 0 < int(flow_cookie["max-age"]) <= 600
        assert flow_cookie["expires"]
        assert flow_cookie.value not in start.text

        client.cookies.delete(SESSION_COOKIE_NAME)
        params = {"state": state, "code": CODE, "scope": GOOGLE_CALENDAR_FREEBUSY_SCOPE}
        missing = client.get(CALLBACK, params=params, headers={"Cookie": ""})
        wrong = client.get(CALLBACK, params=params, headers=_flow_header())
        for response in (missing, wrong):
            _assert_redirect(response, "failed", origin=COACH_ORIGIN)
            assert "set-cookie" not in response.headers
        assert exchanges == []

        success = client.get(CALLBACK, params=params)
        _assert_redirect(success, "connected", origin=COACH_ORIGIN)
        assert success.request.headers["cookie"] == f"{SECURE_FLOW_COOKIE}={flow_cookie.value}"
        assert "set-cookie" not in success.headers
        assert client.cookies.get(SECURE_FLOW_COOKIE) == flow_cookie.value

        replay = client.get(CALLBACK, params=params, headers=_flow_header(flow_cookie.value))
        _assert_redirect(replay, "failed", origin=COACH_ORIGIN)
        assert len(exchanges) == 1
        assert exchanges[0]["code"] == CODE
        assert derive_s256_code_challenge(exchanges[0]["code_verifier"]) == (
            authorization["code_challenge"][0]
        )
        status = client.get(
            "/api/coach/calendar-connections/google",
            headers={"Cookie": f"{SESSION_COOKIE_NAME}={provider_cookie.value}"},
        )
        assert status.status_code == 200
        assert status.json()["state"] == "connected"
        assert REFRESH_TOKEN not in status.text

        with db.SessionLocal() as session:
            connection = session.query(ProviderCalendarConnectionDB).one()
            envelope = EncryptedCalendarCredential(
                version=connection.credential_version,
                key_id=connection.credential_key_id,
                nonce=connection.credential_nonce,
                ciphertext=connection.credential_ciphertext,
            )
            scope = CalendarCredentialScope(
                instructor_id=DEMO_PROVIDER_INSTRUCTOR_ID, connection_id=connection.id
            )
        assert config.calendar_credential_cipher_from_env().decrypt_refresh_token(
            envelope, scope=scope
        ) == REFRESH_TOKEN


@pytest.mark.integration
def test_callback_rechecks_initiating_session_after_fake_exchange(migrated_postgres_db, monkeypatch):
    monkeypatch.setenv("SWIM_COACH_COOKIE_SECURE", "false")
    monkeypatch.setenv(config.GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV, LOCAL_ORIGIN + CALLBACK)
    with TestClient(api.create_app(), base_url=LOCAL_ORIGIN, follow_redirects=False) as client:
        login = _sign_in(client)
        provider_token = login.cookies.get(SESSION_COOKIE_NAME)
        start, authorization = _start(client, origin=LOCAL_ORIGIN)
        flow_cookie = SimpleCookie(start.headers["set-cookie"])[LOCAL_FLOW_COOKIE]
        assert flow_cookie["path"] == CALLBACK
        assert flow_cookie["samesite"] == "lax" and flow_cookie["httponly"]
        assert not flow_cookie["secure"] and not flow_cookie["domain"]
        client.cookies.delete(SESSION_COOKIE_NAME)

        async def revoke_during_exchange(**_kwargs):
            assert revoke_presented_session(provider_token)
            return _fake_token_response()

        monkeypatch.setattr(
            google_calendar_token_deadline, "_post_form_async", revoke_during_exchange
        )
        response = client.get(CALLBACK, params={"state": authorization["state"][0], "code": CODE})
        _assert_redirect(response, "failed", origin=LOCAL_ORIGIN)
        with db.SessionLocal() as session:
            assert session.query(ProviderCalendarConnectionDB).count() == 0
