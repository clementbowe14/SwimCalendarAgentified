import datetime

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text

from app import db
from tests.db_test_utils import TEST_DATABASE_URL, reset_postgres_database


PREVIOUS_REVISION = "20261109_0011"
BLOCKER_REVISION = "20261110_0012"


@pytest.fixture()
def database_at_previous_revision(monkeypatch):
    monkeypatch.setenv("SWIM_DATABASE_URL", TEST_DATABASE_URL)
    reset_postgres_database(seed=False)
    command.downgrade(_config(), PREVIOUS_REVISION)
    db.configure_database(TEST_DATABASE_URL)
    try:
        yield
    finally:
        db.configure_database()


def test_blocker_migration_backfills_legacy_break_and_round_trips(
    database_at_previous_revision,
):
    with db.engine.begin() as connection:
        _insert_instructor(connection)
        connection.execute(
            text(
                """
                INSERT INTO appointments (
                    id, instructor_id, client_id, title, start_time, end_time,
                    location_address, lat, lng, is_break, event_type, status
                ) VALUES (
                    'legacy_break', 'inst_migration', 'instructor_break', 'Lunch',
                    :start_time, :end_time, '100 Main St', 30.1, -97.1,
                    TRUE, 'block', 'scheduled'
                ), (
                    'legacy_lesson', 'inst_migration', 'client_1', 'Lesson',
                    :lesson_start, :lesson_end, '200 Pool St', 30.2, -97.2,
                    FALSE, 'lesson', 'scheduled'
                )
                """
            ),
            {
                "start_time": datetime.datetime(2026, 11, 10, 12, 0),
                "end_time": datetime.datetime(2026, 11, 10, 12, 30),
                "lesson_start": datetime.datetime(2026, 11, 10, 14, 0),
                "lesson_end": datetime.datetime(2026, 11, 10, 15, 0),
            },
        )

    command.upgrade(_config(), BLOCKER_REVISION)
    columns = {column["name"]: column for column in inspect(db.engine).get_columns("appointments")}
    assert columns["client_id"]["nullable"] is True
    assert columns["location_address"]["nullable"] is True
    assert columns["blocker_create_payload"]["nullable"] is True
    with db.engine.connect() as connection:
        rows = {
            row.id: row
            for row in connection.execute(
                text(
                    """
                    SELECT id, client_id, blocker_kind, revision, is_break, location_address
                    FROM appointments
                    ORDER BY id
                    """
                )
            )
        }
    assert rows["legacy_break"].client_id is None
    assert rows["legacy_break"].blocker_kind == "break"
    assert rows["legacy_break"].revision == 1
    assert rows["legacy_break"].is_break is True
    assert rows["legacy_break"].location_address == "100 Main St"
    assert rows["legacy_lesson"].blocker_kind is None
    assert rows["legacy_lesson"].client_id == "client_1"

    command.downgrade(_config(), PREVIOUS_REVISION)
    columns = {column["name"]: column for column in inspect(db.engine).get_columns("appointments")}
    assert "blocker_kind" not in columns
    assert "blocker_create_payload" not in columns
    assert "revision" not in columns
    assert columns["client_id"]["nullable"] is False
    with db.engine.connect() as connection:
        break_row = connection.execute(
            text("SELECT client_id, is_break, location_address FROM appointments WHERE id='legacy_break'")
        ).one()
    assert break_row.client_id == "instructor_break"
    assert break_row.is_break is True
    assert break_row.location_address == "100 Main St"


def test_blocker_migration_refuses_lossy_downgrade_without_mutation(
    database_at_previous_revision,
):
    with db.engine.begin() as connection:
        _insert_instructor(connection)
    command.upgrade(_config(), BLOCKER_REVISION)
    with db.engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO appointments (
                    id, instructor_id, client_id, title, start_time, end_time,
                    location_address, lat, lng, is_break, event_type,
                    blocker_kind, revision, status
                ) VALUES (
                    'new_unavailable', 'inst_migration', NULL, 'Unavailable',
                    :start_time, :end_time, NULL, NULL, NULL, FALSE, 'block',
                    'unavailable', 1, 'scheduled'
                )
                """
            ),
            {
                "start_time": datetime.datetime(2026, 11, 10, 9, 0),
                "end_time": datetime.datetime(2026, 11, 10, 10, 0),
            },
        )

    with pytest.raises(RuntimeError, match="Cannot downgrade provider blockers"):
        command.downgrade(_config(), PREVIOUS_REVISION)

    assert "blocker_kind" in {
        column["name"] for column in inspect(db.engine).get_columns("appointments")
    }
    with db.engine.connect() as connection:
        assert connection.execute(
            text("SELECT blocker_kind FROM appointments WHERE id='new_unavailable'")
        ).scalar_one() == "unavailable"


def test_blocker_shape_preserves_cancelled_clientless_history(
    migrated_postgres_db,
):
    with db.engine.begin() as connection:
        source = connection.execute(
            text(
                """
                SELECT instructor_id, start_time, end_time, location_address, lat, lng
                FROM appointments
                WHERE event_type = 'lesson'
                LIMIT 1
                """
            )
        ).one()
        connection.execute(
            text(
                """
                INSERT INTO appointments (
                    id, instructor_id, client_id, title, start_time, end_time,
                    location_address, lat, lng, is_break, event_type, status,
                    created_at, updated_at
                ) VALUES (
                    'cancelled-clientless-history', :instructor_id, NULL,
                    'Removed client', :start_time, :end_time, :location_address,
                    :lat, :lng, FALSE, 'lesson', 'cancelled', now(), now()
                )
                """
            ),
            source._mapping,
        )

        assert connection.execute(
            text(
                "SELECT client_id FROM appointments "
                "WHERE id = 'cancelled-clientless-history'"
            )
        ).scalar_one_or_none() is None


def test_blocker_migration_refuses_clientless_history_downgrade_without_mutation(
    database_at_previous_revision,
):
    with db.engine.begin() as connection:
        _insert_instructor(connection)
    command.upgrade(_config(), BLOCKER_REVISION)
    with db.engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO appointments (
                    id, instructor_id, client_id, title, start_time, end_time,
                    location_address, lat, lng, is_break, event_type, status
                ) VALUES (
                    'clientless_history', 'inst_migration', NULL, 'Removed client',
                    :start_time, :end_time, '200 Pool St', 30.2, -97.2,
                    FALSE, 'lesson', 'cancelled'
                )
                """
            ),
            {
                "start_time": datetime.datetime(2026, 11, 10, 15, 0),
                "end_time": datetime.datetime(2026, 11, 10, 16, 0),
            },
        )

    with pytest.raises(RuntimeError, match="clientless historical appointments"):
        command.downgrade(_config(), PREVIOUS_REVISION)

    assert "blocker_kind" in {
        column["name"] for column in inspect(db.engine).get_columns("appointments")
    }
    with db.engine.connect() as connection:
        assert connection.execute(
            text("SELECT status FROM appointments WHERE id='clientless_history'")
        ).scalar_one() == "cancelled"


def _insert_instructor(connection) -> None:
    connection.execute(
        text(
            """
            INSERT INTO instructors (
                id, name, main_address, lat, lng, timezone,
                business_name, base_address, base_lat, base_lng,
                service_address, service_lat, service_lng,
                service_name, default_duration_mins
            ) VALUES (
                'inst_migration', 'Migration Coach', '100 Main St', 30.1, -97.1,
                'America/Chicago', 'Migration Coach', '100 Main St', 30.1, -97.1,
                '100 Main St', 30.1, -97.1, 'Lesson', 60
            )
            """
        )
    )


def _config() -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return config
