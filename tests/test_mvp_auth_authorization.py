import datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import create_app
from app.auth import (
    CSRF_HEADER_NAME,
    CSRF_HEADER_VALUE,
    DEMO_PROVIDER_EMAIL,
    DEMO_PROVIDER_PASSWORD,
    SESSION_COOKIE_NAME,
    provision_provider_account,
)
from app.models import (
    AppointmentDB,
    ClientDB,
    InstructorDB,
    InviteTokenDB,
    LessonRequestDB,
    ProviderWorkingHoursDB,
)


JAMIE_EMAIL = "jamie.coach@example.com"
JAMIE_PASSWORD = "jamie-secure-password"
JAMIE_REQUEST_TOKEN = "req_jamie_pending_auth"
SCHEDULER_SETTINGS_PAYLOAD = {
    "expected_revision": 0,
    "distance_buffers_enabled": True,
    "post_break_recovery_mins": 15,
    "distance_buffer_tiers": [
        {"id": "near", "buffer_mins": 10},
        {"id": "medium", "buffer_mins": 20},
        {"id": "far", "buffer_mins": 30},
    ],
}
AVAILABILITY_PAYLOAD = {
    "client_name": "Validation Swimmer",
    "client_address": "450 North Blvd, Austin, TX",
    "requested_date": "2026-08-21",
    "duration_mins": 45,
}


@pytest.fixture()
def auth_client(migrated_postgres_db: None):
    _seed_second_coach()
    provision_provider_account("inst_002", JAMIE_EMAIL, JAMIE_PASSWORD)
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("GET", "/api/coach/me", {}),
        ("GET", "/api/coach/schedule", {"params": {"date": "2026-08-20"}}),
        ("GET", "/api/coach/command-center/snapshot", {"params": {"date": "2026-08-20"}}),
        ("GET", "/api/coach/settings", {}),
        ("PUT", "/api/coach/settings", {"json": {}}),
        ("GET", "/api/coach/scheduling-policy", {}),
        ("PUT", "/api/coach/scheduling-policy", {"json": SCHEDULER_SETTINGS_PAYLOAD}),
        ("POST", "/api/coach/availability", {"json": AVAILABILITY_PAYLOAD}),
        ("POST", "/api/coach/lesson-requests/req_sofia_pending_0820/approve", {}),
        ("POST", "/api/coach/lesson-requests/req_sofia_pending_0820/confirm", {"json": {}}),
        ("POST", "/api/coach/lesson-requests/req_sofia_pending_0820/alternatives", {}),
        ("POST", "/api/coach/lesson-requests/req_sofia_pending_0820/decline", {}),
    ],
)
def test_every_coach_route_requires_stable_cookie_auth_before_csrf(
    auth_client: TestClient,
    method: str,
    path: str,
    kwargs: dict[str, Any],
):
    missing = auth_client.request(method, path, **kwargs)
    bearer_only = auth_client.request(
        method,
        path,
        headers={"Authorization": "Bearer not-a-real-token"},
        **kwargs,
    )
    unknown_cookie = auth_client.request(
        method,
        path,
        headers={"Cookie": f"{SESSION_COOKIE_NAME}={'A' * 43}"},
        **kwargs,
    )

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert bearer_only.status_code == 401
    assert bearer_only.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert unknown_cookie.status_code == 401
    assert unknown_cookie.json()["error"]["code"] == "INVALID_AUTH_TOKEN"


def test_valid_cookie_reaches_representative_coach_reads_and_mutations(auth_client: TestClient):
    headers = _login(auth_client, DEMO_PROVIDER_EMAIL, DEMO_PROVIDER_PASSWORD)

    me = auth_client.get("/api/coach/me", headers=headers)
    schedule = auth_client.get(
        "/api/coach/schedule",
        params={"date": "2026-08-20"},
        headers=headers,
    )
    snapshot = auth_client.get(
        "/api/coach/command-center/snapshot",
        params={"date": "2026-08-20"},
        headers=headers,
    )
    settings = auth_client.get("/api/coach/scheduling-policy", headers=headers)
    saved_settings = auth_client.put(
        "/api/coach/scheduling-policy",
        json={
            "expected_revision": settings.json()["revision"],
            "distance_buffers_enabled": settings.json()["distance_buffers_enabled"],
            "post_break_recovery_mins": settings.json()["post_break_recovery_mins"],
            "distance_buffer_tiers": [
                {"id": tier["id"], "buffer_mins": tier["buffer_mins"]}
                for tier in settings.json()["distance_buffer_tiers"]
            ],
        },
        headers=headers,
    )
    availability = auth_client.post(
        "/api/coach/availability",
        json=AVAILABILITY_PAYLOAD,
        headers=headers,
    )

    assert me.status_code == 200 and me.json()["instructor_id"] == "inst_001"
    assert schedule.status_code == 200 and schedule.json()["instructor"]["id"] == "inst_001"
    assert snapshot.status_code == 200 and snapshot.json()["instructor"]["id"] == "inst_001"
    assert settings.status_code == 200
    assert saved_settings.status_code == 200
    assert availability.status_code == 200


