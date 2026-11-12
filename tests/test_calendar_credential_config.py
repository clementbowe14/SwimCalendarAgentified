import base64
import json
import traceback
import typing

import pytest

from app import config, tools
from app.api import create_app
from app.calendar_credentials import (
    CalendarCredentialConfigurationError,
    CalendarCredentialScope,
)
from app.config import (
    CALENDAR_CREDENTIAL_ACTIVE_KEY_ID_ENV,
    CALENDAR_CREDENTIAL_KEYS_ENV,
    MAX_CALENDAR_CREDENTIAL_KEYS_JSON_BYTES,
    calendar_credential_cipher_from_env,
)
from app.routing import HaversineRoutingProvider


KEY_A = bytes(range(32))
KEY_B = bytes(range(32, 64))
SCOPE = CalendarCredentialScope(
    instructor_id="inst_config",
    connection_id="calendar_connection_config",
)


def encode_key(key: bytes) -> str:
    return base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii")


ENCODED_KEY_A = encode_key(KEY_A)
ENCODED_KEY_B = encode_key(KEY_B)
BASE64URL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
LAST_KEY_A_INDEX = BASE64URL_ALPHABET.index(ENCODED_KEY_A[-1])
NONCANONICAL_KEY_A = (
    ENCODED_KEY_A[:-1] + BASE64URL_ALPHABET[LAST_KEY_A_INDEX + 1]
)


@pytest.fixture(autouse=True)
def clear_calendar_credential_environment(monkeypatch):
    monkeypatch.delenv(CALENDAR_CREDENTIAL_ACTIVE_KEY_ID_ENV, raising=False)
    monkeypatch.delenv(CALENDAR_CREDENTIAL_KEYS_ENV, raising=False)


def configure(monkeypatch, *, active_key_id: str, keys: dict[str, str]) -> None:
    monkeypatch.setenv(CALENDAR_CREDENTIAL_ACTIVE_KEY_ID_ENV, active_key_id)
    monkeypatch.setenv(
        CALENDAR_CREDENTIAL_KEYS_ENV,
        json.dumps(keys, separators=(",", ":")),
    )


def formatted_exception(error: Exception) -> str:
    return "".join(traceback.format_exception(type(error), error, error.__traceback__))


def config_traceback_locals(error: Exception) -> str:
    values = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith("/app/config.py"):
            values.extend(current.tb_frame.f_locals.values())
        current = current.tb_next
    return repr(values)


def test_single_key_environment_builds_a_working_redacted_cipher(monkeypatch):
    configure(monkeypatch, active_key_id="key-a", keys={"key-a": ENCODED_KEY_A})

    cipher = calendar_credential_cipher_from_env()
    envelope = cipher.encrypt_refresh_token("opaque-refresh-token", scope=SCOPE)

    assert cipher.active_key_id == "key-a"
    assert cipher.decrypt_refresh_token(envelope, scope=SCOPE) == "opaque-refresh-token"
    assert repr(cipher) == "CalendarCredentialCipher(<redacted>)"
    assert ENCODED_KEY_A not in repr(cipher)
    assert KEY_A.hex() not in repr(cipher)


def test_public_factory_type_hints_are_runtime_resolvable():
    assert typing.get_type_hints(calendar_credential_cipher_from_env) == {}


def test_factory_rereads_environment_and_supports_safe_rotation(monkeypatch):
    keys = {"key-old": ENCODED_KEY_A, "key-new": ENCODED_KEY_B}
    configure(monkeypatch, active_key_id="key-old", keys=keys)
    old_cipher = calendar_credential_cipher_from_env()
    old_envelope = old_cipher.encrypt_refresh_token("rotating-token", scope=SCOPE)

    with monkeypatch.context() as rotated_environment:
        configure(rotated_environment, active_key_id="key-new", keys=keys)
        rotated_cipher = calendar_credential_cipher_from_env()
        rewrapped = rotated_cipher.rewrap_refresh_token(old_envelope, scope=SCOPE)

        assert rotated_cipher is not old_cipher
        assert rotated_cipher.decrypt_refresh_token(old_envelope, scope=SCOPE) == "rotating-token"
        assert rewrapped.key_id == "key-new"

    restored_cipher = calendar_credential_cipher_from_env()
    assert restored_cipher.active_key_id == "key-old"
    assert restored_cipher is not old_cipher


VALID_RING = json.dumps({"key-a": ENCODED_KEY_A}, separators=(",", ":"))


