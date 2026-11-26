from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient
from sqlalchemy import event

from app import calendar_connections, db
from app.auth import provision_provider_account
from app.calendar_credentials import CalendarCredentialCipher
from app.models import InstructorDB, ProviderAccountDB, ProviderCalendarConnectionDB
from tests.conftest import login_coach


UTC = dt.timezone.utc
CONNECTED_AT = dt.datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
AUTHORIZATION_REQUIRED_AT = CONNECTED_AT + dt.timedelta(minutes=5)
KEY_ID = "calendar-status-api-key"
CIPHER = CalendarCredentialCipher(
    active_key_id=KEY_ID,
    keys={KEY_ID: b"s" * 32},
)
ROUTE = "/api/coach/calendar-connections/google"
RESPONSE_KEYS = {
    "provider",
    "state",
    "connected_at",
    "updated_at",
    "authorization_required_at",
}


def test_status_requires_authentication_and_projects_disconnected_without_configuration(
    api_client: TestClient,
    coach_auth_headers: dict[str, str],
    monkeypatch,
):
    unauthenticated = api_client.get(ROUTE)

    for name in (
        "SWIM_CALENDAR_CREDENTIAL_ACTIVE_KEY_ID",
        "SWIM_CALENDAR_CREDENTIAL_KEYS",
        "SWIM_GOOGLE_CALENDAR_OAUTH_CLIENT_ID",
        "SWIM_GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET",
        "SWIM_GOOGLE_CALENDAR_OAUTH_REDIRECT_URI",
    ):
        monkeypatch.delenv(name, raising=False)
    response = api_client.get(
        ROUTE,
        headers={"Cookie": coach_auth_headers["Cookie"]},
    )

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.json() == {
        "provider": "google",
        "state": "disconnected",
        "connected_at": None,
        "updated_at": None,
        "authorization_required_at": None,
    }


