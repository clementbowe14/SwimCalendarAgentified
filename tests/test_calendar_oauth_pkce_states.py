from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import inspect
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from app import calendar_oauth_pkce, calendar_oauth_states, db
from app.calendar_oauth_pkce import (
    CalendarOAuthPKCECipher,
    CalendarOAuthPKCEDecryptionError,
    CalendarOAuthPKCEScope,
    EncryptedCalendarOAuthPKCEVerifier,
    GeneratedGoogleCalendarPKCE,
    derive_s256_code_challenge,
)
from app.calendar_oauth_states import (
    CalendarOAuthStateInputError,
    CalendarOAuthStateInvalid,
    CalendarOAuthStatePersistenceError,
    CalendarOAuthStateSessionInvalid,
    CalendarOAuthStateTransactionRequired,
    IssuedGoogleCalendarOAuthBrowserBoundPKCEState,
    IssuedGoogleCalendarOAuthPKCEState,
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
TOKEN_C = "C" * 43
TOKEN_D = "D" * 43
TOKEN_E = "E" * 43
KEY_ID = "pkce-state-key-2026"
ROOT_KEY = b"p" * 32
BROWSER_BYTES_A = b"b" * 32
BROWSER_BYTES_B = b"c" * 32
BROWSER_BYTES_C = b"d" * 32
BROWSER_BYTES_D = b"e" * 32
BROWSER_A = base64.urlsafe_b64encode(BROWSER_BYTES_A).rstrip(b"=").decode()
BROWSER_B = base64.urlsafe_b64encode(BROWSER_BYTES_B).rstrip(b"=").decode()
BROWSER_C = base64.urlsafe_b64encode(BROWSER_BYTES_C).rstrip(b"=").decode()
BROWSER_DIGEST_DOMAIN = (
    b"dayweave.calendar-oauth-browser-binding.sha256.v1\x00"
)
BROWSER_A_DIGEST = bytes.fromhex(
    "32db74dc866a0163389b7744b091642ef76402ecafc7caca8ac21b0d3a03885d"
)
CIPHER = CalendarOAuthPKCECipher(
    active_key_id=KEY_ID,
    root_keys={KEY_ID: ROOT_KEY},
)


def test_command_requires_explicit_transaction_and_safe_inputs(
    migrated_postgres_db,
):
    account_id, session_id = _active_demo_owner("transactions")

    assert set(
        inspect.signature(
            calendar_oauth_states.issue_google_calendar_oauth_state_with_pkce
        ).parameters
    ) == {
        "session",
        "account_id",
        "session_id",
        "pkce_cipher",
        "now",
    }
    assert [
        field.name
        for field in dataclasses.fields(IssuedGoogleCalendarOAuthPKCEState)
    ] == ["raw_state", "expires_at", "code_challenge"]

    session = db.SessionLocal()
    try:
        with pytest.raises(CalendarOAuthStateTransactionRequired):
            calendar_oauth_states.issue_google_calendar_oauth_state_with_pkce(
                session,
                account_id=account_id,
                session_id=session_id,
                pkce_cipher=CIPHER,
                now=T0,
            )

        session.get(ProviderAccountDB, account_id)
        with pytest.raises(CalendarOAuthStateTransactionRequired):
            calendar_oauth_states.issue_google_calendar_oauth_state_with_pkce(
                session,
                account_id=account_id,
                session_id=session_id,
                pkce_cipher=CIPHER,
                now=T0,
            )
        session.rollback()

        with session.begin():
            with pytest.raises(CalendarOAuthStateInputError):
                calendar_oauth_states.issue_google_calendar_oauth_state_with_pkce(
                    session,
                    account_id=account_id,
                    session_id=session_id,
                    pkce_cipher=object(),
                    now=T0,
                )
            session.execute(text("SELECT 1"))
    finally:
        session.close()

    assert _snapshot(account_id, session_id) is None


def test_bound_command_contract_requires_explicit_transaction(
    migrated_postgres_db,
):
    account_id, session_id = _active_demo_owner("bound-contract")

    signature = inspect.signature(
        calendar_oauth_states
        .issue_google_calendar_oauth_state_with_pkce_and_browser_binding
    )
    assert set(signature.parameters) == {
        "session",
        "account_id",
        "session_id",
        "pkce_cipher",
        "now",
    }
    assert signature.parameters["session"].kind is (
        inspect.Parameter.POSITIONAL_OR_KEYWORD
    )
    assert signature.parameters["session"].default is inspect.Parameter.empty
    for name in ("account_id", "session_id", "pkce_cipher", "now"):
        assert signature.parameters[name].kind is (
            inspect.Parameter.KEYWORD_ONLY
        )
    for name in ("account_id", "session_id", "pkce_cipher"):
        assert signature.parameters[name].default is inspect.Parameter.empty
    assert signature.parameters["now"].default is None
    assert [
        field.name
        for field in dataclasses.fields(
            IssuedGoogleCalendarOAuthBrowserBoundPKCEState
        )
    ] == ["issued_state", "raw_browser_binding"]
    assert IssuedGoogleCalendarOAuthBrowserBoundPKCEState.__slots__ == (
        "issued_state",
        "raw_browser_binding",
    )

    session = db.SessionLocal()
    try:
        with pytest.raises(CalendarOAuthStateTransactionRequired):
            (
                calendar_oauth_states
                .issue_google_calendar_oauth_state_with_pkce_and_browser_binding
            )(
                session,
                account_id=account_id,
                session_id=session_id,
                pkce_cipher=CIPHER,
                now=T0,
            )

        session.get(ProviderAccountDB, account_id)
        with pytest.raises(CalendarOAuthStateTransactionRequired):
            (
                calendar_oauth_states
                .issue_google_calendar_oauth_state_with_pkce_and_browser_binding
            )(
                session,
                account_id=account_id,
                session_id=session_id,
                pkce_cipher=CIPHER,
                now=T0,
            )
        session.rollback()

        with session.begin():
            with pytest.raises(CalendarOAuthStateInputError):
                (
                    calendar_oauth_states
                    .issue_google_calendar_oauth_state_with_pkce_and_browser_binding
                )(
                    session,
                    account_id=account_id,
                    session_id=session_id,
                    pkce_cipher=object(),
                    now=T0,
                )
            with pytest.raises(CalendarOAuthStateInputError):
                (
                    calendar_oauth_states
                    .issue_google_calendar_oauth_state_with_pkce_and_browser_binding
                )(
                    session,
                    account_id=account_id,
                    session_id=session_id,
                    pkce_cipher=CIPHER,
                    now=T0.replace(tzinfo=None),
                )
            session.execute(text("SELECT 1"))
    finally:
        session.close()

    assert _snapshot(account_id, session_id) is None


def test_atomic_issue_persists_only_bound_encrypted_verifier(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, session_id = _active_demo_owner("atomic")
    verifier_entropy = b"v" * 32
    nonce = b"n" * 12
    requested_state_sizes = []
    requested_verifier_sizes = []

    def state_token(size: int) -> str:
        requested_state_sizes.append(size)
        return TOKEN_A

    def verifier_token(size: int) -> bytes:
        requested_verifier_sizes.append(size)
        return verifier_entropy

    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        state_token,
    )
    monkeypatch.setattr(
        calendar_oauth_pkce.secrets,
        "token_bytes",
        verifier_token,
    )
    monkeypatch.setattr(
        calendar_oauth_pkce.os,
        "urandom",
        lambda size: nonce,
    )

    issued = _issue_pkce(account_id, session_id, now=T0)
    stored = _snapshot(account_id, session_id)

    assert isinstance(issued, IssuedGoogleCalendarOAuthPKCEState)
    assert requested_state_sizes == [32]
    assert requested_verifier_sizes == [32]
    assert issued.raw_state == TOKEN_A
    assert issued.expires_at == T0 + datetime.timedelta(minutes=10)
    assert issued.code_challenge_method == "S256"
    assert repr(issued) == "IssuedGoogleCalendarOAuthPKCEState(<redacted>)"
    assert str(issued) == repr(issued)
    assert TOKEN_A not in repr(issued)
    assert issued.code_challenge not in repr(issued)

    assert stored is not None
    assert stored["state_digest"] == _digest(TOKEN_A)
    assert stored["account_id"] == account_id
    assert stored["session_id"] == session_id
    assert stored["provider"] == "google"
    assert stored["browser_binding_digest"] is None
    assert stored["verifier_version"] == 1
    assert stored["verifier_key_id"] == KEY_ID
    assert stored["verifier_nonce"] == nonce
    assert len(stored["verifier_ciphertext"]) == 59

    verifier = _decrypt_snapshot(stored)
    assert derive_s256_code_challenge(verifier) == issued.code_challenge
    assert verifier.encode("ascii") != stored["verifier_ciphertext"]
    assert TOKEN_A.encode("ascii") not in stored["state_digest"]
    assert verifier.encode("ascii") not in stored["verifier_ciphertext"]

    wrong_scope = CalendarOAuthPKCEScope(
        state_digest=stored["state_digest"],
        account_id="acct_other_tenant",
        session_id=session_id,
    )
    with pytest.raises(CalendarOAuthPKCEDecryptionError):
        CIPHER.decrypt_google_calendar_verifier(
            _envelope(stored),
            scope=wrong_scope,
        )


def test_bound_issue_uses_independent_entropy_and_persists_only_digests(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, session_id = _active_demo_owner("bound-atomic")
    entropy_values = iter([BROWSER_BYTES_A, b"v" * 32])
    state_sizes = []
    byte_sizes = []
    nonce = b"n" * 12

    def state_token(size: int) -> str:
        state_sizes.append(size)
        return TOKEN_A

    def byte_token(size: int) -> bytes:
        byte_sizes.append(size)
        return next(entropy_values)

    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        state_token,
    )
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_bytes",
        byte_token,
    )
    monkeypatch.setattr(
        calendar_oauth_pkce.os,
        "urandom",
        lambda size: nonce,
    )

    issued = _issue_bound(account_id, session_id, now=T0)
    stored = _snapshot(account_id, session_id)

    assert isinstance(issued, IssuedGoogleCalendarOAuthBrowserBoundPKCEState)
    assert type(issued.issued_state) is IssuedGoogleCalendarOAuthPKCEState
    assert state_sizes == [32]
    assert byte_sizes == [32, 32]
    assert issued.issued_state.raw_state == TOKEN_A
    assert issued.raw_browser_binding == BROWSER_A
    assert issued.raw_browser_binding != issued.issued_state.raw_state
    assert issued.issued_state.expires_at == (
        T0 + datetime.timedelta(minutes=10)
    )
    assert issued.issued_state.code_challenge_method == "S256"
    assert repr(issued) == (
        "IssuedGoogleCalendarOAuthBrowserBoundPKCEState(<redacted>)"
    )
    assert str(issued) == repr(issued)
    for secret in (
        issued.issued_state.raw_state,
        issued.raw_browser_binding,
        issued.issued_state.code_challenge,
    ):
        assert secret not in repr(issued)
        assert secret not in str(issued)
    with pytest.raises(dataclasses.FrozenInstanceError):
        issued.raw_browser_binding = BROWSER_B

    assert hashlib.sha256(
        BROWSER_DIGEST_DOMAIN + BROWSER_BYTES_A
    ).digest() == BROWSER_A_DIGEST
    assert stored is not None
    assert stored["state_digest"] == _digest(TOKEN_A)
    assert stored["browser_binding_digest"] == BROWSER_A_DIGEST
    assert stored["browser_binding_digest"] != hashlib.sha256(
        BROWSER_A.encode("ascii")
    ).digest()
    assert stored["browser_binding_digest"] != stored["state_digest"]
    assert stored["verifier_nonce"] == nonce
    assert derive_s256_code_challenge(
        _decrypt_snapshot(stored)
    ) == issued.issued_state.code_challenge
    persisted = repr(stored)
    assert TOKEN_A not in persisted
    assert BROWSER_A not in persisted
    for persisted_value in (
        stored["state_digest"],
        stored["browser_binding_digest"],
        stored["verifier_nonce"],
        stored["verifier_ciphertext"],
    ):
        assert BROWSER_BYTES_A not in bytes(persisted_value)
        assert BROWSER_A.encode("ascii") not in bytes(persisted_value)


