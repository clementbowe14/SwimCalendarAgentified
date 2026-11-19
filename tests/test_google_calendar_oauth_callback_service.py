from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import inspect
import os
import subprocess
import sys
import threading
import traceback
from pathlib import Path

import anyio
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from app import (
    db,
    google_calendar_oauth_callback_claim,
    google_calendar_oauth_callback_service,
    google_calendar_token_exchange,
)
from app.calendar_connections import CalendarConnectionStatus
from app.calendar_credentials import CalendarCredentialCipher
from app.calendar_oauth_pkce import (
    CalendarOAuthPKCECipher,
    derive_s256_code_challenge,
)
from app.calendar_oauth_states import (
    issue_google_calendar_oauth_state_with_pkce_and_browser_binding,
)
from app.google_calendar_oauth import (
    GOOGLE_CALENDAR_CALLBACK_PATH,
    GoogleCalendarOAuthConfiguration,
    GoogleCalendarOAuthError,
)
from app.google_calendar_oauth_callback_service import (
    GoogleCalendarOAuthCallbackCompletionError,
    complete_google_calendar_oauth_callback,
)
from app.google_calendar_token_exchange import (
    GoogleCalendarTokenExchangeConfiguration,
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
CLIENT_ID = "123456789-dayweave.apps.googleusercontent.com"
REDIRECT_URI = "https://api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH
CLIENT_SECRET = "callback-service-client-secret-private"


def _canonical(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


RAW_STATE = _canonical(b"s" * 32)
RAW_BROWSER_BINDING = _canonical(b"b" * 32)
AUTHORIZATION_CODE = "4/callback-service-authorization-code-private"
ACCOUNT_ID = "acct_callback_service_private"
SESSION_ID = "sess_callback_service_private"
CODE_VERIFIER = _canonical(b"v" * 32)
REFRESH_TOKEN = "callback-service-refresh-token-private"
PRIVATE_DETAIL = "callback-service-dependency-detail-private"
PKCE_CIPHER = CalendarOAuthPKCECipher(
    active_key_id="callback-service-pkce-key",
    root_keys={"callback-service-pkce-key": b"p" * 32},
)
CREDENTIAL_CIPHER = CalendarCredentialCipher(
    active_key_id="callback-service-credential-key",
    keys={"callback-service-credential-key": b"c" * 32},
)
SUCCESS_STATUS = CalendarConnectionStatus(
    provider="google",
    state="connected",
    connected_at=datetime.datetime(2026, 11, 19, 12, 0, tzinfo=UTC),
    updated_at=datetime.datetime(2026, 11, 19, 12, 0, tzinfo=UTC),
    authorization_required_at=None,
)


class PKCECipherSubclass(CalendarOAuthPKCECipher):
    pass


class CredentialCipherSubclass(CalendarCredentialCipher):
    pass


SUBCLASS_PKCE_CIPHER = PKCECipherSubclass(
    active_key_id="callback-service-subclass-pkce",
    root_keys={"callback-service-subclass-pkce": b"s" * 32},
)
SUBCLASS_CREDENTIAL_CIPHER = CredentialCipherSubclass(
    active_key_id="callback-service-subclass-credential",
    keys={"callback-service-subclass-credential": b"t" * 32},
)


def _configuration() -> GoogleCalendarTokenExchangeConfiguration:
    return GoogleCalendarTokenExchangeConfiguration(
        authorization_configuration=GoogleCalendarOAuthConfiguration(
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
        ),
        client_secret=CLIENT_SECRET,
    )


def _claim():
    return google_calendar_oauth_callback_claim._new_claim(
        account_id=ACCOUNT_ID,
        session_id=SESSION_ID,
        code_verifier=CODE_VERIFIER,
        expected_calendar_connection_revision=1,
    )


def _token_result(*, code_verifier: str = CODE_VERIFIER):
    return google_calendar_token_exchange._new_exchange_result(
        refresh_token=REFRESH_TOKEN,
        pkce_code_challenge=derive_s256_code_challenge(code_verifier),
    )


def _run(awaitable):
    return asyncio.run(awaitable)


def _complete(session_factory=lambda: None, **overrides):
    values = {
        "raw_state": RAW_STATE,
        "raw_browser_binding": RAW_BROWSER_BINDING,
        "authorization_code": AUTHORIZATION_CODE,
        "configuration": _configuration(),
        "pkce_cipher": PKCE_CIPHER,
        "credential_cipher": CREDENTIAL_CIPHER,
    }
    values.update(overrides)
    return complete_google_calendar_oauth_callback(
        session_factory,
        **values,
    )


def _install_success_dependencies(
    monkeypatch,
    events,
    *,
    expected_session_factory,
    expected_configuration,
):
    claim = _claim()
    token_result = _token_result()
    event_loop_thread = threading.get_ident()

    def claim_callback(
        session_factory,
        *,
        raw_state,
        raw_browser_binding,
        pkce_cipher,
    ):
        events.append(("claim", threading.get_ident()))
        assert threading.get_ident() != event_loop_thread
        assert session_factory is expected_session_factory
        assert raw_state == RAW_STATE
        assert raw_browser_binding == RAW_BROWSER_BINDING
        assert pkce_cipher is PKCE_CIPHER
        return claim

    async def exchange_callback(
        *,
        configuration,
        authorization_code,
        code_verifier,
    ):
        events.append(("exchange", threading.get_ident()))
        assert threading.get_ident() == event_loop_thread
        assert configuration is expected_configuration
        assert authorization_code == AUTHORIZATION_CODE
        assert code_verifier == CODE_VERIFIER
        return token_result

    def finalize_callback(
        session_factory,
        *,
        claim: object,
        token_result: object,
        credential_cipher,
    ):
        events.append(("finalize", threading.get_ident()))
        assert threading.get_ident() != event_loop_thread
        assert session_factory is expected_session_factory
        assert claim is _claim_value
        assert token_result is _token_result_value
        assert credential_cipher is CREDENTIAL_CIPHER
        return SUCCESS_STATUS

    _claim_value = claim
    _token_result_value = token_result
    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "claim_google_calendar_oauth_callback",
        claim_callback,
    )
    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "exchange_google_calendar_authorization_code_with_deadline",
        exchange_callback,
    )
    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "finalize_google_calendar_oauth_callback",
        finalize_callback,
    )


