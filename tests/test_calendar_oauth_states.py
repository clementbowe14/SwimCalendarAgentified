from __future__ import annotations

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
from app.calendar_oauth_states import (
    CalendarOAuthStateInputError,
    CalendarOAuthStateInvalid,
    CalendarOAuthStatePersistenceError,
    CalendarOAuthStateSessionInvalid,
    CalendarOAuthStateTransactionRequired,
    ConsumedGoogleCalendarOAuthState,
    IssuedGoogleCalendarOAuthState,
)
from app.models import (
    InstructorDB,
    ProviderAccountDB,
    ProviderCalendarOAuthStateDB,
    ProviderSessionDB,
)


UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 11, 12, 14, 0, tzinfo=UTC)
TOKEN_A = "A" * 43
TOKEN_B = "B" * 43
TOKEN_C = "C" * 43
TOKEN_D = "D" * 43


def test_exported_surface_results_and_commands_require_explicit_transactions(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, session_id = _active_demo_owner("transactions")
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: TOKEN_A,
    )

    assert set(calendar_oauth_states.__all__) == {
        "CalendarOAuthStateError",
        "CalendarOAuthStateInputError",
        "CalendarOAuthStateInvalid",
        "CalendarOAuthStatePersistenceError",
        "CalendarOAuthStateSessionInvalid",
        "CalendarOAuthStateTransactionRequired",
        "ConsumedGoogleCalendarOAuthState",
        "IssuedGoogleCalendarOAuthState",
        "consume_google_calendar_oauth_state",
        "issue_google_calendar_oauth_state",
    }
    assert not hasattr(calendar_oauth_states, "ProviderAccountDB")
    assert not hasattr(calendar_oauth_states, "ProviderSessionDB")
    assert not hasattr(calendar_oauth_states, "ProviderCalendarOAuthStateDB")
    consume_parameters = set(
        inspect.signature(
            calendar_oauth_states.consume_google_calendar_oauth_state
        ).parameters
    )
    assert consume_parameters == {"session", "raw_state", "now"}

    session = db.SessionLocal()
    try:
        with pytest.raises(CalendarOAuthStateTransactionRequired):
            calendar_oauth_states.issue_google_calendar_oauth_state(
                session,
                account_id=account_id,
                session_id=session_id,
                now=T0,
            )
        with pytest.raises(CalendarOAuthStateTransactionRequired):
            calendar_oauth_states.consume_google_calendar_oauth_state(
                session,
                raw_state=TOKEN_A,
                now=T0,
            )

        session.get(ProviderAccountDB, account_id)
        with pytest.raises(CalendarOAuthStateTransactionRequired):
            calendar_oauth_states.issue_google_calendar_oauth_state(
                session,
                account_id=account_id,
                session_id=session_id,
                now=T0,
            )
        session.rollback()

        transaction = session.begin()
        issued = calendar_oauth_states.issue_google_calendar_oauth_state(
            session,
            account_id=account_id,
            session_id=session_id,
            now=T0,
        )
        transaction.rollback()
    finally:
        session.close()

    assert isinstance(issued, IssuedGoogleCalendarOAuthState)
    assert _stored_states() == []


def test_issue_requests_32_random_bytes_and_persists_only_digest(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, session_id = _active_demo_owner("digest")
    requested_sizes = []

    def token_urlsafe(size: int) -> str:
        requested_sizes.append(size)
        return TOKEN_A

    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        token_urlsafe,
    )
    issued = _issue(account_id, session_id, now=T0)

    assert requested_sizes == [32]
    assert issued.raw_state == TOKEN_A
    assert issued.expires_at == T0 + datetime.timedelta(minutes=10)
    assert TOKEN_A not in repr(issued)
    assert TOKEN_A not in str(issued)

    rows = _stored_states()
    assert len(rows) == 1
    row = rows[0]
    digest = hashlib.sha256(TOKEN_A.encode("ascii")).digest()
    assert row.state_digest == digest
    assert row.account_id == account_id
    assert row.session_id == session_id
    assert row.provider == "google"
    assert row.created_at == T0
    assert row.expires_at == issued.expires_at
    assert all(
        TOKEN_A not in str(value)
        for value in (
            row.state_digest,
            row.account_id,
            row.session_id,
            row.provider,
            row.created_at,
            row.expires_at,
        )
    )


