from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from app import api_services, config, db, google_calendar_freebusy_deadline, google_calendar_token_deadline
from app.api import create_app
from app.api_schemas import CoachLessonRequestConfirmRequest
from app.auth import provision_provider_account
from app.calendar_connections import replace_google_calendar_connection
from app.calendar_credentials import CalendarCredentialCipher
from app.external_calendar import CalendarBusyInterval, CalendarBusySnapshot, ExternalCalendarTimeout
from app.google_calendar_authorization import GOOGLE_CALENDAR_FREEBUSY_SCOPE
from app.google_calendar_busy_source_factory import GoogleCalendarBusySourceFactory
from app.google_calendar_freebusy import GoogleCalendarFreeBusyTransportResponse
from app.google_calendar_oauth import GOOGLE_CALENDAR_CALLBACK_PATH
from app.google_calendar_token_exchange import GoogleCalendarTokenTransportResponse
from app.models import (
    AppointmentDB,
    InstructorDB,
    LessonRequestDB,
    NotificationEventDB,
    ProviderAccountDB,
    ProviderWorkingHoursDB,
)
from app.routes import coach as coach_routes
from tests.conftest import login_coach


AVAILABILITY_PATH = "/api/coach/availability"
BOOKINGS_PATH = "/api/coach/bookings"
REQUEST_PATH = "/api/coach/lesson-requests/req_sofia_pending_0820"
PAYLOAD = {
    "client_name": "Calendar Client",
    "client_address": "450 North Blvd, Austin, TX",
    "requested_date": "2026-08-20",
    "duration_mins": 60,
    "is_instructor_facility": False,
}
UNAVAILABLE = {
    "error": {
        "code": "CALENDAR_AVAILABILITY_UNAVAILABLE",
        "message": "Calendar availability could not be verified.",
    }
}
WINDOW_START = datetime(2026, 8, 20, 13, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 8, 20, 22, tzinfo=timezone.utc)
TEST_CIPHER = CalendarCredentialCipher(
    active_key_id="test-key",
    keys={"test-key": b"k" * 32},
)


@pytest.fixture()
def connected_calendar(authenticated_api_client):
    with db.SessionLocal() as session:
        account = session.query(ProviderAccountDB).filter_by(instructor_id="inst_001").one()
        account_id = account.id
    _connect_calendar(account_id)
    return authenticated_api_client


def _connect_calendar(account_id: str) -> None:
    with db.SessionLocal.begin() as session:
        replace_google_calendar_connection(
            session,
            account_id=account_id,
            refresh_token="fake-refresh-token",
            cipher=TEST_CIPHER,
            now=WINDOW_START,
        )


def _request_booking_state():
    with db.SessionLocal() as session:
        lesson = session.query(LessonRequestDB).filter_by(request_token="req_sofia_pending_0820").one()
        return (
            lesson.status,
            session.query(AppointmentDB).filter_by(request_id=lesson.id).count(),
            session.query(NotificationEventDB).filter_by(request_id=lesson.id).count(),
        )


@pytest.mark.parametrize("action", ["confirm", "approve"])
def test_confirmation_refuses_busy_calendar_without_booking(connected_calendar, monkeypatch, action):
    def query(*, instructor_id, window_start, window_end):
        assert instructor_id == "inst_001"
        assert (window_start, window_end) == (WINDOW_START, WINDOW_END)
        # The slow provider read must run before the booking transaction locks the request.
        with db.SessionLocal() as session:
            session.query(LessonRequestDB).filter_by(
                request_token="req_sofia_pending_0820"
            ).with_for_update(nowait=True).one()
        return CalendarBusySnapshot("verified", (CalendarBusyInterval(window_start, window_end),))

    monkeypatch.setattr(
        GoogleCalendarBusySourceFactory, "create_for", lambda *_: SimpleNamespace(query_busy_periods=query)
    )
    before = _request_booking_state()

    response = connected_calendar.post(f"{REQUEST_PATH}/{action}")

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SCHEDULING_CONFLICT"
    assert _request_booking_state() == before