def test_bound_issue_rejects_equal_state_and_browser_candidates(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, session_id = _active_demo_owner("bound-equal")
    states = iter([TOKEN_A, TOKEN_B])
    byte_values = iter([b"\x00" * 32, BROWSER_BYTES_B, b"v" * 32])
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: next(states),
    )
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_bytes",
        lambda size: next(byte_values),
    )

    issued = _issue_bound(account_id, session_id, now=T0)
    stored = _snapshot(account_id, session_id)

    assert base64.urlsafe_b64encode(b"\x00" * 32).rstrip(b"=").decode(
        "ascii"
    ) == TOKEN_A
    assert issued.issued_state.raw_state == TOKEN_B
    assert issued.raw_browser_binding == BROWSER_B
    assert stored["state_digest"] == _digest(TOKEN_B)
    assert stored["browser_binding_digest"] == hashlib.sha256(
        BROWSER_DIGEST_DOMAIN + BROWSER_BYTES_B
    ).digest()
    assert _pair_by_digest(_digest(TOKEN_A)) is None


def test_replacement_commit_and_caller_rollback_are_atomic(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, session_id = _active_demo_owner("replacement")
    generated_states = iter([TOKEN_A, TOKEN_B, TOKEN_C])
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: next(generated_states),
    )

    first = _issue_pkce(account_id, session_id, now=T0)
    original = _snapshot(account_id, session_id)
    assert first.raw_state == TOKEN_A

    session = db.SessionLocal()
    transaction = session.begin()
    try:
        rolled_back = (
            calendar_oauth_states.issue_google_calendar_oauth_state_with_pkce(
                session,
                account_id=account_id,
                session_id=session_id,
                pkce_cipher=CIPHER,
                now=T0 + datetime.timedelta(minutes=1),
            )
        )
        assert rolled_back.raw_state == TOKEN_B
        transaction.rollback()
    finally:
        session.close()

    assert _snapshot(account_id, session_id) == original

    committed = _issue_pkce(
        account_id,
        session_id,
        now=T0 + datetime.timedelta(minutes=2),
    )
    replacement = _snapshot(account_id, session_id)

    assert committed.raw_state == TOKEN_C
    assert replacement is not None
    assert replacement["state_digest"] == _digest(TOKEN_C)
    assert replacement["state_digest"] != original["state_digest"]
    assert _pair_by_digest(original["state_digest"]) is None
    assert derive_s256_code_challenge(
        _decrypt_snapshot(replacement)
    ) == committed.code_challenge


