import datetime
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import create_app
from app.models import ClientDB, InstructorDB, InviteTokenDB, LessonRequestDB


COACH_AUTH_ENV = "SWIM_COACH_CREDENTIALS"
SARAH_EMAIL = "sarah.coach@example.test"
SARAH_PASSWORD = "sarah-dev-password"
JAMIE_EMAIL = "jamie.coach@example.test"
JAMIE_PASSWORD = "jamie-dev-password"
JAMIE_REQUEST_TOKEN = "req_jamie_pending_auth"
SCHEDULER_SETTINGS_PAYLOAD = {
    "rounding_increment_mins": 15,
    "post_break_buffer_mins": 15,
    "distance_buffer_tiers": [
        {"id": "near", "label": "Nearby", "min_miles": 0, "max_miles": 5, "buffer_mins": 10},
        {"id": "far", "label": "Farther", "min_miles": 5, "max_miles": None, "buffer_mins": 20},
    ],
    "payments": {
        "enabled_methods": ["card"],
        "cancellation_window_hours": 24,
        "require_payment_method_for_new_clients": False,
    },
}
AVAILABILITY_PAYLOAD = {
    "client_name": "Validation Swimmer",
    "client_address": "450 North Blvd, Austin, TX",
    "requested_date": "2026-08-21",
    "duration_mins": 45,
}
BOOKING_PAYLOAD = {
    "client_name": "Validation Swimmer",
    "client_address": "450 North Blvd, Austin, TX",
    "start_time": "2026-08-21T10:00:00",
    "duration_mins": 45,
    "idempotency_key": "auth-validation-booking",
}


@pytest.fixture()
def auth_client(migrated_postgres_db: None, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        COACH_AUTH_ENV,
        json.dumps(
            [
                {
                    "email": SARAH_EMAIL,
                    "password": SARAH_PASSWORD,
                    "instructor_id": "inst_001",
                    "display_name": "Coach Sarah",
                },
                {
                    "email": JAMIE_EMAIL,
                    "password": JAMIE_PASSWORD,
                    "instructor_id": "inst_002",
                    "display_name": "Coach Jamie",
                },
            ]
        ),
    )
    _seed_second_coach()
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("GET", "/api/coach/me", {}),
        ("GET", "/api/coach/schedule", {"params": {"date": "2026-08-20"}}),
        ("GET", "/api/coach/command-center/snapshot", {"params": {"date": "2026-08-20"}}),
        ("GET", "/api/coach/settings", {}),
        ("PUT", "/api/coach/settings", {"json": SCHEDULER_SETTINGS_PAYLOAD}),
        ("POST", "/api/coach/availability", {"json": AVAILABILITY_PAYLOAD}),
        ("POST", "/api/coach/bookings", {"json": BOOKING_PAYLOAD}),
        ("POST", "/api/coach/lesson-requests/req_sofia_pending_0820/approve", {}),
        ("POST", "/api/coach/lesson-requests/req_sofia_pending_0820/confirm", {"json": {}}),
        ("POST", "/api/coach/lesson-requests/req_sofia_pending_0820/alternatives", {}),
        ("POST", "/api/coach/lesson-requests/req_sofia_pending_0820/decline", {}),
    ],
)
def test_every_coach_route_requires_stable_json_auth(
    auth_client: TestClient,
    method: str,
    path: str,
    kwargs: dict[str, Any],
):
    missing = auth_client.request(method, path, **kwargs)
    invalid = auth_client.request(
        method,
        path,
        headers={"Authorization": "Bearer not-a-real-token"},
        **kwargs,
    )

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "INVALID_AUTH_TOKEN"


