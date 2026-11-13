import datetime
import hashlib

import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import LargeBinary, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import configure_mappers

from app import db
from app.models import (
    InstructorDB,
    ProviderAccountDB,
    ProviderCalendarOAuthStateDB,
    ProviderSessionDB,
)
from tests.db_test_utils import TEST_DATABASE_URL


PREVIOUS_REVISION = "20261112_0015"
OAUTH_STATE_REVISION = "20261112_0016"
CURRENT_HEAD_REVISION = "20261112_0017"


@pytest.fixture(autouse=True)
def use_test_database_for_alembic(monkeypatch):
    monkeypatch.setenv("SWIM_DATABASE_URL", TEST_DATABASE_URL)


def test_oauth_state_revision_remains_in_linear_alembic_history():
    script = ScriptDirectory.from_config(_config())
    assert script.get_revision(OAUTH_STATE_REVISION) is not None
    assert script.get_revision(CURRENT_HEAD_REVISION).down_revision == (
        OAUTH_STATE_REVISION
    )
    assert script.get_heads() == [CURRENT_HEAD_REVISION]


def test_schema_is_digest_only_and_binds_account_to_session(
    migrated_postgres_db,
):
    inspector = inspect(db.engine)
    assert inspector.has_table("provider_calendar_oauth_states")
    columns = {
        column["name"]: column
        for column in inspector.get_columns("provider_calendar_oauth_states")
    }
    assert set(columns) == {
        "state_digest",
        "account_id",
        "session_id",
        "provider",
        "created_at",
        "expires_at",
    }
    assert isinstance(columns["state_digest"]["type"], LargeBinary)
    assert columns["provider"]["type"].length == 16
    assert columns["provider"]["default"] is not None
    assert columns["created_at"]["default"] is not None
    assert columns["created_at"]["type"].timezone is True
    assert columns["expires_at"]["type"].timezone is True
    assert all(column["nullable"] is False for column in columns.values())

    checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints(
            "provider_calendar_oauth_states"
        )
    }
    assert checks == {
        "ck_provider_calendar_oauth_states_digest_length",
        "ck_provider_calendar_oauth_states_provider",
        "ck_provider_calendar_oauth_states_lifetime",
    }
    primary_key = inspector.get_pk_constraint(
        "provider_calendar_oauth_states"
    )
    assert primary_key["name"] == "pk_provider_calendar_oauth_states"
    assert primary_key["constrained_columns"] == ["state_digest"]

    state_uniques = {
        constraint["name"]: constraint
        for constraint in inspector.get_unique_constraints(
            "provider_calendar_oauth_states"
        )
    }
    assert set(state_uniques) == {
        "uq_provider_calendar_oauth_states_account_session"
    }
    assert state_uniques[
        "uq_provider_calendar_oauth_states_account_session"
    ]["column_names"] == ["account_id", "session_id"]

    session_uniques = {
        constraint["name"]: constraint
        for constraint in inspector.get_unique_constraints("provider_sessions")
    }
    assert session_uniques["uq_provider_sessions_account_id_id"][
        "column_names"
    ] == ["account_id", "id"]

    indexes = {
        index["name"]: index
        for index in inspector.get_indexes("provider_calendar_oauth_states")
    }
    expiry_index = indexes["ix_provider_calendar_oauth_states_expires_at"]
    assert expiry_index["unique"] is False
    assert expiry_index["column_names"] == ["expires_at"]

    foreign_keys = {
        foreign_key["name"]: foreign_key
        for foreign_key in inspector.get_foreign_keys(
            "provider_calendar_oauth_states"
        )
    }
    assert set(foreign_keys) == {
        "fk_provider_calendar_oauth_states_account_session"
    }
    owner = foreign_keys[
        "fk_provider_calendar_oauth_states_account_session"
    ]
    assert owner["referred_table"] == "provider_sessions"
    assert owner["constrained_columns"] == ["account_id", "session_id"]
    assert owner["referred_columns"] == ["account_id", "id"]
    assert owner["options"]["ondelete"] == "CASCADE"

    forbidden_columns = {
        "state",
        "raw_state",
        "authorization_code",
        "access_token",
        "refresh_token",
        "pkce_verifier",
        "redirect_uri",
        "scope",
        "email",
        "instructor_id",
        "calendar_id",
        "event_id",
        "provider_response",
        "provider_error",
    }
    assert forbidden_columns.isdisjoint(columns)


