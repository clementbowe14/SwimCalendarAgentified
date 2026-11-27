from datetime import date, datetime, timedelta, timezone
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import coach_agenda, db, timezones, tools
from app.api import create_app
from app.calendar_connections import disconnect_google_calendar, replace_google_calendar_connection
from app.calendar_credentials import CalendarCredentialCipher
from app.engine import BufferPolicy, DistanceBufferTier
from app.external_calendar import CalendarBusyInterval, CalendarBusySnapshot, ExternalCalendarTimeout
from app.google_calendar_busy_source_factory import GoogleCalendarBusySourceFactory
from app.models import AppointmentDB, InstructorDB, ProviderAccountDB
from app.routes import coach as coach_routes
from app.routing import RoutingTimeout


PATH = "/api/coach/agenda"
DAY = date(2026, 8, 25)
NOW = datetime(2026, 8, 25, 15, tzinfo=timezone.utc)
PROVIDER = "inst_001"
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
    response = client.get(PATH, params={"date": selected_date.isoformat()})
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

    response = authenticated_api_client.get(PATH)

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


@pytest.mark.parametrize("outage", ["calendar", "routing"])
def test_agenda_keeps_booked_rows_when_planning_fails(authenticated_api_client, monkeypatch, outage):
    with db.SessionLocal.begin() as session:
        _add_event(session, "next-appointment", _hour(17), _hour(18))
    route = Mock(side_effect=RoutingTimeout())
    monkeypatch.setattr(tools, "routing_provider", SimpleNamespace(get_distance_and_travel_time=route))
    if outage == "calendar":
        _connect_calendar(monkeypatch, Mock(side_effect=ExternalCalendarTimeout()))
    else:
        _connect_calendar(monkeypatch, Mock(return_value=CalendarBusySnapshot(
            "verified", (CalendarBusyInterval(_hour(16), _hour(16, 15)),)
        )))

    agenda = _agenda(authenticated_api_client)

    assert agenda["status"] == f"{outage}_unavailable"
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

    assert anonymous.get(PATH).status_code == 401
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

    response = authenticated_api_client.get(PATH, params={
        "date": DAY.isoformat(), "instructor_id": "other-provider",
    })

    assert response.status_code == 200, response.text
    assert response.json()["instructor_id"] == PROVIDER
    assert "own-appointment" in response.text
    assert "private-other-appointment" not in response.text
