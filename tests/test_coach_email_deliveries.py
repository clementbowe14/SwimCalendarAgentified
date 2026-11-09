from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

from app import db, notifications as n
from app.api_errors import ApiException
from app.models import (
    AppointmentDB,
    ClientDB,
    InstructorDB,
    InviteIntakeDB,
    InviteTokenDB,
    LessonRequestDB,
    NotificationEventDB,
)
from tests.test_notifications import INVITE, REQUEST, Provider, prepare


DELIVERY_KEYS = {
    "id",
    "kind",
    "status",
    "updated_at",
    "client_id",
    "client_name",
    "request_token",
    "issue",
    "source_state",
    "can_resend",
    "can_edit_contact",
}
PRIVATE_KEYS = {
    "recipient",
    "failure_code",
    "provider_message_id",
    "template_context",
    "attempt_count",
    "next_attempt_at",
    "lease_expires_at",
    "attempt_token",
}


def test_invite_response_list_and_snapshot_expose_only_coach_email_projection(
    authenticated_api_client,
):
    prepare()
    sent = authenticated_api_client.post(
        f"/api/coach/invites/{INVITE}/notifications",
        headers={"Idempotency-Key": "coach-email-projection"},
    )
    assert sent.status_code == 202, sent.text
    assert sent.headers["cache-control"] == "private, no-store"
    assert [item["channel"] for item in sent.json()["notifications"]] == ["sms", "email"]
    delivery = sent.json()["email_delivery"]
    _assert_private_projection(delivery)
    assert delivery == {
        **delivery,
        "kind": "invite",
        "status": "queued",
        "client_id": "client_sofia",
        "client_name": "Sofia Martinez",
        "request_token": None,
        "issue": None,
        "source_state": "active",
        "can_resend": False,
        "can_edit_contact": False,
    }

    listed = authenticated_api_client.get("/api/coach/email-deliveries")
    assert listed.status_code == 200, listed.text
    assert listed.headers["cache-control"] == "private, no-store"
    assert listed.json() == [delivery]

    snapshot = authenticated_api_client.get(
        "/api/coach/command-center/snapshot",
        params={"date": "2026-08-20"},
    )
    assert snapshot.status_code == 200, snapshot.text
    snapshot_delivery = snapshot.json()["email_deliveries"][0]
    assert snapshot_delivery["id"] == delivery["id"]
    assert snapshot_delivery["status"] == delivery["status"]
    _assert_private_projection(snapshot_delivery, optional_nulls=True)
    with db.SessionLocal() as session:
        assert session.query(NotificationEventDB).count() == 2


@pytest.mark.parametrize(
    ("status", "failure_code", "issue", "can_resend", "can_edit"),
    [
        ("queued", None, None, False, False),
        ("processing", None, None, False, False),
        ("accepted", None, None, False, False),
        ("delivered", None, None, False, False),
        ("failed", "provider_retryable", "temporary_failure", True, False),
        ("skipped", "invalid_recipient", "invalid_address", False, True),
    ],
)
def test_every_email_state_projects_without_internal_fields(
    authenticated_api_client,
    status,
    failure_code,
    issue,
    can_resend,
    can_edit,
):
    prepare()
    response = authenticated_api_client.post(
        f"/api/coach/invites/{INVITE}/notifications",
        headers={"Idempotency-Key": "all-email-states"},
    )
    assert response.status_code == 202
    delivery_id = response.json()["email_delivery"]["id"]
    with db.SessionLocal() as session:
        event = session.get(NotificationEventDB, delivery_id)
        event.status = status
        event.failure_code = failure_code
        event.attempt_count = 0 if status == "skipped" else max(event.attempt_count, 1)
        event.recipient = None if status == "skipped" else "private@example.com"
        event.provider_message_id = "private-provider-id"
        event.template_context = {**event.template_context, "private": "do not expose"}
        session.commit()

    payload = authenticated_api_client.get("/api/coach/email-deliveries").json()[0]
    _assert_private_projection(payload)
    assert payload["status"] == status
    assert payload["issue"] == issue
    assert payload["can_resend"] is can_resend
    assert payload["can_edit_contact"] is can_edit
    assert "private@example.com" not in str(payload)


