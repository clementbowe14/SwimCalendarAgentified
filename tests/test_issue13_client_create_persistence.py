from __future__ import annotations

import datetime
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import ClientDB, InstructorDB


def _availability(prefix: str) -> list[dict]:
    return [
        {
            "id": f"window_{prefix}_weekday",
            "day_mask": 5,
            "start_time": datetime.time(8, 0),
            "end_time": datetime.time(10, 0),
            "position": 0,
        },
        {
            "id": f"window_{prefix}_weekend",
            "day_mask": 32,
            "start_time": datetime.time(12, 0),
            "end_time": datetime.time(14, 0),
            "position": 1,
        },
    ]


def _replay_payload(client_id: str, name: str) -> dict:
    return {
        "schema_version": 1,
        "request": {
            "name": name,
            "phone": "+15125550100",
            "location_mode": "client_location",
        },
        "response": {
            "id": client_id,
            "name": name,
            "revision": 1,
        },
    }


def _create(
    instructor_id: str,
    *,
    client_id: str,
    name: str,
    key: str,
    payload: dict,
    availability: list[dict] | None = None,
) -> dict:
    return db.create_client_profile(
        instructor_id,
        client_id=client_id,
        name=name,
        phone="+15125550100",
        email=None,
        notes="Gate code 2468",
        location_mode="client_location",
        default_location_address="901 Home Rd, Austin, TX",
        default_lat=30.401,
        default_lng=-97.701,
        default_duration_mins=45,
        availability=availability if availability is not None else _availability(client_id),
        idempotency_key=key,
        create_payload=payload,
    )


def test_create_profile_persists_windows_and_replays_original_envelope(
    migrated_postgres_db: None,
):
    key = "repository-create-maya"
    payload = _replay_payload("client_repository_maya", "Maya Chen")

    created = _create(
        "inst_001",
        client_id="client_repository_maya",
        name="Maya Chen",
        key=key,
        payload=payload,
    )

    assert created == payload
    assert db.get_client_creation_by_idempotency_key("inst_001", key) == payload
    profile = db.get_client_profile_for_instructor(
        "inst_001",
        "client_repository_maya",
    )
    assert profile == {
        "id": "client_repository_maya",
        "name": "Maya Chen",
        "email": None,
        "phone": "+15125550100",
        "notes": "Gate code 2468",
        "location_mode": "client_location",
        "default_location_address": "901 Home Rd, Austin, TX",
        "default_lat": 30.401,
        "default_lng": -97.701,
        "default_duration_mins": 45,
        "revision": 1,
        "availability": _availability("client_repository_maya"),
    }

    replay = _create(
        "inst_001",
        client_id="client_repository_maya_retry",
        name="Maya Chen",
        key=key,
        payload={
            **payload,
            "response": {
                "id": "client_repository_maya_retry",
                "name": "Maya Chen",
                "revision": 1,
            },
        },
    )
    assert replay == payload

    with pytest.raises(db.IdempotencyKeyConflictError):
        _create(
            "inst_001",
            client_id="client_repository_other",
            name="Other client",
            key=key,
            payload=_replay_payload("client_repository_other", "Other client"),
        )

    with db.SessionLocal() as session:
        rows = session.query(ClientDB).filter_by(
            instructor_id="inst_001",
            create_idempotency_key=key,
        ).all()
        assert [row.id for row in rows] == ["client_repository_maya"]
        assert [window.id for window in rows[0].availability_windows] == [
            "window_client_repository_maya_weekday",
            "window_client_repository_maya_weekend",
        ]


