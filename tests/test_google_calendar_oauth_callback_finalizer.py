from __future__ import annotations

import base64
import datetime
import hashlib
import inspect
import os
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from app import (
    auth,
    db,
    google_calendar_oauth_callback_claim,
    google_calendar_oauth_callback_finalizer,
    google_calendar_token_exchange,
)
from app.calendar_connections import (
    CalendarConnectionStatus,
    disconnect_google_calendar,
    replace_google_calendar_connection,
)
from app.calendar_credentials import (
    CalendarCredentialCipher,
    CalendarCredentialScope,
    EncryptedCalendarCredential,
)
from app.calendar_oauth_pkce import (
    CalendarOAuthPKCECipher,
    derive_s256_code_challenge,
)
from app.calendar_oauth_states import (
    issue_google_calendar_oauth_state_with_pkce_and_browser_binding,
)
from app.google_calendar_oauth import GoogleCalendarOAuthError
from app.google_calendar_oauth_callback_claim import (
    GoogleCalendarOAuthCallbackClaim,
    GoogleCalendarOAuthCallbackClaimError,
    claim_google_calendar_oauth_callback,
)
from app.google_calendar_oauth_callback_finalizer import (
    GoogleCalendarOAuthCallbackFinalizationError,
    finalize_google_calendar_oauth_callback,
)
from app.google_calendar_token_exchange import (
    GoogleCalendarTokenExchangeResult,
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
T0 = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
T1 = T0 + datetime.timedelta(minutes=5)
ACCOUNT_ID = "acct_callback_finalizer_private"
SESSION_ID = "sess_callback_finalizer_private"
REFRESH_TOKEN = "callback-finalizer-refresh-token-private"
POSTGRESQL_BIGINT_MAX = 9_223_372_036_854_775_807
_COORDINATOR_TIMEOUT_SECONDS = 10
_HOLDER_TIMEOUT_SECONDS = 30
_FUTURE_TIMEOUT_SECONDS = 30


def _canonical(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


CODE_VERIFIER = _canonical(b"v" * 32)
OTHER_CODE_VERIFIER = _canonical(b"w" * 32)
CREDENTIAL_CIPHER = CalendarCredentialCipher(
    active_key_id="callback-finalizer-key",
    keys={"callback-finalizer-key": b"c" * 32},
)


class _CredentialCipherSubclass(CalendarCredentialCipher):
    pass


SUBCLASS_CREDENTIAL_CIPHER = _CredentialCipherSubclass(
    active_key_id="callback-finalizer-subclass-key",
    keys={"callback-finalizer-subclass-key": b"s" * 32},
)
PKCE_CIPHER = CalendarOAuthPKCECipher(
    active_key_id="callback-finalizer-pkce-key",
    root_keys={"callback-finalizer-pkce-key": b"p" * 32},
)
_PURE_TEST_ENGINE = create_engine("sqlite+pysqlite:///:memory:")
_SUCCESS_STATUS = CalendarConnectionStatus(
    provider="google",
    state="connected",
    connected_at=T1,
    updated_at=T1,
    authorization_required_at=None,
)


@contextmanager
def _race_executor(*release_events):
    executor = ThreadPoolExecutor(max_workers=2)
    try:
        yield executor
    finally:
        for release_event in release_events:
            release_event.set()
        executor.shutdown(wait=True)


def _claim(
    *,
    account_id=ACCOUNT_ID,
    session_id=SESSION_ID,
    code_verifier=CODE_VERIFIER,
    expected_revision=1,
):
    return google_calendar_oauth_callback_claim._new_claim(
        account_id=account_id,
        session_id=session_id,
        code_verifier=code_verifier,
        expected_calendar_connection_revision=expected_revision,
    )


def _token_result(
    *,
    refresh_token=REFRESH_TOKEN,
    code_verifier=CODE_VERIFIER,
    pkce_code_challenge=None,
):
    challenge = (
        derive_s256_code_challenge(code_verifier)
        if pkce_code_challenge is None
        else pkce_code_challenge
    )
    return google_calendar_token_exchange._new_exchange_result(
        refresh_token=refresh_token,
        pkce_code_challenge=challenge,
    )


def _finalize(session_factory, **overrides):
    values = {
        "claim": _claim(),
        "token_result": _token_result(),
        "credential_cipher": CREDENTIAL_CIPHER,
    }
    values.update(overrides)
    return finalize_google_calendar_oauth_callback(
        session_factory,
        **values,
    )


def _assert_fails_before_factory(**overrides):
    factory_calls = []

    def forbidden_factory():
        factory_calls.append(True)
        raise AssertionError("invalid handoff must not create a session")

    with pytest.raises(GoogleCalendarOAuthCallbackFinalizationError) as raised:
        _finalize(forbidden_factory, **overrides)

    assert factory_calls == []
    return raised.value


def _module_traceback(error):
    names = []
    values = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith(
            "/app/google_calendar_oauth_callback_finalizer.py"
        ):
            names.append(current.tb_frame.f_code.co_name)
            values.extend(current.tb_frame.f_locals.values())
        current = current.tb_next
    return names, repr(values)


def _formatted_exception(error):
    return "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )


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
    def lock_owner(
        session,
        *,
        account_id,
        session_id,
        expected_revision,
    ):
        events.append("lock_owner")
        assert session.in_transaction()
        assert account_id == ACCOUNT_ID
        assert session_id == SESSION_ID
        assert expected_revision == 1
        return T1

    def replace(
        session,
        *,
        account_id,
        refresh_token,
        cipher,
        now,
    ):
        events.append("replace")
        assert session.in_transaction()
        assert account_id == ACCOUNT_ID
        assert refresh_token == REFRESH_TOKEN
        assert cipher is CREDENTIAL_CIPHER
        assert now == T1
        return _SUCCESS_STATUS

    monkeypatch.setattr(
        google_calendar_oauth_callback_finalizer,
        "_lock_and_validate_owner",
        lock_owner,
    )
    monkeypatch.setattr(
        google_calendar_oauth_callback_finalizer,
        "replace_google_calendar_connection",
        replace,
    )


def test_public_surface_owns_commit_and_close_before_return(monkeypatch):
    assert google_calendar_oauth_callback_finalizer.__all__ == (
        "GoogleCalendarOAuthCallbackFinalizationError",
        "finalize_google_calendar_oauth_callback",
    )
    assert issubclass(
        GoogleCalendarOAuthCallbackFinalizationError,
        GoogleCalendarOAuthError,
    )
    signature = inspect.signature(finalize_google_calendar_oauth_callback)
    assert list(signature.parameters) == [
        "session_factory",
        "claim",
        "token_result",
        "credential_cipher",
    ]
    assert "now" not in signature.parameters
    assert signature.parameters["session_factory"].kind is (
        inspect.Parameter.POSITIONAL_OR_KEYWORD
    )
    assert all(
        signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        for name in ("claim", "token_result", "credential_cipher")
    )

    events = []
    factory, sessions = _recording_factory(events)
    _install_success_dependencies(monkeypatch, events, sessions)

    status = _finalize(factory)

    assert status is _SUCCESS_STATUS
    assert sessions[0].was_closed is True
    assert sessions[0].in_transaction() is False
    assert events == [
        "factory",
        "lock_owner",
        "replace",
        "commit",
        "close",
    ]


def test_crossed_exchange_result_fails_before_session_creation():
    _assert_fails_before_factory(
        token_result=_token_result(code_verifier=OTHER_CODE_VERIFIER),
    )


@pytest.mark.parametrize(
    "mutation",
    [
        ("claim", "account_id", " bad"),
        ("claim", "session_id", "café"),
        ("claim", "code_verifier", "not-a-verifier"),
        ("claim", "expected_calendar_connection_revision", 0),
        ("claim", "expected_calendar_connection_revision", True),
        ("result", "refresh_token", "bad\nrefresh"),
        ("result", "pkce_code_challenge", "not-a-challenge"),
    ],
)
def test_forged_handoff_fields_fail_before_session_creation(mutation):
    target, field, value = mutation
    claim = _claim()
    result = _token_result()
    object.__setattr__(claim if target == "claim" else result, field, value)
    _assert_fails_before_factory(claim=claim, token_result=result)


@pytest.mark.parametrize(
    ("claim", "result", "cipher"),
    [
        (object(), _token_result(), CREDENTIAL_CIPHER),
        (_claim(), object(), CREDENTIAL_CIPHER),
        (_claim(), _token_result(), object()),
        (_claim(), _token_result(), SUBCLASS_CREDENTIAL_CIPHER),
    ],
)
def test_handoff_requires_exact_value_types_before_session(
    claim,
    result,
    cipher,
):
    _assert_fails_before_factory(
        claim=claim,
        token_result=result,
        credential_cipher=cipher,
    )


@pytest.mark.parametrize(
    "outcome",
    [CODE_VERIFIER, "A" * 43, None, RuntimeError("private derivation detail")],
)
def test_malformed_pkce_derivation_fails_before_session_and_is_sanitized(
    monkeypatch,
    outcome,
):
    def derive(_verifier):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(
        google_calendar_oauth_callback_finalizer,
        "derive_s256_code_challenge",
        derive,
    )

    raised = _assert_fails_before_factory()
    output = _formatted_exception(raised)
    assert CODE_VERIFIER not in output
    assert "private derivation detail" not in output


def test_factory_and_session_boundary_failures_are_uniform():
    with pytest.raises(GoogleCalendarOAuthCallbackFinalizationError):
        _finalize(None)
    with pytest.raises(GoogleCalendarOAuthCallbackFinalizationError):
        _finalize(lambda: object())

    active_session = Session(bind=_PURE_TEST_ENGINE)
    active_session.begin()
    with pytest.raises(GoogleCalendarOAuthCallbackFinalizationError):
        _finalize(lambda: active_session)
    assert active_session.in_transaction() is False

    connection = _PURE_TEST_ENGINE.connect()
    try:
        connection_bound_session = Session(bind=connection)
        with pytest.raises(GoogleCalendarOAuthCallbackFinalizationError):
            _finalize(lambda: connection_bound_session)
    finally:
        connection.close()

    other_engine = create_engine("sqlite+pysqlite:///:memory:")
    split_session = Session(
        bind=_PURE_TEST_ENGINE,
        binds={ProviderSessionDB: other_engine},
    )
    with pytest.raises(GoogleCalendarOAuthCallbackFinalizationError):
        _finalize(lambda: split_session)


@pytest.mark.parametrize("failure_stage", ["commit", "close"])
def test_transaction_failures_return_no_status_and_close(
    monkeypatch,
    failure_stage,
):
    events = []
    factory, sessions = _recording_factory(
        events,
        commit_error=(
            RuntimeError("private commit detail")
            if failure_stage == "commit"
            else None
        ),
        close_error=(
            RuntimeError("private close detail")
            if failure_stage == "close"
            else None
        ),
    )
    _install_success_dependencies(monkeypatch, events, sessions)

    with pytest.raises(GoogleCalendarOAuthCallbackFinalizationError) as raised:
        _finalize(factory)

    assert sessions[0].was_closed is True
    assert sessions[0].in_transaction() is False
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    output = _formatted_exception(raised.value)
    for private in (
        "private commit detail",
        "private close detail",
    ):
        assert private not in output


def test_invalid_replacement_status_rolls_back(monkeypatch):
    events = []
    factory, sessions = _recording_factory(events)
    _install_success_dependencies(monkeypatch, events, sessions)
    monkeypatch.setattr(
        google_calendar_oauth_callback_finalizer,
        "replace_google_calendar_connection",
        lambda *_args, **_kwargs: object(),
    )

    with pytest.raises(GoogleCalendarOAuthCallbackFinalizationError):
        _finalize(factory)

    assert "commit" not in events
    assert "rollback" in events
    assert sessions[0].was_closed is True


class _FakeQuery:
    def __init__(self, events, model, value):
        self.events = events
        self.model = model
        self.value = value

    def filter(self, *_criteria):
        self.events.append(("filter", self.model))
        return self

    def populate_existing(self):
        self.events.append(("refresh", self.model))
        return self

    def with_for_update(self):
        self.events.append(("lock", self.model))
        return self

    def one_or_none(self):
        self.events.append(("row", self.model))
        return self.value


class _FakeOwnerSession:
    def __init__(self, account, provider_session, events):
        self.account = account
        self.provider_session = provider_session
        self.events = events

    def query(self, model):
        self.events.append(("query", model))
        value = (
            self.account if model is ProviderAccountDB else self.provider_session
        )
        return _FakeQuery(self.events, model, value)


def _owner_rows(**overrides):
    account = SimpleNamespace(
        id=ACCOUNT_ID,
        disabled_at=None,
        session_version=1,
        calendar_connection_revision=1,
    )
    provider_session = SimpleNamespace(
        id=SESSION_ID,
        account_id=ACCOUNT_ID,
        revoked_at=None,
        session_version=1,
        created_at=T0,
        expires_at=T1 + datetime.timedelta(minutes=5),
    )
    for name, value in overrides.items():
        target_name, field = name.split("__", 1)
        setattr(account if target_name == "account" else provider_session, field, value)
    return account, provider_session


def test_trusted_clock_is_sampled_after_account_and_session_locks(monkeypatch):
    events = []
    account, provider_session = _owner_rows()
    session = _FakeOwnerSession(account, provider_session, events)

    def clock():
        assert events[-1] == ("row", ProviderSessionDB)
        events.append("clock")
        return T1

    monkeypatch.setattr(
        google_calendar_oauth_callback_finalizer,
        "_utc_now",
        clock,
    )

    finalized_at = (
        google_calendar_oauth_callback_finalizer._lock_and_validate_owner(
            session,
            account_id=ACCOUNT_ID,
            session_id=SESSION_ID,
            expected_revision=1,
        )
    )

    assert finalized_at == T1
    assert events.index(("lock", ProviderAccountDB)) < events.index(
        ("lock", ProviderSessionDB)
    )
    assert events[-1] == "clock"


@pytest.mark.parametrize(
    ("overrides", "expected_revision"),
    [
        ({"account__disabled_at": T0}, 1),
        ({"session__revoked_at": T0}, 1),
        ({"account__session_version": 2}, 1),
        ({"session__created_at": T1 + datetime.timedelta(seconds=1)}, 1),
        ({"session__expires_at": T1}, 1),
        ({"account__calendar_connection_revision": 2}, 1),
        (
            {"account__calendar_connection_revision": POSTGRESQL_BIGINT_MAX},
            POSTGRESQL_BIGINT_MAX,
        ),
    ],
)
def test_locked_owner_rejects_inactive_stale_and_exhausted_states(
    monkeypatch,
    overrides,
    expected_revision,
):
    account, provider_session = _owner_rows(**overrides)
    session = _FakeOwnerSession(account, provider_session, [])
    monkeypatch.setattr(
        google_calendar_oauth_callback_finalizer,
        "_utc_now",
        lambda: T1,
    )

    with pytest.raises(ValueError):
        google_calendar_oauth_callback_finalizer._lock_and_validate_owner(
            session,
            account_id=ACCOUNT_ID,
            session_id=SESSION_ID,
            expected_revision=expected_revision,
        )


def test_missing_locked_account_or_session_fails(monkeypatch):
    monkeypatch.setattr(
        google_calendar_oauth_callback_finalizer,
        "_utc_now",
        lambda: T1,
    )
    account, provider_session = _owner_rows()
    for missing_account, missing_session in ((True, False), (False, True)):
        session = _FakeOwnerSession(
            None if missing_account else account,
            None if missing_session else provider_session,
            [],
        )
        with pytest.raises(ValueError):
            google_calendar_oauth_callback_finalizer._lock_and_validate_owner(
                session,
                account_id=ACCOUNT_ID,
                session_id=SESSION_ID,
                expected_revision=1,
            )


def test_failures_are_fixed_cause_free_and_secret_free(monkeypatch, caplog):
    private_detail = "dependency-finalizer-private-detail"
    events = []
    factory, sessions = _recording_factory(events)
    _install_success_dependencies(monkeypatch, events, sessions)

    def fail_replace(*_args, **_kwargs):
        raise RuntimeError(private_detail)

    monkeypatch.setattr(
        google_calendar_oauth_callback_finalizer,
        "replace_google_calendar_connection",
        fail_replace,
    )

    with pytest.raises(GoogleCalendarOAuthCallbackFinalizationError) as raised:
        _finalize(factory)

    assert "rollback" in events
    assert sessions[0].was_closed is True
    assert str(raised.value) == (
        "Google Calendar authorization could not be finalized."
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, frame_values = _module_traceback(raised.value)
    assert names == [
        "finalize_google_calendar_oauth_callback",
        "_raise_finalization_error",
    ]
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
        ACCOUNT_ID,
        SESSION_ID,
        CODE_VERIFIER,
        REFRESH_TOKEN,
        private_detail,
    ):
        assert private not in output


def test_ambient_exception_context_is_cleared():
    ambient_secret = "ambient-finalizer-private-detail"
    try:
        raise RuntimeError(ambient_secret)
    except RuntimeError:
        with pytest.raises(
            GoogleCalendarOAuthCallbackFinalizationError
        ) as raised:
            _finalize(None)

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
            "import app.calendar_connections as connections",
            "def forbidden(*_args, **_kwargs):",
            "    calls.append(True)",
            "    raise AssertionError('finalizer must remain call-time only')",
            "connections.replace_google_calendar_connection = forbidden",
            (
                "import app.google_calendar_oauth_callback_finalizer "
                "as finalizer"
            ),
            "finalizer.replace_google_calendar_connection = forbidden",
            "from app.api import create_app",
            "app = create_app()",
            (
                "assert not hasattr("
                "app.state, 'google_calendar_oauth_callback_finalizer')"
            ),
            (
                "assert 'google_calendar_oauth_callback_finalizer' "
                "not in app.state.routing_readiness"
            ),
            "assert 'httpx' not in finalizer.__dict__",
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


@pytest.fixture()
def fixed_finalizer_clock(monkeypatch):
    monkeypatch.setattr(
        google_calendar_oauth_callback_finalizer,
        "_utc_now",
        lambda: T1,
    )


def _add_owner(suffix: str, *, revision: int = 1) -> tuple[str, str, str]:
    instructor_id = f"inst_callback_finalizer_{suffix}"
    account_id = f"acct_callback_finalizer_{suffix}"
    session_id = f"sess_callback_finalizer_{suffix}"
    with db.SessionLocal.begin() as session:
        session.add(
            InstructorDB(
                id=instructor_id,
                name=f"Callback Finalizer {suffix}",
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
                email_normalized=f"callback-finalizer-{suffix}@example.com",
                password_hash="unused-test-password-hash",
                session_version=1,
                calendar_connection_revision=revision,
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
                    f"callback-finalizer-session-{suffix}".encode("ascii")
                ).digest(),
                session_version=1,
                created_at=T0,
                expires_at=T1 + datetime.timedelta(hours=1),
            )
        )
    return account_id, session_id, instructor_id


def _claim_for(account_id: str, session_id: str, *, revision: int = 1):
    return _claim(
        account_id=account_id,
        session_id=session_id,
        expected_revision=revision,
    )


def _finalize_real(
    account_id: str,
    session_id: str,
    *,
    token=REFRESH_TOKEN,
    expected_revision=1,
):
    return finalize_google_calendar_oauth_callback(
        db.SessionLocal,
        claim=_claim_for(
            account_id,
            session_id,
            revision=expected_revision,
        ),
        token_result=_token_result(refresh_token=token),
        credential_cipher=CREDENTIAL_CIPHER,
    )


def _stored_revision(account_id: str) -> int:
    with db.SessionLocal() as session:
        account = session.get(ProviderAccountDB, account_id)
        assert account is not None
        return account.calendar_connection_revision


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


def _stored_refresh_token(account_id: str, instructor_id: str):
    with db.SessionLocal() as session:
        row = session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=account_id
        ).one_or_none()
        if row is None:
            return None
        return CREDENTIAL_CIPHER.decrypt_refresh_token(
            EncryptedCalendarCredential(
                version=row.credential_version,
                key_id=row.credential_key_id,
                nonce=bytes(row.credential_nonce),
                ciphertext=bytes(row.credential_ciphertext),
            ),
            scope=CalendarCredentialScope(
                instructor_id=instructor_id,
                connection_id=row.id,
                provider="google",
            ),
        )


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
        now=T1,
    )