def _formatted_exception(error: BaseException) -> str:
    return "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )


def _module_traceback(error: BaseException) -> tuple[list[str], str]:
    names = []
    values = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith(
            "/app/google_calendar_oauth_callback_service.py"
        ):
            names.append(current.tb_frame.f_code.co_name)
            values.extend(current.tb_frame.f_locals.values())
        current = current.tb_next
    return names, repr(values)


def test_public_surface_and_successful_ordering(monkeypatch):
    assert google_calendar_oauth_callback_service.__all__ == (
        "GoogleCalendarOAuthCallbackCompletionError",
        "complete_google_calendar_oauth_callback",
    )
    assert issubclass(
        GoogleCalendarOAuthCallbackCompletionError,
        GoogleCalendarOAuthError,
    )
    assert inspect.iscoroutinefunction(
        complete_google_calendar_oauth_callback
    )
    signature = inspect.signature(complete_google_calendar_oauth_callback)
    assert list(signature.parameters) == [
        "session_factory",
        "raw_state",
        "raw_browser_binding",
        "authorization_code",
        "configuration",
        "pkce_cipher",
        "credential_cipher",
    ]
    assert signature.parameters["session_factory"].kind is (
        inspect.Parameter.POSITIONAL_OR_KEYWORD
    )
    assert all(
        signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        for name in list(signature.parameters)[1:]
    )
    for prohibited in (
        "now",
        "transport",
        "timeout",
        "endpoint",
        "session",
        "claim",
        "token_result",
    ):
        assert prohibited not in signature.parameters

    events = []
    configuration = _configuration()

    def session_factory():
        return None

    _install_success_dependencies(
        monkeypatch,
        events,
        expected_session_factory=session_factory,
        expected_configuration=configuration,
    )

    status = _run(
        _complete(
            session_factory,
            configuration=configuration,
        )
    )

    assert status is SUCCESS_STATUS
    assert [name for name, _thread in events] == [
        "claim",
        "exchange",
        "finalize",
    ]


