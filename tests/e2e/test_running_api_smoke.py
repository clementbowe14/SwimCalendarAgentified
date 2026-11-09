import json
import os
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.parse import urlparse
import urllib.request

import pytest


BASE_URL = os.getenv("SWIM_E2E_BASE_URL", "").rstrip("/")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not BASE_URL,
        reason="Set SWIM_E2E_BASE_URL, for example http://127.0.0.1:8000, to run API e2e smoke tests.",
    ),
]


def test_running_api_health_endpoint():
    assert _get_json("/health") == {"status": "ok"}


def test_running_api_exposes_frontend_contract_routes():
    openapi = _get_json("/openapi.json")
    paths = set(openapi["paths"].keys())

    assert "/api/coach/schedule" in paths
    assert "/api/coach/availability" in paths
    assert "/api/public/client-invites/{token}" in paths
    assert "/api/public/client-invites/{token}/slots" in paths
    assert "/api/public/client-invites/{token}/lesson-requests" in paths


def test_running_api_uses_revocable_cookie_session():
    cookie_jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    sign_in = _request_json(
        opener,
        "/api/auth/coach/sign-in",
        method="POST",
        payload={
            "email": os.getenv("SWIM_E2E_EMAIL", "coach@example.com"),
            "password": os.getenv("SWIM_E2E_PASSWORD", "swim-dev-pass"),
        },
        csrf=True,
    )

    assert sign_in["authenticated"] is True
    assert set(sign_in) == {"authenticated", "expires_at", "coach"}
    assert "access_token" not in sign_in
    assert "token_type" not in sign_in

    session_cookie = next(cookie for cookie in cookie_jar if cookie.name == "swim_session")
    assert session_cookie.path == "/api"
    assert session_cookie.has_nonstandard_attr("HttpOnly")
    assert session_cookie.get_nonstandard_attr("SameSite").lower() == "strict"
    captured_cookie = f"{session_cookie.name}={session_cookie.value}"

    session = _request_json(opener, "/api/auth/coach/session")
    assert session["authenticated"] is True
    assert _request_json(opener, "/api/coach/me")["instructor_id"] == sign_in["coach"]["instructor_id"]

    signed_out = _request_json(
        opener,
        "/api/auth/coach/sign-out",
        method="POST",
        csrf=True,
    )
    assert signed_out["success"] is True

    replay = urllib.request.build_opener()
    for path in ("/api/auth/coach/session", "/api/coach/me"):
        with pytest.raises(HTTPError) as exc_info:
            _request_json(replay, path, cookie=captured_cookie)
        assert exc_info.value.code == 401
        error = json.loads(exc_info.value.read().decode("utf-8"))
        assert error["error"]["code"] == "INVALID_AUTH_TOKEN"


def _get_json(path: str) -> dict:
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        headers={"accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _request_json(
    opener: urllib.request.OpenerDirector,
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    csrf: bool = False,
    cookie: str | None = None,
) -> dict:
    headers = {"accept": "application/json"}
    if payload is not None:
        headers["content-type"] = "application/json"
    if csrf:
        headers["X-CSRF-Protection"] = "1"
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers=headers,
        method=method,
    )
    with opener.open(request, timeout=5) as response:
        assert urlparse(response.url).netloc == urlparse(BASE_URL).netloc
        return json.loads(response.read().decode("utf-8"))
