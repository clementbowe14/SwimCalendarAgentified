from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app import db, notifications as n
from app.models import InviteIntakeDB, InviteTokenDB, NotificationEventDB, LessonRequestDB, AppointmentDB


INVITE = "invite_sofia_0826"
REQUEST = "req_sofia_pending_0820"


class Provider:
    supports_idempotent_send = True
    timeout_seconds = 30

    def __init__(self, outcome="accepted"):
        self.outcome = outcome
        self.calls = []

    def send(self, message, idempotency_key):
        self.calls.append((message, idempotency_key))
        return n.ProviderResult(self.outcome)


def prepare(phone="+15125551234", email="guardian@example.com"):
    with db.SessionLocal() as s:
        invite = s.query(InviteTokenDB).filter_by(token=INVITE).one()
        intake = s.query(InviteIntakeDB).filter_by(invite_token=INVITE).first()
        if intake is None:
            intake = InviteIntakeDB(id="notify-intake", invite_token=INVITE, client_name="Guardian", location_address="Pool")
            s.add(intake)
        intake.guardian_phone = phone
        intake.guardian_email = email
        intake.notes = "PRIVATE NOTES"
        instructor = invite.instructor_id
        s.commit()
    return instructor


def events():
    with db.SessionLocal() as s:
        rows = s.query(NotificationEventDB).order_by(NotificationEventDB.channel).all()
        s.expunge_all()
        return rows


@pytest.mark.parametrize("phone,email,expected", [(None,None,["skipped","skipped"]), ("5125551234","guardian@example.com",["skipped","queued"]), ("+15125551234","bad",["queued","skipped"]), ("bad","bad",["skipped","skipped"])])
def test_recipient_validation_and_snapshot(migrated_postgres_db, phone, email, expected):
    instructor = prepare(phone,email)
    result = n.enqueue_invite(instructor,INVITE,"first")
    assert [r.status for r in result] == expected
    prepare()
    assert [r.status for r in n.enqueue_invite(instructor,INVITE,"first")] == expected
    assert [r.status for r in n.enqueue_invite(instructor,INVITE,"second")] == ["queued","queued"]
    assert all("PRIVATE" not in str(e.template_context) for e in events())


def test_concurrent_invite_replays(migrated_postgres_db):
    instructor = prepare()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: n.enqueue_invite(instructor,INVITE,"same"),range(4)))
    assert len(events()) == 2
    assert all(len(r)==2 for r in results)


def test_invite_conflict_revocation_and_expiry(migrated_postgres_db):
    from app.api_errors import IdempotencyConflict, InviteNotFound, InviteExpired
    instructor = prepare()
    n.enqueue_invite(instructor,INVITE,"same")
    with db.SessionLocal() as s:
        original=s.query(InviteTokenDB).filter_by(token=INVITE).one()
        s.add(InviteTokenDB(token="other",instructor_id=instructor,business_name="Pool",location_label="Pool",expires_at=original.expires_at))
        s.commit()
    with pytest.raises(IdempotencyConflict):
        n.enqueue_invite(instructor,"other","same")
    with db.SessionLocal() as s:
        invite=s.query(InviteTokenDB).filter_by(token=INVITE).one()
        invite.expires_at=n.utcnow().replace(tzinfo=None)-timedelta(seconds=1)
        s.commit()
    with pytest.raises(InviteExpired):
        n.enqueue_invite(instructor,INVITE,"same")
    with db.SessionLocal() as s:
        s.query(InviteTokenDB).filter_by(token=INVITE).one().revoked_at=n.utcnow()
        s.commit()
    with pytest.raises(InviteNotFound):
        n.enqueue_invite(instructor,INVITE,"same")


