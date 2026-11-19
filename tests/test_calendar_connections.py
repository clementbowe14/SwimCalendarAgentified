from __future__ import annotations

import datetime
import hashlib
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields

import pytest
from sqlalchemy import create_engine, event, inspect as sqlalchemy_inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import calendar_connections, calendar_credentials, db
from app.calendar_connections import (
    CalendarConnectionOwnerNotFound,
    CalendarConnectionPersistenceError,
    CalendarConnectionStatus,
    CalendarConnectionTransactionRequired,
)
from app.calendar_credentials import (
    CalendarCredentialCipher,
    CalendarCredentialDecryptionError,
    CalendarCredentialInputError,
    CalendarCredentialScope,
    EncryptedCalendarCredential,
)
from app.calendar_oauth_pkce import CalendarOAuthPKCECipher
from app.calendar_oauth_states import (
    CalendarOAuthStateInvalid,
    consume_google_calendar_oauth_state_with_pkce_and_browser_binding,
    issue_google_calendar_oauth_state_with_pkce_and_browser_binding,
)
from app.models import (
    InstructorDB,
    ProviderAccountDB,
    ProviderCalendarConnectionDB,
    ProviderCalendarOAuthPKCEVerifierDB,
    ProviderCalendarOAuthStateDB,
    ProviderSessionDB,
)


UTC = datetime.timezone.utc
KEY_ID = "repository-key-2026"
CIPHER = CalendarCredentialCipher(
    active_key_id=KEY_ID,
    keys={KEY_ID: b"r" * 32},
)
OAUTH_PKCE_CIPHER = CalendarOAuthPKCECipher(
    active_key_id="oauth-state-cancellation-key",
    root_keys={"oauth-state-cancellation-key": b"p" * 32},
)
T0 = datetime.datetime(2026, 11, 12, 14, 0, tzinfo=UTC)
POSTGRESQL_BIGINT_MAX = 9_223_372_036_854_775_807


def test_status_projection_is_disconnected_and_owner_errors_are_identifier_free(
    migrated_postgres_db,
):
    account_id, _ = _demo_owner()
    with db.SessionLocal() as session:
        status = calendar_connections.get_google_calendar_connection_status(
            session,
            account_id=account_id,
        )

    assert status == CalendarConnectionStatus(
        provider="google",
        state="disconnected",
        connected_at=None,
        updated_at=None,
        authorization_required_at=None,
    )
    assert {field.name for field in fields(status)} == {
        "provider",
        "state",
        "connected_at",
        "updated_at",
        "authorization_required_at",
    }
    assert set(calendar_connections.__all__) == {
        "CalendarConnectionError",
        "CalendarConnectionInputError",
        "CalendarConnectionOwnerNotFound",
        "CalendarConnectionPersistenceError",
        "CalendarConnectionState",
        "CalendarConnectionStatus",
        "CalendarConnectionTransactionRequired",
        "disconnect_google_calendar",
        "get_google_calendar_connection_status",
        "mark_google_calendar_authorization_required",
        "replace_google_calendar_connection",
    }
    assert not hasattr(calendar_connections, "EncryptedCalendarCredential")
    assert not hasattr(calendar_connections, "ProviderAccountDB")
    assert not hasattr(calendar_connections, "ProviderCalendarConnectionDB")

    missing_id = "acct_missing_private_owner"
    with db.SessionLocal() as session:
        with pytest.raises(CalendarConnectionOwnerNotFound) as exc_info:
            calendar_connections.get_google_calendar_connection_status(
                session,
                account_id=missing_id,
            )
    assert missing_id not in str(exc_info.value)
    assert missing_id not in repr(exc_info.value)


def test_status_read_preserves_pending_account_security_changes(
    migrated_postgres_db,
):
    account_id, _ = _demo_owner()
    session = db.SessionLocal()
    transaction = session.begin()
    try:
        account = session.get(ProviderAccountDB, account_id)
        original_session_version = account.session_version
        account.disabled_at = T0
        account.session_version += 1

        status = calendar_connections.get_google_calendar_connection_status(
            session,
            account_id=account_id,
        )

        assert status.state == "disconnected"
        assert account.disabled_at == T0
        assert account.session_version == original_session_version + 1
        assert account in session.dirty
        transaction.rollback()
    finally:
        session.close()

    with db.SessionLocal() as session:
        persisted = session.get(ProviderAccountDB, account_id)
        assert persisted.disabled_at is None
        assert persisted.session_version == original_session_version


def test_status_read_preserves_pending_connection_changes(
    migrated_postgres_db,
):
    account_id, _ = _demo_owner()
    _replace(account_id, token="pending-status-connection", now=T0)
    pending_mark = T0 + datetime.timedelta(minutes=1)

    session = db.SessionLocal()
    transaction = session.begin()
    try:
        connection = session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=account_id
        ).one()
        connection.authorization_required_at = pending_mark
        connection.updated_at = pending_mark

        status = calendar_connections.get_google_calendar_connection_status(
            session,
            account_id=account_id,
        )

        assert status.state == "authorization_required"
        assert status.authorization_required_at == pending_mark
        assert status.updated_at == pending_mark
        assert connection in session.dirty
        transaction.rollback()
    finally:
        session.close()

    persisted = _stored_envelope(account_id)
    assert persisted["authorization_required_at"] is None
    assert persisted["updated_at"] == T0
    assert _stored_revision(account_id) == 1


@pytest.mark.parametrize("transition", ["disconnect", "replace"])
def test_status_read_rejects_cached_connection_after_competing_lifecycle(
    migrated_postgres_db,
    transition,
):
    account_id, _ = _demo_owner()
    _replace(account_id, token="cached-status-original", now=T0)
    changed_at = T0 + datetime.timedelta(minutes=1)

    session = db.SessionLocal()
    try:
        cached = session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=account_id
        ).one()

        if transition == "disconnect":
            with db.SessionLocal.begin() as competing_session:
                calendar_connections.disconnect_google_calendar(
                    competing_session,
                    account_id=account_id,
                )
        else:
            _replace(
                account_id,
                token="cached-status-replacement",
                now=changed_at,
            )

        status = calendar_connections.get_google_calendar_connection_status(
            session,
            account_id=account_id,
        )

        assert cached in session.identity_map.values()
        if transition == "disconnect":
            assert status == CalendarConnectionStatus(
                provider="google",
                state="disconnected",
                connected_at=None,
                updated_at=None,
                authorization_required_at=None,
            )
        else:
            assert status.state == "connected"
            assert status.connected_at == changed_at
            assert status.updated_at == changed_at
            assert status.authorization_required_at is None
    finally:
        session.close()


@pytest.mark.parametrize("unrelated_dirty", [False, True])
def test_status_read_rejects_non_lifecycle_cache_after_competing_recovery_mark(
    migrated_postgres_db,
    unrelated_dirty,
):
    account_id, _ = _demo_owner()
    _replace(account_id, token="cached-status-recovery", now=T0)
    marked_at = T0 + datetime.timedelta(minutes=1)

    session = db.SessionLocal()
    try:
        cached = session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=account_id
        ).one()
        if unrelated_dirty:
            cached.credential_key_id = "unrelated-local-dirty-key"
        with db.SessionLocal.begin() as competing_session:
            calendar_connections.mark_google_calendar_authorization_required(
                competing_session,
                account_id=account_id,
                expected_connection_id=cached.id,
                now=marked_at,
            )

        status = calendar_connections.get_google_calendar_connection_status(
            session,
            account_id=account_id,
        )

        assert (cached in session.dirty) is unrelated_dirty
        assert status.state == "authorization_required"
        assert status.connected_at == T0
        assert status.updated_at == marked_at
        assert status.authorization_required_at == marked_at
    finally:
        session.close()


def test_status_read_does_not_refresh_expired_credential_columns(
    migrated_postgres_db,
):
    account_id, _ = _demo_owner()
    _replace(account_id, token="expired-status-cache", now=T0)

    session = db.SessionLocal()
    statements = []

    def capture_connection_query(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        if "provider_calendar_connections" in statement:
            statements.append(statement)

    try:
        cached = session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=account_id
        ).one()
        session.expire(cached)
        event.listen(db.engine, "before_cursor_execute", capture_connection_query)
        try:
            status = calendar_connections.get_google_calendar_connection_status(
                session,
                account_id=account_id,
            )
        finally:
            event.remove(
                db.engine,
                "before_cursor_execute",
                capture_connection_query,
            )

        assert status.state == "connected"
        assert status.connected_at == T0
        assert len(statements) == 1
        assert "credential_" not in statements[0]
    finally:
        session.close()


