from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import inspect
import json
import os
import pickle
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from app import db, google_calendar_oauth_callback_claim
from app.calendar_connections import (
    disconnect_google_calendar,
    replace_google_calendar_connection,
)
from app.calendar_credentials import CalendarCredentialCipher
from app.calendar_oauth_pkce import (
    CalendarOAuthPKCECipher,
    derive_s256_code_challenge,
)
from app.calendar_oauth_states import (
    ConsumedGoogleCalendarOAuthPKCEState,
    issue_google_calendar_oauth_state_with_pkce_and_browser_binding,
)
from app.google_calendar_oauth import GoogleCalendarOAuthError
from app.google_calendar_oauth_callback_claim import (
    GoogleCalendarOAuthCallbackClaim,
    GoogleCalendarOAuthCallbackClaimError,
    claim_google_calendar_oauth_callback,
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
T0 = datetime.datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
POSTGRESQL_BIGINT_MAX = 9_223_372_036_854_775_807
ACCOUNT_ID = "acct_callback_claim_private"
SESSION_ID = "sess_callback_claim_private"
ROOT_KEY = b"p" * 32
PKCE_CIPHER = CalendarOAuthPKCECipher(
    active_key_id="callback-claim-key",
    root_keys={"callback-claim-key": ROOT_KEY},
)
CREDENTIAL_CIPHER = CalendarCredentialCipher(
    active_key_id="callback-credential-key",
    keys={"callback-credential-key": b"c" * 32},
)
_PURE_TEST_ENGINE = create_engine("sqlite+pysqlite:///:memory:")


def _canonical(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


RAW_STATE = _canonical(b"s" * 32)
RAW_BROWSER_BINDING = _canonical(b"b" * 32)
CODE_VERIFIER = _canonical(b"v" * 32)


class TextSubclass(str):
    pass


class TrackingSession(Session):
    def __init__(self, events, *, close_error=None):
        super().__init__(bind=_PURE_TEST_ENGINE)
        self.events = events
        self.close_error = close_error
        self.was_closed = False

    def close(self):
        self.events.append("close")
        self.was_closed = True
        super().close()
        if self.close_error is not None:
            raise self.close_error


def _consumed(
    *,
    account_id=ACCOUNT_ID,
    session_id=SESSION_ID,
    code_verifier=CODE_VERIFIER,
):
    return ConsumedGoogleCalendarOAuthPKCEState(
        account_id=account_id,
        session_id=session_id,
        code_verifier=code_verifier,
    )


def _recording_factory(events, *, commit_error=None, close_error=None):
    sessions = []

    def factory():
        events.append("factory")
        session = TrackingSession(events, close_error=close_error)
        sessions.append(session)
        event.listen(
            session,
            "after_commit",
            lambda _session: events.append("commit"),
        )
        event.listen(
            session,
            "after_rollback",
            lambda _session: events.append("rollback"),
        )
        if commit_error is not None:

            def fail_commit(_session):
                events.append("commit_attempt")
                raise commit_error

            event.listen(session, "before_commit", fail_commit)
        return session

    return factory, sessions


def _install_success_dependencies(monkeypatch, events, sessions):
    def consume(
        session,
        *,
        raw_state,
        raw_browser_binding,
        pkce_cipher,
        now,
    ):
        events.append("consume")
        assert session.in_transaction()
        assert raw_state == RAW_STATE
        assert raw_browser_binding == RAW_BROWSER_BINDING
        assert type(pkce_cipher) is CalendarOAuthPKCECipher
        assert now == T0
        return _consumed()

    def advance(session, *, account_id):
        events.append("advance")
        assert session.in_transaction()
        assert account_id == ACCOUNT_ID
        return 7

    real_builder = google_calendar_oauth_callback_claim._new_claim

    def build(**values):
        events.append("build")
        assert sessions[0].was_closed is True
        assert sessions[0].in_transaction() is False
        return real_builder(**values)

    monkeypatch.setattr(
        google_calendar_oauth_callback_claim,
        "consume_google_calendar_oauth_state_with_pkce_and_browser_binding",
        consume,
    )
    monkeypatch.setattr(
        google_calendar_oauth_callback_claim,
        "_advance_claim_revision",
        advance,
    )
    monkeypatch.setattr(
        google_calendar_oauth_callback_claim,
        "_new_claim",
        build,
    )


def _claim(session_factory, **overrides):
    values = {
        "raw_state": RAW_STATE,
        "raw_browser_binding": RAW_BROWSER_BINDING,
        "pkce_cipher": PKCE_CIPHER,
        "now": T0,
    }
    values.update(overrides)
    return claim_google_calendar_oauth_callback(session_factory, **values)


def _module_traceback(error):
    names = []
    values = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith(
            "/app/google_calendar_oauth_callback_claim.py"
        ):
            names.append(current.tb_frame.f_code.co_name)
            values.extend(current.tb_frame.f_locals.values())
        current = current.tb_next
    return names, repr(values)


def _formatted_exception(error):
    return "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )


def test_public_surface_commits_and_closes_before_return(monkeypatch):
    assert google_calendar_oauth_callback_claim.__all__ == (
        "GoogleCalendarOAuthCallbackClaim",
        "GoogleCalendarOAuthCallbackClaimError",
        "claim_google_calendar_oauth_callback",
    )
    assert issubclass(GoogleCalendarOAuthCallbackClaimError, GoogleCalendarOAuthError)
    signature = inspect.signature(claim_google_calendar_oauth_callback)
    assert list(signature.parameters) == [
        "session_factory",
        "raw_state",
        "raw_browser_binding",
        "pkce_cipher",
        "now",
    ]
    assert signature.parameters["session_factory"].kind is (
        inspect.Parameter.POSITIONAL_OR_KEYWORD
    )
    assert all(
        signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        for name in (
            "raw_state",
            "raw_browser_binding",
            "pkce_cipher",
            "now",
        )
    )

    events = []
    session_factory, sessions = _recording_factory(events)
    _install_success_dependencies(monkeypatch, events, sessions)

    result = _claim(session_factory)

    assert type(result) is GoogleCalendarOAuthCallbackClaim
    assert result.account_id == ACCOUNT_ID
    assert result.session_id == SESSION_ID
    assert result.code_verifier == CODE_VERIFIER
    assert result.expected_calendar_connection_revision == 7
    assert events == [
        "factory",
        "consume",
        "advance",
        "commit",
        "close",
        "build",
    ]


def test_claim_handoff_is_builder_only_immutable_and_nonserializable(monkeypatch):
    events = []
    session_factory, sessions = _recording_factory(events)
    _install_success_dependencies(monkeypatch, events, sessions)
    result = _claim(session_factory)

    assert not dataclasses.is_dataclass(result)
    assert not hasattr(result, "__dict__")
    assert repr(result) == "GoogleCalendarOAuthCallbackClaim(<redacted>)"
    assert str(result) == repr(result)
    for private in (ACCOUNT_ID, SESSION_ID, CODE_VERIFIER):
        assert private not in repr(result)
    with pytest.raises(AttributeError):
        result.code_verifier = "replacement"
    with pytest.raises(AttributeError):
        del result.account_id
    with pytest.raises(TypeError):
        type("UnsafeClaim", (GoogleCalendarOAuthCallbackClaim,), {})
    for serializer in (
        dataclasses.asdict,
        dataclasses.astuple,
        vars,
        pickle.dumps,
        json.dumps,
        jsonable_encoder,
    ):
        with pytest.raises((TypeError, ValueError)):
            serializer(result)

    with pytest.raises(GoogleCalendarOAuthCallbackClaimError) as raised:
        GoogleCalendarOAuthCallbackClaim(
            account_id=ACCOUNT_ID,
            session_id=SESSION_ID,
            code_verifier=CODE_VERIFIER,
            expected_calendar_connection_revision=7,
        )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    names, values = _module_traceback(raised.value)
    assert names == ["__init__", "_raise_claim_error"]
    for private in (ACCOUNT_ID, SESSION_ID, CODE_VERIFIER):
        assert private not in values


def test_invalid_inputs_fail_before_session_creation():
    factory_calls = []

    def forbidden_factory():
        factory_calls.append(True)
        raise AssertionError("session creation must not occur")

    cases = (
        {"raw_state": None},
        {"raw_state": "bad"},
        {"raw_state": RAW_STATE + "="},
        {"raw_state": TextSubclass(RAW_STATE)},
        {"raw_browser_binding": None},
        {"raw_browser_binding": "bad"},
        {"raw_browser_binding": RAW_STATE},
        {"raw_browser_binding": TextSubclass(RAW_BROWSER_BINDING)},
        {"pkce_cipher": object()},
        {"now": datetime.datetime(2026, 11, 16, 12, 0)},
        {"now": "2026-11-16T12:00:00Z"},
    )

    for overrides in cases:
        with pytest.raises(GoogleCalendarOAuthCallbackClaimError):
            _claim(forbidden_factory, **overrides)

    with pytest.raises(GoogleCalendarOAuthCallbackClaimError):
        _claim(None)
    assert factory_calls == []


def test_owned_session_and_single_engine_are_required(monkeypatch):
    consume_calls = []
    monkeypatch.setattr(
        google_calendar_oauth_callback_claim,
        "consume_google_calendar_oauth_state_with_pkce_and_browser_binding",
        lambda *_args, **_kwargs: consume_calls.append(True),
    )

    events = []
    active_session = TrackingSession(events)
    transaction = active_session.begin()
    with pytest.raises(GoogleCalendarOAuthCallbackClaimError):
        _claim(lambda: active_session)
    assert active_session.was_closed is True
    transaction.close()

    engine = create_engine("sqlite+pysqlite:///:memory:")
    connection = engine.connect()
    outer_transaction = connection.begin()
    external_session = Session(bind=connection)
    try:
        with pytest.raises(GoogleCalendarOAuthCallbackClaimError):
            _claim(lambda: external_session)
        assert outer_transaction.is_active is True
    finally:
        outer_transaction.rollback()
        connection.close()
        engine.dispose()

    primary = create_engine("sqlite+pysqlite:///:memory:")
    split = create_engine("sqlite+pysqlite:///:memory:")
    split_session = Session(
        bind=primary,
        binds={ProviderSessionDB: split},
    )
    try:
        with pytest.raises(GoogleCalendarOAuthCallbackClaimError):
            _claim(lambda: split_session)
    finally:
        primary.dispose()
        split.dispose()

    with pytest.raises(GoogleCalendarOAuthCallbackClaimError):
        _claim(lambda: object())
    assert consume_calls == []


@pytest.mark.parametrize(
    "consumed",
    [
        object(),
        _consumed(account_id=""),
        _consumed(account_id=" acct"),
        _consumed(account_id="café"),
        _consumed(session_id="session\nprivate"),
        _consumed(code_verifier="bad"),
        _consumed(code_verifier=TextSubclass(CODE_VERIFIER)),
    ],
)
def test_malformed_consumed_snapshot_rolls_back(monkeypatch, consumed):
    events = []
    session_factory, sessions = _recording_factory(events)
    monkeypatch.setattr(
        google_calendar_oauth_callback_claim,
        "consume_google_calendar_oauth_state_with_pkce_and_browser_binding",
        lambda *_args, **_kwargs: (events.append("consume") or consumed),
    )
    advance_calls = []
    monkeypatch.setattr(
        google_calendar_oauth_callback_claim,
        "_advance_claim_revision",
        lambda *_args, **_kwargs: advance_calls.append(True),
    )

    with pytest.raises(GoogleCalendarOAuthCallbackClaimError):
        _claim(session_factory)

    assert sessions[0].was_closed is True
    assert events == ["factory", "consume", "rollback", "close"]
    assert advance_calls == []


def test_commit_and_close_failures_return_no_handoff(monkeypatch):
    events = []
    session_factory, sessions = _recording_factory(
        events,
        commit_error=RuntimeError("commit-private-detail"),
    )
    _install_success_dependencies(monkeypatch, events, sessions)
    build_calls = []
    monkeypatch.setattr(
        google_calendar_oauth_callback_claim,
        "_new_claim",
        lambda **_kwargs: build_calls.append(True),
    )

    with pytest.raises(GoogleCalendarOAuthCallbackClaimError):
        _claim(session_factory)
    assert sessions[0].was_closed is True
    assert "commit_attempt" in events
    assert build_calls == []

    events = []
    session_factory, sessions = _recording_factory(
        events,
        close_error=RuntimeError("close-private-detail"),
    )
    _install_success_dependencies(monkeypatch, events, sessions)
    monkeypatch.setattr(
        google_calendar_oauth_callback_claim,
        "_new_claim",
        lambda **_kwargs: build_calls.append(True),
    )

    with pytest.raises(GoogleCalendarOAuthCallbackClaimError):
        _claim(session_factory)
    assert events == [
        "factory",
        "consume",
        "advance",
        "commit",
        "close",
    ]
    assert build_calls == []


def test_failures_have_fixed_secret_free_diagnostics(monkeypatch, caplog):
    events = []
    session_factory, _ = _recording_factory(events)
    private_detail = "callback-claim-internal-private-detail"

    def fail_consume(*_args, **_kwargs):
        raise RuntimeError(
            " ".join(
                (
                    RAW_STATE,
                    RAW_BROWSER_BINDING,
                    CODE_VERIFIER,
                    ACCOUNT_ID,
                    SESSION_ID,
                    ROOT_KEY.decode("ascii"),
                    private_detail,
                )
            )
        )

    monkeypatch.setattr(
        google_calendar_oauth_callback_claim,
        "consume_google_calendar_oauth_state_with_pkce_and_browser_binding",
        fail_consume,
    )

    with pytest.raises(GoogleCalendarOAuthCallbackClaimError) as raised:
        _claim(session_factory)

    assert str(raised.value) == (
        "Google Calendar authorization could not be claimed."
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, frame_values = _module_traceback(raised.value)
    assert names == ["claim_google_calendar_oauth_callback", "_raise_claim_error"]
    output = " ".join(
        (
            str(raised.value),
            repr(raised.value),
            _formatted_exception(raised.value),
            frame_values,
            caplog.text,
        )
    )
    for private in (
        RAW_STATE,
        RAW_BROWSER_BINDING,
        CODE_VERIFIER,
        ACCOUNT_ID,
        SESSION_ID,
        ROOT_KEY.decode("ascii"),
        private_detail,
    ):
        assert private not in output


def test_ambient_failure_context_is_cleared():
    ambient_secret = "ambient-callback-claim-private-detail"
    try:
        raise RuntimeError(ambient_secret)
    except RuntimeError:
        with pytest.raises(GoogleCalendarOAuthCallbackClaimError) as raised:
            _claim(None)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    _, frame_values = _module_traceback(raised.value)
    assert ambient_secret not in " ".join(
        (_formatted_exception(raised.value), frame_values)
    )


def test_clean_import_and_application_startup_are_inert():
    project_root = Path(__file__).resolve().parents[1]
    child_environment = os.environ.copy()
    child_environment["SWIM_ENV"] = "test"
    script = "\n".join(
        (
            "calls = []",
            "import app.calendar_oauth_states as oauth_states",
            "def forbidden(*_args, **_kwargs):",
            "    calls.append(True)",
            (
                "    raise AssertionError("
                "'callback claim must remain call-time only')"
            ),
            (
                "oauth_states."
                "consume_google_calendar_oauth_state_with_pkce_and_browser_binding "
                "= forbidden"
            ),
            (
                "import app.google_calendar_oauth_callback_claim "
                "as callback_claim"
            ),
            "callback_claim._advance_claim_revision = forbidden",
            "from app.api import create_app",
            "app = create_app()",
            (
                "assert not hasattr("
                "app.state, 'google_calendar_oauth_callback_claim')"
            ),
            (
                "assert 'google_calendar_oauth_callback_claim' "
                "not in app.state.routing_readiness"
            ),
            "assert 'httpx' not in callback_claim.__dict__",
            "assert calls == [], calls",
        )
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=child_environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def _add_owner(suffix: str) -> tuple[str, str]:
    instructor_id = f"inst_callback_claim_{suffix}"
    account_id = f"acct_callback_claim_{suffix}"
    session_id = f"sess_callback_claim_{suffix}"
    with db.SessionLocal.begin() as session:
        session.add(
            InstructorDB(
                id=instructor_id,
                name=f"Callback Claim {suffix}",
                main_address="1 Test Lane",
                lat=41.0,
                lng=-72.0,
                timezone="America/New_York",
            )
        )
        session.add(
            ProviderAccountDB(
                id=account_id,
                instructor_id=instructor_id,
                email_normalized=f"callback-{suffix}@example.com",
                password_hash="unused-test-password-hash",
                session_version=1,
                created_at=T0 - datetime.timedelta(hours=1),
                updated_at=T0 - datetime.timedelta(hours=1),
            )
        )
        session.flush()
        session.add(
            ProviderSessionDB(
                id=session_id,
                account_id=account_id,
                token_digest=hashlib.sha256(
                    f"callback-session-{suffix}".encode("ascii")
                ).digest(),
                session_version=1,
                created_at=T0 - datetime.timedelta(minutes=1),
                expires_at=T0 + datetime.timedelta(hours=1),
            )
        )
    return account_id, session_id


def _add_session(account_id: str, suffix: str) -> str:
    session_id = f"sess_callback_claim_{suffix}"
    with db.SessionLocal.begin() as session:
        account = session.get(ProviderAccountDB, account_id)
        assert account is not None
        session.add(
            ProviderSessionDB(
                id=session_id,
                account_id=account_id,
                token_digest=hashlib.sha256(
                    f"callback-extra-session-{suffix}".encode("ascii")
                ).digest(),
                session_version=account.session_version,
                created_at=T0 - datetime.timedelta(minutes=1),
                expires_at=T0 + datetime.timedelta(hours=1),
            )
        )
    return session_id


def _issue(account_id: str, session_id: str):
    with db.SessionLocal.begin() as session:
        return issue_google_calendar_oauth_state_with_pkce_and_browser_binding(
            session,
            account_id=account_id,
            session_id=session_id,
            pkce_cipher=PKCE_CIPHER,
            now=T0,
        )


def _claim_issued(issued):
    return claim_google_calendar_oauth_callback(
        db.SessionLocal,
        raw_state=issued.issued_state.raw_state,
        raw_browser_binding=issued.raw_browser_binding,
        pkce_cipher=PKCE_CIPHER,
        now=T0 + datetime.timedelta(minutes=1),
    )


def _state_digest(issued) -> bytes:
    return hashlib.sha256(
        issued.issued_state.raw_state.encode("ascii")
    ).digest()


def _stored_revision(account_id: str) -> int:
    with db.SessionLocal() as session:
        account = session.get(ProviderAccountDB, account_id)
        assert account is not None
        return account.calendar_connection_revision


def _stored_oauth_pair(state_digest: bytes) -> tuple[bool, bool]:
    with db.SessionLocal() as session:
        return (
            session.get(ProviderCalendarOAuthStateDB, state_digest) is not None,
            session.get(ProviderCalendarOAuthPKCEVerifierDB, state_digest)
            is not None,
        )


def _stored_connection(account_id: str):
    with db.SessionLocal() as session:
        row = session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=account_id
        ).one_or_none()
        if row is None:
            return None
        return (
            row.id,
            row.credential_version,
            row.credential_key_id,
            bytes(row.credential_nonce),
            bytes(row.credential_ciphertext),
            row.authorization_required_at,
            row.created_at,
            row.updated_at,
        )


def test_real_claim_commits_pair_revision_and_preserves_connection_and_tenant(
    migrated_postgres_db,
):
    account_id, session_id = _add_owner("success")
    other_account_id, other_session_id = _add_owner("other")
    with db.SessionLocal.begin() as session:
        replace_google_calendar_connection(
            session,
            account_id=account_id,
            refresh_token="existing-callback-connection",
            cipher=CREDENTIAL_CIPHER,
            now=T0 - datetime.timedelta(minutes=2),
        )
    original_connection = _stored_connection(account_id)
    issued = _issue(account_id, session_id)
    other_issued = _issue(other_account_id, other_session_id)
    digest = _state_digest(issued)
    other_digest = _state_digest(other_issued)

    claim = _claim_issued(issued)

    assert claim.account_id == account_id
    assert claim.session_id == session_id
    assert claim.expected_calendar_connection_revision == 2
    assert derive_s256_code_challenge(claim.code_verifier) == (
        issued.issued_state.code_challenge
    )
    assert _stored_oauth_pair(digest) == (False, False)
    assert _stored_revision(account_id) == 2
    assert _stored_connection(account_id) == original_connection
    assert _stored_oauth_pair(other_digest) == (True, True)
    assert _stored_revision(other_account_id) == 0

    with pytest.raises(GoogleCalendarOAuthCallbackClaimError):
        _claim_issued(issued)
    assert _stored_revision(account_id) == 2


@pytest.mark.parametrize(
    ("initial_revision", "succeeds"),
    [
        (POSTGRESQL_BIGINT_MAX - 1, True),
        (POSTGRESQL_BIGINT_MAX, False),
    ],
)
def test_revision_boundary_commits_or_restores_pair(
    migrated_postgres_db,
    initial_revision,
    succeeds,
):
    account_id, session_id = _add_owner(f"revision-{succeeds}")
    issued = _issue(account_id, session_id)
    digest = _state_digest(issued)
    with db.SessionLocal.begin() as session:
        session.get(
            ProviderAccountDB,
            account_id,
        ).calendar_connection_revision = initial_revision

    if succeeds:
        claim = _claim_issued(issued)
        assert claim.expected_calendar_connection_revision == (
            POSTGRESQL_BIGINT_MAX
        )
        assert _stored_oauth_pair(digest) == (False, False)
        assert _stored_revision(account_id) == POSTGRESQL_BIGINT_MAX
    else:
        with pytest.raises(GoogleCalendarOAuthCallbackClaimError):
            _claim_issued(issued)
        assert _stored_oauth_pair(digest) == (True, True)
        assert _stored_revision(account_id) == POSTGRESQL_BIGINT_MAX


def test_real_commit_failure_restores_pair_and_revision(migrated_postgres_db):
    account_id, session_id = _add_owner("commit-failure")
    issued = _issue(account_id, session_id)
    digest = _state_digest(issued)

    def factory():
        session = db.SessionLocal()

        def fail_commit(_session):
            raise RuntimeError("private commit failure")

        event.listen(session, "before_commit", fail_commit, once=True)
        return session

    with pytest.raises(GoogleCalendarOAuthCallbackClaimError):
        claim_google_calendar_oauth_callback(
            factory,
            raw_state=issued.issued_state.raw_state,
            raw_browser_binding=issued.raw_browser_binding,
            pkce_cipher=PKCE_CIPHER,
            now=T0 + datetime.timedelta(minutes=1),
        )

    assert _stored_oauth_pair(digest) == (True, True)
    assert _stored_revision(account_id) == 0


@pytest.mark.parametrize("inactive", ["revoked", "version", "disabled"])
def test_inactive_owner_fails_without_mutation(migrated_postgres_db, inactive):
    account_id, session_id = _add_owner(f"inactive-{inactive}")
    issued = _issue(account_id, session_id)
    digest = _state_digest(issued)
    with db.SessionLocal.begin() as session:
        account = session.get(ProviderAccountDB, account_id)
        provider_session = session.get(ProviderSessionDB, session_id)
        if inactive == "revoked":
            provider_session.revoked_at = T0
        elif inactive == "version":
            account.session_version += 1
        else:
            account.disabled_at = T0

    with pytest.raises(GoogleCalendarOAuthCallbackClaimError):
        _claim_issued(issued)

    assert _stored_oauth_pair(digest) == (True, True)
    assert _stored_revision(account_id) == 0


def _run_overlapping_claims(monkeypatch, first_issued, second_issued):
    first_ready = threading.Event()
    allow_first_commit = threading.Event()
    second_backend_ready = threading.Event()
    backend_pids = []
    backend_pids_lock = threading.Lock()
    real_consume = (
        google_calendar_oauth_callback_claim
        .consume_google_calendar_oauth_state_with_pkce_and_browser_binding
    )
    real_advance = google_calendar_oauth_callback_claim._advance_claim_revision

    def tracked_consume(session, **kwargs):
        backend_pid = session.execute(
            text("SELECT pg_backend_pid()")
        ).scalar_one()
        with backend_pids_lock:
            backend_pids.append(backend_pid)
            if len(backend_pids) == 2:
                second_backend_ready.set()
        return real_consume(session, **kwargs)

    def hold_first_claim(session, *, account_id):
        revision = real_advance(session, account_id=account_id)
        if not first_ready.is_set():
            first_ready.set()
            assert allow_first_commit.wait(timeout=5)
        return revision

    monkeypatch.setattr(
        google_calendar_oauth_callback_claim,
        "consume_google_calendar_oauth_state_with_pkce_and_browser_binding",
        tracked_consume,
    )
    monkeypatch.setattr(
        google_calendar_oauth_callback_claim,
        "_advance_claim_revision",
        hold_first_claim,
    )

    def attempt(issued):
        try:
            return _claim_issued(issued)
        except GoogleCalendarOAuthCallbackClaimError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(attempt, first_issued)
        assert first_ready.wait(timeout=5)
        try:
            second_future = executor.submit(attempt, second_issued)
            assert second_backend_ready.wait(timeout=5)
            _wait_until_blocked_by(backend_pids[1], backend_pids[0])
        finally:
            allow_first_commit.set()
        return (
            first_future.result(timeout=10),
            second_future.result(timeout=10),
        )


def test_concurrent_same_state_has_one_claim_and_one_increment(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, session_id = _add_owner("same-state-race")
    issued = _issue(account_id, session_id)
    results = _run_overlapping_claims(monkeypatch, issued, issued)

    claims = [value for value in results if value is not None]
    assert len(claims) == 1
    assert claims[0].expected_calendar_connection_revision == 1
    assert _stored_revision(account_id) == 1
    assert _stored_oauth_pair(_state_digest(issued)) == (False, False)


def test_different_states_for_one_account_receive_consecutive_revisions(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, first_session_id = _add_owner("sibling-state-race")
    second_session_id = _add_session(account_id, "sibling-state-race-second")
    first = _issue(account_id, first_session_id)
    second = _issue(account_id, second_session_id)

    claims = _run_overlapping_claims(monkeypatch, first, second)

    assert {
        claim.expected_calendar_connection_revision for claim in claims
    } == {1, 2}
    assert {claim.session_id for claim in claims} == {
        first_session_id,
        second_session_id,
    }
    assert _stored_revision(account_id) == 2
    assert _stored_oauth_pair(_state_digest(first)) == (False, False)
    assert _stored_oauth_pair(_state_digest(second)) == (False, False)


@pytest.mark.parametrize("first_operation", ["claim", "lifecycle"])
@pytest.mark.parametrize("lifecycle_operation", ["replace", "disconnect"])
def test_claim_and_lifecycle_serialize_on_account_lock(
    migrated_postgres_db,
    monkeypatch,
    first_operation,
    lifecycle_operation,
):
    account_id, session_id = _add_owner(
        f"{first_operation}-{lifecycle_operation}"
    )
    issued = _issue(account_id, session_id)
    first_ready = threading.Event()
    allow_first_commit = threading.Event()
    claim_backend_ready = threading.Event()
    lifecycle_backend_ready = threading.Event()
    claim_backend_pid = []
    lifecycle_backend_pid = []
    real_consume = (
        google_calendar_oauth_callback_claim
        .consume_google_calendar_oauth_state_with_pkce_and_browser_binding
    )
    real_advance = google_calendar_oauth_callback_claim._advance_claim_revision

    def tracked_consume(session, **kwargs):
        claim_backend_pid.append(
            session.execute(text("SELECT pg_backend_pid()")).scalar_one()
        )
        claim_backend_ready.set()
        return real_consume(session, **kwargs)

    def tracked_advance(session, *, account_id):
        revision = real_advance(session, account_id=account_id)
        if first_operation == "claim":
            first_ready.set()
            assert allow_first_commit.wait(timeout=5)
        return revision

    monkeypatch.setattr(
        google_calendar_oauth_callback_claim,
        "consume_google_calendar_oauth_state_with_pkce_and_browser_binding",
        tracked_consume,
    )
    monkeypatch.setattr(
        google_calendar_oauth_callback_claim,
        "_advance_claim_revision",
        tracked_advance,
    )

    def run_claim():
        try:
            return _claim_issued(issued)
        except GoogleCalendarOAuthCallbackClaimError:
            return "invalid"

    def run_lifecycle(*, wait_before_commit):
        with db.SessionLocal.begin() as session:
            lifecycle_backend_pid.append(
                session.execute(text("SELECT pg_backend_pid()")).scalar_one()
            )
            lifecycle_backend_ready.set()
            if lifecycle_operation == "replace":
                result = replace_google_calendar_connection(
                    session,
                    account_id=account_id,
                    refresh_token="lifecycle-race-refresh-token",
                    cipher=CREDENTIAL_CIPHER,
                    now=T0 + datetime.timedelta(minutes=2),
                )
            else:
                result = disconnect_google_calendar(
                    session,
                    account_id=account_id,
                )
            if wait_before_commit:
                first_ready.set()
                assert allow_first_commit.wait(timeout=5)
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        if first_operation == "claim":
            first_future = executor.submit(run_claim)
            assert first_ready.wait(timeout=5)
            second_future = executor.submit(
                run_lifecycle,
                wait_before_commit=False,
            )
            assert lifecycle_backend_ready.wait(timeout=5)
            try:
                _wait_until_blocked_by(
                    lifecycle_backend_pid[0],
                    claim_backend_pid[0],
                )
            finally:
                allow_first_commit.set()
            claim_result = first_future.result(timeout=10)
            lifecycle_result = second_future.result(timeout=10)
        else:
            first_future = executor.submit(
                run_lifecycle,
                wait_before_commit=True,
            )
            assert first_ready.wait(timeout=5)
            second_future = executor.submit(run_claim)
            assert claim_backend_ready.wait(timeout=5)
            try:
                _wait_until_blocked_by(
                    claim_backend_pid[0],
                    lifecycle_backend_pid[0],
                )
            finally:
                allow_first_commit.set()
            lifecycle_result = first_future.result(timeout=10)
            claim_result = second_future.result(timeout=10)

    assert lifecycle_result.state == (
        "connected" if lifecycle_operation == "replace" else "disconnected"
    )
    if first_operation == "claim":
        assert type(claim_result) is GoogleCalendarOAuthCallbackClaim
        assert claim_result.expected_calendar_connection_revision == 1
        assert _stored_revision(account_id) == 2
    else:
        assert claim_result == "invalid"
        assert _stored_revision(account_id) == 1
    assert (_stored_connection(account_id) is not None) is (
        lifecycle_operation == "replace"
    )
    assert _stored_oauth_pair(_state_digest(issued)) == (False, False)


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
