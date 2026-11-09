import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import ProviderWorkingHoursDB
from tests.db_test_utils import TEST_DATABASE_URL, reset_postgres_database


ISSUE_54_REVISION = "20261109_0009"
ISSUE_55_REVISION = "20261109_0010"
NEW_INSTRUCTOR_COLUMNS = {
    "business_name",
    "base_address",
    "base_lat",
    "base_lng",
    "service_address",
    "service_lat",
    "service_lng",
    "service_name",
    "default_duration_mins",
}


@pytest.fixture()
def database_at_issue54(monkeypatch):
    monkeypatch.setenv("SWIM_DATABASE_URL", TEST_DATABASE_URL)
    reset_postgres_database(seed=False)
    command.downgrade(_alembic_config(), ISSUE_54_REVISION)
    try:
        yield
    finally:
        db.configure_database()


def test_upgrade_has_exact_schema_and_deterministic_fail_closed_backfill(
    database_at_issue54,
):
    with db.engine.begin() as connection:
        _insert_instructor(
            connection,
            instructor_id="inst_alpha",
            name="Coach Alpha",
            address="10 Alpha Way",
            lat=30.1,
            lng=-97.1,
            start="08:15",
            end="17:45",
        )
        _insert_instructor(
            connection,
            instructor_id="inst_beta",
            name="Coach Beta",
            address="20 Beta Way",
            lat=31.2,
            lng=-98.2,
            start="8:00",
            end="17:00",
        )
        _insert_instructor(
            connection,
            instructor_id="inst_gamma",
            name="Coach Gamma",
            address="30 Gamma Way",
            lat=32.3,
            lng=-99.3,
            start="18:00",
            end="08:00",
        )
        _insert_instructor(
            connection,
            instructor_id="inst_delta",
            name="Coach Delta",
            address="40 Delta Way",
            lat=33.4,
            lng=-100.4,
            start=None,
            end="17:00",
        )
        tied_created_at = datetime.datetime(2026, 10, 1, 12, 0)
        _insert_invite(
            connection,
            token="alpha-zzzz-older",
            instructor_id="inst_alpha",
            business_name="Older Despite Token",
            preferred_duration_mins=90,
            created_at=datetime.datetime(2026, 9, 30, 12, 0),
        )
        _insert_invite(
            connection,
            token="alpha-a",
            instructor_id="inst_alpha",
            business_name="Tie Loser",
            preferred_duration_mins=30,
            created_at=tied_created_at,
        )
        _insert_invite(
            connection,
            token="alpha-z",
            instructor_id="inst_alpha",
            business_name="  Tie Winner  ",
            preferred_duration_mins=45,
            created_at=tied_created_at,
        )
        _insert_invite(
            connection,
            token="beta-only",
            instructor_id="inst_beta",
            business_name="Beta Business",
            preferred_duration_mins=17,
            created_at=datetime.datetime(2026, 10, 2, 12, 0),
        )

    command.upgrade(_alembic_config(), ISSUE_55_REVISION)

    inspector = inspect(db.engine)
    instructor_columns = {
        column["name"]: column for column in inspector.get_columns("instructors")
    }
    assert NEW_INSTRUCTOR_COLUMNS.issubset(instructor_columns)
    assert instructor_columns["business_name"]["type"].length == 160
    assert instructor_columns["base_address"]["type"].length == 300
    assert instructor_columns["service_address"]["type"].length == 300
    assert instructor_columns["service_name"]["type"].length == 160
    assert all(instructor_columns[name]["nullable"] for name in NEW_INSTRUCTOR_COLUMNS)

    instructor_checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("instructors")
    }
    assert instructor_checks == {
        "ck_instructors_base_coordinates",
        "ck_instructors_base_location_complete",
        "ck_instructors_default_duration",
        "ck_instructors_service_coordinates",
        "ck_instructors_service_location_complete",
    }

    assert inspector.has_table("provider_working_hours")
    hours_columns = {
        column["name"]: column
        for column in inspector.get_columns("provider_working_hours")
    }
    assert set(hours_columns) == {
        "instructor_id",
        "weekday",
        "enabled",
        "start_time",
        "end_time",
        "created_at",
        "updated_at",
    }
    assert isinstance(hours_columns["start_time"]["type"], sa.Time)
    assert isinstance(hours_columns["end_time"]["type"], sa.Time)
    assert hours_columns["start_time"]["nullable"] is True
    assert hours_columns["end_time"]["nullable"] is True
    assert hours_columns["enabled"]["nullable"] is False
    assert hours_columns["created_at"]["type"].timezone is True
    assert hours_columns["updated_at"]["type"].timezone is True
    assert _normalized_default(hours_columns["enabled"]["default"]) == "false"
    assert (
        _normalized_default(hours_columns["created_at"]["default"])
        == "current_timestamp"
    )
    assert (
        _normalized_default(hours_columns["updated_at"]["default"])
        == "current_timestamp"
    )
    hours_pk = inspector.get_pk_constraint("provider_working_hours")
    assert hours_pk["constrained_columns"] == ["instructor_id", "weekday"]
    assert hours_pk["name"] == "pk_provider_working_hours"
    hours_checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("provider_working_hours")
    }
    assert hours_checks == {
        "ck_provider_working_hours_state",
        "ck_provider_working_hours_weekday",
    }
    hours_fks = {
        foreign_key["name"]: foreign_key
        for foreign_key in inspector.get_foreign_keys("provider_working_hours")
    }
    hours_fk = hours_fks["fk_provider_working_hours_instructor_id"]
    assert hours_fk["constrained_columns"] == ["instructor_id"]
    assert hours_fk["referred_table"] == "instructors"
    assert hours_fk["referred_columns"] == ["id"]
    assert hours_fk["options"] == {"ondelete": "CASCADE"}
    with db.engine.connect() as connection:
        indexes = {
            row.indexname: row.indexdef
            for row in connection.execute(
                text(
                    """
                    SELECT indexname, indexdef
                    FROM pg_indexes
                    WHERE schemaname = current_schema()
                      AND tablename = 'provider_working_hours'
                    """
                )
            )
        }
    assert set(indexes) == {"pk_provider_working_hours"}
    assert "UNIQUE INDEX" in indexes["pk_provider_working_hours"]
    assert "(instructor_id, weekday)" in indexes["pk_provider_working_hours"]

    with db.engine.connect() as connection:
        providers = {
            row.id: row
            for row in connection.execute(
                text(
                    """
                    SELECT
                        id,
                        business_name,
                        base_address,
                        base_lat,
                        base_lng,
                        service_address,
                        service_lat,
                        service_lng,
                        service_name,
                        default_duration_mins
                    FROM instructors
                    ORDER BY id
                    """
                )
            )
        }
        alpha_hours = connection.execute(
            text(
                """
                SELECT weekday, enabled, start_time, end_time
                FROM provider_working_hours
                WHERE instructor_id = 'inst_alpha'
                ORDER BY weekday
                """
            )
        ).all()
        incomplete_hour_counts = dict(
            connection.execute(
                text(
                    """
                    SELECT instructors.id, count(hours.weekday)
                    FROM instructors
                    LEFT JOIN provider_working_hours AS hours
                        ON hours.instructor_id = instructors.id
                    WHERE instructors.id IN ('inst_beta', 'inst_delta', 'inst_gamma')
                    GROUP BY instructors.id
                    ORDER BY instructors.id
                    """
                )
            ).all()
        )

    alpha = providers["inst_alpha"]
    assert alpha.business_name == "Tie Winner"
    assert alpha.default_duration_mins == 45
    assert alpha.service_name == "Swim lesson"
    assert (
        alpha.base_address,
        alpha.base_lat,
        alpha.base_lng,
        alpha.service_address,
        alpha.service_lat,
        alpha.service_lng,
    ) == (
        "10 Alpha Way",
        30.1,
        -97.1,
        "10 Alpha Way",
        30.1,
        -97.1,
    )
    assert [row.weekday for row in alpha_hours] == list(range(7))
    assert all(row.enabled is True for row in alpha_hours)
    assert {row.start_time for row in alpha_hours} == {datetime.time(8, 15)}
    assert {row.end_time for row in alpha_hours} == {datetime.time(17, 45)}

    beta = providers["inst_beta"]
    assert beta.business_name == "Beta Business"
    assert beta.default_duration_mins == 60
    assert beta.base_address == beta.service_address == "20 Beta Way"
    gamma = providers["inst_gamma"]
    assert gamma.business_name is None
    assert gamma.default_duration_mins == 60
    assert incomplete_hour_counts == {
        "inst_beta": 0,
        "inst_delta": 0,
        "inst_gamma": 0,
    }