def test_replace_persists_only_an_encrypted_owner_bound_envelope(
    migrated_postgres_db,
):
    token = "distinctive-repository-refresh-token"
    account_id, instructor_id = _demo_owner()
    assert _stored_revision(account_id) == 0

    status = _replace(account_id, token=token, now=T0)

    assert status.provider == "google"
    assert status.state == "connected"
    assert status.connected_at == T0
    assert status.updated_at == T0
    assert status.authorization_required_at is None
    assert token not in repr(status)
    assert KEY_ID not in repr(status)
    assert _stored_revision(account_id) == 1

    with db.SessionLocal() as session:
        row = session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=account_id
        ).one()
        assert row.id.startswith("calconn_")
        assert row.credential_key_id == KEY_ID
        assert token.encode() != row.credential_ciphertext
        assert token.encode() not in row.credential_ciphertext
        envelope = _envelope(row)
        scope = CalendarCredentialScope(
            instructor_id=instructor_id,
            connection_id=row.id,
            provider="google",
        )
        assert CIPHER.decrypt_refresh_token(envelope, scope=scope) == token


def test_locked_replace_refreshes_cached_owner_before_building_aad(
    migrated_postgres_db,
):
    account_id, original_instructor_id = _demo_owner()
    replacement_instructor_id = _add_instructor("authoritative-owner")
    token = "owner-refresh-regression-token"

    with db.SessionLocal() as session:
        with session.begin():
            cached = session.get(ProviderAccountDB, account_id)
            assert cached.instructor_id == original_instructor_id

            with db.SessionLocal.begin() as other_session:
                other_session.query(ProviderAccountDB).filter_by(
                    id=account_id
                ).update({"instructor_id": replacement_instructor_id})

            calendar_connections.replace_google_calendar_connection(
                session,
                account_id=account_id,
                refresh_token=token,
                cipher=CIPHER,
                now=T0,
            )

    with db.SessionLocal() as session:
        row = session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=account_id
        ).one()
        envelope = _envelope(row)
        assert CIPHER.decrypt_refresh_token(
            envelope,
            scope=CalendarCredentialScope(
                instructor_id=replacement_instructor_id,
                connection_id=row.id,
                provider="google",
            ),
        ) == token
        with pytest.raises(CalendarCredentialDecryptionError):
            CIPHER.decrypt_refresh_token(
                envelope,
                scope=CalendarCredentialScope(
                    instructor_id=original_instructor_id,
                    connection_id=row.id,
                    provider="google",
                ),
            )

    assert _stored_revision(account_id) == 1


def test_locked_replace_refreshes_a_cached_connection_after_competing_update(
    migrated_postgres_db,
):
    account_id, _ = _demo_owner()
    _replace(account_id, token="cached-connection", now=T0)
    original = _stored_envelope(account_id)
    _issue_pending_oauth_state(account_id, "stale-timestamp")
    original_oauth_rows = _stored_oauth_rows(account_id)
    marked_at = T0 + datetime.timedelta(minutes=2)

    session = db.SessionLocal()
    transaction = session.begin()
    try:
        cached = session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=account_id
        ).one()
        assert cached.id == original["id"]
        assert cached.authorization_required_at is None

        with db.SessionLocal.begin() as other_session:
            marked = (
                calendar_connections.mark_google_calendar_authorization_required(
                    other_session,
                    account_id=account_id,
                    expected_connection_id=original["id"],
                    now=marked_at,
                )
            )
        assert marked.authorization_required_at == marked_at

        with pytest.raises(
            calendar_connections.CalendarConnectionInputError
        ):
            calendar_connections.replace_google_calendar_connection(
                session,
                account_id=account_id,
                refresh_token="must-not-replace-newer-state",
                cipher=CIPHER,
                now=T0 + datetime.timedelta(minutes=1),
            )
        transaction.rollback()
    finally:
        session.close()

    final = _stored_envelope(account_id)
    assert final["id"] == original["id"]
    assert final["nonce"] == original["nonce"]
    assert final["ciphertext"] == original["ciphertext"]
    assert final["authorization_required_at"] == marked_at
    assert _stored_revision(account_id) == 1
    assert _stored_oauth_rows(account_id) == original_oauth_rows


def test_locked_recovery_mark_refreshes_a_cached_competing_update(
    migrated_postgres_db,
):
    account_id, _ = _demo_owner()
    _replace(account_id, token="cached-recovery-mark", now=T0)
    connection_id = _stored_envelope(account_id)["id"]
    first_marked_at = T0 + datetime.timedelta(minutes=1)

    session = db.SessionLocal()
    transaction = session.begin()
    try:
        cached = session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=account_id
        ).one()
        assert cached.authorization_required_at is None

        with db.SessionLocal.begin() as other_session:
            first = (
                calendar_connections.mark_google_calendar_authorization_required(
                    other_session,
                    account_id=account_id,
                    expected_connection_id=connection_id,
                    now=first_marked_at,
                )
            )
        assert first.authorization_required_at == first_marked_at

        repeated = (
            calendar_connections.mark_google_calendar_authorization_required(
                session,
                account_id=account_id,
                expected_connection_id=connection_id,
                now=T0 + datetime.timedelta(minutes=2),
            )
        )
        assert repeated.authorization_required_at == first_marked_at
        assert repeated.updated_at == first_marked_at
        transaction.rollback()
    finally:
        session.close()

    final = _stored_envelope(account_id)
    assert final["id"] == connection_id
    assert final["authorization_required_at"] == first_marked_at
    assert final["updated_at"] == first_marked_at
    assert _stored_revision(account_id) == 1


def test_replacement_rotates_identity_nonce_and_clears_recovery(
    migrated_postgres_db,
):
    account_id, _ = _demo_owner()
    _replace(account_id, token="same-token", now=T0)
    old = _stored_envelope(account_id)
    assert _stored_revision(account_id) == 1

    with db.SessionLocal.begin() as session:
        marked = calendar_connections.mark_google_calendar_authorization_required(
            session,
            account_id=account_id,
            expected_connection_id=old["id"],
            now=T0 + datetime.timedelta(minutes=1),
        )
    assert marked.state == "authorization_required"
    assert _stored_revision(account_id) == 1

    replacement = _replace(
        account_id,
        token="same-token",
        now=T0 + datetime.timedelta(minutes=2),
    )
    new = _stored_envelope(account_id)

    assert replacement.state == "connected"
    assert replacement.authorization_required_at is None
    assert new["id"] != old["id"]
    assert new["nonce"] != old["nonce"]
    assert new["ciphertext"] != old["ciphertext"]
    assert new["authorization_required_at"] is None
    assert _stored_revision(account_id) == 2
    with db.SessionLocal() as session:
        assert session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=account_id
        ).count() == 1


def test_old_and_cross_tenant_nonce_collisions_fail_before_mutation(
    migrated_postgres_db,
    monkeypatch,
    caplog,
):
    first_id, _ = _demo_owner()
    second_id, _ = _add_owner("nonce")
    _replace(first_id, token="first-old", now=T0)
    _replace(second_id, token="second-token", now=T0)
    first_old = _stored_envelope(first_id)
    second_old = _stored_envelope(second_id)
    _issue_pending_oauth_state(first_id, "nonce-collision")
    original_oauth_rows = _stored_oauth_rows(first_id)

    class FixedUuid:
        hex = "a" * 32

    monkeypatch.setattr(calendar_connections, "uuid4", lambda: FixedUuid())
    monkeypatch.setattr(
        calendar_credentials.os,
        "urandom",
        lambda size: first_old["nonce"],
    )

    with pytest.raises(CalendarConnectionPersistenceError) as own_collision:
        _replace(
            first_id,
            token="first-new",
            now=T0 + datetime.timedelta(minutes=1),
        )
    _assert_error_safe(
        own_collision.value,
        "first-new",
        first_id,
        KEY_ID,
        repr(first_old["nonce"]),
        first_old["nonce"].hex(),
    )
    assert _stored_envelope(first_id) == first_old
    assert _stored_envelope(second_id) == second_old
    assert _stored_revision(first_id) == 1
    assert _stored_revision(second_id) == 1
    assert _stored_oauth_rows(first_id) == original_oauth_rows

    monkeypatch.setattr(
        calendar_credentials.os,
        "urandom",
        lambda size: second_old["nonce"],
    )
    with pytest.raises(CalendarConnectionPersistenceError) as tenant_collision:
        _replace(
            first_id,
            token="first-new",
            now=T0 + datetime.timedelta(minutes=1),
        )
    _assert_error_safe(
        tenant_collision.value,
        "first-new",
        first_id,
        second_id,
        KEY_ID,
        repr(second_old["nonce"]),
        second_old["nonce"].hex(),
    )
    assert _stored_envelope(first_id) == first_old
    assert _stored_envelope(second_id) == second_old
    assert _stored_revision(first_id) == 1
    assert _stored_revision(second_id) == 1
    assert _stored_oauth_rows(first_id) == original_oauth_rows
    _assert_log_safe(
        caplog.text,
        "first-new",
        first_id,
        second_id,
        KEY_ID,
        first_old["nonce"].hex(),
        second_old["nonce"].hex(),
    )


