import datetime
import json

import pytest
from sqlalchemy.orm import Session

from app import db
from app.auth import CoachPrincipal, authenticate_coach, issue_access_token
from app.models import InstructorDB


COACH_ENDPOINTS = [
    (
        "GET",
        "/api/coach/me",
        None,
    ),
    (
        "GET",
        "/api/coach/schedule?date=2026-08-20",
        None,
    ),
    (
        "GET",
        "/api/coach/command-center/snapshot?date=2026-08-20",
        None,
    ),
    (
        "GET",
        "/api/coach/settings",
        None,
    ),
    (
        "PUT",
        "/api/coach/settings",
        {
            "rounding_increment_mins": 15,
            "post_break_buffer_mins": 15,
            "distance_buffer_tiers": [
                {
                    "id": "near",
                    "label": "Under 5 mi",
                    "min_miles": 0,
                    "max_miles": 4.999,
                    "buffer_mins": 10,
                }
            ],
            "payments": {
                "enabled_methods": ["cash"],
                "cancellation_window_hours": 24,
                "require_payment_method_for_new_clients": False,
            },
        },
    ),
    (
        "POST",
        "/api/coach/availability",
        {
            "client_name": "Charlie",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-21",
            "duration_mins": 60,
            "is_instructor_facility": False,
        },
    ),
    (
        "POST",
        "/api/coach/bookings",
        {
            "client_name": "Charlie",
            "client_address": "450 North Blvd, Austin, TX",
            "start_time": "2026-08-21T10:30:00",
            "duration_mins": 60,
            "is_instructor_facility": False,
        },
    ),
    (
        "POST",
        "/api/coach/lesson-requests/req_sofia_pending_0820/approve",
        None,
    ),
    (
        "POST",
        "/api/coach/lesson-requests/req_sofia_pending_0820/alternatives",
        None,
    ),
    (
        "POST",
        "/api/coach/lesson-requests/req_sofia_pending_0820/decline",
        None,
    ),
]


def test_all_coach_routes_require_bearer_auth(api_client):
    for method, path, body in COACH_ENDPOINTS:
        response = api_client.request(method, path, json=body)

        assert response.status_code == 401, (method, path, response.text)
        assert response.json() == {
            "error": {
                "code": "AUTHENTICATION_REQUIRED",
                "message": "Coach authentication is required.",
            }
        }


def test_invalid_login_credentials_return_stable_401(api_client):
    response = api_client.post(
        "/api/auth/coach/login",
        json={"email": "coach@example.com", "password": "wrong-password"},
    )

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "INVALID_CREDENTIALS",
            "message": "The coach credentials are invalid.",
        }
    }


def test_invalid_bearer_token_returns_stable_401(api_client):
    response = api_client.get(
        "/api/coach/me",
        headers={"Authorization": "Bearer not-a-real-token"},
    )

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "INVALID_AUTH_TOKEN",
            "message": "The coach session is invalid or expired.",
        }
    }


def test_valid_login_and_session_can_read_coach_profile(api_client, coach_auth_headers):
    login_response = api_client.post(
        "/api/auth/coach/login",
        json={"email": "coach@example.com", "password": "swim-dev-pass"},
    )
    login_payload = login_response.json()

    assert login_response.status_code == 200
    assert login_payload["token_type"] == "bearer"
    assert login_payload["coach"] == {
        "instructor_id": "inst_001",
        "display_name": "Coach Sarah",
    }
    expires_at = datetime.datetime.fromisoformat(login_payload["expires_at"].replace("Z", "+00:00"))
    assert expires_at > datetime.datetime.now(datetime.timezone.utc)

    session_response = api_client.get("/api/auth/coach/session", headers=coach_auth_headers)
    me_response = api_client.get("/api/coach/me", headers=coach_auth_headers)

    assert session_response.status_code == 200
    assert session_response.json()["authenticated"] is True
    assert session_response.json()["coach"] == login_payload["coach"]
    assert me_response.status_code == 200
    assert me_response.json() == login_payload["coach"]


