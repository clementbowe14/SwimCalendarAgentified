from __future__ import annotations

import datetime

from fastapi.testclient import TestClient
from sqlalchemy import event

from app import db
from app.auth import SESSION_COOKIE_NAME
from app.models import ClientAvailabilityWindowDB, ClientDB, InstructorDB


def _seed_client_read_cases() -> None:
    now = datetime.datetime.utcnow()
    with db.SessionLocal.begin() as session:
        alice = session.get(ClientDB, "client_alice")
        alice.name = "alpha"
        alice.phone = "   "
        alice.email = None
        alice.notes = "Gate code 2468"
        alice.location_mode = "client_location"
        alice.default_location_address = "901 Home Rd, Austin, TX"
        alice.default_lat = 30.3
        alice.default_lng = -97.3
        alice.default_duration_mins = 60
        alice.revision = 3

        bob = session.get(ClientDB, "client_bob")
        bob.name = "Alpha"
        bob.notes = "Meet by the side entrance"
        bob.location_mode = "service_location"
        bob.default_location_address = "700 Saved Client Rd, Austin, TX"
        bob.default_lat = 30.7
        bob.default_lng = -97.7
        bob.default_duration_mins = 30
        bob.revision = 2

        sofia = session.get(ClientDB, "client_sofia")
        sofia.status = "deactivated"
        sofia.deactivated_at = now

        session.add_all(
            [
                ClientDB(
                    id="client_service_contact",
                    instructor_id="inst_001",
                    name="Zulu",
                    phone="+15125550199",
                    email="zulu@example.test",
                    notes="Service-location client",
                    location_mode="service_location",
                    default_duration_mins=45,
                ),
                ClientAvailabilityWindowDB(
                    id="window_weekdays_z",
                    client_id="client_alice",
                    instructor_id="inst_001",
                    day_mask=21,
                    start_time=datetime.time(16, 0),
                    end_time=datetime.time(19, 0),
                    position=2,
                ),
                ClientAvailabilityWindowDB(
                    id="window_weekend",
                    client_id="client_alice",
                    instructor_id="inst_001",
                    day_mask=96,
                    start_time=datetime.time(8, 0),
                    end_time=datetime.time(10, 0),
                    position=0,
                ),
                ClientAvailabilityWindowDB(
                    id="window_weekdays_a",
                    client_id="client_alice",
                    instructor_id="inst_001",
                    day_mask=10,
                    start_time=datetime.time(12, 0),
                    end_time=datetime.time(14, 0),
                    position=2,
                ),
            ]
        )

        foreign_provider = InstructorDB(
            id="inst_002",
            name="Provider Two",
            main_address="200 Service Way",
            lat=31.2,
            lng=-98.2,
            timezone="America/Chicago",
            service_address="200 Service Way",
            service_lat=31.2,
            service_lng=-98.2,
            service_name="Consultation",
            default_duration_mins=45,
        )
        session.add(foreign_provider)
        session.add(
            ClientDB(
                id="client_foreign",
                instructor_id="inst_002",
                name="Foreign client",
                email="foreign@example.test",
                location_mode="service_location",
            )
        )


def test_client_reads_project_canonical_profiles_and_current_effective_location(
    authenticated_api_client: TestClient,
):
    _seed_client_read_cases()

    listed = authenticated_api_client.get("/api/coach/clients")

    assert listed.status_code == 200, listed.text
    assert listed.headers["cache-control"] == "private, no-store"
    assert listed.headers["referrer-policy"] == "no-referrer"
    clients = listed.json()["clients"]
    assert [client["id"] for client in clients] == [
        "client_alice",
        "client_bob",
        "client_service_contact",
    ]
    assert clients[0] == {
        "id": "client_alice",
        "name": "alpha",
        "phone": None,
        "email": None,
        "notes": "Gate code 2468",
        "location_mode": "client_location",
        "client_location": {
            "address": "901 Home Rd, Austin, TX",
            "lat": 30.3,
            "lng": -97.3,
        },
        "effective_location": {
            "address": "901 Home Rd, Austin, TX",
            "lat": 30.3,
            "lng": -97.3,
        },
        "default_duration_mins": 60,
        "availability": [
            {
                "id": "window_weekend",
                "days": ["saturday", "sunday"],
                "start_time": "08:00",
                "end_time": "10:00",
            },
            {
                "id": "window_weekdays_a",
                "days": ["tuesday", "thursday"],
                "start_time": "12:00",
                "end_time": "14:00",
            },
            {
                "id": "window_weekdays_z",
                "days": ["monday", "wednesday", "friday"],
                "start_time": "16:00",
                "end_time": "19:00",
            },
        ],
        "revision": 3,
    }
    assert clients[1]["client_location"] == {
        "address": "700 Saved Client Rd, Austin, TX",
        "lat": 30.7,
        "lng": -97.7,
    }
    assert clients[1]["effective_location"] == {
        "address": "100 Main St, Austin, TX",
        "lat": 30.2672,
        "lng": -97.7431,
    }
    assert clients[2]["phone"] == "+15125550199"
    assert clients[2]["email"] == "zulu@example.test"
    assert clients[2]["client_location"] is None
    assert clients[2]["effective_location"] == {
        "address": "100 Main St, Austin, TX",
        "lat": 30.2672,
        "lng": -97.7431,
    }

    with db.SessionLocal.begin() as session:
        provider = session.get(InstructorDB, "inst_001")
        provider.service_address = "300 Updated Service Way"
        provider.service_lat = 30.8
        provider.service_lng = -97.8

    service_client = authenticated_api_client.get("/api/coach/clients/client_bob")
    contact_client = authenticated_api_client.get(
        "/api/coach/clients/client_service_contact"
    )
    home_client = authenticated_api_client.get("/api/coach/clients/client_alice")
    assert service_client.status_code == 200, service_client.text
    updated_service_location = {
        "address": "300 Updated Service Way",
        "lat": 30.8,
        "lng": -97.8,
    }
    assert service_client.json() == {
        **clients[1],
        "effective_location": updated_service_location,
    }
    assert contact_client.status_code == 200, contact_client.text
    assert contact_client.json() == {
        **clients[2],
        "effective_location": updated_service_location,
    }
    assert home_client.status_code == 200, home_client.text
    assert home_client.json() == clients[0]
    for response in (service_client, contact_client, home_client):
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["referrer-policy"] == "no-referrer"

    with db.SessionLocal.begin() as session:
        provider = session.get(InstructorDB, "inst_001")
        provider.service_address = None
        provider.service_lat = None
        provider.service_lng = None

    incomplete = authenticated_api_client.get("/api/coach/clients/client_bob")
    assert incomplete.status_code == 200, incomplete.text
    assert incomplete.json()["effective_location"] is None


