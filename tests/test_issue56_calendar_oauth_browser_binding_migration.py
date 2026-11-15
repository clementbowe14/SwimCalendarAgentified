import datetime
import hashlib

import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import LargeBinary, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app import db
from app.models import ProviderCalendarOAuthStateDB
from tests.db_test_utils import TEST_DATABASE_URL


PKCE_REVISION = "20261112_0017"
BINDING_REVISION = "20261113_0018"
TABLE_NAME = "provider_calendar_oauth_states"
COLUMN_NAME = "browser_binding_digest"
CHECK_NAME = "ck_provider_calendar_oauth_states_browser_binding_digest"
INDEX_NAME = "uq_provider_calendar_oauth_states_browser_binding_digest"
UTC = datetime.timezone.utc


@pytest.fixture(autouse=True)
def use_test_database_for_alembic(monkeypatch):
    monkeypatch.setenv("SWIM_DATABASE_URL", TEST_DATABASE_URL)


def test_browser_binding_revision_remains_in_linear_alembic_history():
    script = ScriptDirectory.from_config(_config())
    assert script.get_revision(BINDING_REVISION).down_revision == (
        PKCE_REVISION
    )


def test_schema_adds_one_optional_digest_with_fixed_invariants(
    migrated_postgres_db,
):
    inspector = inspect(db.engine)
    columns = {
        column["name"]: column
        for column in inspector.get_columns(TABLE_NAME)
    }
    binding = columns[COLUMN_NAME]
    assert isinstance(binding["type"], LargeBinary)
    assert binding["nullable"] is True
    assert binding["default"] is None

    checks = {
        constraint["name"]: constraint
        for constraint in inspector.get_check_constraints(TABLE_NAME)
    }
    assert CHECK_NAME in checks
    check_sql = checks[CHECK_NAME]["sqltext"]
    assert "octet_length(browser_binding_digest) = 32" in check_sql
    assert "browser_binding_digest <> state_digest" in check_sql

    indexes = {
        index["name"]: index
        for index in inspector.get_indexes(TABLE_NAME)
    }
    binding_index = indexes[INDEX_NAME]
    assert binding_index["unique"] is True
    assert binding_index["column_names"] == [COLUMN_NAME]
    predicate = str(
        binding_index["dialect_options"]["postgresql_where"]
    ).lower()
    assert "browser_binding_digest is not null" in predicate

    forbidden_columns = {
        "browser_secret",
        "raw_browser_secret",
        "flow_cookie",
        "cookie_value",
        "state",
        "raw_state",
        "code_verifier",
        "code_challenge",
        "authorization_code",
        "access_token",
        "refresh_token",
        "calendar_id",
    }
    assert forbidden_columns.isdisjoint(columns)


def test_upgrade_preserves_existing_unbound_states_without_backfill(
    migrated_postgres_db,
):
    command.downgrade(_config(), PKCE_REVISION)
    digest, _, _ = _insert_state(
        suffix="legacy-unbound",
        include_binding_column=False,
    )

    command.upgrade(_config(), BINDING_REVISION)

    with db.engine.connect() as connection:
        stored = connection.execute(
            text(
                "SELECT browser_binding_digest "
                "FROM provider_calendar_oauth_states "
                "WHERE state_digest = :digest"
            ),
            {"digest": digest},
        ).scalar_one()
    assert stored is None


@pytest.mark.parametrize("invalid_length", [31, 33])
def test_binding_digest_must_be_exactly_32_bytes_when_present(
    migrated_postgres_db,
    invalid_length,
):
    digest, _, _ = _insert_state(suffix=f"length-{invalid_length}")

    with pytest.raises(IntegrityError):
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE provider_calendar_oauth_states "
                    "SET browser_binding_digest = :binding "
                    "WHERE state_digest = :digest"
                ),
                {
                    "binding": b"b" * invalid_length,
                    "digest": digest,
                },
            )


def test_binding_digest_must_differ_from_its_state_digest(
    migrated_postgres_db,
):
    digest, _, _ = _insert_state(suffix="independence")

    with pytest.raises(IntegrityError):
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE provider_calendar_oauth_states "
                    "SET browser_binding_digest = state_digest "
                    "WHERE state_digest = :digest"
                ),
                {"digest": digest},
            )


def test_one_binding_digest_cannot_attach_to_two_states(
    migrated_postgres_db,
):
    shared_binding = hashlib.sha256(b"shared-browser-secret").digest()
    first_digest, _, _ = _insert_state(
        suffix="unique-first",
        browser_binding_digest=shared_binding,
    )
    second_digest, _, _ = _insert_state(suffix="unique-second")

    with pytest.raises(IntegrityError):
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE provider_calendar_oauth_states "
                    "SET browser_binding_digest = :binding "
                    "WHERE state_digest = :digest"
                ),
                {
                    "binding": shared_binding,
                    "digest": second_digest,
                },
            )

    with db.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT state_digest FROM provider_calendar_oauth_states "
                "WHERE browser_binding_digest = :binding"
            ),
            {"binding": shared_binding},
        ).scalars().all()
    assert rows == [first_digest]


