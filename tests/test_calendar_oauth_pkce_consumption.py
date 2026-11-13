from __future__ import annotations

import dataclasses
import datetime
import hashlib
import inspect
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, text

from app import calendar_oauth_states, db
from app.calendar_oauth_pkce import (
    CalendarOAuthPKCECipher,
    derive_s256_code_challenge,
)
from app.calendar_oauth_states import (
    CalendarOAuthStateInputError,
    CalendarOAuthStateInvalid,
    CalendarOAuthStatePersistenceError,
    CalendarOAuthStateTransactionRequired,
    ConsumedGoogleCalendarOAuthPKCEState,
)
from app.models import (
    InstructorDB,
    ProviderAccountDB,
    ProviderCalendarOAuthPKCEVerifierDB,
    ProviderCalendarOAuthStateDB,
    ProviderSessionDB,
)


UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 11, 13, 14, 0, tzinfo=UTC)
TOKEN_A = "A" * 43
TOKEN_B = "B" * 43
OLD_KEY_ID = "pkce-consume-old"
NEW_KEY_ID = "pkce-consume-new"
OLD_ROOT_KEY = b"o" * 32
NEW_ROOT_KEY = b"n" * 32
OLD_CIPHER = CalendarOAuthPKCECipher(
    active_key_id=OLD_KEY_ID,
    root_keys={OLD_KEY_ID: OLD_ROOT_KEY},
)


def test_public_contract_requires_explicit_transaction_and_safe_inputs(
    migrated_postgres_db,
):
    account_id, session_id = _active_demo_owner("contract")

    assert set(
        inspect.signature(
            calendar_oauth_states.consume_google_calendar_oauth_state_with_pkce
        ).parameters
    ) == {"session", "raw_state", "pkce_cipher", "now"}
    assert [
        field.name
        for field in dataclasses.fields(ConsumedGoogleCalendarOAuthPKCEState)
    ] == ["account_id", "session_id", "code_verifier"]
    result = ConsumedGoogleCalendarOAuthPKCEState(
        account_id=account_id,
        session_id=session_id,
        code_verifier="v" * 43,
    )
    assert repr(result) == "ConsumedGoogleCalendarOAuthPKCEState(<redacted>)"
    assert account_id not in repr(result)
    assert session_id not in str(result)
    assert result.code_verifier not in repr(result)

    session = db.SessionLocal()
    try:
        with pytest.raises(CalendarOAuthStateTransactionRequired):
            calendar_oauth_states.consume_google_calendar_oauth_state_with_pkce(
                session,
                raw_state=TOKEN_A,
                pkce_cipher=OLD_CIPHER,
                now=T0,
            )

        session.get(ProviderAccountDB, account_id)
        with pytest.raises(CalendarOAuthStateTransactionRequired):
            calendar_oauth_states.consume_google_calendar_oauth_state_with_pkce(
                session,
                raw_state=TOKEN_A,
                pkce_cipher=OLD_CIPHER,
                now=T0,
            )
        session.rollback()

        with session.begin():
            with pytest.raises(CalendarOAuthStateInputError):
                calendar_oauth_states.consume_google_calendar_oauth_state_with_pkce(
                    session,
                    raw_state=TOKEN_A,
                    pkce_cipher=object(),
                    now=T0,
                )
            with pytest.raises(CalendarOAuthStateInputError):
                calendar_oauth_states.consume_google_calendar_oauth_state_with_pkce(
                    session,
                    raw_state=TOKEN_A,
                    pkce_cipher=OLD_CIPHER,
                    now=T0.replace(tzinfo=None),
                )
            session.execute(text("SELECT 1"))
    finally:
        session.close()


def test_success_locks_decrypts_then_cascade_deletes_pair(
    migrated_postgres_db,
):
    account_id, session_id = _active_demo_owner("success")
    issued = _issue(account_id, session_id)
    before = _snapshot(account_id, session_id)
    statements = []

    def capture_statement(
        connection,
        cursor,
        statement,
        parameters,
        context,
        executemany,
    ):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", capture_statement)
    try:
        consumed = _consume(issued.raw_state)
    finally:
        event.remove(db.engine, "before_cursor_execute", capture_statement)

    assert consumed.account_id == account_id
    assert consumed.session_id == session_id
    assert derive_s256_code_challenge(consumed.code_verifier) == (
        issued.code_challenge
    )
    for rendered in (repr(consumed), str(consumed)):
        assert account_id not in rendered
        assert session_id not in rendered
        assert consumed.code_verifier not in rendered
        assert before["verifier_key_id"] not in rendered

    locking_selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
        and "FOR UPDATE" in statement.upper()
    ]
    assert any("provider_calendar_oauth_states" in sql for sql in locking_selects)
    assert any(
        "provider_calendar_oauth_states" in sql
        and "browser_binding_digest IS NULL" in sql
        for sql in locking_selects
    )
    assert any(
        "provider_calendar_oauth_pkce_verifiers" in sql
        for sql in locking_selects
    )
    state_deletes = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("DELETE")
        and "provider_calendar_oauth_states" in statement
    ]
    child_mutations = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(
            (
                "DELETE FROM PROVIDER_CALENDAR_OAUTH_PKCE_VERIFIERS",
                "UPDATE PROVIDER_CALENDAR_OAUTH_PKCE_VERIFIERS",
                "INSERT INTO PROVIDER_CALENDAR_OAUTH_PKCE_VERIFIERS",
            )
        )
    ]
    assert len(state_deletes) == 1
    assert "RETURNING" in state_deletes[0].upper()
    assert "browser_binding_digest IS NULL" in state_deletes[0]
    assert child_mutations == []
    assert _snapshot(account_id, session_id) is None

    with pytest.raises(CalendarOAuthStateInvalid):
        _consume(issued.raw_state)