def test_cross_account_nonce_race_uses_unique_index_and_restores_loser(
    migrated_postgres_db,
    monkeypatch,
):
    first_id, _ = _demo_owner()
    second_id, _ = _add_owner("nonce-race")
    _replace(first_id, token="first-before-race", now=T0)
    _replace(second_id, token="second-before-race", now=T0)
    originals = {
        first_id: _stored_envelope(first_id),
        second_id: _stored_envelope(second_id),
    }
    shared_nonce = b"n" * 12
    id_lock = threading.Lock()
    generated_ids = iter(["a" * 32, "b" * 32])

    class GeneratedUuid:
        def __init__(self, value: str):
            self.hex = value

    def next_uuid():
        with id_lock:
            return GeneratedUuid(next(generated_ids))

    monkeypatch.setattr(calendar_connections, "uuid4", next_uuid)
    monkeypatch.setattr(
        calendar_credentials.os,
        "urandom",
        lambda size: shared_nonce,
    )
    preflight_barrier = threading.Barrier(2)
    original_preflight = calendar_connections._unused_envelope

    def synchronized_preflight(*args, **kwargs):
        envelope = original_preflight(*args, **kwargs)
        preflight_barrier.wait(timeout=5)
        return envelope

    monkeypatch.setattr(
        calendar_connections,
        "_unused_envelope",
        synchronized_preflight,
    )

    def replace(account_id: str):
        try:
            with db.SessionLocal.begin() as session:
                calendar_connections.replace_google_calendar_connection(
                    session,
                    account_id=account_id,
                    refresh_token=f"race-token-for-{account_id}",
                    cipher=CIPHER,
                    now=T0 + datetime.timedelta(minutes=1),
                )
            return account_id, "connected"
        except CalendarConnectionPersistenceError as exc:
            _assert_error_safe(exc, account_id, f"race-token-for-{account_id}")
            return account_id, "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result(timeout=10)
            for future in [
                executor.submit(replace, first_id),
                executor.submit(replace, second_id),
            ]
        ]

    assert sorted(outcome for _, outcome in outcomes) == [
        "conflict",
        "connected",
    ]
    loser_id = next(
        account_id for account_id, outcome in outcomes if outcome == "conflict"
    )
    winner_id = next(
        account_id for account_id, outcome in outcomes if outcome == "connected"
    )
    assert _stored_envelope(loser_id) == originals[loser_id]
    assert _stored_revision(loser_id) == 1
    winner = _stored_envelope(winner_id)
    assert winner["id"] != originals[winner_id]["id"]
    assert winner["nonce"] == shared_nonce
    assert _stored_revision(winner_id) == 2


def test_database_failure_preserves_old_connection_and_hides_secret(
    migrated_postgres_db,
    monkeypatch,
    caplog,
):
    account_id, _ = _demo_owner()
    token = "credential-that-must-survive"
    _replace(account_id, token=token, now=T0)
    original = _stored_envelope(account_id)
    original_revision = _stored_revision(account_id)
    _issue_pending_oauth_state(account_id, "replacement-database-failure")
    original_oauth_rows = _stored_oauth_rows(account_id)

    class InvalidUuid:
        hex = "g" * 32

    monkeypatch.setattr(calendar_connections, "uuid4", lambda: InvalidUuid())
    with pytest.raises(CalendarConnectionPersistenceError) as database_error:
        _replace(
            account_id,
            token="database-failure-secret",
            now=T0 + datetime.timedelta(minutes=1),
        )
    _assert_error_safe(database_error.value, "database-failure-secret", KEY_ID)
    _assert_log_safe(
        caplog.text,
        "database-failure-secret",
        account_id,
        KEY_ID,
    )
    assert _stored_envelope(account_id) == original
    assert _stored_revision(account_id) == original_revision
    assert _stored_oauth_rows(account_id) == original_oauth_rows

    monkeypatch.undo()
    _replace(
        account_id,
        token="successful-after-rollback",
        now=T0 + datetime.timedelta(minutes=2),
    )
    assert _stored_envelope(account_id)["id"] != original["id"]
    assert _stored_revision(account_id) == original_revision + 1
    assert _stored_oauth_rows(
        account_id,
        tracked_digests=original_oauth_rows["states"],
    ) == {"states": set(), "verifiers": set()}


def test_encryption_failure_and_caller_rollback_preserve_prior_connection(
    migrated_postgres_db,
):
    account_id, _ = _demo_owner()
    _replace(account_id, token="original-token", now=T0)
    original = _stored_envelope(account_id)
    original_revision = _stored_revision(account_id)

    with pytest.raises(CalendarCredentialInputError):
        _replace(
            account_id,
            token="   ",
            now=T0 + datetime.timedelta(minutes=1),
        )
    assert _stored_envelope(account_id) == original
    assert _stored_revision(account_id) == original_revision

    session = db.SessionLocal()
    transaction = session.begin()
    try:
        calendar_connections.replace_google_calendar_connection(
            session,
            account_id=account_id,
            refresh_token="rolled-back-token",
            cipher=CIPHER,
            now=T0 + datetime.timedelta(minutes=2),
        )
        transaction.rollback()
    finally:
        session.close()
    assert _stored_envelope(account_id) == original
    assert _stored_revision(account_id) == original_revision


def test_recovery_mark_is_idempotent_and_stale_mark_cannot_poison_replacement(
    migrated_postgres_db,
):
    account_id, _ = _demo_owner()
    _replace(account_id, token="old-token", now=T0)
    old_id = _stored_envelope(account_id)["id"]
    first_mark = T0 + datetime.timedelta(minutes=1)

    with db.SessionLocal.begin() as session:
        first = calendar_connections.mark_google_calendar_authorization_required(
            session,
            account_id=account_id,
            expected_connection_id=old_id,
            now=first_mark,
        )
    with db.SessionLocal.begin() as session:
        repeated = calendar_connections.mark_google_calendar_authorization_required(
            session,
            account_id=account_id,
            expected_connection_id=old_id,
            now=first_mark + datetime.timedelta(minutes=1),
        )
    assert first.authorization_required_at == first_mark
    assert repeated.authorization_required_at == first_mark
    assert repeated.updated_at == first_mark
    assert _stored_revision(account_id) == 1

    _replace(
        account_id,
        token="replacement-token",
        now=first_mark + datetime.timedelta(minutes=2),
    )
    replacement = _stored_envelope(account_id)
    with db.SessionLocal.begin() as session:
        stale = calendar_connections.mark_google_calendar_authorization_required(
            session,
            account_id=account_id,
            expected_connection_id=old_id,
            now=first_mark + datetime.timedelta(minutes=3),
        )
    assert stale.state == "connected"
    assert _stored_envelope(account_id) == replacement
    assert _stored_revision(account_id) == 2

    with db.SessionLocal.begin() as session:
        calendar_connections.disconnect_google_calendar(
            session,
            account_id=account_id,
        )
    with db.SessionLocal.begin() as session:
        disconnected = calendar_connections.mark_google_calendar_authorization_required(
            session,
            account_id=account_id,
            expected_connection_id=replacement["id"],
            now=first_mark + datetime.timedelta(minutes=4),
        )
    assert disconnected.state == "disconnected"
    assert _stored_revision(account_id) == 3


def test_disconnect_is_idempotent_and_tenant_scoped(migrated_postgres_db):
    first_id, _ = _demo_owner()
    second_id, _ = _add_owner("disconnect")
    _replace(first_id, token="first-token", now=T0)
    _replace(second_id, token="second-token", now=T0)
    second_before = _stored_envelope(second_id)

    for expected_revision in (2, 3):
        with db.SessionLocal.begin() as session:
            status = calendar_connections.disconnect_google_calendar(
                session,
                account_id=first_id,
            )
        assert status.state == "disconnected"
        assert _stored_revision(first_id) == expected_revision

    with db.SessionLocal() as session:
        assert session.get(ProviderAccountDB, first_id) is not None
        assert session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=first_id
        ).one_or_none() is None
    assert _stored_envelope(second_id) == second_before
    assert _stored_revision(second_id) == 1

    with db.SessionLocal.begin() as session:
        unchanged = calendar_connections.mark_google_calendar_authorization_required(
            session,
            account_id=first_id,
            expected_connection_id=second_before["id"],
            now=T0 + datetime.timedelta(minutes=1),
        )
    assert unchanged.state == "disconnected"
    assert _stored_envelope(second_id) == second_before
    assert _stored_revision(first_id) == 3
    assert _stored_revision(second_id) == 1