@pytest.mark.parametrize("outcome,supported,status", [("accepted",True,"accepted"),("delivered",True,"delivered"),("permanent_failure",True,"failed"),("retryable_failure",False,"queued"),("unknown",True,"queued"),("unknown",False,"failed")])
def test_provider_outcomes(migrated_postgres_db,outcome,supported,status):
    instructor=prepare(email=None)
    n.enqueue_invite(instructor,INVITE,"provider")
    provider=Provider(outcome)
    provider.supports_idempotent_send=supported
    assert n.process_one(provider)
    row=next(e for e in events() if e.channel=="sms")
    assert row.status==status
    assert row.attempt_count==1
    assert provider.calls[0][1]==row.id


def test_five_attempts_backoff_and_no_early_send(migrated_postgres_db):
    instructor=prepare(email=None)
    n.enqueue_invite(instructor,INVITE,"retry")
    provider=Provider("retryable_failure")
    now=n.utcnow()
    for attempt in range(1,6):
        assert n.process_one(provider,now=now)
        row=next(e for e in events() if e.channel=="sms")
        assert row.attempt_count==attempt
        assert not n.process_one(provider,now=now)
        if attempt<5:
            assert row.next_attempt_at==now+timedelta(seconds=60*2**(attempt-1))
            now=row.next_attempt_at
    assert row.status=="failed"
    assert len(provider.calls)==5
    assert len({key for _,key in provider.calls})==1


@pytest.mark.parametrize("supported",[True,False])
def test_expired_lease_and_stale_completion(migrated_postgres_db,supported):
    instructor=prepare(email=None)
    n.enqueue_invite(instructor,INVITE,"lease")
    provider=Provider()
    provider.supports_idempotent_send=supported
    now=n.utcnow()
    claim=n.claim_due(db.SessionLocal,provider,now)
    assert claim
    assert n.claim_due(db.SessionLocal,provider,now+timedelta(seconds=119)) is None
    assert n.claim_due(db.SessionLocal,provider,now+timedelta(seconds=121)) is None
    assert not n.complete_claim(db.SessionLocal,provider,claim,n.ProviderResult("delivered"))
    row=next(e for e in events() if e.channel=="sms")
    assert row.status==("queued" if supported else "failed")
    assert row.failure_code=="outcome_unknown"
    if supported:
        assert n.process_one(provider,now=row.next_attempt_at)


def test_unconfigured_and_concurrent_claims(migrated_postgres_db):
    instructor=prepare(email=None)
    n.enqueue_invite(instructor,INVITE,"claim")
    assert not n.process_one()
    assert all(e.attempt_count==0 for e in events())
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims=list(pool.map(lambda _: n.claim_due(db.SessionLocal,Provider()),range(4)))
    assert sum(c is not None for c in claims)==1


def test_confirmation_atomicity_and_public_privacy(authenticated_api_client, monkeypatch):
    prepare()
    original=n.enqueue_confirmation
    def fail(s,r,a):
        original(s,r,a)
        raise RuntimeError("between enqueue and commit")
    monkeypatch.setattr(n,"enqueue_confirmation",fail)
    assert authenticated_api_client.post(f"/api/coach/lesson-requests/{REQUEST}/confirm",json={}).status_code==500
    assert events()==[]
    with db.SessionLocal() as s:
        r=s.query(LessonRequestDB).filter_by(request_token=REQUEST).one()
        assert r.status=="pending"
        assert s.query(AppointmentDB).filter_by(request_id=r.id).count()==0
    monkeypatch.setattr(n,"enqueue_confirmation",original)
    for _ in range(2):
        response=authenticated_api_client.post(f"/api/coach/lesson-requests/{REQUEST}/approve")
        assert response.status_code==200,response.text
    assert len(events())==2
    for path in ("requests","lesson-requests"):
        response=authenticated_api_client.get(f"/api/public/{path}/{REQUEST}")
        assert response.status_code==200,response.text
        statuses=response.json()["notifications"]
        assert [x["channel"] for x in statuses]==["sms","email"]
        assert all(set(x)=={"kind","channel","status","updated_at"} for x in statuses)


