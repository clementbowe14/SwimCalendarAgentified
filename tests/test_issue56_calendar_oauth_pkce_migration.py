import datetime
import hashlib

import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import Integer, LargeBinary, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import configure_mappers

from app import calendar_oauth_states, db
from app.models import (
    InstructorDB,
    ProviderAccountDB,
    ProviderCalendarOAuthPKCEVerifierDB,
    ProviderCalendarOAuthStateDB,
    ProviderSessionDB,
)
from tests.db_test_utils import TEST_DATABASE_URL


OAUTH_STATE_REVISION = "20261112_0016"
PKCE_REVISION = "20261112_0017"
BROWSER_BINDING_REVISION = "20261113_0018"
TABLE_NAME = "provider_calendar_oauth_pkce_verifiers"
UTC = datetime.timezone.utc


@pytest.fixture(autouse=True)
def use_test_database_for_alembic(monkeypatch):
    monkeypatch.setenv("SWIM_DATABASE_URL", TEST_DATABASE_URL)


def test_pkce_revision_remains_before_browser_binding_revision():
    script = ScriptDirectory.from_config(_config())
    assert script.get_revision(PKCE_REVISION).down_revision == (
        OAUTH_STATE_REVISION
    )
    assert script.get_revision(BROWSER_BINDING_REVISION).down_revision == (
        PKCE_REVISION
    )


def test_schema_contains_only_a_bound_encrypted_verifier_envelope(
    migrated_postgres_db,
):
    inspector = inspect(db.engine)
    assert inspector.has_table(TABLE_NAME)
    columns = {
        column["name"]: column
        for column in inspector.get_columns(TABLE_NAME)
    }
    assert set(columns) == {
        "state_digest",
        "verifier_version",
        "verifier_key_id",
        "verifier_nonce",
        "verifier_ciphertext",
    }
    assert isinstance(columns["state_digest"]["type"], LargeBinary)
    assert isinstance(columns["verifier_version"]["type"], Integer)
    assert columns["verifier_key_id"]["type"].length == 64
    assert isinstance(columns["verifier_nonce"]["type"], LargeBinary)
    assert isinstance(columns["verifier_ciphertext"]["type"], LargeBinary)
    assert all(column["nullable"] is False for column in columns.values())
    assert all(column["default"] is None for column in columns.values())

    checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints(TABLE_NAME)
    }
    assert checks == {
        "ck_provider_calendar_oauth_pkce_verifiers_digest_length",
        "ck_provider_calendar_oauth_pkce_verifiers_version",
        "ck_provider_calendar_oauth_pkce_verifiers_key_id",
        "ck_provider_calendar_oauth_pkce_verifiers_nonce_length",
        "ck_provider_calendar_oauth_pkce_verifiers_ciphertext_length",
    }
    primary_key = inspector.get_pk_constraint(TABLE_NAME)
    assert primary_key["name"] == (
        "pk_provider_calendar_oauth_pkce_verifiers"
    )
    assert primary_key["constrained_columns"] == ["state_digest"]

    foreign_keys = {
        foreign_key["name"]: foreign_key
        for foreign_key in inspector.get_foreign_keys(TABLE_NAME)
    }
    assert set(foreign_keys) == {
        "fk_provider_calendar_oauth_pkce_verifiers_state"
    }
    state_owner = foreign_keys[
        "fk_provider_calendar_oauth_pkce_verifiers_state"
    ]
    assert state_owner["referred_table"] == "provider_calendar_oauth_states"
    assert state_owner["constrained_columns"] == ["state_digest"]
    assert state_owner["referred_columns"] == ["state_digest"]
    assert state_owner["options"]["ondelete"] == "CASCADE"

    indexes = {
        index["name"]: index
        for index in inspector.get_indexes(TABLE_NAME)
    }
    assert set(indexes) == {
        "uq_provider_calendar_oauth_pkce_verifiers_key_nonce"
    }
    key_nonce = indexes[
        "uq_provider_calendar_oauth_pkce_verifiers_key_nonce"
    ]
    assert key_nonce["unique"] is True
    assert key_nonce["column_names"] == [
        "verifier_key_id",
        "verifier_nonce",
    ]

    forbidden_columns = {
        "verifier",
        "raw_verifier",
        "code_verifier",
        "code_challenge",
        "state",
        "raw_state",
        "account_id",
        "session_id",
        "provider",
        "created_at",
        "expires_at",
        "authorization_code",
        "redirect_uri",
        "access_token",
        "refresh_token",
        "calendar_id",
    }
    assert forbidden_columns.isdisjoint(columns)