def test_binding_digest_round_trips_through_model(
    migrated_postgres_db,
):
    binding_digest = hashlib.sha256(
        b"representative-browser-binding-digest"
    ).digest()
    state_digest, _, _ = _insert_state(
        suffix="digest-only",
        browser_binding_digest=binding_digest,
    )

    with db.SessionLocal() as session:
        stored = session.get(ProviderCalendarOAuthStateDB, state_digest)
        assert stored.browser_binding_digest == binding_digest


def test_empty_downgrade_and_reupgrade_round_trip(migrated_postgres_db):
    command.downgrade(_config(), PKCE_REVISION)
    inspector = inspect(db.engine)
    assert COLUMN_NAME not in {
        column["name"] for column in inspector.get_columns(TABLE_NAME)
    }
    assert _current_revision() == PKCE_REVISION

    command.upgrade(_config(), BINDING_REVISION)
    assert COLUMN_NAME in {
        column["name"]
        for column in inspect(db.engine).get_columns(TABLE_NAME)
    }
    assert _current_revision() == BINDING_REVISION


def test_expired_binding_may_be_discarded_by_downgrade(
    migrated_postgres_db,
):
    now = datetime.datetime.now(UTC)
    binding_digest = hashlib.sha256(b"expired-binding").digest()
    state_digest, _, _ = _insert_state(
        suffix="expired-binding",
        browser_binding_digest=binding_digest,
        created_at=now - datetime.timedelta(minutes=11),
        expires_at=now - datetime.timedelta(minutes=1),
    )

    command.downgrade(_config(), PKCE_REVISION)

    with db.engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT count(*) FROM provider_calendar_oauth_states "
                "WHERE state_digest = :digest"
            ),
            {"digest": state_digest},
        ).scalar_one() == 1
    command.upgrade(_config(), BINDING_REVISION)
    with db.engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT browser_binding_digest "
                "FROM provider_calendar_oauth_states "
                "WHERE state_digest = :digest"
            ),
            {"digest": state_digest},
        ).scalar_one() is None


def test_active_binding_refuses_downgrade_without_disclosure_or_loss(
    migrated_postgres_db,
):
    binding_digest = hashlib.sha256(b"active-binding").digest()
    state_digest, account_id, session_id = _insert_state(
        suffix="active-binding",
        browser_binding_digest=binding_digest,
    )
    original_revision = _current_revision()

    with pytest.raises(RuntimeError, match="authorization attempt is active") as raised:
        command.downgrade(_config(), PKCE_REVISION)
    message = str(raised.value)
    for private in (
        state_digest.hex(),
        binding_digest.hex(),
        account_id,
        session_id,
    ):
        assert private not in message

    assert _current_revision() == original_revision
    assert _stored_binding(state_digest) == binding_digest


def test_failed_upgrade_rolls_back_column_check_index_and_revision(
    migrated_postgres_db,
    monkeypatch,
):
    command.downgrade(_config(), PKCE_REVISION)
    original_create_index = Operations.create_index

    def fail_binding_index(self, index_name, *args, **kwargs):
        if index_name == INDEX_NAME:
            raise RuntimeError("forced browser-binding migration failure")
        return original_create_index(self, index_name, *args, **kwargs)

    monkeypatch.setattr(Operations, "create_index", fail_binding_index)
    with pytest.raises(RuntimeError, match="forced browser-binding"):
        command.upgrade(_config(), BINDING_REVISION)

    inspector = inspect(db.engine)
    assert COLUMN_NAME not in {
        column["name"] for column in inspector.get_columns(TABLE_NAME)
    }
    assert CHECK_NAME not in {
        check["name"] for check in inspector.get_check_constraints(TABLE_NAME)
    }
    assert INDEX_NAME not in {
        index["name"] for index in inspector.get_indexes(TABLE_NAME)
    }
    assert _current_revision() == PKCE_REVISION


def test_failed_downgrade_rolls_back_schema_row_and_revision(
    migrated_postgres_db,
    monkeypatch,
):
    now = datetime.datetime.now(UTC)
    binding_digest = hashlib.sha256(b"rollback-binding").digest()
    state_digest, _, _ = _insert_state(
        suffix="rollback-binding",
        browser_binding_digest=binding_digest,
        created_at=now - datetime.timedelta(minutes=11),
        expires_at=now - datetime.timedelta(minutes=1),
    )
    original_revision = _current_revision()
    original_drop_column = Operations.drop_column

    def fail_binding_column_drop(self, table_name, column_name, *args, **kwargs):
        if table_name == TABLE_NAME and column_name == COLUMN_NAME:
            raise RuntimeError("forced browser-binding downgrade failure")
        return original_drop_column(
            self,
            table_name,
            column_name,
            *args,
            **kwargs,
        )

    monkeypatch.setattr(Operations, "drop_column", fail_binding_column_drop)
    with pytest.raises(RuntimeError, match="forced browser-binding"):
        command.downgrade(_config(), PKCE_REVISION)

    inspector = inspect(db.engine)
    assert COLUMN_NAME in {
        column["name"] for column in inspector.get_columns(TABLE_NAME)
    }
    assert CHECK_NAME in {
        check["name"] for check in inspector.get_check_constraints(TABLE_NAME)
    }
    assert INDEX_NAME in {
        index["name"] for index in inspector.get_indexes(TABLE_NAME)
    }
    assert _stored_binding(state_digest) == binding_digest
    assert _current_revision() == original_revision


