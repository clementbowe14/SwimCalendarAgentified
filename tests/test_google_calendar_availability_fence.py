from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import datetime
import pickle
from threading import Event
import time

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from app import config, db, google_calendar_availability_fence as fence_module
from app.calendar_connections import (
    disconnect_google_calendar,
    mark_google_calendar_authorization_required,
    replace_google_calendar_connection,
)
from app.calendar_credentials import CalendarCredentialCipher
from app.google_calendar_availability_fence import (
    GoogleCalendarAvailabilityFence,
    GoogleCalendarAvailabilityFenceError,
    claim_google_calendar_availability_fence,
    revalidate_google_calendar_availability_fence,
)
from app.models import (
    InstructorDB,
    ProviderAccountDB,
    ProviderCalendarConnectionDB,
)


UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
CIPHER = CalendarCredentialCipher(
    active_key_id="availability-fence-primary",
    keys={"availability-fence-primary": b"f" * 32},
)
_PURE_TEST_ENGINE = create_engine("sqlite+pysqlite:///:memory:")


class TrackingSession(Session):
    def __init__(self, events, *, bind=_PURE_TEST_ENGINE, close_error=None):
        super().__init__(bind=bind)
        self.events = events
        self.close_error = close_error
        self.was_closed = False

    def close(self):
        self.events.append("close")
        self.was_closed = True
        super().close()
        if self.close_error is not None:
            raise self.close_error


class RollbackFailureTransaction:
    def __init__(self, transaction, rollback_error):
        self.transaction = transaction
        self.rollback_error = rollback_error

    def __enter__(self):
        self.transaction.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.transaction.commit()
            return False
        self.transaction.rollback()
        raise self.rollback_error


class RollbackFailureSession(TrackingSession):
    def __init__(self, events, *, rollback_error):
        super().__init__(events)
        self.rollback_error = rollback_error

    def begin(self, *args, **kwargs):
        return RollbackFailureTransaction(
            super().begin(*args, **kwargs),
            self.rollback_error,
        )


def _add_owner(suffix: str) -> tuple[str, str]:
    instructor_id = f"inst_availability_fence_{suffix}"
    account_id = f"acct_availability_fence_{suffix}"
    with db.SessionLocal.begin() as session:
        session.add(
            InstructorDB(
                id=instructor_id,
                name=f"Availability Fence {suffix}",
                main_address="1 Fence Way",
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
                email_normalized=f"availability-fence-{suffix}@example.com",
                password_hash="not-used-by-availability-fence-test",
                is_demo=False,
                session_version=1,
            )
        )
    return account_id, instructor_id


def _connect(
    account_id: str,
    *,
    token: str = "availability-fence-refresh-private",
    now: datetime.datetime = T0,
) -> None:
    with db.SessionLocal.begin() as session:
        replace_google_calendar_connection(
            session,
            account_id=account_id,
            refresh_token=token,
            cipher=CIPHER,
            now=now,
        )


def _connection_id(account_id: str) -> str:
    with db.SessionLocal() as session:
        value = session.query(ProviderCalendarConnectionDB.id).filter_by(
            account_id=account_id
        ).scalar()
    assert isinstance(value, str)
    return value


def _mark_authorization_required(account_id: str) -> None:
    with db.SessionLocal.begin() as session:
        mark_google_calendar_authorization_required(
            session,
            account_id=account_id,
            expected_connection_id=_connection_id(account_id),
            now=T0 + datetime.timedelta(minutes=1),
        )


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    pending = [error]
    seen: set[int] = set()
    result: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return tuple(result)


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


