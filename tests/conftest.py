from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import create_app
from tests.db_test_utils import reset_postgres_database


FORBIDDEN_PUBLIC_KEYS = {"instructor_id", "client_id"}
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
def assert_public_payload_safe():
    return assert_public_payload_has_no_internal_identifiers


def assert_public_payload_has_no_internal_identifiers(payload: Any) -> None:
    keys = _flatten_keys(payload)
    values = _flatten_values(payload)
    assert FORBIDDEN_PUBLIC_KEYS.isdisjoint(keys)
    assert not any(
        isinstance(value, str) and value.startswith(FORBIDDEN_PUBLIC_VALUE_PREFIXES)
        for value in values
    )


def _flatten_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.add(key)
            keys.update(_flatten_keys(nested))
    elif isinstance(value, list):
        for item in value:
            keys.update(_flatten_keys(item))
    return keys


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
