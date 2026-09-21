from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi.testclient import TestClient
import pytest

from app import db
from app.api import create_app
from app.db import SOFIA_INVITE_TOKEN
from app.models import AppointmentDB, LessonRequestDB
from tests.db_test_utils import reset_postgres_database


def test_client_invite_openapi_documents_fetch_adapter_contract():
    schema = create_app().openapi()

    expected_paths = {
        ("GET", "/api/public/client-invites/{token}"): "ClientInviteEnvelopeResponse",
        ("POST", "/api/public/client-invites/{token}/intake"): "ClientInviteEnvelopeResponse",
        ("POST", "/api/public/client-invites/{token}/slots"): "ClientInviteAvailabilityResponse",
        ("POST", "/api/public/client-invites/{token}/lesson-requests"): "ClientInviteLessonRequestResponse",
        ("GET", "/api/public/lesson-requests/{request_token}"): "ClientInviteLessonRequestResponse",
        ("GET", "/api/public/lesson-requests/{request_token}/calendar-links"): "ClientInviteCalendarLinks",
    }

    for (method, path), response_schema in expected_paths.items():
        operation = schema["paths"][path][method.lower()]
        success_response = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert success_response["$ref"].endswith(f"/{response_schema}")


@pytest.fixture()
def client():
    reset_postgres_database()
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client
    db.configure_database()