@pytest.mark.parametrize(
    ("active_key_id", "encoded_key_ring"),
    [
        (None, VALID_RING),
        ("key-a", None),
        ("", VALID_RING),
        ("   ", VALID_RING),
        ("bad key", json.dumps({"bad key": ENCODED_KEY_A})),
        ("k" * 65, VALID_RING),
        ("key-a", ""),
        ("key-a", "   "),
        ("key-a", "{"),
        ("key-a", "[]"),
        ("key-a", "null"),
        ("key-a", "true"),
        ("key-a", "{}"),
        ("key-a", '{"key-a":NaN}'),
        ("key-a", '{"key-a":Infinity}'),
        ("key-a", json.dumps({"key-a": None})),
        ("key-a", json.dumps({"key-a": 42})),
        ("key-a", json.dumps({"key-a": [ENCODED_KEY_A]})),
        ("key-a", json.dumps({"key-a": {"nested": ENCODED_KEY_A}})),
        ("missing", VALID_RING),
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
def test_missing_or_malformed_key_rings_fail_with_one_error(
    monkeypatch,
    active_key_id,
    encoded_key_ring,
):
    if active_key_id is not None:
        monkeypatch.setenv(CALENDAR_CREDENTIAL_ACTIVE_KEY_ID_ENV, active_key_id)
    if encoded_key_ring is not None:
        monkeypatch.setenv(CALENDAR_CREDENTIAL_KEYS_ENV, encoded_key_ring)

    with pytest.raises(CalendarCredentialConfigurationError) as raised:
        calendar_credential_cipher_from_env()

    assert str(raised.value) == "Calendar credential encryption is not configured safely."
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "encoded_key",
    [
        ENCODED_KEY_A + "=",
        " " + ENCODED_KEY_A[1:],
        ENCODED_KEY_A[:-1],
        ENCODED_KEY_A + "A",
        "!" * 43,
        base64.b64encode(b"\xfb" * 32).rstrip(b"=").decode("ascii"),
        NONCANONICAL_KEY_A,
    ],
)
def test_noncanonical_or_malformed_base64url_keys_fail_closed(monkeypatch, encoded_key):
    configure(monkeypatch, active_key_id="key-a", keys={"key-a": encoded_key})

    with pytest.raises(CalendarCredentialConfigurationError):
        calendar_credential_cipher_from_env()


def test_exact_canonical_base64url_boundary_is_accepted(monkeypatch):
    assert len(ENCODED_KEY_A) == 43
    configure(monkeypatch, active_key_id="key-a", keys={"key-a": ENCODED_KEY_A})

    cipher = calendar_credential_cipher_from_env()

    assert cipher.active_key_id == "key-a"


def test_oversized_key_ring_is_rejected_before_json_parsing(monkeypatch):
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

    with pytest.raises(CalendarCredentialConfigurationError):
        calendar_credential_cipher_from_env()

    assert json_calls == []


@pytest.mark.parametrize(
    "encoded_key_ring",
    [
        "{" + ("é" * (MAX_CALENDAR_CREDENTIAL_KEYS_JSON_BYTES - 1)),
        '{"key-a":"\ud800"}',
    ],
)
def test_non_ascii_or_malformed_unicode_is_rejected_before_json_parsing(
    monkeypatch,
    encoded_key_ring,
):
    json_calls = []

    def record_json_call(*_args, **_kwargs):
        json_calls.append(True)
        raise AssertionError("non-ASCII key JSON must not be parsed")

    configured_values = {
        CALENDAR_CREDENTIAL_ACTIVE_KEY_ID_ENV: "key-a",
        CALENDAR_CREDENTIAL_KEYS_ENV: encoded_key_ring,
    }
    monkeypatch.setattr(
        config.os,
        "getenv",
        lambda name, default=None: configured_values.get(name, default),
    )
    monkeypatch.setattr(config.json, "loads", record_json_call)

    with pytest.raises(CalendarCredentialConfigurationError):
        calendar_credential_cipher_from_env()

    assert json_calls == []


def test_configuration_failures_expose_no_secret_material(monkeypatch, caplog):
    secret_key_id = "private-key-id"
    raw_key_ring = (
        '{"private-key-id":"'
        + ENCODED_KEY_A
        + '","private-key-id":"'
        + ENCODED_KEY_B
        + '"}'
    )
    monkeypatch.setenv(CALENDAR_CREDENTIAL_ACTIVE_KEY_ID_ENV, secret_key_id)
    monkeypatch.setenv(CALENDAR_CREDENTIAL_KEYS_ENV, raw_key_ring)

    with pytest.raises(CalendarCredentialConfigurationError) as raised:
        calendar_credential_cipher_from_env()

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
        raw_key_ring,
    ):
        assert secret not in output
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True


@pytest.mark.parametrize("encoded_key_ring", [None, "not-json", '{"key-a":"secret"}'])
def test_calendar_key_environment_is_not_activated_at_application_startup(
    monkeypatch,
    encoded_key_ring,
):
    monkeypatch.setenv(CALENDAR_CREDENTIAL_ACTIVE_KEY_ID_ENV, "key-a")
    if encoded_key_ring is not None:
        monkeypatch.setenv(CALENDAR_CREDENTIAL_KEYS_ENV, encoded_key_ring)

    try:
        app = create_app()
        assert not hasattr(app.state, "calendar_credential_cipher")
    finally:
        tools.configure_default_routing_provider(HaversineRoutingProvider())
        tools.reset_routing_provider()