def _state_digest(issued) -> bytes:
    return hashlib.sha256(
        issued.issued_state.raw_state.encode("ascii")
    ).digest()


def _stored_oauth_pair(state_digest: bytes) -> tuple[bool, bool]:
    with db.SessionLocal() as session:
        return (
            session.get(ProviderCalendarOAuthStateDB, state_digest) is not None,
            session.get(ProviderCalendarOAuthPKCEVerifierDB, state_digest)
            is not None,
        )


def test_real_finalization_encrypts_replaces_and_advances_once(
    migrated_postgres_db,
    fixed_finalizer_clock,
):
    account_id, session_id, instructor_id = _add_owner(
        "success",
        revision=0,
    )
    issued = _issue(account_id, session_id)
    claim = _claim_issued(issued)
    assert claim.expected_calendar_connection_revision == 1
    pending = _issue(account_id, session_id)
    pending_digest = _state_digest(pending)

    status = finalize_google_calendar_oauth_callback(
        db.SessionLocal,
        claim=claim,
        token_result=_token_result(code_verifier=claim.code_verifier),
        credential_cipher=CREDENTIAL_CIPHER,
    )

    assert status == CalendarConnectionStatus(
        provider="google",
        state="connected",
        connected_at=T1,
        updated_at=T1,
        authorization_required_at=None,
    )
    assert _stored_revision(account_id) == 2
    assert _stored_refresh_token(account_id, instructor_id) == REFRESH_TOKEN
    assert REFRESH_TOKEN.encode("ascii") not in _stored_connection(account_id)[4]
    assert _stored_oauth_pair(pending_digest) == (False, False)