def test_bound_replacement_commit_and_caller_rollback_are_atomic(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, session_id = _active_demo_owner("bound-replacement")
    states = iter([TOKEN_A, TOKEN_B, TOKEN_C])
    byte_values = iter(
        [
            BROWSER_BYTES_A,
            b"u" * 32,
            BROWSER_BYTES_B,
            b"v" * 32,
            BROWSER_BYTES_C,
            b"w" * 32,
        ]
    )
    nonces = iter([b"a" * 12, b"b" * 12, b"c" * 12])
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: next(states),
    )
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_bytes",
        lambda size: next(byte_values),
    )
    monkeypatch.setattr(
        calendar_oauth_pkce.os,
        "urandom",
        lambda size: next(nonces),
    )

    first = _issue_bound(account_id, session_id, now=T0)
    original = _snapshot(account_id, session_id)

    session = db.SessionLocal()
    transaction = session.begin()
    try:
        rolled_back = (
            calendar_oauth_states
            .issue_google_calendar_oauth_state_with_pkce_and_browser_binding(
                session,
                account_id=account_id,
                session_id=session_id,
                pkce_cipher=CIPHER,
                now=T0 + datetime.timedelta(minutes=1),
            )
        )
        transaction.rollback()
    finally:
        session.close()

    assert first.issued_state.raw_state == TOKEN_A
    assert first.raw_browser_binding == BROWSER_A
    assert rolled_back.issued_state.raw_state == TOKEN_B
    assert rolled_back.raw_browser_binding == BROWSER_B
    assert _snapshot(account_id, session_id) == original

    committed = _issue_bound(
        account_id,
        session_id,
        now=T0 + datetime.timedelta(minutes=2),
    )
    replacement = _snapshot(account_id, session_id)

    assert committed.issued_state.raw_state == TOKEN_C
    assert committed.raw_browser_binding == BROWSER_C
    assert replacement["state_digest"] == _digest(TOKEN_C)
    assert replacement["browser_binding_digest"] == hashlib.sha256(
        BROWSER_DIGEST_DOMAIN + BROWSER_BYTES_C
    ).digest()
    assert replacement["state_digest"] != original["state_digest"]
    assert replacement["browser_binding_digest"] != original[
        "browser_binding_digest"
    ]
    assert _pair_by_digest(original["state_digest"]) is None
    assert _pair_by_digest(_digest(TOKEN_B)) is None
    assert derive_s256_code_challenge(
        _decrypt_snapshot(replacement)
    ) == committed.issued_state.code_challenge


@pytest.mark.parametrize("previous", ["bare", "pkce"])
def test_bound_issue_replaces_every_unbound_state_form(
    migrated_postgres_db,
    monkeypatch,
    previous,
):
    account_id, session_id = _active_demo_owner(
        f"bound-replaces-{previous}"
    )
    if previous == "bare":
        _store_bare_state(account_id, session_id, TOKEN_A)
    else:
        monkeypatch.setattr(
            calendar_oauth_states.secrets,
            "token_urlsafe",
            lambda size: TOKEN_A,
        )
        _issue_pkce(account_id, session_id, now=T0)
    original = _snapshot(account_id, session_id)
    byte_values = iter([BROWSER_BYTES_A, b"v" * 32])
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: TOKEN_B,
    )
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_bytes",
        lambda size: next(byte_values),
    )

    issued = _issue_bound(
        account_id,
        session_id,
        now=T0 + datetime.timedelta(minutes=1),
    )
    stored = _snapshot(account_id, session_id)

    assert issued.issued_state.raw_state == TOKEN_B
    assert issued.raw_browser_binding == BROWSER_A
    assert stored["state_digest"] == _digest(TOKEN_B)
    assert stored["browser_binding_digest"] == BROWSER_A_DIGEST
    assert stored["verifier_ciphertext"] is not None
    assert stored["state_digest"] != original["state_digest"]
    assert _pair_by_digest(original["state_digest"]) is None


def test_legacy_issue_and_consume_preserve_verifier_backed_state(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, session_id = _active_demo_owner("legacy-fence")
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: TOKEN_A,
    )
    issued = _issue_pkce(account_id, session_id, now=T0)
    original = _snapshot(account_id, session_id)
    legacy_entropy_calls = []

    def legacy_state(size: int) -> str:
        legacy_entropy_calls.append(size)
        return TOKEN_B

    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        legacy_state,
    )

    with db.SessionLocal.begin() as session:
        with pytest.raises(CalendarOAuthStatePersistenceError):
            calendar_oauth_states.issue_google_calendar_oauth_state(
                session,
                account_id=account_id,
                session_id=session_id,
                now=T0 + datetime.timedelta(minutes=1),
            )
        session.execute(text("SELECT 1"))

    assert legacy_entropy_calls == []
    assert _snapshot(account_id, session_id) == original

    for _ in range(2):
        with db.SessionLocal.begin() as session:
            with pytest.raises(CalendarOAuthStateInvalid):
                calendar_oauth_states.consume_google_calendar_oauth_state(
                    session,
                    raw_state=issued.raw_state,
                    now=T0 + datetime.timedelta(minutes=1),
                )
            session.execute(text("SELECT 1"))
        assert _snapshot(account_id, session_id) == original


def test_pkce_issue_refuses_browser_bound_pair_before_generation(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, session_id = _active_demo_owner("bound-pkce-issue")
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: TOKEN_A,
    )
    issued = _issue_pkce(account_id, session_id, now=T0)
    binding_digest = _binding_digest("pkce-issue")
    with db.SessionLocal.begin() as session:
        session.get(
            ProviderCalendarOAuthStateDB,
            _digest(issued.raw_state),
        ).browser_binding_digest = binding_digest
    original = _snapshot(account_id, session_id)
    entropy_calls = []
    generation_calls = []

    def fail_entropy(size: int) -> str:
        entropy_calls.append(size)
        return TOKEN_B

    def fail_generation(*args, **kwargs):
        generation_calls.append((args, kwargs))
        raise AssertionError("bound states must not generate PKCE material")

    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        fail_entropy,
    )
    monkeypatch.setattr(
        CalendarOAuthPKCECipher,
        "generate_google_calendar_pkce",
        fail_generation,
    )

    with db.SessionLocal.begin() as session:
        with pytest.raises(CalendarOAuthStatePersistenceError):
            calendar_oauth_states.issue_google_calendar_oauth_state_with_pkce(
                session,
                account_id=account_id,
                session_id=session_id,
                pkce_cipher=CIPHER,
                now=T0 + datetime.timedelta(minutes=1),
            )
        session.execute(text("SELECT 1"))

    assert entropy_calls == []
    assert generation_calls == []
    assert _snapshot(account_id, session_id) == original


def test_legacy_commands_cannot_touch_actual_bound_issuance(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, session_id = _active_demo_owner("actual-bound-fences")
    setup_bytes = iter([BROWSER_BYTES_A, b"v" * 32])
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: TOKEN_A,
    )
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_bytes",
        lambda size: next(setup_bytes),
    )
    issued = _issue_bound(account_id, session_id, now=T0)
    original = _snapshot(account_id, session_id)
    entropy_calls = []
    decrypt_calls = []

    def fail_entropy(size: int) -> str:
        entropy_calls.append(size)
        return TOKEN_B

    def fail_decrypt(*args, **kwargs):
        decrypt_calls.append((args, kwargs))
        raise AssertionError("legacy consumers must not decrypt bound state")

    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        fail_entropy,
    )
    monkeypatch.setattr(
        CalendarOAuthPKCECipher,
        "decrypt_google_calendar_verifier",
        fail_decrypt,
    )

    for operation in (
        "legacy_issue",
        "pkce_issue",
        "legacy_consume",
        "pkce_consume",
    ):
        with db.SessionLocal.begin() as session:
            error = (
                CalendarOAuthStatePersistenceError
                if operation.endswith("issue")
                else CalendarOAuthStateInvalid
            )
            with pytest.raises(error):
                if operation == "legacy_issue":
                    calendar_oauth_states.issue_google_calendar_oauth_state(
                        session,
                        account_id=account_id,
                        session_id=session_id,
                        now=T0,
                    )
                elif operation == "pkce_issue":
                    calendar_oauth_states.issue_google_calendar_oauth_state_with_pkce(
                        session,
                        account_id=account_id,
                        session_id=session_id,
                        pkce_cipher=CIPHER,
                        now=T0,
                    )
                elif operation == "legacy_consume":
                    calendar_oauth_states.consume_google_calendar_oauth_state(
                        session,
                        raw_state=issued.issued_state.raw_state,
                        now=T0,
                    )
                else:
                    calendar_oauth_states.consume_google_calendar_oauth_state_with_pkce(
                        session,
                        raw_state=issued.issued_state.raw_state,
                        pkce_cipher=CIPHER,
                        now=T0,
                    )
            session.execute(text("SELECT 1"))
        assert _snapshot(account_id, session_id) == original

    assert entropy_calls == []
    assert decrypt_calls == []


