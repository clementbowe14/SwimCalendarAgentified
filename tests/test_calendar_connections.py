from __future__ import annotations

import datetime
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields

import pytest

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
from app.models import (
    InstructorDB,
    ProviderAccountDB,
    ProviderCalendarConnectionDB,
)


UTC = datetime.timezone.utc
KEY_ID = "repository-key-2026"
CIPHER = CalendarCredentialCipher(
    active_key_id=KEY_ID,
    keys={KEY_ID: b"r" * 32},
)
T0 = datetime.datetime(2026, 11, 12, 14, 0, tzinfo=UTC)


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


def test_replace_persists_only_an_encrypted_owner_bound_envelope(
    migrated_postgres_db,
):
    token = "distinctive-repository-refresh-token"
    account_id, instructor_id = _demo_owner()

    status = _replace(account_id, token=token, now=T0)

    assert status.provider == "google"
    assert status.state == "connected"
    assert status.connected_at == T0
    assert status.updated_at == T0
    assert status.authorization_required_at is None
    assert token not in repr(status)
    assert KEY_ID not in repr(status)

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


def test_replacement_rotates_identity_nonce_and_clears_recovery(
    migrated_postgres_db,
):
    account_id, _ = _demo_owner()
    _replace(account_id, token="same-token", now=T0)
    old = _stored_envelope(account_id)

    with db.SessionLocal.begin() as session:
        marked = calendar_connections.mark_google_calendar_authorization_required(
            session,
            account_id=account_id,
            expected_connection_id=old["id"],
            now=T0 + datetime.timedelta(minutes=1),
        )
    assert marked.state == "authorization_required"

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
    winner = _stored_envelope(winner_id)
    assert winner["id"] != originals[winner_id]["id"]
    assert winner["nonce"] == shared_nonce


def test_database_failure_preserves_old_connection_and_hides_secret(
    migrated_postgres_db,
    monkeypatch,
    caplog,
):
    account_id, _ = _demo_owner()
    token = "credential-that-must-survive"
    _replace(account_id, token=token, now=T0)
    original = _stored_envelope(account_id)

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

    monkeypatch.undo()
    _replace(
        account_id,
        token="successful-after-rollback",
        now=T0 + datetime.timedelta(minutes=2),
    )
    assert _stored_envelope(account_id)["id"] != original["id"]


def test_encryption_failure_and_caller_rollback_preserve_prior_connection(
    migrated_postgres_db,
):
    account_id, _ = _demo_owner()
    _replace(account_id, token="original-token", now=T0)
    original = _stored_envelope(account_id)

    with pytest.raises(CalendarCredentialInputError):
        _replace(
            account_id,
            token="   ",
            now=T0 + datetime.timedelta(minutes=1),
        )
    assert _stored_envelope(account_id) == original

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


def test_disconnect_is_idempotent_and_tenant_scoped(migrated_postgres_db):
    first_id, _ = _demo_owner()
    second_id, _ = _add_owner("disconnect")
    _replace(first_id, token="first-token", now=T0)
    _replace(second_id, token="second-token", now=T0)
    second_before = _stored_envelope(second_id)

    for _ in range(2):
        with db.SessionLocal.begin() as session:
            status = calendar_connections.disconnect_google_calendar(
                session,
                account_id=first_id,
            )
        assert status.state == "disconnected"

    with db.SessionLocal() as session:
        assert session.get(ProviderAccountDB, first_id) is not None
        assert session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=first_id
        ).one_or_none() is None
    assert _stored_envelope(second_id) == second_before

    with db.SessionLocal.begin() as session:
        unchanged = calendar_connections.mark_google_calendar_authorization_required(
            session,
            account_id=first_id,
            expected_connection_id=second_before["id"],
            now=T0 + datetime.timedelta(minutes=1),
        )
    assert unchanged.state == "disconnected"
    assert _stored_envelope(second_id) == second_before


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
