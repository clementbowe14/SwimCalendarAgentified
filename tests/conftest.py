import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SWIM_ENV", "test")
os.environ.setdefault("SWIM_ENABLE_DEMO_DATA", "true")
os.environ.setdefault("SWIM_ENABLE_DEMO_AUTH", "true")

from app import db
from app.api import create_app
from app.auth import CSRF_HEADER_NAME, CSRF_HEADER_VALUE, SESSION_COOKIE_NAME
from tests.db_test_utils import reset_postgres_database
from tests.public_contract_safety import PUBLIC_FORBIDDEN_KEYS, flatten_keys


FORBIDDEN_PUBLIC_VALUE_PREFIXES = ("app_", "client_", "inst_")


@pytest.fixture()
def migrated_postgres_db() -> Iterator[None]:
    reset_postgres_database()
    try:
        yield
    finally:
        db.configure_database()


@pytest.fixture()
def api_client(migrated_postgres_db: None) -> Iterator[TestClient]:
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture()
def coach_auth_headers(api_client: TestClient) -> dict[str, str]:
    return login_coach(api_client)


@pytest.fixture()
def authenticated_api_client(api_client: TestClient, coach_auth_headers: dict[str, str]) -> Iterator[TestClient]:
    api_client.headers.update(coach_auth_headers)
    yield api_client


def login_coach(
    client: TestClient,
    *,
    email: str = "coach@example.com",
    password: str = "swim-dev-pass",
) -> dict[str, str]:
    response = client.post(
        "/api/auth/coach/login",
        json={"email": email, "password": password},
        headers={CSRF_HEADER_NAME: CSRF_HEADER_VALUE},
    )
    assert response.status_code == 200, response.text
    session_token = response.cookies.get(SESSION_COOKIE_NAME)
    assert session_token
    client.cookies.clear()
    return {
        "Cookie": f"{SESSION_COOKIE_NAME}={session_token}",
        CSRF_HEADER_NAME: CSRF_HEADER_VALUE,
    }


@pytest.fixture()
def assert_public_payload_safe():
    return assert_public_payload_is_safe


def assert_public_payload_is_safe(payload: Any) -> None:
    keys = flatten_keys(payload)
    values = _flatten_values(payload)
    assert PUBLIC_FORBIDDEN_KEYS.isdisjoint(keys)
    assert not any(
        isinstance(value, str) and value.startswith(FORBIDDEN_PUBLIC_VALUE_PREFIXES)
        for value in values
    )

def _flatten_values(value: Any) -> set[Any]:
    values = set()
    if isinstance(value, dict):
        for nested in value.values():
            values.update(_flatten_values(nested))
    elif isinstance(value, list):
        for item in value:
            values.update(_flatten_values(item))
    elif isinstance(value, (str, int, float, bool, type(None))):
        values.add(value)
    return values