def test_malformed_unknown_and_replayed_states_are_indistinguishable(
    migrated_postgres_db,
):
    account_id, session_id = _active_demo_owner("invalid")
    issued = _issue(account_id, session_id)
    _consume(issued.raw_state)

    rendered = []
    for raw_state in (
        issued.raw_state,
        TOKEN_A,
        "short",
        TOKEN_B + " ",
        "é" * 43,
        None,
        TOKEN_A.encode("ascii"),
    ):
        with pytest.raises(CalendarOAuthStateInvalid) as exc_info:
            _consume(raw_state)
        rendered.append(
            (type(exc_info.value), str(exc_info.value), repr(exc_info.value))
        )

    assert len(set(rendered)) == 1
    assert rendered[0][1] == "Calendar authorization state is invalid."


def test_bare_state_is_preserved_for_legacy_consumer(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, session_id = _active_demo_owner("bare")
    _store_bare_state(account_id, session_id, TOKEN_A)

    def fail_decrypt(*args, **kwargs):
        raise AssertionError("bare states must not reach PKCE decryption")

    monkeypatch.setattr(
        CalendarOAuthPKCECipher,
        "decrypt_google_calendar_verifier",
        fail_decrypt,
    )
    with pytest.raises(CalendarOAuthStateInvalid):
        _consume(TOKEN_A)

    assert _owner_row_counts(account_id, session_id) == (1, 0)
    with db.SessionLocal.begin() as session:
        consumed = calendar_oauth_states.consume_google_calendar_oauth_state(
            session,
            raw_state=TOKEN_A,
            now=T0,
        )
    assert consumed.session_id == session_id
    assert _owner_row_counts(account_id, session_id) == (0, 0)


def test_pkce_consume_rejects_browser_bound_pair_before_decryption(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, session_id = _active_demo_owner("bound-pkce-consume")
    issued = _issue(account_id, session_id)
    binding_digest = _binding_digest("pkce-consume")
    with db.SessionLocal.begin() as session:
        session.get(
            ProviderCalendarOAuthStateDB,
            _digest(issued.raw_state),
        ).browser_binding_digest = binding_digest
    original = _snapshot(account_id, session_id)
    decrypt_calls = []

    def fail_decrypt(*args, **kwargs):
        decrypt_calls.append((args, kwargs))
        raise AssertionError("bound states must not reach PKCE decryption")

    monkeypatch.setattr(
        CalendarOAuthPKCECipher,
        "decrypt_google_calendar_verifier",
        fail_decrypt,
    )

    with db.SessionLocal.begin() as session:
        with pytest.raises(CalendarOAuthStateInvalid):
            calendar_oauth_states.consume_google_calendar_oauth_state_with_pkce(
                session,
                raw_state=issued.raw_state,
                pkce_cipher=OLD_CIPHER,
                now=T0,
            )
        session.execute(text("SELECT 1"))

    assert decrypt_calls == []
    assert _snapshot(account_id, session_id) == original


def test_missing_verifier_child_is_invalid_and_parent_is_preserved(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, session_id = _active_demo_owner("missing-child")
    issued = _issue(account_id, session_id)
    with db.SessionLocal.begin() as session:
        session.query(ProviderCalendarOAuthPKCEVerifierDB).filter_by(
            state_digest=_digest(issued.raw_state)
        ).delete(synchronize_session=False)

    def fail_decrypt(*args, **kwargs):
        raise AssertionError("a missing child must not reach decryption")

    monkeypatch.setattr(
        CalendarOAuthPKCECipher,
        "decrypt_google_calendar_verifier",
        fail_decrypt,
    )
    with pytest.raises(CalendarOAuthStateInvalid):
        _consume(issued.raw_state)
    assert _owner_row_counts(account_id, session_id) == (1, 0)


@pytest.mark.parametrize(
    "case",
    [
        "disabled",
        "revoked",
        "session_expired",
        "session_future",
        "version_stale",
        "state_expired",
        "state_future",
    ],
)
def test_inactive_owner_and_state_time_fail_before_decryption(
    migrated_postgres_db,
    monkeypatch,
    case,
):
    account_id, session_id = _active_demo_owner(f"inactive-{case}")
    issued = _issue(account_id, session_id)
    with db.SessionLocal.begin() as session:
        account = session.get(ProviderAccountDB, account_id)
        provider_session = session.get(ProviderSessionDB, session_id)
        state = session.get(
            ProviderCalendarOAuthStateDB,
            _digest(issued.raw_state),
        )
        if case == "disabled":
            account.disabled_at = T0
        elif case == "revoked":
            provider_session.revoked_at = T0
        elif case == "session_expired":
            provider_session.expires_at = T0
        elif case == "session_future":
            provider_session.created_at = T0 + datetime.timedelta(seconds=1)
        elif case == "version_stale":
            account.session_version += 1
        elif case == "state_expired":
            state.created_at = T0 - datetime.timedelta(minutes=10)
            state.expires_at = T0
        elif case == "state_future":
            state.created_at = T0 + datetime.timedelta(seconds=1)
            state.expires_at = T0 + datetime.timedelta(minutes=1)

    def fail_decrypt(*args, **kwargs):
        raise AssertionError("invalid ownership or time must fail first")

    monkeypatch.setattr(
        CalendarOAuthPKCECipher,
        "decrypt_google_calendar_verifier",
        fail_decrypt,
    )
    with pytest.raises(CalendarOAuthStateInvalid) as exc_info:
        _consume(issued.raw_state)
    assert str(exc_info.value) == "Calendar authorization state is invalid."
    assert _owner_row_counts(account_id, session_id) == (1, 1)


@pytest.mark.parametrize(
    ("consumed_at", "valid"),
    [
        (T0 - datetime.timedelta(microseconds=1), True),
        (T0, False),
    ],
)
def test_exact_state_and_session_expiry_boundary(
    migrated_postgres_db,
    consumed_at,
    valid,
):
    account_id = _demo_account_id()
    session_id = _add_session(
        account_id,
        f"expiry-{valid}",
        created_at=T0 - datetime.timedelta(minutes=10),
        expires_at=T0,
    )
    issued = _issue(
        account_id,
        session_id,
        now=T0 - datetime.timedelta(minutes=10),
    )

    if valid:
        consumed = _consume(issued.raw_state, now=consumed_at)
        assert consumed.session_id == session_id
        assert _owner_row_counts(account_id, session_id) == (0, 0)
    else:
        with pytest.raises(CalendarOAuthStateInvalid):
            _consume(issued.raw_state, now=consumed_at)
        assert _owner_row_counts(account_id, session_id) == (1, 1)


@pytest.mark.parametrize(
    "failure",
    ["ciphertext", "nonce", "unknown_key", "wrong_root"],
)
def test_envelope_or_key_failure_is_recoverable_and_non_destructive(
    migrated_postgres_db,
    failure,
    caplog,
):
    account_id, session_id = _active_demo_owner(f"decrypt-{failure}")
    issued = _issue(account_id, session_id)
    state_digest = _digest(issued.raw_state)
    cipher = OLD_CIPHER

    if failure != "wrong_root":
        with db.SessionLocal.begin() as session:
            verifier = session.get(
                ProviderCalendarOAuthPKCEVerifierDB,
                state_digest,
            )
            if failure == "ciphertext":
                verifier.verifier_ciphertext = _flip_first_byte(
                    verifier.verifier_ciphertext
                )
            elif failure == "nonce":
                verifier.verifier_nonce = _flip_first_byte(
                    verifier.verifier_nonce
                )
            else:
                verifier.verifier_key_id = "pkce-consume-retired"
    else:
        cipher = CalendarOAuthPKCECipher(
            active_key_id=OLD_KEY_ID,
            root_keys={OLD_KEY_ID: b"w" * 32},
        )
    before = _snapshot(account_id, session_id)
    caplog.set_level("DEBUG")

    with pytest.raises(CalendarOAuthStatePersistenceError) as exc_info:
        _consume(issued.raw_state, cipher=cipher)

    assert str(exc_info.value) == (
        "Calendar authorization state could not be persisted."
    )
    _assert_error_safe(
        exc_info.value,
        issued.raw_state,
        state_digest.hex(),
        repr(state_digest),
        account_id,
        session_id,
        before["verifier_key_id"],
        before["verifier_nonce"].hex(),
        before["verifier_ciphertext"].hex(),
    )
    assert caplog.text == ""
    assert _snapshot(account_id, session_id) == before


def test_retained_key_rotation_recovers_after_early_key_removal(
    migrated_postgres_db,
):
    account_id, session_id = _active_demo_owner("rotation")
    issued = _issue(account_id, session_id)
    new_only_cipher = CalendarOAuthPKCECipher(
        active_key_id=NEW_KEY_ID,
        root_keys={NEW_KEY_ID: NEW_ROOT_KEY},
    )

    with pytest.raises(CalendarOAuthStatePersistenceError):
        _consume(issued.raw_state, cipher=new_only_cipher)
    assert _owner_row_counts(account_id, session_id) == (1, 1)

    rotated_cipher = CalendarOAuthPKCECipher(
        active_key_id=NEW_KEY_ID,
        root_keys={
            OLD_KEY_ID: OLD_ROOT_KEY,
            NEW_KEY_ID: NEW_ROOT_KEY,
        },
    )
    consumed = _consume(issued.raw_state, cipher=rotated_cipher)
    assert derive_s256_code_challenge(consumed.code_verifier) == (
        issued.code_challenge
    )
    assert _owner_row_counts(account_id, session_id) == (0, 0)


@pytest.mark.parametrize("target_kind", ["same_account", "other_account"])
def test_persisted_owner_transplant_fails_aad_and_preserves_pair(
    migrated_postgres_db,
    target_kind,
):
    source_account, source_session = _active_demo_owner(
        f"aad-source-{target_kind}"
    )
    issued = _issue(source_account, source_session)
    if target_kind == "same_account":
        target_account = source_account
        target_session = _add_session(
            target_account,
            "aad-target-same-account",
        )
    else:
        target_account, target_session = _add_owner("aad-target-other")

    with db.SessionLocal.begin() as session:
        state = session.get(
            ProviderCalendarOAuthStateDB,
            _digest(issued.raw_state),
        )
        state.account_id = target_account
        state.session_id = target_session
    before = _snapshot(target_account, target_session)

    with pytest.raises(CalendarOAuthStatePersistenceError):
        _consume(issued.raw_state)

    assert _snapshot(target_account, target_session) == before
    assert _owner_row_counts(source_account, source_session) == (0, 0)


def test_caller_rollback_restores_pair_and_verifier(
    migrated_postgres_db,
):
    account_id, session_id = _active_demo_owner("rollback")
    issued = _issue(account_id, session_id)
    before = _snapshot(account_id, session_id)

    session = db.SessionLocal()
    transaction = session.begin()
    try:
        first = calendar_oauth_states.consume_google_calendar_oauth_state_with_pkce(
            session,
            raw_state=issued.raw_state,
            pkce_cipher=OLD_CIPHER,
            now=T0,
        )
        assert session.get(
            ProviderCalendarOAuthStateDB,
            _digest(issued.raw_state),
        ) is None
        transaction.rollback()
    finally:
        session.close()

    assert _snapshot(account_id, session_id) == before
    second = _consume(issued.raw_state)
    assert second.code_verifier == first.code_verifier
    assert _owner_row_counts(account_id, session_id) == (0, 0)


def test_concurrent_consumers_commit_exactly_once(migrated_postgres_db):
    account_id, session_id = _active_demo_owner("concurrent")
    issued = _issue(account_id, session_id)
    barrier = threading.Barrier(2)

    def consume():
        try:
            with db.SessionLocal.begin() as session:
                barrier.wait(timeout=5)
                result = (
                    calendar_oauth_states
                    .consume_google_calendar_oauth_state_with_pkce(
                        session,
                        raw_state=issued.raw_state,
                        pkce_cipher=OLD_CIPHER,
                        now=T0,
                    )
                )
            return "consumed", result.code_verifier
        except CalendarOAuthStateInvalid:
            return "invalid", None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result(timeout=10)
            for future in [
                executor.submit(consume),
                executor.submit(consume),
            ]
        ]

    assert sorted(outcome for outcome, _ in outcomes) == [
        "consumed",
        "invalid",
    ]
    verifier = next(
        value for outcome, value in outcomes if outcome == "consumed"
    )
    assert derive_s256_code_challenge(verifier) == issued.code_challenge
    assert _owner_row_counts(account_id, session_id) == (0, 0)


@pytest.mark.parametrize("first_commits", [True, False])
def test_waiting_consumer_observes_first_commit_or_rollback(
    migrated_postgres_db,
    first_commits,
):
    account_id, session_id = _active_demo_owner(
        f"waiting-{first_commits}"
    )
    issued = _issue(account_id, session_id)
    first_consumed = threading.Event()
    release_first = threading.Event()
    second_pid_ready = threading.Event()
    second_pid = {}

    def first_consumer():
        session = db.SessionLocal()
        transaction = session.begin()
        try:
            result = (
                calendar_oauth_states
                .consume_google_calendar_oauth_state_with_pkce(
                    session,
                    raw_state=issued.raw_state,
                    pkce_cipher=OLD_CIPHER,
                    now=T0,
                )
            )
            first_consumed.set()
            assert release_first.wait(timeout=10)
            if first_commits:
                transaction.commit()
            else:
                transaction.rollback()
            return result.code_verifier
        finally:
            session.close()

    def second_consumer():
        assert first_consumed.wait(timeout=10)
        session = db.SessionLocal()
        transaction = session.begin()
        try:
            second_pid["value"] = session.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            second_pid_ready.set()
            try:
                result = (
                    calendar_oauth_states
                    .consume_google_calendar_oauth_state_with_pkce(
                        session,
                        raw_state=issued.raw_state,
                        pkce_cipher=OLD_CIPHER,
                        now=T0,
                    )
                )
                transaction.commit()
                return "consumed", result.code_verifier
            except CalendarOAuthStateInvalid:
                transaction.rollback()
                return "invalid", None
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first_consumer)
        assert first_consumed.wait(timeout=10)
        second_future = executor.submit(second_consumer)
        assert second_pid_ready.wait(timeout=10)
        try:
            _wait_until_database_blocked(second_pid["value"])
        finally:
            release_first.set()
        first_verifier = first_future.result(timeout=10)
        second_outcome = second_future.result(timeout=10)

    assert derive_s256_code_challenge(first_verifier) == issued.code_challenge
    assert second_outcome[0] == ("invalid" if first_commits else "consumed")
    if second_outcome[1] is not None:
        assert second_outcome[1] == first_verifier
    assert _owner_row_counts(account_id, session_id) == (0, 0)


@pytest.mark.parametrize("first_operation", ["issue", "consume"])
@pytest.mark.parametrize("first_commits", [True, False])
def test_replacement_and_consume_serialize_without_reviving_old_state(
    migrated_postgres_db,
    monkeypatch,
    first_operation,
    first_commits,
):
    account_id, session_id = _active_demo_owner(
        f"replacement-race-{first_operation}-{first_commits}"
    )
    original = _issue(account_id, session_id)
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: TOKEN_B,
    )
    first_finished = threading.Event()
    release_first = threading.Event()
    second_pid_ready = threading.Event()
    second_pid = {}

    def issue(session):
        return calendar_oauth_states.issue_google_calendar_oauth_state_with_pkce(
            session,
            account_id=account_id,
            session_id=session_id,
            pkce_cipher=OLD_CIPHER,
            now=T0,
        )

    def consume(session):
        return calendar_oauth_states.consume_google_calendar_oauth_state_with_pkce(
            session,
            raw_state=original.raw_state,
            pkce_cipher=OLD_CIPHER,
            now=T0,
        )

    def first_command():
        session = db.SessionLocal()
        transaction = session.begin()
        try:
            result = issue(session) if first_operation == "issue" else consume(
                session
            )
            first_finished.set()
            assert release_first.wait(timeout=10)
            if first_commits:
                transaction.commit()
            else:
                transaction.rollback()
            if first_operation == "issue":
                assert result.raw_state == TOKEN_B
            else:
                assert (
                    derive_s256_code_challenge(result.code_verifier)
                    == original.code_challenge
                )
            return first_operation
        finally:
            session.close()

    def second_command():
        assert first_finished.wait(timeout=10)
        session = db.SessionLocal()
        transaction = session.begin()
        try:
            second_pid["value"] = session.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            second_pid_ready.set()
            if first_operation == "issue":
                try:
                    consume(session)
                    outcome = "consumed"
                except CalendarOAuthStateInvalid:
                    outcome = "invalid"
            else:
                assert issue(session).raw_state == TOKEN_B
                outcome = "issued"
            transaction.commit()
            return outcome
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first_command)
        assert first_finished.wait(timeout=10)
        second_future = executor.submit(second_command)
        assert second_pid_ready.wait(timeout=10)
        try:
            _wait_until_database_blocked(second_pid["value"])
        finally:
            release_first.set()
        assert first_future.result(timeout=10) == first_operation
        second_outcome = second_future.result(timeout=10)

    expected_second = (
        ("invalid" if first_commits else "consumed")
        if first_operation == "issue"
        else "issued"
    )
    assert second_outcome == expected_second
    final_pair = _snapshot(account_id, session_id)
    if first_operation == "issue" and not first_commits:
        assert final_pair is None
    else:
        assert final_pair["state_digest"] == _digest(TOKEN_B)
        assert final_pair["verifier_key_id"] == OLD_KEY_ID
        assert _owner_row_counts(account_id, session_id) == (1, 1)