@pytest.mark.parametrize("command", ["legacy", "pkce"])
def test_issue_race_preserves_binding_after_stale_precheck(
    migrated_postgres_db,
    monkeypatch,
    command,
):
    account_id, session_id = _active_demo_owner(f"binding-race-{command}")
    _store_bare_state(
        account_id,
        session_id,
        TOKEN_A,
        browser_binding_digest=_binding_digest(f"binding-race-{command}"),
    )
    original = _snapshot(account_id, session_id)
    binding_checks = []
    entropy_calls = []

    def stale_then_bound(*args, **kwargs) -> bool:
        binding_checks.append((args, kwargs))
        return len(binding_checks) > 1

    def state_candidate(size: int) -> str:
        entropy_calls.append(size)
        return TOKEN_B

    monkeypatch.setattr(
        calendar_oauth_states,
        "_locked_owner_has_browser_bound_state",
        stale_then_bound,
    )
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        state_candidate,
    )

    digest_checks = []
    if command == "pkce":
        def digest_exists(*args, **kwargs) -> bool:
            digest_checks.append((args, kwargs))
            return len(digest_checks) > 1

        monkeypatch.setattr(
            calendar_oauth_states,
            "_digest_exists",
            digest_exists,
        )

    with db.SessionLocal.begin() as session:
        with pytest.raises(CalendarOAuthStatePersistenceError):
            if command == "legacy":
                calendar_oauth_states.issue_google_calendar_oauth_state(
                    session,
                    account_id=account_id,
                    session_id=session_id,
                    now=T0,
                )
            else:
                calendar_oauth_states.issue_google_calendar_oauth_state_with_pkce(
                    session,
                    account_id=account_id,
                    session_id=session_id,
                    pkce_cipher=CIPHER,
                    now=T0,
                )
        session.execute(text("SELECT 1"))

    assert entropy_calls == [32]
    assert len(binding_checks) == 2
    assert len(digest_checks) == (1 if command == "pkce" else 0)
    assert _snapshot(account_id, session_id) == original


def test_state_digest_collision_retries_before_mutation(
    migrated_postgres_db,
    monkeypatch,
):
    occupied_account, occupied_session = _active_demo_owner("digest-occupied")
    target_account, target_session = _add_owner("digest-target")
    _store_bare_state(occupied_account, occupied_session, TOKEN_A)
    candidates = iter([TOKEN_A, TOKEN_B])
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: next(candidates),
    )

    issued = _issue_pkce(target_account, target_session, now=T0)

    assert issued.raw_state == TOKEN_B
    assert _snapshot(occupied_account, occupied_session)["state_digest"] == _digest(
        TOKEN_A
    )
    target = _snapshot(target_account, target_session)
    assert target is not None
    assert target["state_digest"] == _digest(TOKEN_B)
    assert target["verifier_ciphertext"] is not None


@pytest.mark.parametrize("collision", ["state", "binding", "nonce"])
@pytest.mark.parametrize("post_precheck", [False, True])
def test_bound_issue_retries_collisions_with_fresh_material(
    migrated_postgres_db,
    monkeypatch,
    collision,
    post_precheck,
):
    occupied_account, occupied_session = _active_demo_owner(
        f"bound-collision-{collision}"
    )
    target_account, target_session = _add_owner(
        f"bound-collision-target-{collision}"
    )
    shared_nonce = b"n" * 12
    target_nonce = b"t" * 12
    fresh_nonce = b"f" * 12
    setup_states = iter([TOKEN_A, TOKEN_E])
    setup_bytes = iter(
        [BROWSER_BYTES_A, b"o" * 32, BROWSER_BYTES_D, b"t" * 32]
    )
    setup_nonces = iter([shared_nonce, target_nonce])
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: next(setup_states),
    )
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_bytes",
        lambda size: next(setup_bytes),
    )
    monkeypatch.setattr(
        calendar_oauth_pkce.os,
        "urandom",
        lambda size: next(setup_nonces),
    )
    _issue_bound(occupied_account, occupied_session, now=T0)
    _issue_bound(target_account, target_session, now=T0)
    occupied = _snapshot(occupied_account, occupied_session)
    original = _snapshot(target_account, target_session)
    hidden_once = False
    restored_after_failure = False

    def assert_target_restored(session):
        nonlocal restored_after_failure
        state, verifier = (
            session.query(
                ProviderCalendarOAuthStateDB,
                ProviderCalendarOAuthPKCEVerifierDB,
            )
            .join(
                ProviderCalendarOAuthPKCEVerifierDB,
                ProviderCalendarOAuthPKCEVerifierDB.state_digest
                == ProviderCalendarOAuthStateDB.state_digest,
            )
            .filter(
                ProviderCalendarOAuthStateDB.account_id == target_account,
                ProviderCalendarOAuthStateDB.session_id == target_session,
            )
            .one()
        )
        assert bytes(state.state_digest) == original["state_digest"]
        assert bytes(state.browser_binding_digest) == original[
            "browser_binding_digest"
        ]
        assert bytes(verifier.verifier_nonce) == original["verifier_nonce"]
        assert bytes(verifier.verifier_ciphertext) == original[
            "verifier_ciphertext"
        ]
        restored_after_failure = True

    if post_precheck and collision == "state":
        real_exists = calendar_oauth_states._digest_exists

        def digest_exists(session, state_digest):
            nonlocal hidden_once
            if state_digest == occupied["state_digest"] and not hidden_once:
                hidden_once = True
                return False
            if state_digest == occupied["state_digest"]:
                assert_target_restored(session)
            return real_exists(session, state_digest)

        monkeypatch.setattr(
            calendar_oauth_states,
            "_digest_exists",
            digest_exists,
        )
    elif post_precheck and collision == "binding":
        real_exists = calendar_oauth_states._browser_binding_digest_exists

        def binding_exists(session, browser_binding_digest):
            nonlocal hidden_once
            if (
                browser_binding_digest
                == occupied["browser_binding_digest"]
                and not hidden_once
            ):
                hidden_once = True
                return False
            if browser_binding_digest == occupied["browser_binding_digest"]:
                assert_target_restored(session)
            return real_exists(session, browser_binding_digest)

        monkeypatch.setattr(
            calendar_oauth_states,
            "_browser_binding_digest_exists",
            binding_exists,
        )
    elif post_precheck:
        real_exists = calendar_oauth_states._pkce_nonce_exists

        def nonce_exists(session, *, key_id, nonce):
            nonlocal hidden_once
            if nonce == occupied["verifier_nonce"] and not hidden_once:
                hidden_once = True
                return False
            if nonce == occupied["verifier_nonce"]:
                assert_target_restored(session)
            return real_exists(session, key_id=key_id, nonce=nonce)

        monkeypatch.setattr(
            calendar_oauth_states,
            "_pkce_nonce_exists",
            nonce_exists,
        )

    if collision == "state":
        state_values = [TOKEN_A, TOKEN_B]
        if post_precheck:
            byte_values = [
                BROWSER_BYTES_B,
                b"v" * 32,
                BROWSER_BYTES_C,
                b"w" * 32,
            ]
            nonce_values = [b"r" * 12, fresh_nonce]
            expected_browser = BROWSER_C
        else:
            byte_values = [BROWSER_BYTES_B, b"v" * 32]
            nonce_values = [fresh_nonce]
            expected_browser = BROWSER_B
        expected_state = TOKEN_B
    elif collision == "binding":
        state_values = [TOKEN_B, TOKEN_C]
        if post_precheck:
            byte_values = [
                BROWSER_BYTES_A,
                b"v" * 32,
                BROWSER_BYTES_B,
                b"w" * 32,
            ]
            nonce_values = [b"r" * 12, fresh_nonce]
        else:
            byte_values = [
                BROWSER_BYTES_A,
                BROWSER_BYTES_B,
                b"v" * 32,
            ]
            nonce_values = [fresh_nonce]
        expected_state = TOKEN_C
        expected_browser = BROWSER_B
    else:
        state_values = [TOKEN_B, TOKEN_C]
        byte_values = [
            BROWSER_BYTES_B,
            b"v" * 32,
            BROWSER_BYTES_C,
            b"w" * 32,
        ]
        nonce_values = [shared_nonce, fresh_nonce]
        expected_state = TOKEN_C
        expected_browser = BROWSER_C

    states = iter(state_values)
    bytes_and_verifiers = iter(byte_values)
    nonces = iter(nonce_values)
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: next(states),
    )
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_bytes",
        lambda size: next(bytes_and_verifiers),
    )
    monkeypatch.setattr(
        calendar_oauth_pkce.os,
        "urandom",
        lambda size: next(nonces),
    )

    issued = _issue_bound(target_account, target_session, now=T0)
    target = _snapshot(target_account, target_session)

    assert _snapshot(occupied_account, occupied_session) == occupied
    assert hidden_once is post_precheck
    assert restored_after_failure is post_precheck
    assert issued.issued_state.raw_state == expected_state
    assert issued.raw_browser_binding == expected_browser
    assert target["state_digest"] == _digest(expected_state)
    expected_browser_bytes = base64.urlsafe_b64decode(
        expected_browser + "="
    )
    assert target["browser_binding_digest"] == hashlib.sha256(
        BROWSER_DIGEST_DOMAIN + expected_browser_bytes
    ).digest()
    assert target["verifier_nonce"] == fresh_nonce
    assert derive_s256_code_challenge(
        _decrypt_snapshot(target)
    ) == issued.issued_state.code_challenge