def test_confirmation_calendar_failure_and_committed_replay(connected_calendar, monkeypatch):
    query = Mock(side_effect=ExternalCalendarTimeout())
    monkeypatch.setattr(
        GoogleCalendarBusySourceFactory, "create_for", lambda *_: SimpleNamespace(query_busy_periods=query)
    )
    before = _request_booking_state()

    unavailable = connected_calendar.post(f"{REQUEST_PATH}/confirm")

    assert unavailable.status_code == 503
    assert unavailable.json() == UNAVAILABLE
    assert _request_booking_state() == before

    query.side_effect = None
    query.return_value = CalendarBusySnapshot("verified", ())
    confirmed = connected_calendar.post(f"{REQUEST_PATH}/confirm")

    assert confirmed.status_code == 200, confirmed.text
    after = _request_booking_state()
    assert after[0:2] == ("confirmed", 1)
    assert after[2] > before[2]
    query.reset_mock(side_effect=True)
    query.side_effect = ExternalCalendarTimeout()
    replayed = connected_calendar.post(f"{REQUEST_PATH}/confirm")

    assert replayed.status_code == 200
    assert replayed.json()["appointment_ids"] == confirmed.json()["appointment_ids"]
    assert _request_booking_state() == after
    query.assert_not_called()


def test_confirmation_checks_later_week_before_booking_series(connected_calendar, monkeypatch):
    with db.SessionLocal.begin() as session:
        lesson = session.query(LessonRequestDB).filter_by(request_token="req_sofia_pending_0820").one()
        lesson.recurrence = "weekly"
        lesson.recurrence_count = 2
    days = []

    def query(*, instructor_id, window_start, window_end):
        assert instructor_id == "inst_001"
        days.append(window_start.date().isoformat())
        busy = (CalendarBusyInterval(window_start, window_end),) if len(days) == 2 else ()
        return CalendarBusySnapshot("verified", busy)

    monkeypatch.setattr(
        GoogleCalendarBusySourceFactory, "create_for", lambda *_: SimpleNamespace(query_busy_periods=query)
    )
    before = _request_booking_state()

    response = connected_calendar.post(f"{REQUEST_PATH}/confirm")

    assert response.status_code == 409, response.text
    assert days == ["2026-08-20", "2026-08-27"]
    assert _request_booking_state() == before


@pytest.mark.parametrize("busy", [False, True], ids=["free", "busy"])
def test_confirmation_rejects_calendar_replaced_after_verification(connected_calendar, monkeypatch, busy):
    periods = (CalendarBusyInterval(WINDOW_START, WINDOW_END),) if busy else ()
    query = Mock(return_value=CalendarBusySnapshot("verified", periods))
    monkeypatch.setattr(
        GoogleCalendarBusySourceFactory, "create_for", lambda *_: SimpleNamespace(query_busy_periods=query)
    )
    with db.SessionLocal() as session:
        account_id = session.query(ProviderAccountDB.id).filter_by(instructor_id="inst_001").scalar()
    acquire_locks = api_services._acquire_confirmation_locks

    def reconnect_before_lock(*args, **kwargs):
        _connect_calendar(account_id)
        return acquire_locks(*args, **kwargs)

    monkeypatch.setattr(api_services, "_acquire_confirmation_locks", reconnect_before_lock)
    before = _request_booking_state()

    response = connected_calendar.post(f"{REQUEST_PATH}/confirm")

    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    assert _request_booking_state() == before
    with db.SessionLocal() as session:
        lesson = session.query(LessonRequestDB).filter_by(request_token="req_sofia_pending_0820").one()
        assert lesson.validation_failure_code is None
    query.assert_called_once()


def test_confirmation_replays_if_another_attempt_commits_during_calendar_timeout(
    connected_calendar, monkeypatch
):
    committed = []

    def query_busy(**kwargs):
        concurrent = api_services.confirm_coach_lesson_request(
            "inst_001", "req_sofia_pending_0820", CoachLessonRequestConfirmRequest()
        )
        committed.extend(concurrent.appointment_ids)
        raise ExternalCalendarTimeout()

    monkeypatch.setattr(
        GoogleCalendarBusySourceFactory, "create_for", lambda *_: SimpleNamespace(query_busy_periods=query_busy)
    )

    response = connected_calendar.post(f"{REQUEST_PATH}/confirm")

    assert response.status_code == 200, response.text
    assert response.json()["appointment_ids"] == committed
    assert _request_booking_state()[:2] == ("confirmed", 1)