def test_digest_round_trips_without_persisting_raw_state_and_relationships_resolve(
    migrated_postgres_db,
):
    raw_state = "raw-state-that-must-never-enter-postgresql"
    now = datetime.datetime.now(datetime.timezone.utc)
    with db.SessionLocal() as session:
        account = _demo_account(session)
        provider_session = _add_session(session, account, suffix="roundtrip")
        oauth_state = ProviderCalendarOAuthStateDB(
            **_state_values(
                account_id=account.id,
                session_id=provider_session.id,
                raw_state=raw_state,
                created_at=now,
                expires_at=now + datetime.timedelta(minutes=10),
            )
        )
        provider_session.calendar_oauth_state = oauth_state
        session.commit()
        session.expire_all()

        stored = session.get(
            ProviderCalendarOAuthStateDB,
            hashlib.sha256(raw_state.encode()).digest(),
        )
        assert stored is not None
        assert stored.session.id == provider_session.id
        assert stored.session.calendar_oauth_state is stored
        assert stored.created_at.tzinfo is not None
        assert stored.expires_at.tzinfo is not None

    with db.engine.connect() as connection:
        stored_values = connection.execute(
            text("SELECT * FROM provider_calendar_oauth_states")
        ).mappings().one()
    assert stored_values["state_digest"] == hashlib.sha256(
        raw_state.encode()
    ).digest()
    assert all(raw_state not in str(value) for value in stored_values.values())

    configure_mappers()
    assert ProviderSessionDB.calendar_oauth_state.property.uselist is False
    assert (
        ProviderCalendarOAuthStateDB.session.property.mapper.class_
        is ProviderSessionDB
    )


def test_database_defaults_supply_google_and_creation_time(
    migrated_postgres_db,
):
    before_insert = datetime.datetime.now(datetime.timezone.utc)
    with db.SessionLocal() as session:
        account = _demo_account(session)
        provider_session = _add_session(
            session,
            account,
            suffix="database-defaults",
        )
        account_id = account.id
        session_id = provider_session.id
        session.commit()

    digest = hashlib.sha256(b"database-default-state").digest()
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO provider_calendar_oauth_states "
                "(state_digest, account_id, session_id, expires_at) "
                "VALUES (:digest, :account_id, :session_id, "
                "CURRENT_TIMESTAMP + INTERVAL '10 minutes')"
            ),
            {
                "digest": digest,
                "account_id": account_id,
                "session_id": session_id,
            },
        )
        stored = connection.execute(
            text(
                "SELECT provider, created_at, expires_at "
                "FROM provider_calendar_oauth_states "
                "WHERE state_digest = :digest"
            ),
            {"digest": digest},
        ).mappings().one()

    assert stored["provider"] == "google"
    assert stored["created_at"] >= before_insert
    assert stored["created_at"].tzinfo is not None
    assert stored["expires_at"] - stored["created_at"] == (
        datetime.timedelta(minutes=10)
    )


