import base64
import dataclasses
import inspect
import traceback
from dataclasses import replace

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import calendar_oauth_pkce as pkce
from app.calendar_oauth_pkce import (
    CalendarOAuthPKCECipher,
    CalendarOAuthPKCEConfigurationError,
    CalendarOAuthPKCEDecryptionError,
    CalendarOAuthPKCEEncryptionError,
    CalendarOAuthPKCEGenerationError,
    CalendarOAuthPKCEInputError,
    CalendarOAuthPKCEScope,
    EncryptedCalendarOAuthPKCEVerifier,
    derive_s256_code_challenge,
)


ROOT_OLD = b"o" * 32
ROOT_NEW = b"n" * 32
RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
RFC_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
RFC_ENTROPY = base64.urlsafe_b64decode(RFC_VERIFIER + "=")
SCOPE = CalendarOAuthPKCEScope(
    state_digest=b"s" * 32,
    account_id="acct_pkce_001",
    session_id="sess_pkce_001",
)


def _formatted_exception(error: Exception) -> str:
    return "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )


def _cipher(*, active="key-new", root_keys=None):
    return CalendarOAuthPKCECipher(
        active_key_id=active,
        root_keys=root_keys or {"key-new": ROOT_NEW},
    )


def test_exported_surface_and_rfc_vector_never_return_plaintext_verifier(
    monkeypatch,
):
    entropy_sizes = []
    nonce_sizes = []

    def token_bytes(size):
        entropy_sizes.append(size)
        return RFC_ENTROPY

    def urandom(size):
        nonce_sizes.append(size)
        return b"n" * 12

    monkeypatch.setattr(pkce.secrets, "token_bytes", token_bytes)
    monkeypatch.setattr(pkce.os, "urandom", urandom)

    generated = _cipher().generate_google_calendar_pkce(scope=SCOPE)

    assert set(pkce.__all__) == {
        "CalendarOAuthPKCECipher",
        "CalendarOAuthPKCEConfigurationError",
        "CalendarOAuthPKCEDecryptionError",
        "CalendarOAuthPKCEEncryptionError",
        "CalendarOAuthPKCEError",
        "CalendarOAuthPKCEGenerationError",
        "CalendarOAuthPKCEInputError",
        "CalendarOAuthPKCEScope",
        "EncryptedCalendarOAuthPKCEVerifier",
        "GeneratedGoogleCalendarPKCE",
        "derive_s256_code_challenge",
    }
    assert set(
        inspect.signature(
            CalendarOAuthPKCECipher.generate_google_calendar_pkce
        ).parameters
    ) == {"self", "scope"}
    assert {field.name for field in dataclasses.fields(generated)} == {
        "code_challenge",
        "encrypted_verifier",
    }
    assert not hasattr(generated, "verifier")
    assert generated.code_challenge == RFC_CHALLENGE
    assert generated.code_challenge_method == "S256"
    with pytest.raises(
        (dataclasses.FrozenInstanceError, AttributeError, TypeError)
    ):
        generated.code_challenge_method = "plain"

    envelope = generated.encrypted_verifier
    assert envelope.version == 1
    assert envelope.key_id == "key-new"
    assert len(envelope.nonce) == 12
    assert len(envelope.ciphertext) == 59
    assert RFC_VERIFIER.encode("ascii") not in envelope.ciphertext
    assert entropy_sizes == [32]
    assert nonce_sizes == [12]
    assert _cipher().decrypt_google_calendar_verifier(
        envelope,
        scope=SCOPE,
    ) == RFC_VERIFIER


def test_rfc_7636_appendix_b_s256_known_answer():
    assert derive_s256_code_challenge(RFC_VERIFIER) == RFC_CHALLENGE


def test_each_generation_uses_fresh_verifier_challenge_nonce_and_ciphertext():
    protector = _cipher()

    first = protector.generate_google_calendar_pkce(scope=SCOPE)
    second = protector.generate_google_calendar_pkce(scope=SCOPE)

    first_verifier = protector.decrypt_google_calendar_verifier(
        first.encrypted_verifier,
        scope=SCOPE,
    )
    second_verifier = protector.decrypt_google_calendar_verifier(
        second.encrypted_verifier,
        scope=SCOPE,
    )
    assert first_verifier != second_verifier
    assert first.code_challenge != second.code_challenge
    assert first.encrypted_verifier.nonce != second.encrypted_verifier.nonce
    assert (
        first.encrypted_verifier.ciphertext
        != second.encrypted_verifier.ciphertext
    )
    assert len(first_verifier) == len(second_verifier) == 43
    assert "=" not in first_verifier + second_verifier


