from datetime import date, datetime, time, timedelta
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.errors import LockNotAvailable
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app import api_services, availability_search, db, routing_cache, timezones, tools
from app.calendar_connections import disconnect_google_calendar, replace_google_calendar_connection
from app.calendar_credentials import CalendarCredentialCipher
from app.external_calendar import MAX_BUSY_QUERY_WINDOW, CalendarBusyInterval, CalendarBusySnapshot, ExternalCalendarTimeout
from app.google_calendar_busy_source_factory import GoogleCalendarBusySourceFactory
from app.models import AppointmentDB, InstructorDB, ProviderAccountDB, ProviderWorkingHoursDB
from app.routing_providers import MapboxRoutingProvider
from app.routing import RoutingTimeout
from app.scheduler_settings import InvalidSchedulerPolicy


PATH = "/api/coach/availability/search"
INSTRUCTOR = "inst_001"
NOW = datetime.fromisoformat("2026-11-24T15:30:00+00:00")
SERVICE_ADDRESS = "10 Main Street"
BASE_COORDS = (30.26, -97.75)
REQUEST = {
    "start_date": "2026-11-24",
    "end_date": "2026-11-27",
    "duration_mins": 30,
    "is_instructor_facility": True,
}


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    _set_clock(monkeypatch, NOW)


def _set_clock(monkeypatch, now):
    monkeypatch.setattr(
        api_services, "search_one_time_availability",
        partial(availability_search.search_one_time_availability, now=now),
    )


@pytest.fixture()
def provider(authenticated_api_client):
    with db.SessionLocal.begin() as session:
        settings = session.get(InstructorDB, INSTRUCTOR)
        settings.base_address = settings.service_address = SERVICE_ADDRESS
        settings.base_lat = settings.service_lat = BASE_COORDS[0]
        settings.base_lng = settings.service_lng = BASE_COORDS[1]
        for row in session.query(ProviderWorkingHoursDB).filter_by(instructor_id=INSTRUCTOR):
            row.enabled = True
            row.start_time, row.end_time = time(8), time(17)
    return authenticated_api_client


def _block(identifier, start, end, *, located=False):
    return AppointmentDB(
        id=identifier, instructor_id=INSTRUCTOR, title=identifier,
        start_time=timezones.to_utc_storage(datetime.fromisoformat(start)),
        end_time=timezones.to_utc_storage(datetime.fromisoformat(end)),
        event_type="block", blocker_kind="personal",
        location_address=SERVICE_ADDRESS if located else None,
        lat=BASE_COORDS[0] if located else None,
        lng=BASE_COORDS[1] if located else None,
    )


def _connect_calendar(monkeypatch, query):
    with db.SessionLocal.begin() as session:
        account = session.query(ProviderAccountDB).filter_by(instructor_id=INSTRUCTOR).one()
        replace_google_calendar_connection(
            session, account_id=account.id, refresh_token="synthetic-range-refresh",
            cipher=CalendarCredentialCipher(active_key_id="test", keys={"test": b"k" * 32}), now=NOW,
        )
    monkeypatch.setattr(
        GoogleCalendarBusySourceFactory, "create_for", lambda *_: SimpleNamespace(query_busy_periods=query),
    )


def test_bounded_search_filters_whole_slots_and_applies_internal_and_google_busy_time(provider, monkeypatch):
    with db.SessionLocal.begin() as session:
        wednesday = session.get(ProviderWorkingHoursDB, (INSTRUCTOR, 2))
        wednesday.enabled, wednesday.start_time, wednesday.end_time = False, None, None
        session.add(_block("Personal appointment", "2026-11-24T09:45:00-06:00", "2026-11-24T10:00:00-06:00"))

    query = Mock(return_value=CalendarBusySnapshot("verified", (
        CalendarBusyInterval(
            datetime.fromisoformat("2026-11-26T09:00:00-06:00"),
            datetime.fromisoformat("2026-11-26T09:15:00-06:00"),
        ),
    )))
    _connect_calendar(monkeypatch, query)
    geocode = Mock()
    monkeypatch.setattr(tools.routing_provider, "geocode", geocode)
    windows = [
        {"days": ["tuesday", "wednesday", "thursday", "friday"], "start_time": "09:00", "end_time": "09:30"},
        {"days": ["tuesday", "wednesday", "thursday", "friday"], "start_time": "09:30", "end_time": "10:00"},
    ]

    response = provider.post(PATH, json={**REQUEST, "windows": windows, "limit": 3})

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    result = response.json()
    assert result["timezone"] == "America/Chicago"
    assert result["location"]["address"] == SERVICE_ADDRESS
    assert result["empty_reason"] is None
    assert [slot["start_iso"] for slot in result["available_slots"]] == [
        "2026-11-26T09:30:00-06:00", "2026-11-27T09:00:00-06:00", "2026-11-27T09:15:00-06:00",
    ]
    assert result["available_slots"][0]["buffer_mins"] == 10
    query.assert_called_once_with(
        instructor_id=INSTRUCTOR, window_start=datetime.fromisoformat("2026-11-24T14:00:00+00:00"),
        window_end=datetime.fromisoformat("2026-11-27T23:00:00+00:00"),
    )
    geocode.assert_not_called()