def test_issue_caps_expiry_to_ten_minutes_or_session_expiry(
    migrated_postgres_db,
    monkeypatch,
):
    account_id = _demo_account_id()
    long_session = _add_session(
        account_id,
        "long-expiry",
        expires_at=T0 + datetime.timedelta(hours=1),
    )
    short_expiry = T0 + datetime.timedelta(minutes=2)
    short_session = _add_session(
        account_id,
        "short-expiry",
        expires_at=short_expiry,
    )
    expired_session = _add_session(
        account_id,
        "exact-expiry",
        expires_at=T0,
    )
    generated = iter([TOKEN_A, TOKEN_B])
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: next(generated),
    )

    assert _issue(account_id, long_session, now=T0).expires_at == (
        T0 + datetime.timedelta(minutes=10)
    )
    assert _issue(account_id, short_session, now=T0).expires_at == short_expiry
    with pytest.raises(CalendarOAuthStateSessionInvalid):
        _issue(account_id, expired_session, now=T0)


def test_issue_replaces_old_state_only_when_transaction_commits(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, session_id = _active_demo_owner("replacement")
    generated = iter([TOKEN_A, TOKEN_B, TOKEN_C])
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: next(generated),
    )

    first = _issue(account_id, session_id, now=T0)
    second = _issue(
        account_id,
        session_id,
        now=T0 + datetime.timedelta(minutes=1),
    )
    assert first.raw_state == TOKEN_A
    assert second.raw_state == TOKEN_B
    assert [row.state_digest for row in _stored_states()] == [
        hashlib.sha256(TOKEN_B.encode("ascii")).digest()
    ]
    with pytest.raises(CalendarOAuthStateInvalid):
        _consume(TOKEN_A, now=T0 + datetime.timedelta(minutes=1))

    session = db.SessionLocal()
    transaction = session.begin()
    try:
        rolled_back = calendar_oauth_states.issue_google_calendar_oauth_state(
            session,
            account_id=account_id,
            session_id=session_id,
            now=T0 + datetime.timedelta(minutes=2),
        )
        transaction.rollback()
    finally:
        session.close()

    assert rolled_back.raw_state == TOKEN_C
    consumed = _consume(TOKEN_B, now=T0 + datetime.timedelta(minutes=2))
    assert consumed.account_id == account_id
    assert consumed.session_id == session_id
    with pytest.raises(CalendarOAuthStateInvalid):
        _consume(TOKEN_C, now=T0 + datetime.timedelta(minutes=2))


def test_issue_retries_existing_session_and_cross_tenant_digest_collisions(
    migrated_postgres_db,
    monkeypatch,
):
    first_account, first_session = _active_demo_owner("collision-first")
    second_account, second_session = _add_owner("collision-second")
    _store_state(first_account, first_session, TOKEN_A)
    _store_state(second_account, second_session, TOKEN_B)
    generated = iter([TOKEN_A, TOKEN_B, TOKEN_C])
    requested_sizes = []

    def token_urlsafe(size: int) -> str:
        requested_sizes.append(size)
        return next(generated)

    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        token_urlsafe,
    )
    issued = _issue(first_account, first_session, now=T0)

    assert requested_sizes == [32, 32, 32]
    assert issued.raw_state == TOKEN_C
    by_owner = {
        (row.account_id, row.session_id): row.state_digest
        for row in _stored_states()
    }
    assert by_owner[(first_account, first_session)] == _digest(TOKEN_C)
    assert by_owner[(second_account, second_session)] == _digest(TOKEN_B)