@pytest.mark.parametrize("first_operation", ["revoke", "consume"])
@pytest.mark.parametrize("first_commits", [True, False])
def test_revocation_and_consume_serialize_without_deadlock(
    migrated_postgres_db,
    first_operation,
    first_commits,
):
    account_id, session_id = _active_demo_owner(
        f"revocation-race-{first_operation}-{first_commits}"
    )
    issued = _issue(account_id, session_id)
    first_finished = threading.Event()
    release_first = threading.Event()
    second_pid_ready = threading.Event()
    second_pid = {}

    def revoke(session):
        account = (
            session.query(ProviderAccountDB)
            .filter_by(id=account_id)
            .populate_existing()
            .with_for_update()
            .one()
        )
        provider_session = (
            session.query(ProviderSessionDB)
            .filter_by(account_id=account.id, id=session_id)
            .populate_existing()
            .with_for_update()
            .one()
        )
        provider_session.revoked_at = T0
        session.flush()

    def consume(session):
        return calendar_oauth_states.consume_google_calendar_oauth_state_with_pkce(
            session,
            raw_state=issued.raw_state,
            pkce_cipher=OLD_CIPHER,
            now=T0,
        )

    def first_command():
        session = db.SessionLocal()
        transaction = session.begin()
        try:
            if first_operation == "revoke":
                revoke(session)
                outcome = "revoked"
            else:
                consume(session)
                outcome = "consumed"
            first_finished.set()
            assert release_first.wait(timeout=10)
            if first_commits:
                transaction.commit()
            else:
                transaction.rollback()
            return outcome
        finally:
            session.close()

    def second_command():
        assert first_finished.wait(timeout=10)
        session = db.SessionLocal()
        transaction = session.begin()
        try:
            second_pid["value"] = session.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            second_pid_ready.set()
            if first_operation == "revoke":
                try:
                    consume(session)
                    outcome = "consumed"
                except CalendarOAuthStateInvalid:
                    outcome = "invalid"
            else:
                revoke(session)
                outcome = "revoked"
            transaction.commit()
            return outcome
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first_command)
        assert first_finished.wait(timeout=10)
        second_future = executor.submit(second_command)
        assert second_pid_ready.wait(timeout=10)
        try:
            _wait_until_database_blocked(second_pid["value"])
        finally:
            release_first.set()
        first_outcome = first_future.result(timeout=10)
        second_outcome = second_future.result(timeout=10)

    assert first_outcome == (
        "revoked" if first_operation == "revoke" else "consumed"
    )
    assert second_outcome == (
        ("invalid" if first_commits else "consumed")
        if first_operation == "revoke"
        else "revoked"
    )
    with db.SessionLocal() as session:
        revoked_at = session.get(ProviderSessionDB, session_id).revoked_at
    assert (revoked_at is not None) == (
        first_operation == "consume" or first_commits
    )
    consume_committed = (
        first_operation == "consume" and first_commits
    ) or (first_operation == "revoke" and not first_commits)
    assert _owner_row_counts(account_id, session_id) == (
        (0, 0) if consume_committed else (1, 1)
    )