def test_same_key_nonce_collision_retries_complete_candidate_bundle(
    migrated_postgres_db,
    monkeypatch,
):
    occupied_account, occupied_session = _active_demo_owner("nonce-occupied")
    target_account, target_session = _add_owner("nonce-target")
    shared_nonce = b"n" * 12
    fresh_nonce = b"f" * 12
    states = iter([TOKEN_A, TOKEN_B, TOKEN_C])
    nonces = iter([shared_nonce, shared_nonce, fresh_nonce])
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: next(states),
    )
    monkeypatch.setattr(
        calendar_oauth_pkce.os,
        "urandom",
        lambda size: next(nonces),
    )

    _issue_pkce(occupied_account, occupied_session, now=T0)
    issued = _issue_pkce(target_account, target_session, now=T0)

    assert issued.raw_state == TOKEN_C
    assert _snapshot(occupied_account, occupied_session)[
        "verifier_nonce"
    ] == shared_nonce
    target = _snapshot(target_account, target_session)
    assert target is not None
    assert target["state_digest"] == _digest(TOKEN_C)
    assert target["verifier_nonce"] == fresh_nonce


def test_post_precheck_digest_race_retries_after_primary_key_conflict(
    migrated_postgres_db,
    monkeypatch,
):
    occupied_account, occupied_session = _active_demo_owner("digest-race")
    target_account, target_session = _add_owner("digest-race-target")
    _store_bare_state(occupied_account, occupied_session, TOKEN_A)
    candidates = iter([TOKEN_A, TOKEN_B])
    real_digest_exists = calendar_oauth_states._digest_exists
    hidden_once = False

    def digest_exists(session, state_digest):
        nonlocal hidden_once
        if state_digest == _digest(TOKEN_A) and not hidden_once:
            hidden_once = True
            return False
        return real_digest_exists(session, state_digest)

    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: next(candidates),
    )
    monkeypatch.setattr(
        calendar_oauth_states,
        "_digest_exists",
        digest_exists,
    )

    issued = _issue_pkce(target_account, target_session, now=T0)

    assert hidden_once is True
    assert issued.raw_state == TOKEN_B
    assert _snapshot(occupied_account, occupied_session)["state_digest"] == _digest(
        TOKEN_A
    )
    assert _snapshot(target_account, target_session)["state_digest"] == _digest(
        TOKEN_B
    )


def test_post_precheck_nonce_race_retries_after_unique_index_conflict(
    migrated_postgres_db,
    monkeypatch,
):
    occupied_account, occupied_session = _active_demo_owner("nonce-race")
    target_account, target_session = _add_owner("nonce-race-target")
    shared_nonce = b"r" * 12
    fresh_nonce = b"s" * 12
    states = iter([TOKEN_A, TOKEN_B, TOKEN_C])
    nonces = iter([shared_nonce, shared_nonce, fresh_nonce])
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: next(states),
    )
    monkeypatch.setattr(
        calendar_oauth_pkce.os,
        "urandom",
        lambda size: next(nonces),
    )
    _issue_pkce(occupied_account, occupied_session, now=T0)

    real_nonce_exists = calendar_oauth_states._pkce_nonce_exists
    hidden_once = False

    def nonce_exists(session, *, key_id, nonce):
        nonlocal hidden_once
        if key_id == KEY_ID and nonce == shared_nonce and not hidden_once:
            hidden_once = True
            return False
        return real_nonce_exists(session, key_id=key_id, nonce=nonce)

    monkeypatch.setattr(
        calendar_oauth_states,
        "_pkce_nonce_exists",
        nonce_exists,
    )

    issued = _issue_pkce(target_account, target_session, now=T0)

    assert hidden_once is True
    assert issued.raw_state == TOKEN_C
    assert _snapshot(occupied_account, occupied_session)[
        "verifier_nonce"
    ] == shared_nonce
    assert _snapshot(target_account, target_session)[
        "verifier_nonce"
    ] == fresh_nonce


def test_same_nonce_is_valid_under_different_active_key_ids(
    migrated_postgres_db,
    monkeypatch,
):
    first_account, first_session = _active_demo_owner("nonce-key-a")
    second_account, second_session = _add_owner("nonce-key-b")
    second_cipher = CalendarOAuthPKCECipher(
        active_key_id="pkce-state-key-rotated",
        root_keys={"pkce-state-key-rotated": b"q" * 32},
    )
    shared_nonce = b"k" * 12
    states = iter([TOKEN_A, TOKEN_B])
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: next(states),
    )
    monkeypatch.setattr(
        calendar_oauth_pkce.os,
        "urandom",
        lambda size: shared_nonce,
    )

    _issue_pkce(first_account, first_session, now=T0)
    _issue_pkce(
        second_account,
        second_session,
        now=T0,
        cipher=second_cipher,
    )

    first = _snapshot(first_account, first_session)
    second = _snapshot(second_account, second_session)
    assert first["verifier_nonce"] == second["verifier_nonce"] == shared_nonce
    assert first["verifier_key_id"] == KEY_ID
    assert second["verifier_key_id"] == "pkce-state-key-rotated"


def test_nonce_collision_exhaustion_preserves_previous_pair_and_transaction(
    migrated_postgres_db,
    monkeypatch,
):
    occupied_account, occupied_session = _active_demo_owner("nonce-exhausted")
    target_account, target_session = _add_owner("nonce-preserved")
    occupied_nonce = b"o" * 12
    target_nonce = b"t" * 12
    setup_states = iter([TOKEN_A, TOKEN_B])
    setup_nonces = iter([occupied_nonce, target_nonce])
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: next(setup_states),
    )
    monkeypatch.setattr(
        calendar_oauth_pkce.os,
        "urandom",
        lambda size: next(setup_nonces),
    )
    _issue_pkce(occupied_account, occupied_session, now=T0)
    _issue_pkce(target_account, target_session, now=T0)
    original = _snapshot(target_account, target_session)

    candidates = iter([TOKEN_C, TOKEN_D, TOKEN_E])
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: next(candidates),
    )
    monkeypatch.setattr(
        calendar_oauth_pkce.os,
        "urandom",
        lambda size: occupied_nonce,
    )

    with db.SessionLocal.begin() as session:
        with pytest.raises(CalendarOAuthStatePersistenceError) as raised:
            calendar_oauth_states.issue_google_calendar_oauth_state_with_pkce(
                session,
                account_id=target_account,
                session_id=target_session,
                pkce_cipher=CIPHER,
                now=T0 + datetime.timedelta(minutes=1),
            )
        session.execute(text("SELECT 1"))

    _assert_error_safe(
        raised.value,
        target_account,
        target_session,
        occupied_nonce.hex(),
        repr(occupied_nonce),
    )
    assert _snapshot(target_account, target_session) == original
    assert _snapshot(occupied_account, occupied_session)[
        "verifier_nonce"
    ] == occupied_nonce


