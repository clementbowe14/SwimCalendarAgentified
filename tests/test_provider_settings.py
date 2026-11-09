from __future__ import annotations

import datetime as dt
from copy import deepcopy
from typing import Any

from fastapi.testclient import TestClient

from app import db, tools
from app.auth import provision_provider_account
from app.models import (
    AppointmentDB,
    InstructorDB,
    InviteTokenDB,
    LessonRequestDB,
    ProviderWorkingHoursDB,
)
from app.routing import BaseRoutingProvider, GeocodeFailed, HaversineRoutingProvider, RouteEstimate
from tests.conftest import login_coach


WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


class RecordingRoutingProvider(BaseRoutingProvider):
    provider_name = "recording-provider-settings"

    def __init__(self) -> None:
        self.coordinates = {
            "100 Main St, Austin, TX": (30.2672, -97.7431),
            "450 North Blvd, Austin, TX": (30.3300, -97.7200),
            "10 Private Base Way": (30.1001, -97.1001),
            "20 Public Service Way": (30.2002, -97.2002),
            "30 Replacement Service Way": (30.3003, -97.3003),
            "One Shared Address": (30.4004, -97.4004),
            "First Good Address": (30.5005, -97.5005),
            "Second Bad Address": (30.6006, -97.6006),
            "200 Second Base Way": (31.1001, -98.1001),
            "210 Second Service Way": (31.2002, -98.2002),
        }
        self.fail_addresses: set[str] = set()
        self.geocode_calls: list[str] = []
        self.route_calls: list[tuple[tuple[float, float], tuple[float, float]]] = []
        self._routes = HaversineRoutingProvider()

    def geocode(self, address: str) -> tuple[float, float]:
        self.geocode_calls.append(address)
        if address in self.fail_addresses:
            raise GeocodeFailed("No match")
        if address in self.coordinates:
            return self.coordinates[address]
        return self._routes.geocode(address)

    def estimate_route(
        self,
        origin_coords: tuple[float, float],
        dest_coords: tuple[float, float],
        departure_time: dt.datetime | None = None,
        traffic_mode: str = "none",
    ) -> RouteEstimate:
        self.route_calls.append((origin_coords, dest_coords))
        return self._routes.estimate_route(
            origin_coords,
            dest_coords,
            departure_time=departure_time,
            traffic_mode=traffic_mode,
        )


def _hours(*, disabled: set[str] | None = None) -> list[dict[str, Any]]:
    disabled = disabled or set()
    return [
        {
            "weekday": weekday,
            "enabled": weekday not in disabled,
            "start_time": None if weekday in disabled else "08:00",
            "end_time": None if weekday in disabled else "17:00",
        }
        for weekday in WEEKDAYS
    ]


def _provider_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "display_name": "Coach Rowan",
        "business_name": "Rowan Scheduling",
        "timezone": "America/New_York",
        "base_address": "10 Private Base Way",
        "service_address": "20 Public Service Way",
        "service_name": "On-site consultation",
        "default_duration_mins": 45,
        "working_hours": _hours(),
    }
    payload.update(overrides)
    return payload


def _install_routing_probe(monkeypatch) -> RecordingRoutingProvider:
    provider = RecordingRoutingProvider()
    monkeypatch.setattr(tools, "routing_provider", provider)
    return provider


def _validation_locations(response) -> list[str]:
    return [
        ".".join(str(part) for part in error["loc"])
        for error in response.json()["error"]["details"]["fields"]
    ]


def _all_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.add(key)
            keys.update(_all_keys(nested))
    elif isinstance(value, list):
        for nested in value:
            keys.update(_all_keys(nested))
    return keys