def test_opaque_envelope_round_trips_and_relationships_resolve(
    migrated_postgres_db,
):
    with db.SessionLocal() as session:
        account = _demo_account(session)
        provider_session = _add_session(session, account, suffix="roundtrip")
        oauth_state = _add_state(
            session,
            account,
            provider_session,
            suffix="roundtrip",
        )
        envelope = ProviderCalendarOAuthPKCEVerifierDB(
            **_envelope_values(oauth_state.state_digest, suffix="roundtrip")
        )
        oauth_state.pkce_verifier = envelope
        session.commit()
        digest = oauth_state.state_digest
        session.expire_all()

        stored = session.get(ProviderCalendarOAuthPKCEVerifierDB, digest)
        assert stored is not None
        assert stored.oauth_state.state_digest == digest
        assert stored.oauth_state.pkce_verifier is stored
        assert stored.verifier_version == 1
        assert len(stored.verifier_nonce) == 12
        assert len(stored.verifier_ciphertext) == 59

    configure_mappers()
    assert ProviderCalendarOAuthStateDB.pkce_verifier.property.uselist is False
    assert (
        ProviderCalendarOAuthPKCEVerifierDB.oauth_state.property.mapper.class_
        is ProviderCalendarOAuthStateDB
    )


def test_existing_state_repository_remains_compatible_without_a_verifier(
    migrated_postgres_db,
):
    now = datetime.datetime.now(UTC)
    with db.SessionLocal() as setup_session:
        account = _demo_account(setup_session)
        provider_session = _add_session(
            setup_session,
            account,
            suffix="repository-compatibility",
            now=now,
        )
        account_id = account.id
        session_id = provider_session.id
        setup_session.commit()

    with db.SessionLocal.begin() as issue_session:
        issued = calendar_oauth_states.issue_google_calendar_oauth_state(
            issue_session,
            account_id=account_id,
            session_id=session_id,
            now=now,
        )

    digest = hashlib.sha256(issued.raw_state.encode("ascii")).digest()
    with db.SessionLocal() as inspection_session:
        stored_state = inspection_session.get(
            ProviderCalendarOAuthStateDB,
            digest,
        )
        assert stored_state is not None
        assert stored_state.pkce_verifier is None

    with db.SessionLocal.begin() as consume_session:
        consumed = calendar_oauth_states.consume_google_calendar_oauth_state(
            consume_session,
            raw_state=issued.raw_state,
            now=now,
        )
    assert consumed.account_id == account_id
    assert consumed.session_id == session_id


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("state_digest", b"d" * 31),
        ("verifier_version", 0),
        ("verifier_version", 2),
        ("verifier_key_id", ""),
        ("verifier_key_id", "-starts-with-punctuation"),
        ("verifier_key_id", "contains whitespace"),
        ("verifier_nonce", b"n" * 11),
        ("verifier_nonce", b"n" * 13),
        ("verifier_ciphertext", b"c" * 58),
        ("verifier_ciphertext", b"c" * 60),
    ],
)
def test_malformed_envelope_fields_are_rejected(
    migrated_postgres_db,
    field,
    invalid_value,
):
    digest = _persist_state(suffix=f"malformed-{field}")
    values = _envelope_values(digest, suffix=f"malformed-{field}")
    values[field] = invalid_value

    with pytest.raises(IntegrityError):
        with db.engine.begin() as connection:
            connection.execute(
                ProviderCalendarOAuthPKCEVerifierDB.__table__.insert().values(
                    **values
                )
            )


