from datetime import date, datetime, time, timedelta, timezone
from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app import coach_agenda, db, timezones, tools
from app.api import create_app
from app.calendar_connections import disconnect_google_calendar, replace_google_calendar_connection
from app.calendar_credentials import CalendarCredentialCipher
from app.engine import BufferPolicy, DistanceBufferTier
from app.external_calendar import CalendarBusyInterval, CalendarBusySnapshot, ExternalCalendarTimeout
from app.google_calendar_busy_source_factory import GoogleCalendarBusySourceFactory
from app.models import AppointmentDB, InstructorDB, ProviderAccountDB, ProviderWorkingHoursDB, SchedulerSettingsDB, TimeSlotDTO
from app.provider_settings import ProviderLocation, ProviderService
from app.routes import coach as coach_routes
from app.routing import RouteEstimate, RoutingTimeout
from app.routing_cache import CachingRoutingProvider, InMemoryRouteEstimateCache
from app.routing_providers import MapboxRoutingProvider
from app.scheduler_settings import InvalidSchedulerPolicy, default_scheduler_rules


PATH = "/api/coach/agenda"
DAY = date(2026, 8, 25)
NOW = datetime(2026, 8, 25, 15, tzinfo=timezone.utc)
PROVIDER = "inst_001"
OWNER = {"X-Expected-Instructor-Id": PROVIDER}
HOME = ("Client location", 30.33, -97.72)
SERVICE = ("Service location", 30.2672, -97.7431)
POLICY = BufferPolicy(
    distance_buffers_enabled=True,
    post_break_recovery_mins=30,
    distance_buffer_tiers=(DistanceBufferTier(0, None, 5),),
)


@pytest.fixture(autouse=True)
def agenda_clock(monkeypatch):
    monkeypatch.setattr(coach_routes, "get_coach_agenda", partial(coach_agenda.get_coach_agenda, now=NOW))


def _hour(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 25, hour, minute, tzinfo=timezone.utc)


def _interval(item: dict) -> tuple[datetime, datetime]:
    return datetime.fromisoformat(item["start_time"]), datetime.fromisoformat(item["end_time"])


def _add_event(
    session,
    identifier: str,
    start: datetime,
    end: datetime,
    *,
    location: tuple[str, float, float] | None = HOME,
    blocker_kind: str | None = None,
    instructor_id: str = PROVIDER,
) -> None:
    address, lat, lng = location or (None, None, None)
    session.add(AppointmentDB(
        id=identifier,
        instructor_id=instructor_id,
        client_id="client_alice" if blocker_kind is None else None,
        title=identifier,
        start_time=timezones.to_utc_storage(start),
        end_time=timezones.to_utc_storage(end),
        location_address=address,
        lat=lat,
        lng=lng,
        event_type="block" if blocker_kind else "lesson",
        blocker_kind=blocker_kind,
        is_break=blocker_kind == "break",
    ))


def _connect_calendar(monkeypatch, query: Mock) -> None:
    with db.SessionLocal.begin() as session:
        account = session.query(ProviderAccountDB).filter_by(instructor_id=PROVIDER).one()
        replace_google_calendar_connection(
            session,
            account_id=account.id,
            refresh_token="synthetic-agenda-refresh",
            cipher=CalendarCredentialCipher(active_key_id="test", keys={"test": b"g" * 32}),
            now=NOW,
        )
    monkeypatch.setattr(
        GoogleCalendarBusySourceFactory, "create_for",
        lambda *_: SimpleNamespace(query_busy_periods=query),
    )


def _agenda(client, selected_date: date = DAY) -> dict:
    response = client.get(PATH, params={"date": selected_date.isoformat()}, headers=OWNER)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    return response.json()