@pytest.mark.parametrize(
    "verifier",
    [
        None,
        RFC_VERIFIER.encode("ascii"),
        "",
        "A" * 42,
        "A" * 44,
        RFC_VERIFIER + "=",
        RFC_VERIFIER + " ",
        "." * 43,
        "é" * 43,
        RFC_VERIFIER[:-1] + "l",
    ],
)
def test_non_profile_or_noncanonical_verifiers_are_rejected(verifier):
    with pytest.raises(CalendarOAuthPKCEInputError) as raised:
        derive_s256_code_challenge(verifier)

    assert str(raised.value) == "Calendar OAuth PKCE input is invalid."
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True


def test_frozen_hkdf_vector_and_root_key_separation():
    protector = CalendarOAuthPKCECipher(
        active_key_id="key-2026",
        root_keys={"key-2026": b"r" * 32},
    )
    derived = protector._derived_keys["key-2026"]

    assert derived.hex() == (
        "9ec73c2b4f3ba5b20da06f3b5df6e515"
        "e19c913536ce1eb7f45822f9bcc8a1b6"
    )
    assert len(derived) == 32
    assert derived != b"r" * 32

    renamed = CalendarOAuthPKCECipher(
        active_key_id="renamed-2026",
        root_keys={"renamed-2026": b"r" * 32},
    )
    assert renamed._derived_keys["renamed-2026"] != derived


def test_direct_root_key_cannot_decrypt_pkce_ciphertext(monkeypatch):
    monkeypatch.setattr(pkce.secrets, "token_bytes", lambda size: RFC_ENTROPY)
    generated = _cipher().generate_google_calendar_pkce(scope=SCOPE)
    envelope = generated.encrypted_verifier
    aad = pkce._associated_data(
        SCOPE,
        version=envelope.version,
        key_id=envelope.key_id,
    )

    with pytest.raises(InvalidTag):
        AESGCM(ROOT_NEW).decrypt(
            envelope.nonce,
            envelope.ciphertext,
            aad,
        )


