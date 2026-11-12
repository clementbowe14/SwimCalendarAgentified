import traceback
from dataclasses import replace

import pytest

from app.calendar_credentials import (
    AES_GCM_NONCE_BYTES,
    CREDENTIAL_ENVELOPE_VERSION,
    MAX_CREDENTIAL_KEYS,
    MAX_REFRESH_TOKEN_BYTES,
    MAX_SCOPE_COMPONENT_BYTES,
    CalendarCredentialCipher,
    CalendarCredentialConfigurationError,
    CalendarCredentialDecryptionError,
    CalendarCredentialEncryptionError,
    CalendarCredentialInputError,
    CalendarCredentialScope,
    EncryptedCalendarCredential,
)


OLD_KEY = b"o" * 32
NEW_KEY = b"n" * 32
SCOPE = CalendarCredentialScope(
    instructor_id="inst_001",
    connection_id="calendar_connection_001",
)


def formatted_exception(error: Exception) -> str:
    return "".join(traceback.format_exception(type(error), error, error.__traceback__))


def cipher(*, active="key-new", keys=None):
    return CalendarCredentialCipher(
        active_key_id=active,
        keys=keys or {"key-new": NEW_KEY},
    )


def test_unicode_refresh_token_round_trips_in_an_opaque_envelope():
    refresh_token = "  opaque-refresh-令牌-🔒\n"

    envelope = cipher().encrypt_refresh_token(refresh_token, scope=SCOPE)

    assert cipher().decrypt_refresh_token(envelope, scope=SCOPE) == refresh_token
    assert envelope.version == CREDENTIAL_ENVELOPE_VERSION
    assert envelope.key_id == "key-new"
    assert len(envelope.nonce) == AES_GCM_NONCE_BYTES
    assert refresh_token.encode() not in envelope.ciphertext


def test_identical_plaintext_uses_fresh_nonce_and_ciphertext():
    protector = cipher()

    first = protector.encrypt_refresh_token("same-token", scope=SCOPE)
    second = protector.encrypt_refresh_token("same-token", scope=SCOPE)

    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext


@pytest.mark.parametrize(
    "changed_scope",
    [
        replace(SCOPE, instructor_id="inst_002"),
        replace(SCOPE, connection_id="calendar_connection_002"),
        replace(SCOPE, provider="outlook"),
    ],
)
def test_provider_scope_changes_fail_closed(changed_scope):
    envelope = cipher().encrypt_refresh_token("scope-bound-token", scope=SCOPE)

    with pytest.raises(CalendarCredentialDecryptionError):
        cipher().decrypt_refresh_token(envelope, scope=changed_scope)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda envelope: replace(envelope, version=2),
        lambda envelope: replace(envelope, key_id="key-old"),
        lambda envelope: replace(
            envelope,
            nonce=bytes([envelope.nonce[0] ^ 1]) + envelope.nonce[1:],
        ),
        lambda envelope: replace(
            envelope,
            ciphertext=envelope.ciphertext[:-1]
            + bytes([envelope.ciphertext[-1] ^ 1]),
        ),
    ],
)
def test_envelope_tampering_fails_closed(mutate):
    protector = cipher(keys={"key-new": NEW_KEY, "key-old": OLD_KEY})
    envelope = protector.encrypt_refresh_token("tamper-evident-token", scope=SCOPE)

    with pytest.raises(CalendarCredentialDecryptionError):
        protector.decrypt_refresh_token(mutate(envelope), scope=SCOPE)


def test_rotation_reads_retained_key_and_writes_only_with_active_key():
    old_cipher = cipher(active="key-old", keys={"key-old": OLD_KEY})
    old_envelope = old_cipher.encrypt_refresh_token("rotating-token", scope=SCOPE)
    rotated = cipher(
        active="key-new",
        keys={"key-old": OLD_KEY, "key-new": NEW_KEY},
    )

    assert rotated.decrypt_refresh_token(old_envelope, scope=SCOPE) == "rotating-token"
    assert rotated.needs_rewrap(old_envelope, scope=SCOPE) is True

    rewrapped = rotated.rewrap_refresh_token(old_envelope, scope=SCOPE)

    assert rewrapped.key_id == "key-new"
    assert rewrapped.nonce != old_envelope.nonce
    assert rotated.needs_rewrap(rewrapped, scope=SCOPE) is False
    new_only = cipher(active="key-new", keys={"key-new": NEW_KEY})
    assert new_only.decrypt_refresh_token(rewrapped, scope=SCOPE) == "rotating-token"
    with pytest.raises(CalendarCredentialDecryptionError):
        new_only.decrypt_refresh_token(old_envelope, scope=SCOPE)


