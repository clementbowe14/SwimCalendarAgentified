import pytest

from app.db import SOFIA_INVITE_TOKEN
from app.public_tokens import DEMO_INVITE_TOKEN


pytestmark = pytest.mark.integration


def test_instructor_command_workflow_uses_real_backend_data(api_client):
    coach = api_client.get("/api/coach/me")
    assert coach.status_code == 200
    assert coach.json() == {"instructor_id": "inst_001", "display_name": "Coach Sarah"}

    schedule = api_client.get("/api/coach/schedule", params={"date": "2026-08-20"})
    assert schedule.status_code == 200
    schedule_payload = schedule.json()
    assert schedule_payload["instructor"]["id"] == "inst_001"
    assert len(schedule_payload["appointments"]) == 3

    availability = api_client.post(
        "/api/coach/availability",
        json={
            "client_name": "Command App Client",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-21",
            "duration_mins": 60,
            "is_instructor_facility": False,
        },
    )
    assert availability.status_code == 200
    availability_payload = availability.json()
    assert availability_payload["status"] == "PRESENTING"
    assert availability_payload["available_slots"]

    booking_payload = {
        "client_name": "Command App Client",
        "client_address": "450 North Blvd, Austin, TX",
        "start_time": availability_payload["available_slots"][0]["start_iso"],
        "duration_mins": 60,
        "is_instructor_facility": False,
        "idempotency_key": "integration-command-booking-123",
    }
    booking = api_client.post("/api/coach/bookings", json=booking_payload)
    retry = api_client.post("/api/coach/bookings", json=booking_payload)

    assert booking.status_code == 200
    assert retry.status_code == 200
    assert booking.json()["appointment_id"] == retry.json()["appointment_id"]

    updated_schedule = api_client.get("/api/coach/schedule", params={"date": "2026-08-21"})
    assert updated_schedule.status_code == 200
    appointment_ids = {appointment["id"] for appointment in updated_schedule.json()["appointments"]}
    assert booking.json()["appointment_id"] in appointment_ids


def test_client_invite_workflow_is_token_scoped_and_idempotent(
    api_client,
    assert_public_payload_safe,
):
    envelope = api_client.get(f"/api/public/client-invites/{SOFIA_INVITE_TOKEN}")
    assert envelope.status_code == 200
    assert_public_payload_safe(envelope.json())

    intake = api_client.post(
        f"/api/public/client-invites/{SOFIA_INVITE_TOKEN}/intake",
        json={
            "client_name": "Sofia Martinez",
            "guardian_phone": "(512) 555-0194",
            "guardian_email": "ana.martinez@example.com",
            "notes": "Frontend integration flow.",
            "location_type": "home",
            "location": {
                "address": "450 North Blvd, Austin, TX",
                "lat": 30.33,
                "lng": -97.72,
                "is_instructor_facility": False,
            },
        },
    )
    assert intake.status_code == 200
    assert intake.json()["intake"]["notes"] == "Frontend integration flow."
    assert_public_payload_safe(intake.json())

    slots = api_client.post(
        f"/api/public/client-invites/{SOFIA_INVITE_TOKEN}/slots",
        json={
            "invite_token": SOFIA_INVITE_TOKEN,
            "instructor_id": "inst_should_be_ignored",
            "client_id": "client_should_be_ignored",
            "client_name": "Sofia Martinez",
            "location": {
                "address": "450 North Blvd, Austin, TX",
                "lat": 30.33,
                "lng": -97.72,
                "is_instructor_facility": False,
            },
            "requested_date": "2026-08-21",
            "duration_mins": 45,
            "recurrence": "single",
            "windows": [{"id": "fri", "days": ["Fri"], "start_time": "08:00", "end_time": "18:00"}],
        },
    )
    assert slots.status_code == 200
    slot_payload = slots.json()
    assert slot_payload["status"] == "PRESENTING"
    assert slot_payload["slots"]
    assert_public_payload_safe(slot_payload)

    request_payload = {
        "slot_id": slot_payload["slots"][0]["id"],
        "idempotency_key": "integration-client-request-123",
    }
    created = api_client.post(
        f"/api/public/client-invites/{SOFIA_INVITE_TOKEN}/lesson-requests",
        json=request_payload,
    )
    retry = api_client.post(
        f"/api/public/client-invites/{SOFIA_INVITE_TOKEN}/lesson-requests",
        json=request_payload,
    )

    assert created.status_code == 200
    assert retry.status_code == 200
    assert created.json()["id"] == retry.json()["id"]
    assert created.json()["status"] == "requested"
    assert_public_payload_safe(created.json())

    status = api_client.get(f"/api/public/lesson-requests/{created.json()['id']}")
    assert status.status_code == 200
    assert status.json()["id"] == created.json()["id"]
    assert_public_payload_safe(status.json())

    calendar_links = api_client.get(f"/api/public/lesson-requests/{created.json()['id']}/calendar-links")
    assert calendar_links.status_code == 409
    assert calendar_links.json()["error"]["code"] == "CALENDAR_LINKS_UNAVAILABLE"


def test_legacy_public_request_status_uses_lesson_request_not_found_code(api_client):
    response = api_client.get("/api/public/requests/req_not_real")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "LESSON_REQUEST_NOT_FOUND",
            "message": "The lesson request could not be found.",
        }
    }


def test_cors_preflight_allows_local_frontend_origins(api_client):
    response = api_client.options(
        "/api/coach/me",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_public_invite_flow_uses_legacy_contract_without_internal_ids(api_client, assert_public_payload_safe):
    invite = api_client.get(f"/api/public/invites/{DEMO_INVITE_TOKEN}")
    assert invite.status_code == 200
    assert_public_payload_safe(invite.json())

    availability = api_client.post(
        f"/api/public/invites/{DEMO_INVITE_TOKEN}/availability",
        json={
            "client_name": "Legacy Invite Client",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-21",
            "location_type": "home",
            "duration_mins": 45,
        },
    )
    assert availability.status_code == 200
    assert availability.json()["available_slots"]
    assert_public_payload_safe(availability.json())