@pytest.mark.parametrize("inactive", ["revoked", "version", "disabled", "expired"])
def test_real_inactive_owner_cannot_install_credential(
    migrated_postgres_db,
    fixed_finalizer_clock,
    inactive,
):
    account_id, session_id, _ = _add_owner(f"inactive-{inactive}")
    with db.SessionLocal.begin() as session:
        account = session.get(ProviderAccountDB, account_id)
        provider_session = session.get(ProviderSessionDB, session_id)
        if inactive == "revoked":
            provider_session.revoked_at = T0
        elif inactive == "version":
            account.session_version += 1
        elif inactive == "disabled":
            account.disabled_at = T0
        else:
            provider_session.expires_at = T1

    with pytest.raises(GoogleCalendarOAuthCallbackFinalizationError):
        _finalize_real(account_id, session_id)

    assert _stored_revision(account_id) == 1
    assert _stored_connection(account_id) is None


@pytest.mark.parametrize(
    ("revision", "expected_revision"),
    [(2, 1), (POSTGRESQL_BIGINT_MAX, POSTGRESQL_BIGINT_MAX)],
)
def test_real_stale_or_exhausted_revision_preserves_state(
    migrated_postgres_db,
    fixed_finalizer_clock,
    revision,
    expected_revision,
):
    account_id, session_id, _ = _add_owner(
        f"revision-{revision}",
        revision=revision,
    )

    with pytest.raises(GoogleCalendarOAuthCallbackFinalizationError):
        _finalize_real(
            account_id,
            session_id,
            expected_revision=expected_revision,
        )

    assert _stored_revision(account_id) == revision
    assert _stored_connection(account_id) is None


