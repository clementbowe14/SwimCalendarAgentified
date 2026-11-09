import datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app import db
from app.models import InstructorDB, InviteTokenDB
from tests.db_test_utils import TEST_DATABASE_URL


def test_auth_migration_has_exact_tables_constraints_and_indexes(migrated_postgres_db):
    inspector = inspect(db.engine)
    assert inspector.has_table("provider_accounts")
    assert inspector.has_table("provider_sessions")

    account_columns = {column["name"]: column for column in inspector.get_columns("provider_accounts")}
    session_columns = {column["name"]: column for column in inspector.get_columns("provider_sessions")}
    assert set(account_columns) >= {
        "id",
        "instructor_id",
        "email_normalized",
        "password_hash",
        "is_demo",
        "session_version",
        "disabled_at",
        "password_changed_at",
        "created_at",
        "updated_at",
    }
    invite_columns = {column["name"]: column for column in inspector.get_columns("invite_tokens")}
    assert invite_columns["is_demo"]["nullable"] is False
    assert set(session_columns) >= {
        "id",
        "account_id",
        "token_digest",
        "session_version",
        "created_at",
        "expires_at",
        "revoked_at",
        "revocation_reason",
    }
    for name in ("disabled_at", "password_changed_at", "created_at", "updated_at"):
        assert account_columns[name]["type"].timezone is True
    for name in ("created_at", "expires_at", "revoked_at"):
        assert session_columns[name]["type"].timezone is True

    account_uniques = {constraint["name"] for constraint in inspector.get_unique_constraints("provider_accounts")}
    account_checks = {constraint["name"] for constraint in inspector.get_check_constraints("provider_accounts")}
    session_checks = {constraint["name"] for constraint in inspector.get_check_constraints("provider_sessions")}
    session_indexes = {index["name"]: index for index in inspector.get_indexes("provider_sessions")}
    appointment_indexes = {index["name"]: index for index in inspector.get_indexes("appointments")}
    assert account_uniques >= {
        "uq_provider_accounts_email_normalized",
        "uq_provider_accounts_instructor_id",
    }
    assert "ck_provider_accounts_session_version" in account_checks
    assert session_checks >= {
        "ck_provider_sessions_session_version",
        "ck_provider_sessions_digest_length",
    }
    assert session_indexes["uq_provider_sessions_token_digest"]["unique"] is True
    assert "ix_provider_sessions_account_revoked" in session_indexes
    assert "ix_provider_sessions_expires_at" in session_indexes
    composite = appointment_indexes["uq_appointments_instructor_idempotency_key"]
    assert composite["column_names"] == ["instructor_id", "idempotency_key"]
    assert composite["unique"] is True
    assert "uq_appointments_idempotency_key" not in appointment_indexes

    account_fks = {foreign_key["name"]: foreign_key for foreign_key in inspector.get_foreign_keys("provider_accounts")}
    session_fks = {foreign_key["name"]: foreign_key for foreign_key in inspector.get_foreign_keys("provider_sessions")}
    assert account_fks["fk_provider_accounts_instructor_id"]["referred_table"] == "instructors"
    assert session_fks["fk_provider_sessions_account_id"]["referred_table"] == "provider_accounts"


def test_safe_downgrade_restores_original_global_partial_index(
    migrated_postgres_db,
    monkeypatch,
):
    monkeypatch.setenv("SWIM_DATABASE_URL", TEST_DATABASE_URL)
    config = _alembic_config()
    command.downgrade(config, "20260922_0008")
    inspector = inspect(db.engine)
    assert not inspector.has_table("provider_sessions")
    assert not inspector.has_table("provider_accounts")
    assert "is_demo" not in {
        column["name"] for column in inspector.get_columns("invite_tokens")
    }
    indexes = {index["name"]: index for index in inspector.get_indexes("appointments")}
    assert indexes["uq_appointments_idempotency_key"]["column_names"] == ["idempotency_key"]
    assert indexes["uq_appointments_idempotency_key"]["unique"] is True
    assert "uq_appointments_instructor_idempotency_key" not in indexes
    command.upgrade(config, "head")
    with db.SessionLocal() as session:
        demo_flags = {
            token: is_demo
            for token, is_demo in session.query(
                InviteTokenDB.token,
                InviteTokenDB.is_demo,
            ).filter(
                InviteTokenDB.token.in_(
                    ["demo-invite-token", "invite_sofia_0826"]
                )
            )
        }
    assert demo_flags == {
        "demo-invite-token": True,
        "invite_sofia_0826": True,
    }


def test_downgrade_aborts_without_data_loss_on_cross_provider_duplicate_keys(
    migrated_postgres_db,
    monkeypatch,
):
    with db.SessionLocal() as session:
        session.add(
            InstructorDB(
                id="inst_migration_other",
                name="Migration Other",
                main_address="2 Migration Way",
                lat=30.1,
                lng=-97.1,
                work_start_time="08:00",
                work_end_time="17:00",
                timezone="America/Chicago",
            )
        )
        session.commit()
    shared_key = "cross-provider-downgrade-key"
    first_id = db.save_appointment(
        "inst_001",
        "client_alice",
        "First duplicate",
        datetime.datetime(2026, 9, 1, 10, 0),
        datetime.datetime(2026, 9, 1, 11, 0),
        "450 North Blvd",
        30.3,
        -97.7,
        idempotency_key=shared_key,
    )
    second_id = db.save_appointment(
        "inst_migration_other",
        "client_other",
        "Second duplicate",
        datetime.datetime(2026, 9, 1, 10, 0),
        datetime.datetime(2026, 9, 1, 11, 0),
        "2 Migration Way",
        30.1,
        -97.1,
        idempotency_key=shared_key,
    )

    monkeypatch.setenv("SWIM_DATABASE_URL", TEST_DATABASE_URL)
    with pytest.raises(RuntimeError, match="Resolve cross-provider duplicate keys"):
        command.downgrade(_alembic_config(), "20260922_0008")

    inspector = inspect(db.engine)
    assert inspector.has_table("provider_accounts")
    indexes = {index["name"] for index in inspector.get_indexes("appointments")}
    assert "uq_appointments_instructor_idempotency_key" in indexes
    with db.SessionLocal() as session:
        assert session.get(db.AppointmentDB, first_id) is not None
        assert session.get(db.AppointmentDB, second_id) is not None


def _alembic_config() -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return config