def test_one_pending_state_per_session_and_digest_is_globally_unique(
    migrated_postgres_db,
):
    now = datetime.datetime.now(datetime.timezone.utc)
    with db.SessionLocal() as session:
        first_account = _demo_account(session)
        first_session = _add_session(
            session,
            first_account,
            suffix="first-pending",
        )
        second_account = _add_account(session, suffix="second-pending")
        second_session = _add_session(
            session,
            second_account,
            suffix="second-pending",
        )
        first_values = _state_values(
            account_id=first_account.id,
            session_id=first_session.id,
            raw_state="shared-pending-state",
            created_at=now,
            expires_at=now + datetime.timedelta(minutes=10),
        )
        same_session_values = _state_values(
            account_id=first_account.id,
            session_id=first_session.id,
            raw_state="different-state-same-session",
            created_at=now,
            expires_at=now + datetime.timedelta(minutes=10),
        )
        duplicate_digest_values = _state_values(
            account_id=second_account.id,
            session_id=second_session.id,
            raw_state="shared-pending-state",
            created_at=now,
            expires_at=now + datetime.timedelta(minutes=10),
        )
        session.add(ProviderCalendarOAuthStateDB(**first_values))
        session.commit()

    with pytest.raises(IntegrityError):
        with db.engine.begin() as connection:
            connection.execute(
                ProviderCalendarOAuthStateDB.__table__.insert().values(
                    **same_session_values
                )
            )

    with pytest.raises(IntegrityError):
        with db.engine.begin() as connection:
            connection.execute(
                ProviderCalendarOAuthStateDB.__table__.insert().values(
                    **duplicate_digest_values
                )
            )


@pytest.mark.parametrize(
    "overrides",
    [
        {"state_digest": b"d" * 31},
        {"state_digest": b"d" * 33},
        {"provider": "outlook"},
        {
            "created_at": datetime.datetime(
                2026,
                11,
                12,
                12,
                tzinfo=datetime.timezone.utc,
            ),
            "expires_at": datetime.datetime(
                2026,
                11,
                12,
                12,
                tzinfo=datetime.timezone.utc,
            ),
        },
        {
            "created_at": datetime.datetime(
                2026,
                11,
                12,
                12,
                tzinfo=datetime.timezone.utc,
            ),
            "expires_at": datetime.datetime(
                2026,
                11,
                12,
                11,
                59,
                tzinfo=datetime.timezone.utc,
            ),
        },
        {
            "created_at": datetime.datetime(
                2026,
                11,
                12,
                12,
                tzinfo=datetime.timezone.utc,
            ),
            "expires_at": datetime.datetime(
                2026,
                11,
                12,
                12,
                10,
                0,
                1,
                tzinfo=datetime.timezone.utc,
            ),
        },
    ],
)
def test_database_rejects_malformed_state_shapes(
    migrated_postgres_db,
    overrides,
):
    now = datetime.datetime.now(datetime.timezone.utc)
    with db.SessionLocal() as session:
        account = _demo_account(session)
        provider_session = _add_session(
            session,
            account,
            suffix="malformed",
        )
        values = _state_values(
            account_id=account.id,
            session_id=provider_session.id,
            raw_state="malformed-state",
            created_at=now,
            expires_at=now + datetime.timedelta(minutes=10),
        )
        session.commit()
    values.update(overrides)

    with pytest.raises(IntegrityError):
        with db.engine.begin() as connection:
            connection.execute(
                ProviderCalendarOAuthStateDB.__table__.insert().values(
                    **values
                )
            )