@pytest.mark.parametrize(
    "state",
    ["disconnected", "connected", "authorization_required"],
)
def test_claims_and_revalidates_credential_free_states(
    migrated_postgres_db,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    account_id, instructor_id = _add_owner(state)
    if state != "disconnected":
        _connect(account_id)
    if state == "authorization_required":
        _mark_authorization_required(account_id)
    loader_calls: list[str] = []

    def forbidden_loader():
        loader_calls.append("secret")
        raise AssertionError("lifecycle fences must not load secrets")

    monkeypatch.setattr(
        config,
        "google_calendar_token_exchange_configuration_from_env",
        forbidden_loader,
    )
    monkeypatch.setattr(
        config,
        "calendar_credential_cipher_from_env",
        forbidden_loader,
    )

    fence = claim_google_calendar_availability_fence(
        db.SessionLocal,
        instructor_id=instructor_id,
    )
    assert type(fence) is GoogleCalendarAvailabilityFence
    assert fence.state == state
    assert repr(fence) == "GoogleCalendarAvailabilityFence(<redacted>)"
    assert str(fence) == repr(fence)
    revalidate_google_calendar_availability_fence(
        db.SessionLocal,
        fence=fence,
    )
    assert loader_calls == []


def test_fence_is_immutable_nonserializable_and_not_directly_constructible(
    migrated_postgres_db,
) -> None:
    _account_id, instructor_id = _add_owner("sealed")
    fence = claim_google_calendar_availability_fence(
        db.SessionLocal,
        instructor_id=instructor_id,
    )

    with pytest.raises(AttributeError):
        fence.state = "connected"
    with pytest.raises(AttributeError):
        del fence.state
    with pytest.raises(TypeError):
        pickle.dumps(fence)
    with pytest.raises(TypeError):
        GoogleCalendarAvailabilityFence(
            state="disconnected",
            account_id="account",
            instructor_id="instructor",
            revision=0,
            connection_id=None,
        )


def test_fence_selects_no_credential_columns_and_locks_in_order(
    migrated_postgres_db,
) -> None:
    account_id, instructor_id = _add_owner("projection")
    _connect(account_id)
    statements: list[str] = []

    def capture(_connection, _cursor, statement, *_args):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select ") and (
            " from provider_accounts " in normalized
            or " from provider_calendar_connections " in normalized
        ):
            statements.append(normalized)

    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        fence = claim_google_calendar_availability_fence(
            db.SessionLocal,
            instructor_id=instructor_id,
        )
        revalidate_google_calendar_availability_fence(
            db.SessionLocal,
            fence=fence,
        )
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)

    assert len(statements) == 4
    assert [
        "provider_accounts" if " from provider_accounts " in value else "connection"
        for value in statements
    ] == ["provider_accounts", "connection"] * 2
    assert all(" for update" in statement for statement in statements)
    for statement in (statements[1], statements[3]):
        projection = statement.split(" from ", 1)[0]
        assert "provider_calendar_connections.id" in projection
        assert "provider_calendar_connections.account_id" in projection
        assert "provider_calendar_connections.provider" in projection
        assert "authorization_required_at" in projection
        assert "credential_version" not in projection
        assert "credential_key_id" not in projection
        assert "credential_nonce" not in projection
        assert "credential_ciphertext" not in projection


