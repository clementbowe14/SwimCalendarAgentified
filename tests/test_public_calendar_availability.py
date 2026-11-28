from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timezone
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import availability_search, client_invites, db, tools
from app.auth import seed_demo_provider_account
from app.calendar_connections import replace_google_calendar_connection
from app.calendar_credentials import CalendarCredentialCipher
from app.external_calendar import CalendarBusyInterval, CalendarBusySnapshot, ExternalCalendarTimeout
from app.google_calendar_busy_source_factory import GoogleCalendarBusySourceFactory
from app.models import InstructorDB, InviteSlotOfferDB, InviteTokenDB, LessonRequestDB, ProviderAccountDB, ProviderWorkingHoursDB
from app.public_tokens import _replay_lesson_request, require_active_invite
from tests.public_contract_safety import PUBLIC_SCHEDULING_EVIDENCE_KEYS, flatten_keys


INVITE = db.SOFIA_INVITE_TOKEN
SLOTS_PATH = f"/api/public/client-invites/{INVITE}/slots"
RANGE_PATH = f"/api/public/client-invites/{INVITE}/availability/search"
LEGACY_PATH = f"/api/public/invites/{INVITE}/availability"
ADDRESS = "450 North Blvd, Austin, TX"
BASE_PAYLOAD = {"client_name": "Sofia Martinez", "requested_date": "2026-08-20", "duration_mins": 45}
SLOT_PAYLOAD = {
    **BASE_PAYLOAD,
    "location": {"address": ADDRESS, "is_instructor_facility": False},
    "instructor_id": "inst_untrusted",
}
LEGACY_PAYLOAD = {**BASE_PAYLOAD, "client_address": ADDRESS}
RANGE_PAYLOAD = {"client_name": "Client for range search", "duration_mins": 45, "is_instructor_facility": True}
BUSY_START = datetime(2026, 8, 20, 18, tzinfo=timezone.utc)
BUSY_END = datetime(2026, 8, 20, 21, tzinfo=timezone.utc)
UNAVAILABLE = {
    "error": {
        "code": "CALENDAR_AVAILABILITY_UNAVAILABLE",
        "message": "Calendar availability could not be verified.",
    }
}


def _connect() -> None:
    cipher = CalendarCredentialCipher(active_key_id="test-key", keys={"test-key": b"k" * 32})
    with db.SessionLocal.begin() as session:
        account_id = session.query(ProviderAccountDB.id).filter_by(instructor_id="inst_001").scalar()
        replace_google_calendar_connection(
            session, account_id=account_id, refresh_token="fake-refresh-token", cipher=cipher,
            now=BUSY_START,
        )


def _mock_google(monkeypatch, query: Mock) -> Mock:
    factory = Mock(return_value=SimpleNamespace(query_busy_periods=query))
    monkeypatch.setattr(GoogleCalendarBusySourceFactory, "create_for", factory)
    return factory


def _set_range_clock(monkeypatch, instant):
    monkeypatch.setattr(
        client_invites, "search_one_time_availability",
        partial(availability_search.search_one_time_availability, now=datetime.fromisoformat(instant)),
    )