def test_post_decryption_failure_redacts_every_sensitive_value(
    migrated_postgres_db,
    monkeypatch,
    caplog,
):
    account_id, session_id = _active_demo_owner("redaction")
    issued = _issue(account_id, session_id)
    snapshot = _snapshot(account_id, session_id)
    plaintext_verifier = "V" * 43
    exception_secret = "delete-failure-private-detail"
    decrypted = False

    def decrypt(*args, **kwargs):
        nonlocal decrypted
        assert _owner_row_counts(account_id, session_id) == (1, 1)
        decrypted = True
        return plaintext_verifier

    monkeypatch.setattr(
        CalendarOAuthPKCECipher,
        "decrypt_google_calendar_verifier",
        decrypt,
    )
    caplog.set_level("DEBUG")
    session = db.SessionLocal()
    transaction = session.begin()
    real_execute = session.execute

    def fail_delete(statement, *args, **kwargs):
        if decrypted and getattr(statement, "is_delete", False):
            raise RuntimeError(exception_secret)
        return real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", fail_delete)
    try:
        with pytest.raises(CalendarOAuthStatePersistenceError) as exc_info:
            calendar_oauth_states.consume_google_calendar_oauth_state_with_pkce(
                session,
                raw_state=issued.raw_state,
                pkce_cipher=OLD_CIPHER,
                now=T0,
            )
        session.execute(text("SELECT 1"))
        transaction.rollback()
    finally:
        session.close()

    assert decrypted is True
    _assert_error_safe(
        exc_info.value,
        issued.raw_state,
        snapshot["state_digest"].hex(),
        repr(snapshot["state_digest"]),
        account_id,
        session_id,
        plaintext_verifier,
        snapshot["verifier_key_id"],
        snapshot["verifier_nonce"].hex(),
        snapshot["verifier_ciphertext"].hex(),
        OLD_ROOT_KEY.hex(),
        exception_secret,
    )
    assert caplog.text == ""
    assert _snapshot(account_id, session_id) == snapshot