@pytest.mark.parametrize("operation", ["replace", "disconnect"])
def test_lifecycle_changes_cancel_all_pending_oauth_states_for_account(
    migrated_postgres_db,
    operation,
):
    account_id, _ = _demo_owner()
    other_account_id, _ = _add_owner(f"oauth-cancel-{operation}")
    _issue_pending_oauth_state(account_id, f"{operation}-first")
    _issue_pending_oauth_state(account_id, f"{operation}-second")
    _issue_pending_oauth_state(
        other_account_id,
        f"{operation}-other-tenant",
    )
    own_before = _stored_oauth_rows(account_id)
    other_before = _stored_oauth_rows(other_account_id)
    assert len(own_before["states"]) == 2
    assert own_before["verifiers"] == own_before["states"]
    assert len(other_before["states"]) == 1
    assert other_before["verifiers"] == other_before["states"]

    if operation == "replace":
        status = _replace(
            account_id,
            token="oauth-state-cancellation-replacement",
            now=T0 + datetime.timedelta(minutes=1),
        )
    else:
        with db.SessionLocal.begin() as session:
            status = calendar_connections.disconnect_google_calendar(
                session,
                account_id=account_id,
            )

    assert status.state == (
        "connected" if operation == "replace" else "disconnected"
    )
    assert _stored_oauth_rows(
        account_id,
        tracked_digests=own_before["states"],
    ) == {
        "states": set(),
        "verifiers": set(),
    }
    assert _stored_oauth_rows(other_account_id) == other_before
    assert _stored_revision(account_id) == 1
    assert _stored_revision(other_account_id) == 0


@pytest.mark.parametrize("operation", ["replace", "disconnect"])
def test_lifecycle_cancellation_detaches_cached_state_and_verifier(
    migrated_postgres_db,
    operation,
):
    account_id, _ = _demo_owner()
    issued = _issue_pending_oauth_state(account_id, f"cached-{operation}")
    state_digest = hashlib.sha256(
        issued.issued_state.raw_state.encode("ascii")
    ).digest()

    with db.SessionLocal.begin() as session:
        state = session.get(ProviderCalendarOAuthStateDB, state_digest)
        verifier = session.get(
            ProviderCalendarOAuthPKCEVerifierDB,
            state_digest,
        )
        assert state is not None
        assert verifier is not None
        assert sqlalchemy_inspect(state).persistent
        assert sqlalchemy_inspect(verifier).persistent

        if operation == "replace":
            calendar_connections.replace_google_calendar_connection(
                session,
                account_id=account_id,
                refresh_token="cached-state-replacement",
                cipher=CIPHER,
                now=T0 + datetime.timedelta(minutes=1),
            )
        else:
            calendar_connections.disconnect_google_calendar(
                session,
                account_id=account_id,
            )

        assert sqlalchemy_inspect(state).deleted
        assert sqlalchemy_inspect(verifier).deleted
        assert session.get(
            ProviderCalendarOAuthStateDB,
            state_digest,
        ) is None
        assert session.get(
            ProviderCalendarOAuthPKCEVerifierDB,
            state_digest,
        ) is None

        state.expires_at += datetime.timedelta(seconds=1)
        verifier.verifier_key_id = "detached-cached-key"
        session.flush()

    assert sqlalchemy_inspect(state).detached
    assert sqlalchemy_inspect(verifier).detached
    assert _stored_oauth_rows(
        account_id,
        tracked_digests={state_digest},
    ) == {"states": set(), "verifiers": set()}
    assert _stored_revision(account_id) == 1


@pytest.mark.parametrize("operation", ["replace", "disconnect"])
def test_lifecycle_cancellation_detaches_state_consumed_after_cache(
    migrated_postgres_db,
    operation,
):
    account_id, _ = _demo_owner()
    issued = _issue_pending_oauth_state(
        account_id,
        f"consumed-after-cache-{operation}",
    )
    state_digest = hashlib.sha256(
        issued.issued_state.raw_state.encode("ascii")
    ).digest()

    with db.SessionLocal.begin() as lifecycle_session:
        state = lifecycle_session.get(
            ProviderCalendarOAuthStateDB,
            state_digest,
        )
        verifier = lifecycle_session.get(
            ProviderCalendarOAuthPKCEVerifierDB,
            state_digest,
        )
        assert state is not None
        assert verifier is not None

        with db.SessionLocal.begin() as consumer_session:
            consumed = (
                consume_google_calendar_oauth_state_with_pkce_and_browser_binding(
                    consumer_session,
                    raw_state=issued.issued_state.raw_state,
                    raw_browser_binding=issued.raw_browser_binding,
                    pkce_cipher=OAUTH_PKCE_CIPHER,
                    now=T0 + datetime.timedelta(minutes=1),
                )
            )
        assert consumed.account_id == account_id
        assert sqlalchemy_inspect(state).persistent
        assert sqlalchemy_inspect(verifier).persistent

        if operation == "replace":
            calendar_connections.replace_google_calendar_connection(
                lifecycle_session,
                account_id=account_id,
                refresh_token="consumed-cached-state-replacement",
                cipher=CIPHER,
                now=T0 + datetime.timedelta(minutes=2),
            )
        else:
            calendar_connections.disconnect_google_calendar(
                lifecycle_session,
                account_id=account_id,
            )

        assert sqlalchemy_inspect(state).detached
        assert sqlalchemy_inspect(verifier).detached
        state.expires_at += datetime.timedelta(seconds=1)
        verifier.verifier_key_id = "detached-consumed-cached-key"
        lifecycle_session.flush()

    assert _stored_oauth_rows(
        account_id,
        tracked_digests={state_digest},
    ) == {"states": set(), "verifiers": set()}
    assert _stored_revision(account_id) == 1


@pytest.mark.parametrize("operation", ["replace", "disconnect"])
def test_lifecycle_cancellation_detaches_orphaned_cached_verifier(
    migrated_postgres_db,
    operation,
):
    account_id, _ = _demo_owner()
    other_account_id, _ = _add_owner(f"cached-verifier-other-{operation}")
    issued = _issue_pending_oauth_state(
        account_id,
        f"orphaned-cached-verifier-{operation}",
    )
    other_issued = _issue_pending_oauth_state(
        other_account_id,
        f"live-cross-account-verifier-{operation}",
    )
    state_digest = hashlib.sha256(
        issued.issued_state.raw_state.encode("ascii")
    ).digest()
    other_state_digest = hashlib.sha256(
        other_issued.issued_state.raw_state.encode("ascii")
    ).digest()

    with db.SessionLocal.begin() as lifecycle_session:
        verifier = lifecycle_session.get(
            ProviderCalendarOAuthPKCEVerifierDB,
            state_digest,
        )
        other_verifier = lifecycle_session.get(
            ProviderCalendarOAuthPKCEVerifierDB,
            other_state_digest,
        )
        assert verifier is not None
        assert other_verifier is not None
        assert "oauth_state" in sqlalchemy_inspect(verifier).unloaded
        assert "oauth_state" in sqlalchemy_inspect(other_verifier).unloaded

        with db.SessionLocal.begin() as consumer_session:
            consumed = (
                consume_google_calendar_oauth_state_with_pkce_and_browser_binding(
                    consumer_session,
                    raw_state=issued.issued_state.raw_state,
                    raw_browser_binding=issued.raw_browser_binding,
                    pkce_cipher=OAUTH_PKCE_CIPHER,
                    now=T0 + datetime.timedelta(minutes=1),
                )
            )
        assert consumed.account_id == account_id
        assert sqlalchemy_inspect(verifier).persistent

        if operation == "replace":
            calendar_connections.replace_google_calendar_connection(
                lifecycle_session,
                account_id=account_id,
                refresh_token="orphaned-cached-verifier-replacement",
                cipher=CIPHER,
                now=T0 + datetime.timedelta(minutes=2),
            )
        else:
            calendar_connections.disconnect_google_calendar(
                lifecycle_session,
                account_id=account_id,
            )

        assert sqlalchemy_inspect(verifier).detached
        assert sqlalchemy_inspect(other_verifier).persistent
        verifier.verifier_key_id = "detached-orphaned-cached-key"
        lifecycle_session.flush()

    assert _stored_oauth_rows(
        account_id,
        tracked_digests={state_digest},
    ) == {"states": set(), "verifiers": set()}
    assert _stored_oauth_rows(other_account_id) == {
        "states": {other_state_digest},
        "verifiers": {other_state_digest},
    }
    assert _stored_revision(account_id) == 1
    assert _stored_revision(other_account_id) == 0