def test_unknown_state_and_second_envelope_for_one_state_are_rejected(
    migrated_postgres_db,
):
    unknown_digest = hashlib.sha256(b"unknown-state").digest()
    with pytest.raises(IntegrityError):
        with db.engine.begin() as connection:
            connection.execute(
                ProviderCalendarOAuthPKCEVerifierDB.__table__.insert().values(
                    **_envelope_values(unknown_digest, suffix="unknown")
                )
            )

    digest = _persist_state(suffix="one-envelope")
    with db.engine.begin() as connection:
        connection.execute(
            ProviderCalendarOAuthPKCEVerifierDB.__table__.insert().values(
                **_envelope_values(digest, suffix="first-envelope")
            )
        )
    with pytest.raises(IntegrityError):
        with db.engine.begin() as connection:
            connection.execute(
                ProviderCalendarOAuthPKCEVerifierDB.__table__.insert().values(
                    **_envelope_values(digest, suffix="second-envelope")
                )
            )


def test_nonce_cannot_repeat_under_one_key_but_may_repeat_across_keys(
    migrated_postgres_db,
):
    first_digest = _persist_state(suffix="nonce-first")
    second_digest = _persist_state(suffix="nonce-second")
    shared_nonce = b"shared-nonce"
    first = _envelope_values(first_digest, suffix="nonce-first")
    first["verifier_nonce"] = shared_nonce
    first["verifier_key_id"] = "pkce-key-a"
    with db.engine.begin() as connection:
        connection.execute(
            ProviderCalendarOAuthPKCEVerifierDB.__table__.insert().values(
                **first
            )
        )

    duplicate = _envelope_values(second_digest, suffix="nonce-duplicate")
    duplicate["verifier_nonce"] = shared_nonce
    duplicate["verifier_key_id"] = "pkce-key-a"
    with pytest.raises(IntegrityError):
        with db.engine.begin() as connection:
            connection.execute(
                ProviderCalendarOAuthPKCEVerifierDB.__table__.insert().values(
                    **duplicate
                )
            )

    duplicate["verifier_key_id"] = "pkce-key-b"
    with db.engine.begin() as connection:
        connection.execute(
            ProviderCalendarOAuthPKCEVerifierDB.__table__.insert().values(
                **duplicate
            )
        )
    assert _count_verifiers() == 2


def test_deleting_verifier_preserves_its_parent_state(migrated_postgres_db):
    digest = _persist_state_with_verifier(suffix="reverse-delete")
    with db.SessionLocal.begin() as session:
        verifier = session.get(ProviderCalendarOAuthPKCEVerifierDB, digest)
        session.delete(verifier)

    with db.SessionLocal() as session:
        assert session.get(ProviderCalendarOAuthPKCEVerifierDB, digest) is None
        state = session.get(ProviderCalendarOAuthStateDB, digest)
        assert state is not None
        assert state.pkce_verifier is None


@pytest.mark.parametrize("deleted_owner", ["state", "session", "account"])
def test_parent_deletion_cascades_to_verifier(
    migrated_postgres_db,
    deleted_owner,
):
    suffix = f"cascade-{deleted_owner}"
    with db.SessionLocal() as session:
        account = _add_account(session, suffix=suffix)
        provider_session = _add_session(session, account, suffix=suffix)
        oauth_state = _add_state(
            session,
            account,
            provider_session,
            suffix=suffix,
        )
        oauth_state.pkce_verifier = ProviderCalendarOAuthPKCEVerifierDB(
            **_envelope_values(oauth_state.state_digest, suffix=suffix)
        )
        session.commit()
        digest = oauth_state.state_digest
        account_id = account.id
        session_id = provider_session.id

    with db.engine.begin() as connection:
        if deleted_owner == "state":
            connection.execute(
                text(
                    "DELETE FROM provider_calendar_oauth_states "
                    "WHERE state_digest = :digest"
                ),
                {"digest": digest},
            )
        elif deleted_owner == "session":
            connection.execute(
                text("DELETE FROM provider_sessions WHERE id = :session_id"),
                {"session_id": session_id},
            )
        else:
            connection.execute(
                text("DELETE FROM provider_accounts WHERE id = :account_id"),
                {"account_id": account_id},
            )

    assert _count_verifiers(digest) == 0


def test_empty_downgrade_and_reupgrade_round_trip(migrated_postgres_db):
    command.downgrade(_config(), OAUTH_STATE_REVISION)
    inspector = inspect(db.engine)
    assert not inspector.has_table(TABLE_NAME)
    assert inspector.has_table("provider_calendar_oauth_states")
    assert _current_revision() == OAUTH_STATE_REVISION

    command.upgrade(_config(), PKCE_REVISION)
    assert inspect(db.engine).has_table(TABLE_NAME)
    assert _current_revision() == PKCE_REVISION