def test_post_delete_failure_rolls_back_savepoint_before_outer_commit(
    migrated_postgres_db,
    monkeypatch,
    caplog,
):
    account_id, session_id = _active_demo_owner("post-delete-failure")
    issued = _issue(account_id, session_id)
    snapshot = _snapshot(account_id, session_id)
    plaintext_verifier = "W" * 43
    exception_secret = "result-proxy-private-detail"
    delete_executed = False

    monkeypatch.setattr(
        CalendarOAuthPKCECipher,
        "decrypt_google_calendar_verifier",
        lambda *args, **kwargs: plaintext_verifier,
    )
    caplog.set_level("DEBUG")
    session = db.SessionLocal()
    transaction = session.begin()
    real_execute = session.execute

    class FailingDeleteResult:
        def __init__(self, result):
            self.result = result

        def one_or_none(self):
            nonlocal delete_executed
            deleted = self.result.one_or_none()
            assert deleted.account_id == account_id
            assert deleted.session_id == session_id
            delete_executed = True
            raise RuntimeError(exception_secret)

    def fail_after_delete(statement, *args, **kwargs):
        result = real_execute(statement, *args, **kwargs)
        if getattr(statement, "is_delete", False):
            return FailingDeleteResult(result)
        return result

    monkeypatch.setattr(session, "execute", fail_after_delete)
    try:
        with pytest.raises(CalendarOAuthStatePersistenceError) as exc_info:
            calendar_oauth_states.consume_google_calendar_oauth_state_with_pkce(
                session,
                raw_state=issued.raw_state,
                pkce_cipher=OLD_CIPHER,
                now=T0,
            )
        transaction.commit()
    finally:
        session.close()

    assert delete_executed is True
    _assert_error_safe(
        exc_info.value,
        issued.raw_state,
        snapshot["state_digest"].hex(),
        account_id,
        session_id,
        plaintext_verifier,
        snapshot["verifier_key_id"],
        snapshot["verifier_nonce"].hex(),
        snapshot["verifier_ciphertext"].hex(),
        exception_secret,
    )
    assert caplog.text == ""
    assert _snapshot(account_id, session_id) == snapshot