def test_appointment_routes_and_openings_use_shared_policy(authenticated_api_client, monkeypatch):
    with db.SessionLocal.begin() as session:
        provider = session.get(InstructorDB, PROVIDER)
        provider.service_address, provider.service_lat, provider.service_lng = SERVICE
        provider.default_duration_mins = 45
        _add_event(session, "first-appointment", _hour(14), _hour(14, 45))
        _add_event(session, "lunch", _hour(16), _hour(16, 30), location=None, blocker_kind="break")
        _add_event(session, "next-appointment", _hour(17, 30), _hour(18, 15), location=SERVICE)
    query = Mock(return_value=CalendarBusySnapshot(
        "verified", (CalendarBusyInterval(_hour(16, 35), _hour(16, 40)),)
    ))
    _connect_calendar(monkeypatch, query)
    route = Mock(side_effect=lambda origin, dest, **_: (0.0, 0) if origin == dest else (4.0, 20))
    monkeypatch.setattr(tools, "routing_provider", SimpleNamespace(get_distance_and_travel_time=route))
    monkeypatch.setattr(coach_agenda, "load_buffer_policy", lambda *_args, **_kwargs: POLICY)

    agenda = _agenda(authenticated_api_client)

    assert agenda["status"] == "ready"
    assert agenda["instructor_id"] == PROVIDER
    assert agenda["today"] == DAY.isoformat()
    assert agenda["timezone"] == "America/Chicago"
    next_up = agenda["next_up"]
    assert next_up["appointment_id"] == "next-appointment"
    assert datetime.fromisoformat(next_up["leave_by"]) == _hour(16, 50)
    assert (next_up["travel_time_mins"], next_up["buffer_mins"]) == (20, 20)
    query.assert_called_once_with(
        instructor_id=PROVIDER, window_start=_hour(5), window_end=_hour(5) + timedelta(days=1)
    )

    items = agenda["items"]
    assert [_interval(item) for item in items] == sorted(_interval(item) for item in items)
    assert all(
        _interval(first)[1] <= _interval(next_)[0]
        for first, next_ in zip(items, items[1:])
    )
    by_id = {item["id"]: item for item in items}
    assert by_id["lunch"]["kind"] == "break"
    assert _interval(by_id["agenda-travel-next-appointment"]) == (_hour(16, 50), _hour(17, 10))
    assert _interval(by_id["agenda-buffer-next-appointment"]) == (_hour(17, 10), _hour(17, 30))
    opaque = next(item for item in items if item["kind"] == "calendar_busy")
    assert _interval(opaque) == (_hour(16, 35), _hour(16, 40))
    assert opaque["title"] == "Calendar busy"
    assert all(opaque[field] is None for field in ("location", "client_id", "note", "status"))
    windows = [item for item in items if item["kind"] == "open_gap"]
    assert [_interval(item) for item in windows] == [
        (_hour(15, 15), _hour(16)), (_hour(18, 30), _hour(22)),
    ]
    assert all("45 min" in item["note"] for item in windows)


def test_agenda_defaults_to_the_providers_day_and_handles_empty_days(authenticated_api_client, monkeypatch):
    monkeypatch.setattr(
        coach_routes, "get_coach_agenda", partial(coach_agenda.get_coach_agenda, now=_hour(3))
    )
    factory = Mock(side_effect=AssertionError("No Google call when disconnected"))
    monkeypatch.setattr(GoogleCalendarBusySourceFactory, "create_for", factory)

    response = authenticated_api_client.get(PATH, headers=OWNER)

    assert response.status_code == 200
    assert response.json()["date"] == response.json()["today"] == "2026-08-24"
    assert response.json()["items"] == []
    assert response.json()["next_up"] is None
    factory.assert_not_called()


def test_agenda_splits_cross_midnight_appointments_on_correct_days(authenticated_api_client):
    with db.SessionLocal.begin() as session:
        _add_event(session, "overnight", _hour(4, 30), _hour(5, 30))
    previous = _agenda(authenticated_api_client, DAY - timedelta(days=1))
    current = _agenda(authenticated_api_client)
    assert _interval(next(item for item in previous["items"] if item["id"] == "overnight")) == (
        _hour(4, 30), _hour(5),
    )
    assert _interval(next(item for item in current["items"] if item["id"] == "overnight")) == (
        _hour(5), _hour(5, 30),
    )
    assert previous["next_up"] is None and current["next_up"] is None