@pytest.mark.parametrize(
    ("account_selector", "session_selector"),
    [
        ("missing", "first"),
        ("first", "missing"),
        ("first", "second"),
    ],
)
def test_unknown_or_cross_account_session_ownership_is_rejected(
    migrated_postgres_db,
    account_selector,
    session_selector,
):
    now = datetime.datetime.now(datetime.timezone.utc)
    with db.SessionLocal() as session:
        first_account = _demo_account(session)
        first_session = _add_session(
            session,
            first_account,
            suffix=f"owner-first-{account_selector}-{session_selector}",
        )
        second_account = _add_account(
            session,
            suffix=f"owner-second-{account_selector}-{session_selector}",
        )
        second_session = _add_session(
            session,
            second_account,
            suffix=f"owner-second-{account_selector}-{session_selector}",
        )
        account_ids = {
            "first": first_account.id,
            "missing": "acct_missing_oauth_owner",
        }
        session_ids = {
            "first": first_session.id,
            "second": second_session.id,
            "missing": "sess_missing_oauth_owner",
        }
        values = _state_values(
            account_id=account_ids[account_selector],
            session_id=session_ids[session_selector],
            raw_state=(
                f"owner-state-{account_selector}-{session_selector}"
            ),
            created_at=now,
            expires_at=now + datetime.timedelta(minutes=10),
        )
        session.commit()

    with pytest.raises(IntegrityError):
        with db.engine.begin() as connection:
            connection.execute(
                ProviderCalendarOAuthStateDB.__table__.insert().values(
                    **values
                )
            )


def test_deletion_cascades_only_from_the_owning_session_or_account(
    migrated_postgres_db,
):
    now = datetime.datetime.now(datetime.timezone.utc)
    with db.SessionLocal() as session:
        first_account = _demo_account(session)
        first_session = _add_session(
            session,
            first_account,
            suffix="delete-first",
        )
        second_account = _add_account(session, suffix="delete-second")
        second_session = _add_session(
            session,
            second_account,
            suffix="delete-second",
        )
        first_values = _state_values(
            account_id=first_account.id,
            session_id=first_session.id,
            raw_state="delete-first-state",
            created_at=now,
            expires_at=now + datetime.timedelta(minutes=10),
        )
        second_values = _state_values(
            account_id=second_account.id,
            session_id=second_session.id,
            raw_state="delete-second-state",
            created_at=now,
            expires_at=now + datetime.timedelta(minutes=10),
        )
        first_account_id = first_account.id
        first_session_id = first_session.id
        second_account_id = second_account.id
        second_session_id = second_session.id
        session.add_all(
            [
                ProviderCalendarOAuthStateDB(**first_values),
                ProviderCalendarOAuthStateDB(**second_values),
            ]
        )
        session.commit()

    with db.engine.begin() as connection:
        connection.execute(
            ProviderCalendarOAuthStateDB.__table__.delete().where(
                ProviderCalendarOAuthStateDB.state_digest
                == first_values["state_digest"]
            )
        )
        assert _count(connection, "provider_sessions", first_session_id) == 1
        assert _count(connection, "provider_accounts", first_account_id) == 1
        assert _count_state(connection, second_values["state_digest"]) == 1

        connection.execute(
            ProviderCalendarOAuthStateDB.__table__.insert().values(
                **first_values
            )
        )
        connection.execute(
            text("DELETE FROM provider_sessions WHERE id = :id"),
            {"id": first_session_id},
        )
        assert _count_state(connection, first_values["state_digest"]) == 0
        assert _count(connection, "provider_accounts", first_account_id) == 1
        assert _count(connection, "provider_sessions", second_session_id) == 1
        assert _count_state(connection, second_values["state_digest"]) == 1

        connection.execute(
            text("DELETE FROM provider_accounts WHERE id = :id"),
            {"id": second_account_id},
        )
        assert _count(connection, "provider_sessions", second_session_id) == 0
        assert _count_state(connection, second_values["state_digest"]) == 0
        assert _count(connection, "provider_accounts", first_account_id) == 1