@pytest.mark.parametrize(
    ("active_key_id", "root_keys"),
    [
        ("key-new", None),
        ("key-new", {}),
        ("key-new", {"key-old": ROOT_OLD}),
        ("bad key", {"bad key": ROOT_NEW}),
        ("-bad", {"-bad": ROOT_NEW}),
        ("key-new", {"key-new": b"short"}),
        ("key-new", {"key-new": bytearray(32)}),
        (
            "key-new",
            {"key-new": ROOT_NEW, "key-copy": ROOT_NEW},
        ),
        (
            "key-0",
            {f"key-{index}": bytes([index]) * 32 for index in range(9)},
        ),
    ],
)
def test_unsafe_root_key_rings_use_one_cause_free_error(
    active_key_id,
    root_keys,
):
    with pytest.raises(CalendarOAuthPKCEConfigurationError) as raised:
        CalendarOAuthPKCECipher(
            active_key_id=active_key_id,
            root_keys=root_keys,
        )

    assert str(raised.value) == (
        "Calendar OAuth PKCE encryption is not configured safely."
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True


def test_constructor_defensively_derives_and_copies_root_key_ring():
    root_keys = {"key-new": ROOT_NEW}
    protector = _cipher(root_keys=root_keys)
    generated = protector.generate_google_calendar_pkce(scope=SCOPE)

    root_keys["key-new"] = ROOT_OLD
    root_keys.clear()

    assert not hasattr(protector, "_root_keys")
    assert protector.active_key_id == "key-new"
    assert protector.decrypt_google_calendar_verifier(
        generated.encrypted_verifier,
        scope=SCOPE,
    )
    with pytest.raises(TypeError):
        protector._derived_keys["key-new"] = b"x" * 32


def test_key_id_and_root_key_ring_exact_limits_work():
    key_id = "k" + ("x" * 63)
    roots = {
        key_id: b"a" * 32,
        **{
            f"key-{index}": bytes([index]) * 32
            for index in range(1, 8)
        },
    }
    protector = CalendarOAuthPKCECipher(
        active_key_id=key_id,
        root_keys=roots,
    )

    generated = protector.generate_google_calendar_pkce(scope=SCOPE)

    assert generated.encrypted_verifier.key_id == key_id
    assert protector.decrypt_google_calendar_verifier(
        generated.encrypted_verifier,
        scope=SCOPE,
    )


def test_rotation_reads_retained_root_and_writes_only_with_active_key():
    old_cipher = _cipher(active="key-old", root_keys={"key-old": ROOT_OLD})
    old_generated = old_cipher.generate_google_calendar_pkce(scope=SCOPE)
    old_verifier = old_cipher.decrypt_google_calendar_verifier(
        old_generated.encrypted_verifier,
        scope=SCOPE,
    )
    rotated = _cipher(
        active="key-new",
        root_keys={"key-old": ROOT_OLD, "key-new": ROOT_NEW},
    )

    assert rotated.decrypt_google_calendar_verifier(
        old_generated.encrypted_verifier,
        scope=SCOPE,
    ) == old_verifier
    new_generated = rotated.generate_google_calendar_pkce(scope=SCOPE)
    assert new_generated.encrypted_verifier.key_id == "key-new"

    new_only = _cipher(root_keys={"key-new": ROOT_NEW})
    assert new_only.decrypt_google_calendar_verifier(
        new_generated.encrypted_verifier,
        scope=SCOPE,
    )
    with pytest.raises(CalendarOAuthPKCEDecryptionError):
        new_only.decrypt_google_calendar_verifier(
            old_generated.encrypted_verifier,
            scope=SCOPE,
        )


@pytest.mark.parametrize(
    "changed_scope",
    [
        replace(SCOPE, state_digest=b"t" * 32),
        replace(SCOPE, account_id="acct_pkce_002"),
        replace(SCOPE, session_id="sess_pkce_002"),
        replace(SCOPE, provider="outlook"),
    ],
)
def test_state_owner_session_and_provider_are_bound_as_aad(changed_scope):
    protector = _cipher()
    generated = protector.generate_google_calendar_pkce(scope=SCOPE)

    with pytest.raises(CalendarOAuthPKCEDecryptionError):
        protector.decrypt_google_calendar_verifier(
            generated.encrypted_verifier,
            scope=changed_scope,
        )


def test_aad_has_one_frozen_unambiguous_field_order():
    aad = pkce._associated_data(
        SCOPE,
        version=1,
        key_id="key-new",
    )

    assert aad.hex() == (
        "0000001c64617977656176652e63616c656e6461722d6f617574682d706b6365"
        "000000020001"
        "000000076b65792d6e6577"
        "0000002073737373737373737373737373737373737373737373737373737373"
        "73737373"
        "0000000d616363745f706b63655f303031"
        "0000000d736573735f706b63655f303031"
        "00000006676f6f676c65"
    )


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
def test_envelope_version_key_nonce_and_ciphertext_tampering_fail_closed(mutate):
    protector = _cipher(
        root_keys={"key-new": ROOT_NEW, "key-old": ROOT_OLD}
    )
    generated = protector.generate_google_calendar_pkce(scope=SCOPE)

    with pytest.raises(CalendarOAuthPKCEDecryptionError):
        protector.decrypt_google_calendar_verifier(
            mutate(generated.encrypted_verifier),
            scope=SCOPE,
        )


@pytest.mark.parametrize(
    "envelope",
    [
        None,
        EncryptedCalendarOAuthPKCEVerifier(1, "key-new", b"short", b"c" * 59),
        EncryptedCalendarOAuthPKCEVerifier(1, "key-new", b"n" * 12, b"c" * 58),
        EncryptedCalendarOAuthPKCEVerifier(1, "key-new", b"n" * 12, b"c" * 60),
        EncryptedCalendarOAuthPKCEVerifier(1, "bad key", b"n" * 12, b"c" * 59),
        EncryptedCalendarOAuthPKCEVerifier(1, "key-new", bytearray(12), b"c" * 59),
        EncryptedCalendarOAuthPKCEVerifier(1, "key-new", b"n" * 12, bytearray(59)),
        EncryptedCalendarOAuthPKCEVerifier(True, "key-new", b"n" * 12, b"c" * 59),
    ],
)
def test_malformed_envelopes_use_the_generic_decryption_error(envelope):
    protector = _cipher()
    valid = protector.generate_google_calendar_pkce(scope=SCOPE)
    tampered = replace(
        valid.encrypted_verifier,
        ciphertext=valid.encrypted_verifier.ciphertext[:-1]
        + bytes([valid.encrypted_verifier.ciphertext[-1] ^ 1]),
    )
    with pytest.raises(CalendarOAuthPKCEDecryptionError) as reference:
        protector.decrypt_google_calendar_verifier(tampered, scope=SCOPE)
    with pytest.raises(CalendarOAuthPKCEDecryptionError) as raised:
        _cipher().decrypt_google_calendar_verifier(envelope, scope=SCOPE)
    assert (
        type(raised.value),
        str(raised.value),
        repr(raised.value),
        raised.value.__cause__,
        raised.value.__context__,
        raised.value.__suppress_context__,
    ) == (
        type(reference.value),
        str(reference.value),
        repr(reference.value),
        reference.value.__cause__,
        reference.value.__context__,
        reference.value.__suppress_context__,
    )
    assert str(raised.value) == (
        "Calendar OAuth PKCE verifier could not be decrypted."
    )


@pytest.mark.parametrize(
    "plaintext",
    [
        b"." * 43,
        b"\xff" * 43,
        (RFC_VERIFIER[:-1] + "l").encode("ascii"),
    ],
)
def test_authenticated_but_invalid_decrypted_verifier_fails_closed(plaintext):
    protector = _cipher()
    nonce = b"i" * 12
    aad = pkce._associated_data(
        SCOPE,
        version=1,
        key_id="key-new",
    )
    ciphertext = AESGCM(protector._derived_keys["key-new"]).encrypt(
        nonce,
        plaintext,
        aad,
    )
    envelope = EncryptedCalendarOAuthPKCEVerifier(
        version=1,
        key_id="key-new",
        nonce=nonce,
        ciphertext=ciphertext,
    )

    with pytest.raises(CalendarOAuthPKCEDecryptionError):
        protector.decrypt_google_calendar_verifier(envelope, scope=SCOPE)


@pytest.mark.parametrize(
    "scope",
    [
        None,
        replace(SCOPE, state_digest=b"short"),
        replace(SCOPE, state_digest=bytearray(32)),
        replace(SCOPE, account_id=""),
        replace(SCOPE, account_id="   "),
        replace(SCOPE, account_id="x" * 256),
        replace(SCOPE, account_id="é" * 128),
        replace(SCOPE, account_id="\ud800"),
        replace(SCOPE, session_id=""),
        replace(SCOPE, session_id="y" * 256),
        replace(SCOPE, provider="outlook"),
    ],
)
def test_invalid_generation_scope_is_rejected_before_entropy(scope, monkeypatch):
    def unexpected_entropy(_size):
        raise AssertionError("invalid scope must not consume entropy")

    monkeypatch.setattr(pkce.secrets, "token_bytes", unexpected_entropy)
    with pytest.raises(CalendarOAuthPKCEInputError) as raised:
        _cipher().generate_google_calendar_pkce(scope=scope)

    assert str(raised.value) == "Calendar OAuth PKCE input is invalid."


def test_scope_exact_byte_limit_round_trips():
    exact_scope = replace(
        SCOPE,
        account_id="a" * 255,
        session_id="s" * 255,
    )
    protector = _cipher()
    generated = protector.generate_google_calendar_pkce(scope=exact_scope)

    assert protector.decrypt_google_calendar_verifier(
        generated.encrypted_verifier,
        scope=exact_scope,
    )


@pytest.mark.parametrize(
    "entropy_result",
    [
        b"short",
        bytearray(32),
        RuntimeError("verifier-entropy-secret"),
    ],
)
def test_entropy_failures_are_sanitized(monkeypatch, entropy_result):
    def fail_entropy(_size):
        if isinstance(entropy_result, Exception):
            raise entropy_result
        return entropy_result

    monkeypatch.setattr(pkce.secrets, "token_bytes", fail_entropy)
    with pytest.raises(CalendarOAuthPKCEGenerationError) as raised:
        _cipher().generate_google_calendar_pkce(scope=SCOPE)

    assert str(raised.value) == (
        "Calendar OAuth PKCE material could not be generated."
    )
    assert "verifier-entropy-secret" not in _formatted_exception(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "nonce_result",
    [b"short", bytearray(12), RuntimeError("nonce-source-secret")],
)
def test_nonce_failures_are_sanitized(monkeypatch, nonce_result):
    def fail_nonce(_size):
        if isinstance(nonce_result, Exception):
            raise nonce_result
        return nonce_result

    monkeypatch.setattr(pkce.os, "urandom", fail_nonce)
    with pytest.raises(CalendarOAuthPKCEEncryptionError) as raised:
        _cipher().generate_google_calendar_pkce(scope=SCOPE)

    assert str(raised.value) == (
        "Calendar OAuth PKCE verifier could not be encrypted."
    )
    assert "nonce-source-secret" not in _formatted_exception(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize("ciphertext_result", [b"short", RuntimeError("aes-secret")])
def test_aes_encryption_failures_are_sanitized(
    monkeypatch,
    ciphertext_result,
):
    def fail_encrypt(*_args, **_kwargs):
        if isinstance(ciphertext_result, Exception):
            raise ciphertext_result
        return ciphertext_result

    monkeypatch.setattr(pkce.AESGCM, "encrypt", fail_encrypt)
    with pytest.raises(CalendarOAuthPKCEEncryptionError) as raised:
        _cipher().generate_google_calendar_pkce(scope=SCOPE)

    assert "aes-secret" not in _formatted_exception(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_aes_decryption_failure_is_sanitized(monkeypatch):
    generated = _cipher().generate_google_calendar_pkce(scope=SCOPE)

    def fail_decrypt(*_args, **_kwargs):
        raise RuntimeError("aes-decryption-secret")

    monkeypatch.setattr(pkce.AESGCM, "decrypt", fail_decrypt)
    with pytest.raises(CalendarOAuthPKCEDecryptionError) as raised:
        _cipher().decrypt_google_calendar_verifier(
            generated.encrypted_verifier,
            scope=SCOPE,
        )

    assert "aes-decryption-secret" not in _formatted_exception(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_hkdf_failure_is_a_sanitized_configuration_error(monkeypatch):
    def fail_derivation(*_args, **_kwargs):
        raise RuntimeError("root-key-derivation-secret")

    monkeypatch.setattr(pkce, "_derive_pkce_key", fail_derivation)
    with pytest.raises(CalendarOAuthPKCEConfigurationError) as raised:
        _cipher()

    assert "root-key-derivation-secret" not in _formatted_exception(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_unknown_key_invalid_tag_and_wrong_scope_are_indistinguishable():
    protector = _cipher()
    generated = protector.generate_google_calendar_pkce(scope=SCOPE)
    envelope = generated.encrypted_verifier
    failures = [
        (replace(envelope, key_id="retired-key"), SCOPE),
        (
            replace(
                envelope,
                ciphertext=envelope.ciphertext[:-1]
                + bytes([envelope.ciphertext[-1] ^ 1]),
            ),
            SCOPE,
        ),
        (envelope, replace(SCOPE, account_id="acct_pkce_attacker")),
    ]

    rendered = []
    for failed_envelope, failed_scope in failures:
        with pytest.raises(CalendarOAuthPKCEDecryptionError) as raised:
            protector.decrypt_google_calendar_verifier(
                failed_envelope,
                scope=failed_scope,
            )
        rendered.append(
            (
                type(raised.value),
                str(raised.value),
                repr(raised.value),
                raised.value.__cause__,
                raised.value.__context__,
            )
        )
    assert len(set(rendered)) == 1


def test_representations_tracebacks_and_logs_expose_no_sensitive_material(
    caplog,
    monkeypatch,
):
    monkeypatch.setattr(pkce.secrets, "token_bytes", lambda size: RFC_ENTROPY)
    protector = _cipher()
    generated = protector.generate_google_calendar_pkce(scope=SCOPE)
    envelope = generated.encrypted_verifier
    rendered = " ".join(
        (
            repr(protector),
            str(protector),
            repr(SCOPE),
            str(SCOPE),
            repr(generated),
            str(generated),
            repr(envelope),
            str(envelope),
        )
    )
    sensitive_values = (
        RFC_VERIFIER,
        repr(RFC_VERIFIER.encode("ascii")),
        generated.code_challenge,
        ROOT_NEW.hex(),
        repr(ROOT_NEW),
        protector._derived_keys["key-new"].hex(),
        repr(protector._derived_keys["key-new"]),
        SCOPE.state_digest.hex(),
        repr(SCOPE.state_digest),
        SCOPE.account_id,
        SCOPE.session_id,
        envelope.nonce.hex(),
        repr(envelope.nonce),
        envelope.ciphertext.hex(),
        repr(envelope.ciphertext),
    )
    assert all(value not in rendered for value in sensitive_values)

    wrong_key = _cipher(root_keys={"key-new": ROOT_OLD})
    with pytest.raises(CalendarOAuthPKCEDecryptionError) as raised:
        wrong_key.decrypt_google_calendar_verifier(envelope, scope=SCOPE)
    diagnostics = (
        str(raised.value)
        + repr(raised.value)
        + _formatted_exception(raised.value)
        + caplog.text
    )
    assert all(value not in diagnostics for value in sensitive_values)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    assert caplog.text == ""


def test_value_objects_are_frozen_and_slotted():
    generated = _cipher().generate_google_calendar_pkce(scope=SCOPE)

    for value in (SCOPE, generated, generated.encrypted_verifier, _cipher()):
        assert not hasattr(value, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        generated.code_challenge = "changed"
    with pytest.raises(dataclasses.FrozenInstanceError):
        generated.encrypted_verifier.nonce = b"changed"