@pytest.mark.parametrize(
    ("initial_state", "transition"),
    [
        ("disconnected", "connect"),
        ("disconnected", "disconnect"),
        ("connected", "replace"),
        ("connected", "disconnect"),
        ("connected", "authorization_required"),
        ("connected", "disable"),
        ("authorization_required", "replace"),
    ],
)
def test_revalidation_rejects_lifecycle_races(
    migrated_postgres_db,
    initial_state: str,
    transition: str,
) -> None:
    account_id, instructor_id = _add_owner(
        f"{initial_state}-{transition}"
    )
    if initial_state != "disconnected":
        _connect(account_id)
    if initial_state == "authorization_required":
        _mark_authorization_required(account_id)
    fence = claim_google_calendar_availability_fence(
        db.SessionLocal,
        instructor_id=instructor_id,
    )

    if transition == "connect":
        _connect(account_id)
    elif transition == "replace":
        _connect(
            account_id,
            token="replacement-refresh-private",
            now=T0 + datetime.timedelta(minutes=2),
        )
    elif transition == "disconnect":
        with db.SessionLocal.begin() as session:
            disconnect_google_calendar(session, account_id=account_id)
    elif transition == "authorization_required":
        _mark_authorization_required(account_id)
    else:
        with db.SessionLocal.begin() as session:
            account = session.get(ProviderAccountDB, account_id)
            assert account is not None
            account.disabled_at = T0 + datetime.timedelta(minutes=2)

    with pytest.raises(GoogleCalendarAvailabilityFenceError) as captured:
        revalidate_google_calendar_availability_fence(
            db.SessionLocal,
            fence=fence,
        )
    assert captured.value.reason_code == "provider_unavailable"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_lifecycle_transition_waits_behind_claim_then_invalidates_fence(
    migrated_postgres_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id, instructor_id = _add_owner("claim-first")
    _connect(account_id)
    original_read = fence_module._read_locked_fence
    claim_locked = Event()
    release_claim = Event()
    transition_started = Event()
    pids: dict[str, int] = {}

    def held_read(session, *, instructor_id, account_id):
        result = original_read(
            session,
            instructor_id=instructor_id,
            account_id=account_id,
        )
        if account_id is None:
            pids["claim"] = session.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            claim_locked.set()
            assert release_claim.wait(timeout=5)
        return result

    monkeypatch.setattr(fence_module, "_read_locked_fence", held_read)

    def disconnect_after_claim():
        with db.SessionLocal.begin() as session:
            pids["transition"] = session.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            transition_started.set()
            disconnect_google_calendar(session, account_id=account_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claim_future = executor.submit(
            claim_google_calendar_availability_fence,
            db.SessionLocal,
            instructor_id=instructor_id,
        )
        assert claim_locked.wait(timeout=5)
        transition_future = executor.submit(disconnect_after_claim)
        assert transition_started.wait(timeout=5)
        _wait_until_blocked_by(pids["transition"], pids["claim"])
        release_claim.set()
        fence = claim_future.result(timeout=5)
        transition_future.result(timeout=5)

    assert fence.state == "connected"
    with pytest.raises(GoogleCalendarAvailabilityFenceError):
        revalidate_google_calendar_availability_fence(
            db.SessionLocal,
            fence=fence,
        )


def test_claim_waits_behind_transition_and_observes_committed_state(
    migrated_postgres_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id, instructor_id = _add_owner("transition-first")
    _connect(account_id)
    original_read = fence_module._read_locked_fence
    claim_started = Event()
    pids: dict[str, int] = {}

    def observed_read(session, *, instructor_id, account_id):
        if account_id is None:
            pids["claim"] = session.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            claim_started.set()
        return original_read(
            session,
            instructor_id=instructor_id,
            account_id=account_id,
        )

    monkeypatch.setattr(fence_module, "_read_locked_fence", observed_read)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with db.SessionLocal.begin() as transition_session:
            transition_session.query(ProviderAccountDB).filter_by(
                id=account_id
            ).with_for_update().one()
            blocker_pid = transition_session.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            claim_future = executor.submit(
                claim_google_calendar_availability_fence,
                db.SessionLocal,
                instructor_id=instructor_id,
            )
            assert claim_started.wait(timeout=5)
            _wait_until_blocked_by(pids["claim"], blocker_pid)
            disconnect_google_calendar(
                transition_session,
                account_id=account_id,
            )
            # Leaving the transaction releases the account lock for the claim.
        fence = claim_future.result(timeout=5)

    assert fence.state == "disconnected"
    revalidate_google_calendar_availability_fence(
        db.SessionLocal,
        fence=fence,
    )


@pytest.mark.parametrize("owner_state", ["missing", "disabled"])
def test_missing_and_disabled_owners_fail_closed(
    migrated_postgres_db,
    owner_state: str,
) -> None:
    if owner_state == "missing":
        instructor_id = "inst_availability_fence_missing"
    else:
        account_id, instructor_id = _add_owner("disabled")
        with db.SessionLocal.begin() as session:
            account = session.get(ProviderAccountDB, account_id)
            assert account is not None
            account.disabled_at = T0

    with pytest.raises(GoogleCalendarAvailabilityFenceError) as captured:
        claim_google_calendar_availability_fence(
            db.SessionLocal,
            instructor_id=instructor_id,
        )
    assert str(captured.value) == (
        "External calendar availability could not be verified."
    )
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_invalid_sessions_fail_before_locked_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked_reads: list[bool] = []
    monkeypatch.setattr(
        fence_module,
        "_read_locked_fence",
        lambda *_args, **_kwargs: locked_reads.append(True),
    )

    active_session = Session(bind=_PURE_TEST_ENGINE)
    active_transaction = active_session.begin()
    with pytest.raises(GoogleCalendarAvailabilityFenceError):
        claim_google_calendar_availability_fence(
            lambda: active_session,
            instructor_id="inst_availability_fence_active",
        )
    active_transaction.close()

    primary = create_engine("sqlite+pysqlite:///:memory:")
    split = create_engine("sqlite+pysqlite:///:memory:")
    split_session = Session(
        bind=primary,
        binds={ProviderCalendarConnectionDB: split},
    )
    try:
        with pytest.raises(GoogleCalendarAvailabilityFenceError):
            claim_google_calendar_availability_fence(
                lambda: split_session,
                instructor_id="inst_availability_fence_split",
            )
    finally:
        split_session.close()
        primary.dispose()
        split.dispose()

    autocommit_engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with pytest.raises(GoogleCalendarAvailabilityFenceError):
            claim_google_calendar_availability_fence(
                lambda: Session(bind=autocommit_engine),
                instructor_id="inst_availability_fence_autocommit",
            )
    finally:
        autocommit_engine.dispose()

    with pytest.raises(GoogleCalendarAvailabilityFenceError):
        claim_google_calendar_availability_fence(
            lambda: object(),
            instructor_id="inst_availability_fence_not_session",
        )
    assert locked_reads == []


@pytest.mark.parametrize("stage", ["operation", "commit", "rollback", "close"])
def test_operation_and_session_cleanup_failures_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    private_failure = RuntimeError(f"{stage}-private")
    events: list[str] = []
    if stage == "rollback":
        session: TrackingSession = RollbackFailureSession(
            events,
            rollback_error=RuntimeError("rollback-cleanup-private"),
        )
    else:
        session = TrackingSession(
            events,
            close_error=(private_failure if stage == "close" else None),
        )
    if stage == "commit":

        def fail_commit(_session):
            raise private_failure

        event.listen(session, "before_commit", fail_commit)

    def locked(*_args, **_kwargs):
        if stage in ("operation", "rollback"):
            raise private_failure
        return fence_module._new_fence(
            state="disconnected",
            account_id="acct_availability_fence_cleanup",
            instructor_id="inst_availability_fence_cleanup",
            revision=0,
            connection_id=None,
        )

    monkeypatch.setattr(fence_module, "_read_locked_fence", locked)

    with pytest.raises(GoogleCalendarAvailabilityFenceError) as captured:
        claim_google_calendar_availability_fence(
            lambda: session,
            instructor_id="inst_availability_fence_cleanup",
        )

    assert session.was_closed is True
    assert private_failure not in _exception_chain(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "invalid"),
        ("_revision", -1),
        ("_connection_id", "invalid"),
        ("_account_id", ""),
        ("_instructor_id", ""),
    ],
)
def test_malformed_fences_fail_before_session_creation(
    migrated_postgres_db,
    field: str,
    value: object,
) -> None:
    _account_id, instructor_id = _add_owner(f"malformed-{field}")
    fence = claim_google_calendar_availability_fence(
        db.SessionLocal,
        instructor_id=instructor_id,
    )
    object.__setattr__(fence, field, value)
    factory_calls: list[bool] = []

    def forbidden_factory():
        factory_calls.append(True)
        raise AssertionError("malformed fences must fail before sessions")

    with pytest.raises(GoogleCalendarAvailabilityFenceError) as captured:
        revalidate_google_calendar_availability_fence(
            forbidden_factory,
            fence=fence,
        )

    assert factory_calls == []
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_ordinary_failures_are_fresh_and_retain_no_private_chain() -> None:
    private_marker = object()
    original = RuntimeError(private_marker)

    def fail():
        raise original

    with pytest.raises(GoogleCalendarAvailabilityFenceError) as captured:
        claim_google_calendar_availability_fence(
            fail,
            instructor_id="inst_availability_fence_private",
        )

    assert original not in _exception_chain(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True
    current = captured.value.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith(
            "/app/google_calendar_availability_fence.py"
        ):
            assert original not in current.tb_frame.f_locals.values()
            assert private_marker not in current.tb_frame.f_locals.values()
        current = current.tb_next


@pytest.mark.parametrize(
    ("raised", "expected_type", "expected_code"),
    [
        (asyncio.CancelledError("cancel-private"), asyncio.CancelledError, None),
        (KeyboardInterrupt("keyboard-private"), KeyboardInterrupt, None),
        (SystemExit("system-private"), SystemExit, 1),
        (GeneratorExit("generator-private"), GeneratorExit, None),
    ],
)
def test_process_controls_are_fresh_and_cause_free(
    raised: BaseException,
    expected_type: type[BaseException],
    expected_code: int | None,
) -> None:
    def interrupt():
        raise raised

    with pytest.raises(expected_type) as captured:
        claim_google_calendar_availability_fence(
            interrupt,
            instructor_id="inst_availability_fence_control",
        )

    assert captured.value is not raised
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True
    assert "private" not in str(captured.value)
    if expected_type is SystemExit:
        assert captured.value.code == expected_code


def test_session_cleanup_failure_does_not_mask_active_cancellation() -> None:
    original = asyncio.CancelledError("control-private")

    class MaskingSession(Session):
        def get_bind(self, *args, **kwargs):
            del args, kwargs
            raise original

        def close(self):
            raise RuntimeError("cleanup-private")

    with pytest.raises(asyncio.CancelledError) as captured:
        claim_google_calendar_availability_fence(
            MaskingSession,
            instructor_id="inst_availability_fence_masked_control",
        )

    assert captured.value is not original
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True
    assert "private" not in str(captured.value)