def test_bound_binding_collision_exhaustion_is_redacted_and_non_destructive(
    migrated_postgres_db,
    monkeypatch,
    caplog,
):
    occupied_account, occupied_session = _active_demo_owner(
        "bound-binding-occupied"
    )
    target_account, target_session = _add_owner("bound-binding-preserved")
    setup_states = iter([TOKEN_A, TOKEN_B])
    setup_bytes = iter(
        [BROWSER_BYTES_A, b"o" * 32, BROWSER_BYTES_B, b"t" * 32]
    )
    setup_nonces = iter([b"o" * 12, b"t" * 12])
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: next(setup_states),
    )
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_bytes",
        lambda size: next(setup_bytes),
    )
    monkeypatch.setattr(
        calendar_oauth_pkce.os,
        "urandom",
        lambda size: next(setup_nonces),
    )
    _issue_bound(occupied_account, occupied_session, now=T0)
    _issue_bound(target_account, target_session, now=T0)
    occupied = _snapshot(occupied_account, occupied_session)
    original = _snapshot(target_account, target_session)
    candidates = iter([TOKEN_C, TOKEN_D, TOKEN_E])
    generation_calls = []
    caplog.set_level("DEBUG")

    def fail_generation(*args, **kwargs):
        generation_calls.append((args, kwargs))
        raise AssertionError("binding collisions must fail before PKCE")

    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: next(candidates),
    )
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_bytes",
        lambda size: BROWSER_BYTES_A,
    )
    monkeypatch.setattr(
        CalendarOAuthPKCECipher,
        "generate_google_calendar_pkce",
        fail_generation,
    )

    with db.SessionLocal.begin() as session:
        with pytest.raises(CalendarOAuthStatePersistenceError) as raised:
            (
                calendar_oauth_states
                .issue_google_calendar_oauth_state_with_pkce_and_browser_binding
            )(
                session,
                account_id=target_account,
                session_id=target_session,
                pkce_cipher=CIPHER,
                now=T0 + datetime.timedelta(minutes=1),
            )
        session.execute(text("SELECT 1"))

    _assert_error_safe(
        raised.value,
        target_account,
        target_session,
        BROWSER_A,
        BROWSER_A_DIGEST.hex(),
        repr(BROWSER_A_DIGEST),
    )
    assert generation_calls == []
    assert caplog.text == ""
    assert _snapshot(occupied_account, occupied_session) == occupied
    assert _snapshot(target_account, target_session) == original


@pytest.mark.parametrize("issuer", ["pkce", "bound"])
def test_unrelated_integrity_failure_is_not_retried_and_preserves_previous_pair(
    migrated_postgres_db,
    monkeypatch,
    issuer,
):
    account_id, session_id = _active_demo_owner(f"integrity-{issuer}")
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: TOKEN_A,
    )
    if issuer == "bound":
        _issue_bound(account_id, session_id, now=T0)
    else:
        _issue_pkce(account_id, session_id, now=T0)
    original = _snapshot(account_id, session_id)
    generation_calls = []

    def invalid_envelope(self, *, scope):
        generation_calls.append(scope)
        return GeneratedGoogleCalendarPKCE(
            code_challenge="c" * 43,
            encrypted_verifier=EncryptedCalendarOAuthPKCEVerifier(
                version=2,
                key_id=KEY_ID,
                nonce=b"i" * 12,
                ciphertext=b"x" * 59,
            ),
        )

    monkeypatch.setattr(
        CalendarOAuthPKCECipher,
        "generate_google_calendar_pkce",
        invalid_envelope,
    )
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: TOKEN_B,
    )

    with db.SessionLocal.begin() as session:
        with pytest.raises(CalendarOAuthStatePersistenceError):
            if issuer == "bound":
                (
                    calendar_oauth_states
                    .issue_google_calendar_oauth_state_with_pkce_and_browser_binding
                )(
                    session,
                    account_id=account_id,
                    session_id=session_id,
                    pkce_cipher=CIPHER,
                    now=T0 + datetime.timedelta(minutes=1),
                )
            else:
                calendar_oauth_states.issue_google_calendar_oauth_state_with_pkce(
                    session,
                    account_id=account_id,
                    session_id=session_id,
                    pkce_cipher=CIPHER,
                    now=T0 + datetime.timedelta(minutes=1),
                )
        session.execute(text("SELECT 1"))

    assert len(generation_calls) == 1
    assert _snapshot(account_id, session_id) == original


@pytest.mark.parametrize("issuer", ["pkce", "bound"])
def test_generation_failure_is_redacted_and_preserves_previous_pair(
    migrated_postgres_db,
    monkeypatch,
    caplog,
    issuer,
):
    account_id, session_id = _active_demo_owner(
        f"generation-failure-{issuer}"
    )
    secret = "plaintext-verifier-from-failed-generator"
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: TOKEN_A,
    )
    if issuer == "bound":
        _issue_bound(account_id, session_id, now=T0)
    else:
        _issue_pkce(account_id, session_id, now=T0)
    original = _snapshot(account_id, session_id)
    caplog.set_level("DEBUG")

    def fail_generation(self, *, scope):
        raise RuntimeError(secret + repr(scope))

    monkeypatch.setattr(
        CalendarOAuthPKCECipher,
        "generate_google_calendar_pkce",
        fail_generation,
    )
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: TOKEN_B,
    )
    if issuer == "bound":
        monkeypatch.setattr(
            calendar_oauth_states.secrets,
            "token_bytes",
            lambda size: BROWSER_BYTES_B,
        )

    with db.SessionLocal.begin() as session:
        with pytest.raises(CalendarOAuthStatePersistenceError) as raised:
            if issuer == "bound":
                (
                    calendar_oauth_states
                    .issue_google_calendar_oauth_state_with_pkce_and_browser_binding
                )(
                    session,
                    account_id=account_id,
                    session_id=session_id,
                    pkce_cipher=CIPHER,
                    now=T0 + datetime.timedelta(minutes=1),
                )
            else:
                calendar_oauth_states.issue_google_calendar_oauth_state_with_pkce(
                    session,
                    account_id=account_id,
                    session_id=session_id,
                    pkce_cipher=CIPHER,
                    now=T0 + datetime.timedelta(minutes=1),
                )
        session.execute(text("SELECT 1"))

    secrets_to_redact = [
        secret,
        account_id,
        session_id,
        TOKEN_A,
        TOKEN_B,
        ROOT_KEY.hex(),
        repr(ROOT_KEY),
    ]
    if issuer == "bound":
        secrets_to_redact.extend(
            [
                BROWSER_B,
                hashlib.sha256(
                    BROWSER_DIGEST_DOMAIN + BROWSER_BYTES_B
                ).digest().hex(),
            ]
        )
    _assert_error_safe(raised.value, *secrets_to_redact)
    assert caplog.text == ""
    assert _snapshot(account_id, session_id) == original


