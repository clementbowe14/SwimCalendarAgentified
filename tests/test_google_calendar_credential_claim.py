from __future__ import annotations

import asyncio
import dataclasses
import datetime
import dis
import inspect
import json
import logging
import pickle
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from app import (
    db,
    google_calendar_credential_claim,
    google_calendar_credential_handoff,
    google_calendar_refresh_recovery,
    google_calendar_token_exchange,
)
from app.calendar_connections import (
    disconnect_google_calendar,
    mark_google_calendar_authorization_required,
    replace_google_calendar_connection,
)
from app.calendar_credentials import CalendarCredentialCipher
from app.google_calendar_credential_claim import (
    GoogleCalendarRefreshCredentialClaim,
    GoogleCalendarRefreshCredentialClaimError,
    claim_google_calendar_refresh_credential,
    revalidate_google_calendar_refresh_credential_claim,
)
from app.google_calendar_credential_handoff import (
    GoogleCalendarValidatedRefreshCredentialClaim,
    validate_google_calendar_refresh_credential_claim,
)
from app.google_calendar_oauth import GoogleCalendarOAuthError
from app.google_calendar_refresh_recovery import (
    GoogleCalendarRefreshRecoveryError,
    mark_google_calendar_refresh_credential_authorization_required,
)
from app.google_calendar_token_exchange import (
    GoogleCalendarRefreshAuthorizationRequired,
)
from app.models import (
    InstructorDB,
    ProviderAccountDB,
    ProviderCalendarConnectionDB,
)


UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 11, 19, 14, 0, tzinfo=UTC)
KEY_ID = "refresh-claim-key"
KEY = b"c" * 32
CIPHER = CalendarCredentialCipher(active_key_id=KEY_ID, keys={KEY_ID: KEY})
_PURE_TEST_ENGINE = create_engine("sqlite+pysqlite:///:memory:")


class TextSubclass(str):
    pass


class CipherSubclass(CalendarCredentialCipher):
    pass


class PrivateBaseException(BaseException):
    pass


class HostileSystemExit(SystemExit):
    @property
    def code(self):
        raise RuntimeError("system-exit-getter-private")


class IncompleteTimezone(datetime.tzinfo):
    def utcoffset(self, _value):
        return None

    def dst(self, _value):
        return None


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

    def commit(self):
        return self.transaction.commit()

    def rollback(self):
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


def _recording_factory(
    events,
    *,
    bind=_PURE_TEST_ENGINE,
    commit_error=None,
    close_error=None,
):
    sessions = []

    def factory():
        events.append("factory")
        session = TrackingSession(
            events,
            bind=bind,
            close_error=close_error,
        )
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


def _claim(session_factory, **overrides):
    values = {
        "instructor_id": "inst_refresh_claim_private",
        "credential_cipher": CIPHER,
    }
    values.update(overrides)
    return claim_google_calendar_refresh_credential(session_factory, **values)


def _sealed_claim(
    *,
    account_id="acct_refresh_claim_private",
    instructor_id="inst_refresh_claim_private",
    connection_id="calconn_" + "1" * 32,
    revision=7,
    refresh_token="refresh-token-private",
):
    return google_calendar_credential_claim._new_claim(
        account_id=account_id,
        instructor_id=instructor_id,
        connection_id=connection_id,
        expected_calendar_connection_revision=revision,
        refresh_token=refresh_token,
    )


def _forged_claim(**mutations):
    claim = _sealed_claim()
    field_names = {
        "revision": "expected_calendar_connection_revision",
    }
    for name, value in mutations.items():
        object.__setattr__(claim, field_names.get(name, name), value)
    return claim


def _recovery_outcome(claim=None, **mutations):
    source = claim or _sealed_claim()
    values = {
        "account_id": source.account_id,
        "instructor_id": source.instructor_id,
        "connection_id": source.connection_id,
        "expected_calendar_connection_revision": (
            source.expected_calendar_connection_revision
        ),
    }
    values.update(mutations)
    return google_calendar_token_exchange._new_refresh_authorization_required(
        **values
    )


def _module_traceback(error):
    names = []
    values = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith(
            (
                "/app/google_calendar_credential_claim.py",
                "/app/google_calendar_credential_handoff.py",
                "/app/google_calendar_refresh_recovery.py",
            )
        ):
            names.append(current.tb_frame.f_code.co_name)
            values.extend(current.tb_frame.f_locals.values())
        current = current.tb_next
    return names, repr(values)


def _module_frame_locals(error):
    result = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith(
            (
                "/app/google_calendar_credential_claim.py",
                "/app/google_calendar_credential_handoff.py",
                "/app/google_calendar_refresh_recovery.py",
            )
        ):
            result.append(dict(current.tb_frame.f_locals))
        current = current.tb_next
    return result


def _formatted_exception(error):
    return "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )


def _install_success_snapshot(monkeypatch, events, sessions):
    def locked(session, *, instructor_id, credential_cipher):
        events.append("locked")
        assert session.in_transaction()
        assert instructor_id == "inst_refresh_claim_private"
        assert type(credential_cipher) is CalendarCredentialCipher
        return (
            "acct_refresh_claim_private",
            instructor_id,
            "calconn_" + "1" * 32,
            7,
            "refresh-token-private",
        )

    real_builder = google_calendar_credential_claim._new_claim

    def build(**values):
        events.append("build")
        assert sessions[0].was_closed is True
        assert sessions[0].in_transaction() is False
        return real_builder(**values)

    monkeypatch.setattr(
        google_calendar_credential_claim,
        "_claim_locked_refresh_credential",
        locked,
    )
    monkeypatch.setattr(
        google_calendar_credential_claim,
        "_new_claim",
        build,
    )


def test_public_surface_commits_and_closes_before_return(monkeypatch):
    assert google_calendar_credential_claim.__all__ == (
        "GoogleCalendarRefreshCredentialClaim",
        "GoogleCalendarRefreshCredentialClaimError",
        "GoogleCalendarRefreshCredentialClaimResult",
        "GoogleCalendarRefreshCredentialState",
        "claim_google_calendar_refresh_credential",
        "revalidate_google_calendar_refresh_credential_claim",
    )
    assert google_calendar_refresh_recovery.__all__ == (
        "GoogleCalendarRefreshRecoveryError",
        "mark_google_calendar_refresh_credential_authorization_required",
    )
    assert issubclass(
        GoogleCalendarRefreshCredentialClaimError,
        GoogleCalendarOAuthError,
    )
    assert issubclass(
        GoogleCalendarRefreshRecoveryError,
        GoogleCalendarOAuthError,
    )
    claim_signature = inspect.signature(
        claim_google_calendar_refresh_credential
    )
    assert list(claim_signature.parameters) == [
        "session_factory",
        "instructor_id",
        "credential_cipher",
    ]
    assert claim_signature.parameters["session_factory"].kind is (
        inspect.Parameter.POSITIONAL_OR_KEYWORD
    )
    assert all(
        claim_signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        for name in ("instructor_id", "credential_cipher")
    )
    revalidate_signature = inspect.signature(
        revalidate_google_calendar_refresh_credential_claim
    )
    assert list(revalidate_signature.parameters) == [
        "session_factory",
        "claim",
    ]
    assert revalidate_signature.parameters["claim"].kind is (
        inspect.Parameter.KEYWORD_ONLY
    )
    recovery_signature = inspect.signature(
        mark_google_calendar_refresh_credential_authorization_required
    )
    assert list(recovery_signature.parameters) == [
        "session_factory",
        "claim",
        "outcome",
    ]
    assert all(
        recovery_signature.parameters[name].kind
        is inspect.Parameter.KEYWORD_ONLY
        for name in ("claim", "outcome")
    )

    events = []
    session_factory, sessions = _recording_factory(events)
    _install_success_snapshot(monkeypatch, events, sessions)

    result = _claim(session_factory)

    assert type(result) is GoogleCalendarRefreshCredentialClaim
    assert result.account_id == "acct_refresh_claim_private"
    assert result.instructor_id == "inst_refresh_claim_private"
    assert result.connection_id == "calconn_" + "1" * 32
    assert result.expected_calendar_connection_revision == 7
    assert result.refresh_token == "refresh-token-private"
    assert events == ["factory", "locked", "commit", "close", "build"]


def test_claim_handoff_is_builder_only_immutable_and_nonserializable(monkeypatch):
    events = []
    session_factory, sessions = _recording_factory(events)
    _install_success_snapshot(monkeypatch, events, sessions)
    result = _claim(session_factory)

    assert not dataclasses.is_dataclass(result)
    assert not hasattr(result, "__dict__")
    assert repr(result) == "GoogleCalendarRefreshCredentialClaim(<redacted>)"
    assert str(result) == repr(result)
    for private in (
        result.account_id,
        result.instructor_id,
        result.connection_id,
        result.refresh_token,
    ):
        assert private not in repr(result)
    with pytest.raises(AttributeError):
        result.refresh_token = "replacement"
    with pytest.raises(AttributeError):
        del result.account_id
    with pytest.raises(TypeError):
        type("UnsafeClaim", (GoogleCalendarRefreshCredentialClaim,), {})
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

    with pytest.raises(GoogleCalendarRefreshCredentialClaimError) as raised:
        GoogleCalendarRefreshCredentialClaim(
            account_id=result.account_id,
            instructor_id=result.instructor_id,
            connection_id=result.connection_id,
            expected_calendar_connection_revision=7,
            refresh_token=result.refresh_token,
        )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    names, values = _module_traceback(raised.value)
    assert names == ["__init__", "_raise_claim_error"]
    for private in (
        result.account_id,
        result.instructor_id,
        result.connection_id,
        result.refresh_token,
    ):
        assert private not in values