def test_confirmation_validates_the_persisted_duration_across_fall_dst(connected_calendar, monkeypatch):
    with db.SessionLocal.begin() as session:
        provider = session.get(InstructorDB, "inst_001")
        provider.timezone = "America/New_York"
        provider.base_address = provider.service_address
        provider.base_lat = provider.service_lat
        provider.base_lng = provider.service_lng
        sunday = next(hours for hours in provider.working_hours if hours.weekday == 6)
        sunday.enabled = True
        sunday.start_time = "00:00"
        sunday.end_time = "04:00"
        lesson = session.query(LessonRequestDB).filter_by(request_token="req_sofia_pending_0820").one()
        lesson.recurrence = "weekly"
        lesson.recurrence_count = 2
        lesson.duration_mins = 120
        lesson.location_type = "facility"
        lesson.client_address = provider.service_address
        lesson.lat = provider.service_lat
        lesson.lng = provider.service_lng
        lesson.requested_date = datetime(2026, 10, 25).date()
        lesson.slot_start = datetime(2026, 10, 25, 4, 30)
        lesson.slot_end = datetime(2026, 10, 25, 6, 30)

    def query(*, instructor_id, window_start, window_end):
        busy = ()
        if window_start.month == 11:
            busy = (CalendarBusyInterval(
                datetime(2026, 11, 1, 7, tzinfo=timezone.utc),
                datetime(2026, 11, 1, 7, 15, tzinfo=timezone.utc),
            ),)
        return CalendarBusySnapshot("verified", busy)

    monkeypatch.setattr(
        GoogleCalendarBusySourceFactory, "create_for", lambda *_: SimpleNamespace(query_busy_periods=query)
    )
    before = _request_booking_state()

    response = connected_calendar.post(f"{REQUEST_PATH}/confirm")

    assert response.status_code == 409, response.text
    assert _request_booking_state() == before


def test_availability_requires_auth_before_building_calendar_source(monkeypatch):
    build_source = Mock(side_effect=AssertionError("Unauthenticated request reached calendar"))
    monkeypatch.setattr(coach_routes, "_google_calendar_availability_source", build_source)
    client = TestClient(create_app())

    response = client.post(AVAILABILITY_PATH, json=PAYLOAD)

    assert response.status_code == 401
    build_source.assert_not_called()


def test_disconnected_calendar_keeps_availability_without_google(authenticated_api_client, monkeypatch):
    create_for = Mock(side_effect=AssertionError("Disconnected calendar loaded Google configuration"))
    monkeypatch.setattr(GoogleCalendarBusySourceFactory, "create_for", create_for)

    response = authenticated_api_client.post(AVAILABILITY_PATH, json=PAYLOAD)

    assert response.status_code == 200
    assert response.json()["available_slots"]
    create_for.assert_not_called()


@pytest.mark.parametrize(("instructor_id", "connected"), [
    pytest.param("Riley García", False, id="unicode-disconnected"),
    pytest.param("Riley García", True, id="unicode-connected"),
    pytest.param(" " + "é" * 128 + " ", False, id="wide-padded-disconnected"),
])
def test_availability_preserves_existing_instructor_ids(
    api_client, monkeypatch, instructor_id, connected
):
    with db.SessionLocal.begin() as session:
        demo = session.get(InstructorDB, "inst_001")
        session.add(InstructorDB(
            id=instructor_id,
            name="Riley",
            main_address=demo.main_address,
            lat=demo.lat,
            lng=demo.lng,
            timezone=demo.timezone,
            base_address=demo.base_address,
            base_lat=demo.base_lat,
            base_lng=demo.base_lng,
            service_address=demo.service_address,
            service_lat=demo.service_lat,
            service_lng=demo.service_lng,
            service_name=demo.service_name,
            default_duration_mins=demo.default_duration_mins,
            working_hours=[
                ProviderWorkingHoursDB(
                    weekday=day.weekday,
                    enabled=day.enabled,
                    start_time=day.start_time,
                    end_time=day.end_time,
                )
                for day in demo.working_hours
            ],
        ))
    email, password = "existing-provider@example.com", "existing-test-password"
    account_id = provision_provider_account(instructor_id, email, password)
    api_client.headers.update(login_coach(api_client, email=email, password=password))
    if connected:
        _connect_calendar(account_id)
        _stub_google_responses(monkeypatch)

    response = api_client.post(AVAILABILITY_PATH, json=PAYLOAD)

    assert response.status_code == 200, response.text
    assert bool(response.json()["available_slots"]) == (not connected)