def _seed_second_provider() -> None:
    with db.SessionLocal() as session:
        session.add(
            InstructorDB(
                id="inst_002",
                name="Coach Jamie",
                main_address="210 Second Service Way",
                lat=31.2002,
                lng=-98.2002,
                work_start_time="08:00",
                work_end_time="17:00",
                timezone="America/Chicago",
                business_name="Jamie Scheduling",
                base_address="200 Second Base Way",
                base_lat=31.1001,
                base_lng=-98.1001,
                service_address="210 Second Service Way",
                service_lat=31.2002,
                service_lng=-98.2002,
                service_name="Second provider service",
                default_duration_mins=60,
            )
        )
        session.add_all(
            [
                ProviderWorkingHoursDB(
                    instructor_id="inst_002",
                    weekday=weekday,
                    enabled=True,
                    start_time=dt.time(8, 0),
                    end_time=dt.time(17, 0),
                )
                for weekday in range(7)
            ]
        )
        session.commit()
    provision_provider_account(
        "inst_002",
        "jamie-provider@example.com",
        "jamie-provider-password",
    )


def test_provider_get_put_roundtrip_no_store_and_optional_business_name(
    authenticated_api_client: TestClient,
    monkeypatch,
):
    routing = _install_routing_probe(monkeypatch)
    payload = _provider_payload(
        working_hours=_hours(disabled={"sunday"}),
    )

    saved = authenticated_api_client.put("/api/coach/provider", json=payload)

    assert saved.status_code == 200, saved.text
    assert saved.headers["cache-control"] == "private, no-store"
    assert saved.headers["referrer-policy"] == "no-referrer"
    assert saved.json() == {
        "id": "inst_001",
        "display_name": "Coach Rowan",
        "business_name": "Rowan Scheduling",
        "timezone": "America/New_York",
        "base_location": {
            "address": "10 Private Base Way",
            "lat": 30.1001,
            "lng": -97.1001,
        },
        "service": {
            "name": "On-site consultation",
            "default_duration_mins": 45,
            "location": {
                "address": "20 Public Service Way",
                "lat": 30.2002,
                "lng": -97.2002,
            },
        },
        "working_hours": _hours(disabled={"sunday"}),
        "setup_complete": True,
        "setup_missing_fields": [],
    }
    assert routing.geocode_calls == ["10 Private Base Way", "20 Public Service Way"]

    loaded = authenticated_api_client.get("/api/coach/provider")
    assert loaded.status_code == 200, loaded.text
    assert loaded.headers["cache-control"] == "private, no-store"
    assert loaded.headers["referrer-policy"] == "no-referrer"
    assert loaded.json() == saved.json()

    without_business = deepcopy(payload)
    without_business["business_name"] = "   "
    optional = authenticated_api_client.put("/api/coach/provider", json=without_business)
    assert optional.status_code == 200, optional.text
    assert optional.json()["business_name"] is None
    assert optional.json()["setup_complete"] is True


