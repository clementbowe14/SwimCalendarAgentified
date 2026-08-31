from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import db
from app.api import create_app
from app.dependencies import get_current_coach
from app.models import AppointmentDB, LessonRequestDB


SEEDED_REQUEST_TOKEN = "req_sofia_pending_0820"


def test_coach_approves_pending_request_into_scheduled_appointment(
    api_client,
    assert_public_payload_safe,
):
    locked_links = api_client.get(f"/api/public/lesson-requests/{SEEDED_REQUEST_TOKEN}/calendar-links")
    assert locked_links.status_code == 409
    assert locked_links.json()["error"]["code"] == "CALENDAR_LINKS_UNAVAILABLE"

    approval = api_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/approve")

    assert approval.status_code == 200
    approval_payload = approval.json()
    assert approval_payload["id"] == SEEDED_REQUEST_TOKEN
    assert approval_payload["status"] == "approved"
    assert approval_payload["appointment_id"].startswith("app_")

    schedule = api_client.get("/api/coach/schedule", params={"date": "2026-08-20"})
    assert approval_payload["appointment_id"] in {
        appointment["id"] for appointment in schedule.json()["appointments"]
    }

    public_status = api_client.get(f"/api/public/lesson-requests/{SEEDED_REQUEST_TOKEN}")
    assert public_status.status_code == 200
    assert public_status.json()["status"] == "confirmed"
    assert public_status.json()["confirmation"]["calendar_links"]["ics"].endswith(
        f"/calendar/{SEEDED_REQUEST_TOKEN}.ics"
    )
    assert_public_payload_safe(public_status.json())

    legacy_status = api_client.get(f"/api/public/requests/{SEEDED_REQUEST_TOKEN}")
    assert legacy_status.status_code == 200
    assert legacy_status.json()["status"] == "confirmed"
    assert_public_payload_safe(legacy_status.json())

    calendar_links = api_client.get(f"/api/public/lesson-requests/{SEEDED_REQUEST_TOKEN}/calendar-links")
    assert calendar_links.status_code == 200
    assert set(calendar_links.json()) == {"google", "apple", "ics"}


def test_coach_request_approval_is_idempotent(api_client):
    first = api_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/approve")
    second = api_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/approve")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["appointment_id"] == second.json()["appointment_id"]
    assert _appointment_count_for_request(SEEDED_REQUEST_TOKEN) == 1


def test_coach_request_approval_conflict_does_not_confirm_request(
    api_client,
    assert_public_payload_safe,
):
    _add_conflicting_appointment(SEEDED_REQUEST_TOKEN)

    approval = api_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/approve")

    assert approval.status_code == 409
    assert approval.json()["error"]["code"] == "SCHEDULING_CONFLICT"
    assert _appointment_count_for_request(SEEDED_REQUEST_TOKEN) == 0

    public_status = api_client.get(f"/api/public/lesson-requests/{SEEDED_REQUEST_TOKEN}")
    assert public_status.status_code == 200
    assert public_status.json()["status"] == "requested"
    assert public_status.json()["confirmation"] is None
    assert_public_payload_safe(public_status.json())


def test_coach_can_request_alternatives_without_exposing_private_schedule(
    api_client,
    assert_public_payload_safe,
):
    alternatives = api_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/alternatives")

    assert alternatives.status_code == 200
    assert alternatives.json() == {
        "id": SEEDED_REQUEST_TOKEN,
        "status": "needs_alternatives",
        "appointment_id": None,
        "message": "Client should choose new lesson times.",
    }

    public_status = api_client.get(f"/api/public/lesson-requests/{SEEDED_REQUEST_TOKEN}")
    assert public_status.status_code == 200
    assert public_status.json()["status"] == "needs_new_times"
    assert public_status.json()["confirmation"] is None
    assert_public_payload_safe(public_status.json())

    legacy_status = api_client.get(f"/api/public/requests/{SEEDED_REQUEST_TOKEN}")
    assert legacy_status.status_code == 200
    assert legacy_status.json()["status"] == "needs_new_times"
    assert_public_payload_safe(legacy_status.json())

    calendar_links = api_client.get(f"/api/public/lesson-requests/{SEEDED_REQUEST_TOKEN}/calendar-links")
    assert calendar_links.status_code == 409


def test_coach_can_decline_request_without_exposing_private_schedule(
    api_client,
    assert_public_payload_safe,
):
    decline = api_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/decline")

    assert decline.status_code == 200
    assert decline.json() == {
        "id": SEEDED_REQUEST_TOKEN,
        "status": "declined",
        "appointment_id": None,
        "message": "Lesson request was declined.",
    }

    public_status = api_client.get(f"/api/public/lesson-requests/{SEEDED_REQUEST_TOKEN}")
    assert public_status.status_code == 200
    assert public_status.json()["status"] == "needs_new_times"
    assert public_status.json()["confirmation"] is None
    assert_public_payload_safe(public_status.json())


def test_coach_lesson_request_action_routes_require_coach_dependency():
    app = create_app()

    def reject_coach():
        raise HTTPException(status_code=401, detail="coach required")

    app.dependency_overrides[get_current_coach] = reject_coach
    test_client = TestClient(app, raise_server_exceptions=False)
    try:
        responses = [
            test_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/approve"),
            test_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/alternatives"),
            test_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/decline"),
        ]
    finally:
        test_client.close()

    assert {response.status_code for response in responses} == {401}


def _appointment_count_for_request(request_token: str) -> int:
    session = db.SessionLocal()
    try:
        lesson_request = session.query(LessonRequestDB).filter_by(request_token=request_token).one()
        return session.query(AppointmentDB).filter_by(request_id=lesson_request.id).count()
    finally:
        session.close()


def _add_conflicting_appointment(request_token: str) -> None:
    session = db.SessionLocal()
    try:
        lesson_request = session.query(LessonRequestDB).filter_by(request_token=request_token).one()
        session.add(
            AppointmentDB(
                id="app_request_conflict",
                instructor_id=lesson_request.instructor_id,
                client_id="client_conflict",
                title="Existing lesson",
                start_time=lesson_request.slot_start,
                end_time=lesson_request.slot_end,
                location_address=lesson_request.client_address,
                lat=lesson_request.lat,
                lng=lesson_request.lng,
                is_break=False,
                event_type="lesson",
                status="scheduled",
            )
        )
        session.commit()
    finally:
        session.close()