def _issue(
    account_id: str,
    session_id: str,
    *,
    now: datetime.datetime = T0,
    cipher: CalendarOAuthPKCECipher = OLD_CIPHER,
):
    with db.SessionLocal.begin() as session:
        return calendar_oauth_states.issue_google_calendar_oauth_state_with_pkce(
            session,
            account_id=account_id,
            session_id=session_id,
            pkce_cipher=cipher,
            now=now,
        )


def _consume(
    raw_state,
    *,
    now: datetime.datetime = T0,
    cipher: CalendarOAuthPKCECipher = OLD_CIPHER,
):
    with db.SessionLocal.begin() as session:
        return calendar_oauth_states.consume_google_calendar_oauth_state_with_pkce(
            session,
            raw_state=raw_state,
            pkce_cipher=cipher,
            now=now,
        )


def _snapshot(account_id: str, session_id: str):
    with db.SessionLocal() as session:
        row = (
            session.query(
                ProviderCalendarOAuthStateDB,
                ProviderCalendarOAuthPKCEVerifierDB,
            )
            .outerjoin(
                ProviderCalendarOAuthPKCEVerifierDB,
                ProviderCalendarOAuthPKCEVerifierDB.state_digest
                == ProviderCalendarOAuthStateDB.state_digest,
            )
            .filter(
                ProviderCalendarOAuthStateDB.account_id == account_id,
                ProviderCalendarOAuthStateDB.session_id == session_id,
            )
            .one_or_none()
        )
        if row is None:
            return None
        state, verifier = row
        return {
            "state_digest": bytes(state.state_digest),
            "account_id": state.account_id,
            "session_id": state.session_id,
            "provider": state.provider,
            "created_at": state.created_at,
            "expires_at": state.expires_at,
            "browser_binding_digest": (
                bytes(state.browser_binding_digest)
                if state.browser_binding_digest is not None
                else None
            ),
            "verifier_version": (
                verifier.verifier_version if verifier is not None else None
            ),
            "verifier_key_id": (
                verifier.verifier_key_id if verifier is not None else None
            ),
            "verifier_nonce": (
                bytes(verifier.verifier_nonce) if verifier is not None else None
            ),
            "verifier_ciphertext": (
                bytes(verifier.verifier_ciphertext)
                if verifier is not None
                else None
            ),
        }