def test_collision_and_entropy_exhaustion_preserve_old_state_and_are_redacted(
    migrated_postgres_db,
    monkeypatch,
    caplog,
):
    account_id, session_id = _active_demo_owner("exhaustion")
    _store_state(account_id, session_id, TOKEN_A)
    second_account, second_session = _add_owner("exhaustion-collision")
    _store_state(second_account, second_session, TOKEN_B)
    old_digest = _digest(TOKEN_A)
    collision_digest = _digest(TOKEN_B)
    monkeypatch.setattr(
        calendar_oauth_states,
        "_digest_exists",
        lambda session, state_digest: False,
    )
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: TOKEN_B,
    )

    session = db.SessionLocal()
    try:
        with session.begin():
            with pytest.raises(CalendarOAuthStatePersistenceError) as exc_info:
                calendar_oauth_states.issue_google_calendar_oauth_state(
                    session,
                    account_id=account_id,
                    session_id=session_id,
                    now=T0,
                )
            session.execute(text("SELECT 1"))
    finally:
        session.close()

    _assert_error_safe(
        exc_info.value,
        TOKEN_A,
        TOKEN_B,
        old_digest.hex(),
        collision_digest.hex(),
        repr(old_digest),
        repr(collision_digest),
        account_id,
        session_id,
        second_account,
        second_session,
    )
    assert caplog.text == ""
    assert {
        (row.account_id, row.session_id): row.state_digest
        for row in _stored_states()
    } == {
        (account_id, session_id): old_digest,
        (second_account, second_session): collision_digest,
    }

    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: (_ for _ in ()).throw(ValueError(TOKEN_B)),
    )
    with pytest.raises(CalendarOAuthStatePersistenceError) as entropy_error:
        _issue(account_id, session_id, now=T0)
    _assert_error_safe(entropy_error.value, TOKEN_A, TOKEN_B, account_id)
    assert {
        (row.account_id, row.session_id): row.state_digest
        for row in _stored_states()
    } == {
        (account_id, session_id): old_digest,
        (second_account, second_session): collision_digest,
    }


@pytest.mark.parametrize(
    "case",
    [
        "missing_account",
        "missing_session",
        "cross_account_session",
        "revoked",
        "expired",
        "future_created",
        "version_stale",
        "disabled",
    ],
)
def test_issue_rejects_every_inactive_or_substituted_owner_uniformly(
    migrated_postgres_db,
    monkeypatch,
    case,
):
    account_id, session_id = _active_demo_owner(f"inactive-{case}")
    _store_state(account_id, session_id, TOKEN_A)
    call_account_id = account_id
    call_session_id = session_id

    with db.SessionLocal.begin() as session:
        account = session.get(ProviderAccountDB, account_id)
        provider_session = session.get(ProviderSessionDB, session_id)
        if case == "missing_account":
            call_account_id = "acct_missing_oauth_issue"
        elif case == "missing_session":
            call_session_id = "sess_missing_oauth_issue"
        elif case == "cross_account_session":
            _, call_session_id = _add_owner(
                "cross-account-issue",
                session=session,
            )
        elif case == "revoked":
            provider_session.revoked_at = T0
        elif case == "expired":
            provider_session.expires_at = T0
        elif case == "future_created":
            provider_session.created_at = T0 + datetime.timedelta(seconds=1)
        elif case == "version_stale":
            account.session_version += 1
        elif case == "disabled":
            account.disabled_at = T0

    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: TOKEN_B,
    )
    with pytest.raises(CalendarOAuthStateSessionInvalid) as exc_info:
        _issue(call_account_id, call_session_id, now=T0)

    assert str(exc_info.value) == "Calendar authorization session is not active."
    _assert_error_safe(
        exc_info.value,
        account_id,
        session_id,
        call_account_id,
        call_session_id,
        TOKEN_A,
        TOKEN_B,
    )
    assert [row.state_digest for row in _stored_states()] == [_digest(TOKEN_A)]


@pytest.mark.parametrize(
    ("operation", "mutation"),
    [
        ("issue", "disable_account"),
        ("issue", "revoke_session"),
        ("consume", "disable_account"),
        ("consume", "revoke_session"),
    ],
)
def test_locked_commands_refresh_stale_cached_account_and_session(
    migrated_postgres_db,
    monkeypatch,
    operation,
    mutation,
):
    suffix = f"stale-cache-{operation}-{mutation}"
    account_id, session_id = _active_demo_owner(suffix)
    if operation == "consume":
        _store_state(account_id, session_id, TOKEN_A)
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: TOKEN_B,
    )

    session = db.SessionLocal()
    try:
        with session.begin():
            cached_account = session.get(ProviderAccountDB, account_id)
            cached_session = session.get(ProviderSessionDB, session_id)
            assert cached_account.disabled_at is None
            assert cached_session.revoked_at is None
            with db.SessionLocal.begin() as other_session:
                if mutation == "disable_account":
                    other_session.query(ProviderAccountDB).filter_by(
                        id=account_id
                    ).update({"disabled_at": T0})
                else:
                    other_session.query(ProviderSessionDB).filter_by(
                        id=session_id
                    ).update({"revoked_at": T0})
            expected_error = (
                CalendarOAuthStateSessionInvalid
                if operation == "issue"
                else CalendarOAuthStateInvalid
            )
            with pytest.raises(expected_error):
                if operation == "issue":
                    calendar_oauth_states.issue_google_calendar_oauth_state(
                        session,
                        account_id=account_id,
                        session_id=session_id,
                        now=T0,
                    )
                else:
                    calendar_oauth_states.consume_google_calendar_oauth_state(
                        session,
                        raw_state=TOKEN_A,
                        now=T0,
                    )
    finally:
        session.close()
    expected_digests = [_digest(TOKEN_A)] if operation == "consume" else []
    assert [row.state_digest for row in _stored_states()] == expected_digests