def test_client_invite_envelope_is_seeded_and_public_safe(client):
    response = client.get(f"/api/public/client-invites/{SOFIA_INVITE_TOKEN}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["invite_token"] == SOFIA_INVITE_TOKEN
    assert payload["intake"]["client_name"] == "Sofia Martinez"
    assert "instructor_id" not in _flatten_keys(payload)
    assert "client_id" not in _flatten_keys(payload)


def test_client_invite_intake_can_be_submitted(client):
    response = client.post(
        f"/api/public/client-invites/{SOFIA_INVITE_TOKEN}/intake",
        json={
            "client_name": "Sofia Martinez",
            "guardian_phone": "(512) 555-0194",
            "guardian_email": "ana.martinez@example.com",
            "notes": "Likes a warmup game.",
            "location_type": "home",
            "location": {
                "address": "450 North Blvd, Austin, TX",
                "lat": 30.33,
                "lng": -97.72,
                "is_instructor_facility": False,
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["intake"]["notes"] == "Likes a warmup game."


def test_client_invite_slots_ignore_spoofed_internal_ids_and_return_slot_ids(client):
    response = client.post(
        f"/api/public/client-invites/{SOFIA_INVITE_TOKEN}/slots",
        json={
            "invite_token": SOFIA_INVITE_TOKEN,
            "instructor_id": "inst_attacker",
            "client_id": "client_attacker",
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
    payload = response.json()
    assert payload["status"] == "PRESENTING"
    assert payload["slots"]
    assert payload["slots"][0]["id"].startswith("slot_")
    assert "inst_attacker" not in str(payload)
    assert "client_attacker" not in str(payload)
    assert "instructor_id" not in _flatten_keys(payload)
    assert "client_id" not in _flatten_keys(payload)


def test_parallel_slot_searches_reuse_server_issued_offer_ids(client):
    request = _slot_search_request()

    def search_slots() -> dict[str, Any]:
        response = client.post(f"/api/public/client-invites/{SOFIA_INVITE_TOKEN}/slots", json=request)
        assert response.status_code == 200
        return response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.map(lambda _: search_slots(), range(2))

    assert first["slots"]
    assert second["slots"]
    assert [slot["id"] for slot in first["slots"]] == [slot["id"] for slot in second["slots"]]


def test_client_invite_lesson_request_uses_server_issued_slot_offer(client):
    slot_response = _create_slot_offer(client)
    slot_id = slot_response["slots"][0]["id"]

    response = client.post(
        f"/api/public/client-invites/{SOFIA_INVITE_TOKEN}/lesson-requests",
        json={"slot_id": slot_id},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"].startswith("req_")
    assert payload["status"] == "requested"
    assert payload["slot"]["id"] == slot_id
    assert payload["slot"]["start_iso"].endswith("-05:00")
    assert payload["confirmation"] is None

    status_response = client.get(f"/api/public/lesson-requests/{payload['id']}")
    assert status_response.status_code == 200
    assert status_response.json()["id"] == payload["id"]


def test_unknown_slot_offer_returns_stable_public_error(client):
    response = client.post(
        f"/api/public/client-invites/{SOFIA_INVITE_TOKEN}/lesson-requests",
        json={"slot_id": "slot_not_real"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SLOT_OFFER_NOT_FOUND"


def test_unknown_lesson_request_returns_stable_public_error(client):
    response = client.get("/api/public/lesson-requests/req_not_real")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "LESSON_REQUEST_NOT_FOUND"


def test_calendar_links_are_unavailable_until_request_is_confirmed(client):
    slot_response = _create_slot_offer(client)
    request_response = client.post(
        f"/api/public/client-invites/{SOFIA_INVITE_TOKEN}/lesson-requests",
        json={"slot_id": slot_response["slots"][0]["id"]},
    )
    request_token = request_response.json()["id"]

    response = client.get(f"/api/public/lesson-requests/{request_token}/calendar-links")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CALENDAR_LINKS_UNAVAILABLE"


def test_confirmed_request_returns_public_calendar_links(client):
    slot_response = _create_slot_offer(client)
    request_response = client.post(
        f"/api/public/client-invites/{SOFIA_INVITE_TOKEN}/lesson-requests",
        json={"slot_id": slot_response["slots"][0]["id"]},
    )
    request_token = request_response.json()["id"]
    _mark_request_confirmed(request_token)

    response = client.get(f"/api/public/lesson-requests/{request_token}/calendar-links")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == {"google", "apple", "ics", "google_events"}
    assert request_token in payload["ics"]
    assert "app_" not in payload["google"]
    from urllib.parse import parse_qs, urlsplit
    dates = parse_qs(urlsplit(payload["google"]).query)["dates"][0]
    assert all(value.endswith("Z") for value in dates.split("/"))
    assert payload["google_events"][0]["url"] == payload["google"]

    ics_response = client.get(payload["ics"])
    assert ics_response.status_code == 200
    assert "DTSTART:" in ics_response.text
    assert "Z\r\n" in ics_response.text


def _create_slot_offer(client: TestClient) -> dict[str, Any]:
    response = client.post(
        f"/api/public/client-invites/{SOFIA_INVITE_TOKEN}/slots",
        json=_slot_search_request(),
    )
    assert response.status_code == 200
    return response.json()


def _slot_search_request() -> dict[str, Any]:
    return {
        "invite_token": SOFIA_INVITE_TOKEN,
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
    }


def _mark_request_confirmed(request_token: str) -> None:
    session = db.SessionLocal()
    try:
        lesson_request = session.query(LessonRequestDB).filter_by(request_token=request_token).one()
        lesson_request.status = "confirmed"
        appointment = AppointmentDB(
            id="app_confirmed_private",
            instructor_id=lesson_request.instructor_id,
            client_id="client_sofia",
            request_id=lesson_request.id,
            title="Lesson with Sofia Martinez",
            start_time=lesson_request.slot_start,
            end_time=lesson_request.slot_end,
            location_address=lesson_request.client_address,
            lat=lesson_request.lat,
            lng=lesson_request.lng,
            is_break=False,
            event_type="lesson",
            status="scheduled",
        )
        session.add(appointment)
        session.commit()
    finally:
        session.close()


def _flatten_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.add(key)
            keys.update(_flatten_keys(nested))
    elif isinstance(value, list):
        for item in value:
            keys.update(_flatten_keys(item))
    return keys