def test_invite_endpoint_auth_key_and_override(authenticated_api_client):
    prepare()
    path=f"/api/coach/invites/{INVITE}/notifications"
    assert authenticated_api_client.post(path).status_code==422
    assert authenticated_api_client.post(path,headers={"Idempotency-Key":"x"},json={"recipient":"other@example.com"}).status_code==422
    response=authenticated_api_client.post(path,headers={"Idempotency-Key":"x"})
    assert response.status_code==202,response.text


def test_tenant_foreign_key(migrated_postgres_db):
    instructor=prepare()
    n.enqueue_invite(instructor,INVITE,"tenant")
    with db.SessionLocal() as s:
        row=s.query(NotificationEventDB).first()
        row.instructor_id="nonexistent"
        with pytest.raises(IntegrityError):
            s.commit()


@pytest.mark.parametrize("state",["queued","processing","accepted","delivered","failed","skipped"])
def test_every_public_state(authenticated_api_client,state):
    prepare()
    assert authenticated_api_client.post(f"/api/coach/lesson-requests/{REQUEST}/approve").status_code==200
    with db.SessionLocal() as s:
        s.query(NotificationEventDB).update({"status":state})
        s.commit()
    for route in ("requests","lesson-requests"):
        response=authenticated_api_client.get(f"/api/public/{route}/{REQUEST}")
        assert response.status_code==200,response.text
        assert [r["status"] for r in response.json()["notifications"]]==[state,state]


def test_fifth_claim_crash_cannot_send_sixth_attempt(migrated_postgres_db):
    instructor=prepare(email=None)
    n.enqueue_invite(instructor,INVITE,"fifth")
    with db.SessionLocal() as s:
        s.query(NotificationEventDB).filter_by(channel="sms").one().attempt_count=4
        s.commit()
    provider=Provider()
    now=n.utcnow()
    assert n.claim_due(db.SessionLocal,provider,now)
    assert not n.process_one(provider,now=now+timedelta(seconds=121))
    row=next(e for e in events() if e.channel=="sms")
    assert (row.status,row.attempt_count)==("failed",5)
    assert not provider.calls


def test_crash_after_acceptance_reuses_provider_key(migrated_postgres_db):
    instructor=prepare(email=None)
    n.enqueue_invite(instructor,INVITE,"accepted-crash")
    provider=Provider()
    now=n.utcnow()
    claim=n.claim_due(db.SessionLocal,provider,now)
    provider.send(claim.message,claim.event_id)
    n.claim_due(db.SessionLocal,provider,now+timedelta(seconds=121))
    row=next(e for e in events() if e.channel=="sms")
    assert n.process_one(provider,now=row.next_attempt_at)
    assert [key for _,key in provider.calls]==[claim.event_id,claim.event_id]


def test_invalid_adapter_timeout_consumes_no_attempt(migrated_postgres_db):
    instructor=prepare()
    n.enqueue_invite(instructor,INVITE,"timeout")
    provider=Provider()
    provider.timeout_seconds=31
    with pytest.raises(ValueError):
        n.process_one(provider)
    assert all(e.attempt_count==0 for e in events())


def test_channel_insert_failure_rolls_back(migrated_postgres_db,monkeypatch):
    from sqlalchemy.orm import Session
    instructor=prepare()
    original=Session.add
    def fail(self,instance,*args,**kwargs):
        if isinstance(instance,NotificationEventDB) and instance.channel=="email":
            raise RuntimeError("second channel")
        return original(self,instance,*args,**kwargs)
    monkeypatch.setattr(Session,"add",fail)
    with pytest.raises(RuntimeError):
        n.enqueue_invite(instructor,INVITE,"rollback")
    assert not events()


