import datetime

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app import db
from app.models import ClientAvailabilityWindowDB, ClientDB, InstructorDB
from tests.db_test_utils import TEST_DATABASE_URL, reset_postgres_database


PREVIOUS_REVISION = "20261110_0012"
CLIENT_PROFILE_REVISION = "20261110_0013"
CURRENT_REVISION = "20261111_0014"


@pytest.fixture(autouse=True)
def use_test_database_for_alembic(monkeypatch):
    # migrations/env.py intentionally prioritizes SWIM_DATABASE_URL over the
    # Alembic config value. Pin it here because CI exports the application DB URL.
    monkeypatch.setenv("SWIM_DATABASE_URL", TEST_DATABASE_URL)


@pytest.fixture()
def database_at_previous_revision():
    reset_postgres_database(seed=False)
    command.downgrade(_config(), PREVIOUS_REVISION)
    db.configure_database(TEST_DATABASE_URL)
    try:
        yield
    finally:
        db.configure_database()


def test_upgrade_backfills_profiles_and_installs_tenant_storage(
    database_at_previous_revision,
):
    _insert_legacy_provider_and_clients()

    command.upgrade(_config(), CLIENT_PROFILE_REVISION)

    inspector = inspect(db.engine)
    client_columns = {
        column["name"]: column for column in inspector.get_columns("clients")
    }
    assert client_columns["notes"]["nullable"] is False
    assert client_columns["location_mode"]["nullable"] is False
    assert client_columns["default_duration_mins"]["nullable"] is False
    assert client_columns["revision"]["nullable"] is False
    assert client_columns["deactivated_at"]["nullable"] is True

    client_checks = {
        constraint["name"] for constraint in inspector.get_check_constraints("clients")
    }
    assert {
        "ck_clients_name_nonblank",
        "ck_clients_status",
        "ck_clients_deactivation_state",
        "ck_clients_location_complete",
        "ck_clients_location_coordinates",
        "ck_clients_location_mode",
        "ck_clients_client_location_required",
        "ck_clients_default_duration",
        "ck_clients_revision_positive",
    }.issubset(client_checks)
    assert "uq_clients_id_instructor_id" in {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("clients")
    }
    assert "ix_clients_active_instructor_name" in {
        index["name"] for index in inspector.get_indexes("clients")
    }

    assert inspector.has_table("client_availability_windows")
    window_checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints(
            "client_availability_windows"
        )
    }
    assert window_checks == {
        "ck_client_availability_windows_day_mask",
        "ck_client_availability_windows_position",
        "ck_client_availability_windows_time_order",
    }
    tenant_fk = next(
        constraint
        for constraint in inspector.get_foreign_keys("client_availability_windows")
        if constraint["name"] == "fk_client_availability_windows_client_tenant"
    )
    assert tenant_fk["constrained_columns"] == ["client_id", "instructor_id"]
    assert tenant_fk["referred_columns"] == ["id", "instructor_id"]
    assert tenant_fk["options"]["ondelete"] == "CASCADE"

    with db.engine.connect() as connection:
        rows = {
            row.id: row
            for row in connection.execute(
                text(
                    """
                    SELECT id, notes, location_mode, default_duration_mins,
                           revision, status, deactivated_at, updated_at
                    FROM clients
                    ORDER BY id
                    """
                )
            )
        }
        assert connection.execute(
            text("SELECT count(*) FROM client_availability_windows")
        ).scalar_one() == 0

    assert rows["client_home"].location_mode == "client_location"
    assert rows["client_service_copy"].location_mode == "service_location"
    assert rows["client_service_fallback"].location_mode == "service_location"
    assert all(row.default_duration_mins == 60 for row in rows.values())
    assert all(row.notes == "" and row.revision == 1 for row in rows.values())
    assert rows["client_deactivated"].deactivated_at == rows[
        "client_deactivated"
    ].updated_at