@pytest.mark.parametrize("operation", ["replace", "disconnect"])
def test_lifecycle_cancellation_handles_expired_cache_after_consumption(
    migrated_postgres_db,
    operation,
):
    account_id, _ = _demo_owner()
    issued = _issue_pending_oauth_state(
        account_id,
        f"expired-cache-after-consumption-{operation}",
    )
    state_digest = hashlib.sha256(
        issued.issued_state.raw_state.encode("ascii")
    ).digest()

    lifecycle_session = db.SessionLocal()
    try:
        with lifecycle_session.begin():
            state = lifecycle_session.get(
                ProviderCalendarOAuthStateDB,
                state_digest,
            )
            verifier = lifecycle_session.get(
                ProviderCalendarOAuthPKCEVerifierDB,
                state_digest,
            )
            assert state is not None
            assert verifier is not None

        assert sqlalchemy_inspect(state).expired
        assert sqlalchemy_inspect(verifier).expired

        with db.SessionLocal.begin() as consumer_session:
            consumed = (
                consume_google_calendar_oauth_state_with_pkce_and_browser_binding(
                    consumer_session,
                    raw_state=issued.issued_state.raw_state,
                    raw_browser_binding=issued.raw_browser_binding,
                    pkce_cipher=OAUTH_PKCE_CIPHER,
                    now=T0 + datetime.timedelta(minutes=1),
                )
            )
        assert consumed.account_id == account_id

        with lifecycle_session.begin():
            if operation == "replace":
                calendar_connections.replace_google_calendar_connection(
                    lifecycle_session,
                    account_id=account_id,
                    refresh_token="expired-cached-state-replacement",
                    cipher=CIPHER,
                    now=T0 + datetime.timedelta(minutes=2),
                )
            else:
                calendar_connections.disconnect_google_calendar(
                    lifecycle_session,
                    account_id=account_id,
                )

            assert sqlalchemy_inspect(state).detached
            assert sqlalchemy_inspect(verifier).detached
            state.expires_at = T0 + datetime.timedelta(minutes=9)
            verifier.verifier_key_id = "detached-expired-cached-key"
            lifecycle_session.flush()
    finally:
        lifecycle_session.close()

    assert _stored_oauth_rows(
        account_id,
        tracked_digests={state_digest},
    ) == {"states": set(), "verifiers": set()}
    assert _stored_revision(account_id) == 1


@pytest.mark.parametrize("operation", ["replace", "disconnect"])
def test_lifecycle_cancellation_rolls_back_with_connection_and_revision(
    migrated_postgres_db,
    operation,
):
    account_id, _ = _demo_owner()
    _replace(account_id, token="before-oauth-cancellation-rollback", now=T0)
    original_connection = _stored_envelope(account_id)
    issued = _issue_pending_oauth_state(account_id, f"{operation}-rollback")
    state_digest = hashlib.sha256(
        issued.issued_state.raw_state.encode("ascii")
    ).digest()
    original_oauth_rows = _stored_oauth_rows(account_id)

    session = db.SessionLocal()
    transaction = session.begin()
    try:
        state = session.get(ProviderCalendarOAuthStateDB, state_digest)
        verifier = session.get(
            ProviderCalendarOAuthPKCEVerifierDB,
            state_digest,
        )
        assert state is not None
        assert verifier is not None
        if operation == "replace":
            status = calendar_connections.replace_google_calendar_connection(
                session,
                account_id=account_id,
                refresh_token="rolled-back-oauth-state-replacement",
                cipher=CIPHER,
                now=T0 + datetime.timedelta(minutes=1),
            )
        else:
            status = calendar_connections.disconnect_google_calendar(
                session,
                account_id=account_id,
            )
        assert status.state == (
            "connected" if operation == "replace" else "disconnected"
        )
        assert sqlalchemy_inspect(state).deleted
        assert sqlalchemy_inspect(verifier).deleted
        assert session.query(ProviderCalendarOAuthStateDB).filter_by(
            account_id=account_id
        ).count() == 0
        assert (
            session.query(ProviderCalendarOAuthPKCEVerifierDB)
            .join(
                ProviderCalendarOAuthStateDB,
                ProviderCalendarOAuthStateDB.state_digest
                == ProviderCalendarOAuthPKCEVerifierDB.state_digest,
            )
            .filter(ProviderCalendarOAuthStateDB.account_id == account_id)
            .count()
            == 0
        )
        assert session.get(
            ProviderAccountDB,
            account_id,
        ).calendar_connection_revision == 2
        transaction.rollback()
        assert sqlalchemy_inspect(state).persistent
        assert sqlalchemy_inspect(verifier).persistent
        assert session.get(
            ProviderCalendarOAuthStateDB,
            state_digest,
        ) is state
        assert session.get(
            ProviderCalendarOAuthPKCEVerifierDB,
            state_digest,
        ) is verifier
    finally:
        session.close()

    assert _stored_envelope(account_id) == original_connection
    assert _stored_revision(account_id) == 1
    assert _stored_oauth_rows(account_id) == original_oauth_rows


def test_replacement_preflight_failure_preserves_pending_oauth_state(
    migrated_postgres_db,
):
    account_id, _ = _demo_owner()
    _issue_pending_oauth_state(account_id, "preflight-failure")
    original_oauth_rows = _stored_oauth_rows(account_id)

    with pytest.raises(CalendarCredentialInputError):
        _replace(
            account_id,
            token="   ",
            now=T0 + datetime.timedelta(minutes=1),
        )

    assert _stored_oauth_rows(account_id) == original_oauth_rows
    assert _stored_revision(account_id) == 0
    assert _stored_envelope(account_id, required=False) is None


@pytest.mark.parametrize("pending_model", ["state", "verifier"])
def test_pending_oauth_mutation_is_rejected_before_lifecycle_lock(
    migrated_postgres_db,
    pending_model,
):
    account_id, _ = _demo_owner()
    _issue_pending_oauth_state(account_id, f"pending-{pending_model}")
    original_oauth_rows = _stored_oauth_rows(account_id)

    session = db.SessionLocal()
    transaction = session.begin()
    try:
        if pending_model == "state":
            value = session.query(ProviderCalendarOAuthStateDB).filter_by(
                account_id=account_id
            ).one()
            value.expires_at += datetime.timedelta(seconds=1)
        else:
            value = (
                session.query(ProviderCalendarOAuthPKCEVerifierDB)
                .join(
                    ProviderCalendarOAuthStateDB,
                    ProviderCalendarOAuthStateDB.state_digest
                    == ProviderCalendarOAuthPKCEVerifierDB.state_digest,
                )
                .filter(
                    ProviderCalendarOAuthStateDB.account_id == account_id
                )
                .one()
            )
            value.verifier_key_id = "pending-oauth-key"

        with pytest.raises(CalendarConnectionPersistenceError) as raised:
            calendar_connections.disconnect_google_calendar(
                session,
                account_id=account_id,
            )
        _assert_error_safe(raised.value, account_id, "pending-oauth-key")
        transaction.rollback()
    finally:
        session.close()

    assert _stored_oauth_rows(account_id) == original_oauth_rows
    assert _stored_revision(account_id) == 0


