import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import BigInteger, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app import db
from app.models import InstructorDB, ProviderAccountDB
from tests.db_test_utils import TEST_DATABASE_URL


PREVIOUS_REVISION = "20261113_0018"
CALENDAR_REVISION = "20261114_0019"
TABLE_NAME = "provider_accounts"
COLUMN_NAME = "calendar_connection_revision"
CHECK_NAME = "ck_provider_accounts_calendar_connection_revision_nonnegative"


@pytest.fixture(autouse=True)
def use_test_database_for_alembic(monkeypatch):
    monkeypatch.setenv("SWIM_DATABASE_URL", TEST_DATABASE_URL)


def test_calendar_revision_is_the_only_alembic_head():
    script = ScriptDirectory.from_config(_config())
    assert script.get_revision(CALENDAR_REVISION).down_revision == (
        PREVIOUS_REVISION
    )
    assert script.get_heads() == [CALENDAR_REVISION]


def test_schema_and_model_define_one_nonnegative_bigint_fence(
    migrated_postgres_db,
):
    inspector = inspect(db.engine)
    columns = {
        column["name"]: column
        for column in inspector.get_columns(TABLE_NAME)
    }
    revision = columns[COLUMN_NAME]
    assert isinstance(revision["type"], BigInteger)
    assert revision["nullable"] is False
    assert revision["default"] is not None
    assert "0" in str(revision["default"])

    checks = {
        constraint["name"]: constraint
        for constraint in inspector.get_check_constraints(TABLE_NAME)
    }
    assert CHECK_NAME in checks
    assert COLUMN_NAME in checks[CHECK_NAME]["sqltext"]
    assert ">= 0" in checks[CHECK_NAME]["sqltext"]

    model_column = ProviderAccountDB.__table__.c[COLUMN_NAME]
    assert isinstance(model_column.type, BigInteger)
    assert model_column.nullable is False
    assert model_column.default.arg == 0
    assert model_column.server_default is not None
    assert CHECK_NAME in {
        constraint.name
        for constraint in ProviderAccountDB.__table__.constraints
    }


def test_upgrade_preserves_accounts_and_backfills_zero(
    migrated_postgres_db,
):
    command.downgrade(_config(), PREVIOUS_REVISION)
    before = _account_rows_without_revision()
    assert before
    assert COLUMN_NAME not in {
        column["name"]
        for column in inspect(db.engine).get_columns(TABLE_NAME)
    }

    command.upgrade(_config(), CALENDAR_REVISION)

    assert _account_rows_without_revision() == before
    with db.engine.connect() as connection:
        revisions = connection.execute(
            text(
                "SELECT calendar_connection_revision "
                "FROM provider_accounts ORDER BY id"
            )
        ).scalars().all()
    assert revisions == [0] * len(before)


def test_model_and_database_defaults_initialize_new_accounts_to_zero(
    migrated_postgres_db,
):
    with db.SessionLocal() as session:
        account = _add_account(session, suffix="model-default")
        session.commit()
        session.refresh(account)
        assert account.calendar_connection_revision == 0

    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO instructors "
                "(id, name, main_address, lat, lng, work_start_time, "
                "work_end_time, timezone) "
                "VALUES (:id, :name, :address, 30.2, -97.2, '08:00', "
                "'17:00', 'America/Chicago')"
            ),
            {
                "id": "inst_calendar_revision_database-default",
                "name": "Calendar Revision Database Default",
                "address": "1 Revision Way",
            },
        )
        connection.execute(
            text(
                "INSERT INTO provider_accounts "
                "(id, instructor_id, email_normalized, password_hash, "
                "is_demo, session_version) "
                "VALUES (:id, :instructor_id, :email, :password_hash, "
                "false, 1)"
            ),
            {
                "id": "acct_calendar_revision_database-default",
                "instructor_id": "inst_calendar_revision_database-default",
                "email": "calendar-revision-database-default@example.com",
                "password_hash": "not-used-by-schema-test",
            },
        )
        stored = connection.execute(
            text(
                "SELECT calendar_connection_revision "
                "FROM provider_accounts WHERE id = :id"
            ),
            {"id": "acct_calendar_revision_database-default"},
        ).scalar_one()
    assert stored == 0


def test_nonnegative_revision_values_round_trip(migrated_postgres_db):
    account_id = _demo_account_id()
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE provider_accounts "
                "SET calendar_connection_revision = 42 WHERE id = :id"
            ),
            {"id": account_id},
        )

    with db.SessionLocal() as session:
        account = session.get(ProviderAccountDB, account_id)
        assert account is not None
        assert account.calendar_connection_revision == 42


def test_negative_revision_is_rejected(migrated_postgres_db):
    account_id = _demo_account_id()
    with pytest.raises(IntegrityError):
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE provider_accounts "
                    "SET calendar_connection_revision = -1 WHERE id = :id"
                ),
                {"id": account_id},
            )

    with db.engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT calendar_connection_revision "
                "FROM provider_accounts WHERE id = :id"
            ),
            {"id": account_id},
        ).scalar_one() == 0


def test_zero_valued_downgrade_and_reupgrade_round_trip(
    migrated_postgres_db,
):
    before = _account_rows_without_revision()

    command.downgrade(_config(), PREVIOUS_REVISION)
    assert COLUMN_NAME not in {
        column["name"]
        for column in inspect(db.engine).get_columns(TABLE_NAME)
    }
    assert _current_revision() == PREVIOUS_REVISION
    assert _account_rows_without_revision() == before

    command.upgrade(_config(), CALENDAR_REVISION)
    assert _current_revision() == CALENDAR_REVISION
    assert _account_rows_without_revision() == before
    assert _demo_revision() == 0