def test_confirmation_response_and_replay_return_current_email_state(authenticated_api_client):
    prepare()
    confirmed = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{REQUEST}/confirm",
        json={},
    )
    assert confirmed.status_code == 200, confirmed.text
    delivery = confirmed.json()["email_delivery"]
    _assert_private_projection(delivery)
    assert delivery["kind"] == "confirmation"
    assert delivery["request_token"] == REQUEST

    with db.SessionLocal() as session:
        event = session.get(NotificationEventDB, delivery["id"])
        event.status = "failed"
        event.failure_code = "provider_retryable"
        event.attempt_count = 5
        event.updated_at = n.utcnow()
        session.commit()

    replay = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{REQUEST}/confirm",
        json={},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["email_delivery"]["id"] == delivery["id"]
    assert replay.json()["email_delivery"]["status"] == "failed"
    assert replay.json()["email_delivery"]["can_resend"] is True
    with db.SessionLocal() as session:
        assert session.query(NotificationEventDB).count() == 2


def test_confirmation_projects_public_request_token_not_internal_id(authenticated_api_client):
    from tests.test_public_client_invite_api import _create_slot_offer

    prepare()
    slot = _create_slot_offer(authenticated_api_client)["slots"][0]
    created = authenticated_api_client.post(
        f"/api/public/client-invites/{INVITE}/lesson-requests",
        json={"slot_id": slot["id"], "idempotency_key": "email-token-privacy"},
    )
    assert created.status_code == 200, created.text
    request_token = created.json()["id"]
    with db.SessionLocal() as session:
        internal_id = session.query(LessonRequestDB).filter_by(
            request_token=request_token
        ).one().id
    assert internal_id != request_token

    confirmed = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{request_token}/confirm",
        json={},
    )
    assert confirmed.status_code == 200, confirmed.text
    projection = confirmed.json()["email_delivery"]
    assert projection["request_token"] == request_token
    assert internal_id not in str(projection)


def test_legacy_confirmation_without_ledger_returns_null_and_creates_nothing(
    authenticated_api_client,
):
    with db.SessionLocal() as session:
        request = session.query(LessonRequestDB).filter_by(request_token=REQUEST).one()
        request.status = "confirmed"
        session.commit()
    response = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{REQUEST}/confirm",
        json={},
    )
    assert response.status_code == 200, response.text
    assert response.json()["email_delivery"] is None
    assert authenticated_api_client.get("/api/coach/email-deliveries").json() == []
    with db.SessionLocal() as session:
        assert session.query(NotificationEventDB).count() == 0