def test_consume_returns_only_owner_from_one_conditional_delete(
    migrated_postgres_db,
):
    account_id, session_id = _active_demo_owner("consume-delete")
    _store_state(account_id, session_id, TOKEN_A)
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
        consumed = _consume(TOKEN_A, now=T0)
    finally:
        event.remove(db.engine, "before_cursor_execute", capture_statement)

    assert consumed == ConsumedGoogleCalendarOAuthState(
        account_id=account_id,
        session_id=session_id,
    )
    assert account_id not in repr(consumed)
    assert session_id not in str(consumed)
    state_dml = [
        statement
        for statement in statements
        if "provider_calendar_oauth_states" in statement
        and statement.lstrip().upper().startswith(
            ("DELETE", "INSERT", "UPDATE")
        )
    ]
    assert len(state_dml) == 1
    assert state_dml[0].lstrip().upper().startswith("DELETE")
    assert "RETURNING" in state_dml[0].upper()
    assert _stored_states() == []


def test_replay_unknown_and_malformed_states_are_indistinguishable(
    migrated_postgres_db,
):
    account_id, session_id = _active_demo_owner("invalid-consume")
    _store_state(account_id, session_id, TOKEN_A)
    _consume(TOKEN_A, now=T0)

    invalid_values = [
        TOKEN_A,
        "Z" * 43,
        "short",
        TOKEN_B + " ",
        "é" * 43,
        None,
        TOKEN_C.encode("ascii"),
    ]
    rendered = []
    for value in invalid_values:
        with pytest.raises(CalendarOAuthStateInvalid) as exc_info:
            _consume(value, now=T0)
        rendered.append(
            (type(exc_info.value), str(exc_info.value), repr(exc_info.value))
        )
    assert len(set(rendered)) == 1
    assert rendered[0][1] == "Calendar authorization state is invalid."


@pytest.mark.parametrize(
    "case",
    [
        "revoked",
        "session_expired",
        "session_future",
        "version_stale",
        "disabled",
        "state_expired",
        "state_future",
    ],
)
def test_consume_revalidates_state_account_and_session_uniformly(
    migrated_postgres_db,
    case,
):
    account_id, session_id = _active_demo_owner(f"consume-{case}")
    _store_state(account_id, session_id, TOKEN_A)
    with db.SessionLocal.begin() as session:
        account = session.get(ProviderAccountDB, account_id)
        provider_session = session.get(ProviderSessionDB, session_id)
        state = session.get(ProviderCalendarOAuthStateDB, _digest(TOKEN_A))
        if case == "revoked":
            provider_session.revoked_at = T0
        elif case == "session_expired":
            provider_session.expires_at = T0
        elif case == "session_future":
            provider_session.created_at = T0 + datetime.timedelta(seconds=1)
        elif case == "version_stale":
            account.session_version += 1
        elif case == "disabled":
            account.disabled_at = T0
        elif case == "state_expired":
            state.created_at = T0 - datetime.timedelta(minutes=10)
            state.expires_at = T0
        elif case == "state_future":
            state.created_at = T0 + datetime.timedelta(seconds=1)
            state.expires_at = T0 + datetime.timedelta(minutes=1)

    with pytest.raises(CalendarOAuthStateInvalid) as exc_info:
        _consume(TOKEN_A, now=T0)
    assert str(exc_info.value) == "Calendar authorization state is invalid."
    _assert_error_safe(exc_info.value, TOKEN_A, account_id, session_id)
    assert [row.state_digest for row in _stored_states()] == [_digest(TOKEN_A)]


