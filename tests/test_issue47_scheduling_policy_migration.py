from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app import db
from scripts import check_scheduler_settings
from tests.db_test_utils import TEST_DATABASE_URL, reset_postgres_database


ISSUE_55_REVISION = "20261109_0010"
ISSUE_47_REVISION = "20261109_0011"


@pytest.fixture()
def database_at_issue55(monkeypatch):
    monkeypatch.setenv("SWIM_DATABASE_URL", TEST_DATABASE_URL)
    # Seed while the database matches the current ORM, then exercise the historical
    # migration boundary. Current models intentionally include columns unavailable at
    # the older revision under test.
    reset_postgres_database(seed=True)
    command.downgrade(_alembic_config(), ISSUE_55_REVISION)
    db.configure_database(TEST_DATABASE_URL)
    try:
        yield
    finally:
        db.configure_database()


def test_upgrade_trigger_and_downgrade_preserve_legacy_values(database_at_issue55):
    initial = {
        "buffers_enabled": False,
        "apply_distance_buffer_at_shift_start": True,
        "rounding_increment_mins": 10,
        "post_break_buffer_mins": 37,
        "distance_buffer_tiers": (
            '[{"id":"all","label":"All routes","min_miles":0,'
            '"max_miles":null,"buffer_mins":22}]'
        ),
        "payments_enabled_methods": "cash,wallet",
        "cancellation_window_hours": 73,
        "require_payment_method_for_new_clients": True,
    }
    _insert_legacy_settings(initial)

    command.upgrade(_alembic_config(), ISSUE_47_REVISION)

    inspector = inspect(db.engine)
    columns = {
        column["name"]: column
        for column in inspector.get_columns("scheduler_settings")
    }
    assert columns["policy_revision"]["nullable"] is False
    assert str(columns["policy_revision"]["default"]).strip("()'") == "0"
    checks = {
        item["name"] for item in inspector.get_check_constraints("scheduler_settings")
    }
    assert "ck_scheduler_settings_policy_revision_nonnegative" in checks
    assert _legacy_values() == {**initial, "policy_revision": 0}

    with db.engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE scheduler_settings
                SET buffers_enabled = true, updated_at = CURRENT_TIMESTAMP
                WHERE instructor_id = 'inst_001'
                """
            )
        )
        assert connection.execute(
            text(
                "SELECT policy_revision FROM scheduler_settings "
                "WHERE instructor_id = 'inst_001'"
            )
        ).scalar_one() == 1

        connection.execute(
            text(
                """
                UPDATE scheduler_settings
                SET payments_enabled_methods = 'card'
                WHERE instructor_id = 'inst_001'
                """
            )
        )
        assert connection.execute(
            text(
                "SELECT policy_revision FROM scheduler_settings "
                "WHERE instructor_id = 'inst_001'"
            )
        ).scalar_one() == 1

        connection.execute(
            text(
                """
                UPDATE scheduler_settings
                SET post_break_buffer_mins = 41, policy_revision = 2
                WHERE instructor_id = 'inst_001'
                """
            )
        )
        assert connection.execute(
            text(
                "SELECT policy_revision FROM scheduler_settings "
                "WHERE instructor_id = 'inst_001'"
            )
        ).scalar_one() == 2

    with pytest.raises(IntegrityError):
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE scheduler_settings
                    SET policy_revision = -1
                    WHERE instructor_id = 'inst_001'
                    """
                )
            )

    before_downgrade = _legacy_values(include_revision=False)
    command.downgrade(_alembic_config(), ISSUE_55_REVISION)
    assert "policy_revision" not in {
        column["name"] for column in inspect(db.engine).get_columns("scheduler_settings")
    }
    assert _legacy_values(include_revision=False) == before_downgrade

    command.upgrade(_alembic_config(), ISSUE_47_REVISION)
    assert _legacy_values(include_revision=False) == before_downgrade
    assert _legacy_values()["policy_revision"] == 0


@pytest.mark.parametrize(
    ("column", "updated_value"),
    [
        ("buffers_enabled", True),
        ("apply_distance_buffer_at_shift_start", False),
        ("rounding_increment_mins", 15),
        ("post_break_buffer_mins", 38),
        (
            "distance_buffer_tiers",
            '[{"id":"all","label":"All routes","min_miles":0,'
            '"max_miles":null,"buffer_mins":23}]',
        ),
    ],
    ids=[
        "buffers-enabled",
        "shift-start-buffer",
        "rounding-increment",
        "post-break-buffer",
        "distance-tiers",
    ],
)
def test_each_legacy_policy_field_update_advances_revision(
    database_at_issue55,
    column: str,
    updated_value,
):
    _insert_legacy_settings(_valid_legacy_settings())
    command.upgrade(_alembic_config(), ISSUE_47_REVISION)

    with db.engine.begin() as connection:
        revision_before = connection.execute(
            text(
                "SELECT policy_revision FROM scheduler_settings "
                "WHERE instructor_id = 'inst_001'"
            )
        ).scalar_one()
        connection.execute(
            text(
                f"UPDATE scheduler_settings SET {column} = :updated_value "
                "WHERE instructor_id = 'inst_001'"
            ),
            {"updated_value": updated_value},
        )
        revision_after = connection.execute(
            text(
                "SELECT policy_revision FROM scheduler_settings "
                "WHERE instructor_id = 'inst_001'"
            )
        ).scalar_one()

    assert revision_before == 0
    assert revision_after == 1