def test_home_is_geocoded_once_across_multiple_days(provider, monkeypatch):
    coords = (30.255, -97.655)
    geocode = Mock(return_value=coords)
    monkeypatch.setattr(tools.routing_provider, "geocode", geocode)
    windows = [{"days": ["thursday", "friday"], "start_time": "14:00", "end_time": "14:30"}]
    response = provider.post(PATH, json={
        **REQUEST, "windows": windows, "is_instructor_facility": False, "client_address": " 20 Main Street ",
    })

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["location"] == {
        "address": "20 Main Street", "lat": coords[0], "lng": coords[1], "is_instructor_facility": False,
    }
    assert [slot["start_iso"] for slot in result["available_slots"]] == [
        "2026-11-26T14:00:00-06:00", "2026-11-27T14:00:00-06:00",
    ]
    assert all(slot["travel_time_mins"] > 0 for slot in result["available_slots"])
    geocode.assert_called_once_with("20 Main Street")


def test_dst_fall_back_requires_entire_elapsed_slot_in_client_window(provider, monkeypatch):
    _set_clock(monkeypatch, datetime.fromisoformat("2026-11-01T05:00:00+00:00"))
    with db.SessionLocal.begin() as session:
        sunday = session.get(ProviderWorkingHoursDB, (INSTRUCTOR, 6))
        sunday.start_time, sunday.end_time = time(0, 30), time(4)

    response = provider.post(PATH, json={
        **REQUEST, "start_date": "2026-11-01", "end_date": "2026-11-01", "duration_mins": 60,
        "windows": [{"days": ["sunday"], "start_time": "01:30", "end_time": "02:30"}],
    })

    assert response.status_code == 200, response.text
    assert [slot["start_iso"] for slot in response.json()["available_slots"]] == ["2026-11-01T01:30:00-06:00"]


def test_rounding_after_a_blocker_can_offer_the_repeated_hour(provider, monkeypatch):
    _set_clock(monkeypatch, datetime.fromisoformat("2026-11-01T05:00:00+00:00"))
    with db.SessionLocal.begin() as session:
        sunday = session.get(ProviderWorkingHoursDB, (INSTRUCTOR, 6))
        sunday.start_time, sunday.end_time = time(0, 30), time(4)
        session.add(_block("Across first hour", "2026-11-01T00:30:00-05:00", "2026-11-01T01:47:00-05:00"))

    response = provider.post(PATH, json={
        **REQUEST, "start_date": "2026-11-01", "end_date": "2026-11-01",
        "windows": [{"days": ["sunday"], "start_time": "01:00", "end_time": "02:00"}],
    })

    assert response.status_code == 200, response.text
    starts = [slot["start_iso"] for slot in response.json()["available_slots"]]
    assert starts == [
        "2026-11-01T01:00:00-06:00", "2026-11-01T01:15:00-06:00", "2026-11-01T01:30:00-06:00",
    ]
    booked = provider.post("/api/coach/bookings", json={
        "client_name": "Repeated hour client", "client_address": SERVICE_ADDRESS,
        "start_time": starts[0], "duration_mins": 30, "is_instructor_facility": True,
    })
    assert booked.status_code == 200, booked.text
    with db.SessionLocal() as session:
        record = session.get(AppointmentDB, booked.json()["appointment_id"])
        assert record.start_time == datetime.fromisoformat("2026-11-01T07:00:00")


