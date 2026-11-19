from __future__ import annotations

import dataclasses
import datetime
import inspect
import json
import pickle
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from app import db, google_calendar_credential_claim
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
from app.google_calendar_oauth import GoogleCalendarOAuthError
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


def _module_traceback(error):
    names = []
    values = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith(
            "/app/google_calendar_credential_claim.py"
        ):
            names.append(current.tb_frame.f_code.co_name)
            values.extend(current.tb_frame.f_locals.values())
        current = current.tb_next
    return names, repr(values)


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
    assert issubclass(
        GoogleCalendarRefreshCredentialClaimError,
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
        _sealed_claim(account_id=""),
        _sealed_claim(instructor_id=" inst_refresh_claim_private"),
        _sealed_claim(connection_id="invalid-connection"),
        _sealed_claim(revision=0),
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