@pytest.mark.parametrize(
    ("active", "keys"),
    [
        ("key-new", {}),
        ("key-new", {"key-old": OLD_KEY}),
        ("bad key id", {"bad key id": NEW_KEY}),
        ("key-new", {"key-new": b"too-short"}),
        ("key-new", {"key-new": bytearray(32)}),
        ("key-new", {"key-new": NEW_KEY, "key-copy": NEW_KEY}),
        (
            "key-0",
            {f"key-{index}": bytes([index]) * 32 for index in range(MAX_CREDENTIAL_KEYS + 1)},
        ),
    ],
)
def test_unsafe_key_rings_are_rejected_with_one_safe_error(active, keys):
    with pytest.raises(CalendarCredentialConfigurationError) as raised:
        CalendarCredentialCipher(active_key_id=active, keys=keys)

    assert str(raised.value) == "Calendar credential encryption is not configured safely."


def test_caller_mutation_cannot_change_constructed_key_ring():
    keys = {"key-new": NEW_KEY}
    protector = cipher(keys=keys)
    envelope = protector.encrypt_refresh_token("stable-token", scope=SCOPE)

    keys["key-new"] = OLD_KEY
    keys.clear()

    assert protector.decrypt_refresh_token(envelope, scope=SCOPE) == "stable-token"