def test_range_search_defaults_to_provider_dates_and_persists_exact_later_day(api_client, monkeypatch):
    # UTC has reached December 1 while it is still November 30 for the provider.
    _set_range_clock(monkeypatch, "2026-12-01T01:00:00+00:00")
    _connect()
    query = Mock(return_value=CalendarBusySnapshot("verified", (
        CalendarBusyInterval(
            datetime.fromisoformat("2026-12-02T14:00:00-06:00"),
            datetime.fromisoformat("2026-12-02T14:45:00-06:00"),
        ),
    )))
    _mock_google(monkeypatch, query)

    payload = {**RANGE_PAYLOAD, "limit": 2, "windows": [
        {"days": ["tuesday", "wednesday", "thursday"], "start_time": "14:00", "end_time": "14:45"},
    ]}
    response = api_client.post(RANGE_PATH, json=payload)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    result = response.json()
    assert set(result) == {"start_date", "end_date", "duration_mins", "slots"}
    assert result["start_date"] == "2026-11-30"
    assert result["end_date"] == "2026-12-13"
    assert result["duration_mins"] == 45
    slots = result["slots"]
    assert [slot["start_iso"] for slot in slots] == ["2026-12-01T14:00:00-06:00", "2026-12-03T14:00:00-06:00"]
    assert all(set(slot) == {"id", "start_iso", "end_iso", "display", "location_address"} for slot in slots)
    query.assert_called_once()

    later = slots[-1]
    with db.SessionLocal() as session:
        offer = session.get(InviteSlotOfferDB, later["id"])
        assert offer.requested_date == date(2026, 12, 3)
        assert (offer.recurrence, offer.recurrence_count, offer.recurrence_available) == ("single", 1, False)
        assert (offer.invite_token, offer.instructor_id) == (INVITE, "inst_001")

    other_path = f"/api/public/client-invites/{db.DEMO_INVITE_TOKEN}/lesson-requests"
    create_path = f"/api/public/client-invites/{INVITE}/lesson-requests"
    request = {"slot_id": later["id"], "idempotency_key": "range-selected-later-day"}
    assert api_client.post(other_path, json=request).status_code == 404
    check_slot = Mock(wraps=tools.find_available_swim_slots)
    monkeypatch.setattr(tools, "find_available_swim_slots", check_slot)
    submitted = api_client.post(create_path, json=request)
    assert submitted.status_code == 200, submitted.text
    replay = api_client.post(create_path, json=request)
    assert replay.json()["id"] == submitted.json()["id"]
    assert submitted.json()["slot"]["start_iso"] == later["start_iso"]
    assert check_slot.call_args.kwargs["requested_start"] == datetime.fromisoformat(later["start_iso"])
    with db.SessionLocal() as session:
        record = session.query(LessonRequestDB).filter_by(request_token=submitted.json()["id"]).one()
        assert record.requested_date == date(2026, 12, 3)


def test_range_offer_ids_preserve_location_mode_at_the_same_address(api_client, monkeypatch):
    _set_range_clock(monkeypatch, "2026-12-01T01:00:00+00:00")
    payload = {**RANGE_PAYLOAD, "limit": 1, "windows": [
        {"days": ["tuesday"], "start_time": "14:00", "end_time": "14:45"},
    ]}
    in_facility = api_client.post(RANGE_PATH, json=payload)
    assert in_facility.status_code == 200, in_facility.text
    facility = in_facility.json()["slots"][0]
    home_coords = (30.28, -97.75)
    monkeypatch.setattr(tools.routing_provider, "geocode", lambda _address: home_coords)
    at_home = api_client.post(RANGE_PATH, json={
        **payload, "is_instructor_facility": False, "client_address": facility["location_address"],
    })
    assert at_home.status_code == 200, at_home.text
    home = at_home.json()["slots"][0]
    assert home["start_iso"] == facility["start_iso"]
    assert home["id"] != facility["id"]
    with db.SessionLocal() as session:
        original_facility = session.get(InviteSlotOfferDB, facility["id"])
        stored_home = session.get(InviteSlotOfferDB, home["id"])
        assert original_facility.location_type == "facility"
        assert stored_home.location_type == "home"
        assert (stored_home.lat, stored_home.lng) == home_coords


@pytest.mark.parametrize(("change", "status", "code"), [
    ("revoked", 404, "INVITE_NOT_FOUND"),
    ("invite_expired", 410, "INVITE_EXPIRED"),
    ("offer_expired", 404, "SLOT_OFFER_NOT_FOUND"),
])
def test_redemption_rechecks_invite_and_offer_before_writing(api_client, monkeypatch, change, status, code):
    _set_range_clock(monkeypatch, "2026-12-01T01:00:00+00:00")
    result = api_client.post(RANGE_PATH, json={**RANGE_PAYLOAD, "limit": 1})
    assert result.status_code == 200, result.text
    slot_id = result.json()["slots"][0]["id"]
    validate_occurrences = client_invites._validate_offer_occurrences

    def validate_then_change(*args):
        validate_occurrences(*args)
        now = datetime.utcnow()
        with db.SessionLocal.begin() as session:
            if change == "offer_expired":
                session.get(InviteSlotOfferDB, slot_id).expires_at = now
            else:
                invite = session.get(InviteTokenDB, INVITE)
                if change == "revoked":
                    invite.revoked_at = now
                else:
                    invite.expires_at = now

    monkeypatch.setattr(client_invites, "_validate_offer_occurrences", validate_then_change)
    key = "range-selected-slot-invite-liveness"
    response = api_client.post(f"/api/public/client-invites/{INVITE}/lesson-requests", json={
        "slot_id": slot_id, "idempotency_key": key,
    })
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    with db.SessionLocal() as session:
        assert session.query(LessonRequestDB).filter_by(idempotency_key=key).first() is None


