from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import inspect
import json
import pickle
import traceback
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app import api, db, google_calendar_oauth_start, tools
from app.auth import (
    DEMO_PROVIDER_EMAIL,
    DEMO_PROVIDER_PASSWORD,
    authenticate_coach,
    revoke_presented_session,
)
from app.calendar_oauth_pkce import CalendarOAuthPKCECipher
from app.calendar_oauth_states import (
    IssuedGoogleCalendarOAuthBrowserBoundPKCEState,
    IssuedGoogleCalendarOAuthPKCEState,
)
from app.google_calendar_authorization import (
    GoogleCalendarAuthorizationRequest,
    build_google_calendar_authorization_request,
)
from app.google_calendar_oauth import (
    GOOGLE_CALENDAR_CALLBACK_PATH,
    GoogleCalendarOAuthConfiguration,
    GoogleCalendarOAuthError,
)
from app.google_calendar_oauth_start import (
    GoogleCalendarAuthorizationStart,
    GoogleCalendarAuthorizationStartError,
    prepare_google_calendar_authorization_start,
)
from app.models import (
    ProviderAccountDB,
    ProviderCalendarOAuthPKCEVerifierDB,
    ProviderCalendarOAuthStateDB,
)
from app.routing import HaversineRoutingProvider


UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
CLIENT_ID = "123456789-dayweave.apps.googleusercontent.com"
REDIRECT_URI = "https://api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH
ACCOUNT_ID = "acct_google_oauth_start_private"
SESSION_ID = "sess_google_oauth_start_private"
ROOT_KEY = b"k" * 32