def test_real_commit_failure_rolls_back_replacement_revision_and_cancellation(
    migrated_postgres_db,
    fixed_finalizer_clock,
):
    account_id, session_id, _ = _add_owner("commit-failure")
    issued = _issue(account_id, session_id)
    digest = _state_digest(issued)

    def factory():
        session = db.SessionLocal()

        def fail_commit(_session):
            raise RuntimeError("private finalizer commit failure")

        event.listen(session, "before_commit", fail_commit, once=True)
        return session

    with pytest.raises(GoogleCalendarOAuthCallbackFinalizationError):
        finalize_google_calendar_oauth_callback(
            factory,
            claim=_claim_for(account_id, session_id),
            token_result=_token_result(),
            credential_cipher=CREDENTIAL_CIPHER,
        )

    assert _stored_revision(account_id) == 1
    assert _stored_connection(account_id) is None
    assert _stored_oauth_pair(digest) == (True, True)


def test_autocommit_engine_is_rejected_before_owner_lookup(
    migrated_postgres_db,
    fixed_finalizer_clock,
):
    account_id, session_id, _ = _add_owner("autocommit")
    autocommit_engine = create_engine(
        db.engine.url,
        isolation_level="AUTOCOMMIT",
    )

    def factory():
        return Session(bind=autocommit_engine)

    try:
        with pytest.raises(GoogleCalendarOAuthCallbackFinalizationError):
            finalize_google_calendar_oauth_callback(
                factory,
                claim=_claim_for(account_id, session_id),
                token_result=_token_result(),
                credential_cipher=CREDENTIAL_CIPHER,
            )
    finally:
        autocommit_engine.dispose()

    assert _stored_revision(account_id) == 1
    assert _stored_connection(account_id) is None