@pytest.mark.parametrize(
    ("issuer", "invalid_owner"),
    [
        ("pkce", "missing_account"),
        ("pkce", "missing_session"),
        ("pkce", "disabled_account"),
        ("pkce", "revoked_session"),
        ("pkce", "expired_session"),
        ("pkce", "future_session"),
        ("pkce", "stale_session_version"),
        ("bound", "revoked_session"),
    ],
)
def test_inactive_owner_is_rejected_before_pkce_generation_and_preserves_pair(
    migrated_postgres_db,
    monkeypatch,
    invalid_owner,
    issuer,
):
    account_id, session_id = _active_demo_owner(
        f"inactive-{issuer}-{invalid_owner}"
    )
    if issuer == "bound":
        _issue_bound(account_id, session_id, now=T0)
    else:
        _issue_pkce(account_id, session_id, now=T0)
    original = _snapshot(account_id, session_id)
    call_account_id = account_id
    call_session_id = session_id

    if invalid_owner == "missing_account":
        call_account_id = "acct_missing_pkce_owner"
    elif invalid_owner == "missing_session":
        call_session_id = "sess_missing_pkce_owner"
    else:
        with db.SessionLocal.begin() as session:
            account = session.get(ProviderAccountDB, account_id)
            provider_session = session.get(ProviderSessionDB, session_id)
            if invalid_owner == "disabled_account":
                account.disabled_at = T0
            elif invalid_owner == "revoked_session":
                provider_session.revoked_at = T0
            elif invalid_owner == "expired_session":
                provider_session.expires_at = T0
            elif invalid_owner == "future_session":
                provider_session.created_at = T0 + datetime.timedelta(minutes=1)
            else:
                account.session_version += 1

    generation_calls = []
    state_entropy_calls = []
    byte_entropy_calls = []

    def record_generation(self, *, scope):
        generation_calls.append(scope)
        raise AssertionError("inactive owner must fail before PKCE generation")

    monkeypatch.setattr(
        CalendarOAuthPKCECipher,
        "generate_google_calendar_pkce",
        record_generation,
    )
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_urlsafe",
        lambda size: state_entropy_calls.append(size) or TOKEN_A,
    )
    monkeypatch.setattr(
        calendar_oauth_states.secrets,
        "token_bytes",
        lambda size: byte_entropy_calls.append(size) or BROWSER_BYTES_A,
    )

    with pytest.raises(CalendarOAuthStateSessionInvalid) as raised:
        if issuer == "bound":
            _issue_bound(call_account_id, call_session_id, now=T0)
        else:
            _issue_pkce(call_account_id, call_session_id, now=T0)

    assert generation_calls == []
    assert state_entropy_calls == []
    assert byte_entropy_calls == []
    _assert_error_safe(
        raised.value,
        account_id,
        session_id,
        call_account_id,
        call_session_id,
    )
    assert _snapshot(account_id, session_id) == original


@pytest.mark.parametrize("issuer", ["pkce", "bound"])
def test_short_session_caps_state_expiry(migrated_postgres_db, issuer):
    account_id = _demo_account_id()
    session_expiry = T0 + datetime.timedelta(minutes=2)
    session_id = _add_session(
        account_id,
        f"short-expiry-{issuer}",
        expires_at=session_expiry,
    )

    if issuer == "bound":
        issued_state = _issue_bound(
            account_id,
            session_id,
            now=T0,
        ).issued_state
    else:
        issued_state = _issue_pkce(account_id, session_id, now=T0)

    assert issued_state.expires_at == session_expiry
    assert _snapshot(account_id, session_id)["expires_at"] == session_expiry


def test_persisted_owner_binding_rejects_cross_tenant_session(
    migrated_postgres_db,
):
    first_account, first_session = _active_demo_owner("tenant-first")
    second_account, _ = _add_owner("tenant-second")

    with pytest.raises(CalendarOAuthStateSessionInvalid) as raised:
        _issue_pkce(second_account, first_session, now=T0)

    _assert_error_safe(
        raised.value,
        first_account,
        first_session,
        second_account,
    )
    assert _snapshot(first_account, first_session) is None


@pytest.mark.parametrize(
    ("first_operation", "second_operation"),
    [
        ("pkce", "legacy_issue"),
        ("pkce", "legacy_consume"),
        ("legacy_issue", "pkce"),
        ("legacy_consume", "pkce"),
        ("binding", "legacy_issue"),
        ("binding", "legacy_consume"),
        ("binding", "pkce"),
        ("binding", "pkce_consume"),
    ],
)
@pytest.mark.parametrize("first_commits", [True, False])
def test_oauth_commands_serialize_without_partial_or_bound_rows(
    migrated_postgres_db,
    monkeypatch,
    first_operation,
    second_operation,
    first_commits,
):
    suffix = f"legacy-race-{first_operation}-{second_operation}-{first_commits}"
    account_id, session_id = _active_demo_owner(suffix)
    callback_state = TOKEN_A
    binding_digest = _binding_digest(suffix)
    if first_operation == "binding" and second_operation in {
        "pkce",
        "pkce_consume",
    }:
        callback_state = _issue_pkce(
            account_id,
            session_id,
            now=T0,
        ).raw_state
    elif (
        first_operation == "binding"
        or any(
            operation.endswith("consume")
            for operation in (first_operation, second_operation)
        )
        or first_operation == "pkce"
    ):
        _store_bare_state(account_id, session_id, TOKEN_A)
    original = _snapshot(account_id, session_id)
    entropy_calls = []
    if first_operation == "binding":
        def replacement_state(size: int) -> str:
            entropy_calls.append(size)
            return TOKEN_B

        monkeypatch.setattr(
            calendar_oauth_states.secrets,
            "token_urlsafe",
            replacement_state,
        )

    first_finished = threading.Event()
    release_first = threading.Event()
    second_pid_ready = threading.Event()
    second_pid = {}
    results = {}

    def run_operation(session, operation):
        if operation == "binding":
            session.query(ProviderAccountDB).filter_by(
                id=account_id
            ).with_for_update().one()
            session.query(ProviderSessionDB).filter_by(
                id=session_id,
                account_id=account_id,
            ).with_for_update().one()
            state = session.query(ProviderCalendarOAuthStateDB).filter_by(
                account_id=account_id,
                session_id=session_id,
            ).with_for_update().one()
            state.browser_binding_digest = binding_digest
            session.flush()
            return "binding", binding_digest
        if operation == "pkce":
            try:
                result = (
                    calendar_oauth_states.issue_google_calendar_oauth_state_with_pkce(
                        session,
                        account_id=account_id,
                        session_id=session_id,
                        pkce_cipher=CIPHER,
                        now=T0,
                    )
                )
                return "pkce", result
            except CalendarOAuthStatePersistenceError:
                return "protected", None
        if operation == "legacy_issue":
            try:
                result = calendar_oauth_states.issue_google_calendar_oauth_state(
                    session,
                    account_id=account_id,
                    session_id=session_id,
                    now=T0,
                )
                return "legacy_issued", result
            except CalendarOAuthStatePersistenceError:
                return "protected", None
        try:
            if operation == "pkce_consume":
                result = (
                    calendar_oauth_states.consume_google_calendar_oauth_state_with_pkce(
                        session,
                        raw_state=callback_state,
                        pkce_cipher=CIPHER,
                        now=T0,
                    )
                )
                return "pkce_consumed", result
            result = calendar_oauth_states.consume_google_calendar_oauth_state(
                session,
                raw_state=callback_state,
                now=T0,
            )
            return "legacy_consumed", result
        except CalendarOAuthStateInvalid:
            return "invalid", None

    def first_command():
        session = db.SessionLocal()
        transaction = session.begin()
        try:
            outcome, result = run_operation(session, first_operation)
            results["first"] = (outcome, result)
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
            outcome, result = run_operation(session, second_operation)
            results["second"] = (outcome, result)
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
        first_future.result(timeout=15)
        second_outcome = second_future.result(timeout=15)

    if first_operation == "binding":
        if first_commits:
            assert second_outcome == (
                "protected"
                if second_operation.endswith("issue")
                or second_operation == "pkce"
                else "invalid"
            )
            expected = dict(original)
            expected["browser_binding_digest"] = binding_digest
            assert _snapshot(account_id, session_id) == expected
            assert entropy_calls == []
        elif second_operation == "legacy_issue":
            assert second_outcome == "legacy_issued"
            assert _owner_row_counts(account_id, session_id) == (1, 0)
            stored = _snapshot(account_id, session_id)
            assert stored["state_digest"] == _digest(
                results["second"][1].raw_state
            )
            assert stored["state_digest"] != original["state_digest"]
            assert stored["browser_binding_digest"] is None
            assert entropy_calls == [32]
        elif second_operation == "legacy_consume":
            assert second_outcome == "legacy_consumed"
            assert _owner_row_counts(account_id, session_id) == (0, 0)
            assert entropy_calls == []
        elif second_operation == "pkce":
            assert second_outcome == "pkce"
            assert _owner_row_counts(account_id, session_id) == (1, 1)
            stored = _snapshot(account_id, session_id)
            assert stored["state_digest"] == _digest(
                results["second"][1].raw_state
            )
            assert stored["state_digest"] != original["state_digest"]
            assert stored["browser_binding_digest"] is None
            assert stored["verifier_ciphertext"] is not None
            assert entropy_calls == [32]
        else:
            assert second_outcome == "pkce_consumed"
            assert _owner_row_counts(account_id, session_id) == (0, 0)
            assert entropy_calls == []
    elif first_operation == "pkce" and first_commits:
        assert second_outcome == (
            "protected" if second_operation == "legacy_issue" else "invalid"
        )
        assert _owner_row_counts(account_id, session_id) == (1, 1)
        stored = _snapshot(account_id, session_id)
        assert stored["state_digest"] == _digest(
            results["first"][1].raw_state
        )
    elif first_operation == "pkce":
        if second_operation == "legacy_issue":
            assert second_outcome == "legacy_issued"
            assert _owner_row_counts(account_id, session_id) == (1, 0)
        else:
            assert second_outcome == "legacy_consumed"
            assert _owner_row_counts(account_id, session_id) == (0, 0)
    else:
        assert second_outcome == "pkce"
        assert _owner_row_counts(account_id, session_id) == (1, 1)
        stored = _snapshot(account_id, session_id)
        assert stored["state_digest"] == _digest(
            results["second"][1].raw_state
        )