def test_fresh_database_enforces_profile_and_availability_shape(
    migrated_postgres_db,
):
    invalid_clients = [
        ("blank-name", {"name": "   "}),
        ("blank-tab-name", {"name": "\t\n"}),
        ("bad-status", {"status": "inactive"}),
        ("bad-duration", {"default_duration_mins": 50}),
        ("bad-revision", {"revision": 0}),
        (
            "partial-location",
            {"default_location_address": "100 Partial St"},
        ),
        ("missing-client-location", {"location_mode": "client_location"}),
        (
            "bad-coordinates",
            {
                "location_mode": "client_location",
                "default_location_address": "100 Far Away",
                "default_lat": 91,
                "default_lng": -97,
            },
        ),
        (
            "blank-location",
            {
                "location_mode": "client_location",
                "default_location_address": "\t\n",
                "default_lat": 30,
                "default_lng": -97,
            },
        ),
        (
            "missing-deactivation-time",
            {"status": "deactivated"},
        ),
    ]
    for client_id, overrides in invalid_clients:
        values = {
            "id": client_id,
            "instructor_id": "inst_001",
            "name": "Valid name",
            "email": "client@example.test",
            **overrides,
        }
        with pytest.raises(IntegrityError):
            with db.SessionLocal.begin() as session:
                session.add(ClientDB(**values))

    # Contactless legacy/demo callers remain compatible in this storage-only slice.
    with db.SessionLocal.begin() as session:
        session.add(
            ClientDB(
                id="contactless-compatible",
                instructor_id="inst_001",
                name="Legacy contactless client",
            )
        )

    with db.SessionLocal() as session:
        client = session.get(ClientDB, "contactless-compatible")
        assert client.location_mode == "service_location"
        assert client.default_duration_mins == 45

    invalid_windows = [
        {"id": "bad-mask-zero", "day_mask": 0},
        {"id": "bad-mask-large", "day_mask": 128},
        {
            "id": "bad-time-order",
            "day_mask": 1,
            "start_time": datetime.time(10, 0),
            "end_time": datetime.time(9, 0),
        },
        {"id": "bad-position", "day_mask": 1, "position": -1},
    ]
    for overrides in invalid_windows:
        values = {
            "client_id": "client_alice",
            "instructor_id": "inst_001",
            "day_mask": 31,
            "start_time": datetime.time(8, 0),
            "end_time": datetime.time(10, 0),
            "position": 0,
            **overrides,
        }
        with pytest.raises(IntegrityError):
            with db.SessionLocal.begin() as session:
                session.add(ClientAvailabilityWindowDB(**values))


def test_orm_defaults_use_provider_configuration_and_windows_are_ordered(
    migrated_postgres_db,
):
    with db.SessionLocal.begin() as session:
        session.add(
            InstructorDB(
                id="inst_profile_model",
                name="Profile Provider",
                main_address="900 Service Rd",
                lat=30.2,
                lng=-97.2,
                timezone="America/Chicago",
                service_address="900 Service Rd",
                service_lat=30.2,
                service_lng=-97.2,
                default_duration_mins=60,
            )
        )
        session.add_all(
            [
                ClientDB(
                    id="client_at_service",
                    instructor_id="inst_profile_model",
                    name="At service",
                    email="service@example.test",
                    default_location_address="900 Service Rd",
                    default_lat=30.2,
                    default_lng=-97.2,
                ),
                ClientDB(
                    id="client_at_home",
                    instructor_id="inst_profile_model",
                    name="At home",
                    phone="555-0100",
                    default_location_address="901 Home Rd",
                    default_lat=30.3,
                    default_lng=-97.3,
                ),
            ]
        )
        fallback_provider = InstructorDB(
            id="inst_profile_fallback",
            name="Fallback Provider",
            main_address="902 Service Rd",
            lat=30.4,
            lng=-97.4,
            timezone="America/Chicago",
            service_address="902 Service Rd",
            service_lat=30.4,
            service_lng=-97.4,
            default_duration_mins=None,
        )
        session.add(fallback_provider)
        session.add(
            ClientDB(
                id="client_fallback_duration",
                instructor_id="inst_profile_fallback",
                instructor=fallback_provider,
                name="Fallback duration",
                email="fallback@example.test",
            )
        )

    with db.SessionLocal.begin() as session:
        session.add_all(
            [
                ClientAvailabilityWindowDB(
                    id="window_late",
                    client_id="client_at_home",
                    instructor_id="inst_profile_model",
                    day_mask=96,
                    start_time=datetime.time(10, 0),
                    end_time=datetime.time(12, 0),
                    position=2,
                ),
                ClientAvailabilityWindowDB(
                    id="window_early",
                    client_id="client_at_home",
                    instructor_id="inst_profile_model",
                    day_mask=31,
                    start_time=datetime.time(8, 0),
                    end_time=datetime.time(10, 0),
                    position=0,
                ),
            ]
        )

    with db.SessionLocal() as session:
        service_client = session.get(ClientDB, "client_at_service")
        home_client = session.get(ClientDB, "client_at_home")
        fallback_client = session.get(ClientDB, "client_fallback_duration")
        assert service_client.location_mode == "service_location"
        assert service_client.default_duration_mins == 60
        assert home_client.location_mode == "client_location"
        assert home_client.default_duration_mins == 60
        assert fallback_client.default_duration_mins == 45
        assert [window.id for window in home_client.availability_windows] == [
            "window_early",
            "window_late",
        ]

    with pytest.raises(IntegrityError):
        with db.SessionLocal.begin() as session:
            session.add(
                ClientAvailabilityWindowDB(
                    id="cross-tenant-window",
                    client_id="client_alice",
                    instructor_id="inst_profile_model",
                    day_mask=1,
                    start_time=datetime.time(8, 0),
                    end_time=datetime.time(10, 0),
                )
            )
            session.flush()


