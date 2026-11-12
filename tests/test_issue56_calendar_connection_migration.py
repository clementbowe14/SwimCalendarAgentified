import datetime

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import LargeBinary, inspect, text
from sqlalchemy.exc import DataError, IntegrityError, OperationalError
from sqlalchemy.orm import configure_mappers

from app import db
from app.calendar_credentials import (
    CalendarCredentialCipher,
    CalendarCredentialDecryptionError,
    CalendarCredentialScope,
    EncryptedCalendarCredential,
)
from app.models import (
    InstructorDB,
    ProviderAccountDB,
    ProviderCalendarConnectionDB,
)
from tests.db_test_utils import TEST_DATABASE_URL


PREVIOUS_REVISION = "20261111_0014"
CALENDAR_CONNECTION_REVISION = "20261112_0015"
KEY_ID = "calendar-key-2026"
CIPHER = CalendarCredentialCipher(
    active_key_id=KEY_ID,
    keys={KEY_ID: b"c" * 32},
)


@pytest.fixture(autouse=True)
def use_test_database_for_alembic(monkeypatch):
    monkeypatch.setenv("SWIM_DATABASE_URL", TEST_DATABASE_URL)


def test_calendar_connection_revision_remains_in_alembic_history():
    assert (
        ScriptDirectory.from_config(_config()).get_revision(
            CALENDAR_CONNECTION_REVISION
        )
        is not None
    )


def test_schema_has_only_encrypted_provider_owned_connection_fields(
    migrated_postgres_db,
):
    inspector = inspect(db.engine)
    assert inspector.has_table("provider_calendar_connections")
    columns = {
        column["name"]: column
        for column in inspector.get_columns("provider_calendar_connections")
    }
    assert set(columns) == {
        "id",
        "account_id",
        "provider",
        "credential_version",
        "credential_key_id",
        "credential_nonce",
        "credential_ciphertext",
        "authorization_required_at",
        "created_at",
        "updated_at",
    }
    assert columns["id"]["type"].length == 40
    assert columns["provider"]["type"].length == 16
    assert columns["credential_key_id"]["type"].length == 64
    assert isinstance(columns["credential_nonce"]["type"], LargeBinary)
    assert isinstance(columns["credential_ciphertext"]["type"], LargeBinary)
    assert columns["authorization_required_at"]["nullable"] is True
    for name in (
        "id",
        "account_id",
        "provider",
        "credential_version",
        "credential_key_id",
        "credential_nonce",
        "credential_ciphertext",
        "created_at",
        "updated_at",
    ):
        assert columns[name]["nullable"] is False
    for name in ("authorization_required_at", "created_at", "updated_at"):
        assert columns[name]["type"].timezone is True
    assert columns["provider"]["default"] is not None
    assert columns["created_at"]["default"] is not None
    assert columns["updated_at"]["default"] is not None

    check_names = {
        constraint["name"]
        for constraint in inspector.get_check_constraints(
            "provider_calendar_connections"
        )
    }
    assert check_names == {
        "ck_provider_calendar_connections_id",
        "ck_provider_calendar_connections_provider",
        "ck_provider_calendar_connections_credential_version",
        "ck_provider_calendar_connections_credential_key_id",
        "ck_provider_calendar_connections_credential_nonce_length",
        "ck_provider_calendar_connections_credential_ciphertext_length",
        "ck_provider_calendar_connections_timestamp_order",
        "ck_provider_calendar_connections_authorization_timing",
    }
    unique_names = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            "provider_calendar_connections"
        )
    }
    assert unique_names == {"uq_provider_calendar_connections_account_id"}
    indexes = {
        index["name"]: index
        for index in inspector.get_indexes("provider_calendar_connections")
    }
    key_nonce = indexes["uq_provider_calendar_connections_key_nonce"]
    assert key_nonce["unique"] is True
    assert key_nonce["column_names"] == [
        "credential_key_id",
        "credential_nonce",
    ]
    foreign_keys = {
        foreign_key["name"]: foreign_key
        for foreign_key in inspector.get_foreign_keys(
            "provider_calendar_connections"
        )
    }
    account_fk = foreign_keys["fk_provider_calendar_connections_account_id"]
    assert account_fk["referred_table"] == "provider_accounts"
    assert account_fk["constrained_columns"] == ["account_id"]
    assert account_fk["options"]["ondelete"] == "CASCADE"
    primary_key = inspector.get_pk_constraint("provider_calendar_connections")
    assert primary_key["name"] == "pk_provider_calendar_connections"
    assert primary_key["constrained_columns"] == ["id"]

    forbidden_fragments = {
        "refresh_token",
        "access_token",
        "authorization_code",
        "calendar_id",
        "calendar_email",
        "event_id",
        "title",
        "attendee",
        "description",
        "location",
    }
    assert forbidden_fragments.isdisjoint(columns)