def test_upgrade_does_not_require_or_backfill_existing_states(
    migrated_postgres_db,
):
    command.downgrade(_config(), OAUTH_STATE_REVISION)
    digest = _persist_state_without_binding_column(suffix="no-backfill")

    command.upgrade(_config(), PKCE_REVISION)

    assert _count_states(digest) == 1
    assert _count_verifiers(digest) == 0


def test_failed_upgrade_rolls_back_table_index_and_revision(
    migrated_postgres_db,
    monkeypatch,
):
    command.downgrade(_config(), OAUTH_STATE_REVISION)
    original_create_index = Operations.create_index

    def fail_pkce_index(self, index_name, *args, **kwargs):
        if index_name == "uq_provider_calendar_oauth_pkce_verifiers_key_nonce":
            raise RuntimeError("forced migration failure")
        return original_create_index(self, index_name, *args, **kwargs)

    monkeypatch.setattr(Operations, "create_index", fail_pkce_index)
    with pytest.raises(RuntimeError, match="forced migration failure"):
        command.upgrade(_config(), PKCE_REVISION)

    assert not inspect(db.engine).has_table(TABLE_NAME)
    assert _current_revision() == OAUTH_STATE_REVISION


def test_failed_downgrade_rolls_back_index_table_row_and_revision(
    migrated_postgres_db,
    monkeypatch,
):
    now = datetime.datetime.now(UTC)
    digest = _persist_state_with_verifier(
        suffix="failed-downgrade",
        created_at=now - datetime.timedelta(minutes=11),
        expires_at=now - datetime.timedelta(minutes=1),
    )
    original_revision = _current_revision()
    original_drop_table = Operations.drop_table

    def fail_pkce_table_drop(self, table_name, *args, **kwargs):
        if table_name == TABLE_NAME:
            raise RuntimeError("forced migration failure")
        return original_drop_table(self, table_name, *args, **kwargs)

    monkeypatch.setattr(Operations, "drop_table", fail_pkce_table_drop)
    with pytest.raises(RuntimeError, match="forced migration failure"):
        command.downgrade(_config(), OAUTH_STATE_REVISION)

    inspector = inspect(db.engine)
    assert inspector.has_table(TABLE_NAME)
    assert "uq_provider_calendar_oauth_pkce_verifiers_key_nonce" in {
        index["name"] for index in inspector.get_indexes(TABLE_NAME)
    }
    assert _count_verifiers(digest) == 1
    assert _current_revision() == original_revision


def test_expired_verifier_may_be_discarded_by_downgrade(
    migrated_postgres_db,
):
    now = datetime.datetime.now(UTC)
    digest = _persist_state_with_verifier(
        suffix="expired-downgrade",
        created_at=now - datetime.timedelta(minutes=11),
        expires_at=now - datetime.timedelta(minutes=1),
    )

    command.downgrade(_config(), OAUTH_STATE_REVISION)

    assert not inspect(db.engine).has_table(TABLE_NAME)
    assert _count_states(digest) == 1
    assert _current_revision() == OAUTH_STATE_REVISION
    command.upgrade(_config(), PKCE_REVISION)
    assert _count_verifiers(digest) == 0


def test_active_verifier_refuses_downgrade_without_disclosing_or_losing_data(
    migrated_postgres_db,
):
    digest = _persist_state_with_verifier(suffix="active-downgrade")
    with db.engine.connect() as connection:
        sensitive_values = connection.execute(
            text(
                "SELECT verifier.verifier_key_id, verifier.verifier_nonce, "
                "verifier.verifier_ciphertext, oauth_state.account_id, "
                "oauth_state.session_id "
                "FROM provider_calendar_oauth_pkce_verifiers AS verifier "
                "JOIN provider_calendar_oauth_states AS oauth_state "
                "USING (state_digest) "
                "WHERE verifier.state_digest = :digest"
            ),
            {"digest": digest},
        ).mappings().one()
    original_revision = _current_revision()

    with pytest.raises(RuntimeError, match="authorization attempt is active") as exc_info:
        command.downgrade(_config(), OAUTH_STATE_REVISION)
    message = str(exc_info.value)
    assert digest.hex() not in message
    for name in ("verifier_key_id", "account_id", "session_id"):
        assert sensitive_values[name] not in message
    for name in ("verifier_nonce", "verifier_ciphertext"):
        assert sensitive_values[name].hex() not in message
        assert repr(sensitive_values[name]) not in message

    assert inspect(db.engine).has_table(TABLE_NAME)
    assert _count_verifiers(digest) == 1
    assert _current_revision() == original_revision