def test_upgrade_uses_newest_nonblank_business_name_independently_from_duration(
    database_at_issue54,
):
    with db.engine.begin() as connection:
        _insert_instructor(
            connection,
            instructor_id="inst_blank_latest",
            name="Coach Blank Latest",
            address="60 Backfill Way",
            lat=30.6,
            lng=-97.6,
            start="08:00",
            end="17:00",
        )
        _insert_invite(
            connection,
            token="blank-latest-oldest",
            instructor_id="inst_blank_latest",
            business_name="Old Business",
            preferred_duration_mins=30,
            created_at=datetime.datetime(2026, 10, 1, 12, 0),
        )
        _insert_invite(
            connection,
            token="blank-latest-named",
            instructor_id="inst_blank_latest",
            business_name="  Newest Named Business  ",
            preferred_duration_mins=45,
            created_at=datetime.datetime(2026, 10, 2, 12, 0),
        )
        _insert_invite(
            connection,
            token="blank-latest-newest",
            instructor_id="inst_blank_latest",
            business_name="   ",
            preferred_duration_mins=90,
            created_at=datetime.datetime(2026, 10, 3, 12, 0),
        )

    command.upgrade(_alembic_config(), ISSUE_55_REVISION)

    with db.engine.connect() as connection:
        provider = connection.execute(
            text(
                """
                SELECT business_name, default_duration_mins
                FROM instructors
                WHERE id = 'inst_blank_latest'
                """
            )
        ).one()

    assert provider.business_name == "Newest Named Business"
    assert provider.default_duration_mins == 90