@pytest.mark.parametrize(
    ("first_operation", "lifecycle_operation"),
    [
        ("issue", "disconnect"),
        ("disconnect", "disconnect"),
        ("issue", "replace"),
        ("replace", "replace"),
    ],
)
def test_issuance_and_lifecycle_change_serialize_on_expected_account_lock(
    migrated_postgres_db,
    first_operation,
    lifecycle_operation,
):
    account_id, _ = _demo_owner()
    session_id = _add_active_provider_session(
        account_id,
        f"issuance-disconnect-{first_operation}",
    )
    first_ready = threading.Event()
    allow_first_commit = threading.Event()
    second_backend_ready = threading.Event()
    first_backend_pid = []
    second_backend_pid = []

    def run(operation: str, *, wait_before_commit: bool, report_second: bool):
        with db.SessionLocal.begin() as session:
            backend_pid = session.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            if report_second:
                second_backend_pid.append(backend_pid)
                second_backend_ready.set()
            else:
                first_backend_pid.append(backend_pid)

            if operation == "issue":
                result = (
                    issue_google_calendar_oauth_state_with_pkce_and_browser_binding(
                        session,
                        account_id=account_id,
                        session_id=session_id,
                        pkce_cipher=OAUTH_PKCE_CIPHER,
                        now=T0,
                    )
                )
            elif operation == "disconnect":
                result = calendar_connections.disconnect_google_calendar(
                    session,
                    account_id=account_id,
                )
            else:
                result = (
                    calendar_connections.replace_google_calendar_connection(
                        session,
                        account_id=account_id,
                        refresh_token="issuance-lifecycle-replacement",
                        cipher=CIPHER,
                        now=T0 + datetime.timedelta(minutes=1),
                    )
                )

            if wait_before_commit:
                first_ready.set()
                assert allow_first_commit.wait(timeout=5)
            return result

    second_operation = (
        lifecycle_operation if first_operation == "issue" else "issue"
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            run,
            first_operation,
            wait_before_commit=True,
            report_second=False,
        )
        assert first_ready.wait(timeout=5)
        second_future = executor.submit(
            run,
            second_operation,
            wait_before_commit=False,
            report_second=True,
        )
        assert second_backend_ready.wait(timeout=5)
        try:
            _wait_until_blocked_by(
                second_backend_pid[0],
                first_backend_pid[0],
            )
        finally:
            allow_first_commit.set()
        first_result = first_future.result(timeout=10)
        second_result = second_future.result(timeout=10)

    issued = first_result if first_operation == "issue" else second_result
    issued_digest = hashlib.sha256(
        issued.issued_state.raw_state.encode("ascii")
    ).digest()
    final_rows = _stored_oauth_rows(
        account_id,
        tracked_digests={issued_digest},
    )
    assert _stored_revision(account_id) == 1
    if first_operation == "issue":
        assert final_rows == {"states": set(), "verifiers": set()}
    else:
        assert len(final_rows["states"]) == 1
        assert final_rows["verifiers"] == final_rows["states"]


@pytest.mark.parametrize(
    ("first_operation", "lifecycle_operation"),
    [
        ("consume", "disconnect"),
        ("disconnect", "disconnect"),
        ("consume", "replace"),
        ("replace", "replace"),
    ],
)
def test_consumption_and_lifecycle_change_serialize_on_expected_account_lock(
    migrated_postgres_db,
    first_operation,
    lifecycle_operation,
):
    account_id, _ = _demo_owner()
    issued = _issue_pending_oauth_state(
        account_id,
        f"consume-lifecycle-{first_operation}-{lifecycle_operation}",
    )
    state_digest = hashlib.sha256(
        issued.issued_state.raw_state.encode("ascii")
    ).digest()
    first_ready = threading.Event()
    allow_first_commit = threading.Event()
    second_backend_ready = threading.Event()
    first_backend_pid = []
    second_backend_pid = []

    def run(operation: str, *, wait_before_commit: bool, report_second: bool):
        with db.SessionLocal.begin() as session:
            backend_pid = session.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            if report_second:
                second_backend_pid.append(backend_pid)
                second_backend_ready.set()
            else:
                first_backend_pid.append(backend_pid)

            if operation == "consume":
                try:
                    consume_google_calendar_oauth_state_with_pkce_and_browser_binding(
                        session,
                        raw_state=issued.issued_state.raw_state,
                        raw_browser_binding=issued.raw_browser_binding,
                        pkce_cipher=OAUTH_PKCE_CIPHER,
                        now=T0 + datetime.timedelta(minutes=1),
                    )
                    result = "consumed"
                except CalendarOAuthStateInvalid:
                    result = "invalid"
            elif operation == "disconnect":
                result = calendar_connections.disconnect_google_calendar(
                    session,
                    account_id=account_id,
                )
            else:
                result = (
                    calendar_connections.replace_google_calendar_connection(
                        session,
                        account_id=account_id,
                        refresh_token="consumption-lifecycle-replacement",
                        cipher=CIPHER,
                        now=T0 + datetime.timedelta(minutes=1),
                    )
                )

            if wait_before_commit:
                first_ready.set()
                assert allow_first_commit.wait(timeout=5)
            return result

    second_operation = (
        lifecycle_operation if first_operation == "consume" else "consume"
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            run,
            first_operation,
            wait_before_commit=True,
            report_second=False,
        )
        assert first_ready.wait(timeout=5)
        second_future = executor.submit(
            run,
            second_operation,
            wait_before_commit=False,
            report_second=True,
        )
        assert second_backend_ready.wait(timeout=5)
        try:
            _wait_until_blocked_by(
                second_backend_pid[0],
                first_backend_pid[0],
            )
        finally:
            allow_first_commit.set()
        first_result = first_future.result(timeout=10)
        second_result = second_future.result(timeout=10)

    consume_result = (
        first_result if first_operation == "consume" else second_result
    )
    assert consume_result == (
        "consumed" if first_operation == "consume" else "invalid"
    )
    assert _stored_oauth_rows(
        account_id,
        tracked_digests={state_digest},
    ) == {"states": set(), "verifiers": set()}
    assert _stored_revision(account_id) == 1
    assert (
        _stored_envelope(account_id, required=False) is not None
    ) is (lifecycle_operation == "replace")


def test_disconnect_rollback_restores_connected_and_disconnected_states(
    migrated_postgres_db,
):
    account_id, _ = _demo_owner()
    _replace(account_id, token="disconnect-rollback", now=T0)
    original = _stored_envelope(account_id)

    session = db.SessionLocal()
    transaction = session.begin()
    try:
        status = calendar_connections.disconnect_google_calendar(
            session,
            account_id=account_id,
        )
        assert status.state == "disconnected"
        assert session.get(
            ProviderAccountDB,
            account_id,
        ).calendar_connection_revision == 2
        transaction.rollback()
    finally:
        session.close()
    assert _stored_envelope(account_id) == original
    assert _stored_revision(account_id) == 1

    with db.SessionLocal.begin() as session:
        calendar_connections.disconnect_google_calendar(
            session,
            account_id=account_id,
        )
    assert _stored_envelope(account_id, required=False) is None
    assert _stored_revision(account_id) == 2

    session = db.SessionLocal()
    transaction = session.begin()
    try:
        status = calendar_connections.disconnect_google_calendar(
            session,
            account_id=account_id,
        )
        assert status.state == "disconnected"
        assert session.get(
            ProviderAccountDB,
            account_id,
        ).calendar_connection_revision == 3
        transaction.rollback()
    finally:
        session.close()
    assert _stored_envelope(account_id, required=False) is None
    assert _stored_revision(account_id) == 2