def test_orm_orphan_session_and_account_deletes_preserve_ownership_rules(
    migrated_postgres_db,
):
    now = datetime.datetime.now(datetime.timezone.utc)
    with db.SessionLocal() as session:
        account = _add_account(session, suffix="orm-cascades")
        first_session = _add_session(
            session,
            account,
            suffix="orm-orphan",
        )
        first_state = ProviderCalendarOAuthStateDB(
            **_state_values(
                account_id=account.id,
                session_id=first_session.id,
                raw_state="orm-orphan-state",
                created_at=now,
                expires_at=now + datetime.timedelta(minutes=10),
            )
        )
        first_digest = first_state.state_digest
        first_session.calendar_oauth_state = first_state
        account_id = account.id
        first_session_id = first_session.id
        session.commit()

        first_session.calendar_oauth_state = None
        session.commit()
        assert session.get(ProviderCalendarOAuthStateDB, first_digest) is None
        assert session.get(ProviderSessionDB, first_session_id) is not None

        second_state = ProviderCalendarOAuthStateDB(
            **_state_values(
                account_id=account_id,
                session_id=first_session_id,
                raw_state="orm-session-delete-state",
                created_at=now,
                expires_at=now + datetime.timedelta(minutes=10),
            )
        )
        second_digest = second_state.state_digest
        first_session.calendar_oauth_state = second_state
        session.commit()

        session.delete(first_session)
        session.commit()
        assert session.get(ProviderCalendarOAuthStateDB, second_digest) is None
        assert session.get(ProviderAccountDB, account_id) is not None

        account = session.get(ProviderAccountDB, account_id)
        second_session = _add_session(
            session,
            account,
            suffix="orm-account-delete",
        )
        third_state = ProviderCalendarOAuthStateDB(
            **_state_values(
                account_id=account_id,
                session_id=second_session.id,
                raw_state="orm-account-delete-state",
                created_at=now,
                expires_at=now + datetime.timedelta(minutes=10),
            )
        )
        third_digest = third_state.state_digest
        second_session_id = second_session.id
        second_session.calendar_oauth_state = third_state
        session.commit()

        session.delete(account)
        session.commit()
        assert session.get(ProviderSessionDB, second_session_id) is None
        assert session.get(ProviderCalendarOAuthStateDB, third_digest) is None


def test_session_with_pending_state_cannot_be_reparented(
    migrated_postgres_db,
):
    now = datetime.datetime.now(datetime.timezone.utc)
    with db.SessionLocal() as session:
        first_account = _demo_account(session)
        provider_session = _add_session(
            session,
            first_account,
            suffix="reparent",
        )
        second_account = _add_account(session, suffix="reparent")
        values = _state_values(
            account_id=first_account.id,
            session_id=provider_session.id,
            raw_state="reparent-state",
            created_at=now,
            expires_at=now + datetime.timedelta(minutes=10),
        )
        provider_session_id = provider_session.id
        second_account_id = second_account.id
        session.add(ProviderCalendarOAuthStateDB(**values))
        session.commit()

    with pytest.raises(IntegrityError):
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE provider_sessions SET account_id = :account_id "
                    "WHERE id = :session_id"
                ),
                {
                    "account_id": second_account_id,
                    "session_id": provider_session_id,
                },
            )


def test_empty_downgrade_and_reupgrade_round_trip(migrated_postgres_db):
    command.downgrade(_config(), PREVIOUS_REVISION)
    inspector = inspect(db.engine)
    assert not inspector.has_table("provider_calendar_oauth_states")
    assert "uq_provider_sessions_account_id_id" not in {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            "provider_sessions"
        )
    }
    with db.engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == PREVIOUS_REVISION

    command.upgrade(_config(), OAUTH_STATE_REVISION)
    assert inspect(db.engine).has_table("provider_calendar_oauth_states")


def test_failed_upgrade_after_parent_constraint_rolls_back_all_ddl(
    migrated_postgres_db,
    monkeypatch,
):
    command.downgrade(_config(), PREVIOUS_REVISION)
    original_create_table = Operations.create_table

    def fail_oauth_state_table(self, table_name, *args, **kwargs):
        if table_name == "provider_calendar_oauth_states":
            raise RuntimeError("forced migration failure")
        return original_create_table(self, table_name, *args, **kwargs)

    monkeypatch.setattr(Operations, "create_table", fail_oauth_state_table)
    with pytest.raises(RuntimeError, match="forced migration failure"):
        command.upgrade(_config(), OAUTH_STATE_REVISION)

    inspector = inspect(db.engine)
    assert not inspector.has_table("provider_calendar_oauth_states")
    assert "uq_provider_sessions_account_id_id" not in {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            "provider_sessions"
        )
    }
    with db.engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == PREVIOUS_REVISION


