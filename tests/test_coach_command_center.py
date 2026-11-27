from datetime import datetime

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app import api_services, db
from app.api import create_app
from app.dependencies import get_current_coach
from app.models import InstructorDB, SchedulerSettingsDB
from tests.conftest import login_coach
from tests.db_test_utils import reset_postgres_database


@pytest.fixture()
def client():
    reset_postgres_database()
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        test_client.headers.update(login_coach(test_client))
        yield test_client
    db.configure_database()


def test_coach_command_center_snapshot_includes_backend_operational_data(client):
    response = client.get("/api/coach/command-center/snapshot?date=2026-08-20")

    assert response.status_code == 200
    payload = response.json()
    assert payload["instructor"]["id"] == "inst_001"
    assert payload["instructor"]["business_name"] == "Swim Command"
    assert payload["date"] == "2026-08-20"
    assert {client["id"] for client in payload["clients"]} >= {
        "client_alice",
        "client_bob",
        "client_sofia",
    }
    assert payload["schedule"][0]["kind"] == "lesson"
    assert payload["schedule"][0]["client_id"] == "client_alice"
    assert payload["requests"][0]["id"] == "req_sofia_pending_0820"
    assert payload["requests"][0]["requested_slot"]["target_location"]["address"] == "820 Lakeview Dr, Austin, TX"
    assert payload["rules"]["payments"]["enabled_methods"] == ["card", "ach", "cash"]
    assert payload["scheduling_policy"]["revision"] == 0
    assert payload["scheduling_policy"]["distance_buffers_enabled"] is True


def test_snapshot_uses_profile_timezone_for_appointment_selection(client, monkeypatch):
    appointment_id = db.save_appointment(
        instructor_id="inst_001",
        client_id="client_alice",
        title="Late appointment",
        start_time=datetime.fromisoformat("2026-08-28T04:30:00+00:00"),
        end_time=datetime.fromisoformat("2026-08-28T04:50:00+00:00"),
        location_address="450 North Blvd, Austin, TX",
        lat=30.3300,
        lng=-97.7200,
    )
    load_configuration = api_services.load_provider_configuration

    def load_then_change_timezone(instructor_id):
        configuration = load_configuration(instructor_id)
        with db.SessionLocal.begin() as session:
            session.get(InstructorDB, instructor_id).timezone = "America/New_York"
        return configuration

    monkeypatch.setattr(api_services, "load_provider_configuration", load_then_change_timezone)
    response = client.get("/api/coach/command-center/snapshot?date=2026-08-27")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["instructor"]["timezone"] == "America/Chicago"
    appointment = next(item for item in payload["schedule"] if item["id"] == appointment_id)
    assert appointment["start_time"] == "2026-08-27T23:30:00-05:00"


def test_legacy_scheduler_settings_are_read_only(client):
    initial = client.get("/api/coach/settings")
    assert initial.status_code == 200

    update = client.put("/api/coach/settings", json=initial.json())
    reread = client.get("/api/coach/settings")

    assert update.status_code == 410
    assert update.json()["error"]["code"] == "SCHEDULING_POLICY_ENDPOINT_MOVED"
    assert reread.status_code == 200
    assert reread.json() == initial.json()


def test_persisted_buffer_toggle_changes_real_availability(client):
    request = {
        "client_name": "Charlie",
        "client_address": "450 North Blvd, Austin, TX",
        "requested_date": "2026-08-20",
        "duration_mins": 60,
        "is_instructor_facility": False,
    }
    enabled = client.post("/api/coach/availability", json=request)
    assert enabled.status_code == 200
    enabled_slots = enabled.json()["available_slots"]
    assert any(slot["buffer_mins"] > 0 for slot in enabled_slots)
    assert enabled_slots[0]["start_iso"] == "2026-08-20T10:15:00-05:00"

    policy = client.get("/api/coach/scheduling-policy").json()
    update = {
        "expected_revision": policy["revision"],
        "distance_buffers_enabled": False,
        "post_break_recovery_mins": policy["post_break_recovery_mins"],
        "distance_buffer_tiers": [
            {"id": tier["id"], "buffer_mins": tier["buffer_mins"]}
            for tier in policy["distance_buffer_tiers"]
        ],
    }
    saved = client.put("/api/coach/scheduling-policy", json=update)
    disabled = client.post("/api/coach/availability", json=request)

    assert saved.status_code == 200
    assert saved.json()["revision"] == 1
    assert disabled.status_code == 200
    disabled_slots = disabled.json()["available_slots"]
    assert disabled_slots
    assert disabled_slots[0]["start_iso"] == "2026-08-20T10:00:00-05:00"