def test_consume_uses_persisted_tenant_and_preserves_other_tenant(
    migrated_postgres_db,
):
    first_account, first_session = _active_demo_owner("tenant-first")
    second_account, second_session = _add_owner("tenant-second")
    _store_state(first_account, first_session, TOKEN_A)
    _store_state(second_account, second_session, TOKEN_B)

    consumed = _consume(TOKEN_B, now=T0)

    assert consumed.account_id == second_account
    assert consumed.session_id == second_session
    remaining = _stored_states()
    assert len(remaining) == 1
    assert remaining[0].account_id == first_account
    assert remaining[0].state_digest == _digest(TOKEN_A)


@pytest.mark.parametrize(
    ("consumed_at", "valid"),
    [
        (T0 - datetime.timedelta(microseconds=1), True),
        (T0, False),
    ],
)
def test_consume_exact_expiry_boundary(
    migrated_postgres_db,
    consumed_at,
    valid,
):
    account_id = _demo_account_id()
    session_id = _add_session(
        account_id,
        f"boundary-{valid}",
        created_at=T0 - datetime.timedelta(minutes=10),
        expires_at=T0,
    )
    _store_state(
        account_id,
        session_id,
        TOKEN_A,
        created_at=T0 - datetime.timedelta(minutes=10),
        expires_at=T0,
    )

    if valid:
        assert _consume(TOKEN_A, now=consumed_at).account_id == account_id
        assert _stored_states() == []
    else:
        with pytest.raises(CalendarOAuthStateInvalid):
            _consume(TOKEN_A, now=consumed_at)
        assert len(_stored_states()) == 1


def test_creation_exactly_at_operation_time_is_valid(
    migrated_postgres_db,
    monkeypatch,
):
    account_id = _demo_account_id()
    issue_session = _add_session(
        account_id,
        "created-boundary-issue",
        created_at=T0,
        expires_at=T0 + datetime.timedelta(minutes=20),
    )
    consume_session = _add_session(
        account_id,
        "created-boundary-consume",
        created_at=T0,
        expires_at=T0 + datetime.timedelta(minutes=20),
    )
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: TOKEN_A,
    )

    assert _issue(account_id, issue_session, now=T0).raw_state == TOKEN_A
    _store_state(
        account_id,
        consume_session,
        TOKEN_B,
        created_at=T0,
        expires_at=T0 + datetime.timedelta(minutes=10),
    )
    assert _consume(TOKEN_B, now=T0).session_id == consume_session


def test_timezone_normalization_operation_clock_and_naive_rejection(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, session_id = _active_demo_owner("timezones")
    local_t0 = T0.astimezone(
        datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    )
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: TOKEN_A,
    )

    with db.SessionLocal.begin() as session:
        session.execute(text("SET LOCAL TIME ZONE 'America/Los_Angeles'"))
        issued = calendar_oauth_states.issue_google_calendar_oauth_state(
            session,
            account_id=account_id,
            session_id=session_id,
            now=local_t0,
        )
    assert issued.expires_at == T0 + datetime.timedelta(minutes=10)
    row = _stored_states()[0]
    assert row.created_at.astimezone(UTC) == T0
    assert row.expires_at.astimezone(UTC) == issued.expires_at

    with pytest.raises(CalendarOAuthStateInputError):
        _issue(account_id, session_id, now=T0.replace(tzinfo=None))
    with pytest.raises(CalendarOAuthStateInputError):
        _consume(TOKEN_A, now=T0.replace(tzinfo=None))
    assert [stored.state_digest for stored in _stored_states()] == [
        _digest(TOKEN_A)
    ]


def test_consume_rollback_restores_state(migrated_postgres_db):
    account_id, session_id = _active_demo_owner("consume-rollback")
    _store_state(account_id, session_id, TOKEN_A)

    session = db.SessionLocal()
    transaction = session.begin()
    try:
        consumed = calendar_oauth_states.consume_google_calendar_oauth_state(
            session,
            raw_state=TOKEN_A,
            now=T0,
        )
        transaction.rollback()
    finally:
        session.close()

    assert consumed.account_id == account_id
    assert len(_stored_states()) == 1
    assert _consume(TOKEN_A, now=T0).session_id == session_id