def test_calendar_fails_closed_if_any_chunk_is_unverified(provider, monkeypatch):
    with db.SessionLocal.begin() as session:
        session.add_all([
            _block("before search", "2026-11-23T01:00:00-06:00", "2026-11-23T01:30:00-06:00", located=True),
            _block("after search", "2026-12-08T22:00:00-06:00", "2026-12-08T22:30:00-06:00", located=True),
        ])
    query = Mock(side_effect=[CalendarBusySnapshot("verified", ()), ExternalCalendarTimeout()])
    _connect_calendar(monkeypatch, query)

    response = provider.post(PATH, json={**REQUEST, "end_date": "2026-12-07"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CALENDAR_AVAILABILITY_UNAVAILABLE"
    first, second = (call.kwargs for call in query.call_args_list)
    assert first["window_end"] - first["window_start"] == MAX_BUSY_QUERY_WINDOW
    assert second["window_start"] == first["window_end"]
    assert second["window_end"] == datetime.fromisoformat("2026-12-09T04:00:00+00:00")


def test_returns_actionable_empty_outcomes_without_searching_irrelevant_days(provider):
    with db.SessionLocal.begin() as session:
        tuesday = session.get(ProviderWorkingHoursDB, (INSTRUCTOR, 1))
        tuesday.enabled, tuesday.start_time, tuesday.end_time = False, None, None
        session.add(_block("Thursday blocked", "2026-11-26T09:00:00-06:00", "2026-11-26T10:00:00-06:00"))
    no_work = provider.post(PATH, json={**REQUEST, "end_date": REQUEST["start_date"]})
    other_days = provider.post(PATH, json={
        **REQUEST, "windows": [{"days": ["saturday"], "start_time": "09:00", "end_time": "10:00"}],
    })
    outside_hours = provider.post(PATH, json={
        **REQUEST, "windows": [{"days": ["thursday"], "start_time": "19:00", "end_time": "20:00"}],
    })
    no_safe_slots = provider.post(PATH, json={
        **REQUEST, "windows": [{"days": ["thursday"], "start_time": "09:00", "end_time": "10:00"}],
    })

    for response, reason in zip((no_work, other_days, outside_hours, no_safe_slots), (
        "no_working_hours", "outside_client_windows", "outside_client_windows", "no_safe_slots",
    )):
        assert response.status_code == 200, response.text
        assert response.json()["available_slots"] == []
        assert response.json()["empty_reason"] == reason


@pytest.mark.parametrize("changes", [
    {"end_date": "2026-11-23"},
    {"end_date": "2026-12-08"},
    {"limit": 49},
    {"recurrence": "weekly"},
    {"is_instructor_facility": False},
    {"windows": [{"days": ["thursday"], "start_time": "15:00", "end_time": "14:00"}]},
    {"windows": [{"days": ["thursday"], "start_time": "09:0٠", "end_time": "10:00"}]},
])
def test_validates_range_filters_and_one_time_scope(provider, changes):
    response = provider.post(PATH, json={**REQUEST, **changes})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_fences_search_to_fourteen_days_on_the_provider_calendar(provider, monkeypatch):
    # It is already November 25 in UTC, but still November 24 in Chicago.
    _set_clock(monkeypatch, datetime.fromisoformat("2026-11-25T02:00:00+00:00"))
    end_date = (date.fromisoformat(REQUEST["start_date"]) + timedelta(days=13)).isoformat()
    allowed = provider.post(PATH, json={**REQUEST, "end_date": end_date, "limit": 1})
    outside = provider.post(PATH, json={**REQUEST, "start_date": "2026-12-08", "end_date": "2026-12-09"})
    past = provider.post(PATH, json={**REQUEST, "start_date": "2026-11-23"})

    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["available_slots"][0]["start_iso"].startswith("2026-11-25")
    for response in (outside, past):
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "INVALID_SCHEDULING_REQUEST"


def test_requires_authenticated_provider_and_rejects_a_changed_owner(api_client, coach_auth_headers):
    unauthenticated = api_client.post(PATH, json=REQUEST)
    changed_owner = api_client.post(PATH, json=REQUEST, headers={
        **coach_auth_headers, "X-Expected-Instructor-Id": "another-provider",
    })

    assert unauthenticated.status_code == 401
    assert changed_owner.status_code == 409
    assert changed_owner.json()["error"]["code"] == "CALENDAR_CONNECTION_OWNER_CHANGED"


def test_range_reads_one_snapshot_when_a_blocker_moves_between_days(provider, monkeypatch):
    with db.SessionLocal.begin() as session:
        session.add(_block("moved", "2026-11-27T13:00:00-06:00", "2026-11-27T13:30:00-06:00"))
    read_events = tools.routing_events_for_date

    def move_after_thursday(instructor, day, zone, **options):
        events = read_events(instructor, day, zone, **options)
        if day.isoformat() == "2026-11-26":
            with db.SessionLocal.begin() as session:
                blocker = session.get(AppointmentDB, "moved")
                blocker.start_time -= timedelta(days=1)
                blocker.end_time -= timedelta(days=1)
        return events

    monkeypatch.setattr(tools, "routing_events_for_date", move_after_thursday)
    response = provider.post(PATH, json={
        **REQUEST, "windows": [{"days": ["thursday", "friday"], "start_time": "13:00", "end_time": "13:30"}],
    })

    assert response.status_code == 200, response.text
    assert [slot["start_iso"] for slot in response.json()["available_slots"]] == ["2026-11-26T13:00:00-06:00"]


@pytest.mark.parametrize("routing_budget", [1, 2])
def test_candidate_filters_and_result_limit_apply_before_outbound_routing(provider, monkeypatch, routing_budget):
    with db.SessionLocal.begin() as session:
        session.add(_block("located", "2026-11-24T14:00:00-06:00", "2026-11-24T14:30:00-06:00", located=True))
    monkeypatch.setattr(availability_search, "_MAX_ROUTING_CALLS", routing_budget)
    estimate = Mock(wraps=tools.routing_provider.estimate_route)
    monkeypatch.setattr(tools.routing_provider, "estimate_route", estimate)

    response = provider.post(PATH, json={
        **REQUEST, "limit": 1,
        "windows": [{"days": ["tuesday"], "start_time": "10:00", "end_time": "11:00"}],
    })

    assert estimate.call_count == routing_budget
    if routing_budget == 1:
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "AVAILABILITY_SEARCH_BUDGET_EXCEEDED"
    else:
        assert response.status_code == 200, response.text
        assert [slot["start_iso"] for slot in response.json()["available_slots"]] == ["2026-11-24T10:00:00-06:00"]
        assert estimate.call_args.kwargs["departure_time"] == datetime.fromisoformat("2026-11-24T16:30:00+00:00")


def test_deadline_stops_search_after_an_inflight_geocode_call(provider, monkeypatch):
    elapsed = 0

    def slow_geocode(_address):
        nonlocal elapsed
        elapsed = availability_search._SEARCH_SECONDS
        return BASE_COORDS

    monkeypatch.setattr(availability_search, "monotonic", lambda: elapsed)
    monkeypatch.setattr(tools.routing_provider, "geocode", slow_geocode)
    calendar = Mock()
    monkeypatch.setattr(availability_search, "_read_busy_periods", calendar)
    response = provider.post(PATH, json={
        **REQUEST, "is_instructor_facility": False, "client_address": "20 Main Street",
    })

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "AVAILABILITY_SEARCH_BUDGET_EXCEEDED"
    calendar.assert_not_called()


def test_schedule_row_budget_fails_before_calendar_io(provider, monkeypatch):
    with db.SessionLocal.begin() as session:
        session.add_all([
            _block("first", "2026-11-24T09:00:00-06:00", "2026-11-24T09:30:00-06:00"),
            _block("second", "2026-11-24T10:00:00-06:00", "2026-11-24T10:30:00-06:00"),
        ])
    monkeypatch.setattr(availability_search, "_MAX_SEARCH_EVENTS", 1)
    calendar = Mock()
    monkeypatch.setattr(availability_search, "_read_busy_periods", calendar)
    response = provider.post(PATH, json=REQUEST)

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "AVAILABILITY_SEARCH_BUDGET_EXCEEDED"
    calendar.assert_not_called()


def test_calendar_connection_change_during_geocoding_invalidates_the_range(provider, monkeypatch):
    _connect_calendar(monkeypatch, Mock(return_value=CalendarBusySnapshot("verified", ())))

    def geocode_then_disconnect(_address):
        with db.SessionLocal.begin() as session:
            account = session.query(ProviderAccountDB).filter_by(instructor_id=INSTRUCTOR).one()
            disconnect_google_calendar(session, account_id=account.id)
        return BASE_COORDS

    monkeypatch.setattr(tools.routing_provider, "geocode", geocode_then_disconnect)
    response = provider.post(PATH, json={
        **REQUEST, "is_instructor_facility": False, "client_address": "20 Main Street",
    })

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "CALENDAR_AVAILABILITY_UNAVAILABLE"


def test_calendar_connection_change_during_snapshot_read_invalidates_the_range(provider, monkeypatch):
    _connect_calendar(monkeypatch, Mock(return_value=CalendarBusySnapshot("verified", ())))
    read_provider = availability_search.require_provider_setup

    def read_then_disconnect(instructor_id, *, session):
        result = read_provider(instructor_id, session=session)
        with db.SessionLocal.begin() as writer:
            account = writer.query(ProviderAccountDB).filter_by(instructor_id=INSTRUCTOR).one()
            disconnect_google_calendar(writer, account_id=account.id)
        return result

    monkeypatch.setattr(availability_search, "require_provider_setup", read_then_disconnect)
    response = provider.post(PATH, json=REQUEST)

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "CALENDAR_AVAILABILITY_UNAVAILABLE"


def test_range_errors_do_not_echo_client_details_or_allow_caching(provider, monkeypatch):
    address = "20 Private Client Street"
    payload = {**REQUEST, "is_instructor_facility": False, "client_address": address}
    invalid = provider.post(PATH, json={**payload, "end_date": "2026-11-22"})
    monkeypatch.setattr(availability_search, "require_provider_setup", Mock(side_effect=RuntimeError(address)))
    unexpected = provider.post(PATH, json=payload)

    assert invalid.status_code == 422
    assert unexpected.status_code == 500
    for response in (invalid, unexpected):
        assert address not in response.text
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_mounted_search_keeps_responses_private_and_contains_unexpected_errors(provider, monkeypatch):
    app = FastAPI()
    app.mount("/mounted", provider.app)
    address = "20 Private Client Street"
    payload = {**REQUEST, "is_instructor_facility": False, "client_address": address}
    with TestClient(app, raise_server_exceptions=True) as mounted:
        mounted.headers.update(provider.headers)
        success = mounted.post(f"/mounted{PATH}", json=payload)
        invalid = mounted.post(f"/mounted{PATH}", json={**payload, "end_date": "2026-11-22"})
        monkeypatch.setattr(availability_search, "require_provider_setup", Mock(side_effect=RuntimeError(address)))
        unexpected = mounted.post(f"/mounted{PATH}", json=payload)

    assert [response.status_code for response in (success, invalid, unexpected)] == [200, 422, 500]
    assert address not in invalid.text
    assert address not in unexpected.text
    for response in (success, invalid, unexpected):
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_range_passes_the_remaining_budget_to_mapbox_without_changing_other_requests(provider, monkeypatch):
    def fetch(url, _timeout):
        if "/geocoding/" in url:
            return {"features": [{"center": [BASE_COORDS[1], BASE_COORDS[0]]}]}
        return {"routes": [{"distance": 0, "duration": 0}]}

    transport = Mock(side_effect=fetch)
    mapbox = MapboxRoutingProvider("synthetic", timeout_seconds=30, fetch_json=transport)
    monkeypatch.setattr(tools, "routing_provider", mapbox)
    response = provider.post(PATH, json={
        **REQUEST, "limit": 1, "client_address": "20 Main Street", "is_instructor_facility": False,
    })

    assert response.status_code == 200, response.text
    assert transport.call_count >= 2
    assert all(0 < call.args[1] <= availability_search._SEARCH_SECONDS for call in transport.call_args_list)
    mapbox.geocode("another address")
    assert transport.call_args.args[1] == 30


def test_google_source_receives_a_bounded_database_session_factory(provider, monkeypatch):
    _connect_calendar(monkeypatch, Mock())
    read = Mock(return_value=CalendarBusySnapshot("verified", ()))

    def make_source(_factory, session_factory):
        with session_factory() as session:
            bounded = session.execute(text("""
                SELECT current_setting('lock_timeout')::interval = INTERVAL '500 milliseconds'
                    AND current_setting('statement_timeout')::interval <= INTERVAL '15 seconds'
            """)).scalar_one()
            assert bounded
        return SimpleNamespace(query_busy_periods=read)

    monkeypatch.setattr(GoogleCalendarBusySourceFactory, "create_for", make_source)
    response = provider.post(PATH, json=REQUEST)

    assert response.status_code == 200, response.text
    read.assert_called_once()


def test_routing_cache_statements_use_the_remaining_budget(provider, monkeypatch):
    clock = 10.0

    def verify_cache_session(session, _key):
        nonlocal clock
        read_timeout = text("SELECT current_setting('statement_timeout')::interval")
        first = session.execute(read_timeout).scalar_one()
        clock += 1
        second = session.execute(read_timeout).scalar_one()
        assert first - second == timedelta(seconds=1)
        return None

    monkeypatch.setattr(availability_search, "monotonic", lambda: clock)
    cache = routing_cache.DatabaseRouteEstimateCache()
    monkeypatch.setattr(routing_cache, "_query_cache_row", verify_cache_session)
    monkeypatch.setattr(tools, "routing_provider", routing_cache.CachingRoutingProvider(tools.routing_provider, cache))
    response = provider.post(PATH, json={**REQUEST, "limit": 1})

    assert response.status_code == 200, response.text
    assert routing_cache.route_cache_session_factory.get() is None


def test_search_capacity_fails_before_planning(provider, monkeypatch):
    permits = provider.app.state.provider_availability_reads
    start = Mock()
    monkeypatch.setattr(api_services, "search_one_time_availability", start)
    for _ in range(4):
        assert permits.acquire(blocking=False)
    try:
        response = provider.post(PATH, json=REQUEST)
    finally:
        for _ in range(4):
            permits.release()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AVAILABILITY_SEARCH_BUDGET_EXCEEDED"
    start.assert_not_called()


def test_closed_client_windows_do_not_require_a_calendar_read_or_fence(provider, monkeypatch):
    claim = Mock(side_effect=ExternalCalendarTimeout())
    monkeypatch.setattr(availability_search, "claim_google_calendar_availability_fence", claim)
    response = provider.post(PATH, json={
        **REQUEST, "windows": [{"days": ["thursday"], "start_time": "19:00", "end_time": "20:00"}],
    })
    assert response.status_code == 200, response.text
    assert response.json()["empty_reason"] == "outside_client_windows"
    claim.assert_not_called()


def test_policy_query_timeout_returns_a_retryable_failure(provider, monkeypatch):
    def locked_policy(*_args, **_kwargs):
        raise InvalidSchedulerPolicy("Policy read failed.") from OperationalError(None, None, LockNotAvailable())

    monkeypatch.setattr(availability_search, "load_buffer_policy", locked_policy)
    response = provider.post(PATH, json=REQUEST)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "AVAILABILITY_SEARCH_BUDGET_EXCEEDED"


def test_search_does_not_quote_inbound_travel_for_an_excluded_gap(provider, monkeypatch):
    with db.SessionLocal.begin() as session:
        session.add(_block("midday anchor", "2026-11-24T12:00:00-06:00", "2026-11-24T12:30:00-06:00", located=True))
    estimate = tools.routing_provider.estimate_route

    def route(*args, departure_time, **kwargs):
        if departure_time == datetime.fromisoformat("2026-11-24T14:00:00+00:00"):
            raise RoutingTimeout("Unrelated morning route timed out.")
        return estimate(*args, departure_time=departure_time, **kwargs)

    monkeypatch.setattr(tools.routing_provider, "estimate_route", route)
    response = provider.post(PATH, json={
        **REQUEST, "duration_mins": 15,
        "windows": [{"days": ["tuesday"], "start_time": "14:00", "end_time": "14:15"}],
    })
    assert response.status_code == 200, response.text
    assert [slot["start_iso"] for slot in response.json()["available_slots"]] == ["2026-11-24T14:00:00-06:00"]