def test_no_intake_no_history_on_reads_or_legacy_retry(authenticated_api_client):
    with db.SessionLocal() as s:
        s.query(InviteIntakeDB).filter_by(invite_token=INVITE).delete()
        s.query(LessonRequestDB).filter_by(request_token=REQUEST).one().status="confirmed"
        s.commit()
    for route in ("requests","lesson-requests"):
        response=authenticated_api_client.get(f"/api/public/{route}/{REQUEST}")
        assert response.status_code==200
        assert response.json()["notifications"]==[]
    assert authenticated_api_client.post(f"/api/coach/lesson-requests/{REQUEST}/approve").status_code==200
    assert not events()
    with db.SessionLocal() as s:
        instructor=s.query(InviteTokenDB).filter_by(token=INVITE).one().instructor_id
    statuses=n.enqueue_invite(instructor,INVITE,"no-intake")
    assert [r.status for r in statuses]==["skipped","skipped"]
    assert all(e.failure_code=="missing_recipient" for e in events())


def test_weekly_confirmation_one_message_per_channel(authenticated_api_client):
    from tests.test_coach_lesson_request_approval import _create_weekly_offer_id
    prepare()
    slot_id=_create_weekly_offer_id(authenticated_api_client)
    created = authenticated_api_client.post(
        f"/api/public/client-invites/{INVITE}/lesson-requests", json={"slot_id": slot_id})
    assert created.status_code == 200, created.text
    request_token = created.json()["id"]
    response=authenticated_api_client.post(f"/api/coach/lesson-requests/{request_token}/confirm",json={})
    assert response.status_code==200,response.text
    assert len(events())==2
    assert all(len(e.template_context["occurrences"])==4 for e in events())


def test_failed_booking_has_no_events(authenticated_api_client):
    from tests.test_coach_lesson_request_approval import _add_conflicting_appointment
    prepare()
    _add_conflicting_appointment(REQUEST)
    assert authenticated_api_client.post(f"/api/coach/lesson-requests/{REQUEST}/approve").status_code==409
    assert not events()


def test_revoked_parent_hides_legacy_request(authenticated_api_client):
    with db.SessionLocal() as s:
        s.query(InviteTokenDB).filter_by(token=INVITE).one().revoked_at=n.utcnow()
        s.commit()
    for token in (REQUEST,"unknown"):
        response=authenticated_api_client.get(f"/api/public/requests/{token}")
        assert response.status_code==404
        assert response.json()["error"]["code"]=="LESSON_REQUEST_NOT_FOUND"


def test_queued_without_due_time_can_be_processed(migrated_postgres_db):
    instructor = prepare(email=None)
    n.enqueue_invite(instructor, INVITE, "no-due-time")
    with db.SessionLocal() as session:
        session.query(NotificationEventDB).filter_by(channel="sms").one().next_attempt_at = None
        session.commit()
    assert n.process_one(Provider())
    assert next(e for e in events() if e.channel == "sms").status == "accepted"


def test_concurrent_confirmation_creates_only_two_events(migrated_postgres_db):
    from app.api_services import confirm_coach_lesson_request
    from app.api_schemas import CoachLessonRequestConfirmRequest
    instructor = prepare()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: confirm_coach_lesson_request(
            instructor, REQUEST, CoachLessonRequestConfirmRequest()), range(3)))
    assert len({r.appointment_id for r in results}) == 1
    assert len(events()) == 2


def test_concurrent_same_key_different_invites_conflicts(migrated_postgres_db):
    from app.api_errors import IdempotencyConflict
    instructor = prepare()
    with db.SessionLocal() as session:
        original = session.query(InviteTokenDB).filter_by(token=INVITE).one()
        session.add(InviteTokenDB(token="competing", instructor_id=instructor,
            business_name="Swim", location_label="Pool", expires_at=original.expires_at))
        session.commit()
    def enqueue(token):
        try:
            n.enqueue_invite(instructor, token, "competing-key")
            return "created"
        except IdempotencyConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(enqueue, [INVITE, "competing"])) == ["conflict", "created"]
    assert len(events()) == 2