def test_client_reads_hide_foreign_unknown_and_deactivated_records(
    authenticated_api_client: TestClient,
):
    _seed_client_read_cases()

    responses = [
        authenticated_api_client.get("/api/coach/clients/client_foreign"),
        authenticated_api_client.get("/api/coach/clients/client_sofia"),
        authenticated_api_client.get("/api/coach/clients/client_missing"),
    ]

    assert [response.status_code for response in responses] == [404, 404, 404]
    assert all(
        response.json()
        == {
            "error": {
                "code": "RESOURCE_NOT_FOUND",
                "message": "The client could not be found.",
            }
        }
        for response in responses
    )
    assert all(response.headers["cache-control"] == "private, no-store" for response in responses)
    assert all(response.headers["referrer-policy"] == "no-referrer" for response in responses)

    with db.SessionLocal.begin() as session:
        for client in (
            session.query(ClientDB)
            .filter(
                ClientDB.instructor_id == "inst_001",
                ClientDB.status == "active",
            )
            .all()
        ):
            client.status = "deactivated"
            client.deactivated_at = datetime.datetime.utcnow()

    empty = authenticated_api_client.get("/api/coach/clients")
    assert empty.status_code == 200
    assert empty.json() == {"clients": []}


def test_client_reads_require_session_and_ignore_spoofed_tenant_query(
    api_client: TestClient,
    coach_auth_headers: dict[str, str],
):
    _seed_client_read_cases()

    unauthenticated = api_client.get("/api/coach/clients")
    unauthenticated_detail = api_client.get("/api/coach/clients/client_alice")
    invalid = api_client.get(
        "/api/coach/clients",
        headers={"Cookie": f"{SESSION_COOKIE_NAME}=not-a-valid-session"},
    )
    invalid_detail = api_client.get(
        "/api/coach/clients/client_alice",
        headers={"Cookie": f"{SESSION_COOKIE_NAME}=not-a-valid-session"},
    )
    spoofed = api_client.get(
        "/api/coach/clients?instructor_id=inst_002",
        headers={"Cookie": coach_auth_headers["Cookie"]},
    )
    cookie_only_detail = api_client.get(
        "/api/coach/clients/client_alice",
        headers={"Cookie": coach_auth_headers["Cookie"]},
    )

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert unauthenticated_detail.status_code == 401
    assert unauthenticated_detail.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "INVALID_AUTH_TOKEN"
    assert invalid_detail.status_code == 401
    assert invalid_detail.json()["error"]["code"] == "INVALID_AUTH_TOKEN"
    assert spoofed.status_code == 200, spoofed.text
    assert [client["id"] for client in spoofed.json()["clients"]] == [
        "client_alice",
        "client_bob",
        "client_service_contact",
    ]
    assert cookie_only_detail.status_code == 200, cookie_only_detail.text
    for response in (
        unauthenticated,
        unauthenticated_detail,
        invalid,
        invalid_detail,
        spoofed,
        cookie_only_detail,
    ):
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_client_read_slice_preserves_command_center_client_projection(
    authenticated_api_client: TestClient,
):
    _seed_client_read_cases()

    snapshot = authenticated_api_client.get(
        "/api/coach/command-center/snapshot?date=2026-08-20"
    )

    assert snapshot.status_code == 200, snapshot.text
    alice = next(
        client
        for client in snapshot.json()["clients"]
        if client["id"] == "client_alice"
    )
    assert set(alice) == {
        "id",
        "name",
        "phone",
        "email",
        "guardian_name",
        "default_location",
        "location_mode",
        "availability",
        "lesson_duration_mins",
        "notes",
        "history_summary",
        "progress_focus",
    }
    assert alice["location_mode"] == "client_home"
    assert alice["lesson_duration_mins"] == 45
    assert alice["availability"] == []
    assert alice["notes"] == ""
    assert alice["history_summary"] == "Loaded from client profile"


def test_client_profile_list_eager_loads_availability_without_per_client_queries(
    migrated_postgres_db: None,
):
    _seed_client_read_cases()
    selects: list[str] = []

    def record_select(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(db.engine, "before_cursor_execute", record_select)
    try:
        clients = db.list_client_profiles_for_instructor("inst_001")
    finally:
        event.remove(db.engine, "before_cursor_execute", record_select)

    assert [client["id"] for client in clients] == [
        "client_alice",
        "client_bob",
        "client_service_contact",
    ]
    assert clients[0]["availability"]
    assert 1 <= len(selects) <= 2