def _owner_row_counts(account_id: str, session_id: str) -> tuple[int, int]:
    with db.SessionLocal() as session:
        state_digests = session.query(
            ProviderCalendarOAuthStateDB.state_digest
        ).filter(
            ProviderCalendarOAuthStateDB.account_id == account_id,
            ProviderCalendarOAuthStateDB.session_id == session_id,
        )
        state_count = state_digests.count()
        verifier_count = (
            session.query(ProviderCalendarOAuthPKCEVerifierDB.state_digest)
            .filter(
                ProviderCalendarOAuthPKCEVerifierDB.state_digest.in_(
                    state_digests
                )
            )
            .count()
        )
        return state_count, verifier_count


def _active_demo_owner(suffix: str) -> tuple[str, str]:
    account_id = _demo_account_id()
    return account_id, _add_session(account_id, suffix)


def _demo_account_id() -> str:
    with db.SessionLocal() as session:
        return session.query(ProviderAccountDB.id).filter_by(
            instructor_id="inst_001"
        ).one()[0]


def _add_owner(suffix: str) -> tuple[str, str]:
    with db.SessionLocal.begin() as session:
        instructor_id = f"inst_oauth_pkce_consume_{suffix}"
        account_id = f"acct_oauth_pkce_consume_{suffix}"
        session.add(
            InstructorDB(
                id=instructor_id,
                name=f"OAuth PKCE Consume {suffix}",
                main_address="1 Authorization Way",
                lat=30.2,
                lng=-97.2,
                work_start_time="08:00",
                work_end_time="17:00",
                timezone="America/Chicago",
            )
        )
        session.add(
            ProviderAccountDB(
                id=account_id,
                instructor_id=instructor_id,
                email_normalized=f"oauth-pkce-consume-{suffix}@example.com",
                password_hash="not-used-by-pkce-consume-test",
                is_demo=False,
                session_version=1,
            )
        )
        session.flush()
        session_id = _add_session(
            account_id,
            f"owner-{suffix}",
            session=session,
        )
    return account_id, session_id