def test_supported_validator_returns_an_independent_sealed_snapshot():
    assert google_calendar_credential_handoff.__all__ == (
        "GoogleCalendarRefreshCredentialClaim",
        "GoogleCalendarRefreshCredentialClaimError",
        "GoogleCalendarValidatedRefreshCredentialClaim",
        "validate_google_calendar_refresh_credential_claim",
    )
    claim = _sealed_claim()

    snapshot = validate_google_calendar_refresh_credential_claim(claim)
    object.__setattr__(claim, "refresh_token", "mutated-after-validation")

    assert type(snapshot) is GoogleCalendarValidatedRefreshCredentialClaim
    assert snapshot.account_id == "acct_refresh_claim_private"
    assert snapshot.instructor_id == "inst_refresh_claim_private"
    assert snapshot.connection_id == "calconn_" + "1" * 32
    assert snapshot.expected_calendar_connection_revision == 7
    assert snapshot.refresh_token == "refresh-token-private"
    assert not hasattr(snapshot, "__dict__")
    assert repr(snapshot) == (
        "GoogleCalendarValidatedRefreshCredentialClaim(<redacted>)"
    )
    assert snapshot.refresh_token not in repr(snapshot)
    with pytest.raises(AttributeError):
        snapshot.refresh_token = "replacement"
    for serializer in (
        dataclasses.asdict,
        dataclasses.astuple,
        vars,
        pickle.dumps,
        json.dumps,
        jsonable_encoder,
    ):
        with pytest.raises((TypeError, ValueError)):
            serializer(snapshot)
    with pytest.raises(GoogleCalendarRefreshCredentialClaimError):
        GoogleCalendarValidatedRefreshCredentialClaim(
            account_id=snapshot.account_id,
            instructor_id=snapshot.instructor_id,
            connection_id=snapshot.connection_id,
            expected_calendar_connection_revision=7,
            refresh_token=snapshot.refresh_token,
        )


@pytest.mark.parametrize("target_kind", ["claim", "snapshot"])
@pytest.mark.parametrize("operation", ["assign", "delete"])
def test_sealed_mutation_traceback_does_not_retain_attempted_secret(
    target_kind,
    operation,
    caplog,
):
    caplog.set_level(logging.DEBUG)
    claim = _sealed_claim()
    target = (
        claim
        if target_kind == "claim"
        else validate_google_calendar_refresh_credential_claim(claim)
    )
    attempted_secret = f"attempted-{target_kind}-refresh-private"
    attempted_name = f"attempted_{target_kind}_{operation}_attribute_private"

    try:
        raise RuntimeError("ambient-mutation-private")
    except RuntimeError:
        with pytest.raises(AttributeError) as raised:
            if operation == "assign":
                setattr(target, attempted_name, attempted_secret)
            else:
                delattr(target, attempted_name)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, frame_values = _module_traceback(raised.value)
    expected_mutator = (
        "__setattr__" if operation == "assign" else "__delattr__"
    )
    assert names == [expected_mutator, "_raise_immutable_error"]
    output = " ".join(
        (
            str(raised.value),
            repr(raised.value),
            _formatted_exception(raised.value),
            frame_values,
            caplog.text,
        )
    )
    assert attempted_secret not in output
    assert attempted_name not in output
    assert "ambient-mutation-private" not in output


@pytest.mark.parametrize("target_kind", ["claim", "snapshot"])
@pytest.mark.parametrize(
    ("hook_name", "expected_frame"),
    [
        ("reduce", "__reduce__"),
        ("reduce_ex", "__reduce_ex__"),
        ("getstate", "__getstate__"),
    ],
)
def test_serialization_hook_traceback_does_not_retain_sealed_secret(
    target_kind,
    hook_name,
    expected_frame,
    caplog,
):
    caplog.set_level(logging.DEBUG)
    claim = _sealed_claim(refresh_token="serialization-refresh-private")
    target = (
        claim
        if target_kind == "claim"
        else validate_google_calendar_refresh_credential_claim(claim)
    )

    try:
        raise RuntimeError("ambient-serialization-private")
    except RuntimeError:
        with pytest.raises(TypeError) as raised:
            if hook_name == "reduce":
                target.__reduce__()
            elif hook_name == "reduce_ex":
                target.__reduce_ex__(5)
            else:
                target.__getstate__()

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, frame_values = _module_traceback(raised.value)
    assert names == [expected_frame, "_raise_serialization_error"]
    output = " ".join(
        (
            str(raised.value),
            repr(raised.value),
            _formatted_exception(raised.value),
            frame_values,
            caplog.text,
        )
    )
    assert "serialization-refresh-private" not in output
    assert "ambient-serialization-private" not in output


@pytest.mark.parametrize(
    "base_class",
    [
        GoogleCalendarRefreshCredentialClaim,
        GoogleCalendarValidatedRefreshCredentialClaim,
    ],
)
def test_subclass_rejection_does_not_retain_keyword_or_ambient_secret(
    base_class,
    caplog,
):
    caplog.set_level(logging.DEBUG)
    attempted_secret = "subclass-keyword-private"

    try:
        raise RuntimeError("ambient-subclass-private")
    except RuntimeError:
        with pytest.raises(TypeError) as raised:

            class UnsafeClaim(
                base_class,
                private_option=attempted_secret,
            ):
                pass

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, frame_values = _module_traceback(raised.value)
    assert names == ["__init_subclass__", "_raise_subclass_error"]
    output = " ".join(
        (
            str(raised.value),
            repr(raised.value),
            _formatted_exception(raised.value),
            frame_values,
            caplog.text,
        )
    )
    assert attempted_secret not in output
    assert "ambient-subclass-private" not in output