def test_later_fold_offer_preserves_the_selected_instant(api_client, monkeypatch):
    _set_range_clock(monkeypatch, "2026-11-01T05:00:00+00:00")
    with db.SessionLocal.begin() as session:
        sunday = session.get(ProviderWorkingHoursDB, ("inst_001", 6))
        sunday.enabled, sunday.start_time, sunday.end_time = True, time(0, 30), time(4)
    found = api_client.post(RANGE_PATH, json={
        **RANGE_PAYLOAD, "start_date": "2026-11-01", "end_date": "2026-11-01", "duration_mins": 30,
        "windows": [{"days": ["sunday"], "start_time": "01:00", "end_time": "02:00"}],
    })
    assert found.status_code == 200, found.text
    by_start = {slot["start_iso"]: slot for slot in found.json()["slots"]}
    assert by_start["2026-11-01T01:00:00-05:00"]["display"].endswith("CDT")
    later = by_start["2026-11-01T01:00:00-06:00"]
    assert later["display"].endswith("CST")

    response = api_client.post(f"/api/public/client-invites/{INVITE}/lesson-requests", json={"slot_id": later["id"]})
    assert response.status_code == 200, response.text
    assert response.json()["slot"]["start_iso"] == later["start_iso"]


def test_range_validation_keeps_contact_inputs_out_of_the_response(api_client):
    address = "Private client address"
    response = api_client.post(RANGE_PATH, json={
        **RANGE_PAYLOAD, "is_instructor_facility": False, "client_address": address,
        "start_date": "2026-12-03", "end_date": "2026-12-01",
    })
    assert response.status_code == 422
    assert response.headers["cache-control"] == "private, no-store"
    assert all(value not in response.text for value in (address, RANGE_PAYLOAD["client_name"]))


@pytest.mark.parametrize("field", ["invite_token", "instructor_id", "original_slot_offer_id"])
def test_request_replay_requires_the_same_invite_provider_and_offer(field):
    context = {
        "invite_token": "invite-for-client",
        "instructor_id": "provider-for-client",
        "original_slot_offer_id": "selected-offer",
    }
    saved = LessonRequestDB(**context)
    submitted = LessonRequestDB(**{**context, field: "different"})
    with pytest.raises(db.IdempotencyKeyConflictError):
        _replay_lesson_request(saved, submitted)


@pytest.mark.parametrize(("path", "payload", "field"), [
    (SLOTS_PATH, SLOT_PAYLOAD, "slots"),
    (LEGACY_PATH, LEGACY_PAYLOAD, "available_slots"),
])
def test_public_search_excludes_busy_slots_without_explaining_why(
    api_client, monkeypatch, assert_public_payload_safe, path, payload, field
):
    query = Mock(return_value=CalendarBusySnapshot(
        "verified", (CalendarBusyInterval(BUSY_START, BUSY_END),)
    ))
    factory = _mock_google(monkeypatch, query)
    baseline = api_client.post(path, json=payload)
    assert baseline.status_code == 200, baseline.text
    factory.assert_not_called()

    _connect()
    response = api_client.post(path, json=payload)

    assert response.status_code == 200, response.text
    data = response.json()
    assert 0 < len(data[field]) < len(baseline.json()[field])
    assert_public_payload_safe(data)
    assert PUBLIC_SCHEDULING_EVIDENCE_KEYS.isdisjoint(flatten_keys(data))
    for slot in data[field]:
        start = datetime.fromisoformat(slot["start_iso"])
        end = datetime.fromisoformat(slot["end_iso"])
        assert end <= BUSY_START or start >= BUSY_END
    query.assert_called_once()
    assert query.call_args.kwargs["instructor_id"] == "inst_001"