def _add_session(
    account_id: str,
    suffix: str,
    *,
    created_at: datetime.datetime = T0 - datetime.timedelta(hours=1),
    expires_at: datetime.datetime = T0 + datetime.timedelta(hours=1),
    session=None,
) -> str:
    owns_session = session is None
    active_session = session or db.SessionLocal()
    transaction = active_session.begin() if owns_session else None
    try:
        session_id = f"sess_oauth_pkce_consume_{suffix}"
        account_version = active_session.query(
            ProviderAccountDB.session_version
        ).filter_by(id=account_id).one()[0]
        active_session.add(
            ProviderSessionDB(
                id=session_id,
                account_id=account_id,
                token_digest=hashlib.sha256(
                    f"pkce-consume-session-{suffix}".encode()
                ).digest(),
                session_version=account_version,
                created_at=created_at,
                expires_at=expires_at,
            )
        )
        if transaction is not None:
            transaction.commit()
        else:
            active_session.flush()
        return session_id
    except Exception:
        if transaction is not None:
            transaction.rollback()
        raise
    finally:
        if owns_session:
            active_session.close()


def _store_bare_state(
    account_id: str,
    session_id: str,
    raw_state: str,
) -> None:
    with db.SessionLocal.begin() as session:
        session.add(
            ProviderCalendarOAuthStateDB(
                state_digest=_digest(raw_state),
                account_id=account_id,
                session_id=session_id,
                provider="google",
                created_at=T0 - datetime.timedelta(minutes=1),
                expires_at=T0 + datetime.timedelta(minutes=9),
            )
        )


def _digest(raw_state: str) -> bytes:
    return hashlib.sha256(raw_state.encode("ascii")).digest()


def _binding_digest(label: str) -> bytes:
    return hashlib.sha256(f"test-browser-binding:{label}".encode()).digest()


def _flip_first_byte(value: bytes) -> bytes:
    copied = bytes(value)
    return bytes([copied[0] ^ 1]) + copied[1:]


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


def _assert_error_safe(error: Exception, *forbidden: str) -> None:
    assert error.__context__ is None
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    rendered = "".join(traceback.format_exception(error))
    repository_locals = []
    repository_frames = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith(
            "/app/calendar_oauth_states.py"
        ):
            repository_frames.append(current.tb_frame.f_code.co_name)
            repository_locals.extend(current.tb_frame.f_locals.values())
        current = current.tb_next
    assert "_consume_google_calendar_oauth_state_with_pkce" not in (
        repository_frames
    )
    rendered_locals = repr(repository_locals)
    for value in forbidden:
        assert value not in str(error)
        assert value not in repr(error)
        assert value not in rendered
        assert value not in rendered_locals
