import datetime
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app import api_services, db, tools
from app.api_schemas import CoachBlockerMutation
from app.models import AppointmentDB, InstructorDB
from app.routing import BaseRoutingProvider, GeocodeFailed


def _blocker_payload(**overrides):
    payload = {
        "kind": "break",
        "title": "Lunch break",
        "date": "2026-08-21",
        "start_time": "12:00",
        "end_time": "12:30",
        "location_address": None,
    }
    payload.update(overrides)
    return payload


def test_blocker_schema_and_database_constraints_are_installed(migrated_postgres_db):
    inspector = inspect(db.engine)
    columns = {column["name"]: column for column in inspector.get_columns("appointments")}
    assert columns["blocker_kind"]["nullable"] is True
    assert columns["blocker_create_payload"]["nullable"] is True
    assert columns["revision"]["nullable"] is False
    assert columns["client_id"]["nullable"] is True
    assert columns["location_address"]["nullable"] is True
    assert columns["lat"]["nullable"] is True
    assert columns["lng"]["nullable"] is True
    checks = {check["name"] for check in inspector.get_check_constraints("appointments")}
    assert {
        "ck_appointments_blocker_kind",
        "ck_appointments_revision_positive",
        "ck_appointments_location_complete",
        "ck_appointments_location_coordinates",
        "ck_appointments_blocker_shape",
        "ck_appointments_break_consistency",
        "ck_appointments_unavailable_locationless",
        "ck_appointments_blocker_create_payload",
    }.issubset(checks)