def test_nonzero_downgrade_refuses_without_disclosure_or_data_loss(
    migrated_postgres_db,
):
    account_id = _demo_account_id()
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE provider_accounts "
                "SET calendar_connection_revision = 7 WHERE id = :id"
            ),
            {"id": account_id},
        )

    with pytest.raises(RuntimeError, match="calendar connection activity") as raised:
        command.downgrade(_config(), PREVIOUS_REVISION)

    assert account_id not in str(raised.value)
    assert COLUMN_NAME in {
        column["name"]
        for column in inspect(db.engine).get_columns(TABLE_NAME)
    }
    assert _current_revision() == CALENDAR_REVISION
    assert _demo_revision() == 7


def test_failed_upgrade_rolls_back_column_constraint_and_revision(
    migrated_postgres_db,
    monkeypatch,
):
    command.downgrade(_config(), PREVIOUS_REVISION)
    original_create_check = Operations.create_check_constraint

    def fail_revision_check(self, constraint_name, *args, **kwargs):
        if constraint_name == CHECK_NAME:
            raise RuntimeError("forced calendar revision migration failure")
        return original_create_check(self, constraint_name, *args, **kwargs)

    monkeypatch.setattr(
        Operations,
        "create_check_constraint",
        fail_revision_check,
    )
    with pytest.raises(RuntimeError, match="forced calendar revision"):
        command.upgrade(_config(), CALENDAR_REVISION)

    inspector = inspect(db.engine)
    assert COLUMN_NAME not in {
        column["name"] for column in inspector.get_columns(TABLE_NAME)
    }
    assert CHECK_NAME not in {
        check["name"] for check in inspector.get_check_constraints(TABLE_NAME)
    }
    assert _current_revision() == PREVIOUS_REVISION


def test_failed_downgrade_rolls_back_constraint_column_and_revision(
    migrated_postgres_db,
    monkeypatch,
):
    original_drop_column = Operations.drop_column

    def fail_revision_column_drop(self, table_name, column_name, *args, **kwargs):
        if table_name == TABLE_NAME and column_name == COLUMN_NAME:
            raise RuntimeError("forced calendar revision downgrade failure")
        return original_drop_column(
            self,
            table_name,
            column_name,
            *args,
            **kwargs,
        )

    monkeypatch.setattr(Operations, "drop_column", fail_revision_column_drop)
    with pytest.raises(RuntimeError, match="forced calendar revision"):
        command.downgrade(_config(), PREVIOUS_REVISION)

    inspector = inspect(db.engine)
    assert COLUMN_NAME in {
        column["name"] for column in inspector.get_columns(TABLE_NAME)
    }
    assert CHECK_NAME in {
        check["name"] for check in inspector.get_check_constraints(TABLE_NAME)
    }
    assert _current_revision() == CALENDAR_REVISION


def test_upgrade_lock_contention_preserves_previous_schema_and_revision(
    migrated_postgres_db,
):
    command.downgrade(_config(), PREVIOUS_REVISION)

    with db.engine.connect() as locking_connection:
        transaction = locking_connection.begin()
        try:
            locking_connection.execute(
                text("LOCK TABLE provider_accounts IN ROW EXCLUSIVE MODE")
            )
            with pytest.raises(OperationalError) as raised:
                command.upgrade(_config(), CALENDAR_REVISION)
            assert getattr(raised.value.orig, "sqlstate", None) == "55P03"
        finally:
            transaction.rollback()

    assert COLUMN_NAME not in {
        column["name"]
        for column in inspect(db.engine).get_columns(TABLE_NAME)
    }
    assert _current_revision() == PREVIOUS_REVISION


def test_downgrade_lock_contention_preserves_fence_and_revision(
    migrated_postgres_db,
):
    with db.engine.connect() as locking_connection:
        transaction = locking_connection.begin()
        try:
            locking_connection.execute(
                text("LOCK TABLE provider_accounts IN ACCESS SHARE MODE")
            )
            with pytest.raises(OperationalError) as raised:
                command.downgrade(_config(), PREVIOUS_REVISION)
            assert getattr(raised.value.orig, "sqlstate", None) == "55P03"
        finally:
            transaction.rollback()

    assert COLUMN_NAME in {
        column["name"]
        for column in inspect(db.engine).get_columns(TABLE_NAME)
    }
    assert _current_revision() == CALENDAR_REVISION
    assert _demo_revision() == 0


def _add_account(session, *, suffix: str) -> ProviderAccountDB:
    instructor = InstructorDB(
        id=f"inst_calendar_revision_{suffix}",
        name=f"Calendar Revision {suffix}",
        main_address="1 Revision Way",
        lat=30.2,
        lng=-97.2,
        work_start_time="08:00",
        work_end_time="17:00",
        timezone="America/Chicago",
    )
    account = ProviderAccountDB(
        id=f"acct_calendar_revision_{suffix}",
        instructor=instructor,
        email_normalized=f"calendar-revision-{suffix}@example.com",
        password_hash="not-used-by-schema-test",
        is_demo=False,
        session_version=1,
    )
    session.add(account)
    session.flush()
    return account


def _account_rows_without_revision() -> list[tuple]:
    with db.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, instructor_id, email_normalized, password_hash, "
                "is_demo, session_version, disabled_at, password_changed_at, "
                "created_at, updated_at FROM provider_accounts ORDER BY id"
            )
        ).all()
    return [tuple(row) for row in rows]


def _demo_account_id() -> str:
    with db.engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT id FROM provider_accounts "
                "WHERE instructor_id = 'inst_001'"
            )
        ).scalar_one()


def _demo_revision() -> int:
    with db.engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT calendar_connection_revision "
                "FROM provider_accounts WHERE instructor_id = 'inst_001'"
            )
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