def test_safe_resend_requeues_same_row_once_and_clears_attempt_state(
    authenticated_api_client,
):
    delivery_id = _enqueue_invite_email(authenticated_api_client, "safe-resend")
    with db.SessionLocal() as session:
        event = session.get(NotificationEventDB, delivery_id)
        event.status = "failed"
        event.failure_code = "provider_retryable"
        event.attempt_count = 5
        event.provider_message_id = "private-provider-id"
        event.attempt_token = "private-attempt"
        event.lease_expires_at = n.utcnow() + timedelta(minutes=1)
        event.next_attempt_at = None
        session.commit()

    first = authenticated_api_client.post(
        f"/api/coach/email-deliveries/{delivery_id}/resend"
    )
    assert first.status_code == 200, first.text
    assert first.json()["id"] == delivery_id
    assert first.json()["status"] == "queued"
    assert first.json()["issue"] is None
    first_updated = first.json()["updated_at"]

    replay = authenticated_api_client.post(
        f"/api/coach/email-deliveries/{delivery_id}/resend"
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["updated_at"] == first_updated
    assert authenticated_api_client.post(
        f"/api/coach/email-deliveries/{delivery_id}/resend",
        json={"unsafe": True},
    ).status_code == 422

    with db.SessionLocal() as session:
        event = session.get(NotificationEventDB, delivery_id)
        assert event.status == "queued"
        assert event.attempt_count == 0
        assert event.next_attempt_at is not None
        assert event.lease_expires_at is None
        assert event.attempt_token is None
        assert event.failure_code is None
        assert event.provider_message_id is None
        assert session.query(NotificationEventDB).count() == 2


def test_concurrent_resend_produces_one_queued_mutation(authenticated_api_client):
    delivery_id = _enqueue_invite_email(authenticated_api_client, "concurrent-resend")
    with db.SessionLocal() as session:
        event = session.get(NotificationEventDB, delivery_id)
        event.status = "failed"
        event.failure_code = "attempts_exhausted"
        event.attempt_count = 5
        session.commit()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: n.resend_coach_email_delivery("inst_001", delivery_id),
                range(4),
            )
        )
    assert {result.id for result in results} == {delivery_id}
    assert {result.status for result in results} == {"queued"}
    assert len({result.updated_at for result in results}) == 1
    with db.SessionLocal() as session:
        assert session.query(NotificationEventDB).count() == 2
        assert session.get(NotificationEventDB, delivery_id).attempt_count == 0


@pytest.mark.parametrize(
    ("status", "failure_code", "expected_status"),
    [
        ("failed", "outcome_unknown", 409),
        ("failed", "unrecognized_private_code", 409),
        ("skipped", "invalid_recipient", 409),
        ("accepted", None, 200),
        ("delivered", None, 200),
    ],
)
def test_resend_fails_closed_or_noops_without_mutation(
    authenticated_api_client,
    status,
    failure_code,
    expected_status,
):
    delivery_id = _enqueue_invite_email(
        authenticated_api_client,
        f"blocked-{status}-{failure_code}",
    )
    with db.SessionLocal() as session:
        event = session.get(NotificationEventDB, delivery_id)
        event.status = status
        event.failure_code = failure_code
        event.attempt_count = 1 if status != "skipped" else 0
        before = event.updated_at
        session.commit()
    response = authenticated_api_client.post(
        f"/api/coach/email-deliveries/{delivery_id}/resend"
    )
    assert response.status_code == expected_status, response.text
    with db.SessionLocal() as session:
        event = session.get(NotificationEventDB, delivery_id)
        assert event.status == status
        assert event.failure_code == failure_code
        assert event.updated_at == before


def test_contact_correction_updates_authoritative_records_and_is_idempotent(
    authenticated_api_client,
):
    prepare(email="not-an-email")
    confirmed = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{REQUEST}/confirm",
        json={},
    )
    assert confirmed.status_code == 200, confirmed.text
    delivery = confirmed.json()["email_delivery"]
    assert delivery["status"] == "skipped"
    assert delivery["issue"] == "invalid_address"
    assert delivery["can_edit_contact"] is True

    invalid = authenticated_api_client.put(
        f"/api/coach/email-deliveries/{delivery['id']}/contact",
        json={"email": "still-not-an-email"},
    )
    assert invalid.status_code == 422
    assert "still-not-an-email" not in invalid.text

    corrected = authenticated_api_client.put(
        f"/api/coach/email-deliveries/{delivery['id']}/contact",
        json={"email": "new.client@example.com"},
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["id"] == delivery["id"]
    assert corrected.json()["status"] == "queued"
    assert corrected.json()["issue"] is None
    corrected_at = corrected.json()["updated_at"]

    replay = authenticated_api_client.put(
        f"/api/coach/email-deliveries/{delivery['id']}/contact",
        json={"email": "new.client@example.com"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["updated_at"] == corrected_at
    conflict = authenticated_api_client.put(
        f"/api/coach/email-deliveries/{delivery['id']}/contact",
        json={"email": "different@example.com"},
    )
    assert conflict.status_code == 409

    with db.SessionLocal() as session:
        event = session.get(NotificationEventDB, delivery["id"])
        intake = session.query(InviteIntakeDB).filter_by(invite_token=INVITE).one()
        request = session.query(LessonRequestDB).filter_by(request_token=REQUEST).one()
        client = session.get(ClientDB, "client_sofia")
        assert event.recipient == "new.client@example.com"
        assert event.status == "queued"
        assert event.attempt_count == 0
        assert intake.guardian_email == "new.client@example.com"
        assert request.client_email == "new.client@example.com"
        assert client.email == "new.client@example.com"
        assert session.query(NotificationEventDB).count() == 2


def test_concurrent_same_contact_correction_queues_once(authenticated_api_client):
    prepare(email="bad")
    confirmed = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{REQUEST}/confirm",
        json={},
    )
    delivery_id = confirmed.json()["email_delivery"]["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: n.correct_coach_email_delivery_contact(
                    "inst_001",
                    delivery_id,
                    "same@example.com",
                ),
                range(4),
            )
        )
    assert {result.id for result in results} == {delivery_id}
    assert {result.status for result in results} == {"queued"}
    assert len({result.updated_at for result in results}) == 1
    with db.SessionLocal() as session:
        event = session.get(NotificationEventDB, delivery_id)
        assert event.recipient == "same@example.com"
        assert event.attempt_count == 0
        assert session.query(NotificationEventDB).count() == 2