@pytest.mark.parametrize("issuer", ["pkce", "bound"])
def test_concurrent_issuance_leaves_one_complete_matching_record(
    migrated_postgres_db,
    issuer,
):
    account_id, session_id = _active_demo_owner(f"concurrent-{issuer}")
    barrier = threading.Barrier(2)

    def issue():
        session = db.SessionLocal()
        try:
            with session.begin():
                barrier.wait(timeout=5)
                if issuer == "bound":
                    issue_bound = (
                        calendar_oauth_states
                        .issue_google_calendar_oauth_state_with_pkce_and_browser_binding
                    )
                    return (
                        issue_bound(
                            session,
                            account_id=account_id,
                            session_id=session_id,
                            pkce_cipher=CIPHER,
                            now=T0,
                        )
                    )
                return (
                    calendar_oauth_states.issue_google_calendar_oauth_state_with_pkce(
                        session,
                        account_id=account_id,
                        session_id=session_id,
                        pkce_cipher=CIPHER,
                        now=T0,
                    )
                )
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result(timeout=15) for future in [
            executor.submit(issue),
            executor.submit(issue),
        ]]

    issued_states = [
        result.issued_state if issuer == "bound" else result
        for result in results
    ]
    assert issued_states[0].raw_state != issued_states[1].raw_state
    stored = _snapshot(account_id, session_id)
    assert stored is not None
    matching_index = next(
        index
        for index, issued_state in enumerate(issued_states)
        if _digest(issued_state.raw_state) == stored["state_digest"]
    )
    matching_state = issued_states[matching_index]
    assert derive_s256_code_challenge(
        _decrypt_snapshot(stored)
    ) == matching_state.code_challenge
    if issuer == "bound":
        assert results[0].raw_browser_binding != (
            results[1].raw_browser_binding
        )
        binding_bytes = base64.urlsafe_b64decode(
            results[matching_index].raw_browser_binding + "="
        )
        assert stored["browser_binding_digest"] == hashlib.sha256(
            BROWSER_DIGEST_DOMAIN + binding_bytes
        ).digest()
    else:
        assert stored["browser_binding_digest"] is None
    assert _owner_row_counts(account_id, session_id) == (1, 1)


def _issue_pkce(
    account_id: str,
    session_id: str,
    *,
    now: datetime.datetime,
    cipher: CalendarOAuthPKCECipher = CIPHER,
) -> IssuedGoogleCalendarOAuthPKCEState:
    with db.SessionLocal.begin() as session:
        return calendar_oauth_states.issue_google_calendar_oauth_state_with_pkce(
            session,
            account_id=account_id,
            session_id=session_id,
            pkce_cipher=cipher,
            now=now,
        )


def _issue_bound(
    account_id: str,
    session_id: str,
    *,
    now: datetime.datetime,
    cipher: CalendarOAuthPKCECipher = CIPHER,
) -> IssuedGoogleCalendarOAuthBrowserBoundPKCEState:
    with db.SessionLocal.begin() as session:
        return (
            calendar_oauth_states
            .issue_google_calendar_oauth_state_with_pkce_and_browser_binding(
                session,
                account_id=account_id,
                session_id=session_id,
                pkce_cipher=cipher,
                now=now,
            )
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


def _pair_by_digest(state_digest: bytes):
    with db.SessionLocal() as session:
        state = session.get(ProviderCalendarOAuthStateDB, state_digest)
        verifier = session.get(
            ProviderCalendarOAuthPKCEVerifierDB,
            state_digest,
        )
        if state is None and verifier is None:
            return None
        return state, verifier


def _envelope(snapshot) -> EncryptedCalendarOAuthPKCEVerifier:
    return EncryptedCalendarOAuthPKCEVerifier(
        version=snapshot["verifier_version"],
        key_id=snapshot["verifier_key_id"],
        nonce=snapshot["verifier_nonce"],
        ciphertext=snapshot["verifier_ciphertext"],
    )


def _decrypt_snapshot(snapshot) -> str:
    return CIPHER.decrypt_google_calendar_verifier(
        _envelope(snapshot),
        scope=CalendarOAuthPKCEScope(
            state_digest=snapshot["state_digest"],
            account_id=snapshot["account_id"],
            session_id=snapshot["session_id"],
            provider=snapshot["provider"],
        ),
    )


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
        instructor_id = f"inst_oauth_pkce_{suffix}"
        account_id = f"acct_oauth_pkce_{suffix}"
        session.add(
            InstructorDB(
                id=instructor_id,
                name=f"OAuth PKCE {suffix}",
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
                email_normalized=f"oauth-pkce-{suffix}@example.com",
                password_hash="not-used-by-pkce-state-test",
                is_demo=False,
                session_version=1,
            )
        )
        session.flush()
        session_id = _add_session(account_id, f"owner-{suffix}", session=session)
    return account_id, session_id


def _add_session(
    account_id: str,
    suffix: str,
    *,
    expires_at: datetime.datetime = T0 + datetime.timedelta(hours=1),
    session=None,
) -> str:
    owns_session = session is None
    active_session = session or db.SessionLocal()
    transaction = active_session.begin() if owns_session else None
    try:
        session_id = f"sess_oauth_pkce_{suffix}"
        account_version = active_session.query(
            ProviderAccountDB.session_version
        ).filter_by(id=account_id).one()[0]
        active_session.add(
            ProviderSessionDB(
                id=session_id,
                account_id=account_id,
                token_digest=hashlib.sha256(
                    f"pkce-session-{suffix}".encode()
                ).digest(),
                session_version=account_version,
                created_at=T0 - datetime.timedelta(hours=1),
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
    *,
    browser_binding_digest: bytes | None = None,
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
                browser_binding_digest=browser_binding_digest,
            )
        )


def _digest(raw_state: str) -> bytes:
    return hashlib.sha256(raw_state.encode("ascii")).digest()


def _binding_digest(label: str) -> bytes:
    return hashlib.sha256(f"test-browser-binding:{label}".encode()).digest()


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
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith(
            "/app/calendar_oauth_states.py"
        ):
            repository_locals.extend(current.tb_frame.f_locals.values())
        current = current.tb_next
    rendered_locals = repr(repository_locals)
    for value in forbidden:
        assert value not in str(error)
        assert value not in repr(error)
        assert value not in rendered
        assert value not in rendered_locals