def test_upgrade_preflight_refuses_oversized_legacy_values_before_schema_mutation(
    database_at_issue54,
):
    private_address = "PRIVATE_ADDRESS_VALUE_" + ("a" * 300)
    private_business_name = "PRIVATE_BUSINESS_VALUE_" + ("b" * 160)
    with db.engine.begin() as connection:
        _insert_instructor(
            connection,
            instructor_id="inst_z_address",
            name="Coach Address",
            address=private_address,
            lat=30.7,
            lng=-97.7,
            start="08:00",
            end="17:00",
        )
        _insert_instructor(
            connection,
            instructor_id="inst_a_business",
            name="Coach Business",
            address="70 Valid Way",
            lat=30.8,
            lng=-97.8,
            start="08:00",
            end="17:00",
        )
        _insert_invite(
            connection,
            token="oversized-business",
            instructor_id="inst_a_business",
            business_name=private_business_name,
            preferred_duration_mins=60,
            created_at=datetime.datetime(2026, 10, 4, 12, 0),
        )

    with pytest.raises(RuntimeError) as error:
        command.upgrade(_alembic_config(), ISSUE_55_REVISION)

    message = str(error.value)
    business_conflict = (
        "provider_id=inst_a_business,"
        "reason_code=LEGACY_BUSINESS_NAME_TOO_LONG"
    )
    address_conflict = (
        "provider_id=inst_z_address,"
        "reason_code=LEGACY_MAIN_ADDRESS_TOO_LONG"
    )
    assert business_conflict in message
    assert address_conflict in message
    assert message.index(business_conflict) < message.index(address_conflict)
    assert "before schema mutation" in message
    assert "no legacy values were truncated" in message
    assert private_address not in message
    assert private_business_name not in message

    inspector = inspect(db.engine)
    assert not inspector.has_table("provider_working_hours")
    assert NEW_INSTRUCTOR_COLUMNS.isdisjoint(
        {column["name"] for column in inspector.get_columns("instructors")}
    )
    with db.engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == ISSUE_54_REVISION
        assert connection.execute(
            text(
                """
                SELECT main_address
                FROM instructors
                WHERE id = 'inst_z_address'
                """
            )
        ).scalar_one() == private_address
        assert connection.execute(
            text(
                """
                SELECT business_name
                FROM invite_tokens
                WHERE token = 'oversized-business'
                """
            )
        ).scalar_one() == private_business_name