def test_concurrent_different_contact_correction_has_one_winner(authenticated_api_client):
    prepare(email="bad")
    confirmed = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{REQUEST}/confirm",
        json={},
    )
    delivery_id = confirmed.json()["email_delivery"]["id"]

    def correct(email):
        try:
            return n.correct_coach_email_delivery_contact("inst_001", delivery_id, email)
        except ApiException as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(correct, ["first@example.com", "second@example.com"]))
    successes = [result for result in results if not isinstance(result, ApiException)]
    conflicts = [result for result in results if isinstance(result, ApiException)]
    assert len(successes) == len(conflicts) == 1
    assert conflicts[0].status_code == 409
    with db.SessionLocal() as session:
        event = session.get(NotificationEventDB, delivery_id)
        intake = session.query(InviteIntakeDB).filter_by(invite_token=INVITE).one()
        request = session.query(LessonRequestDB).filter_by(request_token=REQUEST).one()
        client = session.get(ClientDB, "client_sofia")
        assert event.recipient in {"first@example.com", "second@example.com"}
        assert {event.recipient} == {intake.guardian_email, request.client_email, client.email}


def test_worker_and_contact_replay_serialize_on_event_lock(authenticated_api_client):
    prepare(email="bad")
    confirmed = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{REQUEST}/confirm",
        json={},
    )
    delivery_id = confirmed.json()["email_delivery"]["id"]
    n.correct_coach_email_delivery_contact(
        "inst_001",
        delivery_id,
        "worker-race@example.com",
    )
    with db.SessionLocal() as session:
        session.query(NotificationEventDB).filter(
            NotificationEventDB.id != delivery_id
        ).update({"status": "delivered"})
        event = session.get(NotificationEventDB, delivery_id)
        event.next_attempt_at = n.utcnow() - timedelta(seconds=1)
        session.commit()

    def replay_contact():
        try:
            return n.correct_coach_email_delivery_contact(
                "inst_001",
                delivery_id,
                "worker-race@example.com",
            )
        except ApiException as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        claim_future = pool.submit(n.claim_due, db.SessionLocal, Provider())
        contact_future = pool.submit(replay_contact)
        claim = claim_future.result()
        contact = contact_future.result()

    if claim is None:
        claim = n.claim_due(db.SessionLocal, Provider())
    assert claim is not None
    assert claim.event_id == delivery_id
    assert not isinstance(contact, ApiException) or contact.status_code == 409
    with db.SessionLocal() as session:
        event = session.get(NotificationEventDB, delivery_id)
        assert event.status == "processing"
        assert event.attempt_count == 1
        assert event.recipient == "worker-race@example.com"


