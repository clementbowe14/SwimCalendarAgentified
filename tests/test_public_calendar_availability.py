from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import client_invites, db, tools
from app.auth import seed_demo_provider_account
from app.calendar_connections import replace_google_calendar_connection
from app.calendar_credentials import CalendarCredentialCipher
from app.external_calendar import CalendarBusyInterval, CalendarBusySnapshot, ExternalCalendarTimeout
from app.google_calendar_busy_source_factory import GoogleCalendarBusySourceFactory
from app.models import InviteSlotOfferDB, InviteTokenDB, ProviderAccountDB
from tests.public_contract_safety import PUBLIC_SCHEDULING_EVIDENCE_KEYS, flatten_keys


INVITE = db.SOFIA_INVITE_TOKEN
SLOTS_PATH = f"/api/public/client-invites/{INVITE}/slots"
LEGACY_PATH = f"/api/public/invites/{INVITE}/availability"
ADDRESS = "450 North Blvd, Austin, TX"
BASE_PAYLOAD = {"client_name": "Sofia Martinez", "requested_date": "2026-08-20", "duration_mins": 45}
SLOT_PAYLOAD = {
    **BASE_PAYLOAD,
    "location": {"address": ADDRESS, "is_instructor_facility": False},
    "instructor_id": "inst_untrusted",
}
LEGACY_PAYLOAD = {**BASE_PAYLOAD, "client_address": ADDRESS}
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


def test_public_search_rolls_back_offers_if_calendar_changes_during_save(api_client, monkeypatch):
    _connect()
    _mock_google(monkeypatch, Mock(return_value=CalendarBusySnapshot("verified", ())))
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

    response = api_client.post(SLOTS_PATH, json=SLOT_PAYLOAD)

    assert replaced
    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    with db.SessionLocal() as session:
        assert session.query(InviteSlotOfferDB).count() == total_offers


def test_public_calendar_busy_limit_rejects_without_calling_google(api_client, monkeypatch):
    _connect()
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