@pytest.mark.parametrize(("day", "start", "end"), [
    (date(2026, 3, 8), datetime(2026, 3, 8, 5, tzinfo=timezone.utc), datetime(2026, 3, 9, 4, tzinfo=timezone.utc)),
    (date(2026, 11, 1), datetime(2026, 11, 1, 4, tzinfo=timezone.utc), datetime(2026, 11, 2, 5, tzinfo=timezone.utc)),
])
def test_agenda_uses_full_dst_adjusted_local_calendar_day(authenticated_api_client, monkeypatch, day, start, end):
    with db.SessionLocal.begin() as session:
        session.get(InstructorDB, PROVIDER).timezone = "America/New_York"
    query = Mock(return_value=CalendarBusySnapshot("verified", (CalendarBusyInterval(start, end),)))
    _connect_calendar(monkeypatch, query)
    agenda = _agenda(authenticated_api_client, day)

    query.assert_called_once_with(instructor_id=PROVIDER, window_start=start, window_end=end)
    assert agenda["timezone"] == "America/New_York"
    assert [item["kind"] for item in agenda["items"]] == ["calendar_busy"]
    assert _interval(agenda["items"][0]) == (start, end)


@pytest.mark.parametrize("outage", ["calendar", "routing", "policy", "route_cache"])
def test_agenda_keeps_booked_rows_when_planning_fails(authenticated_api_client, monkeypatch, outage):
    with db.SessionLocal.begin() as session:
        _add_event(session, "next-appointment", _hour(17), _hour(18))
    failure = OperationalError(None, None, RuntimeError()) if outage == "route_cache" else RoutingTimeout()
    route = Mock(side_effect=failure)
    monkeypatch.setattr(tools, "routing_provider", SimpleNamespace(get_distance_and_travel_time=route))
    if outage == "policy":
        monkeypatch.setattr(coach_agenda, "load_buffer_policy", Mock(side_effect=InvalidSchedulerPolicy()))
    if outage == "calendar":
        _connect_calendar(monkeypatch, Mock(side_effect=ExternalCalendarTimeout()))
    else:
        _connect_calendar(monkeypatch, Mock(return_value=CalendarBusySnapshot(
            "verified", (CalendarBusyInterval(_hour(16), _hour(16, 15)),)
        )))

    agenda = _agenda(authenticated_api_client)

    assert agenda["status"] == ("calendar_unavailable" if outage == "calendar" else "routing_unavailable")
    assert [item["kind"] for item in agenda["items"]] == (
        ["lesson"] if outage == "calendar" else ["calendar_busy", "lesson"]
    )
    assert agenda["next_up"] == {
        "appointment_id": "next-appointment",
        "leave_by": None,
        "travel_time_mins": None,
        "buffer_mins": None,
    }
    if outage == "calendar":
        route.assert_not_called()


def test_agenda_requires_auth_and_matching_expected_provider(authenticated_api_client, monkeypatch):
    get_agenda = Mock(side_effect=AssertionError("Unauthorized agenda reached provider data"))
    monkeypatch.setattr(coach_routes, "get_coach_agenda", get_agenda)
    anonymous = TestClient(create_app())

    assert anonymous.get(PATH, headers=OWNER).status_code == 401
    assert authenticated_api_client.get(PATH).status_code == 422
    response = authenticated_api_client.get(PATH, headers={"X-Expected-Instructor-Id": "other-provider"})
    assert response.status_code == 409
    get_agenda.assert_not_called()


def test_agenda_discards_old_calendar_and_openings_if_connection_changes(authenticated_api_client, monkeypatch):
    with db.SessionLocal.begin() as session:
        _add_event(session, "next-appointment", _hour(17), _hour(18))
    _connect_calendar(monkeypatch, Mock(return_value=CalendarBusySnapshot(
        "verified", (CalendarBusyInterval(_hour(16), _hour(16, 15)),)
    )))
    reconnect_checked = False

    def route(*_args, **_kwargs):
        nonlocal reconnect_checked
        if not reconnect_checked:
            with db.SessionLocal.begin() as session:
                account = session.query(ProviderAccountDB).filter_by(instructor_id=PROVIDER).one()
                disconnect_google_calendar(session, account_id=account.id)
            reconnect_checked = True
        return 0.0, 0

    monkeypatch.setattr(tools, "routing_provider", SimpleNamespace(get_distance_and_travel_time=route))

    agenda = _agenda(authenticated_api_client)

    assert reconnect_checked
    assert agenda["status"] == "calendar_unavailable"
    assert [item["id"] for item in agenda["items"]] == ["next-appointment"]
    assert agenda["next_up"]["leave_by"] is None