def test_missing_intake_is_noneditable_and_never_fabricated(authenticated_api_client):
    with db.SessionLocal() as session:
        session.query(InviteIntakeDB).filter_by(invite_token=INVITE).delete()
        session.commit()
    delivery_id = _enqueue_invite_email(
        authenticated_api_client,
        "missing-intake",
        ensure_contact=False,
    )
    listed = authenticated_api_client.get("/api/coach/email-deliveries").json()[0]
    assert listed["id"] == delivery_id
    assert listed["status"] == "skipped"
    assert listed["client_name"] == "Client"
    assert listed["can_edit_contact"] is False
    response = authenticated_api_client.put(
        f"/api/coach/email-deliveries/{delivery_id}/contact",
        json={"email": "new.client@example.com"},
    )
    assert response.status_code == 409
    with db.SessionLocal() as session:
        assert session.query(InviteIntakeDB).filter_by(invite_token=INVITE).count() == 0


def test_empty_confirmation_snapshot_and_appointment_set_fail_closed(
    authenticated_api_client,
):
    prepare()
    confirmed = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{REQUEST}/confirm",
        json={},
    )
    assert confirmed.status_code == 200, confirmed.text
    delivery_id = confirmed.json()["email_delivery"]["id"]

    with db.SessionLocal() as session:
        event = session.get(NotificationEventDB, delivery_id)
        event.status = "failed"
        event.failure_code = "provider_retryable"
        event.attempt_count = 5
        event.template_context = {**event.template_context, "occurrences": []}
        request = session.query(LessonRequestDB).filter_by(request_token=REQUEST).one()
        session.query(AppointmentDB).filter_by(request_id=request.id).delete()
        session.commit()

    listed = authenticated_api_client.get("/api/coach/email-deliveries").json()[0]
    assert listed["id"] == delivery_id
    assert listed["source_state"] == "appointment_inactive"
    assert listed["can_resend"] is False
    assert authenticated_api_client.post(
        f"/api/coach/email-deliveries/{delivery_id}/resend"
    ).status_code == 409


@pytest.mark.parametrize("lifecycle_change", ["cancelled", "relocated", "extra"])
def test_confirmation_lifecycle_changes_disable_recovery(
    authenticated_api_client,
    lifecycle_change,
):
    prepare()
    confirmed = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{REQUEST}/confirm",
        json={},
    )
    assert confirmed.status_code == 200, confirmed.text
    delivery_id = confirmed.json()["email_delivery"]["id"]
    with db.SessionLocal() as session:
        event = session.get(NotificationEventDB, delivery_id)
        event.status = "failed"
        event.failure_code = "provider_retryable"
        event.attempt_count = 5
        request = session.query(LessonRequestDB).filter_by(request_token=REQUEST).one()
        appointment = session.query(AppointmentDB).filter_by(request_id=request.id).one()
        if lifecycle_change == "cancelled":
            appointment.status = "cancelled"
            appointment.cancelled_at = datetime.utcnow()
        elif lifecycle_change == "relocated":
            appointment.location_address = "A different location"
        else:
            session.add(
                AppointmentDB(
                    id="app_extra_occurrence",
                    instructor_id=request.instructor_id,
                    client_id=request.client_id or "client_sofia",
                    request_id=request.id,
                    title="Extra appointment",
                    start_time=appointment.start_time + timedelta(days=7),
                    end_time=appointment.end_time + timedelta(days=7),
                    location_address=appointment.location_address,
                    lat=appointment.lat,
                    lng=appointment.lng,
                    event_type="lesson",
                    status="scheduled",
                    is_break=False,
                )
            )
        session.commit()

    listed = authenticated_api_client.get("/api/coach/email-deliveries").json()[0]
    assert listed["source_state"] == "appointment_inactive"
    assert listed["can_resend"] is False
    assert authenticated_api_client.post(
        f"/api/coach/email-deliveries/{delivery_id}/resend"
    ).status_code == 409