def test_invalid_static_dependencies_fail_before_claim(monkeypatch):
    claim_calls = []

    def forbidden_claim(*_args, **_kwargs):
        claim_calls.append(True)
        raise AssertionError("invalid dependency reached claim")

    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "claim_google_calendar_oauth_callback",
        forbidden_claim,
    )
    cases = (
        (None, {}),
        (lambda: None, {"configuration": object()}),
        (lambda: None, {"pkce_cipher": object()}),
        (lambda: None, {"pkce_cipher": SUBCLASS_PKCE_CIPHER}),
        (lambda: None, {"credential_cipher": object()}),
        (
            lambda: None,
            {"credential_cipher": SUBCLASS_CREDENTIAL_CIPHER},
        ),
    )

    for session_factory, overrides in cases:
        with pytest.raises(GoogleCalendarOAuthCallbackCompletionError):
            _run(_complete(session_factory, **overrides))

    assert claim_calls == []


@pytest.mark.parametrize(
    ("malformed_stage", "expected_events"),
    [
        ("claim", ["claim"]),
        ("exchange", ["claim", "exchange"]),
        ("finalize", ["claim", "exchange", "finalize"]),
    ],
)
def test_malformed_collaborator_results_fail_closed(
    monkeypatch,
    malformed_stage,
    expected_events,
):
    events = []
    claim = _claim()
    token_result = _token_result()

    def claim_callback(*_args, **_kwargs):
        events.append("claim")
        return object() if malformed_stage == "claim" else claim

    async def exchange_callback(**_kwargs):
        events.append("exchange")
        return object() if malformed_stage == "exchange" else token_result

    def finalize_callback(*_args, **_kwargs):
        events.append("finalize")
        return object() if malformed_stage == "finalize" else SUCCESS_STATUS

    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "claim_google_calendar_oauth_callback",
        claim_callback,
    )
    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "exchange_google_calendar_authorization_code_with_deadline",
        exchange_callback,
    )
    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "finalize_google_calendar_oauth_callback",
        finalize_callback,
    )

    with pytest.raises(GoogleCalendarOAuthCallbackCompletionError):
        _run(_complete())

    assert events == expected_events


@pytest.mark.parametrize(
    ("failure_stage", "expected_events"),
    [
        ("claim", ["claim"]),
        ("exchange", ["claim", "exchange"]),
        ("finalize", ["claim", "exchange", "finalize"]),
    ],
)
def test_stage_failure_stops_without_retry(
    monkeypatch,
    failure_stage,
    expected_events,
):
    events = []
    claim = _claim()
    token_result = _token_result()

    def claim_callback(*_args, **_kwargs):
        events.append("claim")
        if failure_stage == "claim":
            raise RuntimeError(PRIVATE_DETAIL)
        return claim

    async def exchange_callback(**_kwargs):
        events.append("exchange")
        if failure_stage == "exchange":
            raise RuntimeError(PRIVATE_DETAIL)
        return token_result

    def finalize_callback(*_args, **_kwargs):
        events.append("finalize")
        if failure_stage == "finalize":
            raise RuntimeError(PRIVATE_DETAIL)
        return SUCCESS_STATUS

    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "claim_google_calendar_oauth_callback",
        claim_callback,
    )
    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "exchange_google_calendar_authorization_code_with_deadline",
        exchange_callback,
    )
    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "finalize_google_calendar_oauth_callback",
        finalize_callback,
    )

    with pytest.raises(GoogleCalendarOAuthCallbackCompletionError) as raised:
        _run(_complete())

    assert events == expected_events
    assert PRIVATE_DETAIL not in _formatted_exception(raised.value)