def test_agenda_ignores_untrusted_provider_query_and_excludes_other_appointments(authenticated_api_client):
    with db.SessionLocal.begin() as session:
        session.add(InstructorDB(
            id="other-provider", name="Other provider", main_address=SERVICE[0],
            lat=SERVICE[1], lng=SERVICE[2],
        ))
        session.flush()
        _add_event(session, "private-other-appointment", _hour(17), _hour(18), instructor_id="other-provider")
        _add_event(session, "own-appointment", _hour(17), _hour(18))

    response = authenticated_api_client.get(PATH, headers=OWNER, params={
        "date": DAY.isoformat(), "instructor_id": "other-provider",
    })

    assert response.status_code == 200, response.text
    assert response.json()["instructor_id"] == PROVIDER
    assert "own-appointment" in response.text
    assert "private-other-appointment" not in response.text


def test_leave_by_uses_the_route_at_the_published_departure(authenticated_api_client, monkeypatch):
    with db.SessionLocal.begin() as session:
        provider = session.get(InstructorDB, PROVIDER)
        provider.base_address, provider.base_lat, provider.base_lng = SERVICE
        _add_event(session, "traffic-appointment", _hour(18), _hour(18, 45))

    def route(origin, destination, *, departure_time):
        if origin == destination:
            return 0, 0
        return 4, 60 if _hour(17, 30) <= departure_time < _hour(18) else 5

    monkeypatch.setattr(tools, "routing_provider", SimpleNamespace(get_distance_and_travel_time=route))
    agenda = _agenda(authenticated_api_client)
    next_up = agenda["next_up"]
    departure = datetime.fromisoformat(next_up["leave_by"])
    assert departure == _hour(17)
    assert next_up["travel_time_mins"] == route(SERVICE[1:], HOME[1:], departure_time=departure)[1]
    assert departure + timedelta(minutes=next_up["travel_time_mins"] + next_up["buffer_mins"]) <= _hour(18)


def test_leave_by_verifies_exact_departure_with_traffic_bucketing(authenticated_api_client, monkeypatch):
    with db.SessionLocal.begin() as session:
        provider = session.get(InstructorDB, PROVIDER)
        provider.base_address, provider.base_lat, provider.base_lng = SERVICE
        _add_event(session, "traffic-appointment", _hour(18), _hour(18, 45))

    class TrafficProvider(MapboxRoutingProvider):
        def estimate_route(self, origin, destination, departure_time=None, traffic_mode="none"):
            assert traffic_mode == "live"
            minutes = 0 if origin == destination else 25 if departure_time >= _hour(17, 50) else 8
            return RouteEstimate(4, minutes, minutes * 60, "test")

    routing = CachingRoutingProvider(
        TrafficProvider("synthetic"), InMemoryRouteEstimateCache(), traffic_mode="live"
    )
    monkeypatch.setattr(tools, "routing_provider", routing)

    next_up = _agenda(authenticated_api_client)["next_up"]
    departure = datetime.fromisoformat(next_up["leave_by"])
    assert departure == _hour(17, 35)
    _, actual = routing.get_distance_and_travel_time_at_exact_departure(
        SERVICE[1:], HOME[1:], departure_time=departure
    )
    assert departure + timedelta(minutes=actual + next_up["buffer_mins"]) <= _hour(18)