@pytest.mark.parametrize(
    "locked_table",
    ["provider_calendar_oauth_states", TABLE_NAME],
)
def test_downgrade_lock_contention_preserves_schema_row_and_revision(
    migrated_postgres_db,
    locked_table,
):
    digest = _persist_state_with_verifier(
        suffix=f"downgrade-lock-{locked_table}"
    )
    original_revision = _current_revision()

    with db.engine.connect() as locking_connection:
        locking_transaction = locking_connection.begin()
        try:
            locking_connection.execute(
                text(f"LOCK TABLE {locked_table} IN ACCESS SHARE MODE")
            )
            with pytest.raises(OperationalError) as exc_info:
                command.downgrade(_config(), OAUTH_STATE_REVISION)
            assert getattr(exc_info.value.orig, "sqlstate", None) == "55P03"
        finally:
            locking_transaction.rollback()

    assert inspect(db.engine).has_table(TABLE_NAME)
    assert _count_verifiers(digest) == 1
    assert _current_revision() == original_revision


def test_upgrade_lock_contention_preserves_previous_schema_and_revision(
    migrated_postgres_db,
):
    command.downgrade(_config(), OAUTH_STATE_REVISION)

    with db.engine.connect() as locking_connection:
        locking_transaction = locking_connection.begin()
        try:
            locking_connection.execute(
                text(
                    "LOCK TABLE provider_calendar_oauth_states "
                    "IN ROW EXCLUSIVE MODE"
                )
            )
            with pytest.raises(OperationalError) as exc_info:
                command.upgrade(_config(), PKCE_REVISION)
            assert getattr(exc_info.value.orig, "sqlstate", None) == "55P03"
        finally:
            locking_transaction.rollback()

    assert not inspect(db.engine).has_table(TABLE_NAME)
    assert _current_revision() == OAUTH_STATE_REVISION
    command.upgrade(_config(), PKCE_REVISION)


def _demo_account(session) -> ProviderAccountDB:
    return session.query(ProviderAccountDB).filter_by(
        instructor_id="inst_001"
    ).one()


def _add_account(session, *, suffix: str) -> ProviderAccountDB:
    instructor = InstructorDB(
        id=f"inst_pkce_{suffix}",
        name=f"PKCE Provider {suffix}",
        main_address="1 Proof Key Way",
        lat=30.2,
        lng=-97.2,
        work_start_time="08:00",
        work_end_time="17:00",
        timezone="America/Chicago",
    )
    account = ProviderAccountDB(
        id=f"acct_pkce_{suffix}",
        instructor=instructor,
        email_normalized=f"pkce-{suffix}@example.com",
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
    now: datetime.datetime | None = None,
) -> ProviderSessionDB:
    now = now or datetime.datetime.now(UTC)
    provider_session = ProviderSessionDB(
        id=f"sess_pkce_{suffix}",
        account_id=account.id,
        token_digest=hashlib.sha256(
            f"pkce-session-token-{suffix}".encode()
        ).digest(),
        session_version=account.session_version,
        created_at=now - datetime.timedelta(minutes=1),
        expires_at=now + datetime.timedelta(hours=1),
    )
    session.add(provider_session)
    session.flush()
    return provider_session


def _add_state(
    session,
    account: ProviderAccountDB,
    provider_session: ProviderSessionDB,
    *,
    suffix: str,
    created_at: datetime.datetime | None = None,
    expires_at: datetime.datetime | None = None,
) -> ProviderCalendarOAuthStateDB:
    created_at = created_at or datetime.datetime.now(UTC)
    expires_at = expires_at or created_at + datetime.timedelta(minutes=10)
    oauth_state = ProviderCalendarOAuthStateDB(
        state_digest=hashlib.sha256(f"pkce-state-{suffix}".encode()).digest(),
        account_id=account.id,
        session_id=provider_session.id,
        provider="google",
        created_at=created_at,
        expires_at=expires_at,
    )
    session.add(oauth_state)
    session.flush()
    return oauth_state