def test_real_encrypted_envelope_round_trips_through_model_and_relationship(
    migrated_postgres_db,
):
    with db.SessionLocal() as session:
        account = _demo_account(session)
        values, plaintext = _connection_values(
            account,
            connection_id="calconn_11111111111111111111111111111111",
        )
        session.add(ProviderCalendarConnectionDB(**values))
        session.commit()
        session.expire_all()

        stored = session.get(ProviderCalendarConnectionDB, values["id"])
        assert stored is not None
        assert stored.account.id == account.id
        assert stored.account.calendar_connection is stored
        assert stored.authorization_required_at is None
        assert stored.created_at.tzinfo is not None
        assert stored.updated_at.tzinfo is not None

        envelope = EncryptedCalendarCredential(
            version=stored.credential_version,
            key_id=stored.credential_key_id,
            nonce=stored.credential_nonce,
            ciphertext=stored.credential_ciphertext,
        )
        scope = CalendarCredentialScope(
            instructor_id=stored.account.instructor_id,
            connection_id=stored.id,
            provider=stored.provider,
        )
        assert CIPHER.decrypt_refresh_token(envelope, scope=scope) == plaintext

        recovery_time = stored.updated_at + datetime.timedelta(minutes=1)
        stored.authorization_required_at = recovery_time
        stored.updated_at = recovery_time
        session.commit()
        session.expire_all()

        recovering = session.get(ProviderCalendarConnectionDB, values["id"])
        assert recovering.authorization_required_at == recovery_time
        assert recovering.authorization_required_at.tzinfo is not None


def test_transplanted_envelope_cannot_authenticate_under_another_connection_id(
    migrated_postgres_db,
):
    original_id = "calconn_12121212121212121212121212121212"
    transplanted_id = "calconn_13131313131313131313131313131313"
    with db.SessionLocal() as session:
        account = _demo_account(session)
        values, _ = _connection_values(account, connection_id=original_id)
        instructor_id = account.instructor_id
        session.add(ProviderCalendarConnectionDB(**values))
        session.commit()

    with db.engine.begin() as connection:
        connection.execute(
            ProviderCalendarConnectionDB.__table__.delete().where(
                ProviderCalendarConnectionDB.id == original_id
            )
        )
        transplanted_values = dict(values)
        transplanted_values["id"] = transplanted_id
        connection.execute(
            ProviderCalendarConnectionDB.__table__.insert().values(
                **transplanted_values
            )
        )

    with db.SessionLocal() as session:
        stored = session.get(ProviderCalendarConnectionDB, transplanted_id)
        envelope = EncryptedCalendarCredential(
            version=stored.credential_version,
            key_id=stored.credential_key_id,
            nonce=stored.credential_nonce,
            ciphertext=stored.credential_ciphertext,
        )

    with pytest.raises(CalendarCredentialDecryptionError):
        CIPHER.decrypt_refresh_token(
            envelope,
            scope=CalendarCredentialScope(
                instructor_id=instructor_id,
                connection_id=transplanted_id,
                provider="google",
            ),
        )


def test_model_mappers_resolve_one_to_one_account_relationship():
    configure_mappers()
    assert ProviderAccountDB.calendar_connection.property.uselist is False
    assert ProviderCalendarConnectionDB.account.property.mapper.class_ is ProviderAccountDB


def test_one_connection_per_account_and_key_nonce_reuse_are_rejected(
    migrated_postgres_db,
):
    with db.SessionLocal() as session:
        first_account = _demo_account(session)
        second_account = _add_provider_account(session, suffix="second")
        first_values, _ = _connection_values(
            first_account,
            connection_id="calconn_22222222222222222222222222222222",
        )
        duplicate_account_values, _ = _connection_values(
            first_account,
            connection_id="calconn_33333333333333333333333333333333",
        )
        second_account_id = second_account.id
        session.add(ProviderCalendarConnectionDB(**first_values))
        session.commit()

    with pytest.raises(IntegrityError):
        with db.engine.begin() as connection:
            connection.execute(
                ProviderCalendarConnectionDB.__table__.insert().values(
                    **duplicate_account_values
                )
            )

    duplicate_nonce_values = dict(first_values)
    duplicate_nonce_values.update(
        id="calconn_44444444444444444444444444444444",
        account_id=second_account_id,
    )
    with pytest.raises(IntegrityError):
        with db.engine.begin() as connection:
            connection.execute(
                ProviderCalendarConnectionDB.__table__.insert().values(
                    **duplicate_nonce_values
                )
            )