def test_transactional_override_of_autocommit_default_is_accepted(
    migrated_postgres_db,
    fixed_finalizer_clock,
):
    account_id, session_id, _ = _add_owner("transactional-override")
    autocommit_default_engine = create_engine(
        db.engine.url,
        isolation_level="AUTOCOMMIT",
    )
    transactional_engine = autocommit_default_engine.execution_options(
        isolation_level="READ COMMITTED"
    )

    def factory():
        return Session(bind=transactional_engine)

    try:
        result = finalize_google_calendar_oauth_callback(
            factory,
            claim=_claim_for(account_id, session_id),
            token_result=_token_result(),
            credential_cipher=CREDENTIAL_CIPHER,
        )
    finally:
        autocommit_default_engine.dispose()

    assert result.state == "connected"
    assert _stored_revision(account_id) == 2
    assert REFRESH_TOKEN.encode("ascii") not in _stored_connection(account_id)[4]


def _install_finalizer_race_probe(
    monkeypatch,
    *,
    ready,
    backend_pid,
    hold,
    first_locked,
    release,
):
    real_lock = google_calendar_oauth_callback_finalizer._lock_and_validate_owner

    def tracked_lock(session, **kwargs):
        backend_pid.append(
            session.execute(text("SELECT pg_backend_pid()")).scalar_one()
        )
        ready.set()
        value = real_lock(session, **kwargs)
        if hold:
            first_locked.set()
            assert release.wait(timeout=_HOLDER_TIMEOUT_SECONDS)
        return value

    monkeypatch.setattr(
        google_calendar_oauth_callback_finalizer,
        "_lock_and_validate_owner",
        tracked_lock,
    )