def test_preflight_runs_before_policy_revision_exists(database_at_issue55, capsys):
    _insert_legacy_settings(
        {
            "buffers_enabled": True,
            "apply_distance_buffer_at_shift_start": False,
            "rounding_increment_mins": 15,
            "post_break_buffer_mins": 15,
            "distance_buffer_tiers": (
                '[{"id":"all","label":"All routes","min_miles":0,'
                '"max_miles":null,"buffer_mins":20}]'
            ),
            "payments_enabled_methods": "cash",
            "cancellation_window_hours": 24,
            "require_payment_method_for_new_clients": False,
        }
    )

    assert check_scheduler_settings.main() == 0
    assert capsys.readouterr().out.strip() == "scheduling_policy_preflight_ok rows=1"


@pytest.mark.parametrize(
    ("distance_buffer_tiers", "reason_code"),
    [
        ('{"private":"preflight-payload-secret"', "invalid_json"),
        ('{"private":"preflight-payload-secret"}', "invalid_shape"),
    ],
    ids=["malformed-json", "invalid-shape"],
)
@pytest.mark.parametrize(
    "after_migration",
    [False, True],
    ids=["before-policy-revision", "after-policy-revision"],
)
def test_preflight_rejects_invalid_policy_without_payload_leakage(
    database_at_issue55,
    capsys,
    distance_buffer_tiers: str,
    reason_code: str,
    after_migration: bool,
):
    values = _valid_legacy_settings()
    values["distance_buffer_tiers"] = distance_buffer_tiers
    _insert_legacy_settings(values)
    if after_migration:
        command.upgrade(_alembic_config(), ISSUE_47_REVISION)

    assert check_scheduler_settings.main() == 1

    captured = capsys.readouterr()
    assert captured.out.strip() == (
        "scheduler_policy_invalid instructor_id=inst_001 "
        f"reason_code={reason_code}"
    )
    captured_text = captured.out + captured.err
    assert distance_buffer_tiers not in captured_text
    assert "preflight-payload-secret" not in captured_text


def _valid_legacy_settings() -> dict:
    return {
        "buffers_enabled": False,
        "apply_distance_buffer_at_shift_start": True,
        "rounding_increment_mins": 10,
        "post_break_buffer_mins": 37,
        "distance_buffer_tiers": (
            '[{"id":"all","label":"All routes","min_miles":0,'
            '"max_miles":null,"buffer_mins":22}]'
        ),
        "payments_enabled_methods": "cash,wallet",
        "cancellation_window_hours": 73,
        "require_payment_method_for_new_clients": True,
    }


def _insert_legacy_settings(values: dict) -> None:
    with db.engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO scheduler_settings (
                    instructor_id,
                    buffers_enabled,
                    apply_distance_buffer_at_shift_start,
                    rounding_increment_mins,
                    post_break_buffer_mins,
                    distance_buffer_tiers,
                    payments_enabled_methods,
                    cancellation_window_hours,
                    require_payment_method_for_new_clients
                ) VALUES (
                    'inst_001',
                    :buffers_enabled,
                    :apply_distance_buffer_at_shift_start,
                    :rounding_increment_mins,
                    :post_break_buffer_mins,
                    :distance_buffer_tiers,
                    :payments_enabled_methods,
                    :cancellation_window_hours,
                    :require_payment_method_for_new_clients
                )
                """
            ),
            values,
        )


def _legacy_values(*, include_revision: bool = True) -> dict:
    fields = [
        "buffers_enabled",
        "apply_distance_buffer_at_shift_start",
        "rounding_increment_mins",
        "post_break_buffer_mins",
        "distance_buffer_tiers",
        "payments_enabled_methods",
        "cancellation_window_hours",
        "require_payment_method_for_new_clients",
    ]
    if include_revision:
        fields.append("policy_revision")
    with db.engine.begin() as connection:
        row = connection.execute(
            text(
                f"SELECT {', '.join(fields)} FROM scheduler_settings "
                "WHERE instructor_id = 'inst_001'"
            )
        ).mappings().one()
    return dict(row)


def _alembic_config() -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return config