def test_representable_profile_migration_round_trips(database_at_previous_revision):
    _insert_legacy_provider_and_clients()
    command.upgrade(_config(), CLIENT_PROFILE_REVISION)
    command.downgrade(_config(), PREVIOUS_REVISION)

    inspector = inspect(db.engine)
    assert not inspector.has_table("client_availability_windows")
    assert {
        "notes",
        "location_mode",
        "default_duration_mins",
        "revision",
        "deactivated_at",
    }.isdisjoint({column["name"] for column in inspector.get_columns("clients")})
    with db.engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM clients")
        ).scalar_one() == 4
        assert connection.execute(
            text("SELECT status FROM clients WHERE id = 'client_deactivated'")
        ).scalar_one() == "deactivated"

    command.upgrade(_config(), CLIENT_PROFILE_REVISION)
    with db.engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT location_mode FROM clients WHERE id = 'client_service_copy'"
            )
        ).scalar_one() == "service_location"


def test_null_provider_duration_backfills_legacy_projection_default(
    database_at_previous_revision,
):
    _insert_legacy_provider(default_duration_mins=None)
    with db.engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO clients (id, instructor_id, name, email)
                VALUES (
                    'client_duration_fallback', 'inst_profile_migration',
                    'Duration fallback', 'fallback@example.test'
                )
                """
            )
        )

    command.upgrade(_config(), CLIENT_PROFILE_REVISION)

    with db.engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT default_duration_mins FROM clients "
                "WHERE id='client_duration_fallback'"
            )
        ).scalar_one() == 45


@pytest.mark.parametrize(
    ("client_name", "address", "lat", "lng", "error"),
    [
        ("Partial", "100 Missing Coordinates", None, None, "partial or blank location"),
        ("\t\n", None, None, None, "blank name"),
        ("Blank location", "\t\n", 30.1, -97.1, "partial or blank location"),
    ],
)
def test_upgrade_refuses_invalid_legacy_profile_without_mutation(
    database_at_previous_revision,
    client_name,
    address,
    lat,
    lng,
    error,
):
    _insert_legacy_provider()
    with db.engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO clients (
                    id, instructor_id, name, email,
                    default_location_address, default_lat, default_lng
                ) VALUES (
                    'client_partial', 'inst_profile_migration', :name,
                    'partial@example.test', :address, :lat, :lng
                )
                """
            ),
            {"name": client_name, "address": address, "lat": lat, "lng": lng},
        )

    with pytest.raises(RuntimeError, match=error):
        command.upgrade(_config(), CLIENT_PROFILE_REVISION)

    assert "notes" not in {
        column["name"] for column in inspect(db.engine).get_columns("clients")
    }
    with db.engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT name, default_location_address, default_lat, default_lng "
                "FROM clients WHERE id='client_partial'"
            )
        ).one()
        assert row == (client_name, address, lat, lng)
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == PREVIOUS_REVISION


