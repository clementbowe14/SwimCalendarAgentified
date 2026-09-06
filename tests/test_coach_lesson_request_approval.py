from __future__ import annotations

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import db
from app.api import create_app
from app.dependencies import get_current_coach
from app.models import AppointmentDB, InviteSlotOfferDB, LessonRequestDB


SEEDED_REQUEST_TOKEN = "req_sofia_pending_0820"


def test_coach_approves_pending_request_into_scheduled_appointment(
    authenticated_api_client,
    assert_public_payload_safe,
):
    api_client = authenticated_api_client
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


def test_coach_request_approval_is_idempotent(authenticated_api_client):
    api_client = authenticated_api_client
    first = api_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/approve")
    second = api_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/approve")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["appointment_id"] == second.json()["appointment_id"]
    assert _appointment_count_for_request(SEEDED_REQUEST_TOKEN) == 1


def test_canonical_confirm_returns_confirmed_with_ordered_appointment_ids(authenticated_api_client):
    api_client = authenticated_api_client

    response = api_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/confirm", json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "confirmed"
    assert payload["appointment_id"] == payload["appointment_ids"][0]
    assert len(payload["appointment_ids"]) == 1
    assert _request_status(SEEDED_REQUEST_TOKEN) == "confirmed"


def test_coach_request_approval_conflict_does_not_confirm_request(
    authenticated_api_client,
    assert_public_payload_safe,
):
    api_client = authenticated_api_client
    _add_conflicting_appointment(SEEDED_REQUEST_TOKEN)

    approval = api_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/approve")

    assert approval.status_code == 409
    assert approval.json()["error"]["code"] == "SCHEDULING_CONFLICT"
    assert _appointment_count_for_request(SEEDED_REQUEST_TOKEN) == 0
    assert _validation_failure_code(SEEDED_REQUEST_TOKEN) == "SLOT_CONFLICT"

    public_status = api_client.get(f"/api/public/lesson-requests/{SEEDED_REQUEST_TOKEN}")
    assert public_status.status_code == 200
    assert public_status.json()["status"] == "requested"
    assert public_status.json()["confirmation"] is None
    assert_public_payload_safe(public_status.json())


def test_expired_request_actions_are_rejected(authenticated_api_client):
    api_client = authenticated_api_client
    _expire_request(SEEDED_REQUEST_TOKEN)

    for path in ("approve", "confirm", "alternatives", "decline"):
        response = api_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/{path}", json={})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "LESSON_REQUEST_EXPIRED"


def test_coach_can_request_alternatives_without_exposing_private_schedule(
    authenticated_api_client,
    assert_public_payload_safe,
):
    api_client = authenticated_api_client
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


def test_alternatives_are_request_bound_and_client_can_select_one(
    authenticated_api_client,
    assert_public_payload_safe,
):
    api_client = authenticated_api_client
    slot_ids = _create_search_offer_ids(api_client)
    request = api_client.post(
        "/api/public/client-invites/invite_sofia_0826/lesson-requests",
        json={"slot_id": slot_ids[0]},
    )
    assert request.status_code == 200
    request_token = request.json()["id"]

    alternatives = api_client.post(
        f"/api/coach/lesson-requests/{request_token}/alternatives",
        json={"slot_ids": [slot_ids[1]], "message": "Try this later opening."},
    )

    assert alternatives.status_code == 200
    public_status = api_client.get(f"/api/public/lesson-requests/{request_token}")
    assert public_status.status_code == 200
    payload = public_status.json()
    assert payload["status"] == "needs_new_times"
    assert [slot["id"] for slot in payload["alternative_slots"]]
    assert_public_payload_safe(payload)

    selection = api_client.post(
        f"/api/public/lesson-requests/{request_token}/select-alternative",
        json={"slot_id": payload["alternative_slots"][0]["id"]},
    )

    assert selection.status_code == 200
    assert selection.json()["status"] == "requested"
    assert selection.json()["slot"]["id"] == payload["alternative_slots"][0]["id"]