def test_create_key_namespace_is_provider_scoped(migrated_postgres_db: None):
    with db.SessionLocal.begin() as session:
        session.add(
            InstructorDB(
                id="inst_repository_two",
                name="Provider Two",
                main_address="200 Service Way",
                lat=31.2,
                lng=-98.2,
            )
        )

    key = "repository-provider-shared-key"
    first = _replay_payload("client_repository_first", "First provider client")
    second = _replay_payload("client_repository_second", "Second provider client")
    assert _create(
        "inst_001",
        client_id="client_repository_first",
        name="First provider client",
        key=key,
        payload=first,
    ) == first
    assert _create(
        "inst_repository_two",
        client_id="client_repository_second",
        name="Second provider client",
        key=key,
        payload=second,
    ) == second
    assert db.get_client_creation_by_idempotency_key("inst_001", key) == first
    assert db.get_client_creation_by_idempotency_key(
        "inst_repository_two",
        key,
    ) == second
    assert db.get_client_creation_by_idempotency_key("inst_unknown", key) is None

    first_replay = _create(
        "inst_001",
        client_id="client_repository_first_retry",
        name="First provider client",
        key=key,
        payload={
            **first,
            "response": {
                "id": "client_repository_first_retry",
                "name": "First provider client",
                "revision": 1,
            },
        },
    )
    assert first_replay == first
    with pytest.raises(db.IdempotencyKeyConflictError):
        _create(
            "inst_001",
            client_id="client_repository_first_conflict",
            name="Conflicting provider-one client",
            key=key,
            payload=_replay_payload(
                "client_repository_first_conflict",
                "Conflicting provider-one client",
            ),
        )
    assert db.get_client_creation_by_idempotency_key(
        "inst_repository_two",
        key,
    ) == second

    with db.SessionLocal() as session:
        rows = session.query(ClientDB).filter_by(
            create_idempotency_key=key,
        ).order_by(ClientDB.instructor_id).all()
        assert [(row.instructor_id, row.id) for row in rows] == [
            ("inst_001", "client_repository_first"),
            ("inst_repository_two", "client_repository_second"),
        ]


def test_window_failure_rolls_back_complete_create_and_leaves_key_reusable(
    migrated_postgres_db: None,
):
    key = "repository-rollback-key"
    client_id = "client_repository_rollback"
    payload = _replay_payload(client_id, "Rollback client")
    duplicate_windows = _availability(client_id)
    duplicate_windows[1]["id"] = duplicate_windows[0]["id"]

    with pytest.raises(IntegrityError):
        _create(
            "inst_001",
            client_id=client_id,
            name="Rollback client",
            key=key,
            payload=payload,
            availability=duplicate_windows,
        )

    assert db.get_client_creation_by_idempotency_key("inst_001", key) is None
    with db.SessionLocal() as session:
        assert session.get(ClientDB, client_id) is None

    assert _create(
        "inst_001",
        client_id=client_id,
        name="Rollback client",
        key=key,
        payload=payload,
    ) == payload


def test_concurrent_identical_creates_return_one_exact_winner(
    migrated_postgres_db: None,
    monkeypatch,
):
    barrier = threading.Barrier(2)
    recovery_lock = threading.Lock()
    recovery_calls = 0
    original_match = db._matching_client_create_payload

    def record_recovery(client, attempted_payload):
        nonlocal recovery_calls
        with recovery_lock:
            recovery_calls += 1
        return original_match(client, attempted_payload)

    monkeypatch.setattr(db, "_matching_client_create_payload", record_recovery)

    def create(index: int) -> dict:
        client_id = f"client_repository_concurrent_{index}"
        payload = _replay_payload(client_id, "Concurrent client")
        barrier.wait(timeout=5)
        return _create(
            "inst_001",
            client_id=client_id,
            name="Concurrent client",
            key="repository-concurrent-identical",
            payload=payload,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(create, index) for index in range(2)]
        results = [future.result(timeout=10) for future in futures]

    assert results[0] == results[1]
    assert recovery_calls == 1
    with db.SessionLocal() as session:
        rows = session.query(ClientDB).filter_by(
            instructor_id="inst_001",
            create_idempotency_key="repository-concurrent-identical",
        ).all()
        assert len(rows) == 1
        assert rows[0].id == results[0]["response"]["id"]
        assert len(rows[0].availability_windows) == 2


def test_concurrent_different_creates_have_one_winner_and_one_conflict(
    migrated_postgres_db: None,
):
    barrier = threading.Barrier(2)

    def create(index: int):
        client_id = f"client_repository_mismatch_{index}"
        name = f"Different client {index}"
        barrier.wait(timeout=5)
        try:
            return "created", _create(
                "inst_001",
                client_id=client_id,
                name=name,
                key="repository-concurrent-mismatch",
                payload=_replay_payload(client_id, name),
            )
        except db.IdempotencyKeyConflictError as exc:
            return "conflict", exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(create, index) for index in range(2)]
        outcomes = [future.result(timeout=10) for future in futures]

    assert sorted(outcome[0] for outcome in outcomes) == ["conflict", "created"]
    winner = next(outcome[1] for outcome in outcomes if outcome[0] == "created")
    with db.SessionLocal() as session:
        rows = session.query(ClientDB).filter_by(
            instructor_id="inst_001",
            create_idempotency_key="repository-concurrent-mismatch",
        ).all()
        assert len(rows) == 1
        assert rows[0].id == winner["response"]["id"]