@pytest.mark.parametrize("first_commits", [True, False])
def test_waiting_consumer_observes_first_commit_or_rollback(
    migrated_postgres_db,
    first_commits,
):
    account_id, session_id = _active_demo_owner(
        f"waiting-consumer-{first_commits}"
    )
    _store_state(account_id, session_id, TOKEN_A)
    first_deleted = threading.Event()
    release_first = threading.Event()
    second_pid_ready = threading.Event()
    second_pid = {}

    def first_consumer():
        session = db.SessionLocal()
        transaction = session.begin()
        try:
            result = (
                calendar_oauth_states.consume_google_calendar_oauth_state(
                    session,
                    raw_state=TOKEN_A,
                    now=T0,
                )
            )
            first_deleted.set()
            assert release_first.wait(timeout=10)
            if first_commits:
                transaction.commit()
            else:
                transaction.rollback()
            return result
        finally:
            session.close()

    def second_consumer():
        assert first_deleted.wait(timeout=10)
        session = db.SessionLocal()
        transaction = session.begin()
        try:
            second_pid["value"] = session.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            second_pid_ready.set()
            try:
                result = (
                    calendar_oauth_states.consume_google_calendar_oauth_state(
                        session,
                        raw_state=TOKEN_A,
                        now=T0,
                    )
                )
                transaction.commit()
                return "consumed", result.session_id
            except CalendarOAuthStateInvalid:
                transaction.rollback()
                return "invalid", None
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first_consumer)
        assert first_deleted.wait(timeout=10)
        second_future = executor.submit(second_consumer)
        assert second_pid_ready.wait(timeout=10)
        try:
            _wait_until_database_blocked(second_pid["value"])
        finally:
            release_first.set()
        first_result = first_future.result(timeout=10)
        second_outcome = second_future.result(timeout=10)

    assert first_result.session_id == session_id
    assert second_outcome == (
        ("invalid", None)
        if first_commits
        else ("consumed", session_id)
    )
    assert _stored_states() == []


def test_concurrent_issue_serializes_to_one_pending_state(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, session_id = _active_demo_owner("concurrent-issue")
    barrier = threading.Barrier(2)
    token_lock = threading.Lock()
    generated = iter([TOKEN_A, TOKEN_B])

    def token_urlsafe(size: int) -> str:
        with token_lock:
            return next(generated)

    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        token_urlsafe,
    )

    def issue() -> IssuedGoogleCalendarOAuthState:
        with db.SessionLocal.begin() as session:
            barrier.wait(timeout=5)
            return calendar_oauth_states.issue_google_calendar_oauth_state(
                session,
                account_id=account_id,
                session_id=session_id,
                now=T0,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            future.result(timeout=10)
            for future in [executor.submit(issue), executor.submit(issue)]
        ]

    rows = _stored_states()
    assert len(rows) == 1
    winner = next(
        result
        for result in results
        if _digest(result.raw_state) == rows[0].state_digest
    )
    loser = next(result for result in results if result is not winner)
    with pytest.raises(CalendarOAuthStateInvalid):
        _consume(loser.raw_state, now=T0)
    assert _consume(winner.raw_state, now=T0).account_id == account_id


@pytest.mark.parametrize("first_operation", ["issue", "consume"])
def test_issue_and_consume_serialize_without_reviving_old_state(
    migrated_postgres_db,
    monkeypatch,
    first_operation,
):
    account_id, session_id = _active_demo_owner(
        f"issue-consume-race-{first_operation}"
    )
    _store_state(account_id, session_id, TOKEN_A)
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: TOKEN_B,
    )
    first_finished_command = threading.Event()
    release_first = threading.Event()
    second_pid_ready = threading.Event()
    second_pid = {}

    def first_command():
        session = db.SessionLocal()
        transaction = session.begin()
        try:
            if first_operation == "issue":
                result = calendar_oauth_states.issue_google_calendar_oauth_state(
                    session,
                    account_id=account_id,
                    session_id=session_id,
                    now=T0,
                )
            else:
                result = (
                    calendar_oauth_states.consume_google_calendar_oauth_state(
                        session,
                        raw_state=TOKEN_A,
                        now=T0,
                    )
                )
            first_finished_command.set()
            assert release_first.wait(timeout=10)
            transaction.commit()
            return result
        finally:
            session.close()

    def second_command():
        assert first_finished_command.wait(timeout=10)
        session = db.SessionLocal()
        transaction = session.begin()
        try:
            second_pid["value"] = session.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            second_pid_ready.set()
            if first_operation == "issue":
                try:
                    calendar_oauth_states.consume_google_calendar_oauth_state(
                        session,
                        raw_state=TOKEN_A,
                        now=T0,
                    )
                    outcome = "consumed"
                except CalendarOAuthStateInvalid:
                    outcome = "invalid"
            else:
                calendar_oauth_states.issue_google_calendar_oauth_state(
                    session,
                    account_id=account_id,
                    session_id=session_id,
                    now=T0,
                )
                outcome = "issued"
            transaction.commit()
            return outcome
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first_command)
        assert first_finished_command.wait(timeout=10)
        second_future = executor.submit(second_command)
        assert second_pid_ready.wait(timeout=10)
        try:
            _wait_until_database_blocked(second_pid["value"])
        finally:
            release_first.set()
        first_future.result(timeout=10)
        second_outcome = second_future.result(timeout=10)

    assert second_outcome == (
        "invalid" if first_operation == "issue" else "issued"
    )
    rows = _stored_states()
    assert len(rows) == 1
    assert rows[0].state_digest == _digest(TOKEN_B)
    with pytest.raises(CalendarOAuthStateInvalid):
        _consume(TOKEN_A, now=T0)
    assert _consume(TOKEN_B, now=T0).session_id == session_id