@pytest.mark.parametrize(
    "new_state",
    [
        "availability",
        "notes",
        "duration",
        "revision",
        "deactivation_time",
        "stale_service_location",
        "client_location_at_service",
    ],
)
def test_downgrade_refuses_new_client_state_without_mutation(
    migrated_postgres_db,
    new_state,
):
    with db.engine.begin() as connection:
        if new_state == "availability":
            connection.execute(
                text(
                    """
                    INSERT INTO client_availability_windows (
                        id, client_id, instructor_id, day_mask, start_time, end_time
                    ) VALUES (
                        'window_preserved', 'client_alice', 'inst_001', 31, '08:00', '10:00'
                    )
                    """
                )
            )
        else:
            mutations = {
                "notes": "UPDATE clients SET notes='Gate code 1234' WHERE id='client_alice'",
                "duration": (
                    "UPDATE clients SET default_duration_mins=60 "
                    "WHERE id='client_alice'"
                ),
                "revision": "UPDATE clients SET revision=2 WHERE id='client_alice'",
                "deactivation_time": (
                    "UPDATE clients SET status='deactivated', "
                    "deactivated_at=updated_at + interval '1 second' "
                    "WHERE id='client_alice'"
                ),
                "stale_service_location": (
                    "UPDATE clients SET location_mode='service_location' "
                    "WHERE id='client_alice'"
                ),
                "client_location_at_service": (
                    "UPDATE clients SET location_mode='client_location', "
                    "default_location_address=(SELECT service_address FROM instructors "
                    "WHERE id='inst_001'), "
                    "default_lat=(SELECT service_lat FROM instructors WHERE id='inst_001'), "
                    "default_lng=(SELECT service_lng FROM instructors WHERE id='inst_001') "
                    "WHERE id='client_alice'"
                ),
            }
            connection.execute(text(mutations[new_state]))

    with pytest.raises(RuntimeError, match="Cannot downgrade client profile persistence"):
        command.downgrade(_config(), PREVIOUS_REVISION)

    assert inspect(db.engine).has_table("client_availability_windows")
    with db.engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == CURRENT_REVISION
        if new_state == "availability":
            assert connection.execute(
                text(
                    "SELECT count(*) FROM client_availability_windows "
                    "WHERE id='window_preserved'"
                )
            ).scalar_one() == 1
        elif new_state == "notes":
            assert connection.execute(
                text("SELECT notes FROM clients WHERE id='client_alice'")
            ).scalar_one() == "Gate code 1234"
        elif new_state == "duration":
            assert connection.execute(
                text(
                    "SELECT default_duration_mins FROM clients "
                    "WHERE id='client_alice'"
                )
            ).scalar_one() == 60
        elif new_state == "revision":
            assert connection.execute(
                text("SELECT revision FROM clients WHERE id='client_alice'")
            ).scalar_one() == 2
        elif new_state == "deactivation_time":
            row = connection.execute(
                text(
                    "SELECT status, deactivated_at = updated_at "
                    "FROM clients WHERE id='client_alice'"
                )
            ).one()
            assert row == ("deactivated", False)
        elif new_state == "stale_service_location":
            assert connection.execute(
                text("SELECT location_mode FROM clients WHERE id='client_alice'")
            ).scalar_one() == "service_location"
        else:
            assert connection.execute(
                text("SELECT location_mode FROM clients WHERE id='client_alice'")
            ).scalar_one() == "client_location"


def test_downgrade_lock_contention_fails_before_schema_mutation(
    migrated_postgres_db,
):
    blocking_connection = db.engine.connect()
    blocking_transaction = blocking_connection.begin()
    try:
        blocking_connection.execute(text("LOCK TABLE clients IN ROW EXCLUSIVE MODE"))

        with pytest.raises(OperationalError, match="could not obtain lock"):
            command.downgrade(_config(), PREVIOUS_REVISION)

        inspector = inspect(db.engine)
        assert inspector.has_table("client_availability_windows")
        assert "notes" in {
            column["name"] for column in inspector.get_columns("clients")
        }
        with db.engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == CURRENT_REVISION
    finally:
        blocking_transaction.rollback()
        blocking_connection.close()


def _insert_legacy_provider_and_clients() -> None:
    _insert_legacy_provider()
    timestamp = datetime.datetime(2026, 11, 9, 15, 30)
    with db.engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO clients (
                    id, instructor_id, name, email, phone,
                    default_location_address, default_lat, default_lng,
                    status, created_at, updated_at
                ) VALUES
                    (
                        'client_home', 'inst_profile_migration', 'Home Client',
                        'home@example.test', NULL, '901 Home Rd', 30.3, -97.3,
                        'active', :created_at, :updated_at
                    ),
                    (
                        'client_service_copy', 'inst_profile_migration', 'Service Client',
                        NULL, '555-0101', '900 Service Rd', 30.2, -97.2,
                        'active', :created_at, :updated_at
                    ),
                    (
                        'client_service_fallback', 'inst_profile_migration', 'No Location',
                        NULL, NULL, NULL, NULL, NULL,
                        'active', :created_at, :updated_at
                    ),
                    (
                        'client_deactivated', 'inst_profile_migration', 'Past Client',
                        'past@example.test', NULL, NULL, NULL, NULL,
                        'deactivated', :created_at, :updated_at
                    )
                """
            ),
            {"created_at": timestamp, "updated_at": timestamp},
        )


def _insert_legacy_provider(default_duration_mins=60) -> None:
    with db.engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO instructors (
                    id, name, main_address, lat, lng, timezone,
                    business_name, base_address, base_lat, base_lng,
                    service_address, service_lat, service_lng,
                    service_name, default_duration_mins
                ) VALUES (
                    'inst_profile_migration', 'Profile Provider',
                    '900 Service Rd', 30.2, -97.2, 'America/Chicago',
                    'Profile Provider', '900 Service Rd', 30.2, -97.2,
                    '900 Service Rd', 30.2, -97.2, 'Private service',
                    :default_duration_mins
                )
                """
            ),
            {"default_duration_mins": default_duration_mins},
        )


def _config() -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return config