def test_openings_account_for_adjacent_appointments_outside_working_hours(authenticated_api_client, monkeypatch):
    with db.SessionLocal.begin() as session:
        provider = session.get(InstructorDB, PROVIDER)
        provider.base_address, provider.base_lat, provider.base_lng = SERVICE
        provider.service_address, provider.service_lat, provider.service_lng = SERVICE
        provider.default_duration_mins = 45
        _add_event(session, "before-shift", _hour(12, 30), _hour(13))
        _add_event(session, "after-shift", _hour(22), _hour(22, 30))
    monkeypatch.setattr(
        coach_routes, "get_coach_agenda", partial(coach_agenda.get_coach_agenda, now=_hour(12))
    )
    monkeypatch.setattr(coach_agenda, "load_buffer_policy", lambda *_args, **_kwargs: POLICY)
    route = lambda origin, dest, **_: (0, 0) if origin == dest else (4, 20)
    monkeypatch.setattr(tools, "routing_provider", SimpleNamespace(get_distance_and_travel_time=route))

    agenda = _agenda(authenticated_api_client)
    openings = [item for item in agenda["items"] if item["kind"] == "open_gap"]
    assert [_interval(item) for item in openings] == [(_hour(13, 30), _hour(21, 30))]
    available = tools.find_available_swim_slots(
        PROVIDER, DAY.isoformat(), SERVICE[0], is_instructor_facility=True, duration_mins=45, buffer_policy=POLICY,
    )
    assert {datetime.fromisoformat(start) for item in openings for start in item["start_times"]} == {
        slot.start_time for slot in available
    }


def test_midnight_endpoint_remains_a_routing_anchor_without_an_appointment_row(
    authenticated_api_client, monkeypatch
):
    midnight = _hour(0) + timedelta(days=1)
    with db.SessionLocal.begin() as session:
        provider = session.get(InstructorDB, PROVIDER)
        provider.timezone = "UTC"
        provider.base_address, provider.base_lat, provider.base_lng = SERVICE
        provider.service_address, provider.service_lat, provider.service_lng = SERVICE
        provider.default_duration_mins = 45
        hours = session.get(ProviderWorkingHoursDB, (PROVIDER, DAY.weekday()))
        hours.end_time = time(23, 59)
        _add_event(session, "tomorrow-at-midnight", midnight, midnight + timedelta(minutes=45))

    def route(origin, destination, **_):
        return (0, 0) if origin == destination else (4, 20)

    monkeypatch.setattr(coach_agenda, "load_buffer_policy", lambda *_args, **_kwargs: POLICY)
    monkeypatch.setattr(tools, "routing_provider", SimpleNamespace(get_distance_and_travel_time=route))

    agenda = _agenda(authenticated_api_client)
    assert all(item["id"] != "tomorrow-at-midnight" for item in agenda["items"])
    assert next(item for item in agenda["items"] if item["kind"] == "travel")["travel_time_mins"] == 20
    available = tools.find_available_swim_slots(
        PROVIDER, DAY.isoformat(), SERVICE[0], is_instructor_facility=True, duration_mins=45, buffer_policy=POLICY,
    )
    assert available[-1].start_time == _hour(22, 45)
    openings = [item for item in agenda["items"] if item["kind"] == "open_gap"]
    assert datetime.fromisoformat(openings[-1]["start_times"][-1]) == available[-1].start_time


def test_start_at_local_midnight_accounts_for_an_appointment_ending_at_midnight(
    authenticated_api_client, monkeypatch
):
    midnight = _hour(0)
    with db.SessionLocal.begin() as session:
        provider = session.get(InstructorDB, PROVIDER)
        provider.timezone = "UTC"
        provider.base_address, provider.base_lat, provider.base_lng = SERVICE
        provider.service_address, provider.service_lat, provider.service_lng = SERVICE
        provider.default_duration_mins = 45
        hours = session.get(ProviderWorkingHoursDB, (PROVIDER, DAY.weekday()))
        hours.start_time = time(0)
        hours.end_time = time(3)
        _add_event(session, "yesterday-until-midnight", midnight - timedelta(minutes=45), midnight)

    def route(origin, destination, **_):
        return (0, 0) if origin == destination else (4, 20)

    monkeypatch.setattr(coach_routes, "get_coach_agenda", partial(coach_agenda.get_coach_agenda, now=midnight))
    monkeypatch.setattr(coach_agenda, "load_buffer_policy", lambda *_args, **_kwargs: POLICY)
    monkeypatch.setattr(tools, "routing_provider", SimpleNamespace(get_distance_and_travel_time=route))

    agenda = _agenda(authenticated_api_client)
    assert all(item["id"] != "yesterday-until-midnight" for item in agenda["items"])
    opening, = (item for item in agenda["items"] if item["kind"] == "open_gap")
    assert datetime.fromisoformat(opening["start_times"][0]) == _hour(0, 30)