def test_disconnect_flush_failure_restores_connection_and_revision(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, _ = _demo_owner()
    _replace(account_id, token="disconnect-flush-failure", now=T0)
    original = _stored_envelope(account_id)
    _issue_pending_oauth_state(account_id, "disconnect-flush-failure")
    original_oauth_rows = _stored_oauth_rows(account_id)

    session = db.SessionLocal()
    transaction = session.begin()

    def fail_flush(*args, **kwargs):
        raise SQLAlchemyError("forced disconnect flush failure")

    monkeypatch.setattr(session, "flush", fail_flush)
    try:
        with pytest.raises(CalendarConnectionPersistenceError) as raised:
            calendar_connections.disconnect_google_calendar(
                session,
                account_id=account_id,
            )
        _assert_error_safe(raised.value, account_id)
        transaction.rollback()
    finally:
        session.close()

    assert _stored_envelope(account_id) == original
    assert _stored_revision(account_id) == 1
    assert _stored_oauth_rows(account_id) == original_oauth_rows


def test_last_available_revision_increment_succeeds_then_exhaustion_fails(
    migrated_postgres_db,
):
    account_id, _ = _demo_owner()
    _set_revision(account_id, POSTGRESQL_BIGINT_MAX - 1)

    with db.SessionLocal.begin() as session:
        status = calendar_connections.disconnect_google_calendar(
            session,
            account_id=account_id,
        )

    assert status.state == "disconnected"
    assert _stored_revision(account_id) == POSTGRESQL_BIGINT_MAX
    with pytest.raises(CalendarConnectionPersistenceError) as raised:
        with db.SessionLocal.begin() as session:
            calendar_connections.disconnect_google_calendar(
                session,
                account_id=account_id,
            )
    _assert_error_safe(raised.value, account_id)
    assert _stored_revision(account_id) == POSTGRESQL_BIGINT_MAX
    assert _stored_envelope(account_id, required=False) is None

    with db.SessionLocal() as session:
        status = calendar_connections.get_google_calendar_connection_status(
            session,
            account_id=account_id,
        )
    with db.SessionLocal.begin() as session:
        recovery = calendar_connections.mark_google_calendar_authorization_required(
            session,
            account_id=account_id,
            expected_connection_id="calconn_" + "1" * 32,
            now=T0,
        )
    assert status.state == "disconnected"
    assert recovery.state == "disconnected"
    assert _stored_revision(account_id) == POSTGRESQL_BIGINT_MAX


@pytest.mark.parametrize("operation", ["replace", "disconnect"])
def test_revision_exhaustion_preserves_existing_connection(
    migrated_postgres_db,
    operation,
):
    account_id, _ = _demo_owner()
    _replace(account_id, token="before-exhaustion", now=T0)
    original = _stored_envelope(account_id)
    _set_revision(account_id, POSTGRESQL_BIGINT_MAX)
    _issue_pending_oauth_state(account_id, f"exhaustion-{operation}")
    original_oauth_rows = _stored_oauth_rows(account_id)

    with pytest.raises(CalendarConnectionPersistenceError) as raised:
        if operation == "replace":
            _replace(
                account_id,
                token="exhaustion-secret",
                now=T0 + datetime.timedelta(minutes=1),
            )
        else:
            with db.SessionLocal.begin() as session:
                calendar_connections.disconnect_google_calendar(
                    session,
                    account_id=account_id,
                )

    _assert_error_safe(
        raised.value,
        account_id,
        "exhaustion-secret",
        str(POSTGRESQL_BIGINT_MAX),
    )
    assert _stored_revision(account_id) == POSTGRESQL_BIGINT_MAX
    assert _stored_envelope(account_id) == original
    assert _stored_oauth_rows(account_id) == original_oauth_rows


def test_commands_require_a_caller_owned_transaction(migrated_postgres_db):
    account_id, _ = _demo_owner()
    session = db.SessionLocal()
    try:
        with pytest.raises(CalendarConnectionTransactionRequired):
            calendar_connections.replace_google_calendar_connection(
                session,
                account_id=account_id,
                refresh_token="not-written",
                cipher=CIPHER,
            )
        with pytest.raises(CalendarConnectionTransactionRequired):
            calendar_connections.mark_google_calendar_authorization_required(
                session,
                account_id=account_id,
                expected_connection_id="calconn_" + "1" * 32,
            )
        with pytest.raises(CalendarConnectionTransactionRequired):
            calendar_connections.disconnect_google_calendar(
                session,
                account_id=account_id,
            )

        status = calendar_connections.get_google_calendar_connection_status(
            session,
            account_id=account_id,
        )
        assert status.state == "disconnected"
        with pytest.raises(CalendarConnectionTransactionRequired):
            calendar_connections.disconnect_google_calendar(
                session,
                account_id=account_id,
            )
        session.rollback()
    finally:
        session.close()
    assert _stored_envelope(account_id, required=False) is None
    assert _stored_revision(account_id) == 0


@pytest.mark.parametrize(
    "split_model",
    [
        ProviderCalendarConnectionDB,
        ProviderCalendarOAuthStateDB,
        ProviderCalendarOAuthPKCEVerifierDB,
    ],
)
def test_lifecycle_commands_reject_split_binds_before_sql(split_model):
    primary_engine = create_engine("sqlite+pysqlite:///:memory:")
    split_engine = create_engine("sqlite+pysqlite:///:memory:")
    session = Session(
        bind=primary_engine,
        binds={split_model: split_engine},
    )
    try:
        with session.begin():
            with pytest.raises(CalendarConnectionPersistenceError) as raised:
                calendar_connections.disconnect_google_calendar(
                    session,
                    account_id="acct_split_bind_private",
                )
        _assert_error_safe(
            raised.value,
            "acct_split_bind_private",
        )
    finally:
        session.close()
        primary_engine.dispose()
        split_engine.dispose()


def test_pending_lifecycle_mutation_is_rejected_before_command_lock(
    migrated_postgres_db,
):
    account_id, _ = _demo_owner()
    session = db.SessionLocal()
    transaction = session.begin()
    try:
        account = session.get(ProviderAccountDB, account_id)
        account.calendar_connection_revision = 8_765_432_109
        with pytest.raises(CalendarConnectionPersistenceError) as raised:
            calendar_connections.disconnect_google_calendar(
                session,
                account_id=account_id,
            )
        _assert_error_safe(raised.value, account_id, "8765432109")
        transaction.rollback()
    finally:
        session.close()

    assert _stored_revision(account_id) == 0
    assert _stored_envelope(account_id, required=False) is None


def test_unrelated_pending_write_flushes_after_provider_account_lock(
    migrated_postgres_db,
):
    account_id, _ = _demo_owner()
    statements = []

    def record_statement(
        connection,
        cursor,
        statement,
        parameters,
        context,
        executemany,
    ):
        statements.append(statement.lower())

    event.listen(db.engine, "before_cursor_execute", record_statement)
    try:
        with db.SessionLocal.begin() as session:
            session.add(
                InstructorDB(
                    id="inst_calendar_repository_pending-unrelated",
                    name="Pending Unrelated Instructor",
                    main_address="1 Pending Way",
                    lat=30.2,
                    lng=-97.2,
                    work_start_time="08:00",
                    work_end_time="17:00",
                    timezone="America/Chicago",
                )
            )
            status = calendar_connections.disconnect_google_calendar(
                session,
                account_id=account_id,
            )
    finally:
        event.remove(db.engine, "before_cursor_execute", record_statement)

    account_lock_index = next(
        index
        for index, statement in enumerate(statements)
        if "from provider_accounts" in statement
        and "for update" in statement
    )
    instructor_insert_index = next(
        index
        for index, statement in enumerate(statements)
        if statement.lstrip().startswith("insert into instructors")
    )
    assert status.state == "disconnected"
    assert account_lock_index < instructor_insert_index
    assert _stored_revision(account_id) == 1


def test_concurrent_replacements_serialize_to_one_valid_connection(
    migrated_postgres_db,
):
    account_id, instructor_id = _demo_owner()
    barrier = threading.Barrier(2)

    def replace(index: int) -> CalendarConnectionStatus:
        with db.SessionLocal.begin() as session:
            barrier.wait(timeout=5)
            return calendar_connections.replace_google_calendar_connection(
                session,
                account_id=account_id,
                refresh_token=f"concurrent-token-{index}",
                cipher=CIPHER,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            future.result(timeout=10)
            for future in [executor.submit(replace, 1), executor.submit(replace, 2)]
        ]

    assert [result.state for result in results] == ["connected", "connected"]
    with db.SessionLocal() as session:
        rows = session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=account_id
        ).all()
        assert len(rows) == 1
        row = rows[0]
        final_token = CIPHER.decrypt_refresh_token(
            _envelope(row),
            scope=CalendarCredentialScope(
                instructor_id=instructor_id,
                connection_id=row.id,
                provider="google",
            ),
        )
    assert final_token in {"concurrent-token-1", "concurrent-token-2"}
    assert _stored_revision(account_id) == 2


