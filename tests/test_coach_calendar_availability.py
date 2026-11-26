from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import create_app
from app.calendar_connections import replace_google_calendar_connection
from app.calendar_credentials import CalendarCredentialCipher
from app.external_calendar import CalendarBusyInterval, CalendarBusySnapshot
from app.google_calendar_busy_source_factory import GoogleCalendarBusySourceFactory
from app.models import ProviderAccountDB
from app.routes import coach as coach_routes
from tests.conftest import login_coach


AVAILABILITY_PATH = "/api/coach/availability"
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


@pytest.fixture()
def connected_calendar(authenticated_api_client):
    with db.SessionLocal.begin() as session:
        account = session.query(ProviderAccountDB).filter_by(instructor_id="inst_001").one()
        replace_google_calendar_connection(
            session,
            account_id=account.id,
            refresh_token="fake-refresh-token",
            cipher=CalendarCredentialCipher(
                active_key_id="test-key",
                keys={"test-key": b"k" * 32},
            ),
            now=WINDOW_START,
        )
    return authenticated_api_client


def test_availability_requires_auth_before_building_calendar_source(monkeypatch):
    build_source = Mock(side_effect=AssertionError("Unauthenticated request reached calendar"))
    monkeypatch.setattr(coach_routes, "GoogleCalendarAvailabilitySource", build_source)
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