def test_external_exchange_cancellation_is_sanitized_and_skips_finalizer(
    monkeypatch,
):
    events = []
    claim = _claim()

    def claim_callback(*_args, **_kwargs):
        events.append("claim")
        return claim

    async def cancelled_exchange(**_kwargs):
        events.append("exchange")
        raise asyncio.CancelledError(PRIVATE_DETAIL)

    def forbidden_finalizer(*_args, **_kwargs):
        events.append("finalize")
        raise AssertionError("cancelled exchange reached finalizer")

    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "claim_google_calendar_oauth_callback",
        claim_callback,
    )
    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "exchange_google_calendar_authorization_code_with_deadline",
        cancelled_exchange,
    )
    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "finalize_google_calendar_oauth_callback",
        forbidden_finalizer,
    )

    with pytest.raises(asyncio.CancelledError) as raised:
        _run(_complete())

    assert events == ["claim", "exchange"]
    assert raised.value.args == ()
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    names, values = _module_traceback(raised.value)
    assert names == [
        "complete_google_calendar_oauth_callback",
        "_raise_cancelled",
    ]
    assert PRIVATE_DETAIL not in " ".join(
        (_formatted_exception(raised.value), values)
    )


def test_cancellation_waits_for_finalizer_worker_to_settle(monkeypatch):
    events = []
    started = threading.Event()
    release = threading.Event()
    claim = _claim()
    token_result = _token_result()

    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "claim_google_calendar_oauth_callback",
        lambda *_args, **_kwargs: claim,
    )

    async def exchange_callback(**_kwargs):
        return token_result

    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "exchange_google_calendar_authorization_code_with_deadline",
        exchange_callback,
    )

    def blocking_finalizer(*_args, **_kwargs):
        events.append("finalizer-started")
        started.set()
        assert release.wait(timeout=5)
        events.append("finalizer-settled")
        raise RuntimeError(PRIVATE_DETAIL)

    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "finalize_google_calendar_oauth_callback",
        blocking_finalizer,
    )

    async def cancel_while_finalizing():
        task = asyncio.create_task(_complete())
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.01)
        assert task.done() is False
        task.cancel()
        await asyncio.sleep(0.01)
        assert task.done() is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(cancel_while_finalizing())

    assert events == ["finalizer-started", "finalizer-settled"]


def test_anyio_level_cancellation_joins_without_spinning(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    events = []
    claim = _claim()
    token_result = _token_result()
    shield_calls = []
    real_shield = asyncio.shield

    def counted_shield(awaitable):
        shield_calls.append(True)
        return real_shield(awaitable)

    monkeypatch.setattr(
        google_calendar_oauth_callback_service.asyncio,
        "shield",
        counted_shield,
    )
    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "claim_google_calendar_oauth_callback",
        lambda *_args, **_kwargs: claim,
    )

    async def exchange_callback(**_kwargs):
        return token_result

    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "exchange_google_calendar_authorization_code_with_deadline",
        exchange_callback,
    )

    def blocking_finalizer(*_args, **_kwargs):
        events.append("finalizer-started")
        started.set()
        assert release.wait(timeout=5)
        events.append("finalizer-settled")
        return SUCCESS_STATUS

    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "finalize_google_calendar_oauth_callback",
        blocking_finalizer,
    )

    async def cancel_with_anyio_scope():
        scopes = []

        async def invoke():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await _complete()

        task = asyncio.create_task(invoke())
        assert await asyncio.to_thread(started.wait, 2)
        shield_calls.clear()
        scopes[0].cancel()

        heartbeat = asyncio.Event()

        async def beat():
            await asyncio.sleep(0.02)
            heartbeat.set()

        heartbeat_task = asyncio.create_task(beat())
        await asyncio.wait_for(heartbeat.wait(), timeout=0.5)
        await heartbeat_task
        assert task.done() is False
        assert len(shield_calls) <= 2

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    _run(cancel_with_anyio_scope())

    assert events == ["finalizer-started", "finalizer-settled"]