def test_independent_tenants_can_reuse_key_but_cannot_access_other_invite(migrated_postgres_db):
    from app.models import InstructorDB
    from app.api_errors import InviteNotFound
    instructor = prepare()
    with db.SessionLocal() as session:
        original = session.query(InviteTokenDB).filter_by(token=INVITE).one()
        session.add(InstructorDB(id="other-coach", name="Other", main_address="Pool", lat=0, lng=0))
        session.flush()
        session.add(InviteTokenDB(token="other-tenant", instructor_id="other-coach",
            business_name="Swim", location_label="Pool", expires_at=original.expires_at))
        session.commit()
    n.enqueue_invite(instructor, INVITE, "shared-key")
    n.enqueue_invite("other-coach", "other-tenant", "shared-key")
    assert len(events()) == 4
    with pytest.raises(InviteNotFound):
        n.enqueue_invite("other-coach", INVITE, "shared-key")


def test_delivery_timestamp_requires_awareness_and_serializes_utc():
    from app.api_schemas import NotificationStatus
    from pydantic import ValidationError
    args = dict(kind="confirmation", channel="email", status="queued")
    with pytest.raises(ValidationError):
        NotificationStatus(**args, updated_at="2026-09-20T09:00:00")
    status = NotificationStatus(**args, updated_at="2026-09-20T09:00:00-04:00")
    assert status.model_dump(mode="json")["updated_at"] == "2026-09-20T13:00:00Z"


