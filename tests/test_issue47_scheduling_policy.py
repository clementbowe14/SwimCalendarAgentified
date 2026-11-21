from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
from sqlalchemy import event, text

from app import db
from app.api_schemas import CoachSchedulingPolicyResponse
from app.models import InstructorDB, InviteSlotOfferDB, LessonRequestDB, SchedulerSettingsDB
from app.scheduler_settings import default_scheduling_policy, load_scheduling_policy


def test_default_policy_read_and_snapshot_do_not_create_a_row(
    authenticated_api_client: TestClient,
):
    with db.SessionLocal() as session:
        assert session.query(SchedulerSettingsDB).count() == 0

    response = authenticated_api_client.get("/api/coach/scheduling-policy")
    snapshot = authenticated_api_client.get(
        "/api/coach/command-center/snapshot",
        params={"date": "2026-08-20"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == default_scheduling_policy().model_dump(mode="json")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["scheduling_policy"] == response.json()
    with db.SessionLocal() as session:
        assert session.query(SchedulerSettingsDB).count() == 0


def test_first_write_omits_payment_columns_and_stale_write_is_atomic(
    authenticated_api_client: TestClient,
):
    statements: list[str] = []

    def capture_sql(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", capture_sql)
    try:
        original = authenticated_api_client.get("/api/coach/scheduling-policy").json()
        request = _update_payload(
            original,
            distance_buffers_enabled=False,
            post_break_recovery_mins=27,
        )

        saved = authenticated_api_client.put(
            "/api/coach/scheduling-policy",
            json=request,
        )
        stale = authenticated_api_client.put(
            "/api/coach/scheduling-policy",
            json={**request, "post_break_recovery_mins": 99},
        )
        current = authenticated_api_client.get(
            "/api/coach/scheduling-policy"
        ).json()
    finally:
        event.remove(db.engine, "before_cursor_execute", capture_sql)

    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1
    assert saved.json()["distance_buffers_enabled"] is False
    assert saved.json()["post_break_recovery_mins"] == 27
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "SCHEDULING_POLICY_STALE"
    assert current == saved.json()

    policy_statements = [
        statement.lower()
        for statement in statements
        if "scheduler_settings" in statement.lower()
    ]
    assert policy_statements
    for payment_column in (
        "payments_enabled_methods",
        "cancellation_window_hours",
        "require_payment_method_for_new_clients",
    ):
        assert all(payment_column not in statement for statement in policy_statements)

    with db.SessionLocal() as session:
        payment_values = session.execute(
            text(
                """
                SELECT
                    payments_enabled_methods,
                    cancellation_window_hours,
                    require_payment_method_for_new_clients
                FROM scheduler_settings
                WHERE instructor_id = 'inst_001'
                """
            )
        ).one()
    assert tuple(payment_values) == ("", 24, False)


def test_update_contract_rejects_unknown_fields_and_server_owned_tier_data(
    authenticated_api_client: TestClient,
):
    policy = authenticated_api_client.get("/api/coach/scheduling-policy").json()
    unknown = {
        **_update_payload(policy),
        "rounding_increment_mins": 10,
    }
    server_owned = _update_payload(policy)
    server_owned["distance_buffer_tiers"][0]["label"] = "Client supplied"

    first = authenticated_api_client.put(
        "/api/coach/scheduling-policy",
        json=unknown,
    )
    second = authenticated_api_client.put(
        "/api/coach/scheduling-policy",
        json=server_owned,
    )

    assert first.status_code == 422
    assert second.status_code == 422
    assert first.json()["error"]["code"] == "VALIDATION_ERROR"
    assert second.json()["error"]["code"] == "VALIDATION_ERROR"
    assert authenticated_api_client.get(
        "/api/coach/scheduling-policy"
    ).json() == policy


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("min_miles", "0"),
        ("max_miles", "4.999"),
        ("buffer_mins", "10"),
    ],
)
def test_persisted_tier_numbers_are_not_silently_coerced(field: str, value: str):
    payload = default_scheduling_policy().model_dump(mode="json")
    payload["distance_buffer_tiers"][0][field] = value

    with pytest.raises(ValidationError):
        CoachSchedulingPolicyResponse.model_validate(payload)


@pytest.mark.parametrize("mutation", ["missing", "unknown", "duplicate", "reordered"])
def test_update_requires_exact_ordered_tier_ids(
    authenticated_api_client: TestClient,
    mutation: str,
):
    policy = authenticated_api_client.get("/api/coach/scheduling-policy").json()
    payload = _update_payload(policy)
    tiers = payload["distance_buffer_tiers"]
    if mutation == "missing":
        payload["distance_buffer_tiers"] = tiers[:-1]
    elif mutation == "unknown":
        tiers[0]["id"] = "unknown"
    elif mutation == "duplicate":
        tiers[1]["id"] = tiers[0]["id"]
    else:
        payload["distance_buffer_tiers"] = list(reversed(tiers))

    response = authenticated_api_client.put(
        "/api/coach/scheduling-policy",
        json=payload,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert authenticated_api_client.get(
        "/api/coach/scheduling-policy"
    ).json() == policy


def test_racing_first_cas_writes_have_one_winner(migrated_postgres_db):
    policy = default_scheduling_policy()
    kwargs = {
        "instructor_id": "inst_001",
        "expected_revision": 0,
        "distance_buffers_enabled": False,
        "post_break_recovery_mins": 25,
        "distance_buffer_tiers": [
            tier.model_dump(mode="json")
            for tier in policy.distance_buffer_tiers
        ],
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: db.save_scheduling_policy_cas(**kwargs),
                range(2),
            )
        )

    assert sum(result is not None for result in results) == 1
    assert sum(result is None for result in results) == 1
    saved = load_scheduling_policy("inst_001", operation="race_test")
    assert saved.revision == 1
    assert saved.distance_buffers_enabled is False


def test_provider_policies_are_isolated(migrated_postgres_db):
    with db.SessionLocal() as session:
        source = session.get(InstructorDB, "inst_001")
        session.add(
            InstructorDB(
                id="inst_002",
                name="Second Provider",
                main_address=source.main_address,
                lat=source.lat,
                lng=source.lng,
                work_start_time=source.work_start_time,
                work_end_time=source.work_end_time,
                timezone=source.timezone,
            )
        )
        session.commit()

    default = default_scheduling_policy()
    tiers = [tier.model_dump(mode="json") for tier in default.distance_buffer_tiers]
    first = db.save_scheduling_policy_cas(
        "inst_001",
        expected_revision=0,
        distance_buffers_enabled=False,
        post_break_recovery_mins=11,
        distance_buffer_tiers=tiers,
    )
    second = db.save_scheduling_policy_cas(
        "inst_002",
        expected_revision=0,
        distance_buffers_enabled=True,
        post_break_recovery_mins=44,
        distance_buffer_tiers=tiers,
    )

    assert first is not None and second is not None
    assert load_scheduling_policy(
        "inst_001", operation="isolation_test"
    ).post_break_recovery_mins == 11
    assert load_scheduling_policy(
        "inst_002", operation="isolation_test"
    ).post_break_recovery_mins == 44


def test_disabled_distance_buffers_preserve_real_post_break_recovery(
    authenticated_api_client: TestClient,
):
    policy = authenticated_api_client.get("/api/coach/scheduling-policy").json()
    saved = authenticated_api_client.put(
        "/api/coach/scheduling-policy",
        json=_update_payload(
            policy,
            distance_buffers_enabled=False,
            post_break_recovery_mins=30,
        ),
    )
    assert saved.status_code == 200, saved.text

    response = authenticated_api_client.post(
        "/api/coach/availability",
        json={
            "client_name": "Post-break client",
            "client_address": "100 Main St, Austin, TX",
            "requested_date": "2026-08-20",
            "duration_mins": 30,
            "is_instructor_facility": True,
        },
    )

    assert response.status_code == 200, response.text
    slots = response.json()["available_slots"]
    post_break = next(
        slot for slot in slots if "T13:" in slot["start_iso"]
    )
    assert post_break["start_iso"].endswith("13:30:00-05:00")
    assert post_break["travel_time_mins"] == 0
    assert post_break["buffer_mins"] == 30


def test_shift_start_enforces_travel_with_zero_buffer(
    authenticated_api_client: TestClient,
):
    response = authenticated_api_client.post(
        "/api/coach/availability",
        json={
            "client_name": "Shift-start client",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-21",
            "duration_mins": 45,
            "is_instructor_facility": False,
        },
    )

    assert response.status_code == 200, response.text
    first = response.json()["available_slots"][0]
    assert first["travel_time_mins"] > 0
    assert first["buffer_mins"] == 0
    assert not first["start_iso"].endswith("08:00:00-05:00")


def test_provider_and_public_searches_share_saved_policy(
    authenticated_api_client: TestClient,
):
    policy = authenticated_api_client.get("/api/coach/scheduling-policy").json()
    saved = authenticated_api_client.put(
        "/api/coach/scheduling-policy",
        json=_update_payload(
            policy,
            distance_buffers_enabled=True,
            post_break_recovery_mins=23,
            tier_minutes={"near": 7, "medium": 17, "far": 29},
        ),
    )
    assert saved.status_code == 200, saved.text

    common = {
        "client_name": "Parity Client",
        "client_address": "450 North Blvd, Austin, TX",
        "requested_date": "2026-08-21",
        "duration_mins": 45,
    }
    provider = authenticated_api_client.post(
        "/api/coach/availability",
        json=common,
    )
    legacy_public = authenticated_api_client.post(
        "/api/public/invites/demo-invite-token/availability",
        json={**common, "location_type": "home"},
    )
    canonical_public = authenticated_api_client.post(
        "/api/public/client-invites/invite_sofia_0826/slots",
        json={
            "client_name": common["client_name"],
            "location": {
                "address": common["client_address"],
                "lat": 30.33,
                "lng": -97.72,
                "is_instructor_facility": False,
            },
            "requested_date": common["requested_date"],
            "duration_mins": common["duration_mins"],
            "recurrence": "single",
            "windows": [
                {
                    "id": "full-working-day",
                    "days": ["Fri"],
                    "start_time": "00:00",
                    "end_time": "23:59",
                }
            ],
        },
    )

    for response in (provider, legacy_public, canonical_public):
        assert response.status_code == 200, response.text

    provider_instants = _utc_instants(provider.json()["available_slots"])
    legacy_instants = _utc_instants(legacy_public.json()["available_slots"])
    canonical_instants = _utc_instants(canonical_public.json()["slots"])
    assert provider_instants
    assert provider_instants == legacy_instants == canonical_instants


def test_custom_tier_minutes_change_actual_provider_start_instants(
    authenticated_api_client: TestClient,
):
    request = {
        "client_name": "Tier-change client",
        "client_address": "450 North Blvd, Austin, TX",
        "requested_date": "2026-08-20",
        "duration_mins": 45,
        "is_instructor_facility": False,
    }
    before = authenticated_api_client.post("/api/coach/availability", json=request)
    assert before.status_code == 200, before.text
    before_slots = before.json()["available_slots"]
    assert before_slots[0]["start_iso"] == "2026-08-20T10:15:00-05:00"

    _set_all_tier_minutes(authenticated_api_client, 30)
    after = authenticated_api_client.post("/api/coach/availability", json=request)

    assert after.status_code == 200, after.text
    after_slots = after.json()["available_slots"]
    assert after_slots[0]["start_iso"] == "2026-08-20T10:30:00-05:00"


def test_current_policy_rejects_stale_direct_booking(
    authenticated_api_client: TestClient,
):
    slot = _policy_sensitive_canonical_slot(authenticated_api_client)
    _set_all_tier_minutes(authenticated_api_client, 240)

    response = authenticated_api_client.post(
        "/api/coach/bookings",
        json={
            "client_name": "Stale direct booking",
            "client_address": "450 North Blvd, Austin, TX",
            "start_time": slot["start_iso"],
            "duration_mins": 45,
            "is_instructor_facility": False,
            "idempotency_key": "issue47-stale-direct-booking",
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SCHEDULING_CONFLICT"


def test_current_policy_rejects_stale_legacy_public_request(
    authenticated_api_client: TestClient,
):
    search = authenticated_api_client.post(
        "/api/public/invites/demo-invite-token/availability",
        json={
            "client_name": "Legacy policy client",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-20",
            "location_type": "home",
            "duration_mins": 45,
        },
    )
    assert search.status_code == 200, search.text
    public_slots = search.json()["available_slots"]
    coach_search = authenticated_api_client.post(
        "/api/coach/availability",
        json={
            "client_name": "Legacy policy client",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-20",
            "is_instructor_facility": False,
            "duration_mins": 45,
        },
    )
    assert coach_search.status_code == 200, coach_search.text
    public_starts = {item["start_iso"] for item in public_slots}
    policy_sensitive_start = next(
        item["start_iso"]
        for item in coach_search.json()["available_slots"]
        if item["buffer_mins"] > 0 and item["start_iso"] in public_starts
    )
    slot = next(item for item in public_slots if item["start_iso"] == policy_sensitive_start)
    _set_all_tier_minutes(authenticated_api_client, 240)

    response = authenticated_api_client.post(
        "/api/public/invites/demo-invite-token/requests",
        json={
            "client_name": "Legacy policy client",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-20",
            "slot_start": slot["start_iso"],
            "duration_mins": 45,
            "location_type": "home",
            "idempotency_key": "issue47-stale-legacy-request",
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SCHEDULING_CONFLICT"


@pytest.mark.parametrize("recurrence", ["single", "weekly"])
def test_current_policy_rejects_stale_canonical_offer_selection(
    authenticated_api_client: TestClient,
    recurrence: str,
):
    slot = _policy_sensitive_canonical_slot(
        authenticated_api_client,
        recurrence=recurrence,
    )
    _set_all_tier_minutes(authenticated_api_client, 240)

    response = authenticated_api_client.post(
        "/api/public/client-invites/invite_sofia_0826/lesson-requests",
        json={
            "slot_id": slot["id"],
            "idempotency_key": f"issue47-stale-{recurrence}-offer",
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_SCHEDULING_REQUEST"


def test_policy_change_blocks_confirmation_without_rewriting_offer_evidence(
    authenticated_api_client: TestClient,
):
    slot = _policy_sensitive_canonical_slot(authenticated_api_client)
    created = authenticated_api_client.post(
        "/api/public/client-invites/invite_sofia_0826/lesson-requests",
        json={
            "slot_id": slot["id"],
            "idempotency_key": "issue47-confirmation-evidence",
        },
    )
    assert created.status_code == 200, created.text
    request_token = created.json()["id"]
    before = _request_evidence(request_token)
    _set_all_tier_minutes(authenticated_api_client, 240)

    response = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{request_token}/confirm",
        json={},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SCHEDULING_CONFLICT"
    assert _request_evidence(request_token) == before


def test_policy_change_blocks_stale_alternative_promotion(
    authenticated_api_client: TestClient,
):
    slots = _canonical_slots(authenticated_api_client)
    primary = slots[0]
    stale_alternative = next(
        item for item in slots
        if item["id"] != primary["id"] and _slot_offer_has_buffer(item["id"])
    )
    created = authenticated_api_client.post(
        "/api/public/client-invites/invite_sofia_0826/lesson-requests",
        json={
            "slot_id": primary["id"],
            "idempotency_key": "issue47-alternative-promotion",
        },
    )
    assert created.status_code == 200, created.text
    request_token = created.json()["id"]
    before = _request_evidence(request_token)
    _set_all_tier_minutes(authenticated_api_client, 240)

    response = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{request_token}/alternatives",
        json={"slot_ids": [stale_alternative["id"]]},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SCHEDULING_CONFLICT"
    assert _request_evidence(request_token) == before


def test_policy_change_blocks_stale_client_alternative_selection(
    authenticated_api_client: TestClient,
):
    slots = _canonical_slots(authenticated_api_client)
    primary = slots[0]
    source_alternative = next(
        item for item in slots
        if item["id"] != primary["id"] and _slot_offer_has_buffer(item["id"])
    )
    created = authenticated_api_client.post(
        "/api/public/client-invites/invite_sofia_0826/lesson-requests",
        json={
            "slot_id": primary["id"],
            "idempotency_key": "issue47-client-alternative",
        },
    )
    assert created.status_code == 200, created.text
    request_token = created.json()["id"]
    promoted = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{request_token}/alternatives",
        json={"slot_ids": [source_alternative["id"]]},
    )
    assert promoted.status_code == 200, promoted.text
    status = authenticated_api_client.get(
        f"/api/public/lesson-requests/{request_token}"
    )
    alternative = status.json()["alternative_slots"][0]
    before = _request_evidence(request_token)
    _set_all_tier_minutes(authenticated_api_client, 240)

    response = authenticated_api_client.post(
        f"/api/public/lesson-requests/{request_token}/select-alternative",
        json={
            "slot_id": alternative["id"],
            "idempotency_key": "issue47-select-stale-alternative",
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_SCHEDULING_REQUEST"
    assert _request_evidence(request_token) == before


def test_malformed_persisted_policy_fails_closed_without_echoing_data(
    authenticated_api_client: TestClient,
):
    with db.SessionLocal() as session:
        session.add(
            SchedulerSettingsDB(
                instructor_id="inst_001",
                distance_buffer_tiers='{"private":"do-not-echo"}',
            )
        )
        session.commit()

    response = authenticated_api_client.get("/api/coach/scheduling-policy")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_SERVER_ERROR",
            "message": "An unexpected error occurred.",
        }
    }
    assert "do-not-echo" not in response.text


def _update_payload(policy: dict, *, tier_minutes=None, **overrides) -> dict:
    tier_minutes = tier_minutes or {}
    payload = {
        "expected_revision": policy["revision"],
        "distance_buffers_enabled": policy["distance_buffers_enabled"],
        "post_break_recovery_mins": policy["post_break_recovery_mins"],
        "distance_buffer_tiers": [
            {
                "id": tier["id"],
                "buffer_mins": tier_minutes.get(tier["id"], tier["buffer_mins"]),
            }
            for tier in policy["distance_buffer_tiers"]
        ],
    }
    payload.update(overrides)
    return payload


def _utc_instants(slots: list[dict]) -> set[tuple[datetime, datetime]]:
    return {
        (
            datetime.fromisoformat(slot["start_iso"]).astimezone(timezone.utc),
            datetime.fromisoformat(slot["end_iso"]).astimezone(timezone.utc),
        )
        for slot in slots
    }


def _canonical_slots(
    client: TestClient,
    *,
    recurrence: str = "single",
) -> list[dict]:
    response = client.post(
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
            "recurrence": recurrence,
            "windows": [
                {
                    "id": "full-day",
                    "days": ["Thu"],
                    "start_time": "08:00",
                    "end_time": "17:00",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["slots"]
    return response.json()["slots"]


def _policy_sensitive_canonical_slot(
    client: TestClient,
    *,
    recurrence: str = "single",
) -> dict:
    return next(
        slot for slot in _canonical_slots(client, recurrence=recurrence)
        if _slot_offer_has_buffer(slot["id"])
    )


def _slot_offer_has_buffer(slot_id: str) -> bool:
    with db.SessionLocal() as session:
        offer = session.get(InviteSlotOfferDB, slot_id)
        assert offer is not None
        return offer.buffer_mins > 0


def _set_all_tier_minutes(client: TestClient, minutes: int) -> dict:
    policy = client.get("/api/coach/scheduling-policy").json()
    saved = client.put(
        "/api/coach/scheduling-policy",
        json=_update_payload(
            policy,
            distance_buffers_enabled=True,
            tier_minutes={tier["id"]: minutes for tier in policy["distance_buffer_tiers"]},
        ),
    )
    assert saved.status_code == 200, saved.text
    return saved.json()


def _request_evidence(request_token: str) -> dict:
    with db.SessionLocal() as session:
        request = session.query(LessonRequestDB).filter_by(
            request_token=request_token
        ).one()
        return {
            "status": request.status,
            "slot_start": request.slot_start,
            "slot_end": request.slot_end,
            "requested_date": request.requested_date,
            "travel_time_mins": request.travel_time_mins,
            "buffer_mins": request.buffer_mins,
            "distance_miles": request.distance_miles,
            "original_slot_offer_id": request.original_slot_offer_id,
        }