def test_upgrade_preflight_accepts_boundary_length_legacy_values(
    database_at_issue54,
):
    boundary_address = "a" * 300
    boundary_business_name = "b" * 160
    with db.engine.begin() as connection:
        _insert_instructor(
            connection,
            instructor_id="inst_boundary",
            name="Coach Boundary",
            address=boundary_address,
            lat=30.9,
            lng=-97.9,
            start="08:00",
            end="17:00",
        )
        _insert_invite(
            connection,
            token="boundary-business",
            instructor_id="inst_boundary",
            business_name=f"  {boundary_business_name}  ",
            preferred_duration_mins=60,
            created_at=datetime.datetime(2026, 10, 5, 12, 0),
        )

    command.upgrade(_alembic_config(), ISSUE_55_REVISION)

    with db.engine.connect() as connection:
        migrated_provider = connection.execute(
            text(
                """
                SELECT business_name, base_address, service_address
                FROM instructors
                WHERE id = 'inst_boundary'
                """
            )
        ).one()

    assert migrated_provider == (
        boundary_business_name,
        boundary_address,
        boundary_address,
    )


def test_working_hours_orm_defaults_match_migrated_schema(database_at_issue54):
    command.upgrade(_alembic_config(), ISSUE_55_REVISION)

    migrated_columns = {
        column["name"]: column
        for column in inspect(db.engine).get_columns("provider_working_hours")
    }
    orm_columns = ProviderWorkingHoursDB.__table__.c

    for column_name in ("enabled", "created_at", "updated_at"):
        assert orm_columns[column_name].server_default is not None
        assert _normalized_default(
            orm_columns[column_name].server_default.arg
        ) == _normalized_default(migrated_columns[column_name]["default"])


def test_constraints_and_cascade_are_enforced(database_at_issue54):
    with db.engine.begin() as connection:
        _insert_instructor(
            connection,
            instructor_id="inst_constraints",
            name="Coach Constraints",
            address="40 Constraint Way",
            lat=33.4,
            lng=-100.4,
            start="09:00",
            end="15:00",
        )
        _insert_invite(
            connection,
            token="constraints-invite",
            instructor_id="inst_constraints",
            business_name="Constraint Business",
            preferred_duration_mins=60,
            created_at=datetime.datetime(2026, 10, 3, 12, 0),
        )
    command.upgrade(_alembic_config(), ISSUE_55_REVISION)

    with pytest.raises(IntegrityError, match="ck_instructors_base_location_complete"):
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE instructors
                    SET base_address = NULL
                    WHERE id = 'inst_constraints'
                    """
                )
            )
    with pytest.raises(IntegrityError, match="ck_instructors_default_duration"):
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE instructors
                    SET default_duration_mins = 20
                    WHERE id = 'inst_constraints'
                    """
                )
            )
    with pytest.raises(IntegrityError, match="ck_instructors_base_coordinates"):
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE instructors
                    SET base_lat = 91
                    WHERE id = 'inst_constraints'
                    """
                )
            )
    with pytest.raises(IntegrityError, match="ck_provider_working_hours_state"):
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE provider_working_hours
                    SET enabled = false
                    WHERE instructor_id = 'inst_constraints' AND weekday = 0
                    """
                )
            )
    with pytest.raises(IntegrityError, match="ck_provider_working_hours_weekday"):
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO provider_working_hours (
                        instructor_id, weekday, enabled, start_time, end_time
                    ) VALUES (
                        'inst_constraints', 7, false, NULL, NULL
                    )
                    """
                )
            )

    with db.engine.begin() as connection:
        connection.execute(
            text("DELETE FROM invite_tokens WHERE instructor_id = 'inst_constraints'")
        )
        connection.execute(
            text("DELETE FROM instructors WHERE id = 'inst_constraints'")
        )
        remaining_hours = connection.execute(
            text(
                """
                SELECT count(*)
                FROM provider_working_hours
                WHERE instructor_id = 'inst_constraints'
                """
            )
        ).scalar_one()
    assert remaining_hours == 0


def test_representable_downgrade_copies_compatibility_values_and_round_trips(
    database_at_issue54,
):
    _insert_representable_legacy_provider()
    command.upgrade(_alembic_config(), ISSUE_55_REVISION)

    with db.engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE instructors
                SET
                    business_name = 'Updated Business',
                    base_address = '99 Updated Way',
                    base_lat = 35.5,
                    base_lng = -101.5,
                    service_address = '99 Updated Way',
                    service_lat = 35.5,
                    service_lng = -101.5,
                    default_duration_mins = 45
                WHERE id = 'inst_roundtrip'
                """
            )
        )
        connection.execute(
            text(
                """
                UPDATE provider_working_hours
                SET start_time = '07:30', end_time = '16:15'
                WHERE instructor_id = 'inst_roundtrip'
                """
            )
        )

    command.downgrade(_alembic_config(), ISSUE_54_REVISION)

    inspector = inspect(db.engine)
    assert not inspector.has_table("provider_working_hours")
    assert NEW_INSTRUCTOR_COLUMNS.isdisjoint(
        {column["name"] for column in inspector.get_columns("instructors")}
    )
    with db.engine.connect() as connection:
        legacy_provider = connection.execute(
            text(
                """
                SELECT
                    main_address,
                    lat,
                    lng,
                    work_start_time,
                    work_end_time
                FROM instructors
                WHERE id = 'inst_roundtrip'
                """
            )
        ).one()
        invite_values = connection.execute(
            text(
                """
                SELECT token, business_name, preferred_duration_mins
                FROM invite_tokens
                WHERE instructor_id = 'inst_roundtrip'
                ORDER BY token
                """
            )
        ).all()
    assert legacy_provider == (
        "99 Updated Way",
        35.5,
        -101.5,
        "07:30",
        "16:15",
    )
    assert invite_values == [
        ("roundtrip-a", "Updated Business", 45),
        ("roundtrip-b", "Updated Business", 45),
    ]

    command.upgrade(_alembic_config(), ISSUE_55_REVISION)
    with db.engine.connect() as connection:
        round_tripped = connection.execute(
            text(
                """
                SELECT
                    business_name,
                    base_address,
                    service_address,
                    default_duration_mins
                FROM instructors
                WHERE id = 'inst_roundtrip'
                """
            )
        ).one()
        intervals = connection.execute(
            text(
                """
                SELECT weekday, start_time, end_time
                FROM provider_working_hours
                WHERE instructor_id = 'inst_roundtrip'
                ORDER BY weekday
                """
            )
        ).all()
    assert round_tripped == (
        "Updated Business",
        "99 Updated Way",
        "99 Updated Way",
        45,
    )
    assert [row.weekday for row in intervals] == list(range(7))
    assert {row.start_time for row in intervals} == {datetime.time(7, 30)}
    assert {row.end_time for row in intervals} == {datetime.time(16, 15)}