def test_recipient_and_actionable_invite_context_are_immutable_on_replay(migrated_postgres_db, monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    monkeypatch.setenv("SWIM_CLIENT_INVITE_ORIGIN", "https://lessons.example")
    instructor = prepare(email=None)
    n.enqueue_invite(instructor, INVITE, "snapshot")
    first = next(e for e in events() if e.channel == "sms")
    prepare(phone="+442079460000", email=None)
    monkeypatch.setenv("SWIM_CLIENT_INVITE_ORIGIN", "https://new.example")
    n.enqueue_invite(instructor, INVITE, "snapshot")
    replay = next(e for e in events() if e.channel == "sms")
    assert replay.recipient == first.recipient
    assert replay.template_context == first.template_context
    provider = Provider()
    assert n.process_one(provider)
    message = provider.calls[0][0]
    url = urlsplit(message.context["invite_url"])
    assert (url.scheme, url.netloc) == ("https", "lessons.example")
    assert parse_qs(url.query) == {"invite_token": [INVITE]}
    n.enqueue_invite(instructor, INVITE, "new-snapshot")
    assert any(e.recipient == "+442079460000" and "new.example" in e.template_context["invite_url"] for e in events())


@pytest.mark.parametrize("origin", ["", "http://untrusted.example", "https://user:secret@host.example", "https://host.example/path", "https://host.example?redirect=x"])
def test_production_invite_origin_must_be_trusted(migrated_postgres_db, monkeypatch, origin):
    from app.api_errors import ApiException
    instructor = prepare()
    monkeypatch.setenv("SWIM_ENV", "production")
    monkeypatch.setenv("SWIM_CLIENT_INVITE_ORIGIN", origin)
    with pytest.raises(ApiException, match="origin"):
        n.enqueue_invite(instructor, INVITE, "bad-origin")
    assert not events()


@pytest.mark.parametrize("malformed", [False, True])
def test_provider_io_has_committed_unlocked_claim_and_sanitizes_unknown(migrated_postgres_db, malformed):
    from sqlalchemy import text
    instructor = prepare(email=None)
    n.enqueue_invite(instructor, INVITE, "boundary")
    class BrokenProvider(Provider):
        supports_idempotent_send = False
        def send(self, message, idempotency_key):
            with db.SessionLocal() as session:
                session.execute(text("SET LOCAL lock_timeout = '500ms'"))
                row = session.query(NotificationEventDB).filter_by(id=idempotency_key).with_for_update().one()
                assert (row.status, row.attempt_count) == ("processing", 1)
            if malformed:
                return {"status": "delivered"}
            raise RuntimeError("SECRET provider recipient@example.com")
    assert n.process_one(BrokenProvider())
    row = next(e for e in events() if e.channel == "sms")
    assert row.status == "failed"
    assert row.failure_code == "outcome_unknown"
    assert row.provider_message_id is None


def test_stale_completion_cannot_overwrite_active_replacement(migrated_postgres_db):
    instructor = prepare(email=None)
    n.enqueue_invite(instructor, INVITE, "replacement")
    provider = Provider()
    now = n.utcnow()
    old = n.claim_due(db.SessionLocal, provider, now)
    n.claim_due(db.SessionLocal, provider, now + timedelta(seconds=121))
    row = next(e for e in events() if e.channel == "sms")
    replacement = n.claim_due(db.SessionLocal, provider, row.next_attempt_at)
    assert replacement and replacement.attempt_token != old.attempt_token
    assert not n.complete_claim(db.SessionLocal, provider, old, n.ProviderResult("delivered"))
    assert next(e for e in events() if e.channel == "sms").status == "processing"
    assert n.complete_claim(db.SessionLocal, provider, replacement, n.ProviderResult("accepted"))


def test_composite_constraints_reject_existing_but_mismatched_owners(authenticated_api_client):
    from app.models import InstructorDB
    instructor = prepare()
    assert authenticated_api_client.post(f"/api/coach/lesson-requests/{REQUEST}/confirm", json={}).status_code == 200
    with db.SessionLocal() as session:
        invite = session.query(InviteTokenDB).filter_by(token=INVITE).one()
        session.add(InstructorDB(id="valid-other", name="Other", main_address="Pool", lat=0, lng=0))
        session.add(InviteTokenDB(token="valid-other-invite", instructor_id=instructor,
            business_name="Other", location_label="Pool", expires_at=invite.expires_at))
        session.commit()
    for field, value, constraint in [
        ("instructor_id", "valid-other", "fk_notifications_invite_tenant"),
        ("invite_token", "valid-other-invite", "fk_notifications_request_tenant"),
    ]:
        with db.SessionLocal() as session:
            row = session.query(NotificationEventDB).first()
            setattr(row, field, value)
            with pytest.raises(IntegrityError, match=constraint):
                session.commit()


@pytest.mark.parametrize("route", ["legacy", "client"])
def test_create_replay_after_confirmation_preserves_delivery_summaries(authenticated_api_client, route):
    from tests.test_public_client_invite_api import _create_slot_offer
    prepare()
    slot = _create_slot_offer(authenticated_api_client)["slots"][0]
    if route == "client":
        path = f"/api/public/client-invites/{INVITE}/lesson-requests"
        body = {"slot_id": slot["id"], "idempotency_key": "delivery-replay-client"}
        token_field = "id"
    else:
        path = f"/api/public/invites/{INVITE}/requests"
        body = {"client_name": "Sofia Martinez", "client_address": slot["location_address"],
                "requested_date": "2026-08-20", "slot_start": slot["start_iso"],
                "duration_mins": 45, "location_type": "home", "idempotency_key": "delivery-replay-legacy"}
        token_field = "request_token"
    created = authenticated_api_client.post(path, json=body)
    assert created.status_code == 200, created.text
    token = created.json()[token_field]
    confirmed = authenticated_api_client.post(f"/api/coach/lesson-requests/{token}/confirm", json={})
    assert confirmed.status_code == 200, confirmed.text
    replay = authenticated_api_client.post(path, json=body)
    assert replay.status_code == 200, replay.text
    summaries = replay.json()["notifications"]
    assert [item["channel"] for item in summaries] == ["sms", "email"]
    assert all(set(item) == {"kind", "channel", "status", "updated_at"} for item in summaries)
    assert len(events()) == 2