def _run_ordered_race(
    executor,
    *,
    first_operation,
    first_ready,
    first_pid,
    second_operation,
    second_ready,
    second_pid,
    release,
):
    first_future = executor.submit(first_operation)
    assert first_ready.wait(timeout=_COORDINATOR_TIMEOUT_SECONDS)
    second_future = executor.submit(second_operation)
    assert second_ready.wait(timeout=_COORDINATOR_TIMEOUT_SECONDS)
    try:
        _wait_until_blocked_by(second_pid(), first_pid())
    finally:
        release.set()
    return (
        first_future.result(timeout=_FUTURE_TIMEOUT_SECONDS),
        second_future.result(timeout=_FUTURE_TIMEOUT_SECONDS),
    )


def test_same_claim_concurrency_has_one_winner(
    migrated_postgres_db,
    fixed_finalizer_clock,
    monkeypatch,
):
    account_id, session_id, _ = _add_owner("same-claim-race")
    first_locked = threading.Event()
    allow_first_commit = threading.Event()
    second_backend_ready = threading.Event()
    backend_pids = []
    backend_lock = threading.Lock()
    real_lock = google_calendar_oauth_callback_finalizer._lock_and_validate_owner

    def tracked_lock(session, **kwargs):
        backend_pid = session.execute(
            text("SELECT pg_backend_pid()")
        ).scalar_one()
        with backend_lock:
            backend_pids.append(backend_pid)
            if len(backend_pids) == 2:
                second_backend_ready.set()
        finalized_at = real_lock(session, **kwargs)
        if not first_locked.is_set():
            first_locked.set()
            assert allow_first_commit.wait(timeout=_HOLDER_TIMEOUT_SECONDS)
        return finalized_at

    monkeypatch.setattr(
        google_calendar_oauth_callback_finalizer,
        "_lock_and_validate_owner",
        tracked_lock,
    )

    def attempt():
        try:
            return _finalize_real(account_id, session_id)
        except GoogleCalendarOAuthCallbackFinalizationError:
            return None

    with _race_executor(allow_first_commit) as executor:
        results = _run_ordered_race(
            executor,
            first_operation=attempt,
            first_ready=first_locked,
            first_pid=lambda: backend_pids[0],
            second_operation=attempt,
            second_ready=second_backend_ready,
            second_pid=lambda: backend_pids[1],
            release=allow_first_commit,
        )

    assert sum(value is not None for value in results) == 1
    assert _stored_revision(account_id) == 2
    assert _stored_connection(account_id) is not None


@pytest.mark.parametrize("first_operation", ["finalizer", "newer_claim"])
def test_newer_claim_and_finalizer_have_deterministic_ordering(
    migrated_postgres_db,
    fixed_finalizer_clock,
    monkeypatch,
    first_operation,
):
    account_id, session_id, _ = _add_owner(
        f"newer-claim-{first_operation}"
    )
    issued = _issue(account_id, session_id)
    digest = _state_digest(issued)
    first_locked = threading.Event()
    allow_first_commit = threading.Event()
    finalizer_ready = threading.Event()
    claim_ready = threading.Event()
    finalizer_pid = []
    claim_pid = []
    real_consume = (
        google_calendar_oauth_callback_claim
        .consume_google_calendar_oauth_state_with_pkce_and_browser_binding
    )
    real_advance = google_calendar_oauth_callback_claim._advance_claim_revision

    def tracked_consume(session, **kwargs):
        claim_pid.append(
            session.execute(text("SELECT pg_backend_pid()")).scalar_one()
        )
        claim_ready.set()
        return real_consume(session, **kwargs)

    def tracked_advance(session, *, account_id):
        revision = real_advance(session, account_id=account_id)
        if first_operation == "newer_claim":
            first_locked.set()
            assert allow_first_commit.wait(timeout=_HOLDER_TIMEOUT_SECONDS)
        return revision

    _install_finalizer_race_probe(
        monkeypatch,
        ready=finalizer_ready,
        backend_pid=finalizer_pid,
        hold=first_operation == "finalizer",
        first_locked=first_locked,
        release=allow_first_commit,
    )
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

    def run_finalizer():
        try:
            return _finalize_real(account_id, session_id)
        except GoogleCalendarOAuthCallbackFinalizationError:
            return "invalid"

    def run_claim():
        try:
            return _claim_issued(issued)
        except GoogleCalendarOAuthCallbackClaimError:
            return "invalid"

    with _race_executor(allow_first_commit) as executor:
        if first_operation == "finalizer":
            finalizer_result, claim_result = _run_ordered_race(
                executor,
                first_operation=run_finalizer,
                first_ready=first_locked,
                first_pid=lambda: finalizer_pid[0],
                second_operation=run_claim,
                second_ready=claim_ready,
                second_pid=lambda: claim_pid[0],
                release=allow_first_commit,
            )
        else:
            claim_result, finalizer_result = _run_ordered_race(
                executor,
                first_operation=run_claim,
                first_ready=first_locked,
                first_pid=lambda: claim_pid[0],
                second_operation=run_finalizer,
                second_ready=finalizer_ready,
                second_pid=lambda: finalizer_pid[0],
                release=allow_first_commit,
            )

    assert _stored_revision(account_id) == 2
    assert _stored_oauth_pair(digest) == (False, False)
    if first_operation == "finalizer":
        assert type(finalizer_result) is CalendarConnectionStatus
        assert claim_result == "invalid"
        assert _stored_connection(account_id) is not None
    else:
        assert type(claim_result) is GoogleCalendarOAuthCallbackClaim
        assert finalizer_result == "invalid"
        assert _stored_connection(account_id) is None