@pytest.mark.parametrize("first_operation", ["revoke", "consume"])
def test_revocation_and_consume_serialize_without_deadlock(
    migrated_postgres_db,
    first_operation,
):
    account_id, session_id = _active_demo_owner(
        f"revoke-consume-race-{first_operation}"
    )
    _store_state(account_id, session_id, TOKEN_A)
    first_finished_command = threading.Event()
    release_first = threading.Event()
    second_pid_ready = threading.Event()
    second_pid = {}

    def revoke(session):
        provider_session = (
            session.query(ProviderSessionDB)
            .filter_by(id=session_id)
            .with_for_update()
            .one()
        )
        provider_session.revoked_at = T0
        session.flush()

    def first_command():
        session = db.SessionLocal()
        transaction = session.begin()
        try:
            if first_operation == "revoke":
                revoke(session)
                outcome = "revoked"
            else:
                calendar_oauth_states.consume_google_calendar_oauth_state(
                    session,
                    raw_state=TOKEN_A,
                    now=T0,
                )
                outcome = "consumed"
            first_finished_command.set()
            assert release_first.wait(timeout=10)
            transaction.commit()
            return outcome
        finally:
            session.close()

    def second_command():
        assert first_finished_command.wait(timeout=10)
        session = db.SessionLocal()
        transaction = session.begin()
        try:
            second_pid["value"] = session.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            second_pid_ready.set()
            if first_operation == "revoke":
                try:
                    calendar_oauth_states.consume_google_calendar_oauth_state(
                        session,
                        raw_state=TOKEN_A,
                        now=T0,
                    )
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
        assert first_finished_command.wait(timeout=10)
        second_future = executor.submit(second_command)
        assert second_pid_ready.wait(timeout=10)
        try:
            _wait_until_database_blocked(second_pid["value"])
        finally:
            release_first.set()
        first_outcome = first_future.result(timeout=10)
        second_outcome = second_future.result(timeout=10)

    assert (first_outcome, second_outcome) == (
        ("revoked", "invalid")
        if first_operation == "revoke"
        else ("consumed", "revoked")
    )
    with db.SessionLocal() as session:
        assert session.get(ProviderSessionDB, session_id).revoked_at == T0
    assert len(_stored_states()) == (1 if first_operation == "revoke" else 0)


def test_concurrent_consume_commits_exactly_once(migrated_postgres_db):
    account_id, session_id = _active_demo_owner("concurrent-consume")
    _store_state(account_id, session_id, TOKEN_A)
    barrier = threading.Barrier(2)

    def consume():
        try:
            with db.SessionLocal.begin() as session:
                barrier.wait(timeout=5)
                result = (
                    calendar_oauth_states.consume_google_calendar_oauth_state(
                        session,
                        raw_state=TOKEN_A,
                        now=T0,
                    )
                )
            return "consumed", result.account_id
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
    assert next(value for outcome, value in outcomes if outcome == "consumed") == (
        account_id
    )
    assert _stored_states() == []