@pytest.mark.parametrize("inactive", ["expired", "revoked"])
def test_inactive_invite_disables_recovery(authenticated_api_client, inactive):
    delivery_id = _enqueue_invite_email(authenticated_api_client, f"inactive-{inactive}")
    with db.SessionLocal() as session:
        event = session.get(NotificationEventDB, delivery_id)
        event.status = "failed"
        event.failure_code = "provider_retryable"
        event.attempt_count = 5
        invite = session.query(InviteTokenDB).filter_by(token=INVITE).one()
        if inactive == "expired":
            invite.expires_at = datetime.utcnow() - timedelta(seconds=1)
        else:
            invite.revoked_at = n.utcnow()
        session.commit()
    listed = authenticated_api_client.get("/api/coach/email-deliveries").json()[0]
    assert listed["source_state"] == "invite_inactive"
    assert listed["can_resend"] is False
    assert authenticated_api_client.post(
        f"/api/coach/email-deliveries/{delivery_id}/resend"
    ).status_code == 409


def test_cross_tenant_delivery_ids_are_indistinguishable_from_unknown(
    authenticated_api_client,
):
    delivery_id = _enqueue_invite_email(authenticated_api_client, "tenant-isolation")
    for candidate in (delivery_id, "unknown-delivery"):
        with pytest.raises(ApiException) as error:
            n.resend_coach_email_delivery("other-instructor", candidate)
        assert (error.value.status_code, error.value.code) == (404, "EMAIL_DELIVERY_NOT_FOUND")
    assert authenticated_api_client.post(
        "/api/coach/email-deliveries/unknown-delivery/resend"
    ).status_code == 404


def test_cross_tenant_linked_client_fails_closed(authenticated_api_client):
    prepare(email="bad")
    delivery_id = _enqueue_invite_email(
        authenticated_api_client,
        "cross-client",
        ensure_contact=False,
    )
    with db.SessionLocal() as session:
        session.add(
            InstructorDB(
                id="other-instructor",
                name="Other",
                main_address="Other pool",
                lat=0,
                lng=0,
            )
        )
        session.flush()
        session.add(
            ClientDB(
                id="other-client",
                instructor_id="other-instructor",
                name="Private other client",
            )
        )
        session.flush()
        session.query(InviteTokenDB).filter_by(token=INVITE).one().client_id = "other-client"
        session.commit()
    listed = authenticated_api_client.get("/api/coach/email-deliveries").json()[0]
    assert listed["client_id"] is None
    assert listed["client_name"] == "Sofia Martinez"
    assert listed["can_edit_contact"] is False
    assert "Private other client" not in str(listed)
    assert authenticated_api_client.put(
        f"/api/coach/email-deliveries/{delivery_id}/contact",
        json={"email": "safe@example.com"},
    ).status_code == 409


def test_email_delivery_routes_require_authentication(api_client):
    for method, path, kwargs in [
        ("get", "/api/coach/email-deliveries", {}),
        ("post", "/api/coach/email-deliveries/unknown/resend", {}),
        ("put", "/api/coach/email-deliveries/unknown/contact", {"json": {"email": "x@example.com"}}),
    ]:
        assert getattr(api_client, method)(path, **kwargs).status_code == 401


def _enqueue_invite_email(
    authenticated_api_client,
    key: str,
    *,
    ensure_contact: bool = True,
) -> str:
    if ensure_contact:
        prepare()
    response = authenticated_api_client.post(
        f"/api/coach/invites/{INVITE}/notifications",
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    return response.json()["email_delivery"]["id"]


def _assert_private_projection(payload: dict, *, optional_nulls: bool = False) -> None:
    if optional_nulls:
        assert set(payload) <= DELIVERY_KEYS
        assert DELIVERY_KEYS - {"client_id", "request_token", "issue"} <= set(payload)
    else:
        assert set(payload) == DELIVERY_KEYS
    assert PRIVATE_KEYS.isdisjoint(payload)