def test_invalid_coach_credentials_return_stable_json_401(auth_client: TestClient):
    response = auth_client.post(
        "/api/auth/coach/login",
        json={"email": SARAH_EMAIL, "password": "wrong-password"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_valid_auth_reaches_all_mvp_coach_surfaces(auth_client: TestClient):
    auth_headers = _login(auth_client, SARAH_EMAIL, SARAH_PASSWORD)

    me = auth_client.get("/api/coach/me", headers=auth_headers)
    assert me.status_code == 200
    assert me.json()["instructor_id"] == "inst_001"

    schedule = auth_client.get(
        "/api/coach/schedule",
        params={"date": "2026-08-20"},
        headers=auth_headers,
    )
    assert schedule.status_code == 200
    assert schedule.json()["instructor"]["id"] == "inst_001"

    snapshot = auth_client.get(
        "/api/coach/command-center/snapshot",
        params={"date": "2026-08-20"},
        headers=auth_headers,
    )
    assert snapshot.status_code == 200
    assert snapshot.json()["instructor"]["id"] == "inst_001"

    settings = auth_client.get("/api/coach/settings", headers=auth_headers)
    assert settings.status_code == 200

    saved_settings = auth_client.put(
        "/api/coach/settings",
        json=settings.json(),
        headers=auth_headers,
    )
    assert saved_settings.status_code == 200

    availability = auth_client.post(
        "/api/coach/availability",
        json=_availability_payload(),
        headers=auth_headers,
    )
    assert availability.status_code == 200

    booking = auth_client.post(
        "/api/coach/bookings",
        json=_booking_payload(),
        headers=auth_headers,
    )
    assert booking.status_code == 200

    request_actions = {
        "confirm": _create_sarah_lesson_request("req_sarah_auth_confirm", 14),
        "alternatives": _create_sarah_lesson_request("req_sarah_auth_alternatives", 15),
        "decline": _create_sarah_lesson_request("req_sarah_auth_decline", 16),
    }
    for action, request_token in request_actions.items():
        response = auth_client.post(
            f"/api/coach/lesson-requests/{request_token}/{action}",
            headers=auth_headers,
        )
        assert response.status_code == 200


def test_coach_auth_is_scoped_to_authenticated_instructor_not_frontend_ids(auth_client: TestClient):
    sarah_headers = _login(auth_client, SARAH_EMAIL, SARAH_PASSWORD)
    jamie_headers = _login(auth_client, JAMIE_EMAIL, JAMIE_PASSWORD)

    spoofed_schedule = auth_client.get(
        "/api/coach/schedule",
        params={"date": "2026-08-20", "instructor_id": "inst_002"},
        headers=sarah_headers,
    )
    assert spoofed_schedule.status_code == 200
    assert spoofed_schedule.json()["instructor"]["id"] == "inst_001"

    cross_coach_confirmation = auth_client.post(
        f"/api/coach/lesson-requests/{JAMIE_REQUEST_TOKEN}/confirm",
        json={},
        headers=sarah_headers,
    )
    assert cross_coach_confirmation.status_code == 404
    assert cross_coach_confirmation.json()["error"]["code"] == "LESSON_REQUEST_NOT_FOUND"

    same_coach_confirmation = auth_client.post(
        f"/api/coach/lesson-requests/{JAMIE_REQUEST_TOKEN}/confirm",
        json={},
        headers=jamie_headers,
    )
    assert same_coach_confirmation.status_code == 200
    assert same_coach_confirmation.json()["status"] == "confirmed"


def test_public_routes_remain_unauthenticated_and_public_safe(
    auth_client: TestClient,
    assert_public_payload_safe,
):
    invite = auth_client.get("/api/public/client-invites/invite_sofia_0826")
    assert invite.status_code == 200
    assert_public_payload_safe(invite.json())

    status = auth_client.get("/api/public/lesson-requests/req_sofia_pending_0820")
    assert status.status_code == 200
    assert_public_payload_safe(status.json())


def test_openapi_documents_auth_contract_without_fixture_secrets(auth_client: TestClient):
    schema = auth_client.get("/openapi.json").json()

    assert "/api/auth/coach/login" in schema["paths"]
    assert "CoachLoginRequest" in schema["components"]["schemas"]
    assert "CoachAuthResponse" in schema["components"]["schemas"]

    fixtures = Path("contracts/fixtures").glob("**/*.json")
    fixture_text = "\n".join(path.read_text() for path in fixtures)
    assert SARAH_PASSWORD not in fixture_text
    assert JAMIE_PASSWORD not in fixture_text


def _login(client: TestClient, email: str, password: str) -> dict[str, str]:
    response = client.post("/api/auth/coach/login", json={"email": email, "password": password})
    assert response.status_code == 200
    payload = response.json()
    assert payload["token_type"] == "bearer"
    assert payload["access_token"]
    assert payload["coach"]["instructor_id"].startswith("inst_")
    return {"Authorization": f"Bearer {payload['access_token']}"}


def _seed_second_coach() -> None:
    session = db.SessionLocal()
    try:
        if not session.query(InstructorDB).filter_by(id="inst_002").first():
            session.add(
                InstructorDB(
                    id="inst_002",
                    name="Coach Jamie",
                    main_address="200 East Ave, Austin, TX",
                    lat=30.274,
                    lng=-97.731,
                    work_start_time="08:00",
                    work_end_time="17:00",
                )
            )
        if not session.query(ClientDB).filter_by(id="client_jamie").first():
            session.add(
                ClientDB(
                    id="client_jamie",
                    instructor_id="inst_002",
                    name="Jamie Client",
                    email="jamie.client@example.test",
                    phone="(512) 555-0134",
                    default_location_address="210 East Ave, Austin, TX",
                    default_lat=30.275,
                    default_lng=-97.732,
                )
            )
        if not session.query(InviteTokenDB).filter_by(token="invite_jamie_auth").first():
            session.add(
                InviteTokenDB(
                    token="invite_jamie_auth",
                    instructor_id="inst_002",
                    client_id="client_jamie",
                    business_name="Jamie Swim",
                    location_label="Client pool",
                    preferred_duration_mins=45,
                    allowed_durations_mins="30,45,60",
                    expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=30),
                )
            )
        if not session.query(LessonRequestDB).filter_by(request_token=JAMIE_REQUEST_TOKEN).first():
            session.add(
                LessonRequestDB(
                    id=JAMIE_REQUEST_TOKEN,
                    request_token=JAMIE_REQUEST_TOKEN,
                    invite_token="invite_jamie_auth",
                    instructor_id="inst_002",
                    client_id="client_jamie",
                    client_name="Jamie Client",
                    client_email="jamie.client@example.test",
                    client_phone="(512) 555-0134",
                    client_address="210 East Ave, Austin, TX",
                    lat=30.275,
                    lng=-97.732,
                    requested_date=datetime.date(2026, 8, 20),
                    slot_start=datetime.datetime(2026, 8, 20, 14, 0),
                    slot_end=datetime.datetime(2026, 8, 20, 14, 45),
                    duration_mins=45,
                    travel_time_mins=8,
                    buffer_mins=10,
                    distance_miles=1.4,
                    location_type="home",
                    status="pending",
                )
            )
        session.commit()
    finally:
        session.close()


def _create_sarah_lesson_request(request_token: str, hour: int) -> str:
    session = db.SessionLocal()
    try:
        session.add(
            LessonRequestDB(
                id=request_token,
                request_token=request_token,
                invite_token="invite_sofia_0826",
                instructor_id="inst_001",
                client_id="client_sofia",
                client_name="Sofia Martinez",
                client_email="ana.martinez@example.com",
                client_phone="(512) 555-0194",
                client_address="820 Lakeview Dr, Austin, TX",
                lat=30.289,
                lng=-97.739,
                requested_date=datetime.date(2026, 8, 21),
                slot_start=datetime.datetime(2026, 8, 21, hour, 0),
                slot_end=datetime.datetime(2026, 8, 21, hour, 45),
                duration_mins=45,
                travel_time_mins=12,
                buffer_mins=10,
                distance_miles=2.1,
                location_type="home",
                status="pending",
            )
        )
        session.commit()
    finally:
        session.close()
    return request_token


def _scheduler_settings_payload() -> dict[str, Any]:
    return json.loads(json.dumps(SCHEDULER_SETTINGS_PAYLOAD))


def _availability_payload() -> dict[str, Any]:
    return dict(AVAILABILITY_PAYLOAD)


def _booking_payload() -> dict[str, Any]:
    return dict(BOOKING_PAYLOAD)
