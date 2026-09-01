from fastapi.testclient import TestClient
import datetime
import pytest

from app import db
from app.api import create_app
from app.public_tokens import DEMO_INVITE_TOKEN
from tests.conftest import login_coach
from tests.db_test_utils import reset_postgres_database


@pytest.fixture()
def client():
    reset_postgres_database()
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        test_client.headers.update(login_coach(test_client))
        yield test_client
    db.configure_database()


def test_health_endpoint(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_coach_can_search_availability(client):
    response = client.post(
        "/api/coach/availability",
        json={
            "client_name": "Charlie",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-20",
            "duration_mins": 60,
            "is_instructor_facility": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "PRESENTING"
    assert payload["client_name"] == "Charlie"
    assert payload["available_slots"]
    assert payload["available_slots"][0]["display"]
    assert {"travel_time_mins", "buffer_mins", "distance_miles"}.issubset(
        payload["available_slots"][0].keys()
    )
    assert payload["available_slots"][0]["start_iso"].endswith("-05:00")


def test_coach_schedule_returns_local_aware_cross_midnight_events(client):
    db.save_appointment(
        instructor_id="inst_001",
        client_id="client_late_api",
        title="Lesson with Late API",
        start_time=datetime.datetime.fromisoformat("2026-08-21T23:30:00-05:00"),
        end_time=datetime.datetime.fromisoformat("2026-08-22T00:30:00-05:00"),
        location_address="450 North Blvd, Austin, TX",
        lat=30.3300,
        lng=-97.7200,
    )

    response = client.get("/api/coach/schedule?date=2026-08-22")

    assert response.status_code == 200
    appointment = next(item for item in response.json()["appointments"] if item["title"] == "Lesson with Late API")
    assert appointment["start_time"] == "2026-08-21T23:30:00-05:00"
    assert appointment["end_time"] == "2026-08-22T00:30:00-05:00"


def test_availability_skips_nonexistent_spring_forward_local_hour(client):
    session = db.SessionLocal()
    try:
        instructor = session.query(db.InstructorDB).filter_by(id="inst_001").one()
        instructor.work_start_time = "01:00"
        instructor.work_end_time = "04:00"
        session.commit()
    finally:
        session.close()

    response = client.post(
        "/api/coach/availability",
        json={
            "client_name": "Charlie",
            "client_address": "100 Main St, Austin, TX",
            "requested_date": "2026-03-08",
            "duration_mins": 60,
            "is_instructor_facility": True,
        },
    )

    assert response.status_code == 200
    starts = [slot["start_iso"] for slot in response.json()["available_slots"]]
    assert starts
    assert not any("T02:" in start for start in starts)
    assert any(start.endswith("-05:00") for start in starts)


def test_public_invite_is_token_scoped(client):
    response = client.get(f"/api/public/invites/{DEMO_INVITE_TOKEN}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["invite_token"] == DEMO_INVITE_TOKEN
    assert "instructor_id" not in payload
    assert "client_id" not in payload


def test_public_invite_availability_does_not_require_internal_ids(client):
    response = client.post(
        f"/api/public/invites/{DEMO_INVITE_TOKEN}/availability",
        json={
            "client_name": "Sofia Martinez",
            "client_address": "800 West Lake Dr, Austin, TX",
            "requested_date": "2026-08-20",
            "location_type": "home",
            "duration_mins": 45,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "PRESENTING"
    assert payload["available_slots"]
    assert "instructor_id" not in payload
    assert "client_id" not in payload


def test_invalid_public_token_returns_stable_error(client):
    response = client.get("/api/public/invites/not-a-real-token")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "INVITE_NOT_FOUND",
            "message": "The invite could not be found.",
        }
    }


def test_validation_failure_returns_stable_error_code(client):
    response = client.post(
        "/api/coach/availability",
        json={
            "client_name": "",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-20",
            "duration_mins": 17,
        },
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    assert payload["error"]["message"] == "Request validation failed."
    assert "fields" in payload["error"]["details"]


def test_unavailable_booking_returns_scheduling_conflict(client):
    response = client.post(
        "/api/coach/bookings",
        json={
            "client_name": "Charlie",
            "client_address": "450 North Blvd, Austin, TX",
            "start_time": "2026-08-20T09:30:00",
            "duration_mins": 60,
            "is_instructor_facility": False,
        },
    )

    assert response.status_code == 409
    payload = response.json()
    assert payload["error"]["code"] == "SCHEDULING_CONFLICT"
    assert payload["error"]["message"] == "The requested time is unavailable."


def test_ambiguous_fall_back_booking_time_returns_validation_error(client):
    response = client.post(
        "/api/coach/bookings",
        json={
            "client_name": "Charlie",
            "client_address": "450 North Blvd, Austin, TX",
            "start_time": "2026-11-01T01:30:00",
            "duration_mins": 60,
            "is_instructor_facility": False,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_SCHEDULING_REQUEST"


def test_public_lesson_request_creates_pending_request(client):
    availability = client.post(
        f"/api/public/invites/{DEMO_INVITE_TOKEN}/availability",
        json={
            "client_name": "Sofia Martinez",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-20",
            "location_type": "home",
            "duration_mins": 45,
        },
    )
    slot_start = availability.json()["available_slots"][0]["start_iso"]

    response = client.post(
        f"/api/public/invites/{DEMO_INVITE_TOKEN}/requests",
        json={
            "client_name": "Sofia Martinez",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-20",
            "slot_start": slot_start,
            "duration_mins": 45,
            "location_type": "home",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "pending"
    assert payload["request_token"].startswith("req_")
    assert payload["selected_slot"]["start_iso"] == slot_start


def test_coach_booking_retry_with_idempotency_key_returns_same_appointment(client):
    availability = client.post(
        "/api/coach/availability",
        json={
            "client_name": "Charlie",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-21",
            "duration_mins": 60,
            "is_instructor_facility": False,
        },
    )
    slot_start = availability.json()["available_slots"][0]["start_iso"]
    payload = {
        "client_name": "Charlie",
        "client_address": "450 North Blvd, Austin, TX",
        "start_time": slot_start,
        "duration_mins": 60,
        "is_instructor_facility": False,
        "idempotency_key": "booking-api-key-123",
    }

    first = client.post("/api/coach/bookings", json=payload)
    second = client.post("/api/coach/bookings", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["appointment_id"] == second.json()["appointment_id"]


def test_public_request_retry_with_idempotency_key_returns_same_request(client):
    availability = client.post(
        f"/api/public/invites/{DEMO_INVITE_TOKEN}/availability",
        json={
            "client_name": "Sofia Martinez",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-21",
            "location_type": "home",
            "duration_mins": 45,
        },
    )
    slot_start = availability.json()["available_slots"][0]["start_iso"]
    payload = {
        "client_name": "Sofia Martinez",
        "client_address": "450 North Blvd, Austin, TX",
        "requested_date": "2026-08-21",
        "slot_start": slot_start,
        "duration_mins": 45,
        "location_type": "home",
        "idempotency_key": "request-api-key-123",
    }

    first = client.post(f"/api/public/invites/{DEMO_INVITE_TOKEN}/requests", json=payload)
    second = client.post(f"/api/public/invites/{DEMO_INVITE_TOKEN}/requests", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["request_token"] == second.json()["request_token"]