def test_alternative_promotion_rejects_mismatched_context(authenticated_api_client):
    api_client = authenticated_api_client
    slot_ids = _create_search_offer_ids(api_client)
    request = api_client.post(
        "/api/public/client-invites/invite_sofia_0826/lesson-requests",
        json={"slot_id": slot_ids[0]},
    )
    request_token = request.json()["id"]
    _change_offer_duration(slot_ids[1], duration_mins=60)

    alternatives = api_client.post(
        f"/api/coach/lesson-requests/{request_token}/alternatives",
        json={"slot_ids": [slot_ids[1]]},
    )

    assert alternatives.status_code == 409
    assert alternatives.json()["error"]["code"] == "ALTERNATIVE_SLOT_CONTEXT_MISMATCH"
    assert _request_status(request_token) == "pending"
    assert _alternative_count(request_token) == 0


def test_weekly_confirm_creates_multiple_appointments_and_ics_events(authenticated_api_client):
    api_client = authenticated_api_client
    slot_id = _create_weekly_offer_id(api_client)
    request = api_client.post(
        "/api/public/client-invites/invite_sofia_0826/lesson-requests",
        json={"slot_id": slot_id},
    )
    request_token = request.json()["id"]

    confirmation = api_client.post(f"/api/coach/lesson-requests/{request_token}/confirm", json={})

    assert confirmation.status_code == 200
    assert len(confirmation.json()["appointment_ids"]) == 4
    status = api_client.get(f"/api/public/lesson-requests/{request_token}")
    ics = api_client.get(status.json()["confirmation"]["calendar_links"]["ics"])
    assert ics.status_code == 200
    assert ics.text.count("BEGIN:VEVENT") == 4


def test_coach_can_decline_request_without_exposing_private_schedule(
    authenticated_api_client,
    assert_public_payload_safe,
):
    api_client = authenticated_api_client
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


def _request_status(request_token: str) -> str:
    session = db.SessionLocal()
    try:
        return session.query(LessonRequestDB).filter_by(request_token=request_token).one().status
    finally:
        session.close()


def _validation_failure_code(request_token: str) -> str | None:
    session = db.SessionLocal()
    try:
        return session.query(LessonRequestDB).filter_by(request_token=request_token).one().validation_failure_code
    finally:
        session.close()


def _expire_request(request_token: str) -> None:
    session = db.SessionLocal()
    try:
        lesson_request = session.query(LessonRequestDB).filter_by(request_token=request_token).one()
        lesson_request.expires_at = db.datetime.datetime.utcnow() - db.datetime.timedelta(minutes=1)
        session.commit()
    finally:
        session.close()


def _create_search_offer_ids(api_client) -> list[str]:
    response = api_client.post(
        "/api/public/client-invites/invite_sofia_0826/slots",
        json={
            "client_name": "Sofia Martinez",
            "location": {
                "address": "450 North Blvd, Austin, TX",
                "lat": 30.33,
                "lng": -97.72,
                "is_instructor_facility": False,
            },
            "requested_date": "2026-08-20",
            "duration_mins": 45,
            "recurrence": "single",
            "windows": [{"id": "all_day", "days": ["Thu"], "start_time": "08:00", "end_time": "18:00"}],
        },
    )
    assert response.status_code == 200
    slot_ids = [slot["id"] for slot in response.json()["slots"]]
    assert len(slot_ids) >= 2
    return slot_ids


def _create_weekly_offer_id(api_client) -> str:
    response = api_client.post(
        "/api/public/client-invites/invite_sofia_0826/slots",
        json={
            "client_name": "Sofia Martinez",
            "location": {
                "address": "450 North Blvd, Austin, TX",
                "lat": 30.33,
                "lng": -97.72,
                "is_instructor_facility": False,
            },
            "requested_date": "2026-08-21",
            "duration_mins": 45,
            "recurrence": "weekly",
            "windows": [{"id": "all_day", "days": ["Fri"], "start_time": "08:00", "end_time": "18:00"}],
        },
    )
    assert response.status_code == 200
    return response.json()["slots"][0]["id"]


def _change_offer_duration(slot_id: str, *, duration_mins: int) -> None:
    session = db.SessionLocal()
    try:
        offer = session.query(InviteSlotOfferDB).filter_by(id=slot_id).one()
        offer.duration_mins = duration_mins
        session.commit()
    finally:
        session.close()


def _alternative_count(request_token: str) -> int:
    session = db.SessionLocal()
    try:
        lesson_request = session.query(LessonRequestDB).filter_by(request_token=request_token).one()
        return session.query(InviteSlotOfferDB).filter_by(
            lesson_request_id=lesson_request.id,
            offer_kind="alternative",
        ).count()
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
