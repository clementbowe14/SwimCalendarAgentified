import asyncio
import base64
import json
import traceback
import typing

import pytest

from app import api, calendar_oauth_pkce, config, tools
from app.api import create_app
from app.calendar_credentials import CalendarCredentialScope
from app.calendar_oauth_pkce import (
    CalendarOAuthPKCEConfigurationError,
    CalendarOAuthPKCEDecryptionError,
    CalendarOAuthPKCEScope,
    derive_s256_code_challenge,
)
from app.config import (
    CALENDAR_CREDENTIAL_ACTIVE_KEY_ID_ENV,
    CALENDAR_CREDENTIAL_KEYS_ENV,
    MAX_CALENDAR_CREDENTIAL_KEYS_JSON_BYTES,
    calendar_credential_cipher_from_env,
    calendar_oauth_pkce_cipher_from_env,
)
from app.routing import HaversineRoutingProvider


KEY_A = bytes(range(32))
KEY_B = bytes(range(32, 64))
PKCE_SCOPE = CalendarOAuthPKCEScope(
    state_digest=b"s" * 32,
    account_id="acct_pkce_config",
    session_id="session_pkce_config",
)
CREDENTIAL_SCOPE = CalendarCredentialScope(
    instructor_id="inst_pkce_config",
    connection_id="calendar_connection_pkce_config",
)


def encode_key(key: bytes) -> str:
    return base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii")


ENCODED_KEY_A = encode_key(KEY_A)
ENCODED_KEY_B = encode_key(KEY_B)
VALID_RING = json.dumps({"key-a": ENCODED_KEY_A}, separators=(",", ":"))


@pytest.fixture(autouse=True)
def clear_calendar_root_key_environment(monkeypatch):
    monkeypatch.delenv(CALENDAR_CREDENTIAL_ACTIVE_KEY_ID_ENV, raising=False)
    monkeypatch.delenv(CALENDAR_CREDENTIAL_KEYS_ENV, raising=False)


def configure(monkeypatch, *, active_key_id: str, keys: dict[str, str]) -> None:
    monkeypatch.setenv(CALENDAR_CREDENTIAL_ACTIVE_KEY_ID_ENV, active_key_id)
    monkeypatch.setenv(
        CALENDAR_CREDENTIAL_KEYS_ENV,
        json.dumps(keys, separators=(",", ":")),
    )


def formatted_exception(error: Exception) -> str:
    return "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )


def config_traceback_locals(error: Exception) -> str:
    values = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith("/app/config.py"):
            values.extend(current.tb_frame.f_locals.values())
        current = current.tb_next
    return repr(values)


def test_valid_environment_builds_working_redacted_pkce_cipher(monkeypatch):
    configure(monkeypatch, active_key_id="key-a", keys={"key-a": ENCODED_KEY_A})

    cipher = calendar_oauth_pkce_cipher_from_env()
    generated = cipher.generate_google_calendar_pkce(scope=PKCE_SCOPE)
    verifier = cipher.decrypt_google_calendar_verifier(
        generated.encrypted_verifier,
        scope=PKCE_SCOPE,
    )

    assert cipher.active_key_id == "key-a"
    assert derive_s256_code_challenge(verifier) == generated.code_challenge
    assert generated.code_challenge_method == "S256"
    assert repr(cipher) == "CalendarOAuthPKCECipher(<redacted>)"
    assert ENCODED_KEY_A not in repr(cipher)
    assert KEY_A.hex() not in repr(cipher)


def test_credential_and_pkce_factories_share_one_root_key_contract(monkeypatch):
    configure(monkeypatch, active_key_id="key-a", keys={"key-a": ENCODED_KEY_A})

    credential_cipher = calendar_credential_cipher_from_env()
    pkce_cipher = calendar_oauth_pkce_cipher_from_env()
    credential = credential_cipher.encrypt_refresh_token(
        "opaque-refresh-token",
        scope=CREDENTIAL_SCOPE,
    )
    generated = pkce_cipher.generate_google_calendar_pkce(scope=PKCE_SCOPE)

    assert credential_cipher.decrypt_refresh_token(
        credential,
        scope=CREDENTIAL_SCOPE,
    ) == "opaque-refresh-token"
    assert pkce_cipher.decrypt_google_calendar_verifier(
        generated.encrypted_verifier,
        scope=PKCE_SCOPE,
    )
    assert type(credential_cipher) is not type(pkce_cipher)