def test_configured_coach_without_matching_instructor_returns_403(api_client, monkeypatch):
    monkeypatch.setenv(
        "SWIM_COACH_CREDENTIALS",
        json.dumps(
            [
                {
                    "email": "missing@example.com",
                    "password": "secret",
                    "instructor_id": "inst_missing",
                }
            ]
        ),
    )

    response = api_client.post(
        "/api/auth/coach/login",
        json={"email": "missing@example.com", "password": "secret"},
    )

    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "code": "AUTHORIZATION_FORBIDDEN",
            "message": "The authenticated coach is not allowed to access this resource.",
        }
    }


def test_coach_auth_scopes_resources_to_authenticated_instructor(api_client, monkeypatch):
    _add_second_instructor()
    monkeypatch.setenv(
        "SWIM_COACH_CREDENTIALS",
        json.dumps(
            [
                {
                    "email": "other-coach@example.com",
                    "password": "other-dev-pass",
                    "instructor_id": "inst_002",
                }
            ]
        ),
    )
    token = api_client.post(
        "/api/auth/coach/login",
        json={"email": "other-coach@example.com", "password": "other-dev-pass"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    me = api_client.get("/api/coach/me", headers=headers)
    schedule = api_client.get("/api/coach/schedule", params={"date": "2026-08-20"}, headers=headers)
    snapshot = api_client.get(
        "/api/coach/command-center/snapshot",
        params={"date": "2026-08-20"},
        headers=headers,
    )
    approval = api_client.post(
        "/api/coach/lesson-requests/req_sofia_pending_0820/approve",
        headers=headers,
    )

    assert me.status_code == 200
    assert me.json() == {"instructor_id": "inst_002", "display_name": "Coach Jordan"}
    assert schedule.status_code == 200
    assert schedule.json()["instructor"]["id"] == "inst_002"
    assert schedule.json()["appointments"] == []
    assert snapshot.status_code == 200
    assert snapshot.json()["instructor"]["id"] == "inst_002"
    assert snapshot.json()["clients"] == []
    assert snapshot.json()["requests"] == []
    assert approval.status_code == 404
    assert approval.json()["error"]["code"] == "LESSON_REQUEST_NOT_FOUND"


def test_public_routes_remain_available_without_auth(api_client, assert_public_payload_safe):
    health = api_client.get("/health")
    openapi = api_client.get("/openapi.json")
    invite = api_client.get("/api/public/client-invites/invite_sofia_0826")

    assert health.status_code == 200
    assert openapi.status_code == 200
    assert invite.status_code == 200
    assert_public_payload_safe(invite.json())


def test_demo_credentials_are_disabled_in_strict_environments(monkeypatch):
    monkeypatch.setenv("SWIM_ENV", "production")
    monkeypatch.delenv("SWIM_COACH_CREDENTIALS", raising=False)

    with pytest.raises(RuntimeError, match="SWIM_COACH_CREDENTIALS"):
        authenticate_coach("coach@example.com", "swim-dev-pass")


def test_auth_secret_is_required_in_strict_environments(monkeypatch):
    monkeypatch.setenv("SWIM_ENV", "production")
    monkeypatch.delenv("SWIM_COACH_AUTH_SECRET", raising=False)
    principal = CoachPrincipal(
        instructor_id="inst_001",
        display_name="Coach Sarah",
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
    )

    with pytest.raises(RuntimeError, match="SWIM_COACH_AUTH_SECRET"):
        issue_access_token(principal)


def _add_second_instructor() -> None:
    session: Session = db.SessionLocal()
    try:
        session.add(
            InstructorDB(
                id="inst_002",
                name="Coach Jordan",
                main_address="200 East Ave, Austin, TX",
                lat=30.2701,
                lng=-97.7401,
                work_start_time="09:00",
                work_end_time="16:00",
            )
        )
        session.commit()
    finally:
        session.close()