def test_provider_auth_csrf_and_tenant_identity_are_enforced(
    api_client: TestClient,
    coach_auth_headers: dict[str, str],
    monkeypatch,
):
    _install_routing_probe(monkeypatch)
    unauthenticated = api_client.get("/api/coach/provider")
    cookie_only = {"Cookie": coach_auth_headers["Cookie"]}
    missing_csrf = api_client.put(
        "/api/coach/provider",
        json=_provider_payload(),
        headers=cookie_only,
    )

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert unauthenticated.headers["cache-control"] == "private, no-store"
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "CSRF_VALIDATION_FAILED"

    _seed_second_provider()
    second_headers = login_coach(
        api_client,
        email="jamie-provider@example.com",
        password="jamie-provider-password",
    )
    first_before = api_client.get("/api/coach/provider", headers=coach_auth_headers).json()
    second_payload = _provider_payload(
        display_name="Coach Jamie Updated",
        business_name=None,
        base_address="200 Second Base Way",
        service_address="210 Second Service Way",
        service_name="Jamie's service",
        default_duration_mins=60,
    )
    second_saved = api_client.put(
        "/api/coach/provider",
        json=second_payload,
        headers=second_headers,
    )
    first_after = api_client.get("/api/coach/provider", headers=coach_auth_headers)

    assert second_saved.status_code == 200, second_saved.text
    assert second_saved.json()["id"] == "inst_002"
    assert second_saved.json()["display_name"] == "Coach Jamie Updated"
    assert first_after.status_code == 200
    assert first_after.json() == first_before

    spoofed = deepcopy(second_payload)
    spoofed["id"] = "inst_001"
    rejected = api_client.put(
        "/api/coach/provider",
        json=spoofed,
        headers=second_headers,
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "body.id" in _validation_locations(rejected)


def test_provider_update_rejects_invalid_timezone_hours_and_server_owned_fields(
    authenticated_api_client: TestClient,
):
    cases: list[tuple[str, Any, str]] = [
        ("timezone", lambda body: body.update(timezone="Mars/Olympus_Mons"), "timezone"),
        ("provider id", lambda body: body.update(id="inst_other"), "id"),
        ("base coordinate", lambda body: body.update(base_lat=1.23), "base_lat"),
        ("service coordinate", lambda body: body.update(service_lng=4.56), "service_lng"),
        (
            "missing weekday",
            lambda body: body.update(working_hours=body["working_hours"][:-1]),
            "working_hours",
        ),
        (
            "duplicate weekday",
            lambda body: body["working_hours"][-1].update(weekday="monday"),
            "working_hours",
        ),
        (
            "disabled day with times",
            lambda body: body["working_hours"][0].update(enabled=False),
            "working_hours",
        ),
        (
            "enabled day without close",
            lambda body: body["working_hours"][0].update(end_time=None),
            "working_hours",
        ),
        (
            "non-increasing interval",
            lambda body: body["working_hours"][0].update(start_time="17:00", end_time="17:00"),
            "working_hours",
        ),
        (
            "invalid time label",
            lambda body: body["working_hours"][0].update(start_time="8am"),
            "working_hours",
        ),
    ]

    for label, mutate, expected_location in cases:
        payload = _provider_payload(
            base_address="100 Main St, Austin, TX",
            service_address="100 Main St, Austin, TX",
        )
        mutate(payload)
        response = authenticated_api_client.put("/api/coach/provider", json=payload)

        assert response.status_code == 422, (label, response.text)
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        assert any(expected_location in location for location in _validation_locations(response)), (
            label,
            _validation_locations(response),
        )


def test_provider_geocoding_is_atomic_and_identical_addresses_are_geocoded_once(
    authenticated_api_client: TestClient,
    monkeypatch,
):
    routing = _install_routing_probe(monkeypatch)
    before = authenticated_api_client.get("/api/coach/provider").json()
    routing.fail_addresses.add("Second Bad Address")

    failed = authenticated_api_client.put(
        "/api/coach/provider",
        json=_provider_payload(
            base_address="First Good Address",
            service_address="Second Bad Address",
        ),
    )

    assert failed.status_code == 400, failed.text
    assert failed.json()["error"] == {
        "code": "GEOCODING_FAILED",
        "message": "The address could not be geocoded.",
        "details": {"field": "service_address"},
    }
    assert routing.geocode_calls == ["First Good Address", "Second Bad Address"]
    assert authenticated_api_client.get("/api/coach/provider").json() == before

    routing.fail_addresses.clear()
    routing.geocode_calls.clear()
    saved = authenticated_api_client.put(
        "/api/coach/provider",
        json=_provider_payload(
            base_address="One Shared Address",
            service_address="One Shared Address",
        ),
    )

    assert saved.status_code == 200, saved.text
    assert routing.geocode_calls == ["One Shared Address"]
    assert saved.json()["base_location"] == saved.json()["service"]["location"]


def test_incomplete_setup_is_derived_and_public_errors_hide_private_details(
    authenticated_api_client: TestClient,
):
    with db.SessionLocal() as session:
        session.query(ProviderWorkingHoursDB).filter_by(
            instructor_id="inst_001",
            weekday=6,
        ).delete()
        session.commit()

    settings = authenticated_api_client.get("/api/coach/provider")
    coach = authenticated_api_client.post(
        "/api/coach/availability",
        json={
            "client_name": "Private setup test",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-21",
            "duration_mins": 45,
        },
    )
    legacy_public = authenticated_api_client.post(
        "/api/public/invites/demo-invite-token/availability",
        json={
            "client_name": "Public setup test",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-21",
            "location_type": "home",
            "duration_mins": 45,
        },
    )
    client_invite = authenticated_api_client.post(
        "/api/public/client-invites/invite_sofia_0826/slots",
        json={
            "client_name": "Public setup test",
            "location": {
                "address": "450 North Blvd, Austin, TX",
                "lat": 30.33,
                "lng": -97.72,
                "is_instructor_facility": False,
            },
            "requested_date": "2026-08-21",
            "duration_mins": 45,
            "windows": [],
        },
    )

    assert settings.status_code == 200
    assert settings.json()["setup_complete"] is False
    assert settings.json()["setup_missing_fields"] == ["working_hours"]
    assert coach.status_code == 409
    assert coach.json()["error"] == {
        "code": "PROVIDER_SETUP_REQUIRED",
        "message": "Complete provider settings before scheduling.",
        "details": {"missing_fields": ["working_hours"]},
    }
    for response in (legacy_public, client_invite):
        assert response.status_code == 409, response.text
        assert response.json() == {
            "error": {
                "code": "PROVIDER_SETUP_REQUIRED",
                "message": "Scheduling is temporarily unavailable.",
            }
        }
        assert "base" not in response.text.casefold()
        assert "working_hours" not in response.text


def test_invalid_legacy_timezone_keeps_reads_available_and_blocks_scheduling(
    authenticated_api_client: TestClient,
):
    with db.SessionLocal() as session:
        session.get(InstructorDB, "inst_001").timezone = "Not/A_Real_Zone"
        session.commit()

    provider = authenticated_api_client.get("/api/coach/provider")
    schedule = authenticated_api_client.get("/api/coach/schedule?date=2026-08-20")
    snapshot = authenticated_api_client.get(
        "/api/coach/command-center/snapshot?date=2026-08-20"
    )
    availability = authenticated_api_client.post(
        "/api/coach/availability",
        json={
            "client_name": "Timezone gate",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-21",
            "duration_mins": 45,
        },
    )
    confirmation = authenticated_api_client.post(
        "/api/coach/lesson-requests/req_sofia_pending_0820/confirm",
        json={},
    )

    assert provider.status_code == 200
    assert provider.json()["setup_complete"] is False
    assert "timezone" in provider.json()["setup_missing_fields"]
    assert schedule.status_code == 200, schedule.text
    assert snapshot.status_code == 200, snapshot.text
    for response in (availability, confirmation):
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "PROVIDER_SETUP_REQUIRED"
        assert "timezone" in response.json()["error"]["details"]["missing_fields"]


def test_disabled_weekday_returns_no_slots_without_routing(
    authenticated_api_client: TestClient,
    monkeypatch,
):
    routing = _install_routing_probe(monkeypatch)
    saved = authenticated_api_client.put(
        "/api/coach/provider",
        json=_provider_payload(working_hours=_hours(disabled={"thursday"})),
    )
    assert saved.status_code == 200, saved.text
    routing.route_calls.clear()
    routing.geocode_calls.clear()

    response = authenticated_api_client.post(
        "/api/coach/availability",
        json={
            "client_name": "No Thursday",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-20",
            "duration_mins": 45,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["available_slots"] == []
    assert routing.route_calls == []


def test_base_origin_and_service_destination_are_used_independently(
    authenticated_api_client: TestClient,
    monkeypatch,
):
    routing = _install_routing_probe(monkeypatch)
    saved = authenticated_api_client.put("/api/coach/provider", json=_provider_payload())
    assert saved.status_code == 200, saved.text
    routing.route_calls.clear()

    intake = authenticated_api_client.post(
        "/api/public/client-invites/invite_sofia_0826/intake",
        json={
            "client_name": "Facility client",
            "guardian_phone": "",
            "guardian_email": "",
            "notes": "",
            "location_type": "facility",
            "location": {
                "address": "Client-supplied fake facility",
                "lat": 1.0,
                "lng": 2.0,
                "is_instructor_facility": True,
            },
        },
    )
    assert intake.status_code == 200, intake.text
    assert intake.json()["intake"]["location"] == {
        "address": "20 Public Service Way",
        "lat": 30.2002,
        "lng": -97.2002,
        "is_instructor_facility": True,
    }

    response = authenticated_api_client.post(
        "/api/public/client-invites/invite_sofia_0826/slots",
        json={
            "client_name": "Facility client",
            "location": {
                "address": "Client-supplied fake facility",
                "lat": 1.0,
                "lng": 2.0,
                "is_instructor_facility": True,
            },
            "requested_date": "2026-08-21",
            "duration_mins": 45,
            "windows": [],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["slots"]
    assert {slot["location_address"] for slot in response.json()["slots"]} == {
        "20 Public Service Way"
    }
    assert (
        routing.coordinates["10 Private Base Way"],
        routing.coordinates["20 Public Service Way"],
    ) in routing.route_calls
    assert all(destination != (1.0, 2.0) for _, destination in routing.route_calls)


def test_public_invite_families_project_authoritative_provider_without_base_leakage(
    authenticated_api_client: TestClient,
    monkeypatch,
):
    _install_routing_probe(monkeypatch)
    payload = _provider_payload(
        display_name="Taylor Provider",
        business_name="Taylor Services",
        service_name="Focused session",
        default_duration_mins=90,
    )
    saved = authenticated_api_client.put("/api/coach/provider", json=payload)
    assert saved.status_code == 200, saved.text
    with db.SessionLocal() as session:
        for invite in session.query(InviteTokenDB).filter_by(instructor_id="inst_001"):
            invite.business_name = "Stale Invite Business"
            invite.location_label = "Stale Invite Location"
            invite.preferred_duration_mins = 30
        session.commit()

    invite = authenticated_api_client.get("/api/public/invites/demo-invite-token")
    envelope = authenticated_api_client.get(
        "/api/public/client-invites/invite_sofia_0826"
    )

    for response in (invite, envelope):
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["instructor_name"] == "Taylor Provider"
        assert body["business_name"] == "Taylor Services"
        assert body["service_name"] == "Focused session"
        assert body["service_location"] == {
            "address": "20 Public Service Way",
            "lat": 30.2002,
            "lng": -97.2002,
        }
        assert body["preferred_duration_mins"] == 90
        assert 90 in body["allowed_durations_mins"]
        assert "base_location" not in _all_keys(body)
        assert "10 Private Base Way" not in response.text
        assert "Stale Invite Business" not in response.text


def test_stale_facility_offer_and_pending_request_cannot_commit(
    authenticated_api_client: TestClient,
    monkeypatch,
):
    _install_routing_probe(monkeypatch)
    saved = authenticated_api_client.put("/api/coach/provider", json=_provider_payload())
    assert saved.status_code == 200, saved.text
    offers = authenticated_api_client.post(
        "/api/public/client-invites/invite_sofia_0826/slots",
        json={
            "client_name": "Facility client",
            "location": {
                "address": "Untrusted facility coordinates",
                "lat": 1.0,
                "lng": 2.0,
                "is_instructor_facility": True,
            },
            "requested_date": "2026-08-21",
            "duration_mins": 45,
            "windows": [],
        },
    )
    assert offers.status_code == 200, offers.text
    slot_ids = [slot["id"] for slot in offers.json()["slots"]]
    assert len(slot_ids) >= 2

    pending = authenticated_api_client.post(
        "/api/public/client-invites/invite_sofia_0826/lesson-requests",
        json={"slot_id": slot_ids[0]},
    )
    assert pending.status_code == 200, pending.text
    request_token = pending.json()["id"]

    changed = _provider_payload(service_address="30 Replacement Service Way")
    updated = authenticated_api_client.put("/api/coach/provider", json=changed)
    assert updated.status_code == 200, updated.text

    stale_offer = authenticated_api_client.post(
        "/api/public/client-invites/invite_sofia_0826/lesson-requests",
        json={"slot_id": slot_ids[1]},
    )
    stale_request = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{request_token}/confirm",
        json={},
    )

    assert stale_offer.status_code == 400, stale_offer.text
    assert stale_offer.json()["error"]["code"] == "INVALID_SCHEDULING_REQUEST"
    assert stale_request.status_code == 409, stale_request.text
    assert stale_request.json()["error"]["code"] == "SCHEDULING_CONFLICT"
    with db.SessionLocal() as session:
        lesson_request = session.query(LessonRequestDB).filter_by(
            request_token=request_token
        ).one()
        assert lesson_request.status == "pending"
        assert session.query(AppointmentDB).filter_by(
            request_id=lesson_request.id
        ).count() == 0