def test_expired_state_may_be_discarded_by_downgrade(
    migrated_postgres_db,
):
    now = datetime.datetime.now(datetime.timezone.utc)
    with db.SessionLocal() as session:
        account = _demo_account(session)
        provider_session = _add_session(
            session,
            account,
            suffix="expired-downgrade",
        )
        values = _state_values(
            account_id=account.id,
            session_id=provider_session.id,
            raw_state="expired-downgrade-state",
            created_at=now - datetime.timedelta(minutes=11),
            expires_at=now - datetime.timedelta(minutes=1),
        )
        session.add(ProviderCalendarOAuthStateDB(**values))
        session.commit()

    command.downgrade(_config(), PREVIOUS_REVISION)
    assert not inspect(db.engine).has_table("provider_calendar_oauth_states")
    command.upgrade(_config(), OAUTH_STATE_REVISION)


def test_active_state_refuses_downgrade_without_disclosing_or_losing_data(
    migrated_postgres_db,
):
    raw_state = "active-state-never-disclose"
    now = datetime.datetime.now(datetime.timezone.utc)
    with db.SessionLocal() as session:
        account = _demo_account(session)
        provider_session = _add_session(
            session,
            account,
            suffix="active-downgrade",
        )
        values = _state_values(
            account_id=account.id,
            session_id=provider_session.id,
            raw_state=raw_state,
            created_at=now,
            expires_at=now + datetime.timedelta(minutes=10),
        )
        account_id = account.id
        session_id = provider_session.id
        session.add(ProviderCalendarOAuthStateDB(**values))
        session.commit()

    with pytest.raises(RuntimeError, match="authorization attempt is active") as exc_info:
        command.downgrade(_config(), PREVIOUS_REVISION)
    message = str(exc_info.value)
    assert raw_state not in message
    assert values["state_digest"].hex() not in message
    assert account_id not in message
    assert session_id not in message

    assert inspect(db.engine).has_table("provider_calendar_oauth_states")
    with db.engine.connect() as connection:
        assert _count_state(connection, values["state_digest"]) == 1
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == CURRENT_HEAD_REVISION


@pytest.mark.parametrize(
    "locked_table",
    ["provider_sessions", "provider_calendar_oauth_states"],
)
def test_downgrade_lock_contention_preserves_schema_row_and_revision(
    migrated_postgres_db,
    locked_table,
):
    now = datetime.datetime.now(datetime.timezone.utc)
    with db.SessionLocal() as session:
        account = _demo_account(session)
        provider_session = _add_session(
            session,
            account,
            suffix=f"downgrade-lock-{locked_table}",
        )
        values = _state_values(
            account_id=account.id,
            session_id=provider_session.id,
            raw_state=f"downgrade-lock-state-{locked_table}",
            created_at=now,
            expires_at=now + datetime.timedelta(minutes=10),
        )
        session.add(ProviderCalendarOAuthStateDB(**values))
        session.commit()

    with db.engine.connect() as locking_connection:
        locking_transaction = locking_connection.begin()
        try:
            lock_statement = {
                "provider_sessions": text(
                    "LOCK TABLE provider_sessions IN ACCESS SHARE MODE"
                ),
                "provider_calendar_oauth_states": text(
                    "LOCK TABLE provider_calendar_oauth_states "
                    "IN ACCESS SHARE MODE"
                ),
            }[locked_table]
            locking_connection.execute(
                lock_statement
            )
            with pytest.raises(OperationalError) as exc_info:
                command.downgrade(_config(), PREVIOUS_REVISION)
            assert getattr(exc_info.value.orig, "sqlstate", None) == "55P03"
        finally:
            locking_transaction.rollback()

    assert inspect(db.engine).has_table("provider_calendar_oauth_states")
    with db.engine.connect() as connection:
        assert _count_state(connection, values["state_digest"]) == 1
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == CURRENT_HEAD_REVISION