def test_factory_rereads_environment_without_caching(monkeypatch):
    configure(monkeypatch, active_key_id="key-a", keys={"key-a": ENCODED_KEY_A})
    first = calendar_oauth_pkce_cipher_from_env()

    configure(monkeypatch, active_key_id="key-b", keys={"key-b": ENCODED_KEY_B})
    second = calendar_oauth_pkce_cipher_from_env()

    assert first is not second
    assert first.active_key_id == "key-a"
    assert second.active_key_id == "key-b"


def test_unchanged_environment_still_builds_fresh_cipher_instances(monkeypatch):
    configure(monkeypatch, active_key_id="key-a", keys={"key-a": ENCODED_KEY_A})

    first = calendar_oauth_pkce_cipher_from_env()
    second = calendar_oauth_pkce_cipher_from_env()

    assert first is not second
    assert first.active_key_id == second.active_key_id == "key-a"


def test_rotation_reads_retained_root_and_writes_with_active_root(monkeypatch):
    configure(monkeypatch, active_key_id="key-a", keys={"key-a": ENCODED_KEY_A})
    old_cipher = calendar_oauth_pkce_cipher_from_env()
    old_generated = old_cipher.generate_google_calendar_pkce(scope=PKCE_SCOPE)

    configure(
        monkeypatch,
        active_key_id="key-b",
        keys={"key-a": ENCODED_KEY_A, "key-b": ENCODED_KEY_B},
    )
    rotated_cipher = calendar_oauth_pkce_cipher_from_env()
    new_generated = rotated_cipher.generate_google_calendar_pkce(scope=PKCE_SCOPE)

    assert rotated_cipher.decrypt_google_calendar_verifier(
        old_generated.encrypted_verifier,
        scope=PKCE_SCOPE,
    )
    assert new_generated.encrypted_verifier.key_id == "key-b"

    configure(monkeypatch, active_key_id="key-b", keys={"key-b": ENCODED_KEY_B})
    new_only_cipher = calendar_oauth_pkce_cipher_from_env()
    expected_new_verifier = rotated_cipher.decrypt_google_calendar_verifier(
        new_generated.encrypted_verifier,
        scope=PKCE_SCOPE,
    )
    assert new_only_cipher.decrypt_google_calendar_verifier(
        new_generated.encrypted_verifier,
        scope=PKCE_SCOPE,
    ) == expected_new_verifier
    with pytest.raises(CalendarOAuthPKCEDecryptionError):
        new_only_cipher.decrypt_google_calendar_verifier(
            old_generated.encrypted_verifier,
            scope=PKCE_SCOPE,
        )


def test_factory_does_not_cache_failure_or_previous_success(monkeypatch):
    monkeypatch.setenv(CALENDAR_CREDENTIAL_ACTIVE_KEY_ID_ENV, "key-a")
    monkeypatch.setenv(CALENDAR_CREDENTIAL_KEYS_ENV, "not-json")
    with pytest.raises(CalendarOAuthPKCEConfigurationError):
        calendar_oauth_pkce_cipher_from_env()

    configure(monkeypatch, active_key_id="key-a", keys={"key-a": ENCODED_KEY_A})
    recovered = calendar_oauth_pkce_cipher_from_env()
    assert recovered.active_key_id == "key-a"

    monkeypatch.delenv(CALENDAR_CREDENTIAL_KEYS_ENV)
    with pytest.raises(CalendarOAuthPKCEConfigurationError):
        calendar_oauth_pkce_cipher_from_env()


def test_public_factory_type_hints_are_runtime_resolvable():
    assert typing.get_type_hints(calendar_oauth_pkce_cipher_from_env) == {}