def test_replacement_and_stale_recovery_race_leaves_replacement_healthy(
    migrated_postgres_db,
):
    account_id, _ = _demo_owner()
    _replace(account_id, token="before-mixed-race", now=T0)
    original_id = _stored_envelope(account_id)["id"]
    barrier = threading.Barrier(2)

    def replace() -> CalendarConnectionStatus:
        with db.SessionLocal.begin() as session:
            barrier.wait(timeout=5)
            return calendar_connections.replace_google_calendar_connection(
                session,
                account_id=account_id,
                refresh_token="after-mixed-race",
                cipher=CIPHER,
            )

    def mark() -> CalendarConnectionStatus:
        with db.SessionLocal.begin() as session:
            barrier.wait(timeout=5)
            return calendar_connections.mark_google_calendar_authorization_required(
                session,
                account_id=account_id,
                expected_connection_id=original_id,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        replacement_future = executor.submit(replace)
        mark_future = executor.submit(mark)
        replacement_status = replacement_future.result(timeout=10)
        mark_status = mark_future.result(timeout=10)

    assert replacement_status.state == "connected"
    assert mark_status.state in {"connected", "authorization_required"}
    final = _stored_envelope(account_id)
    assert final["id"] != original_id
    assert final["authorization_required_at"] is None
    assert _stored_revision(account_id) == 2


@pytest.mark.parametrize("first_operation", ["replace", "disconnect"])
def test_replacement_and_disconnect_serialize_without_lost_revision(
    migrated_postgres_db,
    first_operation,
):
    account_id, _ = _demo_owner()
    _replace(account_id, token="before-mixed-command-race", now=T0)
    first_has_lock = threading.Event()
    allow_first_commit = threading.Event()
    second_backend_ready = threading.Event()
    second_backend_pid = []

    def run(
        operation: str,
        *,
        wait_before_commit: bool,
        report_backend: bool = False,
    ):
        with db.SessionLocal.begin() as session:
            if report_backend:
                second_backend_pid.append(
                    session.execute(text("SELECT pg_backend_pid()")).scalar_one()
                )
                second_backend_ready.set()
            if operation == "replace":
                status = calendar_connections.replace_google_calendar_connection(
                    session,
                    account_id=account_id,
                    refresh_token="mixed-command-replacement",
                    cipher=CIPHER,
                    now=T0 + datetime.timedelta(minutes=1),
                )
            else:
                status = calendar_connections.disconnect_google_calendar(
                    session,
                    account_id=account_id,
                )
            if wait_before_commit:
                first_has_lock.set()
                assert allow_first_commit.wait(timeout=5)
            return status

    second_operation = (
        "disconnect" if first_operation == "replace" else "replace"
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            run,
            first_operation,
            wait_before_commit=True,
        )
        assert first_has_lock.wait(timeout=5)
        second_future = executor.submit(
            run,
            second_operation,
            wait_before_commit=False,
            report_backend=True,
        )
        assert second_backend_ready.wait(timeout=5)
        _wait_until_database_blocked(second_backend_pid[0])
        allow_first_commit.set()
        first_status = first_future.result(timeout=10)
        second_status = second_future.result(timeout=10)

    assert first_status.state == (
        "connected" if first_operation == "replace" else "disconnected"
    )
    assert second_status.state == (
        "connected" if second_operation == "replace" else "disconnected"
    )
    assert _stored_revision(account_id) == 3
    final = _stored_envelope(account_id, required=False)
    assert (final is not None) is (second_operation == "replace")


def test_concurrent_disconnects_are_both_idempotent(migrated_postgres_db):
    account_id, _ = _demo_owner()
    _replace(account_id, token="disconnect-race", now=T0)
    barrier = threading.Barrier(2)

    def disconnect() -> CalendarConnectionStatus:
        with db.SessionLocal.begin() as session:
            barrier.wait(timeout=5)
            return calendar_connections.disconnect_google_calendar(
                session,
                account_id=account_id,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            future.result(timeout=10)
            for future in [executor.submit(disconnect), executor.submit(disconnect)]
        ]
    assert [result.state for result in results] == [
        "disconnected",
        "disconnected",
    ]
    assert _stored_envelope(account_id, required=False) is None
    assert _stored_revision(account_id) == 3


def _replace(
    account_id: str,
    *,
    token: str,
    now: datetime.datetime,
) -> CalendarConnectionStatus:
    with db.SessionLocal.begin() as session:
        return calendar_connections.replace_google_calendar_connection(
            session,
            account_id=account_id,
            refresh_token=token,
            cipher=CIPHER,
            now=now,
        )


def _demo_owner() -> tuple[str, str]:
    with db.SessionLocal() as session:
        account = session.query(ProviderAccountDB).filter_by(
            instructor_id="inst_001"
        ).one()
        return account.id, account.instructor_id


def _add_owner(suffix: str) -> tuple[str, str]:
    instructor_id = _add_instructor(suffix)
    account_id = f"acct_calendar_repository_{suffix}"
    with db.SessionLocal.begin() as session:
        session.add(
            ProviderAccountDB(
                id=account_id,
                instructor_id=instructor_id,
                email_normalized=f"calendar-repository-{suffix}@example.com",
                password_hash="not-used-by-repository-test",
                is_demo=False,
                session_version=1,
            )
        )
    return account_id, instructor_id


def _add_instructor(suffix: str) -> str:
    instructor_id = f"inst_calendar_repository_{suffix}"
    with db.SessionLocal.begin() as session:
        session.add(
            InstructorDB(
                id=instructor_id,
                name=f"Calendar Repository {suffix}",
                main_address="1 Repository Way",
                lat=30.2,
                lng=-97.2,
                work_start_time="08:00",
                work_end_time="17:00",
                timezone="America/Chicago",
            )
        )
    return instructor_id


def _add_active_provider_session(account_id: str, suffix: str) -> str:
    session_id = f"sess_calendar_repository_{suffix}"
    with db.SessionLocal.begin() as session:
        account = session.get(ProviderAccountDB, account_id)
        assert account is not None
        session.add(
            ProviderSessionDB(
                id=session_id,
                account_id=account_id,
                token_digest=hashlib.sha256(
                    f"calendar-repository-session-{suffix}".encode("ascii")
                ).digest(),
                session_version=account.session_version,
                created_at=T0 - datetime.timedelta(minutes=1),
                expires_at=T0 + datetime.timedelta(hours=1),
            )
        )
    return session_id


def _issue_pending_oauth_state(account_id: str, suffix: str):
    session_id = _add_active_provider_session(account_id, suffix)
    with db.SessionLocal.begin() as session:
        return issue_google_calendar_oauth_state_with_pkce_and_browser_binding(
            session,
            account_id=account_id,
            session_id=session_id,
            pkce_cipher=OAUTH_PKCE_CIPHER,
            now=T0,
        )


def _stored_oauth_rows(
    account_id: str,
    *,
    tracked_digests: set[bytes] | None = None,
) -> dict[str, set[bytes]]:
    with db.SessionLocal() as session:
        states = {
            bytes(value)
            for (value,) in session.query(
                ProviderCalendarOAuthStateDB.state_digest
            )
            .filter(
                ProviderCalendarOAuthStateDB.account_id == account_id
            )
            .all()
        }
        verifier_scope = states | set(tracked_digests or ())
        if verifier_scope:
            verifiers = {
                bytes(value)
                for (value,) in session.query(
                    ProviderCalendarOAuthPKCEVerifierDB.state_digest
                )
                .filter(
                    ProviderCalendarOAuthPKCEVerifierDB.state_digest.in_(
                        verifier_scope
                    )
                )
                .all()
            }
        else:
            verifiers = set()
    return {"states": states, "verifiers": verifiers}


def _stored_envelope(account_id: str, *, required: bool = True):
    with db.SessionLocal() as session:
        row = session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=account_id
        ).one_or_none()
        if row is None:
            assert not required
            return None
        return {
            "id": row.id,
            "key_id": row.credential_key_id,
            "nonce": bytes(row.credential_nonce),
            "ciphertext": bytes(row.credential_ciphertext),
            "authorization_required_at": row.authorization_required_at,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }


def _stored_revision(account_id: str) -> int:
    with db.SessionLocal() as session:
        account = session.get(ProviderAccountDB, account_id)
        assert account is not None
        return account.calendar_connection_revision


def _set_revision(account_id: str, revision: int) -> None:
    with db.SessionLocal.begin() as session:
        account = session.get(ProviderAccountDB, account_id)
        assert account is not None
        account.calendar_connection_revision = revision


def _wait_until_database_blocked(backend_pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with db.engine.connect() as connection:
            blocked = connection.execute(
                text(
                    "SELECT cardinality(pg_blocking_pids(:backend_pid)) > 0"
                ),
                {"backend_pid": backend_pid},
            ).scalar_one()
        if blocked:
            return
        time.sleep(0.01)
    raise AssertionError("The competing database operation did not block.")


def _wait_until_blocked_by(backend_pid: int, blocker_pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with db.engine.connect() as connection:
            blocked = connection.execute(
                text(
                    "SELECT :blocker_pid = ANY("
                    "pg_blocking_pids(:backend_pid)"
                    ")"
                ),
                {
                    "backend_pid": backend_pid,
                    "blocker_pid": blocker_pid,
                },
            ).scalar_one()
        if blocked:
            return
        time.sleep(0.01)
    raise AssertionError("The expected database operation did not block.")


def _envelope(row: ProviderCalendarConnectionDB) -> EncryptedCalendarCredential:
    return EncryptedCalendarCredential(
        version=row.credential_version,
        key_id=row.credential_key_id,
        nonce=row.credential_nonce,
        ciphertext=row.credential_ciphertext,
    )


def _assert_error_safe(error: Exception, *forbidden: str) -> None:
    assert error.__context__ is None
    assert error.__cause__ is None
    rendered = "".join(traceback.format_exception(error))
    for value in forbidden:
        assert value not in str(error)
        assert value not in repr(error)
        assert value not in rendered


def _assert_log_safe(rendered: str, *forbidden: str) -> None:
    for value in forbidden:
        assert value not in rendered