def test_result_and_error_representations_hide_bearer_digest_and_owner(
    migrated_postgres_db,
    monkeypatch,
    caplog,
):
    account_id, session_id = _active_demo_owner("redaction")
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: TOKEN_D,
    )
    issued = _issue(account_id, session_id, now=T0)
    consumed = _consume(TOKEN_D, now=T0)
    digest = _digest(TOKEN_D)

    for rendered in (repr(issued), str(issued), repr(consumed), str(consumed)):
        assert TOKEN_D not in rendered
        assert account_id not in rendered
        assert session_id not in rendered
        assert digest.hex() not in rendered

    with pytest.raises(CalendarOAuthStateInvalid) as exc_info:
        _consume(TOKEN_D, now=T0)
    _assert_error_safe(
        exc_info.value,
        TOKEN_D,
        account_id,
        session_id,
        digest.hex(),
        repr(digest),
    )
    assert caplog.text == ""


def _issue(
    account_id: str,
    session_id: str,
    *,
    now: datetime.datetime,
) -> IssuedGoogleCalendarOAuthState:
    with db.SessionLocal.begin() as session:
        return calendar_oauth_states.issue_google_calendar_oauth_state(
            session,
            account_id=account_id,
            session_id=session_id,
            now=now,
        )


def _consume(
    raw_state,
    *,
    now: datetime.datetime,
) -> ConsumedGoogleCalendarOAuthState:
    with db.SessionLocal.begin() as session:
        return calendar_oauth_states.consume_google_calendar_oauth_state(
            session,
            raw_state=raw_state,
            now=now,
        )


def _active_demo_owner(suffix: str) -> tuple[str, str]:
    account_id = _demo_account_id()
    return account_id, _add_session(account_id, suffix)


def _demo_account_id() -> str:
    with db.SessionLocal() as session:
        return session.query(ProviderAccountDB.id).filter_by(
            instructor_id="inst_001"
        ).one()[0]


def _add_owner(
    suffix: str,
    *,
    session=None,
) -> tuple[str, str]:
    owns_session = session is None
    active_session = session or db.SessionLocal()
    transaction = active_session.begin() if owns_session else None
    try:
        instructor_id = f"inst_oauth_repository_{suffix}"
        account_id = f"acct_oauth_repository_{suffix}"
        active_session.add(
            InstructorDB(
                id=instructor_id,
                name=f"OAuth Repository {suffix}",
                main_address="1 Authorization Way",
                lat=30.2,
                lng=-97.2,
                work_start_time="08:00",
                work_end_time="17:00",
                timezone="America/Chicago",
            )
        )
        active_session.add(
            ProviderAccountDB(
                id=account_id,
                instructor_id=instructor_id,
                email_normalized=f"oauth-repository-{suffix}@example.com",
                password_hash="not-used-by-repository-test",
                is_demo=False,
                session_version=1,
            )
        )
        active_session.flush()
        session_id = _add_session(
            account_id,
            f"owner-{suffix}",
            session=active_session,
        )
        if transaction is not None:
            transaction.commit()
        return account_id, session_id
    except Exception:
        if transaction is not None:
            transaction.rollback()
        raise
    finally:
        if owns_session:
            active_session.close()


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
        session_id = f"sess_oauth_repository_{suffix}"
        account_version = active_session.query(
            ProviderAccountDB.session_version
        ).filter_by(id=account_id).one()[0]
        active_session.add(
            ProviderSessionDB(
                id=session_id,
                account_id=account_id,
                token_digest=hashlib.sha256(
                    f"session-token-{suffix}".encode()
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


def _store_state(
    account_id: str,
    session_id: str,
    raw_state: str,
    *,
    created_at: datetime.datetime = T0 - datetime.timedelta(minutes=1),
    expires_at: datetime.datetime = T0 + datetime.timedelta(minutes=9),
) -> None:
    with db.SessionLocal.begin() as session:
        session.add(
            ProviderCalendarOAuthStateDB(
                state_digest=_digest(raw_state),
                account_id=account_id,
                session_id=session_id,
                provider="google",
                created_at=created_at,
                expires_at=expires_at,
            )
        )


def _stored_states():
    with db.SessionLocal() as session:
        return session.query(ProviderCalendarOAuthStateDB).order_by(
            ProviderCalendarOAuthStateDB.account_id,
            ProviderCalendarOAuthStateDB.session_id,
        ).all()


def _digest(raw_state: str) -> bytes:
    return hashlib.sha256(raw_state.encode("ascii")).digest()


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
    rendered = "".join(traceback.format_exception(error))
    for value in forbidden:
        assert value not in str(error)
        assert value not in repr(error)
        assert value not in rendered