def test_envelope_storage_accepts_exact_supported_boundaries(
    migrated_postgres_db,
):
    with db.SessionLocal() as session:
        minimum_account = _demo_account(session)
        maximum_account = _add_provider_account(session, suffix="boundaries")
        minimum_values, _ = _connection_values(
            minimum_account,
            connection_id="calconn_14141414141414141414141414141414",
        )
        maximum_values, _ = _connection_values(
            maximum_account,
            connection_id="calconn_15151515151515151515151515151515",
        )
        minimum_values.update(
            credential_key_id="k",
            credential_nonce=b"a" * 12,
            credential_ciphertext=b"x" * 17,
        )
        maximum_values.update(
            credential_key_id="k" * 64,
            credential_nonce=b"b" * 12,
            credential_ciphertext=b"y" * 16_400,
        )
        session.add_all(
            [
                ProviderCalendarConnectionDB(**minimum_values),
                ProviderCalendarConnectionDB(**maximum_values),
            ]
        )
        session.commit()

    with db.engine.connect() as connection:
        lengths = connection.execute(
            text(
                "SELECT id, octet_length(credential_ciphertext) AS length "
                "FROM provider_calendar_connections ORDER BY id"
            )
        ).all()
    assert lengths == [
        (minimum_values["id"], 17),
        (maximum_values["id"], 16_400),
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "calendar-not-scoped"},
        {"provider": "outlook"},
        {"credential_version": 2},
        {"credential_key_id": "bad key id"},
        {"credential_key_id": "k" * 65},
        {"credential_nonce": b"n" * 11},
        {"credential_nonce": b"n" * 13},
        {"credential_ciphertext": b"x" * 16},
        {"credential_ciphertext": b"x" * 16_401},
        {
            "created_at": datetime.datetime(2026, 11, 12, 12, tzinfo=datetime.timezone.utc),
            "updated_at": datetime.datetime(2026, 11, 12, 11, tzinfo=datetime.timezone.utc),
        },
        {
            "created_at": datetime.datetime(2026, 11, 12, 12, tzinfo=datetime.timezone.utc),
            "updated_at": datetime.datetime(2026, 11, 12, 14, tzinfo=datetime.timezone.utc),
            "authorization_required_at": datetime.datetime(
                2026,
                11,
                12,
                11,
                tzinfo=datetime.timezone.utc,
            ),
        },
        {
            "created_at": datetime.datetime(2026, 11, 12, 12, tzinfo=datetime.timezone.utc),
            "updated_at": datetime.datetime(2026, 11, 12, 14, tzinfo=datetime.timezone.utc),
            "authorization_required_at": datetime.datetime(
                2026,
                11,
                12,
                15,
                tzinfo=datetime.timezone.utc,
            ),
        },
    ],
)
def test_database_rejects_malformed_connection_shapes(
    migrated_postgres_db,
    overrides,
):
    with db.SessionLocal() as session:
        values, _ = _connection_values(
            _demo_account(session),
            connection_id="calconn_55555555555555555555555555555555",
        )
    values.update(overrides)

    with pytest.raises((DataError, IntegrityError)):
        with db.engine.begin() as connection:
            connection.execute(
                ProviderCalendarConnectionDB.__table__.insert().values(**values)
            )


def test_unknown_account_is_rejected(migrated_postgres_db):
    with db.SessionLocal() as session:
        values, _ = _connection_values(
            _demo_account(session),
            connection_id="calconn_66666666666666666666666666666666",
        )
    values["account_id"] = "acct_missing"

    with pytest.raises(IntegrityError):
        with db.engine.begin() as connection:
            connection.execute(
                ProviderCalendarConnectionDB.__table__.insert().values(**values)
            )


def test_account_delete_cascades_but_connection_delete_preserves_account(
    migrated_postgres_db,
):
    with db.SessionLocal() as session:
        account = _add_provider_account(session, suffix="cascade")
        account_id = account.id
        first_values, _ = _connection_values(
            account,
            connection_id="calconn_77777777777777777777777777777777",
        )
        session.add(ProviderCalendarConnectionDB(**first_values))
        session.commit()

    with db.engine.begin() as connection:
        connection.execute(
            ProviderCalendarConnectionDB.__table__.delete().where(
                ProviderCalendarConnectionDB.id == first_values["id"]
            )
        )
        assert connection.execute(
            text("SELECT count(*) FROM provider_accounts WHERE id = :account_id"),
            {"account_id": account_id},
        ).scalar_one() == 1

    with db.SessionLocal() as session:
        account = session.get(ProviderAccountDB, account_id)
        second_values, _ = _connection_values(
            account,
            connection_id="calconn_88888888888888888888888888888888",
        )
        session.add(ProviderCalendarConnectionDB(**second_values))
        session.commit()

    with db.engine.begin() as connection:
        connection.execute(
            text("DELETE FROM provider_accounts WHERE id = :account_id"),
            {"account_id": account_id},
        )
        assert connection.execute(
            text(
                "SELECT count(*) FROM provider_calendar_connections "
                "WHERE account_id = :account_id"
            ),
            {"account_id": account_id},
        ).scalar_one() == 0