def test_opening_bands_expose_only_verified_start_times():
    service = ProviderService("Appointment", 45, ProviderLocation(SERVICE[0], SERVICE[1], SERVICE[2]))
    slots = [
        TimeSlotDTO(start_time=start, end_time=start + timedelta(minutes=45),
                    travel_time_mins=0, buffer_mins=0, distance_miles=0)
        for start in (_hour(16), _hour(16, 30))
    ]
    opening, = coach_agenda._bookable_windows(slots, [], NOW, service)

    assert opening.start_times == [_hour(16), _hour(16, 30)]
    assert _hour(16, 15) not in opening.start_times


@pytest.mark.parametrize("change", ["blocker", "policy", "clock"])
def test_agenda_rechecks_state_and_clock_before_publishing_openings(authenticated_api_client, monkeypatch, change):
    clock = NOW
    committed = False

    class AgendaClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock

    def route(*_args, **_kwargs):
        nonlocal clock, committed
        if not committed and change == "blocker":
            with db.SessionLocal.begin() as session:
                _add_event(session, "new-blocker", _hour(16), _hour(16, 30), location=None, blocker_kind="personal")
        if change == "clock":
            clock = _hour(16, 7)
        committed = True
        return 0, 0

    monkeypatch.setattr(coach_agenda, "datetime", AgendaClock)
    monkeypatch.setattr(coach_routes, "get_coach_agenda", coach_agenda.get_coach_agenda)
    monkeypatch.setattr(
        coach_agenda, "load_buffer_policy",
        lambda *_a, **_kw: replace(POLICY, distance_buffers_enabled=False) if committed and change == "policy" else POLICY,
    )
    monkeypatch.setattr(tools, "routing_provider", SimpleNamespace(get_distance_and_travel_time=route))
    agenda = _agenda(authenticated_api_client)
    assert committed
    if change == "clock":
        assert agenda["status"] == "ready"
        starts = [
            datetime.fromisoformat(start)
            for item in agenda["items"] if item["kind"] == "open_gap"
            for start in item["start_times"]
        ]
        assert starts and min(starts) == _hour(16, 15)
    else:
        assert agenda["status"] == "schedule_changed"
        assert [item["id"] for item in agenda["items"]] == (["new-blocker"] if change == "blocker" else [])


def test_agenda_final_read_cannot_mix_schedule_and_policy_versions(authenticated_api_client, monkeypatch):
    db.save_scheduler_settings(PROVIDER, default_scheduler_rules().model_dump(mode="json"))
    routed = False
    original_read_policy = db.get_scheduling_policy

    def route(*_args, **_kwargs):
        nonlocal routed
        if not routed:
            with db.SessionLocal.begin() as writer:
                writer.get(SchedulerSettingsDB, PROVIDER).buffers_enabled = False
            routed = True
        return 0, 0

    def update_during_final_policy_read(instructor_id, *, session=None):
        if routed:
            with db.SessionLocal.begin() as writer:
                writer.get(SchedulerSettingsDB, PROVIDER).buffers_enabled = True
                _add_event(writer, "blocker-while-reading-policy", _hour(16), _hour(17), location=None, blocker_kind="break")
        return original_read_policy(instructor_id, session=session)

    monkeypatch.setattr(tools, "routing_provider", SimpleNamespace(get_distance_and_travel_time=route))
    monkeypatch.setattr(db, "get_scheduling_policy", update_during_final_policy_read)

    agenda = _agenda(authenticated_api_client)
    assert routed
    assert agenda["status"] == "schedule_changed"
    assert all(item["kind"] != "open_gap" for item in agenda["items"])