@pytest.mark.parametrize("first_operation", ["finalizer", "lifecycle"])
@pytest.mark.parametrize("lifecycle_operation", ["replace", "disconnect"])
def test_lifecycle_and_finalizer_serialize_on_account_lock(
    migrated_postgres_db,
    fixed_finalizer_clock,
    monkeypatch,
    first_operation,
    lifecycle_operation,
):
    suffix = f"{first_operation}-{lifecycle_operation}"
    account_id, session_id, instructor_id = _add_owner(suffix)
    first_locked = threading.Event()
    allow_first_commit = threading.Event()
    finalizer_ready = threading.Event()
    lifecycle_ready = threading.Event()
    finalizer_pid = []
    lifecycle_pid = []
    _install_finalizer_race_probe(
        monkeypatch,
        ready=finalizer_ready,
        backend_pid=finalizer_pid,
        hold=first_operation == "finalizer",
        first_locked=first_locked,
        release=allow_first_commit,
    )

    def run_finalizer():
        try:
            return _finalize_real(account_id, session_id)
        except GoogleCalendarOAuthCallbackFinalizationError:
            return "invalid"

    def run_lifecycle():
        with db.SessionLocal.begin() as session:
            lifecycle_pid.append(
                session.execute(text("SELECT pg_backend_pid()")).scalar_one()
            )
            lifecycle_ready.set()
            if lifecycle_operation == "replace":
                status = replace_google_calendar_connection(
                    session,
                    account_id=account_id,
                    refresh_token="lifecycle-authoritative-refresh-token",
                    cipher=CREDENTIAL_CIPHER,
                    now=T1 + datetime.timedelta(minutes=1),
                )
            else:
                status = disconnect_google_calendar(
                    session,
                    account_id=account_id,
                )
            if first_operation == "lifecycle":
                first_locked.set()
                assert allow_first_commit.wait(timeout=_HOLDER_TIMEOUT_SECONDS)
            return status

    with _race_executor(allow_first_commit) as executor:
        if first_operation == "finalizer":
            finalizer_result, lifecycle_result = _run_ordered_race(
                executor,
                first_operation=run_finalizer,
                first_ready=first_locked,
                first_pid=lambda: finalizer_pid[0],
                second_operation=run_lifecycle,
                second_ready=lifecycle_ready,
                second_pid=lambda: lifecycle_pid[0],
                release=allow_first_commit,
            )
        else:
            lifecycle_result, finalizer_result = _run_ordered_race(
                executor,
                first_operation=run_lifecycle,
                first_ready=first_locked,
                first_pid=lambda: lifecycle_pid[0],
                second_operation=run_finalizer,
                second_ready=finalizer_ready,
                second_pid=lambda: finalizer_pid[0],
                release=allow_first_commit,
            )

    assert type(lifecycle_result) is CalendarConnectionStatus
    assert _stored_revision(account_id) == (
        3 if first_operation == "finalizer" else 2
    )
    assert (_stored_connection(account_id) is not None) is (
        lifecycle_operation == "replace"
    )
    if lifecycle_operation == "replace":
        assert _stored_refresh_token(account_id, instructor_id) == (
            "lifecycle-authoritative-refresh-token"
        )
    if first_operation == "finalizer":
        assert type(finalizer_result) is CalendarConnectionStatus
    else:
        assert finalizer_result == "invalid"