def _stub_google_responses(monkeypatch):
    monkeypatch.setenv(
        "SWIM_GOOGLE_CALENDAR_OAUTH_CLIENT_ID", "1234-test.apps.googleusercontent.com"
    )
    monkeypatch.setenv("SWIM_GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv(
        "SWIM_GOOGLE_CALENDAR_OAUTH_REDIRECT_URI",
        f"https://calendar.example.com{GOOGLE_CALENDAR_CALLBACK_PATH}",
    )
    monkeypatch.setattr(config, "calendar_credential_cipher_from_env", lambda: TEST_CIPHER)
    token = GoogleCalendarTokenTransportResponse(
        status_code=200,
        content_type="application/json",
        body=json.dumps({
            "access_token": "fake-access-token",
            "expires_in": 3600,
            "scope": GOOGLE_CALENDAR_FREEBUSY_SCOPE,
            "token_type": "Bearer",
        }).encode("utf-8"),
    )
    busy = GoogleCalendarFreeBusyTransportResponse(
        status_code=200,
        content_type="application/json",
        body=json.dumps({
            "kind": "calendar#freeBusy",
            "timeMin": "2026-08-20T13:00:00Z",
            "timeMax": "2026-08-20T22:00:00Z",
            "calendars": {"primary": {"busy": [{
                "start": "2026-08-20T13:00:00Z",
                "end": "2026-08-20T22:00:00Z",
            }]}},
        }).encode("utf-8"),
    )
    monkeypatch.setattr(google_calendar_token_deadline, "_post_form_async", AsyncMock(return_value=token))
    monkeypatch.setattr(
        google_calendar_freebusy_deadline, "_post_json_in_owned_task", AsyncMock(return_value=busy)
    )


def test_connected_calendar_excludes_busy_time(connected_calendar, monkeypatch):
    query = Mock(return_value=CalendarBusySnapshot(
        status="verified",
        periods=(CalendarBusyInterval(start=WINDOW_START, end=WINDOW_END),),
    ))
    create_for = Mock(return_value=SimpleNamespace(query_busy_periods=query))
    monkeypatch.setattr(GoogleCalendarBusySourceFactory, "create_for", create_for)

    response = connected_calendar.post(AVAILABILITY_PATH, json=PAYLOAD)

    assert response.status_code == 200
    assert response.json()["available_slots"] == []
    create_for.assert_called_once_with(db.SessionLocal)
    query.assert_called_once_with(
        instructor_id="inst_001", window_start=WINDOW_START, window_end=WINDOW_END
    )


def test_partial_calendar_block_preserves_break_recovery(connected_calendar, monkeypatch):
    policy_path = "/api/coach/scheduling-policy"
    policy = connected_calendar.get(policy_path).json()
    saved = connected_calendar.put(policy_path, json={
        "expected_revision": policy["revision"],
        "distance_buffers_enabled": False,
        "post_break_recovery_mins": 30,
        "distance_buffer_tiers": [
            {"id": tier["id"], "buffer_mins": tier["buffer_mins"]}
            for tier in policy["distance_buffer_tiers"]
        ],
    })
    assert saved.status_code == 200, saved.text

    query = Mock(return_value=CalendarBusySnapshot(
        status="verified",
        periods=(CalendarBusyInterval(
            start=datetime(2026, 8, 20, 18, 0, tzinfo=timezone.utc),
            end=datetime(2026, 8, 20, 18, 15, tzinfo=timezone.utc),
        ),),
    ))
    monkeypatch.setattr(GoogleCalendarBusySourceFactory, "create_for", Mock(
        return_value=SimpleNamespace(query_busy_periods=query)
    ))

    response = connected_calendar.post(AVAILABILITY_PATH, json={
        **PAYLOAD, "is_instructor_facility": True, "duration_mins": 30,
    })

    assert response.status_code == 200, response.text
    post_break_slots = [
        slot for slot in response.json()["available_slots"] if "T13:" in slot["start_iso"]
    ]
    assert post_break_slots[0]["start_iso"] == "2026-08-20T13:30:00-05:00"
    assert post_break_slots[0]["buffer_mins"] == 15