def _envelope_values(
    state_digest: bytes,
    *,
    suffix: str,
) -> dict:
    return {
        "state_digest": state_digest,
        "verifier_version": 1,
        "verifier_key_id": f"pkce-key-{suffix}"[:64],
        "verifier_nonce": hashlib.sha256(
            f"pkce-nonce-{suffix}".encode()
        ).digest()[:12],
        "verifier_ciphertext": hashlib.sha512(
            f"opaque-pkce-envelope-{suffix}".encode()
        ).digest()[:59],
    }


def _persist_state(
    *,
    suffix: str,
    created_at: datetime.datetime | None = None,
    expires_at: datetime.datetime | None = None,
) -> bytes:
    with db.SessionLocal() as session:
        account = _demo_account(session)
        provider_session = _add_session(session, account, suffix=suffix)
        oauth_state = _add_state(
            session,
            account,
            provider_session,
            suffix=suffix,
            created_at=created_at,
            expires_at=expires_at,
        )
        session.commit()
        return oauth_state.state_digest


def _persist_state_without_binding_column(*, suffix: str) -> bytes:
    created_at = datetime.datetime.now(UTC)
    state_digest = hashlib.sha256(
        f"pkce-state-{suffix}".encode()
    ).digest()
    session_id = f"sess_pkce_{suffix}"
    with db.engine.begin() as connection:
        account = connection.execute(
            text(
                "SELECT id, session_version FROM provider_accounts "
                "WHERE instructor_id = 'inst_001'"
            )
        ).mappings().one()
        connection.execute(
            text(
                "INSERT INTO provider_sessions "
                "(id, account_id, token_digest, session_version, created_at, "
                "expires_at, revoked_at, revocation_reason) "
                "VALUES (:session_id, :account_id, :token_digest, "
                ":session_version, :created_at, :expires_at, NULL, NULL)"
            ),
            {
                "session_id": session_id,
                "account_id": account["id"],
                "token_digest": hashlib.sha256(
                    f"pkce-session-token-{suffix}".encode()
                ).digest(),
                "session_version": account["session_version"],
                "created_at": created_at - datetime.timedelta(minutes=1),
                "expires_at": created_at + datetime.timedelta(hours=1),
            },
        )
        connection.execute(
            text(
                "INSERT INTO provider_calendar_oauth_states "
                "(state_digest, account_id, session_id, provider, created_at, "
                "expires_at) VALUES (:state_digest, :account_id, :session_id, "
                "'google', :created_at, :expires_at)"
            ),
            {
                "state_digest": state_digest,
                "account_id": account["id"],
                "session_id": session_id,
                "created_at": created_at,
                "expires_at": created_at + datetime.timedelta(minutes=10),
            },
        )
    return state_digest


def _persist_state_with_verifier(
    *,
    suffix: str,
    created_at: datetime.datetime | None = None,
    expires_at: datetime.datetime | None = None,
) -> bytes:
    with db.SessionLocal() as session:
        account = _demo_account(session)
        provider_session = _add_session(session, account, suffix=suffix)
        oauth_state = _add_state(
            session,
            account,
            provider_session,
            suffix=suffix,
            created_at=created_at,
            expires_at=expires_at,
        )
        oauth_state.pkce_verifier = ProviderCalendarOAuthPKCEVerifierDB(
            **_envelope_values(oauth_state.state_digest, suffix=suffix)
        )
        session.commit()
        return oauth_state.state_digest


def _count_verifiers(digest: bytes | None = None) -> int:
    query = f"SELECT count(*) FROM {TABLE_NAME}"
    parameters = {}
    if digest is not None:
        query += " WHERE state_digest = :digest"
        parameters["digest"] = digest
    with db.engine.connect() as connection:
        return connection.execute(text(query), parameters).scalar_one()


def _count_states(digest: bytes) -> int:
    with db.engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT count(*) FROM provider_calendar_oauth_states "
                "WHERE state_digest = :digest"
            ),
            {"digest": digest},
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