def test_status_projects_connected_and_authorization_required_without_secrets(
    authenticated_api_client: TestClient,
):
    account_id = _account_id_for("inst_001")
    refresh_token = "calendar-status-refresh-token-must-not-leak"
    connected = _connect(account_id, refresh_token=refresh_token)

    statements: list[str] = []

    def capture_connection_query(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if "provider_calendar_connections" in statement:
            statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", capture_connection_query)
    try:
        response = authenticated_api_client.get(ROUTE)
    finally:
        event.remove(db.engine, "before_cursor_execute", capture_connection_query)

    assert response.status_code == 200, response.text
    assert len(statements) == 1
    assert "credential_" not in statements[0]
    payload = response.json()
    assert set(payload) == RESPONSE_KEYS
    assert payload["provider"] == "google"
    assert payload["state"] == "connected"
    assert _parse(payload["connected_at"]) == CONNECTED_AT
    assert _parse(payload["updated_at"]) == CONNECTED_AT
    assert payload["authorization_required_at"] is None
    _assert_private_values_absent(
        response.text,
        account_id,
        connected.id,
        refresh_token,
        KEY_ID,
    )

    with db.SessionLocal.begin() as session:
        calendar_connections.mark_google_calendar_authorization_required(
            session,
            account_id=account_id,
            expected_connection_id=connected.id,
            now=AUTHORIZATION_REQUIRED_AT,
        )

    recovery = authenticated_api_client.get(ROUTE)

    assert recovery.status_code == 200, recovery.text
    recovery_payload = recovery.json()
    assert set(recovery_payload) == RESPONSE_KEYS
    assert recovery_payload["state"] == "authorization_required"
    assert _parse(recovery_payload["connected_at"]) == CONNECTED_AT
    assert _parse(recovery_payload["updated_at"]) == AUTHORIZATION_REQUIRED_AT
    assert (
        _parse(recovery_payload["authorization_required_at"])
        == AUTHORIZATION_REQUIRED_AT
    )
    _assert_private_values_absent(
        recovery.text,
        account_id,
        connected.id,
        refresh_token,
        KEY_ID,
    )


def test_status_is_bound_to_authenticated_provider_and_ignores_spoofed_identity(
    api_client: TestClient,
    coach_auth_headers: dict[str, str],
):
    second_account_id = _seed_second_provider()
    second_connection = _connect(
        second_account_id,
        refresh_token="second-provider-calendar-status-token",
    )
    second_headers = login_coach(
        api_client,
        email="calendar-status-second@example.com",
        password="calendar-status-second-password",
    )

    first = api_client.get(
        ROUTE,
        params={
            "account_id": second_account_id,
            "connection_id": second_connection.id,
            "provider": "google",
        },
        headers=coach_auth_headers,
    )
    second = api_client.get(
        ROUTE,
        params={"account_id": _account_id_for("inst_001")},
        headers=second_headers,
    )

    assert first.status_code == 200, first.text
    assert first.json()["state"] == "disconnected"
    assert second.status_code == 200, second.text
    assert second.json()["state"] == "connected"
    assert _parse(second.json()["connected_at"]) == CONNECTED_AT
    _assert_private_values_absent(
        first.text + second.text,
        second_account_id,
        second_connection.id,
        "second-provider-calendar-status-token",
        KEY_ID,
    )


def test_disconnect_requires_authentication_and_csrf(
    api_client: TestClient,
    coach_auth_headers: dict[str, str],
):
    _connect(_account_id_for("inst_001"), refresh_token="test-google-refresh")

    assert api_client.delete(ROUTE).status_code == 401
    assert api_client.delete(
        ROUTE, headers={"Cookie": coach_auth_headers["Cookie"]}
    ).status_code == 403
    assert api_client.get(ROUTE, headers=coach_auth_headers).json()["state"] == "connected"


def test_disconnect_is_scoped_to_authenticated_owner_and_repeatable(
    api_client: TestClient,
    coach_auth_headers: dict[str, str],
):
    first_id = _account_id_for("inst_001")
    _connect(first_id, refresh_token="test-first-refresh")
    second_id = _seed_second_provider()
    _connect(second_id, refresh_token="test-second-refresh")

    disconnected = api_client.delete(
        ROUTE, params={"account_id": second_id}, headers=coach_auth_headers
    )
    repeated = api_client.delete(ROUTE, headers=coach_auth_headers)

    assert disconnected.status_code == 200, disconnected.text
    assert disconnected.json() == {
        "provider": "google",
        "state": "disconnected",
        "connected_at": None,
        "updated_at": None,
        "authorization_required_at": None,
    }
    assert disconnected.headers["cache-control"] == "private, no-store"
    assert disconnected.headers["referrer-policy"] == "no-referrer"
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == disconnected.json()
    with db.SessionLocal() as session:
        remaining = session.query(ProviderCalendarConnectionDB).all()
        assert [connection.account_id for connection in remaining] == [second_id]


def _connect(
    account_id: str,
    *,
    refresh_token: str,
) -> ProviderCalendarConnectionDB:
    with db.SessionLocal.begin() as session:
        calendar_connections.replace_google_calendar_connection(
            session,
            account_id=account_id,
            refresh_token=refresh_token,
            cipher=CIPHER,
            now=CONNECTED_AT,
        )
    with db.SessionLocal() as session:
        row = session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=account_id
        ).one()
        session.expunge(row)
        return row


def _account_id_for(instructor_id: str) -> str:
    with db.SessionLocal() as session:
        return session.query(ProviderAccountDB).filter_by(
            instructor_id=instructor_id
        ).one().id


def _seed_second_provider() -> str:
    with db.SessionLocal.begin() as session:
        session.add(
            InstructorDB(
                id="inst_calendar_status_second",
                name="Calendar Status Second Provider",
                main_address="2 Status Way",
                lat=30.2,
                lng=-97.2,
                work_start_time="08:00",
                work_end_time="17:00",
                timezone="America/Chicago",
            )
        )
    return provision_provider_account(
        "inst_calendar_status_second",
        "calendar-status-second@example.com",
        "calendar-status-second-password",
    )


def _parse(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _assert_private_values_absent(serialized: str, *private_values: str) -> None:
    for private_value in private_values:
        assert private_value not in serialized