def test_upgrade_lock_contention_preserves_previous_schema_and_revision(
    migrated_postgres_db,
):
    command.downgrade(_config(), PKCE_REVISION)

    with db.engine.connect() as locking_connection:
        transaction = locking_connection.begin()
        try:
            locking_connection.execute(
                text(
                    "LOCK TABLE provider_calendar_oauth_states "
                    "IN ROW EXCLUSIVE MODE"
                )
            )
            with pytest.raises(OperationalError) as raised:
                command.upgrade(_config(), BINDING_REVISION)
            assert getattr(raised.value.orig, "sqlstate", None) == "55P03"
        finally:
            transaction.rollback()

    assert COLUMN_NAME not in {
        column["name"]
        for column in inspect(db.engine).get_columns(TABLE_NAME)
    }
    assert _current_revision() == PKCE_REVISION
    command.upgrade(_config(), BINDING_REVISION)


def test_downgrade_lock_contention_preserves_binding_and_revision(
    migrated_postgres_db,
):
    binding_digest = hashlib.sha256(b"locked-binding").digest()
    state_digest, _, _ = _insert_state(
        suffix="locked-binding",
        browser_binding_digest=binding_digest,
    )
    original_revision = _current_revision()

    with db.engine.connect() as locking_connection:
        transaction = locking_connection.begin()
        try:
            locking_connection.execute(
                text(
                    "LOCK TABLE provider_calendar_oauth_states "
                    "IN ACCESS SHARE MODE"
                )
            )
            with pytest.raises(OperationalError) as raised:
                command.downgrade(_config(), PKCE_REVISION)
            assert getattr(raised.value.orig, "sqlstate", None) == "55P03"
        finally:
            transaction.rollback()

    assert _stored_binding(state_digest) == binding_digest
    assert _current_revision() == original_revision


def _insert_state(
    *,
    suffix: str,
    browser_binding_digest: bytes | None = None,
    created_at: datetime.datetime | None = None,
    expires_at: datetime.datetime | None = None,
    include_binding_column: bool = True,
) -> tuple[bytes, str, str]:
    created_at = created_at or datetime.datetime.now(UTC)
    expires_at = expires_at or created_at + datetime.timedelta(minutes=10)
    state_digest = hashlib.sha256(f"state-{suffix}".encode()).digest()
    session_id = f"sess_browser_binding_{suffix}"
    token_digest = hashlib.sha256(f"session-{suffix}".encode()).digest()

    with db.engine.begin() as connection:
        account_id = connection.execute(
            text(
                "SELECT id FROM provider_accounts "
                "WHERE instructor_id = 'inst_001'"
            )
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO provider_sessions "
                "(id, account_id, token_digest, session_version, created_at, "
                "expires_at, revoked_at, revocation_reason) "
                "VALUES (:session_id, :account_id, :token_digest, 1, "
                ":created_at, :session_expires_at, NULL, NULL)"
            ),
            {
                "session_id": session_id,
                "account_id": account_id,
                "token_digest": token_digest,
                "created_at": created_at - datetime.timedelta(minutes=1),
                "session_expires_at": max(
                    expires_at,
                    created_at + datetime.timedelta(hours=1),
                ),
            },
        )
        values = {
            "state_digest": state_digest,
            "account_id": account_id,
            "session_id": session_id,
            "created_at": created_at,
            "expires_at": expires_at,
        }
        columns = (
            "state_digest, account_id, session_id, provider, created_at, "
            "expires_at"
        )
        parameters = (
            ":state_digest, :account_id, :session_id, 'google', "
            ":created_at, :expires_at"
        )
        if include_binding_column:
            columns += ", browser_binding_digest"
            parameters += ", :browser_binding_digest"
            values["browser_binding_digest"] = browser_binding_digest
        connection.execute(
            text(
                f"INSERT INTO provider_calendar_oauth_states ({columns}) "
                f"VALUES ({parameters})"
            ),
            values,
        )
    return state_digest, account_id, session_id


def _stored_binding(state_digest: bytes) -> bytes | None:
    with db.engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT browser_binding_digest "
                "FROM provider_calendar_oauth_states "
                "WHERE state_digest = :digest"
            ),
            {"digest": state_digest},
        ).scalar_one()


def _current_revision() -> str:
    with db.engine.connect() as connection:
        return connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()


def _config() -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return config