@pytest.mark.parametrize(
    "candidate",
    [
        None,
        object(),
        _forged_claim(account_id="acct_refresh_claim_second"),
        _forged_claim(instructor_id="inst_refresh_claim_second"),
        _forged_claim(connection_id="calconn_" + "2" * 32),
        _forged_claim(revision=8),
        _forged_claim(refresh_token="cross-tenant-refresh-private"),
        _forged_claim(_integrity_tag=b"x" * 32),
    ],
)
def test_supported_validator_rejects_invalid_claims_without_detail(candidate):

    with pytest.raises(GoogleCalendarRefreshCredentialClaimError) as raised:
        validate_google_calendar_refresh_credential_claim(candidate)

    assert str(raised.value) == (
        "Google Calendar refresh credential could not be claimed safely."
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, frame_values = _module_traceback(raised.value)
    assert names == [
        "validate_google_calendar_refresh_credential_claim",
        "_raise_claim_error",
    ]
    assert "cross-tenant-refresh-private" not in frame_values
    assert "cross-tenant-refresh-private" not in _formatted_exception(
        raised.value
    )


def test_handoff_module_import_is_database_free():
    code = """
import sys
from app import google_calendar_credential_handoff
assert callable(
    google_calendar_credential_handoff.validate_google_calendar_refresh_credential_claim
)
assert 'app.models' not in sys.modules
assert 'sqlalchemy' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    ("error", "expected_type", "expected_helper", "expected_exit_code"),
    [
        (
            asyncio.CancelledError("control-private-detail"),
            asyncio.CancelledError,
            "_raise_control_exception",
            None,
        ),
        (
            KeyboardInterrupt("control-private-detail"),
            KeyboardInterrupt,
            "_raise_control_exception",
            None,
        ),
        (
            SystemExit(7),
            SystemExit,
            "_raise_control_exception",
            7,
        ),
        (
            SystemExit("control-private-detail"),
            SystemExit,
            "_raise_control_exception",
            1,
        ),
        (
            HostileSystemExit("control-private-detail"),
            SystemExit,
            "_raise_control_exception",
            1,
        ),
        (
            GeneratorExit("control-private-detail"),
            GeneratorExit,
            "_raise_control_exception",
            None,
        ),
        (
            PrivateBaseException("control-private-detail"),
            GoogleCalendarRefreshCredentialClaimError,
            "_raise_claim_error",
            None,
        ),
    ],
)
def test_validator_sanitizes_every_base_exception_path(
    monkeypatch,
    caplog,
    error,
    expected_type,
    expected_helper,
    expected_exit_code,
):
    caplog.set_level(logging.DEBUG)
    claim = _sealed_claim(refresh_token="control-refresh-token-private")

    def fail_integrity_tag(**_kwargs):
        raise error

    monkeypatch.setattr(
        google_calendar_credential_handoff,
        "_claim_integrity_tag",
        fail_integrity_tag,
    )

    with pytest.raises(expected_type) as raised:
        validate_google_calendar_refresh_credential_claim(claim)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    if expected_type is SystemExit:
        assert raised.value.code == expected_exit_code
    names, frame_values = _module_traceback(raised.value)
    assert names == [
        "validate_google_calendar_refresh_credential_claim",
        expected_helper,
    ]
    output = " ".join(
        (
            repr(raised.value),
            _formatted_exception(raised.value),
            frame_values,
            caplog.text,
        )
    )
    for private in (
        "control-private-detail",
        "control-refresh-token-private",
        "acct_refresh_claim_private",
        "inst_refresh_claim_private",
    ):
        assert private not in output


@pytest.mark.parametrize("boundary", ["claim", "revalidate"])
@pytest.mark.parametrize(
    (
        "exception_factory",
        "expected_type",
        "expected_helper",
        "expected_exit_code",
    ),
    [
        (
            lambda: asyncio.CancelledError("boundary-control-private"),
            asyncio.CancelledError,
            "_raise_control_exception",
            None,
        ),
        (
            lambda: KeyboardInterrupt("boundary-control-private"),
            KeyboardInterrupt,
            "_raise_control_exception",
            None,
        ),
        (
            lambda: SystemExit(7),
            SystemExit,
            "_raise_control_exception",
            7,
        ),
        (
            lambda: SystemExit("boundary-control-private"),
            SystemExit,
            "_raise_control_exception",
            1,
        ),
        (
            lambda: HostileSystemExit("boundary-control-private"),
            SystemExit,
            "_raise_control_exception",
            1,
        ),
        (
            lambda: GeneratorExit("boundary-control-private"),
            GeneratorExit,
            "_raise_control_exception",
            None,
        ),
        (
            lambda: PrivateBaseException("boundary-control-private"),
            GoogleCalendarRefreshCredentialClaimError,
            "_raise_claim_error",
            None,
        ),
    ],
)
def test_public_claim_boundaries_remove_secret_objects_from_control_tracebacks(
    monkeypatch,
    caplog,
    boundary,
    exception_factory,
    expected_type,
    expected_helper,
    expected_exit_code,
):
    caplog.set_level(logging.DEBUG)
    claim = _sealed_claim(refresh_token="boundary-refresh-token-private")

    def fail_boundary(*_args, **_kwargs):
        raise exception_factory()

    if boundary == "claim":
        monkeypatch.setattr(
            google_calendar_credential_claim,
            "_claim_google_calendar_refresh_credential",
            fail_boundary,
        )
        operation = lambda: _claim(lambda: None)
        expected_public_frame = (
            "claim_google_calendar_refresh_credential"
        )
    else:
        monkeypatch.setattr(
            google_calendar_credential_claim,
            "_revalidate_google_calendar_refresh_credential_claim",
            fail_boundary,
        )
        operation = lambda: revalidate_google_calendar_refresh_credential_claim(
            lambda: None,
            claim=claim,
        )
        expected_public_frame = (
            "revalidate_google_calendar_refresh_credential_claim"
        )

    with pytest.raises(expected_type) as raised:
        operation()

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    if expected_type is SystemExit:
        assert raised.value.code == expected_exit_code
    names, frame_values = _module_traceback(raised.value)
    assert names == [expected_public_frame, expected_helper]
    for frame_locals in _module_frame_locals(raised.value):
        assert all(
            not isinstance(value, GoogleCalendarRefreshCredentialClaim)
            for value in frame_locals.values()
        )
        assert all(
            not isinstance(value, CalendarCredentialCipher)
            for value in frame_locals.values()
        )
    output = " ".join(
        (
            repr(raised.value),
            _formatted_exception(raised.value),
            frame_values,
            caplog.text,
        )
    )
    for private in (
        "boundary-control-private",
        "boundary-refresh-token-private",
        "inst_refresh_claim_private",
    ):
        assert private not in output


def test_invalid_inputs_and_sessions_fail_before_claiming(monkeypatch):
    claim_calls = []
    monkeypatch.setattr(
        google_calendar_credential_claim,
        "_claim_locked_refresh_credential",
        lambda *_args, **_kwargs: claim_calls.append(True),
    )
    factory_calls = []

    def forbidden_factory():
        factory_calls.append(True)
        raise AssertionError("session creation must not occur")

    subclass_cipher = CipherSubclass(
        active_key_id=KEY_ID,
        keys={KEY_ID: KEY},
    )
    for overrides in (
        {"instructor_id": None},
        {"instructor_id": ""},
        {"instructor_id": " inst_private"},
        {"instructor_id": "inst\nprivate"},
        {"instructor_id": "café"},
        {"instructor_id": TextSubclass("inst_private")},
        {"credential_cipher": object()},
        {"credential_cipher": subclass_cipher},
    ):
        with pytest.raises(GoogleCalendarRefreshCredentialClaimError):
            _claim(forbidden_factory, **overrides)
    with pytest.raises(GoogleCalendarRefreshCredentialClaimError):
        _claim(None)
    assert factory_calls == []

    events = []
    active_session = TrackingSession(events)
    active_transaction = active_session.begin()
    with pytest.raises(GoogleCalendarRefreshCredentialClaimError):
        _claim(lambda: active_session)
    assert active_session.was_closed is True
    active_transaction.close()

    primary = create_engine("sqlite+pysqlite:///:memory:")
    split = create_engine("sqlite+pysqlite:///:memory:")
    split_session = Session(
        bind=primary,
        binds={ProviderCalendarConnectionDB: split},
    )
    try:
        with pytest.raises(GoogleCalendarRefreshCredentialClaimError):
            _claim(lambda: split_session)
    finally:
        split_session.close()
        primary.dispose()
        split.dispose()

    engine = create_engine("sqlite+pysqlite:///:memory:")
    connection = engine.connect()
    outer_transaction = connection.begin()
    external_session = Session(bind=connection)
    try:
        with pytest.raises(GoogleCalendarRefreshCredentialClaimError):
            _claim(lambda: external_session)
        assert outer_transaction.is_active is True
    finally:
        outer_transaction.rollback()
        connection.close()
        engine.dispose()

    autocommit_engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with pytest.raises(GoogleCalendarRefreshCredentialClaimError):
            _claim(lambda: Session(bind=autocommit_engine))
    finally:
        autocommit_engine.dispose()

    with pytest.raises(GoogleCalendarRefreshCredentialClaimError):
        _claim(lambda: object())
    assert claim_calls == []


def test_commit_close_and_internal_failures_return_no_claim(monkeypatch, caplog):
    build_calls = []
    private_values = (
        "refresh-token-private",
        "acct_refresh_claim_private",
        "inst_refresh_claim_private",
        "calconn_" + "1" * 32,
        "credential-private-detail",
        KEY.decode("ascii"),
    )

    for failure in ("internal", "commit", "close"):
        events = []
        session_factory, sessions = _recording_factory(
            events,
            commit_error=(
                RuntimeError(" ".join(private_values))
                if failure == "commit"
                else None
            ),
            close_error=(
                RuntimeError(" ".join(private_values))
                if failure == "close"
                else None
            ),
        )

        def locked(*_args, **_kwargs):
            if failure == "internal":
                raise RuntimeError(" ".join(private_values))
            return (
                private_values[1],
                private_values[2],
                private_values[3],
                2,
                private_values[0],
            )

        monkeypatch.setattr(
            google_calendar_credential_claim,
            "_claim_locked_refresh_credential",
            locked,
        )
        monkeypatch.setattr(
            google_calendar_credential_claim,
            "_new_claim",
            lambda **_kwargs: build_calls.append(True),
        )

        with pytest.raises(GoogleCalendarRefreshCredentialClaimError) as raised:
            _claim(session_factory)

        assert sessions[0].was_closed is True
        assert str(raised.value) == (
            "Google Calendar refresh credential could not be claimed safely."
        )
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
        assert raised.value.__suppress_context__ is True
        names, frame_values = _module_traceback(raised.value)
        assert names == [
            "claim_google_calendar_refresh_credential",
            "_raise_claim_error",
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
        for private in private_values:
            assert private not in output

    assert build_calls == []


def test_revalidation_uses_a_fresh_owned_transaction(monkeypatch):
    claim_events = []
    claim_factory, claim_sessions = _recording_factory(claim_events)
    _install_success_snapshot(
        monkeypatch,
        claim_events,
        claim_sessions,
    )
    claim = _claim(claim_factory)

    events = []
    session_factory, sessions = _recording_factory(events)

    def revalidate(session, **values):
        events.append("revalidate")
        assert session.in_transaction()
        assert values == {
            "account_id": claim.account_id,
            "instructor_id": claim.instructor_id,
            "connection_id": claim.connection_id,
            "expected_revision": 7,
        }

    monkeypatch.setattr(
        google_calendar_credential_claim,
        "_revalidate_locked_refresh_credential_claim",
        revalidate,
    )

    assert (
        revalidate_google_calendar_refresh_credential_claim(
            session_factory,
            claim=claim,
        )
        is None
    )
    assert sessions[0].was_closed is True
    assert events == ["factory", "revalidate", "commit", "close"]


def test_revalidation_rejects_malformed_claims_before_session_creation():
    factory_calls = []

    def forbidden_factory():
        factory_calls.append(True)
        raise AssertionError("session creation must not occur")

    malformed_claims = (
        None,
        object(),
        _forged_claim(account_id=""),
        _forged_claim(instructor_id=" inst_refresh_claim_private"),
        _forged_claim(connection_id="invalid-connection"),
        _forged_claim(revision=0),
        _forged_claim(refresh_token="swapped-refresh-token-private"),
        _forged_claim(_integrity_tag=b"x" * 32),
    )
    for malformed in malformed_claims:
        with pytest.raises(GoogleCalendarRefreshCredentialClaimError):
            revalidate_google_calendar_refresh_credential_claim(
                forbidden_factory,
                claim=malformed,
            )
    assert factory_calls == []


def test_revalidation_rejects_sessions_and_sanitizes_failures(
    monkeypatch,
    caplog,
):
    claim = _sealed_claim()
    revalidation_calls = []
    monkeypatch.setattr(
        google_calendar_credential_claim,
        "_revalidate_locked_refresh_credential_claim",
        lambda *_args, **_kwargs: revalidation_calls.append(True),
    )

    events = []
    active_session = TrackingSession(events)
    active_transaction = active_session.begin()
    with pytest.raises(GoogleCalendarRefreshCredentialClaimError):
        revalidate_google_calendar_refresh_credential_claim(
            lambda: active_session,
            claim=claim,
        )
    assert active_session.was_closed is True
    active_transaction.close()

    primary = create_engine("sqlite+pysqlite:///:memory:")
    split = create_engine("sqlite+pysqlite:///:memory:")
    split_session = Session(
        bind=primary,
        binds={ProviderCalendarConnectionDB: split},
    )
    try:
        with pytest.raises(GoogleCalendarRefreshCredentialClaimError):
            revalidate_google_calendar_refresh_credential_claim(
                lambda: split_session,
                claim=claim,
            )
    finally:
        split_session.close()
        primary.dispose()
        split.dispose()
    assert revalidation_calls == []

    private_values = (
        claim.account_id,
        claim.instructor_id,
        claim.connection_id,
        claim.refresh_token,
        "revalidation-private-detail",
    )
    for failure in ("internal", "commit", "close"):
        events = []
        session_factory, sessions = _recording_factory(
            events,
            commit_error=(
                RuntimeError(" ".join(private_values))
                if failure == "commit"
                else None
            ),
            close_error=(
                RuntimeError(" ".join(private_values))
                if failure == "close"
                else None
            ),
        )

        def revalidate(*_args, **_kwargs):
            if failure == "internal":
                raise RuntimeError(" ".join(private_values))

        monkeypatch.setattr(
            google_calendar_credential_claim,
            "_revalidate_locked_refresh_credential_claim",
            revalidate,
        )

        with pytest.raises(GoogleCalendarRefreshCredentialClaimError) as raised:
            revalidate_google_calendar_refresh_credential_claim(
                session_factory,
                claim=claim,
            )

        assert sessions[0].was_closed is True
        assert str(raised.value) == (
            "Google Calendar refresh credential could not be claimed safely."
        )
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
        assert raised.value.__suppress_context__ is True
        names, frame_values = _module_traceback(raised.value)
        assert names == [
            "revalidate_google_calendar_refresh_credential_claim",
            "_raise_claim_error",
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
        for private in private_values:
            assert private not in output


def test_connected_claims_are_tenant_scoped_and_read_only(migrated_postgres_db):
    first_account, first_instructor = _add_owner("tenant-first")
    second_account, second_instructor = _add_owner("tenant-second")
    _replace(first_account, token="first-refresh-private", now=T0)
    _replace(second_account, token="second-refresh-private", now=T0)
    before = {
        first_account: _stored_connection(first_account),
        second_account: _stored_connection(second_account),
    }

    first = claim_google_calendar_refresh_credential(
        db.SessionLocal,
        instructor_id=first_instructor,
        credential_cipher=CIPHER,
    )
    second = claim_google_calendar_refresh_credential(
        db.SessionLocal,
        instructor_id=second_instructor,
        credential_cipher=CIPHER,
    )

    assert type(first) is GoogleCalendarRefreshCredentialClaim
    assert type(second) is GoogleCalendarRefreshCredentialClaim
    assert first.account_id == first_account
    assert second.account_id == second_account
    assert first.instructor_id == first_instructor
    assert second.instructor_id == second_instructor
    assert first.connection_id != second.connection_id
    assert first.refresh_token == "first-refresh-private"
    assert second.refresh_token == "second-refresh-private"
    assert first.expected_calendar_connection_revision == 1
    assert second.expected_calendar_connection_revision == 1
    assert _stored_connection(first_account) == before[first_account]
    assert _stored_connection(second_account) == before[second_account]


@pytest.mark.parametrize("owner_state", ["missing", "disabled"])
def test_missing_and_disabled_owners_fail_without_decryption(
    migrated_postgres_db,
    monkeypatch,
    owner_state,
):
    if owner_state == "missing":
        instructor_id = "inst_refresh_claim_missing"
    else:
        account_id, instructor_id = _add_owner("disabled")
        _replace(account_id, token="disabled-refresh-private", now=T0)
        with db.SessionLocal.begin() as session:
            account = session.get(ProviderAccountDB, account_id)
            account.disabled_at = T0 + datetime.timedelta(minutes=1)
    decrypt_calls = []

    def forbidden_decrypt(*_args, **_kwargs):
        decrypt_calls.append(True)
        raise AssertionError("inactive owners must not decrypt")

    monkeypatch.setattr(
        CalendarCredentialCipher,
        "decrypt_refresh_token",
        forbidden_decrypt,
    )

    with pytest.raises(GoogleCalendarRefreshCredentialClaimError) as raised:
        claim_google_calendar_refresh_credential(
            db.SessionLocal,
            instructor_id=instructor_id,
            credential_cipher=CIPHER,
        )
    assert str(raised.value) == (
        "Google Calendar refresh credential could not be claimed safely."
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert decrypt_calls == []


def test_claim_and_revalidation_select_only_required_columns(
    migrated_postgres_db,
):
    account_id, instructor_id = _add_owner("projection")
    _replace(account_id, token="projection-refresh-private", now=T0)

    claim_statements = []

    def capture_claim(_connection, _cursor, statement, *_args):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select ") and (
            " from provider_accounts " in normalized
            or " from provider_calendar_connections " in normalized
        ):
            claim_statements.append(normalized)

    event.listen(db.engine, "before_cursor_execute", capture_claim)
    try:
        claim = claim_google_calendar_refresh_credential(
            db.SessionLocal,
            instructor_id=instructor_id,
            credential_cipher=CIPHER,
        )
    finally:
        event.remove(db.engine, "before_cursor_execute", capture_claim)

    assert type(claim) is GoogleCalendarRefreshCredentialClaim
    assert len(claim_statements) == 2
    account_projection = claim_statements[0].split(" from ", 1)[0]
    connection_projection = claim_statements[1].split(" from ", 1)[0]
    assert "provider_accounts.id" in account_projection
    assert "provider_accounts.instructor_id" in account_projection
    assert "provider_accounts.disabled_at" in account_projection
    assert "provider_accounts.calendar_connection_revision" in account_projection
    assert "password_hash" not in account_projection
    assert "email_normalized" not in account_projection
    assert "session_version" not in account_projection
    for required in (
        "provider_calendar_connections.id",
        "provider_calendar_connections.provider",
        "provider_calendar_connections.credential_version",
        "provider_calendar_connections.credential_key_id",
        "provider_calendar_connections.credential_nonce",
        "provider_calendar_connections.credential_ciphertext",
        "provider_calendar_connections.authorization_required_at",
    ):
        assert required in connection_projection
    assert "created_at" not in connection_projection
    assert "updated_at" not in connection_projection

    revalidation_statements = []

    def capture_revalidation(_connection, _cursor, statement, *_args):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select ") and (
            " from provider_accounts " in normalized
            or " from provider_calendar_connections " in normalized
        ):
            revalidation_statements.append(normalized)

    event.listen(db.engine, "before_cursor_execute", capture_revalidation)
    try:
        revalidate_google_calendar_refresh_credential_claim(
            db.SessionLocal,
            claim=claim,
        )
    finally:
        event.remove(
            db.engine,
            "before_cursor_execute",
            capture_revalidation,
        )

    assert len(revalidation_statements) == 2
    revalidation_account_projection = revalidation_statements[0].split(
        " from ",
        1,
    )[0]
    revalidation_connection_projection = revalidation_statements[1].split(
        " from ",
        1,
    )[0]
    assert "provider_accounts.id" in revalidation_account_projection
    assert "provider_accounts.instructor_id" in (
        revalidation_account_projection
    )
    assert "provider_accounts.disabled_at" in revalidation_account_projection
    assert "provider_accounts.calendar_connection_revision" in (
        revalidation_account_projection
    )
    assert "password_hash" not in revalidation_account_projection
    assert "email_normalized" not in revalidation_account_projection
    assert "session_version" not in revalidation_account_projection
    assert "provider_calendar_connections.id" in (
        revalidation_connection_projection
    )
    assert "provider_calendar_connections.provider" in (
        revalidation_connection_projection
    )
    assert "provider_calendar_connections.authorization_required_at" in (
        revalidation_connection_projection
    )
    assert "credential_" not in revalidation_connection_projection
    assert "created_at" not in revalidation_connection_projection
    assert "updated_at" not in revalidation_connection_projection
    assert all(" for update" in statement for statement in claim_statements)
    assert all(
        " for update" in statement for statement in revalidation_statements
    )


def test_disconnected_and_authorization_required_are_distinct_without_decrypt(
    migrated_postgres_db,
    monkeypatch,
):
    disconnected_account, disconnected_instructor = _add_owner("disconnected")
    recovery_account, recovery_instructor = _add_owner("recovery")
    _replace(recovery_account, token="recovery-private", now=T0)
    connection_id = _stored_connection(recovery_account)["id"]
    with db.SessionLocal.begin() as session:
        status = mark_google_calendar_authorization_required(
            session,
            account_id=recovery_account,
            expected_connection_id=connection_id,
            now=T0 + datetime.timedelta(minutes=1),
        )
    assert status.state == "authorization_required"

    decrypt_calls = []

    def forbidden_decrypt(*_args, **_kwargs):
        decrypt_calls.append(True)
        raise AssertionError("recovery states must not decrypt")

    monkeypatch.setattr(
        CalendarCredentialCipher,
        "decrypt_refresh_token",
        forbidden_decrypt,
    )

    assert claim_google_calendar_refresh_credential(
        db.SessionLocal,
        instructor_id=disconnected_instructor,
        credential_cipher=CIPHER,
    ) == "disconnected"
    assert claim_google_calendar_refresh_credential(
        db.SessionLocal,
        instructor_id=recovery_instructor,
        credential_cipher=CIPHER,
    ) == "authorization_required"
    assert decrypt_calls == []
    assert _stored_revision(disconnected_account) == 0
    assert _stored_revision(recovery_account) == 1


def test_retained_rotation_key_reads_without_rewrap(migrated_postgres_db):
    old_key = b"o" * 32
    new_key = b"n" * 32
    old_cipher = CalendarCredentialCipher(
        active_key_id="old-key",
        keys={"old-key": old_key},
    )
    rotated_cipher = CalendarCredentialCipher(
        active_key_id="new-key",
        keys={"new-key": new_key, "old-key": old_key},
    )
    missing_old_cipher = CalendarCredentialCipher(
        active_key_id="new-key",
        keys={"new-key": new_key},
    )
    account_id, instructor_id = _add_owner("rotation")
    _replace(
        account_id,
        token="rotated-refresh-private",
        now=T0,
        cipher=old_cipher,
    )
    before = _stored_connection(account_id)

    claim = claim_google_calendar_refresh_credential(
        db.SessionLocal,
        instructor_id=instructor_id,
        credential_cipher=rotated_cipher,
    )

    assert type(claim) is GoogleCalendarRefreshCredentialClaim
    assert claim.refresh_token == "rotated-refresh-private"
    assert _stored_connection(account_id) == before
    with pytest.raises(GoogleCalendarRefreshCredentialClaimError):
        claim_google_calendar_refresh_credential(
            db.SessionLocal,
            instructor_id=instructor_id,
            credential_cipher=missing_old_cipher,
        )
    assert _stored_connection(account_id) == before


@pytest.mark.parametrize("tamper", ["ciphertext", "key_id"])
def test_corrupt_envelopes_fail_without_mutation(migrated_postgres_db, tamper):
    account_id, instructor_id = _add_owner(f"tamper-{tamper}")
    _replace(account_id, token="tamper-refresh-private", now=T0)
    with db.SessionLocal.begin() as session:
        row = session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=account_id
        ).one()
        if tamper == "ciphertext":
            ciphertext = bytearray(row.credential_ciphertext)
            ciphertext[0] ^= 1
            row.credential_ciphertext = bytes(ciphertext)
        else:
            row.credential_key_id = "unknown-key"
        row.updated_at = T0 + datetime.timedelta(minutes=1)
    before = _stored_connection(account_id)

    with pytest.raises(GoogleCalendarRefreshCredentialClaimError) as raised:
        claim_google_calendar_refresh_credential(
            db.SessionLocal,
            instructor_id=instructor_id,
            credential_cipher=CIPHER,
        )

    assert str(raised.value) == (
        "Google Calendar refresh credential could not be claimed safely."
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert _stored_connection(account_id) == before


def test_cross_tenant_envelope_transplant_is_rejected(migrated_postgres_db):
    source_account, _ = _add_owner("transplant-source")
    target_account, target_instructor = _add_owner("transplant-target")
    _replace(source_account, token="source-refresh-private", now=T0)
    _replace(target_account, token="target-refresh-private", now=T0)
    source = _stored_connection(source_account)
    with db.SessionLocal.begin() as session:
        disconnect_google_calendar(session, account_id=source_account)
    with db.SessionLocal.begin() as session:
        target = session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=target_account
        ).one()
        target.credential_version = source["version"]
        target.credential_key_id = source["key_id"]
        target.credential_nonce = source["nonce"]
        target.credential_ciphertext = source["ciphertext"]
        target.updated_at = T0 + datetime.timedelta(minutes=1)
    transplanted = _stored_connection(target_account)

    with pytest.raises(GoogleCalendarRefreshCredentialClaimError):
        claim_google_calendar_refresh_credential(
            db.SessionLocal,
            instructor_id=target_instructor,
            credential_cipher=CIPHER,
        )
    assert _stored_connection(target_account) == transplanted


@pytest.mark.parametrize(
    "transition",
    ["replace", "disconnect", "authorization_required", "disable"],
)
def test_post_network_revalidation_rejects_lifecycle_changes(
    migrated_postgres_db,
    transition,
):
    account_id, instructor_id = _add_owner(f"revalidate-{transition}")
    _replace(account_id, token="revalidate-original-private", now=T0)
    claim = claim_google_calendar_refresh_credential(
        db.SessionLocal,
        instructor_id=instructor_id,
        credential_cipher=CIPHER,
    )
    assert type(claim) is GoogleCalendarRefreshCredentialClaim
    before = _stored_connection(account_id)
    assert revalidate_google_calendar_refresh_credential_claim(
        db.SessionLocal,
        claim=claim,
    ) is None
    assert _stored_connection(account_id) == before

    if transition == "replace":
        _replace(
            account_id,
            token="revalidate-replacement-private",
            now=T0 + datetime.timedelta(minutes=1),
        )
    elif transition == "disconnect":
        with db.SessionLocal.begin() as session:
            disconnect_google_calendar(session, account_id=account_id)
    elif transition == "authorization_required":
        with db.SessionLocal.begin() as session:
            mark_google_calendar_authorization_required(
                session,
                account_id=account_id,
                expected_connection_id=claim.connection_id,
                now=T0 + datetime.timedelta(minutes=1),
            )
    else:
        with db.SessionLocal.begin() as session:
            account = session.get(ProviderAccountDB, account_id)
            account.disabled_at = T0 + datetime.timedelta(minutes=1)

    with pytest.raises(GoogleCalendarRefreshCredentialClaimError):
        revalidate_google_calendar_refresh_credential_claim(
            db.SessionLocal,
            claim=claim,
        )


def test_claim_serializes_with_replacement_and_old_claim_revalidates_stale(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, instructor_id = _add_owner("replacement-race")
    _replace(account_id, token="race-original-private", now=T0)
    claim_ready = threading.Event()
    allow_claim_commit = threading.Event()
    replacement_backend_ready = threading.Event()
    claim_backend_pid = []
    replacement_backend_pid = []
    held_once = []
    real_lock_owner = (
        google_calendar_credential_claim._lock_refresh_credential_owner
    )

    def held_owner_lock(session, **kwargs):
        result = real_lock_owner(session, **kwargs)
        if not held_once:
            held_once.append(True)
            claim_backend_pid.append(
                session.execute(text("SELECT pg_backend_pid()")).scalar_one()
            )
            claim_ready.set()
            assert allow_claim_commit.wait(timeout=5)
        return result

    monkeypatch.setattr(
        google_calendar_credential_claim,
        "_lock_refresh_credential_owner",
        held_owner_lock,
    )

    def run_claim():
        return claim_google_calendar_refresh_credential(
            db.SessionLocal,
            instructor_id=instructor_id,
            credential_cipher=CIPHER,
        )

    def run_replacement():
        with db.SessionLocal.begin() as session:
            replacement_backend_pid.append(
                session.execute(text("SELECT pg_backend_pid()")).scalar_one()
            )
            replacement_backend_ready.set()
            return replace_google_calendar_connection(
                session,
                account_id=account_id,
                refresh_token="race-replacement-private",
                cipher=CIPHER,
                now=T0 + datetime.timedelta(minutes=1),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claim_future = executor.submit(run_claim)
        assert claim_ready.wait(timeout=5)
        replacement_future = executor.submit(run_replacement)
        assert replacement_backend_ready.wait(timeout=5)
        try:
            _wait_until_blocked_by(
                replacement_backend_pid[0],
                claim_backend_pid[0],
            )
        finally:
            allow_claim_commit.set()
        old_claim = claim_future.result(timeout=10)
        replacement = replacement_future.result(timeout=10)

    assert type(old_claim) is GoogleCalendarRefreshCredentialClaim
    assert old_claim.refresh_token == "race-original-private"
    assert old_claim.expected_calendar_connection_revision == 1
    assert replacement.state == "connected"
    assert _stored_revision(account_id) == 2
    with pytest.raises(GoogleCalendarRefreshCredentialClaimError):
        revalidate_google_calendar_refresh_credential_claim(
            db.SessionLocal,
            claim=old_claim,
        )
    current = run_claim()
    assert type(current) is GoogleCalendarRefreshCredentialClaim
    assert current.refresh_token == "race-replacement-private"
    assert current.expected_calendar_connection_revision == 2


def test_claim_fenced_recovery_owns_a_fresh_transaction(monkeypatch):
    claim = _sealed_claim()
    outcome = _recovery_outcome(claim)
    events = []
    session_factory, sessions = _recording_factory(events)

    def mark(session, **values):
        events.append("mark")
        assert session.in_transaction()
        assert values == {
            "account_id": claim.account_id,
            "instructor_id": claim.instructor_id,
            "connection_id": claim.connection_id,
            "expected_revision": 7,
        }

    monkeypatch.setattr(
        google_calendar_refresh_recovery,
        "_mark_locked_refresh_credential_authorization_required",
        mark,
    )

    assert (
        mark_google_calendar_refresh_credential_authorization_required(
            session_factory,
            claim=claim,
            outcome=outcome,
        )
        is None
    )
    assert sessions[0].was_closed is True
    assert events == ["factory", "mark", "commit", "close"]


def test_claim_fenced_recovery_uses_copied_claim_fence(monkeypatch):
    claim = _sealed_claim()
    outcome = _recovery_outcome(claim)
    events = []
    session_factory, _sessions = _recording_factory(events)

    def mutating_factory():
        object.__setattr__(outcome, "account_id", "acct_mutated_private")
        return session_factory()

    def mark(_session, **values):
        assert values["account_id"] == claim.account_id
        assert values["instructor_id"] == claim.instructor_id
        assert values["connection_id"] == claim.connection_id
        assert values["expected_revision"] == 7

    monkeypatch.setattr(
        google_calendar_refresh_recovery,
        "_mark_locked_refresh_credential_authorization_required",
        mark,
    )

    assert (
        mark_google_calendar_refresh_credential_authorization_required(
            mutating_factory,
            claim=claim,
            outcome=outcome,
        )
        is None
    )


def test_claim_fenced_recovery_rejects_untrusted_inputs_before_session():
    claim = _sealed_claim()
    factory_calls = []

    def forbidden_factory():
        factory_calls.append(True)
        raise AssertionError("session creation must not occur")

    cases = (
        (None, _recovery_outcome(claim)),
        (_forged_claim(refresh_token="swapped-refresh-private"), _recovery_outcome(claim)),
        (claim, None),
        (claim, object()),
        (claim, object.__new__(google_calendar_token_exchange.GoogleCalendarRefreshAuthorizationRequired)),
        (claim, _recovery_outcome(claim, account_id="acct_cross_tenant_private")),
        (claim, _recovery_outcome(claim, instructor_id="inst_cross_tenant_private")),
        (claim, _recovery_outcome(claim, connection_id="calconn_" + "2" * 32)),
        (
            claim,
            _recovery_outcome(
                claim,
                expected_calendar_connection_revision=8,
            ),
        ),
    )
    for supplied_claim, supplied_outcome in cases:
        with pytest.raises(GoogleCalendarRefreshRecoveryError):
            mark_google_calendar_refresh_credential_authorization_required(
                forbidden_factory,
                claim=supplied_claim,
                outcome=supplied_outcome,
            )
    assert factory_calls == []


def test_claim_fenced_recovery_rejects_unsafe_sessions(monkeypatch):
    claim = _sealed_claim()
    outcome = _recovery_outcome(claim)
    mark_calls = []
    monkeypatch.setattr(
        google_calendar_refresh_recovery,
        "_mark_locked_refresh_credential_authorization_required",
        lambda *_args, **_kwargs: mark_calls.append(True),
    )

    events = []
    active_session = TrackingSession(events)
    active_transaction = active_session.begin()
    with pytest.raises(GoogleCalendarRefreshRecoveryError):
        mark_google_calendar_refresh_credential_authorization_required(
            lambda: active_session,
            claim=claim,
            outcome=outcome,
        )
    assert active_session.was_closed is True
    active_transaction.close()

    primary = create_engine("sqlite+pysqlite:///:memory:")
    split = create_engine("sqlite+pysqlite:///:memory:")
    split_session = Session(
        bind=primary,
        binds={ProviderCalendarConnectionDB: split},
    )
    try:
        with pytest.raises(GoogleCalendarRefreshRecoveryError):
            mark_google_calendar_refresh_credential_authorization_required(
                lambda: split_session,
                claim=claim,
                outcome=outcome,
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
        with pytest.raises(GoogleCalendarRefreshRecoveryError):
            mark_google_calendar_refresh_credential_authorization_required(
                lambda: Session(bind=autocommit_engine),
                claim=claim,
                outcome=outcome,
            )
    finally:
        autocommit_engine.dispose()
    assert mark_calls == []


@pytest.mark.parametrize("failure_point", ["operation", "commit", "close"])
def test_claim_fenced_recovery_sanitizes_failures(
    monkeypatch,
    caplog,
    failure_point,
):
    claim = _sealed_claim()
    outcome = _recovery_outcome(claim)
    private_values = (
        claim.account_id,
        claim.instructor_id,
        claim.connection_id,
        claim.refresh_token,
        "claim-fenced-recovery-private-detail",
    )
    private_message = " ".join(private_values)
    events = []
    session_factory, sessions = _recording_factory(
        events,
        commit_error=(
            RuntimeError(private_message)
            if failure_point == "commit"
            else None
        ),
        close_error=(
            RuntimeError(private_message)
            if failure_point == "close"
            else None
        ),
    )

    def mark(*_args, **_kwargs):
        if failure_point == "operation":
            raise RuntimeError(private_message)

    monkeypatch.setattr(
        google_calendar_refresh_recovery,
        "_mark_locked_refresh_credential_authorization_required",
        mark,
    )

    with pytest.raises(GoogleCalendarRefreshRecoveryError) as raised:
        mark_google_calendar_refresh_credential_authorization_required(
            session_factory,
            claim=claim,
            outcome=outcome,
        )

    assert sessions[0].was_closed is True
    assert str(raised.value) == (
        "Google Calendar refresh recovery could not be persisted safely."
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, frame_values = _module_traceback(raised.value)
    assert names == [
        "mark_google_calendar_refresh_credential_authorization_required",
        "_raise_recovery_error",
    ]
    for frame_locals in _module_frame_locals(raised.value):
        assert all(
            type(value)
            not in (
                GoogleCalendarRefreshCredentialClaim,
                GoogleCalendarRefreshAuthorizationRequired,
            )
            for value in frame_locals.values()
        )
    output = " ".join(
        (
            str(raised.value),
            repr(raised.value),
            _formatted_exception(raised.value),
            frame_values,
            caplog.text,
        )
    )
    for private in private_values:
        assert private not in output


@pytest.mark.parametrize(
    ("failure", "expected_type", "expected_code"),
    [
        (asyncio.CancelledError("private-cancel"), asyncio.CancelledError, None),
        (KeyboardInterrupt("private-keyboard"), KeyboardInterrupt, None),
        (SystemExit(19), SystemExit, 19),
        (GeneratorExit("private-generator"), GeneratorExit, None),
    ],
)
def test_claim_fenced_recovery_preserves_sanitized_controls(
    monkeypatch,
    failure,
    expected_type,
    expected_code,
):
    claim = _sealed_claim()
    outcome = _recovery_outcome(claim)
    session_factory, sessions = _recording_factory([])

    def mark(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(
        google_calendar_refresh_recovery,
        "_mark_locked_refresh_credential_authorization_required",
        mark,
    )

    with pytest.raises(expected_type) as raised:
        mark_google_calendar_refresh_credential_authorization_required(
            session_factory,
            claim=claim,
            outcome=outcome,
        )

    assert sessions[0].was_closed is True
    assert raised.value is not failure
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    if expected_type is SystemExit:
        assert raised.value.code == expected_code


def test_claim_fenced_recovery_entry_interruption_is_sanitized():
    guarded = mark_google_calendar_refresh_credential_authorization_required
    source_lines, first_line = inspect.getsourcelines(guarded)
    target_line = first_line + next(
        offset
        for offset, line in enumerate(source_lines)
        if line.lstrip().startswith("try: return")
    )
    claim = _sealed_claim()
    outcome = _recovery_outcome(claim)
    interrupted = False

    def interrupt_entry(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is guarded.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt("recovery-entry-control-private")
        return interrupt_entry

    try:
        sys.settrace(interrupt_entry)
        with pytest.raises(KeyboardInterrupt) as raised:
            guarded(
                lambda: None,
                claim=claim,
                outcome=outcome,
            )
    finally:
        sys.settrace(None)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, frame_values = _module_traceback(raised.value)
    assert names == [
        "mark_google_calendar_refresh_credential_authorization_required",
        "_raise_control_exception",
    ]
    for frame_locals in _module_frame_locals(raised.value):
        assert all(
            type(value)
            not in (
                GoogleCalendarRefreshCredentialClaim,
                GoogleCalendarRefreshAuthorizationRequired,
            )
            for value in frame_locals.values()
        )
    output = " ".join((_formatted_exception(raised.value), frame_values))
    for private in (
        "recovery-entry-control-private",
        claim.account_id,
        claim.instructor_id,
        claim.connection_id,
        claim.refresh_token,
    ):
        assert private not in output


def test_claim_fenced_recovery_cleanup_interruption_is_sanitized():
    guarded = mark_google_calendar_refresh_credential_authorization_required
    source_lines, first_line = inspect.getsourcelines(guarded)
    target_line = first_line + next(
        offset
        for offset, line in enumerate(source_lines)
        if line.lstrip().startswith("try:") and " = None" in line
    )
    cleanup_instructions = [
        instruction
        for instruction in dis.get_instructions(guarded)
        if instruction.positions.lineno == target_line
    ]
    assert "DELETE_FAST" not in {
        instruction.opname for instruction in cleanup_instructions
    }
    assert sum(
        instruction.opname == "STORE_FAST"
        for instruction in cleanup_instructions
    ) >= 3
    claim = _sealed_claim()
    outcome = _recovery_outcome(claim)
    interrupted = False

    def interrupt_cleanup(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is guarded.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt("recovery-cleanup-control-private")
        return interrupt_cleanup

    try:
        sys.settrace(interrupt_cleanup)
        with pytest.raises(KeyboardInterrupt) as raised:
            guarded(
                None,
                claim=claim,
                outcome=outcome,
            )
    finally:
        sys.settrace(None)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, frame_values = _module_traceback(raised.value)
    assert names == [
        "mark_google_calendar_refresh_credential_authorization_required",
        "_raise_control_exception",
    ]
    for frame_locals in _module_frame_locals(raised.value):
        assert all(
            type(value)
            not in (
                GoogleCalendarRefreshCredentialClaim,
                GoogleCalendarRefreshAuthorizationRequired,
            )
            for value in frame_locals.values()
        )
    output = " ".join((_formatted_exception(raised.value), frame_values))
    for private in (
        "recovery-cleanup-control-private",
        claim.account_id,
        claim.instructor_id,
        claim.connection_id,
        claim.refresh_token,
    ):
        assert private not in output


@pytest.mark.parametrize(
    ("operation_failure", "close_failure", "expected_type"),
    [
        (
            KeyboardInterrupt("operation-control-private"),
            RuntimeError("close-private"),
            KeyboardInterrupt,
        ),
        (
            asyncio.CancelledError("operation-cancel-private"),
            KeyboardInterrupt("close-control-private"),
            KeyboardInterrupt,
        ),
        (
            RuntimeError("operation-private"),
            asyncio.CancelledError("close-cancel-private"),
            asyncio.CancelledError,
        ),
    ],
)
def test_claim_fenced_recovery_preserves_control_across_cleanup(
    monkeypatch,
    operation_failure,
    close_failure,
    expected_type,
):
    claim = _sealed_claim()
    outcome = _recovery_outcome(claim)
    session_factory, sessions = _recording_factory(
        [],
        close_error=close_failure,
    )

    def mark(*_args, **_kwargs):
        raise operation_failure

    monkeypatch.setattr(
        google_calendar_refresh_recovery,
        "_mark_locked_refresh_credential_authorization_required",
        mark,
    )

    with pytest.raises(expected_type) as raised:
        mark_google_calendar_refresh_credential_authorization_required(
            session_factory,
            claim=claim,
            outcome=outcome,
        )
    assert sessions[0].was_closed is True
    assert raised.value is not operation_failure
    assert raised.value is not close_failure
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    ("stage", "primary_failure", "rollback_failure", "expected_type"),
    [
        (
            "operation",
            KeyboardInterrupt("operation-control-private"),
            RuntimeError("rollback-private"),
            KeyboardInterrupt,
        ),
        (
            "operation",
            RuntimeError("operation-private"),
            asyncio.CancelledError("rollback-cancel-private"),
            asyncio.CancelledError,
        ),
        (
            "operation",
            asyncio.CancelledError("operation-cancel-private"),
            KeyboardInterrupt("rollback-control-private"),
            KeyboardInterrupt,
        ),
        (
            "commit",
            RuntimeError("commit-private"),
            GeneratorExit("rollback-control-private"),
            GeneratorExit,
        ),
    ],
)
def test_claim_fenced_recovery_preserves_control_across_rollback(
    monkeypatch,
    stage,
    primary_failure,
    rollback_failure,
    expected_type,
):
    claim = _sealed_claim()
    outcome = _recovery_outcome(claim)
    events = []
    session = RollbackFailureSession(
        events,
        rollback_error=rollback_failure,
    )
    if stage == "commit":
        def fail_commit(_session):
            raise primary_failure

        event.listen(
            session,
            "before_commit",
            fail_commit,
        )

    def mark(*_args, **_kwargs):
        if stage == "operation":
            raise primary_failure

    monkeypatch.setattr(
        google_calendar_refresh_recovery,
        "_mark_locked_refresh_credential_authorization_required",
        mark,
    )

    with pytest.raises(expected_type) as raised:
        mark_google_calendar_refresh_credential_authorization_required(
            lambda: session,
            claim=claim,
            outcome=outcome,
        )
    assert session.was_closed is True
    assert raised.value is not primary_failure
    assert raised.value is not rollback_failure
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_claim_fenced_recovery_prioritizes_nested_non_cancel_control(
    monkeypatch,
):
    claim = _sealed_claim()
    outcome = _recovery_outcome(claim)
    session_factory, _sessions = _recording_factory([])
    failure = BaseExceptionGroup(
        "private-group",
        [
            asyncio.CancelledError("private-cancel"),
            BaseExceptionGroup(
                "private-nested",
                [KeyboardInterrupt("private-keyboard")],
            ),
        ],
    )

    def mark(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(
        google_calendar_refresh_recovery,
        "_mark_locked_refresh_credential_authorization_required",
        mark,
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        mark_google_calendar_refresh_credential_authorization_required(
            session_factory,
            claim=claim,
            outcome=outcome,
        )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    ("graph_kind", "expected_type"),
    [
        ("exact-bound", KeyboardInterrupt),
        ("overflow", asyncio.CancelledError),
        ("shared-dag", KeyboardInterrupt),
    ],
)
def test_claim_fenced_recovery_bounds_exception_graph(
    monkeypatch,
    graph_kind,
    expected_type,
):
    claim = _sealed_claim()
    outcome = _recovery_outcome(claim)
    session_factory, _sessions = _recording_factory([])
    if graph_kind == "exact-bound":
        children = [RuntimeError() for _index in range(4_094)]
        children.append(KeyboardInterrupt())
        failure = BaseExceptionGroup("exact-bound", children)
    elif graph_kind == "overflow":
        failure = BaseExceptionGroup(
            "overflow",
            [RuntimeError() for _index in range(4_096)],
        )
    else:
        shared = ExceptionGroup("shared", [RuntimeError()])
        failure = BaseExceptionGroup(
            "shared-dag",
            [*([shared] * 4_096), KeyboardInterrupt()],
        )

    def mark(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(
        google_calendar_refresh_recovery,
        "_mark_locked_refresh_credential_authorization_required",
        mark,
    )

    with pytest.raises(expected_type) as raised:
        mark_google_calendar_refresh_credential_authorization_required(
            session_factory,
            claim=claim,
            outcome=outcome,
        )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize("persistent", [False, True])
def test_claim_fenced_recovery_survives_classifier_failure(
    monkeypatch,
    persistent,
):
    claim = _sealed_claim()
    outcome = _recovery_outcome(claim)
    session_factory, _sessions = _recording_factory([])
    real_classifier = (
        google_calendar_refresh_recovery
        ._classified_recovery_control_exception
    )
    classifier_calls = []

    def failing_classifier(raised):
        classifier_calls.append(True)
        if persistent or len(classifier_calls) == 1:
            raise RuntimeError("classifier-failure-private")
        return real_classifier(raised)

    def mark(*_args, **_kwargs):
        raise KeyboardInterrupt("operation-control-private")

    monkeypatch.setattr(
        google_calendar_refresh_recovery,
        "_classified_recovery_control_exception",
        failing_classifier,
    )
    monkeypatch.setattr(
        google_calendar_refresh_recovery,
        "_mark_locked_refresh_credential_authorization_required",
        mark,
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        mark_google_calendar_refresh_credential_authorization_required(
            session_factory,
            claim=claim,
            outcome=outcome,
        )
    assert len(classifier_calls) >= 1
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    names, frame_values = _module_traceback(raised.value)
    assert names == [
        "mark_google_calendar_refresh_credential_authorization_required",
        "_raise_control_exception",
    ]
    for private in (
        claim.account_id,
        claim.instructor_id,
        claim.connection_id,
        claim.refresh_token,
        "classifier-failure-private",
        "operation-control-private",
    ):
        assert private not in frame_values


@pytest.mark.parametrize(
    ("operation_failure", "graph_failure", "expected_type"),
    [
        (
            KeyboardInterrupt("operation-control-private"),
            RuntimeError("graph-private"),
            KeyboardInterrupt,
        ),
        (
            RuntimeError("operation-private"),
            GeneratorExit("graph-control-private"),
            GeneratorExit,
        ),
        (
            RuntimeError("operation-private"),
            RuntimeError("graph-private"),
            asyncio.CancelledError,
        ),
    ],
)
def test_claim_fenced_recovery_survives_graph_classifier_failure(
    monkeypatch,
    operation_failure,
    graph_failure,
    expected_type,
):
    claim = _sealed_claim()
    outcome = _recovery_outcome(claim)
    session_factory, _sessions = _recording_factory([])

    def fail_graph(_raised):
        raise graph_failure

    def mark(*_args, **_kwargs):
        raise operation_failure

    monkeypatch.setattr(
        google_calendar_refresh_recovery,
        "_classify_recovery_control_exception_graph",
        fail_graph,
    )
    monkeypatch.setattr(
        google_calendar_refresh_recovery,
        "_mark_locked_refresh_credential_authorization_required",
        mark,
    )

    with pytest.raises(expected_type) as raised:
        mark_google_calendar_refresh_credential_authorization_required(
            session_factory,
            claim=claim,
            outcome=outcome,
        )
    assert raised.value is not operation_failure
    assert raised.value is not graph_failure
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_claim_fenced_recovery_rejects_timezone_incomplete_timestamp():
    incomplete = datetime.datetime(
        2026,
        11,
        19,
        14,
        0,
        tzinfo=IncompleteTimezone(),
    )
    with pytest.raises(ValueError, match="invalid credential timestamp"):
        google_calendar_refresh_recovery._validated_persisted_timestamp(
            incomplete
        )


def test_claim_fenced_recovery_marks_exact_generation_idempotently(
    migrated_postgres_db,
):
    account_id, instructor_id = _add_owner("fenced-recovery")
    _replace(account_id, token="fenced-recovery-private", now=T0)
    claim = claim_google_calendar_refresh_credential(
        db.SessionLocal,
        instructor_id=instructor_id,
        credential_cipher=CIPHER,
    )
    assert type(claim) is GoogleCalendarRefreshCredentialClaim
    outcome = _recovery_outcome(claim)
    before = _stored_connection(account_id)

    assert (
        mark_google_calendar_refresh_credential_authorization_required(
            db.SessionLocal,
            claim=claim,
            outcome=outcome,
        )
        is None
    )
    first = _stored_connection(account_id)
    assert first["authorization_required_at"] is not None
    assert first["updated_at"] == first["authorization_required_at"]
    assert first["revision"] == before["revision"] == 1
    for unchanged in (
        "id",
        "provider",
        "version",
        "key_id",
        "nonce",
        "ciphertext",
        "created_at",
    ):
        assert first[unchanged] == before[unchanged]

    assert (
        mark_google_calendar_refresh_credential_authorization_required(
            db.SessionLocal,
            claim=claim,
            outcome=outcome,
        )
        is None
    )
    assert _stored_connection(account_id) == first
    assert claim_google_calendar_refresh_credential(
        db.SessionLocal,
        instructor_id=instructor_id,
        credential_cipher=CIPHER,
    ) == "authorization_required"


@pytest.mark.parametrize(
    "transition",
    ["replace", "disconnect", "revision", "reassign", "disable"],
)
def test_claim_fenced_recovery_rejects_stale_generation(
    migrated_postgres_db,
    transition,
):
    account_id, instructor_id = _add_owner(f"recovery-stale-{transition}")
    _replace(account_id, token="recovery-original-private", now=T0)
    claim = claim_google_calendar_refresh_credential(
        db.SessionLocal,
        instructor_id=instructor_id,
        credential_cipher=CIPHER,
    )
    assert type(claim) is GoogleCalendarRefreshCredentialClaim
    outcome = _recovery_outcome(claim)

    if transition == "replace":
        _replace(
            account_id,
            token="recovery-replacement-private",
            now=T0 + datetime.timedelta(minutes=1),
        )
    elif transition == "disconnect":
        with db.SessionLocal.begin() as session:
            disconnect_google_calendar(session, account_id=account_id)
    elif transition == "revision":
        with db.SessionLocal.begin() as session:
            account = session.get(ProviderAccountDB, account_id)
            account.calendar_connection_revision += 1
    elif transition == "reassign":
        reassigned_instructor_id = "inst_refresh_claim_reassigned_owner"
        with db.SessionLocal.begin() as session:
            session.add(
                InstructorDB(
                    id=reassigned_instructor_id,
                    name="Reassigned Recovery Owner",
                    main_address="2 Credential Way",
                    lat=30.3,
                    lng=-97.3,
                    work_start_time="08:00",
                    work_end_time="17:00",
                    timezone="America/Chicago",
                )
            )
            account = session.get(ProviderAccountDB, account_id)
            account.instructor_id = reassigned_instructor_id
    else:
        with db.SessionLocal.begin() as session:
            account = session.get(ProviderAccountDB, account_id)
            account.disabled_at = T0 + datetime.timedelta(minutes=1)
    before = _stored_connection(account_id)

    with pytest.raises(GoogleCalendarRefreshRecoveryError):
        mark_google_calendar_refresh_credential_authorization_required(
            db.SessionLocal,
            claim=claim,
            outcome=outcome,
        )
    assert _stored_connection(account_id) == before


def test_claim_fenced_recovery_outcome_cannot_select_another_tenant(
    migrated_postgres_db,
):
    first_account, first_instructor = _add_owner("recovery-tenant-first")
    second_account, second_instructor = _add_owner("recovery-tenant-second")
    _replace(first_account, token="recovery-first-private", now=T0)
    _replace(second_account, token="recovery-second-private", now=T0)
    first_claim = claim_google_calendar_refresh_credential(
        db.SessionLocal,
        instructor_id=first_instructor,
        credential_cipher=CIPHER,
    )
    second_claim = claim_google_calendar_refresh_credential(
        db.SessionLocal,
        instructor_id=second_instructor,
        credential_cipher=CIPHER,
    )
    assert type(first_claim) is GoogleCalendarRefreshCredentialClaim
    assert type(second_claim) is GoogleCalendarRefreshCredentialClaim
    before = {
        first_account: _stored_connection(first_account),
        second_account: _stored_connection(second_account),
    }

    with pytest.raises(GoogleCalendarRefreshRecoveryError):
        mark_google_calendar_refresh_credential_authorization_required(
            db.SessionLocal,
            claim=first_claim,
            outcome=_recovery_outcome(second_claim),
        )
    assert _stored_connection(first_account) == before[first_account]
    assert _stored_connection(second_account) == before[second_account]


def test_claim_fenced_recovery_projects_only_fence_and_status_columns(
    migrated_postgres_db,
):
    account_id, instructor_id = _add_owner("recovery-projection")
    _replace(account_id, token="recovery-projection-private", now=T0)
    claim = claim_google_calendar_refresh_credential(
        db.SessionLocal,
        instructor_id=instructor_id,
        credential_cipher=CIPHER,
    )
    assert type(claim) is GoogleCalendarRefreshCredentialClaim
    statements = []

    def capture(_connection, _cursor, statement, *_args):
        normalized = " ".join(statement.lower().split())
        if (
            normalized.startswith(("select ", "update "))
            and (
                "provider_accounts" in normalized
                or "provider_calendar_connections" in normalized
            )
        ):
            statements.append(normalized)

    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        mark_google_calendar_refresh_credential_authorization_required(
            db.SessionLocal,
            claim=claim,
            outcome=_recovery_outcome(claim),
        )
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)

    assert len(statements) == 3
    assert " from provider_accounts " in statements[0]
    assert " for update" in statements[0]
    assert " from provider_calendar_connections " in statements[1]
    assert " for update" in statements[1]
    assert statements[2].startswith("update provider_calendar_connections ")
    assert "credential_" not in statements[0].split(" from ", 1)[0]
    connection_projection = statements[1].split(" from ", 1)[0]
    assert "credential_version" not in connection_projection
    assert "credential_key_id" not in connection_projection
    assert "credential_nonce" not in connection_projection
    assert "credential_ciphertext" not in connection_projection


def test_claim_fenced_recovery_rolls_back_after_locked_mutation(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, instructor_id = _add_owner("recovery-rollback")
    _replace(account_id, token="recovery-rollback-private", now=T0)
    claim = claim_google_calendar_refresh_credential(
        db.SessionLocal,
        instructor_id=instructor_id,
        credential_cipher=CIPHER,
    )
    assert type(claim) is GoogleCalendarRefreshCredentialClaim
    before = _stored_connection(account_id)
    real_mark = (
        google_calendar_refresh_recovery
        ._mark_locked_refresh_credential_authorization_required
    )

    def fail_after_mark(session, **values):
        real_mark(session, **values)
        raise RuntimeError("rollback-after-recovery-private")

    monkeypatch.setattr(
        google_calendar_refresh_recovery,
        "_mark_locked_refresh_credential_authorization_required",
        fail_after_mark,
    )

    with pytest.raises(GoogleCalendarRefreshRecoveryError):
        mark_google_calendar_refresh_credential_authorization_required(
            db.SessionLocal,
            claim=claim,
            outcome=_recovery_outcome(claim),
        )
    assert _stored_connection(account_id) == before


def test_concurrent_claim_fenced_recovery_is_idempotent(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, instructor_id = _add_owner("recovery-concurrent")
    _replace(account_id, token="recovery-concurrent-private", now=T0)
    claim = claim_google_calendar_refresh_credential(
        db.SessionLocal,
        instructor_id=instructor_id,
        credential_cipher=CIPHER,
    )
    assert type(claim) is GoogleCalendarRefreshCredentialClaim
    outcome = _recovery_outcome(claim)
    first_marked = threading.Event()
    release_first = threading.Event()
    second_backend_ready = threading.Event()
    first_backend_pid = []
    second_backend_pid = []
    calls = []
    real_mark = (
        google_calendar_refresh_recovery
        ._mark_locked_refresh_credential_authorization_required
    )

    def held_mark(session, **values):
        backend_pid = session.execute(
            text("SELECT pg_backend_pid()")
        ).scalar_one()
        call_number = len(calls)
        calls.append(call_number)
        if call_number == 0:
            first_backend_pid.append(backend_pid)
            result = real_mark(session, **values)
            first_marked.set()
            assert release_first.wait(timeout=5)
            return result
        second_backend_pid.append(backend_pid)
        second_backend_ready.set()
        return real_mark(session, **values)

    monkeypatch.setattr(
        google_calendar_refresh_recovery,
        "_mark_locked_refresh_credential_authorization_required",
        held_mark,
    )

    def mark():
        return mark_google_calendar_refresh_credential_authorization_required(
            db.SessionLocal,
            claim=claim,
            outcome=outcome,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(mark)
        assert first_marked.wait(timeout=5)
        second_future = executor.submit(mark)
        assert second_backend_ready.wait(timeout=5)
        try:
            _wait_until_blocked_by(
                second_backend_pid[0],
                first_backend_pid[0],
            )
        finally:
            release_first.set()
        results = (
            first_future.result(timeout=10),
            second_future.result(timeout=10),
        )

    assert results == (None, None)
    stored = _stored_connection(account_id)
    assert stored["authorization_required_at"] is not None
    assert stored["updated_at"] == stored["authorization_required_at"]
    assert stored["revision"] == 1


@pytest.mark.parametrize("transition", ["replace", "disconnect"])
def test_claim_fenced_recovery_serializes_before_lifecycle_change(
    migrated_postgres_db,
    monkeypatch,
    transition,
):
    account_id, instructor_id = _add_owner(
        f"recovery-first-{transition}"
    )
    _replace(account_id, token="recovery-first-private", now=T0)
    claim = claim_google_calendar_refresh_credential(
        db.SessionLocal,
        instructor_id=instructor_id,
        credential_cipher=CIPHER,
    )
    assert type(claim) is GoogleCalendarRefreshCredentialClaim
    recovery_marked = threading.Event()
    release_recovery = threading.Event()
    lifecycle_backend_ready = threading.Event()
    recovery_backend_pid = []
    recovery_marked_at = []
    lifecycle_backend_pid = []
    lifecycle_replaced_at = []
    real_mark = (
        google_calendar_refresh_recovery
        ._mark_locked_refresh_credential_authorization_required
    )

    def held_mark(session, **values):
        result = real_mark(session, **values)
        marked_connection = session.get(
            ProviderCalendarConnectionDB,
            values["connection_id"],
        )
        assert marked_connection is not None
        recovery_marked_at.append(marked_connection.updated_at)
        recovery_backend_pid.append(
            session.execute(text("SELECT pg_backend_pid()")).scalar_one()
        )
        recovery_marked.set()
        assert release_recovery.wait(timeout=5)
        return result

    monkeypatch.setattr(
        google_calendar_refresh_recovery,
        "_mark_locked_refresh_credential_authorization_required",
        held_mark,
    )

    def recover():
        return mark_google_calendar_refresh_credential_authorization_required(
            db.SessionLocal,
            claim=claim,
            outcome=_recovery_outcome(claim),
        )

    def change_lifecycle():
        with db.SessionLocal.begin() as session:
            lifecycle_backend_pid.append(
                session.execute(text("SELECT pg_backend_pid()")).scalar_one()
            )
            lifecycle_backend_ready.set()
            if transition == "replace":
                replacement_at = recovery_marked_at[0] + datetime.timedelta(
                    microseconds=1
                )
                lifecycle_replaced_at.append(replacement_at)
                return replace_google_calendar_connection(
                    session,
                    account_id=account_id,
                    refresh_token="post-recovery-replacement-private",
                    cipher=CIPHER,
                    now=replacement_at,
                )
            return disconnect_google_calendar(
                session,
                account_id=account_id,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        recovery_future = executor.submit(recover)
        assert recovery_marked.wait(timeout=5)
        lifecycle_future = executor.submit(change_lifecycle)
        assert lifecycle_backend_ready.wait(timeout=5)
        try:
            _wait_until_blocked_by(
                lifecycle_backend_pid[0],
                recovery_backend_pid[0],
            )
        finally:
            release_recovery.set()
        assert recovery_future.result(timeout=10) is None
        lifecycle_status = lifecycle_future.result(timeout=10)

    stored = _stored_connection(account_id)
    assert _stored_revision(account_id) == 2
    if transition == "replace":
        assert lifecycle_status.state == "connected"
        assert stored is not None
        assert stored["id"] != claim.connection_id
        assert stored["authorization_required_at"] is None
        assert stored["updated_at"] == lifecycle_replaced_at[0]
    else:
        assert lifecycle_status.state == "disconnected"
        assert stored is None


@pytest.mark.parametrize("transition", ["replace", "disconnect"])
def test_lifecycle_change_serializes_before_claim_fenced_recovery(
    migrated_postgres_db,
    monkeypatch,
    transition,
):
    account_id, instructor_id = _add_owner(
        f"lifecycle-first-{transition}"
    )
    _replace(account_id, token="lifecycle-first-private", now=T0)
    claim = claim_google_calendar_refresh_credential(
        db.SessionLocal,
        instructor_id=instructor_id,
        credential_cipher=CIPHER,
    )
    assert type(claim) is GoogleCalendarRefreshCredentialClaim
    lifecycle_changed = threading.Event()
    release_lifecycle = threading.Event()
    recovery_backend_ready = threading.Event()
    lifecycle_backend_pid = []
    recovery_backend_pid = []
    real_mark = (
        google_calendar_refresh_recovery
        ._mark_locked_refresh_credential_authorization_required
    )

    def observed_mark(session, **values):
        recovery_backend_pid.append(
            session.execute(text("SELECT pg_backend_pid()")).scalar_one()
        )
        recovery_backend_ready.set()
        return real_mark(session, **values)

    monkeypatch.setattr(
        google_calendar_refresh_recovery,
        "_mark_locked_refresh_credential_authorization_required",
        observed_mark,
    )

    def change_lifecycle():
        with db.SessionLocal.begin() as session:
            lifecycle_backend_pid.append(
                session.execute(text("SELECT pg_backend_pid()")).scalar_one()
            )
            if transition == "replace":
                status = replace_google_calendar_connection(
                    session,
                    account_id=account_id,
                    refresh_token="lifecycle-first-replacement-private",
                    cipher=CIPHER,
                    now=T0 + datetime.timedelta(minutes=1),
                )
            else:
                status = disconnect_google_calendar(
                    session,
                    account_id=account_id,
                )
            lifecycle_changed.set()
            assert release_lifecycle.wait(timeout=5)
            return status

    def recover():
        return mark_google_calendar_refresh_credential_authorization_required(
            db.SessionLocal,
            claim=claim,
            outcome=_recovery_outcome(claim),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        lifecycle_future = executor.submit(change_lifecycle)
        assert lifecycle_changed.wait(timeout=5)
        recovery_future = executor.submit(recover)
        assert recovery_backend_ready.wait(timeout=5)
        try:
            _wait_until_blocked_by(
                recovery_backend_pid[0],
                lifecycle_backend_pid[0],
            )
        finally:
            release_lifecycle.set()
        lifecycle_status = lifecycle_future.result(timeout=10)
        with pytest.raises(GoogleCalendarRefreshRecoveryError):
            recovery_future.result(timeout=10)

    stored = _stored_connection(account_id)
    assert _stored_revision(account_id) == 2
    if transition == "replace":
        assert lifecycle_status.state == "connected"
        assert stored is not None
        assert stored["id"] != claim.connection_id
        assert stored["authorization_required_at"] is None
    else:
        assert lifecycle_status.state == "disconnected"
        assert stored is None


def test_claim_fenced_recovery_clamps_clock_regression(
    migrated_postgres_db,
):
    account_id, instructor_id = _add_owner("recovery-clock-regression")
    _replace(account_id, token="recovery-clock-private", now=T0)
    claim = claim_google_calendar_refresh_credential(
        db.SessionLocal,
        instructor_id=instructor_id,
        credential_cipher=CIPHER,
    )
    assert type(claim) is GoogleCalendarRefreshCredentialClaim
    future = datetime.datetime.now(UTC) + datetime.timedelta(days=1)
    with db.SessionLocal.begin() as session:
        row = session.get(ProviderCalendarConnectionDB, claim.connection_id)
        row.updated_at = future

    mark_google_calendar_refresh_credential_authorization_required(
        db.SessionLocal,
        claim=claim,
        outcome=_recovery_outcome(claim),
    )

    stored = _stored_connection(account_id)
    assert stored["authorization_required_at"] == future
    assert stored["updated_at"] == future
    assert stored["revision"] == 1


def _add_owner(suffix: str) -> tuple[str, str]:
    instructor_id = f"inst_refresh_claim_{suffix}"
    account_id = f"acct_refresh_claim_{suffix}"
    with db.SessionLocal.begin() as session:
        session.add(
            InstructorDB(
                id=instructor_id,
                name=f"Refresh Claim {suffix}",
                main_address="1 Credential Way",
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
                email_normalized=f"refresh-claim-{suffix}@example.com",
                password_hash="not-used-by-refresh-claim-test",
                is_demo=False,
                session_version=1,
            )
        )
    return account_id, instructor_id


def _replace(
    account_id: str,
    *,
    token: str,
    now: datetime.datetime,
    cipher: CalendarCredentialCipher = CIPHER,
):
    with db.SessionLocal.begin() as session:
        return replace_google_calendar_connection(
            session,
            account_id=account_id,
            refresh_token=token,
            cipher=cipher,
            now=now,
        )


def _stored_connection(account_id: str):
    with db.SessionLocal() as session:
        row = session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=account_id
        ).one_or_none()
        if row is None:
            return None
        return {
            "id": row.id,
            "provider": row.provider,
            "version": row.credential_version,
            "key_id": row.credential_key_id,
            "nonce": bytes(row.credential_nonce),
            "ciphertext": bytes(row.credential_ciphertext),
            "authorization_required_at": row.authorization_required_at,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "revision": _stored_revision(account_id),
        }


def _stored_revision(account_id: str) -> int:
    with db.SessionLocal() as session:
        account = session.get(ProviderAccountDB, account_id)
        assert account is not None
        return account.calendar_connection_revision


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
