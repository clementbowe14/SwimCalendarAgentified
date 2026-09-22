from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app import db
from app.api import create_app
from app.dependencies import get_current_coach
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


def test_coach_scheduler_settings_can_be_read_and_updated(client):
    initial = client.get("/api/coach/settings")
    assert initial.status_code == 200

    updated_rules = initial.json()
    updated_rules["post_break_buffer_mins"] = 20
    updated_rules["payments"]["enabled_methods"] = ["card", "cash"]
    updated_rules["payments"]["cancellation_window_hours"] = 18

    update = client.put("/api/coach/settings", json=updated_rules)
    reread = client.get("/api/coach/settings")

    assert update.status_code == 200
    assert update.json() == updated_rules
    assert reread.status_code == 200
    assert reread.json() == updated_rules


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
    assert any(slot["buffer_mins"] > 0 for slot in enabled.json()["available_slots"])

    rules = client.get("/api/coach/settings").json()
    rules["buffers_enabled"] = False
    saved = client.put("/api/coach/settings", json=rules)
    disabled = client.post("/api/coach/availability", json=request)

    assert saved.status_code == 200
    assert disabled.status_code == 200
    assert disabled.json()["available_slots"]
    assert all(slot["buffer_mins"] == 0 for slot in disabled.json()["available_slots"])


def test_old_settings_payload_preserves_explicit_buffer_switches(client):
    rules = client.get("/api/coach/settings").json()
    rules["buffers_enabled"] = False
    rules["apply_distance_buffer_at_shift_start"] = True
    assert client.put("/api/coach/settings", json=rules).status_code == 200

    old_payload = dict(rules)
    old_payload.pop("buffers_enabled")
    old_payload.pop("apply_distance_buffer_at_shift_start")
    old_payload["post_break_buffer_mins"] = 22
    response = client.put("/api/coach/settings", json=old_payload)

    assert response.status_code == 200
    assert response.json()["buffers_enabled"] is False
    assert response.json()["apply_distance_buffer_at_shift_start"] is True
    assert response.json()["post_break_buffer_mins"] == 22


def test_invalid_tier_update_is_atomic(client):
    original = client.get("/api/coach/settings").json()
    invalid = dict(original)
    invalid["distance_buffer_tiers"] = [dict(tier) for tier in original["distance_buffer_tiers"]]
    invalid["distance_buffer_tiers"][0]["max_miles"] = 4.98

    response = client.put("/api/coach/settings", json=invalid)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.get("/api/coach/settings").json() == original


@pytest.mark.parametrize(
    ("method", "path", "json"),
    [
        ("GET", "/api/coach/command-center/snapshot?date=2026-08-20", None),
        ("GET", "/api/coach/settings", None),
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