def test_failure_is_fixed_cause_free_and_secret_free(monkeypatch, caplog):
    def fail_claim(*_args, **_kwargs):
        raise RuntimeError(PRIVATE_DETAIL)

    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "claim_google_calendar_oauth_callback",
        fail_claim,
    )

    with pytest.raises(GoogleCalendarOAuthCallbackCompletionError) as raised:
        _run(_complete())

    assert str(raised.value) == (
        "Google Calendar authorization could not be completed."
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, frame_values = _module_traceback(raised.value)
    assert names == [
        "complete_google_calendar_oauth_callback",
        "_raise_completion_error",
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
        RAW_STATE,
        RAW_BROWSER_BINDING,
        AUTHORIZATION_CODE,
        CLIENT_SECRET,
        ACCOUNT_ID,
        SESSION_ID,
        CODE_VERIFIER,
        REFRESH_TOKEN,
        PRIVATE_DETAIL,
    ):
        assert private not in output


def test_ambient_exception_context_is_cleared():
    ambient_secret = "ambient-callback-service-detail-private"

    async def scenario():
        try:
            raise RuntimeError(ambient_secret)
        except RuntimeError:
            await _complete(None)

    with pytest.raises(GoogleCalendarOAuthCallbackCompletionError) as raised:
        _run(scenario())

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert ambient_secret not in _formatted_exception(raised.value)


def test_clean_import_and_application_startup_are_inert():
    project_root = Path(__file__).resolve().parents[1]
    child_environment = os.environ.copy()
    child_environment["SWIM_ENV"] = "test"
    script = "\n".join(
        (
            "calls = []",
            (
                "import app.google_calendar_oauth_callback_service "
                "as service"
            ),
            "def forbidden(*_args, **_kwargs):",
            "    calls.append(True)",
            "    raise AssertionError('callback service is call-time only')",
            "service.claim_google_calendar_oauth_callback = forbidden",
            (
                "service.exchange_google_calendar_authorization_code_with_deadline "
                "= forbidden"
            ),
            "service.finalize_google_calendar_oauth_callback = forbidden",
            "from app.api import create_app",
            "app = create_app()",
            (
                "assert not hasattr("
                "app.state, 'google_calendar_oauth_callback_service')"
            ),
            (
                "assert 'google_calendar_oauth_callback_service' "
                "not in app.state.routing_readiness"
            ),
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


class TrackingDatabaseSession(Session):
    def __init__(self, events):
        super().__init__(bind=db.engine)
        self.events = events
        self.was_closed = False
        self.backend_pid = None

    def close(self):
        self.events.append("session-close")
        self.was_closed = True
        super().close()


def _tracking_factory(events, sessions):
    def factory():
        session = TrackingDatabaseSession(events)
        sessions.append(session)

        def capture_backend_pid(_session, _transaction, connection):
            session.backend_pid = connection.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()

        event.listen(session, "after_begin", capture_backend_pid, once=True)
        return session

    return factory


def _add_owner(suffix: str):
    now = datetime.datetime.now(UTC)
    instructor_id = f"inst_callback_service_{suffix}"
    account_id = f"acct_callback_service_{suffix}"
    session_id = f"sess_callback_service_{suffix}"
    with db.SessionLocal.begin() as session:
        session.add(
            InstructorDB(
                id=instructor_id,
                name=f"Callback Service {suffix}",
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
                email_normalized=f"callback-service-{suffix}@example.com",
                password_hash="unused-test-password-hash",
                session_version=1,
                calendar_connection_revision=0,
                created_at=now - datetime.timedelta(hours=1),
                updated_at=now - datetime.timedelta(hours=1),
            )
        )
        session.flush()
        session.add(
            ProviderSessionDB(
                id=session_id,
                account_id=account_id,
                token_digest=hashlib.sha256(
                    f"callback-service-session-{suffix}".encode("ascii")
                ).digest(),
                session_version=1,
                created_at=now - datetime.timedelta(minutes=1),
                expires_at=now + datetime.timedelta(hours=1),
            )
        )
    return account_id, session_id, instructor_id, now


def _issue(account_id: str, session_id: str, now: datetime.datetime):
    with db.SessionLocal.begin() as session:
        return issue_google_calendar_oauth_state_with_pkce_and_browser_binding(
            session,
            account_id=account_id,
            session_id=session_id,
            pkce_cipher=PKCE_CIPHER,
            now=now,
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
        return session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=account_id
        ).one_or_none()


def test_postgres_composition_commits_before_network_and_finalizes_after(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, session_id, _instructor_id, now = _add_owner("success")
    issued = _issue(account_id, session_id, now)
    state_digest = _state_digest(issued)
    events = []
    sessions = []
    factory = _tracking_factory(events, sessions)
    observer_engine = create_engine(db.engine.url, hide_parameters=True)
    observer = observer_engine.connect()

    async def run_across_observed_gap():
        exchange_entered = asyncio.Event()
        release_exchange = asyncio.Event()

        async def exchange_callback(
            *, configuration, authorization_code, code_verifier
        ):
            events.append("exchange")
            assert type(configuration) is (
                GoogleCalendarTokenExchangeConfiguration
            )
            assert authorization_code == AUTHORIZATION_CODE
            exchange_entered.set()
            await release_exchange.wait()
            return _token_result(code_verifier=code_verifier)

        monkeypatch.setattr(
            google_calendar_oauth_callback_service,
            "exchange_google_calendar_authorization_code_with_deadline",
            exchange_callback,
        )
        completion = asyncio.create_task(
            _complete(
                factory,
                raw_state=issued.issued_state.raw_state,
                raw_browser_binding=issued.raw_browser_binding,
            )
        )
        await asyncio.wait_for(exchange_entered.wait(), timeout=5)

        assert len(sessions) == 1
        assert sessions[0].was_closed is True
        assert sessions[0].in_transaction() is False
        assert observer.execute(
            text(
                "SELECT count(*) FROM provider_calendar_oauth_states "
                "WHERE state_digest = :digest"
            ),
            {"digest": state_digest},
        ).scalar_one() == 0
        assert observer.execute(
            text(
                "SELECT count(*) FROM provider_calendar_oauth_pkce_verifiers "
                "WHERE state_digest = :digest"
            ),
            {"digest": state_digest},
        ).scalar_one() == 0
        assert observer.execute(
            text(
                "SELECT calendar_connection_revision FROM provider_accounts "
                "WHERE id = :account_id"
            ),
            {"account_id": account_id},
        ).scalar_one() == 1
        assert observer.execute(
            text(
                "SELECT count(*) FROM provider_calendar_connections "
                "WHERE account_id = :account_id"
            ),
            {"account_id": account_id},
        ).scalar_one() == 0
        assert sessions[0].backend_pid is not None
        assert observer.execute(
            text(
                "SELECT xact_start FROM pg_stat_activity WHERE pid = :pid"
            ),
            {"pid": sessions[0].backend_pid},
        ).scalar_one_or_none() is None

        release_exchange.set()
        return await completion

    try:
        status = _run(run_across_observed_gap())
    finally:
        observer.close()
        observer_engine.dispose()

    assert status.state == "connected"
    assert events == ["session-close", "exchange", "session-close"]
    assert len(sessions) == 2
    assert all(session.was_closed for session in sessions)
    assert _stored_revision(account_id) == 2
    assert _stored_oauth_pair(state_digest) == (False, False)
    assert _stored_connection(account_id) is not None


def test_postgres_exchange_failure_leaves_committed_claim_without_compensation(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, session_id, _instructor_id, now = _add_owner(
        "exchange-failure"
    )
    issued = _issue(account_id, session_id, now)
    state_digest = _state_digest(issued)
    events = []
    sessions = []
    factory = _tracking_factory(events, sessions)

    async def fail_exchange(**_kwargs):
        events.append("exchange")
        assert len(sessions) == 1
        assert sessions[0].was_closed is True
        raise RuntimeError(PRIVATE_DETAIL)

    monkeypatch.setattr(
        google_calendar_oauth_callback_service,
        "exchange_google_calendar_authorization_code_with_deadline",
        fail_exchange,
    )

    with pytest.raises(GoogleCalendarOAuthCallbackCompletionError):
        _run(
            _complete(
                factory,
                raw_state=issued.issued_state.raw_state,
                raw_browser_binding=issued.raw_browser_binding,
            )
        )

    assert events == ["session-close", "exchange"]
    assert len(sessions) == 1
    assert sessions[0].was_closed is True
    assert _stored_oauth_pair(state_digest) == (False, False)
    assert _stored_revision(account_id) == 1
    assert _stored_connection(account_id) is None