def test_provider_can_create_edit_list_and_remove_blockers(authenticated_api_client):
    created = authenticated_api_client.post(
        "/api/coach/blockers",
        json=_blocker_payload(),
        headers={"Idempotency-Key": "blocker-create-001"},
    )
    assert created.status_code == 201, created.text
    blocker = created.json()
    assert blocker["kind"] == "break"
    assert blocker["location"] is None
    assert blocker["revision"] == 1
    assert blocker["local_date"] == "2026-08-21"
    assert blocker["local_start_time"] == "12:00"
    assert blocker["start_time"].startswith("2026-08-21T12:00:00-05:00")
    assert created.headers["cache-control"] == "private, no-store"
    assert created.headers["referrer-policy"] == "no-referrer"

    listed = authenticated_api_client.get("/api/coach/blockers?date=2026-08-21")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [blocker["id"]]
    assert listed.headers["cache-control"] == "private, no-store"
    assert listed.headers["referrer-policy"] == "no-referrer"

    snapshot = authenticated_api_client.get(
        "/api/coach/command-center/snapshot?date=2026-08-21"
    )
    assert snapshot.status_code == 200
    schedule_item = next(item for item in snapshot.json()["schedule"] if item["id"] == blocker["id"])
    assert schedule_item["kind"] == "break"
    assert schedule_item["location"] is None
    assert schedule_item["revision"] == 1

    updated = authenticated_api_client.put(
        f"/api/coach/blockers/{blocker['id']}",
        json=_blocker_payload(
            kind="personal",
            title="School pickup",
            start_time="14:00",
            end_time="14:45",
            location_address="500 North Ave, Austin, TX",
            expected_revision=1,
        ),
    )
    assert updated.status_code == 200, updated.text
    changed = updated.json()
    assert changed["kind"] == "personal"
    assert changed["revision"] == 2
    assert changed["location"]["address"] == "500 North Ave, Austin, TX"

    stale = authenticated_api_client.put(
        f"/api/coach/blockers/{blocker['id']}",
        json=_blocker_payload(expected_revision=1),
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "BLOCKER_STALE"

    removed = authenticated_api_client.delete(
        f"/api/coach/blockers/{blocker['id']}?expected_revision=2"
    )
    assert removed.status_code == 204
    assert authenticated_api_client.get(
        "/api/coach/blockers?date=2026-08-21"
    ).json() == []

    repeated = authenticated_api_client.delete(
        f"/api/coach/blockers/{blocker['id']}?expected_revision=2"
    )
    assert repeated.status_code == 404


def test_create_is_idempotent_and_rejects_key_reuse(authenticated_api_client):
    headers = {"Idempotency-Key": "blocker-replay-001"}
    first = authenticated_api_client.post(
        "/api/coach/blockers",
        json=_blocker_payload(),
        headers=headers,
    )
    replay = authenticated_api_client.post(
        "/api/coach/blockers",
        json=_blocker_payload(),
        headers=headers,
    )
    assert first.status_code == replay.status_code == 201
    assert first.json()["id"] == replay.json()["id"]

    mismatch = authenticated_api_client.post(
        "/api/coach/blockers",
        json=_blocker_payload(title="Different mutation"),
        headers=headers,
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_blocker_create_rejects_idempotency_key_used_by_booking(
    authenticated_api_client,
):
    key = "booking-key-cannot-create-blocker"
    db.save_appointment(
        "inst_001",
        "client_001",
        "Existing lesson",
        datetime.datetime(2026, 8, 24, 15, 0),
        datetime.datetime(2026, 8, 24, 16, 0),
        "100 Main St, Austin, TX",
        30.27,
        -97.74,
        idempotency_key=key,
    )

    response = authenticated_api_client.post(
        "/api/coach/blockers",
        json=_blocker_payload(date="2026-08-24", start_time="12:00", end_time="12:30"),
        headers={"Idempotency-Key": key},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_idempotent_replay_does_not_repeat_external_geocoding(
    authenticated_api_client,
    monkeypatch,
):
    headers = {"Idempotency-Key": "blocker-geocode-replay"}
    payload = _blocker_payload(
        kind="personal",
        location_address="500 North Ave, Austin, TX",
    )
    first = authenticated_api_client.post(
        "/api/coach/blockers",
        json=payload,
        headers=headers,
    )
    assert first.status_code == 201, first.text

    routing = FailingRecordingGeocoder()
    monkeypatch.setattr(tools, "routing_provider", routing)
    replay = authenticated_api_client.post(
        "/api/coach/blockers",
        json=payload,
        headers=headers,
    )

    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["location"] == first.json()["location"]
    assert routing.geocode_calls == []


def test_idempotent_replay_returns_original_create_after_edit_and_delete(
    authenticated_api_client,
):
    headers = {"Idempotency-Key": "blocker-original-create-replay"}
    original_payload = _blocker_payload(
        kind="personal",
        title="School pickup",
        start_time="13:00",
        end_time="13:45",
        location_address="500 North Ave, Austin, TX",
    )
    created = authenticated_api_client.post(
        "/api/coach/blockers",
        json=original_payload,
        headers=headers,
    )
    assert created.status_code == 201, created.text
    original = created.json()

    updated = authenticated_api_client.put(
        f"/api/coach/blockers/{original['id']}",
        json=_blocker_payload(
            kind="unavailable",
            title="Family commitment",
            start_time="15:00",
            end_time="15:45",
            expected_revision=1,
        ),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 2

    replay_after_edit = authenticated_api_client.post(
        "/api/coach/blockers",
        json=original_payload,
        headers=headers,
    )
    assert replay_after_edit.status_code == 201, replay_after_edit.text
    assert replay_after_edit.json() == original

    removed = authenticated_api_client.delete(
        f"/api/coach/blockers/{original['id']}?expected_revision=2"
    )
    assert removed.status_code == 204, removed.text

    replay_after_delete = authenticated_api_client.post(
        "/api/coach/blockers",
        json=original_payload,
        headers=headers,
    )
    assert replay_after_delete.status_code == 201, replay_after_delete.text
    assert replay_after_delete.json() == original


def test_idempotent_replay_uses_immutable_creation_timezone(
    authenticated_api_client,
):
    session = db.SessionLocal()
    try:
        instructor = session.get(InstructorDB, "inst_001")
        instructor.timezone = "America/Phoenix"
        session.commit()
    finally:
        session.close()

    headers = {"Idempotency-Key": "blocker-creation-timezone-replay"}
    payload = _blocker_payload(
        date="2026-03-08",
        start_time="02:15",
        end_time="02:45",
    )
    created = authenticated_api_client.post(
        "/api/coach/blockers",
        json=payload,
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["timezone"] == "America/Phoenix"

    session = db.SessionLocal()
    try:
        instructor = session.get(InstructorDB, "inst_001")
        instructor.timezone = "America/Chicago"
        session.commit()
    finally:
        session.close()

    replay = authenticated_api_client.post(
        "/api/coach/blockers",
        json=payload,
        headers=headers,
    )
    assert replay.status_code == 201, replay.text
    assert replay.json() == created.json()


def test_concurrent_create_replay_serializes_with_creation_timezone(
    authenticated_api_client,
    monkeypatch,
):
    request = CoachBlockerMutation.model_validate(_blocker_payload())
    creation_timezone = "America/Phoenix"
    start_time, end_time = api_services._blocker_instants(request, creation_timezone)
    replay = {
        "id": "app_concurrent_replay",
        "event_type": "block",
        "blocker_kind": request.kind,
        "title": request.title,
        "start_time": start_time,
        "end_time": end_time,
        "location_address": None,
        "lat": None,
        "lng": None,
        "revision": 1,
        "creation_timezone": creation_timezone,
    }
    lookups = iter((None, replay))
    monkeypatch.setattr(
        db,
        "get_blocker_creation_by_idempotency_key",
        lambda *_args: next(lookups),
    )

    def lose_concurrent_insert(*_args, **kwargs):
        assert kwargs["timezone_name"] == "America/Chicago"
        assert kwargs["start_time"].utcoffset() != start_time.utcoffset()
        raise db.IdempotencyKeyConflictError("The concurrent winner used its creation timezone.")

    monkeypatch.setattr(db, "create_blocker", lose_concurrent_insert)

    response = api_services.create_coach_blocker(
        "inst_001",
        request,
        "blocker-concurrent-timezone-replay",
    )

    assert response.timezone == creation_timezone
    assert response.start_time.isoformat() == "2026-08-21T12:00:00-07:00"


def test_concurrent_identical_creates_ignore_server_owned_coordinate_drift(
    migrated_postgres_db,
    monkeypatch,
):
    routing = BarrierDriftGeocoder()
    monkeypatch.setattr(tools, "routing_provider", routing)
    request = CoachBlockerMutation.model_validate(
        _blocker_payload(
            kind="personal",
            title="Off-site appointment",
            start_time="13:00",
            end_time="13:45",
            location_address="500 North Ave, Austin, TX",
        )
    )

    def create_once(_index: int):
        return api_services.create_coach_blocker(
            "inst_001",
            request,
            "blocker-coordinate-race",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        created = list(executor.map(create_once, range(2)))

    assert routing.geocode_calls == 2
    assert created[0].id == created[1].id
    assert created[0] == created[1]
    assert created[0].location is not None
    assert any(
        created[0].location.lat == pytest.approx(lat)
        and created[0].location.lng == pytest.approx(lng)
        for lat, lng in ((30.31, -97.71), (30.32, -97.72))
    )


def test_every_blocker_kind_excludes_overlapping_mutations(authenticated_api_client):
    first = authenticated_api_client.post(
        "/api/coach/blockers",
        json=_blocker_payload(kind="unavailable", start_time="10:00", end_time="11:00"),
        headers={"Idempotency-Key": "blocker-overlap-001"},
    )
    assert first.status_code == 201, first.text

    overlap = authenticated_api_client.post(
        "/api/coach/blockers",
        json=_blocker_payload(
            kind="personal",
            title="Overlapping event",
            start_time="10:30",
            end_time="11:30",
            location_address="10 Main St, Austin, TX",
        ),
        headers={"Idempotency-Key": "blocker-overlap-002"},
    )
    assert overlap.status_code == 409
    assert overlap.json()["error"]["code"] == "SCHEDULING_CONFLICT"

    adjacent = authenticated_api_client.post(
        "/api/coach/blockers",
        json=_blocker_payload(kind="break", start_time="11:00", end_time="11:30"),
        headers={"Idempotency-Key": "blocker-adjacent-001"},
    )
    assert adjacent.status_code == 201, adjacent.text


def test_blocker_local_times_fail_closed_at_dst_boundaries(authenticated_api_client):
    nonexistent = authenticated_api_client.post(
        "/api/coach/blockers",
        json=_blocker_payload(
            date="2026-03-08",
            start_time="02:15",
            end_time="03:15",
        ),
        headers={"Idempotency-Key": "blocker-dst-spring"},
    )
    assert nonexistent.status_code == 422
    assert nonexistent.json()["error"]["details"] == {
        "field": "start_time",
        "reason": "nonexistent_local_time",
    }

    ambiguous = authenticated_api_client.post(
        "/api/coach/blockers",
        json=_blocker_payload(
            date="2026-11-01",
            start_time="01:15",
            end_time="01:45",
        ),
        headers={"Idempotency-Key": "blocker-dst-fall-01"},
    )
    assert ambiguous.status_code == 422
    assert ambiguous.json()["error"]["details"] == {
        "field": "start_time",
        "reason": "ambiguous_local_time",
    }


def test_cross_provider_blocker_ids_are_indistinguishable_from_missing(
    authenticated_api_client,
    monkeypatch,
):
    session = db.SessionLocal()
    try:
        other = InstructorDB(
            id="inst_other_blocker",
            name="Other Provider",
            main_address="20 Main St, Austin, TX",
            lat=30.2,
            lng=-97.2,
            timezone="America/Chicago",
        )
        session.add(other)
        session.flush()
        session.add(
            AppointmentDB(
                id="app_other_blocker",
                instructor_id=other.id,
                client_id=None,
                request_id=None,
                title="Private event",
                start_time=datetime.datetime(2026, 8, 21, 18, 0),
                end_time=datetime.datetime(2026, 8, 21, 19, 0),
                location_address=None,
                lat=None,
                lng=None,
                is_break=False,
                event_type="block",
                blocker_kind="unavailable",
                revision=1,
                status="scheduled",
            )
        )
        session.commit()
    finally:
        session.close()

    routing = FailingRecordingGeocoder()
    monkeypatch.setattr(tools, "routing_provider", routing)
    update = authenticated_api_client.put(
        "/api/coach/blockers/app_other_blocker",
        json=_blocker_payload(
            kind="personal",
            location_address="Must not be geocoded",
            expected_revision=1,
        ),
    )
    remove = authenticated_api_client.delete(
        "/api/coach/blockers/app_other_blocker?expected_revision=1"
    )
    assert update.status_code == remove.status_code == 404
    assert update.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert remove.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert routing.geocode_calls == []


@pytest.mark.parametrize(
    ("case_id", "values", "constraint"),
    [
        (
            "invalid-kind",
            {"blocker_kind": "meeting", "is_break": False},
            "ck_appointments_blocker_kind",
        ),
        (
            "unavailable-location",
            {
                "blocker_kind": "unavailable",
                "is_break": False,
                "location_address": "500 North Ave",
                "lat": 30.31,
                "lng": -97.71,
            },
            "ck_appointments_unavailable_locationless",
        ),
        (
            "break-flag",
            {"blocker_kind": "break", "is_break": False},
            "ck_appointments_break_consistency",
        ),
        (
            "personal-flag",
            {"blocker_kind": "personal", "is_break": True},
            "ck_appointments_break_consistency",
        ),
        (
            "block-client",
            {"blocker_kind": "personal", "is_break": False, "client_id": "client_001"},
            "ck_appointments_blocker_shape",
        ),
        (
            "partial-location",
            {
                "blocker_kind": "personal",
                "is_break": False,
                "location_address": "500 North Ave",
            },
            "ck_appointments_location_complete",
        ),
    ],
)
def test_blocker_shape_checks_reject_invalid_raw_rows(
    migrated_postgres_db,
    case_id,
    values,
    constraint,
):
    row = {
        "id": f"app_invalid_{case_id}",
        "client_id": None,
        "request_id": None,
        "location_address": None,
        "lat": None,
        "lng": None,
        **values,
    }
    with pytest.raises(IntegrityError, match=constraint):
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO appointments (
                        id, instructor_id, client_id, request_id, title,
                        start_time, end_time, location_address, lat, lng,
                        is_break, event_type, blocker_kind, revision, status
                    ) VALUES (
                        :id, 'inst_001', :client_id, :request_id, 'Invalid blocker',
                        '2026-08-22 12:00:00', '2026-08-22 12:30:00',
                        :location_address, :lat, :lng,
                        :is_break, 'block', :blocker_kind, 1, 'scheduled'
                    )
                    """
                ),
                row,
            )


def test_active_blocker_uses_database_overlap_exclusion_for_lessons_and_blockers(
    migrated_postgres_db,
):
    with db.engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO appointments (
                    id, instructor_id, client_id, title, start_time, end_time,
                    location_address, lat, lng, is_break, event_type,
                    blocker_kind, revision, status
                ) VALUES (
                    'app_raw_blocker', 'inst_001', NULL, 'Protected time',
                    '2026-08-22 12:00:00', '2026-08-22 13:00:00',
                    NULL, NULL, NULL, false, 'block', 'unavailable', 1, 'scheduled'
                )
                """
            )
        )

    with pytest.raises(IntegrityError, match="exclude_appointments_active_overlap"):
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO appointments (
                        id, instructor_id, client_id, title, start_time, end_time,
                        location_address, lat, lng, is_break, event_type,
                        blocker_kind, revision, status
                    ) VALUES (
                        'app_raw_lesson_overlap', 'inst_001', 'client_001', 'Lesson',
                        '2026-08-22 12:30:00', '2026-08-22 13:30:00',
                        '100 Main St', 30.27, -97.74, false, 'lesson', NULL, 1, 'scheduled'
                    )
                    """
                )
            )

    with pytest.raises(IntegrityError, match="exclude_appointments_active_overlap"):
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO appointments (
                        id, instructor_id, client_id, title, start_time, end_time,
                        location_address, lat, lng, is_break, event_type,
                        blocker_kind, revision, status
                    ) VALUES (
                        'app_raw_blocker_overlap', 'inst_001', NULL, 'Another event',
                        '2026-08-22 12:45:00', '2026-08-22 13:15:00',
                        NULL, NULL, NULL, false, 'block', 'personal', 1, 'scheduled'
                    )
                    """
                )
            )


class FailingRecordingGeocoder(BaseRoutingProvider):
    provider_name = "failing-recording"

    def __init__(self):
        self.geocode_calls: list[str] = []

    def estimate_route(self, origin_coords, dest_coords, departure_time=None, traffic_mode="none"):
        raise AssertionError("Route estimation is not expected in this test.")

    def geocode(self, address: str):
        self.geocode_calls.append(address)
        raise GeocodeFailed("This address must not be resolved.")


class BarrierDriftGeocoder(BaseRoutingProvider):
    provider_name = "barrier-drift"

    def __init__(self):
        self._barrier = threading.Barrier(2)
        self._lock = threading.Lock()
        self.geocode_calls = 0

    def estimate_route(self, origin_coords, dest_coords, departure_time=None, traffic_mode="none"):
        raise AssertionError("Route estimation is not expected in this test.")

    def geocode(self, address: str):
        self._barrier.wait(timeout=5)
        with self._lock:
            self.geocode_calls += 1
            call = self.geocode_calls
        return (30.30 + call / 100, -97.70 - call / 100)