@pytest.mark.parametrize(
    ("conflict", "error_match"),
    [
        ("location", "base and service locations"),
        ("hours", "all seven weekdays"),
        ("invite_snapshot", "existing invite snapshots"),
    ],
)
def test_downgrade_refuses_unrepresentable_state_before_any_mutation(
    database_at_issue54,
    conflict,
    error_match,
):
    _insert_representable_legacy_provider()
    command.upgrade(_alembic_config(), ISSUE_55_REVISION)

    with db.engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE instructors
                SET
                    base_address = '88 New Way',
                    base_lat = 34.8,
                    base_lng = -100.8,
                    service_address = '88 New Way',
                    service_lat = 34.8,
                    service_lng = -100.8,
                    business_name = 'Uncopied Business',
                    default_duration_mins = 45
                WHERE id = 'inst_roundtrip'
                """
            )
        )
        if conflict == "location":
            connection.execute(
                text(
                    """
                    UPDATE instructors
                    SET service_address = '89 Different Way'
                    WHERE id = 'inst_roundtrip'
                    """
                )
            )
        elif conflict == "hours":
            connection.execute(
                text(
                    """
                    UPDATE provider_working_hours
                    SET enabled = false, start_time = NULL, end_time = NULL
                    WHERE instructor_id = 'inst_roundtrip' AND weekday = 6
                    """
                )
            )
        else:
            connection.execute(
                text(
                    """
                    UPDATE instructors
                    SET default_duration_mins = NULL
                    WHERE id = 'inst_roundtrip'
                    """
                )
            )
        before_provider = connection.execute(
            text(
                """
                SELECT
                    main_address,
                    lat,
                    lng,
                    work_start_time,
                    work_end_time,
                    business_name,
                    base_address,
                    service_address,
                    default_duration_mins
                FROM instructors
                WHERE id = 'inst_roundtrip'
                """
            )
        ).one()
        before_invites = connection.execute(
            text(
                """
                SELECT token, business_name, preferred_duration_mins
                FROM invite_tokens
                WHERE instructor_id = 'inst_roundtrip'
                ORDER BY token
                """
            )
        ).all()
        before_hours = connection.execute(
            text(
                """
                SELECT weekday, enabled, start_time, end_time
                FROM provider_working_hours
                WHERE instructor_id = 'inst_roundtrip'
                ORDER BY weekday
                """
            )
        ).all()

    with pytest.raises(RuntimeError, match=error_match):
        command.downgrade(_alembic_config(), ISSUE_54_REVISION)

    inspector = inspect(db.engine)
    assert inspector.has_table("provider_working_hours")
    assert NEW_INSTRUCTOR_COLUMNS.issubset(
        {column["name"] for column in inspector.get_columns("instructors")}
    )
    with db.engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == ISSUE_55_REVISION
        after_provider = connection.execute(
            text(
                """
                SELECT
                    main_address,
                    lat,
                    lng,
                    work_start_time,
                    work_end_time,
                    business_name,
                    base_address,
                    service_address,
                    default_duration_mins
                FROM instructors
                WHERE id = 'inst_roundtrip'
                """
            )
        ).one()
        after_invites = connection.execute(
            text(
                """
                SELECT token, business_name, preferred_duration_mins
                FROM invite_tokens
                WHERE instructor_id = 'inst_roundtrip'
                ORDER BY token
                """
            )
        ).all()
        after_hours = connection.execute(
            text(
                """
                SELECT weekday, enabled, start_time, end_time
                FROM provider_working_hours
                WHERE instructor_id = 'inst_roundtrip'
                ORDER BY weekday
                """
            )
        ).all()

    assert after_provider == before_provider
    assert after_invites == before_invites
    assert after_hours == before_hours
    assert after_provider.main_address == "50 Legacy Way"
    assert after_provider.work_start_time == "08:00"
    assert after_invites == [
        ("roundtrip-a", "Original A", 30),
        ("roundtrip-b", "Original B", 60),
    ]


def _insert_representable_legacy_provider() -> None:
    with db.engine.begin() as connection:
        _insert_instructor(
            connection,
            instructor_id="inst_roundtrip",
            name="Coach Roundtrip",
            address="50 Legacy Way",
            lat=34.5,
            lng=-100.5,
            start="08:00",
            end="17:00",
        )
        _insert_invite(
            connection,
            token="roundtrip-a",
            instructor_id="inst_roundtrip",
            business_name="Original A",
            preferred_duration_mins=30,
            created_at=datetime.datetime(2026, 10, 1, 12, 0),
        )
        _insert_invite(
            connection,
            token="roundtrip-b",
            instructor_id="inst_roundtrip",
            business_name="Original B",
            preferred_duration_mins=60,
            created_at=datetime.datetime(2026, 10, 2, 12, 0),
        )


def _normalized_default(value) -> str:
    return str(value).strip().lower().replace("::boolean", "")


def _insert_instructor(
    connection,
    *,
    instructor_id: str,
    name: str,
    address: str,
    lat: float,
    lng: float,
    start: str | None,
    end: str | None,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO instructors (
                id,
                name,
                main_address,
                lat,
                lng,
                work_start_time,
                work_end_time,
                timezone
            ) VALUES (
                :id,
                :name,
                :address,
                :lat,
                :lng,
                :start,
                :end,
                'America/Chicago'
            )
            """
        ),
        {
            "id": instructor_id,
            "name": name,
            "address": address,
            "lat": lat,
            "lng": lng,
            "start": start,
            "end": end,
        },
    )


def _insert_invite(
    connection,
    *,
    token: str,
    instructor_id: str,
    business_name: str,
    preferred_duration_mins: int,
    created_at: datetime.datetime,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO invite_tokens (
                token,
                instructor_id,
                business_name,
                location_label,
                preferred_duration_mins,
                allowed_durations_mins,
                expires_at,
                created_at
            ) VALUES (
                :token,
                :instructor_id,
                :business_name,
                'Migration pool',
                :preferred_duration_mins,
                '30,45,60',
                '2027-01-01 00:00:00',
                :created_at
            )
            """
        ),
        {
            "token": token,
            "instructor_id": instructor_id,
            "business_name": business_name,
            "preferred_duration_mins": preferred_duration_mins,
            "created_at": created_at,
        },
    )


def _alembic_config() -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return config