@pytest.mark.parametrize("first_operation", ["finalizer", "security"])
@pytest.mark.parametrize(
    "security_operation",
    ["session_revocation", "account_disable"],
)
def test_session_security_changes_and_finalizer_serialize(
    migrated_postgres_db,
    fixed_finalizer_clock,
    monkeypatch,
    first_operation,
    security_operation,
):
    suffix = f"{security_operation}-{first_operation}"
    account_id, session_id, _ = _add_owner(suffix)
    email = f"callback-finalizer-{suffix}@example.com"
    first_locked = threading.Event()
    allow_first_commit = threading.Event()
    finalizer_ready = threading.Event()
    security_ready = threading.Event()
    finalizer_pid = []
    security_pid = []
    _install_finalizer_race_probe(
        monkeypatch,
        ready=finalizer_ready,
        backend_pid=finalizer_pid,
        hold=first_operation == "finalizer",
        first_locked=first_locked,
        release=allow_first_commit,
    )
    monkeypatch.setattr(auth, "_utc_now", lambda: T1)

    def run_finalizer():
        try:
            return _finalize_real(account_id, session_id)
        except GoogleCalendarOAuthCallbackFinalizationError:
            return "invalid"

    if security_operation == "session_revocation":

        def run_security():
            with db.SessionLocal.begin() as session:
                security_pid.append(
                    session.execute(
                        text("SELECT pg_backend_pid()")
                    ).scalar_one()
                )
                security_ready.set()
                row = (
                    session.query(ProviderSessionDB)
                    .filter_by(account_id=account_id, id=session_id)
                    .with_for_update()
                    .one()
                )
                row.revoked_at = T1
                row.revocation_reason = "test_revocation"
                if first_operation == "security":
                    first_locked.set()
                    assert allow_first_commit.wait(
                        timeout=_HOLDER_TIMEOUT_SECONDS
                    )

    else:
        real_account_lock = auth._locked_account_by_email

        def tracked_account_lock(session, normalized_email):
            security_pid.append(
                session.execute(text("SELECT pg_backend_pid()")).scalar_one()
            )
            security_ready.set()
            account = real_account_lock(session, normalized_email)
            if first_operation == "security":
                first_locked.set()
                assert allow_first_commit.wait(timeout=_HOLDER_TIMEOUT_SECONDS)
            return account

        monkeypatch.setattr(
            auth,
            "_locked_account_by_email",
            tracked_account_lock,
        )

        def run_security():
            auth.disable_provider_account(email)

    with _race_executor(allow_first_commit) as executor:
        if first_operation == "finalizer":
            finalizer_result, _ = _run_ordered_race(
                executor,
                first_operation=run_finalizer,
                first_ready=first_locked,
                first_pid=lambda: finalizer_pid[0],
                second_operation=run_security,
                second_ready=security_ready,
                second_pid=lambda: security_pid[0],
                release=allow_first_commit,
            )
        else:
            _, finalizer_result = _run_ordered_race(
                executor,
                first_operation=run_security,
                first_ready=first_locked,
                first_pid=lambda: security_pid[0],
                second_operation=run_finalizer,
                second_ready=finalizer_ready,
                second_pid=lambda: finalizer_pid[0],
                release=allow_first_commit,
            )

    with db.SessionLocal() as session:
        account = session.get(ProviderAccountDB, account_id)
        provider_session = session.get(ProviderSessionDB, session_id)
        assert provider_session.revoked_at is not None
        if security_operation == "account_disable":
            assert account.disabled_at is not None
            assert account.session_version == 2
    if first_operation == "finalizer":
        assert type(finalizer_result) is CalendarConnectionStatus
        assert _stored_revision(account_id) == 2
        assert _stored_connection(account_id) is not None
    else:
        assert finalizer_result == "invalid"
        assert _stored_revision(account_id) == 1
        assert _stored_connection(account_id) is None


def test_different_tenant_finalizers_do_not_block_each_other(
    migrated_postgres_db,
    fixed_finalizer_clock,
    monkeypatch,
):
    first_account, first_session, _ = _add_owner("tenant-a")
    second_account, second_session, _ = _add_owner("tenant-b")
    first_locked = threading.Event()
    release_first = threading.Event()
    second_locked = threading.Event()
    release_second = threading.Event()
    backend_pids = {}
    real_lock = google_calendar_oauth_callback_finalizer._lock_and_validate_owner

    def tracked_lock(session, **kwargs):
        account_id = kwargs["account_id"]
        backend_pids[account_id] = session.execute(
            text("SELECT pg_backend_pid()")
        ).scalar_one()
        value = real_lock(session, **kwargs)
        if account_id == first_account:
            first_locked.set()
            assert release_first.wait(timeout=_HOLDER_TIMEOUT_SECONDS)
        else:
            second_locked.set()
            assert release_second.wait(timeout=_HOLDER_TIMEOUT_SECONDS)
        return value

    monkeypatch.setattr(
        google_calendar_oauth_callback_finalizer,
        "_lock_and_validate_owner",
        tracked_lock,
    )

    with _race_executor(release_first, release_second) as executor:
        first_future = executor.submit(
            _finalize_real,
            first_account,
            first_session,
            token="tenant-a-refresh-token",
        )
        assert first_locked.wait(timeout=_COORDINATOR_TIMEOUT_SECONDS)
        second_future = executor.submit(
            _finalize_real,
            second_account,
            second_session,
            token="tenant-b-refresh-token",
        )
        assert second_locked.wait(timeout=_COORDINATOR_TIMEOUT_SECONDS)
        assert not _is_blocked_by(
            backend_pids[second_account],
            backend_pids[first_account],
        )
        release_second.set()
        assert type(
            second_future.result(timeout=_FUTURE_TIMEOUT_SECONDS)
        ) is CalendarConnectionStatus
        assert not release_first.is_set()
        release_first.set()
        assert type(
            first_future.result(timeout=_FUTURE_TIMEOUT_SECONDS)
        ) is CalendarConnectionStatus

    assert _stored_revision(first_account) == 2
    assert _stored_revision(second_account) == 2
    assert _stored_connection(first_account) is not None
    assert _stored_connection(second_account) is not None


def _is_blocked_by(backend_pid: int, blocker_pid: int) -> bool:
    with db.engine.connect() as connection:
        return connection.execute(
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


def _wait_until_blocked_by(backend_pid: int, blocker_pid: int) -> None:
    deadline = time.monotonic() + _COORDINATOR_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _is_blocked_by(backend_pid, blocker_pid):
            return
        time.sleep(0.01)
    raise AssertionError("The expected database operation did not block.")