def _canonical(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


RAW_STATE = _canonical(b"s" * 32)
CODE_CHALLENGE = _canonical(b"c" * 32)
RAW_BROWSER_BINDING = _canonical(b"b" * 32)
EXPIRES_AT = T0 + datetime.timedelta(minutes=10)
_BROWSER_BINDING_DOMAIN = (
    b"dayweave.calendar-oauth-browser-binding.sha256.v1\x00"
)
_PURE_TEST_ENGINE = create_engine("sqlite+pysqlite:///:memory:")


class TextSubclass(str):
    pass


class ConfigurationSubclass(GoogleCalendarOAuthConfiguration):
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


def _configuration():
    return GoogleCalendarOAuthConfiguration(
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
    )


def _cipher():
    return CalendarOAuthPKCECipher(
        active_key_id="key-a",
        root_keys={"key-a": ROOT_KEY},
    )


def _issuance(
    *,
    raw_state=RAW_STATE,
    code_challenge=CODE_CHALLENGE,
    raw_browser_binding=RAW_BROWSER_BINDING,
    expires_at=EXPIRES_AT,
):
    return IssuedGoogleCalendarOAuthBrowserBoundPKCEState(
        issued_state=IssuedGoogleCalendarOAuthPKCEState(
            raw_state=raw_state,
            code_challenge=code_challenge,
            expires_at=expires_at,
        ),
        raw_browser_binding=raw_browser_binding,
    )


def _recording_factory(
    events,
    *,
    commit_error=None,
    close_error=None,
):
    sessions = []

    def factory():
        events.append("factory")
        session = TrackingSession(events, close_error=close_error)
        sessions.append(session)
        event.listen(session, "after_commit", lambda _session: events.append("commit"))
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
    def issue(
        session,
        *,
        account_id,
        session_id,
        pkce_cipher,
        now,
    ):
        events.append("issue")
        assert session.in_transaction()
        assert account_id == ACCOUNT_ID
        assert session_id == SESSION_ID
        assert type(pkce_cipher) is CalendarOAuthPKCECipher
        assert now == T0
        return _issuance()

    real_builder = build_google_calendar_authorization_request

    def build(*, configuration, issued_state):
        events.append("build")
        assert sessions[0].was_closed is True
        assert sessions[0].in_transaction() is False
        return real_builder(
            configuration=configuration,
            issued_state=issued_state,
        )

    monkeypatch.setattr(
        google_calendar_oauth_start,
        "issue_google_calendar_oauth_state_with_pkce_and_browser_binding",
        issue,
    )
    monkeypatch.setattr(
        google_calendar_oauth_start,
        "build_google_calendar_authorization_request",
        build,
    )


def _prepare(session_factory, **overrides):
    values = {
        "account_id": ACCOUNT_ID,
        "session_id": SESSION_ID,
        "configuration": _configuration(),
        "pkce_cipher": _cipher(),
        "now": T0,
    }
    values.update(overrides)
    return prepare_google_calendar_authorization_start(
        session_factory,
        **values,
    )


def _module_traceback(error):
    names = []
    values = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith(
            "/app/google_calendar_oauth_start.py"
        ):
            names.append(current.tb_frame.f_code.co_name)
            values.extend(current.tb_frame.f_locals.values())
        current = current.tb_next
    return names, repr(values)


def _formatted_exception(error):
    return "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )


def test_public_surface_and_successful_commit_before_request_construction(
    monkeypatch,
):
    assert google_calendar_oauth_start.__all__ == (
        "GoogleCalendarAuthorizationStart",
        "GoogleCalendarAuthorizationStartError",
        "prepare_google_calendar_authorization_start",
    )
    assert issubclass(GoogleCalendarAuthorizationStartError, GoogleCalendarOAuthError)
    signature = inspect.signature(prepare_google_calendar_authorization_start)
    assert list(signature.parameters) == [
        "session_factory",
        "account_id",
        "session_id",
        "configuration",
        "pkce_cipher",
        "now",
    ]
    assert signature.parameters["session_factory"].kind is (
        inspect.Parameter.POSITIONAL_OR_KEYWORD
    )
    assert all(
        signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        for name in (
            "account_id",
            "session_id",
            "configuration",
            "pkce_cipher",
            "now",
        )
    )

    events = []
    session_factory, sessions = _recording_factory(events)
    _install_success_dependencies(monkeypatch, events, sessions)

    result = _prepare(session_factory)

    assert type(result) is GoogleCalendarAuthorizationStart
    assert type(result.authorization_request) is GoogleCalendarAuthorizationRequest
    assert result.raw_browser_binding == RAW_BROWSER_BINDING
    assert result.expires_at == EXPIRES_AT
    assert events == ["factory", "issue", "commit", "close", "build"]
    query = parse_qs(
        urlsplit(result.authorization_request.authorization_url).query,
        strict_parsing=True,
    )
    assert query["state"] == [RAW_STATE]
    assert query["code_challenge"] == [CODE_CHALLENGE]
    assert RAW_BROWSER_BINDING not in result.authorization_request.authorization_url


def test_start_handoff_is_builder_only_immutable_and_nonserializable(monkeypatch):
    events = []
    session_factory, sessions = _recording_factory(events)
    _install_success_dependencies(monkeypatch, events, sessions)
    result = _prepare(session_factory)

    assert not dataclasses.is_dataclass(result)
    assert not hasattr(result, "__dict__")
    assert repr(result) == "GoogleCalendarAuthorizationStart(<redacted>)"
    assert str(result) == repr(result)
    for private in (
        RAW_STATE,
        RAW_BROWSER_BINDING,
        CLIENT_ID,
        REDIRECT_URI,
    ):
        assert private not in repr(result)
    with pytest.raises(AttributeError):
        result.raw_browser_binding = "replacement"
    with pytest.raises(AttributeError):
        del result.authorization_request
    with pytest.raises(TypeError):
        type("UnsafeStart", (GoogleCalendarAuthorizationStart,), {})
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

    with pytest.raises(GoogleCalendarAuthorizationStartError) as raised:
        GoogleCalendarAuthorizationStart(
            authorization_request=result.authorization_request,
            raw_browser_binding=RAW_BROWSER_BINDING,
            expires_at=EXPIRES_AT,
        )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    names, values = _module_traceback(raised.value)
    assert names == ["__init__", "_raise_start_error"]
    assert RAW_BROWSER_BINDING not in values


def test_invalid_dependencies_fail_before_session_creation():
    factory_calls = []

    def forbidden_factory():
        factory_calls.append(True)
        raise AssertionError("session creation must not occur")

    cases = (
        {"account_id": None},
        {"account_id": ""},
        {"account_id": " acct"},
        {"account_id": "acct\nprivate"},
        {"account_id": "café"},
        {"account_id": "x" * 256},
        {"account_id": TextSubclass(ACCOUNT_ID)},
        {"session_id": None},
        {"configuration": object()},
        {
            "configuration": ConfigurationSubclass(
                client_id=CLIENT_ID,
                redirect_uri=REDIRECT_URI,
            )
        },
        {"pkce_cipher": object()},
        {"now": datetime.datetime(2026, 11, 14, 12, 0)},
        {"now": "2026-11-14T12:00:00Z"},
    )

    for overrides in cases:
        with pytest.raises(GoogleCalendarAuthorizationStartError):
            _prepare(forbidden_factory, **overrides)

    with pytest.raises(GoogleCalendarAuthorizationStartError):
        _prepare(None)
    assert factory_calls == []


def test_existing_transaction_is_rejected_and_owned_session_is_closed():
    events = []
    session = TrackingSession(events)
    transaction = session.begin()

    with pytest.raises(GoogleCalendarAuthorizationStartError):
        _prepare(lambda: session)

    assert session.was_closed is True
    assert events == ["close"]
    transaction.close()


def test_externally_transactional_connection_is_rejected_before_issuance(
    monkeypatch,
):
    calls = []
    engine = create_engine("sqlite+pysqlite:///:memory:")
    connection = engine.connect()
    outer_transaction = connection.begin()
    session = Session(bind=connection)
    monkeypatch.setattr(
        google_calendar_oauth_start,
        "issue_google_calendar_oauth_state_with_pkce_and_browser_binding",
        lambda *_args, **_kwargs: calls.append("issue"),
    )
    monkeypatch.setattr(
        google_calendar_oauth_start,
        "build_google_calendar_authorization_request",
        lambda **_kwargs: calls.append("build"),
    )

    try:
        with pytest.raises(GoogleCalendarAuthorizationStartError):
            _prepare(lambda: session)

        assert calls == []
        assert outer_transaction.is_active is True
    finally:
        outer_transaction.rollback()
        connection.close()
        engine.dispose()


def test_mapper_bound_external_transaction_is_rejected_before_issuance(
    monkeypatch,
):
    calls = []
    engine = create_engine("sqlite+pysqlite:///:memory:")
    connection = engine.connect()
    outer_transaction = connection.begin()
    session = Session(
        bind=engine,
        binds={ProviderAccountDB: connection},
    )
    monkeypatch.setattr(
        google_calendar_oauth_start,
        "issue_google_calendar_oauth_state_with_pkce_and_browser_binding",
        lambda *_args, **_kwargs: calls.append("issue"),
    )
    monkeypatch.setattr(
        google_calendar_oauth_start,
        "build_google_calendar_authorization_request",
        lambda **_kwargs: calls.append("build"),
    )

    try:
        with pytest.raises(GoogleCalendarAuthorizationStartError):
            _prepare(lambda: session)

        assert calls == []
        assert outer_transaction.is_active is True
    finally:
        outer_transaction.rollback()
        connection.close()
        engine.dispose()


def test_default_operation_time_commits_and_builds(monkeypatch):
    events = []
    session_factory, sessions = _recording_factory(events)

    def issue(
        session,
        *,
        account_id,
        session_id,
        pkce_cipher,
        now,
    ):
        del account_id, session_id, pkce_cipher
        events.append("issue")
        assert session.in_transaction()
        assert now is None
        return _issuance()

    real_builder = build_google_calendar_authorization_request

    def build(*, configuration, issued_state):
        events.append("build")
        assert sessions[0].was_closed is True
        return real_builder(
            configuration=configuration,
            issued_state=issued_state,
        )

    monkeypatch.setattr(
        google_calendar_oauth_start,
        "issue_google_calendar_oauth_state_with_pkce_and_browser_binding",
        issue,
    )
    monkeypatch.setattr(
        google_calendar_oauth_start,
        "build_google_calendar_authorization_request",
        build,
    )

    result = _prepare(session_factory, now=None)

    assert type(result) is GoogleCalendarAuthorizationStart
    assert events == ["factory", "issue", "commit", "close", "build"]


@pytest.mark.parametrize(
    "issuance",
    [
        object(),
        _issuance(raw_state="bad"),
        _issuance(code_challenge="bad"),
        _issuance(raw_browser_binding="bad"),
        _issuance(raw_browser_binding=RAW_STATE),
        _issuance(expires_at=datetime.datetime(2026, 11, 14, 12, 10)),
        _issuance(
            expires_at=datetime.datetime(
                2026,
                11,
                14,
                13,
                10,
                tzinfo=datetime.timezone(datetime.timedelta(hours=1)),
            )
        ),
        _issuance(expires_at=T0),
    ],
)
def test_malformed_issuance_rolls_back_before_builder(
    monkeypatch,
    issuance,
):
    events = []
    session_factory, sessions = _recording_factory(events)

    def issue(*_args, **_kwargs):
        events.append("issue")
        return issuance

    builder_calls = []
    monkeypatch.setattr(
        google_calendar_oauth_start,
        "issue_google_calendar_oauth_state_with_pkce_and_browser_binding",
        issue,
    )
    monkeypatch.setattr(
        google_calendar_oauth_start,
        "build_google_calendar_authorization_request",
        lambda **_kwargs: builder_calls.append(True),
    )

    with pytest.raises(GoogleCalendarAuthorizationStartError):
        _prepare(session_factory)

    assert builder_calls == []
    assert sessions[0].was_closed is True
    assert events == ["factory", "issue", "rollback", "close"]


def test_commit_failure_returns_no_handoff_and_closes_session(monkeypatch):
    events = []
    session_factory, sessions = _recording_factory(
        events,
        commit_error=RuntimeError("commit-private-detail"),
    )
    monkeypatch.setattr(
        google_calendar_oauth_start,
        "issue_google_calendar_oauth_state_with_pkce_and_browser_binding",
        lambda *_args, **_kwargs: (events.append("issue") or _issuance()),
    )
    builder_calls = []
    monkeypatch.setattr(
        google_calendar_oauth_start,
        "build_google_calendar_authorization_request",
        lambda **_kwargs: builder_calls.append(True),
    )

    with pytest.raises(GoogleCalendarAuthorizationStartError):
        _prepare(session_factory)

    assert builder_calls == []
    assert sessions[0].was_closed is True
    assert events[:3] == ["factory", "issue", "commit_attempt"]
    assert events[-1] == "close"


def test_session_close_failure_prevents_request_construction(monkeypatch):
    events = []
    session_factory, _ = _recording_factory(
        events,
        close_error=RuntimeError("close-private-detail"),
    )
    monkeypatch.setattr(
        google_calendar_oauth_start,
        "issue_google_calendar_oauth_state_with_pkce_and_browser_binding",
        lambda *_args, **_kwargs: (events.append("issue") or _issuance()),
    )
    builder_calls = []
    monkeypatch.setattr(
        google_calendar_oauth_start,
        "build_google_calendar_authorization_request",
        lambda **_kwargs: builder_calls.append(True),
    )

    with pytest.raises(GoogleCalendarAuthorizationStartError):
        _prepare(session_factory)

    assert events == ["factory", "issue", "commit", "close"]
    assert builder_calls == []


def test_builder_failure_occurs_after_commit_without_compensating_session(
    monkeypatch,
):
    events = []
    session_factory, sessions = _recording_factory(events)
    monkeypatch.setattr(
        google_calendar_oauth_start,
        "issue_google_calendar_oauth_state_with_pkce_and_browser_binding",
        lambda *_args, **_kwargs: (events.append("issue") or _issuance()),
    )

    def fail_builder(**_kwargs):
        events.append("build")
        assert sessions[0].was_closed is True
        raise RuntimeError("builder-private-detail")

    monkeypatch.setattr(
        google_calendar_oauth_start,
        "build_google_calendar_authorization_request",
        fail_builder,
    )

    with pytest.raises(GoogleCalendarAuthorizationStartError):
        _prepare(session_factory)

    assert events == ["factory", "issue", "commit", "close", "build"]
    assert len(sessions) == 1


def test_failures_have_fixed_secret_free_diagnostics(monkeypatch, caplog):
    events = []
    session_factory, _ = _recording_factory(events)

    def fail_issuance(*_args, **_kwargs):
        raise RuntimeError(
            " ".join(
                (
                    ACCOUNT_ID,
                    SESSION_ID,
                    CLIENT_ID,
                    REDIRECT_URI,
                    RAW_STATE,
                    RAW_BROWSER_BINDING,
                    ROOT_KEY.decode("ascii"),
                )
            )
        )

    monkeypatch.setattr(
        google_calendar_oauth_start,
        "issue_google_calendar_oauth_state_with_pkce_and_browser_binding",
        fail_issuance,
    )

    with pytest.raises(GoogleCalendarAuthorizationStartError) as raised:
        _prepare(session_factory)

    assert str(raised.value) == (
        "Google Calendar authorization could not be started."
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, frame_values = _module_traceback(raised.value)
    assert names == [
        "prepare_google_calendar_authorization_start",
        "_raise_start_error",
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
        CLIENT_ID,
        REDIRECT_URI,
        RAW_STATE,
        RAW_BROWSER_BINDING,
        ROOT_KEY.decode("ascii"),
    ):
        assert private not in output


def test_failure_clears_secret_bearing_ambient_context():
    ambient_secret = "ambient-google-start-private-detail"

    try:
        raise RuntimeError(ambient_secret)
    except RuntimeError:
        with pytest.raises(GoogleCalendarAuthorizationStartError) as raised:
            _prepare(None)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    _, frame_values = _module_traceback(raised.value)
    assert ambient_secret not in " ".join(
        (_formatted_exception(raised.value), frame_values)
    )


def test_import_construction_and_application_startup_remain_inert(monkeypatch):
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("OAuth start must remain call-time only")

    monkeypatch.setattr(
        google_calendar_oauth_start,
        "issue_google_calendar_oauth_state_with_pkce_and_browser_binding",
        forbidden,
    )
    monkeypatch.setattr(
        google_calendar_oauth_start,
        "build_google_calendar_authorization_request",
        forbidden,
    )

    try:
        app = api.create_app()
        assert not hasattr(app.state, "google_calendar_authorization_start")
        assert "google_calendar_authorization_start" not in app.state.routing_readiness
        assert "httpx" not in google_calendar_oauth_start.__dict__
        assert calls == []
    finally:
        tools.configure_default_routing_provider(HaversineRoutingProvider())
        tools.reset_routing_provider()


@pytest.mark.integration
def test_real_start_is_committed_digest_only_replaced_and_session_bound(
    migrated_postgres_db,
    monkeypatch,
):
    issued_session = authenticate_coach(
        DEMO_PROVIDER_EMAIL,
        DEMO_PROVIDER_PASSWORD,
        now=T0,
    )
    principal = issued_session.principal
    configuration = _configuration()
    cipher = _cipher()
    real_builder = build_google_calendar_authorization_request
    visible_digests = []

    def assert_committed_before_build(*, configuration, issued_state):
        state_digest = hashlib.sha256(
            issued_state.raw_state.encode("ascii")
        ).digest()
        with db.SessionLocal() as observer:
            persisted = observer.get(ProviderCalendarOAuthStateDB, state_digest)
            assert persisted is not None
            visible_digests.append(bytes(persisted.state_digest))
        return real_builder(
            configuration=configuration,
            issued_state=issued_state,
        )

    monkeypatch.setattr(
        google_calendar_oauth_start,
        "build_google_calendar_authorization_request",
        assert_committed_before_build,
    )

    first = prepare_google_calendar_authorization_start(
        db.SessionLocal,
        account_id=principal.account_id,
        session_id=principal.session_id,
        configuration=configuration,
        pkce_cipher=cipher,
        now=T0,
    )
    second = prepare_google_calendar_authorization_start(
        db.SessionLocal,
        account_id=principal.account_id,
        session_id=principal.session_id,
        configuration=configuration,
        pkce_cipher=cipher,
        now=T0 + datetime.timedelta(seconds=1),
    )

    first_state = parse_qs(
        urlsplit(first.authorization_request.authorization_url).query
    )["state"][0]
    second_state = parse_qs(
        urlsplit(second.authorization_request.authorization_url).query
    )["state"][0]
    assert first_state != second_state
    second_digest = hashlib.sha256(second_state.encode("ascii")).digest()
    binding_bytes = base64.b64decode(
        second.raw_browser_binding.encode("ascii") + b"=",
        altchars=b"-_",
        validate=True,
    )
    expected_binding_digest = hashlib.sha256(
        _BROWSER_BINDING_DOMAIN + binding_bytes
    ).digest()

    with db.SessionLocal() as observer:
        states = observer.query(ProviderCalendarOAuthStateDB).all()
        verifiers = observer.query(ProviderCalendarOAuthPKCEVerifierDB).all()
        assert len(states) == 1
        assert len(verifiers) == 1
        assert bytes(states[0].state_digest) == second_digest
        assert bytes(states[0].browser_binding_digest) == expected_binding_digest
        assert verifiers[0].state_digest == second_digest
        assert verifiers[0].verifier_ciphertext
        assert first_state.encode("ascii") not in bytes(states[0].state_digest)
        assert second_state.encode("ascii") not in bytes(states[0].state_digest)
        assert second.raw_browser_binding.encode("ascii") not in bytes(
            states[0].browser_binding_digest
        )

    assert visible_digests == [
        hashlib.sha256(first_state.encode("ascii")).digest(),
        second_digest,
    ]

    revoke_presented_session(
        issued_session.raw_token,
        now=T0 + datetime.timedelta(seconds=2),
    )
    with pytest.raises(GoogleCalendarAuthorizationStartError):
        prepare_google_calendar_authorization_start(
            db.SessionLocal,
            account_id=principal.account_id,
            session_id=principal.session_id,
            configuration=configuration,
            pkce_cipher=cipher,
            now=T0 + datetime.timedelta(seconds=3),
        )
    with db.SessionLocal() as observer:
        remaining = observer.query(ProviderCalendarOAuthStateDB).one()
        assert bytes(remaining.state_digest) == second_digest