@pytest.mark.parametrize(
    ("active_key_id", "encoded_key_ring"),
    [
        (None, VALID_RING),
        ("key-a", None),
        ("", VALID_RING),
        ("   ", VALID_RING),
        ("key-a", ""),
        ("key-a", "   "),
        ("key-a", "{"),
        ("key-a", "[]"),
        ("key-a", "{}"),
        ("missing", VALID_RING),
        ("bad key", json.dumps({"bad key": ENCODED_KEY_A})),
        ("key-a", json.dumps({"key-a": ENCODED_KEY_A + "="})),
        (
            "key-a",
            '{"key-a":"' + ENCODED_KEY_A + '","key-a":"' + ENCODED_KEY_B + '"}',
        ),
        (
            "key-a",
            json.dumps({"key-a": ENCODED_KEY_A, "key-copy": ENCODED_KEY_A}),
        ),
        (
            "key-0",
            json.dumps(
                {
                    f"key-{index}": encode_key(bytes([index]) * 32)
                    for index in range(9)
                }
            ),
        ),
    ],
)
def test_malformed_root_key_contract_fails_with_one_pkce_error(
    monkeypatch,
    active_key_id,
    encoded_key_ring,
):
    if active_key_id is not None:
        monkeypatch.setenv(CALENDAR_CREDENTIAL_ACTIVE_KEY_ID_ENV, active_key_id)
    if encoded_key_ring is not None:
        monkeypatch.setenv(CALENDAR_CREDENTIAL_KEYS_ENV, encoded_key_ring)

    with pytest.raises(CalendarOAuthPKCEConfigurationError) as raised:
        calendar_oauth_pkce_cipher_from_env()

    assert str(raised.value) == (
        "Calendar OAuth PKCE encryption is not configured safely."
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True


def test_oversized_ring_is_rejected_before_json_parsing(monkeypatch):
    json_calls = []

    def record_json_call(*_args, **_kwargs):
        json_calls.append(True)
        raise AssertionError("oversized JSON must not be parsed")

    monkeypatch.setenv(CALENDAR_CREDENTIAL_ACTIVE_KEY_ID_ENV, "key-a")
    monkeypatch.setenv(
        CALENDAR_CREDENTIAL_KEYS_ENV,
        "{" + ("x" * MAX_CALENDAR_CREDENTIAL_KEYS_JSON_BYTES),
    )
    monkeypatch.setattr(config.json, "loads", record_json_call)

    with pytest.raises(CalendarOAuthPKCEConfigurationError):
        calendar_oauth_pkce_cipher_from_env()

    assert json_calls == []


def test_constructor_failure_is_sanitized(monkeypatch, caplog):
    secret = "pkce-constructor-secret"
    caplog.set_level("DEBUG")
    configure(monkeypatch, active_key_id="key-a", keys={"key-a": ENCODED_KEY_A})

    def fail_constructor(**_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(
        calendar_oauth_pkce,
        "CalendarOAuthPKCECipher",
        fail_constructor,
    )

    with pytest.raises(CalendarOAuthPKCEConfigurationError) as raised:
        calendar_oauth_pkce_cipher_from_env()

    output = (
        str(raised.value)
        + repr(raised.value)
        + formatted_exception(raised.value)
        + config_traceback_locals(raised.value)
        + caplog.text
    )
    assert secret not in output
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True


def test_configuration_failure_exposes_no_root_key_material(monkeypatch, caplog):
    secret_key_id = "private-pkce-root"
    caplog.set_level("DEBUG")
    raw_key_ring = (
        '{"private-pkce-root":"'
        + ENCODED_KEY_A
        + '","private-pkce-root":"'
        + ENCODED_KEY_B
        + '"}'
    )
    monkeypatch.setenv(CALENDAR_CREDENTIAL_ACTIVE_KEY_ID_ENV, secret_key_id)
    monkeypatch.setenv(CALENDAR_CREDENTIAL_KEYS_ENV, raw_key_ring)

    with pytest.raises(CalendarOAuthPKCEConfigurationError) as raised:
        calendar_oauth_pkce_cipher_from_env()

    output = (
        str(raised.value)
        + repr(raised.value)
        + formatted_exception(raised.value)
        + config_traceback_locals(raised.value)
        + caplog.text
    )
    for secret in (
        secret_key_id,
        ENCODED_KEY_A,
        ENCODED_KEY_B,
        KEY_A.hex(),
        KEY_B.hex(),
        repr(KEY_A),
        repr(KEY_B),
        raw_key_ring,
    ):
        assert secret not in output
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True


@pytest.mark.parametrize("encoded_key_ring", [None, "not-json", '{"key-a":"secret"}'])
def test_pkce_root_key_environment_is_not_activated_at_application_startup(
    monkeypatch,
    encoded_key_ring,
):
    loader_calls = []

    def record_loader_call():
        loader_calls.append(True)
        raise AssertionError("PKCE configuration must remain call-time only")

    monkeypatch.setattr(
        config,
        "_load_calendar_oauth_pkce_cipher_from_env",
        record_loader_call,
    )
    monkeypatch.setenv(CALENDAR_CREDENTIAL_ACTIVE_KEY_ID_ENV, "key-a")
    if encoded_key_ring is not None:
        monkeypatch.setenv(CALENDAR_CREDENTIAL_KEYS_ENV, encoded_key_ring)

    try:
        app = create_app()
        assert loader_calls == []
        assert not hasattr(app.state, "calendar_oauth_pkce_cipher")
        assert not hasattr(app.state.settings, "calendar_oauth_pkce_cipher")

        monkeypatch.setattr(api.db, "init_db", lambda **_kwargs: None)

        async def enter_lifespan() -> None:
            async with app.router.lifespan_context(app):
                pass

        asyncio.run(enter_lifespan())
        assert loader_calls == []
    finally:
        tools.configure_default_routing_provider(HaversineRoutingProvider())
        tools.reset_routing_provider()
