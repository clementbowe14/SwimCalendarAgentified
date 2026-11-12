from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app import db
from tests.db_test_utils import TEST_DATABASE_URL


PREVIOUS_REVISION = "20261110_0013"
CLIENT_MUTATION_REVISION = "20261111_0014"


@pytest.fixture(autouse=True)
def use_test_database_for_alembic(monkeypatch):
    monkeypatch.setenv("SWIM_DATABASE_URL", TEST_DATABASE_URL)


def test_upgrade_installs_provider_scoped_replay_storage(
    migrated_postgres_db: None,
):
    inspector = inspect(db.engine)
    columns = {
        column["name"]: column for column in inspector.get_columns("clients")
    }
    assert columns["create_idempotency_key"]["nullable"] is True
    assert columns["client_create_payload"]["nullable"] is True
    assert "ck_clients_create_replay_complete" in {
        constraint["name"]
        for constraint in inspector.get_check_constraints("clients")
    }
    replay_index = next(
        index
        for index in inspector.get_indexes("clients")
        if index["name"] == "uq_clients_instructor_create_idempotency_key"
    )
    assert replay_index["unique"] is True
    assert replay_index["column_names"] == [
        "instructor_id",
        "create_idempotency_key",
    ]
    assert replay_index["dialect_options"]["postgresql_where"]


@pytest.mark.parametrize(
    "assignment",
    [
        "create_idempotency_key='partial-replay'",
        "client_create_payload='{}'::json",
    ],
)
def test_replay_metadata_constraint_is_all_or_none(
    migrated_postgres_db: None,
    assignment: str,
):
    with pytest.raises(IntegrityError):
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    f"UPDATE clients SET {assignment} WHERE id='client_alice'"
                )
            )

    with db.engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT create_idempotency_key, client_create_payload "
                "FROM clients WHERE id='client_alice'"
            )
        ).one()
    assert row == (None, None)


def test_downgrade_drops_only_replay_metadata_and_round_trips_profile_data(
    migrated_postgres_db: None,
):
    with db.engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE clients
                SET notes='Preserve this note', revision=4,
                    create_idempotency_key='client-replay-to-drop',
                    client_create_payload='{"request": {}, "response": {}}'::json
                WHERE id='client_alice'
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO client_availability_windows (
                    id, client_id, instructor_id, day_mask,
                    start_time, end_time, position
                ) VALUES (
                    'window_replay_downgrade', 'client_alice', 'inst_001', 5,
                    '08:00', '10:00', 2
                )
                """
            )
        )
        before = connection.execute(
            text(
                """
                SELECT name, notes, revision, status,
                       default_location_address, default_lat, default_lng
                FROM clients WHERE id='client_alice'
                """
            )
        ).one()

    command.downgrade(_config(), PREVIOUS_REVISION)

    columns = {
        column["name"] for column in inspect(db.engine).get_columns("clients")
    }
    assert "create_idempotency_key" not in columns
    assert "client_create_payload" not in columns
    with db.engine.connect() as connection:
        after_downgrade = connection.execute(
            text(
                """
                SELECT name, notes, revision, status,
                       default_location_address, default_lat, default_lng
                FROM clients WHERE id='client_alice'
                """
            )
        ).one()
        assert after_downgrade == before
        assert connection.execute(
            text(
                "SELECT count(*) FROM client_availability_windows "
                "WHERE id='window_replay_downgrade'"
            )
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == PREVIOUS_REVISION

    command.upgrade(_config(), CLIENT_MUTATION_REVISION)

    with db.engine.connect() as connection:
        replay = connection.execute(
            text(
                "SELECT create_idempotency_key, client_create_payload "
                "FROM clients WHERE id='client_alice'"
            )
        ).one()
        assert replay == (None, None)
        assert connection.execute(
            text(
                "SELECT count(*) FROM client_availability_windows "
                "WHERE id='window_replay_downgrade'"
            )
        ).scalar_one() == 1


def _config() -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return config