def test_canonical_policy_update_preserves_payment_and_legacy_fields(client):
    policy = client.get("/api/coach/scheduling-policy").json()
    first = client.put(
        "/api/coach/scheduling-policy",
        json=_policy_update(policy, post_break_recovery_mins=22),
    )
    assert first.status_code == 200

    with db.SessionLocal() as session:
        row = session.get(SchedulerSettingsDB, "inst_001")
        row.payments_enabled_methods = "cash,wallet"
        row.cancellation_window_hours = 37
        row.require_payment_method_for_new_clients = True
        row.rounding_increment_mins = 10
        row.apply_distance_buffer_at_shift_start = True
        session.commit()
        preserved = (
            row.payments_enabled_methods,
            row.cancellation_window_hours,
            row.require_payment_method_for_new_clients,
            row.rounding_increment_mins,
            row.apply_distance_buffer_at_shift_start,
        )

    current = client.get("/api/coach/scheduling-policy").json()
    second = client.put(
        "/api/coach/scheduling-policy",
        json=_policy_update(current, post_break_recovery_mins=31),
    )
    assert second.status_code == 200

    with db.SessionLocal() as session:
        row = session.get(SchedulerSettingsDB, "inst_001")
        assert (
            row.payments_enabled_methods,
            row.cancellation_window_hours,
            row.require_payment_method_for_new_clients,
            row.rounding_increment_mins,
            row.apply_distance_buffer_at_shift_start,
        ) == preserved


def test_invalid_tier_update_is_atomic(client):
    original = client.get("/api/coach/scheduling-policy").json()
    invalid = _policy_update(original)
    invalid["distance_buffer_tiers"] = list(reversed(invalid["distance_buffer_tiers"]))

    response = client.put("/api/coach/scheduling-policy", json=invalid)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.get("/api/coach/scheduling-policy").json() == original


@pytest.mark.parametrize(
    ("method", "path", "json"),
    [
        ("GET", "/api/coach/command-center/snapshot?date=2026-08-20", None),
        ("GET", "/api/coach/settings", None),
        ("GET", "/api/coach/scheduling-policy", None),
        ("PUT", "/api/coach/scheduling-policy", {
            "expected_revision": 0,
            "distance_buffers_enabled": True,
            "post_break_recovery_mins": 15,
            "distance_buffer_tiers": [
                {"id": "near", "buffer_mins": 10},
                {"id": "medium", "buffer_mins": 20},
                {"id": "far", "buffer_mins": 30},
            ],
        }),
        ("PUT", "/api/coach/settings", {
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
        }),
    ],
)
def test_coach_command_center_routes_require_coach_dependency(method, path, json):
    app = create_app()

    def reject_coach():
        raise HTTPException(status_code=401, detail="coach required")

    app.dependency_overrides[get_current_coach] = reject_coach
    test_client = TestClient(app, raise_server_exceptions=False)
    try:
        response = test_client.request(method, path, json=json)
    finally:
        test_client.close()

    assert response.status_code == 401


def test_command_center_and_settings_do_not_appear_under_public_routes():
    test_client = TestClient(create_app(), raise_server_exceptions=False)
    try:
        snapshot = test_client.get("/api/public/command-center/snapshot?date=2026-08-20")
        settings = test_client.get("/api/public/settings")
    finally:
        test_client.close()

    assert snapshot.status_code == 404
    assert settings.status_code == 404


def _policy_update(policy, **overrides):
    payload = {
        "expected_revision": policy["revision"],
        "distance_buffers_enabled": policy["distance_buffers_enabled"],
        "post_break_recovery_mins": policy["post_break_recovery_mins"],
        "distance_buffer_tiers": [
            {"id": tier["id"], "buffer_mins": tier["buffer_mins"]}
            for tier in policy["distance_buffer_tiers"]
        ],
    }
    payload.update(overrides)
    return payload