def test_calendar_failure_exposes_no_slots_and_allows_retry(connected_calendar, monkeypatch):
    failing = SimpleNamespace(query_busy_periods=Mock(side_effect=TimeoutError("provider diagnostics")))
    recovered = SimpleNamespace(query_busy_periods=Mock(
        return_value=CalendarBusySnapshot(status="verified", periods=())
    ))
    create_for = Mock(side_effect=[failing, recovered])
    monkeypatch.setattr(GoogleCalendarBusySourceFactory, "create_for", create_for)

    failed = connected_calendar.post(AVAILABILITY_PATH, json=PAYLOAD)
    retried = connected_calendar.post(AVAILABILITY_PATH, json=PAYLOAD)

    assert failed.status_code == 503
    assert failed.json() == UNAVAILABLE
    assert failed.headers["cache-control"] == "private, no-store"
    assert retried.status_code == 200
    assert retried.json()["available_slots"]
    assert create_for.call_count == 2


def test_missing_factory_returns_safe_unavailable_response(migrated_postgres_db):
    app = create_app()
    del app.state.calendar_busy_source_factory

    with TestClient(app) as client:
        client.headers.update(login_coach(client))
        response = client.post(AVAILABILITY_PATH, json=PAYLOAD)

    assert response.status_code == 503
    assert response.json() == UNAVAILABLE


@pytest.mark.parametrize("outcome, expected_status, expected_code", [
    ("busy", 409, "SCHEDULING_CONFLICT"),
    ("timeout", 503, "CALENDAR_AVAILABILITY_UNAVAILABLE"),
])
def test_direct_booking_rechecks_google_before_saving(
    connected_calendar, monkeypatch, outcome, expected_status, expected_code
):
    query = _install_booking_calendar(monkeypatch)
    booking = _request_first_available_booking(connected_calendar)
    if outcome == "busy":
        query.return_value = CalendarBusySnapshot(
            status="verified",
            periods=(CalendarBusyInterval(start=WINDOW_START, end=WINDOW_END),),
        )
    else:
        query.side_effect = TimeoutError("provider diagnostics")

    response = connected_calendar.post(BOOKINGS_PATH, json=booking)

    assert response.status_code == expected_status, response.text
    assert response.json()["error"]["code"] == expected_code
    assert "provider diagnostics" not in response.text
    assert db.get_appointment_by_idempotency_key("inst_001", booking["idempotency_key"]) is None
    assert query.call_count == 2


def test_direct_booking_replay_needs_no_calendar_source(connected_calendar, monkeypatch):
    query = _install_booking_calendar(monkeypatch)
    booking = _request_first_available_booking(connected_calendar)

    booked = connected_calendar.post(BOOKINGS_PATH, json=booking)
    assert booked.status_code == 200, booked.text
    del connected_calendar.app.state.calendar_busy_source_factory
    replayed = connected_calendar.post(BOOKINGS_PATH, json=booking)
    mismatched = connected_calendar.post(BOOKINGS_PATH, json={**booking, "duration_mins": 30})

    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["appointment_id"] == booked.json()["appointment_id"]
    assert mismatched.status_code == 409, mismatched.text
    assert mismatched.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert query.call_count == 2


def _install_booking_calendar(monkeypatch):
    query = Mock(return_value=CalendarBusySnapshot(status="verified", periods=()))
    factory = Mock(return_value=SimpleNamespace(query_busy_periods=query))
    monkeypatch.setattr(GoogleCalendarBusySourceFactory, "create_for", factory)
    return query


def _request_first_available_booking(client):
    response = client.post(AVAILABILITY_PATH, json=PAYLOAD)
    assert response.status_code == 200, response.text
    slot = response.json()["available_slots"][0]
    return {
        key: value for key, value in PAYLOAD.items() if key != "requested_date"
    } | {
        "start_time": slot["start_iso"],
        "idempotency_key": "test-calendar-direct-booking",
    }