@pytest.mark.parametrize("unavailable", [False, True], ids=["busy-fourth-week", "timeout-fourth-week"])
def test_weekly_public_search_checks_each_needed_week_before_saving(
    api_client, monkeypatch, unavailable
):
    _connect()
    days = []
    load_policy = Mock(wraps=client_invites.load_buffer_policy)
    monkeypatch.setattr(client_invites, "load_buffer_policy", load_policy)

    def query_busy(*, instructor_id, window_start, window_end):
        days.append(window_start.date().isoformat())
        if len(days) < 4:
            return CalendarBusySnapshot("verified", ())
        if unavailable:
            raise ExternalCalendarTimeout()
        return CalendarBusySnapshot("verified", (CalendarBusyInterval(window_start, window_end),))

    _mock_google(monkeypatch, Mock(side_effect=query_busy))
    with db.SessionLocal() as session:
        total_offers = session.query(InviteSlotOfferDB).count()

    response = api_client.post(SLOTS_PATH, json={**SLOT_PAYLOAD, "recurrence": "weekly"})

    assert days == ["2026-08-20", "2026-08-27", "2026-09-03", "2026-09-10"]
    load_policy.assert_called_once_with("inst_001", operation="availability")
    if unavailable:
        assert response.status_code == 503
        assert response.json() == UNAVAILABLE
    else:
        assert response.status_code == 200, response.text
        assert response.json()["slots"] == []
    with db.SessionLocal() as session:
        assert session.query(InviteSlotOfferDB).count() == total_offers


def test_invalid_invite_never_reaches_calendar(api_client, monkeypatch):
    build_source = Mock(side_effect=AssertionError("Invalid invite reached calendar"))
    monkeypatch.setattr("app.routes.public.google_calendar_availability_source", build_source)
    response = api_client.post("/api/public/client-invites/unknown-invite/slots", json=SLOT_PAYLOAD)
    assert response.status_code == 404
    build_source.assert_not_called()


@pytest.mark.parametrize(("path", "payload"), [
    (SLOTS_PATH, {**SLOT_PAYLOAD, "recurrence": "weekly"}),
    (LEGACY_PATH, LEGACY_PAYLOAD),
])
def test_inflight_invite_revocation_stops_calendar_search(api_client, monkeypatch, path, payload):
    _connect()

    def query_busy(**kwargs):
        with db.SessionLocal.begin() as session:
            invite = session.get(InviteTokenDB, INVITE)
            invite.revoked_at = datetime.now(timezone.utc).replace(tzinfo=None)
        return CalendarBusySnapshot("verified", ())

    query = Mock(side_effect=query_busy)
    _mock_google(monkeypatch, query)
    with db.SessionLocal() as session:
        total_offers = session.query(InviteSlotOfferDB).count()

    response = api_client.post(path, json=payload)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "INVITE_NOT_FOUND"
    query.assert_called_once()
    with db.SessionLocal() as session:
        assert session.query(InviteSlotOfferDB).count() == total_offers


def test_legacy_instructor_without_an_account_keeps_working(api_client, monkeypatch):
    factory = _mock_google(monkeypatch, Mock())
    with db.SessionLocal.begin() as session:
        account = session.query(ProviderAccountDB).filter_by(instructor_id="inst_001").one()
        session.delete(account)

    response = api_client.post(SLOTS_PATH, json=SLOT_PAYLOAD)

    assert response.status_code == 200, response.text
    assert response.json()["slots"]
    factory.assert_not_called()


def test_legacy_invite_search_retries_without_waiting_for_instructor_lock(api_client):
    with db.SessionLocal.begin() as session:
        account = session.query(ProviderAccountDB).filter_by(instructor_id="inst_001").one()
        session.delete(account)

    with ThreadPoolExecutor(max_workers=1) as worker:
        with db.SessionLocal.begin() as session:
            session.query(InstructorDB).filter_by(id="inst_001").with_for_update().one()
            response = worker.submit(api_client.post, SLOTS_PATH, json=SLOT_PAYLOAD).result(timeout=5)

    assert response.status_code == 503
    assert response.json() == UNAVAILABLE