def test_upgrade_lock_contention_preserves_previous_schema_and_revision(
    migrated_postgres_db,
):
    command.downgrade(_config(), PREVIOUS_REVISION)

    with db.engine.connect() as locking_connection:
        locking_transaction = locking_connection.begin()
        try:
            locking_connection.execute(
                text("LOCK TABLE provider_sessions IN ACCESS SHARE MODE")
            )
            with pytest.raises(OperationalError) as exc_info:
                command.upgrade(_config(), OAUTH_STATE_REVISION)
            assert getattr(exc_info.value.orig, "sqlstate", None) == "55P03"
        finally:
            locking_transaction.rollback()

    inspector = inspect(db.engine)
    assert not inspector.has_table("provider_calendar_oauth_states")
    assert "uq_provider_sessions_account_id_id" not in {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            "provider_sessions"
        )
    }
    with db.engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == PREVIOUS_REVISION
    command.upgrade(_config(), OAUTH_STATE_REVISION)


def _demo_account(session) -> ProviderAccountDB:
    return session.query(ProviderAccountDB).filter_by(
        instructor_id="inst_001"
    ).one()


def _add_account(session, *, suffix: str) -> ProviderAccountDB:
    instructor = InstructorDB(
        id=f"inst_oauth_{suffix}",
        name=f"OAuth Provider {suffix}",
        main_address="1 Authorization Way",
        lat=30.2,
        lng=-97.2,
        work_start_time="08:00",
        work_end_time="17:00",
        timezone="America/Chicago",
    )
    account = ProviderAccountDB(
        id=f"acct_oauth_{suffix}",
        instructor=instructor,
        email_normalized=f"oauth-{suffix}@example.com",
        password_hash="not-used-by-schema-test",
        is_demo=False,
        session_version=1,
    )
    session.add(account)
    session.flush()
    return account


def _add_session(
    session,
    account: ProviderAccountDB,
    *,
    suffix: str,
) -> ProviderSessionDB:
    now = datetime.datetime.now(datetime.timezone.utc)
    provider_session = ProviderSessionDB(
        id=f"sess_oauth_{suffix}",
        account_id=account.id,
        token_digest=hashlib.sha256(
            f"session-token-{suffix}".encode()
        ).digest(),
        session_version=account.session_version,
        created_at=now,
        expires_at=now + datetime.timedelta(hours=1),
    )
    session.add(provider_session)
    session.flush()
    return provider_session


def _state_values(
    *,
    account_id: str,
    session_id: str,
    raw_state: str,
    created_at: datetime.datetime,
    expires_at: datetime.datetime,
) -> dict:
    return {
        "state_digest": hashlib.sha256(raw_state.encode()).digest(),
        "account_id": account_id,
        "session_id": session_id,
        "provider": "google",
        "created_at": created_at,
        "expires_at": expires_at,
    }


def _count(connection, table_name: str, identifier: str) -> int:
    if table_name == "provider_sessions":
        query = text("SELECT count(*) FROM provider_sessions WHERE id = :id")
    elif table_name == "provider_accounts":
        query = text("SELECT count(*) FROM provider_accounts WHERE id = :id")
    else:
        raise AssertionError(f"Unsupported test table: {table_name}")
    return connection.execute(query, {"id": identifier}).scalar_one()


def _count_state(connection, digest: bytes) -> int:
    return connection.execute(
        text(
            "SELECT count(*) FROM provider_calendar_oauth_states "
            "WHERE state_digest = :digest"
        ),
        {"digest": digest},
    ).scalar_one()


def _config() -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return config
