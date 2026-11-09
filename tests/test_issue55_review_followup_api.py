from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient

from app import db, timezones
from app.models import (
    AppointmentDB,
    InstructorDB,
    InviteIntakeDB,
    LessonRequestDB,
    ProviderWorkingHoursDB,
)
from app.public_tokens import DEMO_INVITE_TOKEN


INSTRUCTOR_ID = "inst_001"
SOFIA_INVITE_TOKEN = "invite_sofia_0826"
INVALID_TIMEZONE = "Not/A_Real_Zone"


def _parse_json_datetime(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _set_invalid_provider_timezone() -> None:
    with db.SessionLocal() as session:
        instructor = session.get(InstructorDB, INSTRUCTOR_ID)
        assert instructor is not None
        instructor.timezone = INVALID_TIMEZONE
        session.commit()


def test_schedule_profile_projects_only_explicit_provider_configuration(
    authenticated_api_client: TestClient,
):
    with db.SessionLocal() as session:
        instructor = session.get(InstructorDB, INSTRUCTOR_ID)
        assert instructor is not None
        instructor.name = "Explicit Provider"
        instructor.main_address = "Legacy address must not appear"
        instructor.lat = 11.11
        instructor.lng = -22.22
        instructor.work_start_time = "01:00"
        instructor.work_end_time = "02:00"
        instructor.timezone = INVALID_TIMEZONE
        instructor.service_address = None
        instructor.service_lat = None
        instructor.service_lng = None
        for hours in session.query(ProviderWorkingHoursDB).filter_by(
            instructor_id=INSTRUCTOR_ID
        ):
            hours.enabled = False
            hours.start_time = None
            hours.end_time = None
        session.commit()

    response = authenticated_api_client.get(
        "/api/coach/schedule",
        params={"date": "2026-08-20"},
    )

    assert response.status_code == 200, response.text
    instructor_payload = response.json()["instructor"]
    assert instructor_payload == {
        "id": INSTRUCTOR_ID,
        "name": "Explicit Provider",
        "main_address": "",
        "lat": 0.0,
        "lng": 0.0,
        "work_start_time": "",
        "work_end_time": "",
        "timezone": INVALID_TIMEZONE,
    }


def test_fresh_invite_uses_home_prompt_and_projects_current_facility(
    api_client: TestClient,
):
    with db.SessionLocal() as session:
        session.query(InviteIntakeDB).filter_by(
            invite_token=DEMO_INVITE_TOKEN
        ).delete()
        instructor = session.get(InstructorDB, INSTRUCTOR_ID)
        assert instructor is not None
        service = {
            "address": instructor.service_address,
            "lat": instructor.service_lat,
            "lng": instructor.service_lng,
        }
        session.commit()

    legacy_invite = api_client.get(f"/api/public/invites/{DEMO_INVITE_TOKEN}")
    envelope = api_client.get(f"/api/public/client-invites/{DEMO_INVITE_TOKEN}")

    assert legacy_invite.status_code == 200, legacy_invite.text
    assert legacy_invite.json()["location_label"] == "Client pool"
    assert envelope.status_code == 200, envelope.text
    payload = envelope.json()
    assert payload["service_name"] == "Swim lesson"
    assert payload["intake"]["location_type"] == "home"
    assert payload["intake"]["location"] == {
        "address": "Enter home pool address",
        "lat": None,
        "lng": None,
        "is_instructor_facility": False,
    }
    assert payload["intake"]["location"]["address"] != payload["service_name"]

    unchanged_prompt = api_client.post(
        f"/api/public/client-invites/{DEMO_INVITE_TOKEN}/intake",
        json=payload["intake"],
    )
    assert unchanged_prompt.status_code == 400, unchanged_prompt.text
    assert unchanged_prompt.json()["error"] == {
        "code": "INVALID_SCHEDULING_REQUEST",
        "message": "Enter a home pool address.",
    }

    facility = api_client.post(
        f"/api/public/client-invites/{DEMO_INVITE_TOKEN}/intake",
        json={
            "client_name": "Facility client",
            "guardian_phone": "",
            "guardian_email": "",
            "notes": "",
            "location_type": "facility",
            "location": {
                "address": "Client supplied facility",
                "lat": 1.0,
                "lng": 2.0,
                "is_instructor_facility": True,
            },
        },
    )

    assert facility.status_code == 200, facility.text
    assert facility.json()["intake"]["location"] == {
        **service,
        "is_instructor_facility": True,
    }


def test_invalid_timezone_rejects_new_timezone_aware_public_request_generically(
    api_client: TestClient,
):
    _set_invalid_provider_timezone()

    response = api_client.post(
        f"/api/public/invites/{DEMO_INVITE_TOKEN}/requests",
        json={
            "client_name": "Aware public request",
            "client_address": "55 Home Pool Rd",
            "requested_date": "2027-01-05",
            "slot_start": "2027-01-05T16:00:00Z",
            "duration_mins": 45,
            "location_type": "home",
            "idempotency_key": "followup-public-new-001",
        },
    )

    assert response.status_code == 409, response.text
    assert response.json() == {
        "error": {
            "code": "PROVIDER_SETUP_REQUIRED",
            "message": "Scheduling is temporarily unavailable.",
        }
    }


def test_invalid_timezone_rejects_new_timezone_aware_coach_booking_privately(
    authenticated_api_client: TestClient,
):
    _set_invalid_provider_timezone()

    response = authenticated_api_client.post(
        "/api/coach/bookings",
        json={
            "client_name": "Aware coach booking",
            "client_address": "55 Home Pool Rd",
            "start_time": "2027-01-05T16:00:00Z",
            "duration_mins": 45,
            "is_instructor_facility": False,
            "idempotency_key": "followup-coach-new-001",
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"] == {
        "code": "PROVIDER_SETUP_REQUIRED",
        "message": "Complete provider settings before scheduling.",
        "details": {"missing_fields": ["timezone"]},
    }


def test_invalid_timezone_keeps_committed_coach_idempotent_replay_readable(
    authenticated_api_client: TestClient,
):
    start = dt.datetime(2027, 1, 5, 16, 0)
    with db.SessionLocal() as session:
        instructor = session.get(InstructorDB, INSTRUCTOR_ID)
        assert instructor is not None
        instructor.timezone = INVALID_TIMEZONE
        session.add(
            AppointmentDB(
                id="app_followup_coach_replay",
                instructor_id=INSTRUCTOR_ID,
                client_id="client_replay_client",
                title="Lesson with Replay Client",
                start_time=start,
                end_time=start + dt.timedelta(minutes=45),
                location_address="55 Home Pool Rd",
                lat=30.55,
                lng=-97.55,
                is_break=False,
                event_type="lesson",
                status="scheduled",
                idempotency_key="followup-coach-replay-001",
            )
        )
        session.commit()

    response = authenticated_api_client.post(
        "/api/coach/bookings",
        json={
            "client_name": "Replay Client",
            "client_address": "55 Home Pool Rd",
            "start_time": "2027-01-05T16:00:00Z",
            "duration_mins": 45,
            "is_instructor_facility": False,
            "idempotency_key": "followup-coach-replay-001",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["appointment_id"] == "app_followup_coach_replay"
    assert "Jan 05, 2027 at 04:00 PM UTC" in response.json()["message"]
    assert "America/Chicago" not in response.text


def test_invalid_timezone_keeps_committed_public_idempotent_replay_readable(
    api_client: TestClient,
):
    start = dt.datetime(2027, 1, 5, 16, 0)
    with db.SessionLocal() as session:
        instructor = session.get(InstructorDB, INSTRUCTOR_ID)
        assert instructor is not None
        instructor.timezone = INVALID_TIMEZONE
        session.add(
            LessonRequestDB(
                id="lr_followup_public_replay",
                request_token="req_followup_public_replay",
                invite_token=DEMO_INVITE_TOKEN,
                instructor_id=INSTRUCTOR_ID,
                client_name="Public Replay",
                client_address="55 Home Pool Rd",
                requested_date=dt.date(2027, 1, 5),
                slot_start=start,
                slot_end=start + dt.timedelta(minutes=45),
                duration_mins=45,
                travel_time_mins=12,
                buffer_mins=10,
                distance_miles=3.4,
                location_type="home",
                lat=30.55,
                lng=-97.55,
                status="pending",
                recurrence="single",
                recurrence_count=1,
                idempotency_key="followup-public-replay-001",
                expires_at=dt.datetime.utcnow() + dt.timedelta(days=1),
            )
        )
        session.commit()

    response = api_client.post(
        f"/api/public/invites/{DEMO_INVITE_TOKEN}/requests",
        json={
            "client_name": "Public Replay",
            "client_address": "55 Home Pool Rd",
            "requested_date": "2027-01-05",
            "slot_start": "2027-01-05T16:00:00Z",
            "duration_mins": 45,
            "location_type": "home",
            "idempotency_key": "followup-public-replay-001",
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["request_token"] == "req_followup_public_replay"
    assert _parse_json_datetime(payload["selected_slot"]["start_iso"]) == dt.datetime(
        2027,
        1,
        5,
        16,
        0,
        tzinfo=dt.timezone.utc,
    )
    assert "UTC" in payload["selected_slot"]["display"]
    assert "America/Chicago" not in response.text


def test_invalid_timezone_formats_existing_request_responses_in_explicit_utc(
    authenticated_api_client: TestClient,
):
    with db.SessionLocal() as session:
        request = session.get(LessonRequestDB, "req_sofia_pending_0820")
        assert request is not None
        expected_start = timezones.storage_to_aware_utc(request.slot_start)
        instructor = session.get(InstructorDB, INSTRUCTOR_ID)
        assert instructor is not None
        instructor.timezone = INVALID_TIMEZONE
        session.commit()

    snapshot = authenticated_api_client.get(
        "/api/coach/command-center/snapshot",
        params={"date": "2026-08-20"},
    )
    public_status = authenticated_api_client.get(
        "/api/public/lesson-requests/req_sofia_pending_0820"
    )

    assert snapshot.status_code == 200, snapshot.text
    request_payload = next(
        item
        for item in snapshot.json()["requests"]
        if item["id"] == "req_sofia_pending_0820"
    )
    assert _parse_json_datetime(
        request_payload["requested_slot"]["start_time"]
    ) == expected_start
    assert public_status.status_code == 200, public_status.text
    assert _parse_json_datetime(public_status.json()["slot"]["start_iso"]) == expected_start
    assert "UTC" in public_status.json()["slot"]["display"]