def test_offer_invite_lock_allows_notification_foreign_key_check(migrated_postgres_db):
    with db.SessionLocal.begin() as offers:
        require_active_invite(offers, INVITE, instructor_id="inst_001", lock=True)
        with db.SessionLocal.begin() as notification:
            notification.query(InviteTokenDB).filter_by(token=INVITE).with_for_update(
                read=True, key_share=True, nowait=True
            ).one()


@pytest.mark.parametrize(("path", "payload"), [(SLOTS_PATH, SLOT_PAYLOAD), (LEGACY_PATH, LEGACY_PAYLOAD)])
def test_invite_search_retries_if_calendar_is_enabled_during_legacy_search(
    api_client, monkeypatch, path, payload
):
    with db.SessionLocal.begin() as session:
        account = session.query(ProviderAccountDB).filter_by(instructor_id="inst_001").one()
        session.delete(account)
        total_offers = session.query(InviteSlotOfferDB).count()
    find = tools.find_available_swim_slots

    def provision_after_search(**kwargs):
        slots = find(**kwargs)
        seed_demo_provider_account()
        _connect()
        return slots

    monkeypatch.setattr(tools, "find_available_swim_slots", provision_after_search)

    response = api_client.post(path, json=payload)

    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    with db.SessionLocal() as session:
        assert session.query(InviteSlotOfferDB).count() == total_offers


def test_weekly_public_search_rejects_mixed_calendar_generations(api_client, monkeypatch):
    _connect()
    query = Mock(return_value=CalendarBusySnapshot("verified", ()))
    _mock_google(monkeypatch, query)
    find_slots = client_invites._find_slots

    def find_slots_with_reconnect(**kwargs):
        slots = find_slots(**kwargs)
        if kwargs["requested_date"].isoformat() == "2026-08-20":
            _connect()
        return slots

    monkeypatch.setattr(client_invites, "_find_slots", find_slots_with_reconnect)
    with db.SessionLocal() as session:
        total_offers = session.query(InviteSlotOfferDB).count()

    response = api_client.post(SLOTS_PATH, json={**SLOT_PAYLOAD, "recurrence": "weekly"})

    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    with db.SessionLocal() as session:
        assert session.query(InviteSlotOfferDB).count() == total_offers


@pytest.mark.parametrize("path,payload", [(SLOTS_PATH, SLOT_PAYLOAD), (RANGE_PATH, RANGE_PAYLOAD)])
def test_public_search_rolls_back_offers_if_calendar_changes_during_save(api_client, monkeypatch, path, payload):
    _connect()
    _mock_google(monkeypatch, Mock(return_value=CalendarBusySnapshot("verified", ())))
    _set_range_clock(monkeypatch, "2026-12-01T01:00:00+00:00")
    upsert = client_invites._upsert_slot_offer
    replaced = False

    def upsert_with_reconnect(*args, **kwargs):
        nonlocal replaced
        upsert(*args, **kwargs)
        if not replaced:
            _connect()
            replaced = True

    monkeypatch.setattr(client_invites, "_upsert_slot_offer", upsert_with_reconnect)
    with db.SessionLocal() as session:
        total_offers = session.query(InviteSlotOfferDB).count()

    response = api_client.post(path, json=payload)

    assert replaced
    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    with db.SessionLocal() as session:
        assert session.query(InviteSlotOfferDB).count() == total_offers


@pytest.mark.parametrize("provisioned", [False, True], ids=["legacy", "connected"])
def test_public_calendar_busy_limit_rejects_without_calling_google(api_client, monkeypatch, provisioned):
    if provisioned:
        _connect()
    else:
        with db.SessionLocal.begin() as session:
            account = session.query(ProviderAccountDB).filter_by(instructor_id="inst_001").one()
            session.delete(account)
    factory = _mock_google(monkeypatch, Mock())
    claim = Mock(side_effect=AssertionError("Capacity limit should run before calendar lifecycle locks"))
    monkeypatch.setattr(client_invites, "claim_google_calendar_availability_fence", claim)
    permits = api_client.app.state.public_calendar_reads
    for _ in range(4):
        assert permits.acquire(blocking=False)
    try:
        response = api_client.post(SLOTS_PATH, json=SLOT_PAYLOAD)
    finally:
        for _ in range(4):
            permits.release()
    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    factory.assert_not_called()
    claim.assert_not_called()