@pytest.mark.parametrize(
    "refresh_token",
    [
        None,
        "",
        "   ",
        "\ud800",
        "x" * (MAX_REFRESH_TOKEN_BYTES + 1),
        "é" * ((MAX_REFRESH_TOKEN_BYTES // 2) + 1),
    ],
)
def test_invalid_plaintext_is_rejected_before_encryption(refresh_token):
    with pytest.raises(CalendarCredentialInputError):
        cipher().encrypt_refresh_token(refresh_token, scope=SCOPE)


@pytest.mark.parametrize(
    "refresh_token",
    [
        "x" * MAX_REFRESH_TOKEN_BYTES,
        "é" * (MAX_REFRESH_TOKEN_BYTES // 2),
    ],
)
def test_refresh_token_exact_byte_limit_round_trips(refresh_token):
    envelope = cipher().encrypt_refresh_token(refresh_token, scope=SCOPE)

    assert cipher().decrypt_refresh_token(envelope, scope=SCOPE) == refresh_token


def test_oversized_plaintext_is_rejected_before_scanning_or_encoding():
    class OversizedString(str):
        def __len__(self):
            return MAX_REFRESH_TOKEN_BYTES + 1

        def strip(self, *_args, **_kwargs):
            raise AssertionError("oversized input must not be scanned")

        def encode(self, *_args, **_kwargs):
            raise AssertionError("oversized input must not be encoded")

    with pytest.raises(CalendarCredentialInputError):
        cipher().encrypt_refresh_token(OversizedString("x"), scope=SCOPE)


@pytest.mark.parametrize(
    "scope",
    [
        None,
        replace(SCOPE, instructor_id=""),
        replace(SCOPE, connection_id="   "),
        replace(SCOPE, instructor_id="x" * 256),
        replace(SCOPE, provider="outlook"),
    ],
)
def test_invalid_scope_is_rejected_before_encryption(scope):
    with pytest.raises(CalendarCredentialInputError):
        cipher().encrypt_refresh_token("valid-token", scope=scope)


def test_scope_exact_byte_limit_round_trips_and_multibyte_overflow_fails():
    exact_scope = CalendarCredentialScope(
        instructor_id="i" * MAX_SCOPE_COMPONENT_BYTES,
        connection_id="c" * MAX_SCOPE_COMPONENT_BYTES,
    )
    envelope = cipher().encrypt_refresh_token("valid-token", scope=exact_scope)

    assert cipher().decrypt_refresh_token(envelope, scope=exact_scope) == "valid-token"

    oversized_scope = replace(
        SCOPE,
        connection_id="é" * ((MAX_SCOPE_COMPONENT_BYTES // 2) + 1),
    )
    with pytest.raises(CalendarCredentialInputError):
        cipher().encrypt_refresh_token("valid-token", scope=oversized_scope)


@pytest.mark.parametrize("key_id", ["key-new", "key-old"])
def test_rewrap_check_authenticates_active_and_inactive_envelopes(key_id):
    protector = cipher(
        active="key-new",
        keys={"key-new": NEW_KEY, "key-old": OLD_KEY},
    )
    source = cipher(active=key_id, keys={key_id: NEW_KEY if key_id == "key-new" else OLD_KEY})
    envelope = source.encrypt_refresh_token("auth-required", scope=SCOPE)
    tampered = replace(
        envelope,
        ciphertext=envelope.ciphertext[:-1]
        + bytes([envelope.ciphertext[-1] ^ 1]),
    )

    with pytest.raises(CalendarCredentialDecryptionError):
        protector.needs_rewrap(tampered, scope=SCOPE)


def test_unknown_key_and_authentication_failure_are_indistinguishable():
    envelope = cipher().encrypt_refresh_token("provider-secret", scope=SCOPE)
    failures = [
        replace(envelope, key_id="retired-key"),
        replace(
            envelope,
            ciphertext=envelope.ciphertext[:-1]
            + bytes([envelope.ciphertext[-1] ^ 1]),
        ),
    ]

    errors = []
    for failure in failures:
        with pytest.raises(CalendarCredentialDecryptionError) as raised:
            cipher().decrypt_refresh_token(failure, scope=SCOPE)
        errors.append(raised.value)

    assert {str(error) for error in errors} == {
        "Calendar credential could not be decrypted."
    }
    assert all(error.__cause__ is None for error in errors)
    assert all(error.__suppress_context__ is True for error in errors)


@pytest.mark.parametrize(
    "envelope",
    [
        None,
        EncryptedCalendarCredential(1, "key-new", b"short", b"x" * 17),
        EncryptedCalendarCredential(1, "key-new", b"n" * 12, b"short"),
        EncryptedCalendarCredential(1, "bad key", b"n" * 12, b"x" * 17),
        EncryptedCalendarCredential(1, "key-new", bytearray(12), b"x" * 17),
        EncryptedCalendarCredential(True, "key-new", b"n" * 12, b"x" * 17),
    ],
)
def test_malformed_envelopes_fail_with_the_generic_decryption_error(envelope):
    with pytest.raises(CalendarCredentialDecryptionError, match="could not be decrypted"):
        cipher().decrypt_refresh_token(envelope, scope=SCOPE)


def test_representations_errors_tracebacks_and_logs_do_not_expose_secrets(caplog):
    refresh_token = "refresh-token-super-secret"
    key_secret = NEW_KEY.hex()
    protector = cipher()
    envelope = protector.encrypt_refresh_token(refresh_token, scope=SCOPE)

    assert repr(protector) == "CalendarCredentialCipher(<redacted>)"
    assert repr(envelope) == "EncryptedCalendarCredential(<redacted>)"
    assert refresh_token not in repr(protector) + repr(envelope)
    assert key_secret not in repr(protector) + repr(envelope)

    wrong_key = cipher(keys={"key-new": OLD_KEY})
    with pytest.raises(CalendarCredentialDecryptionError) as raised:
        wrong_key.decrypt_refresh_token(envelope, scope=SCOPE)

    error_output = str(raised.value) + formatted_exception(raised.value) + caplog.text
    assert refresh_token not in error_output
    assert key_secret not in error_output
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


def test_crypto_library_encrypt_failure_is_sanitized(monkeypatch):
    secret = "library-error-secret"

    def fail_encrypt(*_args, **_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr("app.calendar_credentials.AESGCM.encrypt", fail_encrypt)
    with pytest.raises(CalendarCredentialEncryptionError) as raised:
        cipher().encrypt_refresh_token("refresh-token", scope=SCOPE)

    assert secret not in formatted_exception(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


@pytest.mark.parametrize("nonce_result", [b"short", RuntimeError("rng-secret")])
def test_nonce_source_failures_are_sanitized(monkeypatch, nonce_result):
    def fail_nonce(_length):
        if isinstance(nonce_result, Exception):
            raise nonce_result
        return nonce_result

    monkeypatch.setattr("app.calendar_credentials.os.urandom", fail_nonce)
    with pytest.raises(CalendarCredentialEncryptionError) as raised:
        cipher().encrypt_refresh_token("refresh-token", scope=SCOPE)

    assert "rng-secret" not in formatted_exception(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