def test_authenticated_provider_scope_ignores_spoofed_ids_and_hides_foreign_resources(
    auth_client: TestClient,
):
    sarah_headers = _login(auth_client, DEMO_PROVIDER_EMAIL, DEMO_PROVIDER_PASSWORD)
    jamie_headers = _login(auth_client, JAMIE_EMAIL, JAMIE_PASSWORD)

    spoofed_schedule = auth_client.get(
        "/api/coach/schedule",
        params={"date": "2026-08-20", "instructor_id": "inst_002"},
        headers=sarah_headers,
    )
    sarah_cross_read = auth_client.post(
        f"/api/coach/lesson-requests/{JAMIE_REQUEST_TOKEN}/confirm",
        json={},
        headers=sarah_headers,
    )
    jamie_cross_read = auth_client.post(
        "/api/coach/lesson-requests/req_sofia_pending_0820/confirm",
        json={},
        headers=jamie_headers,
    )
    own_confirmation = auth_client.post(
        f"/api/coach/lesson-requests/{JAMIE_REQUEST_TOKEN}/confirm",
        json={},
        headers=jamie_headers,
    )

    assert spoofed_schedule.status_code == 200
    assert spoofed_schedule.json()["instructor"]["id"] == "inst_001"
    assert sarah_cross_read.status_code == 404
    assert sarah_cross_read.json()["error"]["code"] == "LESSON_REQUEST_NOT_FOUND"
    assert jamie_cross_read.status_code == 404
    assert jamie_cross_read.json()["error"]["code"] == "LESSON_REQUEST_NOT_FOUND"
    assert own_confirmation.status_code == 200


def test_request_appointment_replay_is_scoped_to_authenticated_provider(auth_client: TestClient):
    with db.SessionLocal() as session:
        request = session.query(LessonRequestDB).filter_by(
            request_token="req_sofia_pending_0820"
        ).one()
        foreign = AppointmentDB(
            id=f"app_{uuid4().hex[:12]}",
            instructor_id="inst_002",
            client_id="client_jamie",
            request_id=request.id,
            title="Foreign request collision",
            start_time=request.slot_start,
            end_time=request.slot_end,
            location_address="210 East Ave, Austin, TX",
            lat=30.275,
            lng=-97.732,
            is_break=False,
            event_type="lesson",
            status="scheduled",
        )
        session.add(foreign)
        session.commit()
        foreign_id = foreign.id

    headers = _login(auth_client, DEMO_PROVIDER_EMAIL, DEMO_PROVIDER_PASSWORD)
    response = auth_client.post(
        "/api/coach/lesson-requests/req_sofia_pending_0820/confirm",
        json={},
        headers=headers,
    )
    assert response.status_code == 200
    assert foreign_id not in response.json()["appointment_ids"]
    with db.SessionLocal() as session:
        own = session.query(AppointmentDB).filter_by(
            instructor_id="inst_001",
            request_id="req_sofia_pending_0820",
        ).all()
        assert own


def test_appointment_idempotency_is_provider_scoped(migrated_postgres_db):
    _seed_second_coach()
    key = "shared-provider-safe-key"
    first_id = db.save_appointment(
        instructor_id="inst_001",
        client_id="client_alice",
        title="Sarah shared key",
        start_time=datetime.datetime(2026, 8, 23, 14, 0),
        end_time=datetime.datetime(2026, 8, 23, 14, 45),
        location_address="450 North Blvd, Austin, TX",
        lat=30.33,
        lng=-97.72,
        idempotency_key=key,
    )
    second_id = db.save_appointment(
        instructor_id="inst_002",
        client_id="client_jamie",
        title="Jamie shared key",
        start_time=datetime.datetime(2026, 8, 23, 14, 0),
        end_time=datetime.datetime(2026, 8, 23, 14, 45),
        location_address="210 East Ave, Austin, TX",
        lat=30.275,
        lng=-97.732,
        idempotency_key=key,
    )

    assert first_id != second_id
    assert db.get_appointment_by_idempotency_key("inst_001", key)["id"] == first_id
    assert db.get_appointment_by_idempotency_key("inst_002", key)["id"] == second_id


def test_public_routes_remain_unauthenticated_and_public_safe(
    auth_client: TestClient,
    assert_public_payload_safe,
):
    invite = auth_client.get("/api/public/client-invites/invite_sofia_0826")
    status = auth_client.get("/api/public/lesson-requests/req_sofia_pending_0820")
    assert invite.status_code == 200
    assert status.status_code == 200
    assert_public_payload_safe(invite.json())
    assert_public_payload_safe(status.json())


def _login(client: TestClient, email: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/coach/sign-in",
        json={"email": email, "password": password},
        headers={CSRF_HEADER_NAME: CSRF_HEADER_VALUE},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["authenticated"] is True
    assert "access_token" not in payload
    token = response.cookies.get(SESSION_COOKIE_NAME)
    client.cookies.clear()
    return {
        "Cookie": f"{SESSION_COOKIE_NAME}={token}",
        CSRF_HEADER_NAME: CSRF_HEADER_VALUE,
    }


def _seed_second_coach() -> None:
    with db.SessionLocal() as session:
        if not session.get(InstructorDB, "inst_002"):
            session.add(
                InstructorDB(
                    id="inst_002",
                    name="Coach Jamie",
                    main_address="200 East Ave, Austin, TX",
                    lat=30.274,
                    lng=-97.731,
                    work_start_time="08:00",
                    work_end_time="17:00",
                    timezone="America/Chicago",
                    business_name="Jamie Swim",
                    base_address="200 East Ave, Austin, TX",
                    base_lat=30.274,
                    base_lng=-97.731,
                    service_address="200 East Ave, Austin, TX",
                    service_lat=30.274,
                    service_lng=-97.731,
                    service_name="Swim lesson",
                    default_duration_mins=45,
                )
            )
            session.add_all(
                [
                    ProviderWorkingHoursDB(
                        instructor_id="inst_002",
                        weekday=weekday,
                        enabled=True,
                        start_time=datetime.time(8, 0),
                        end_time=datetime.time(17, 0),
                    )
                    for weekday in range(7)
                ]
            )
        if not session.get(ClientDB, "client_jamie"):
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
        if not session.get(InviteTokenDB, "invite_jamie_auth"):
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
                    expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=1),
                )
            )
        session.commit()