def test_empty_downgrade_and_reupgrade_round_trip(migrated_postgres_db):
    command.downgrade(_config(), PREVIOUS_REVISION)
    inspector = inspect(db.engine)
    assert not inspector.has_table("provider_calendar_connections")
    assert inspector.has_table("provider_accounts")
    with db.engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == PREVIOUS_REVISION

    command.upgrade(_config(), CALENDAR_CONNECTION_REVISION)
    assert inspect(db.engine).has_table("provider_calendar_connections")


def test_nonempty_downgrade_refuses_without_losing_connection(
    migrated_postgres_db,
):
    original_revision = _database_revision()
    with db.SessionLocal() as session:
        values, _ = _connection_values(
            _demo_account(session),
            connection_id="calconn_99999999999999999999999999999999",
        )
        session.add(ProviderCalendarConnectionDB(**values))
        session.commit()

    with pytest.raises(RuntimeError, match="Disconnect every provider calendar"):
        command.downgrade(_config(), PREVIOUS_REVISION)

    assert inspect(db.engine).has_table("provider_calendar_connections")
    with db.engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT count(*) FROM provider_calendar_connections WHERE id = :id"
            ),
            {"id": values["id"]},
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == original_revision


def test_downgrade_lock_contention_preserves_schema_row_and_revision(
    migrated_postgres_db,
):
    original_revision = _database_revision()
    with db.SessionLocal() as session:
        values, _ = _connection_values(
            _demo_account(session),
            connection_id="calconn_16161616161616161616161616161616",
        )
        session.add(ProviderCalendarConnectionDB(**values))
        session.commit()

    with db.engine.connect() as locking_connection:
        locking_transaction = locking_connection.begin()
        try:
            locking_connection.execute(
                text("LOCK TABLE provider_calendar_connections IN SHARE MODE")
            )
            with pytest.raises(OperationalError) as exc_info:
                command.downgrade(_config(), PREVIOUS_REVISION)
            assert getattr(exc_info.value.orig, "sqlstate", None) == "55P03"
        finally:
            locking_transaction.rollback()

    assert inspect(db.engine).has_table("provider_calendar_connections")
    with db.engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT count(*) FROM provider_calendar_connections WHERE id = :id"
            ),
            {"id": values["id"]},
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == original_revision


def _demo_account(session) -> ProviderAccountDB:
    return session.query(ProviderAccountDB).filter_by(instructor_id="inst_001").one()


def _add_provider_account(session, *, suffix: str) -> ProviderAccountDB:
    instructor = InstructorDB(
        id=f"inst_calendar_{suffix}",
        name=f"Calendar Provider {suffix}",
        main_address="1 Calendar Way",
        lat=30.2,
        lng=-97.2,
        work_start_time="08:00",
        work_end_time="17:00",
        timezone="America/Chicago",
    )
    account = ProviderAccountDB(
        id=f"acct_calendar_{suffix}",
        instructor=instructor,
        email_normalized=f"calendar-{suffix}@example.com",
        password_hash="not-used-by-schema-test",
        is_demo=False,
        session_version=1,
    )
    session.add(account)
    session.flush()
    return account


def _connection_values(
    account: ProviderAccountDB,
    *,
    connection_id: str,
) -> tuple[dict, str]:
    plaintext = f"refresh-token-for-{connection_id}"
    envelope = CIPHER.encrypt_refresh_token(
        plaintext,
        scope=CalendarCredentialScope(
            instructor_id=account.instructor_id,
            connection_id=connection_id,
            provider="google",
        ),
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    return (
        {
            "id": connection_id,
            "account_id": account.id,
            "provider": "google",
            "credential_version": envelope.version,
            "credential_key_id": envelope.key_id,
            "credential_nonce": envelope.nonce,
            "credential_ciphertext": envelope.ciphertext,
            "authorization_required_at": None,
            "created_at": now,
            "updated_at": now,
        },
        plaintext,
    )


def _config() -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return config


def _database_revision() -> str:
    with db.engine.connect() as connection:
        return connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
