from __future__ import annotations

import asyncio
import builtins
import json
import os
import subprocess
import sys
import traceback
import typing
from pathlib import Path

import pytest

from app import api, config, google_calendar_token_exchange, tools
from app.api import create_app
from app.config import (
    GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV,
    GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV,
    GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV,
    google_calendar_oauth_configuration_from_env,
    google_calendar_token_exchange_configuration_from_env,
)
from app.google_calendar_authorization import GOOGLE_CALENDAR_FREEBUSY_SCOPE
from app.google_calendar_oauth import GOOGLE_CALENDAR_CALLBACK_PATH
from app.google_calendar_token_exchange import (
    GoogleCalendarTokenExchangeConfiguration,
    GoogleCalendarTokenExchangeError,
    GoogleCalendarTokenTransportResponse,
    exchange_google_calendar_authorization_code,
)
from app.routing import HaversineRoutingProvider


CLIENT_ID = "123456789-dayweave.apps.googleusercontent.com"
REDIRECT_URI = "https://api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH
CLIENT_SECRET = "client-secret-private-value"
AUTHORIZATION_CODE = "4/authorization-code-private-value"
CODE_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
ACCESS_TOKEN = "access-token-private-value"
REFRESH_TOKEN = "refresh-token-private-value"


class RecordingTransport:
    def __init__(self):
        self.calls = []

    def post_form(self, *, url, fields, timeout_seconds):
        self.calls.append((url, fields, timeout_seconds))
        body = json.dumps(
            {
                "access_token": ACCESS_TOKEN,
                "refresh_token": REFRESH_TOKEN,
                "expires_in": 3600,
                "scope": GOOGLE_CALENDAR_FREEBUSY_SCOPE,
                "token_type": "Bearer",
            }
        ).encode("utf-8")
        return GoogleCalendarTokenTransportResponse(
            status_code=200,
            content_type="application/json",
            body=body,
        )


@pytest.fixture(autouse=True)
def clear_google_calendar_exchange_environment(monkeypatch):
    monkeypatch.delenv(GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV, raising=False)
    monkeypatch.delenv(GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV, raising=False)


def configure(
    monkeypatch,
    *,
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    redirect_uri=REDIRECT_URI,
):
    monkeypatch.setenv(GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV, client_id)
    monkeypatch.setenv(
        GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV,
        client_secret,
    )
    monkeypatch.setenv(GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV, redirect_uri)


def exchange(configuration):
    transport = RecordingTransport()
    result = exchange_google_calendar_authorization_code(
        configuration=configuration,
        authorization_code=AUTHORIZATION_CODE,
        code_verifier=CODE_VERIFIER,
        transport=transport,
    )
    return result, transport.calls


def formatted_exception(error):
    return "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )


def config_traceback(error):
    names = []
    values = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith("/app/config.py"):
            names.append(current.tb_frame.f_code.co_name)
            values.extend(current.tb_frame.f_locals.values())
        current = current.tb_next
    return names, repr(values)


def test_exact_environment_contract_builds_usable_redacted_configuration(
    monkeypatch,
):
    configure(monkeypatch)

    configuration = google_calendar_token_exchange_configuration_from_env()
    result, calls = exchange(configuration)

    assert GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV == (
        "SWIM_GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET"
    )
    assert type(configuration) is GoogleCalendarTokenExchangeConfiguration
    assert repr(configuration) == (
        "GoogleCalendarTokenExchangeConfiguration(<redacted>)"
    )
    assert str(configuration) == repr(configuration)
    assert CLIENT_ID not in repr(configuration)
    assert REDIRECT_URI not in repr(configuration)
    assert CLIENT_SECRET not in repr(configuration)
    assert result.refresh_token == REFRESH_TOKEN
    assert len(calls) == 1
    _, fields, timeout_seconds = calls[0]
    assert fields == (
        ("code", AUTHORIZATION_CODE),
        ("client_id", CLIENT_ID),
        ("client_secret", CLIENT_SECRET),
        ("redirect_uri", REDIRECT_URI),
        ("grant_type", "authorization_code"),
        ("code_verifier", CODE_VERIFIER),
    )
    assert timeout_seconds == 5.0


def test_factory_reads_only_the_three_exact_values_in_fixed_order(monkeypatch):
    values = {
        GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV: CLIENT_ID,
        GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV: REDIRECT_URI,
        GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV: CLIENT_SECRET,
    }
    calls = []

    def getenv(name):
        calls.append(name)
        return values.get(name)

    monkeypatch.setattr(config.os, "getenv", getenv)

    configuration = google_calendar_token_exchange_configuration_from_env()

    assert type(configuration) is GoogleCalendarTokenExchangeConfiguration
    assert calls == [
        GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV,
        GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV,
        GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV,
    ]


def test_factory_rereads_and_snapshots_all_values_without_caching(monkeypatch):
    configure(monkeypatch)
    first = google_calendar_token_exchange_configuration_from_env()
    second_id = "987654321-dayweave.apps.googleusercontent.com"
    second_secret = "second-client-secret-private-value"
    second_redirect = (
        "https://calendar.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH
    )
    configure(
        monkeypatch,
        client_id=second_id,
        client_secret=second_secret,
        redirect_uri=second_redirect,
    )

    second = google_calendar_token_exchange_configuration_from_env()
    _, first_calls = exchange(first)
    _, second_calls = exchange(second)

    assert second is not first
    assert dict(first_calls[0][1])["client_id"] == CLIENT_ID
    assert dict(first_calls[0][1])["client_secret"] == CLIENT_SECRET
    assert dict(first_calls[0][1])["redirect_uri"] == REDIRECT_URI
    assert dict(second_calls[0][1])["client_id"] == second_id
    assert dict(second_calls[0][1])["client_secret"] == second_secret
    assert dict(second_calls[0][1])["redirect_uri"] == second_redirect


def test_exact_client_secret_size_limit_is_accepted(monkeypatch):
    exact_secret = "s" * (4 * 1024)
    configure(monkeypatch, client_secret=exact_secret)

    configuration = google_calendar_token_exchange_configuration_from_env()
    _, calls = exchange(configuration)

    assert dict(calls[0][1])["client_secret"] == exact_secret


@pytest.mark.parametrize(
    "client_secret",
    [
        None,
        "",
        " ",
        " leading",
        "trailing ",
        "line\nbreak",
        "tab\tvalue",
        "sëcret",
        "s" * (4 * 1024 + 1),
    ],
)
def test_missing_or_malformed_client_secret_uses_one_fixed_error(
    monkeypatch,
    client_secret,
):
    monkeypatch.setenv(GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV, CLIENT_ID)
    monkeypatch.setenv(GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV, REDIRECT_URI)
    if client_secret is not None:
        monkeypatch.setenv(
            GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV,
            client_secret,
        )

    with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
        google_calendar_token_exchange_configuration_from_env()

    assert str(raised.value) == (
        "Google Calendar authorization could not be completed."
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "secret_value",
    [b"bytes-secret", 42, ["list-secret"], "nul\x00value"],
)
def test_wrong_type_secret_from_environment_boundary_fails_closed(
    monkeypatch,
    secret_value,
):
    values = {
        GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV: CLIENT_ID,
        GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV: REDIRECT_URI,
        GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV: secret_value,
    }
    monkeypatch.setattr(config.os, "getenv", values.get)

    with pytest.raises(GoogleCalendarTokenExchangeError):
        google_calendar_token_exchange_configuration_from_env()


def test_string_subclass_secret_from_environment_boundary_fails_closed(
    monkeypatch,
):
    class SecretSubclass(str):
        pass

    values = {
        GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV: CLIENT_ID,
        GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV: REDIRECT_URI,
        GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV: SecretSubclass(CLIENT_SECRET),
    }
    monkeypatch.setattr(config.os, "getenv", values.get)

    with pytest.raises(GoogleCalendarTokenExchangeError):
        google_calendar_token_exchange_configuration_from_env()


@pytest.mark.parametrize(
    ("client_id", "redirect_uri"),
    [
        (None, REDIRECT_URI),
        ("invalid-client", REDIRECT_URI),
        (CLIENT_ID, None),
        (CLIENT_ID, "https://attacker.example/callback"),
    ],
)
def test_authorization_configuration_failures_are_translated(
    monkeypatch,
    client_id,
    redirect_uri,
):
    if client_id is not None:
        monkeypatch.setenv(GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV, client_id)
    if redirect_uri is not None:
        monkeypatch.setenv(GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV, redirect_uri)
    monkeypatch.setenv(
        GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV,
        CLIENT_SECRET,
    )

    with pytest.raises(GoogleCalendarTokenExchangeError):
        google_calendar_token_exchange_configuration_from_env()


def test_factory_does_not_cache_failure_or_previous_success(monkeypatch):
    configure(monkeypatch, client_secret="invalid\nsecret")
    with pytest.raises(GoogleCalendarTokenExchangeError):
        google_calendar_token_exchange_configuration_from_env()

    configure(monkeypatch)
    recovered = google_calendar_token_exchange_configuration_from_env()
    assert type(recovered) is GoogleCalendarTokenExchangeConfiguration

    monkeypatch.delenv(GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV)
    with pytest.raises(GoogleCalendarTokenExchangeError):
        google_calendar_token_exchange_configuration_from_env()


def test_authorization_only_factory_remains_independent_of_client_secret(
    monkeypatch,
):
    monkeypatch.setenv(GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV, CLIENT_ID)
    monkeypatch.setenv(GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV, REDIRECT_URI)

    authorization = google_calendar_oauth_configuration_from_env()

    assert authorization.client_id == CLIENT_ID
    assert authorization.redirect_uri == REDIRECT_URI
    with pytest.raises(GoogleCalendarTokenExchangeError):
        google_calendar_token_exchange_configuration_from_env()


def test_public_factory_type_hints_are_runtime_resolvable():
    assert typing.get_type_hints(
        google_calendar_token_exchange_configuration_from_env
    ) == {}


def test_secret_values_and_unexpected_loader_failure_never_escape(
    monkeypatch,
    caplog,
):
    private_id = "private-calendar.apps.googleusercontent.com"
    private_redirect = (
        "https://private-calendar.example" + GOOGLE_CALENDAR_CALLBACK_PATH
    )
    private_secret = "private-client-secret-value"
    private_detail = "private-loader-detail"
    caplog.set_level("DEBUG")

    def fail_loader():
        raise RuntimeError(
            " ".join(
                (private_id, private_redirect, private_secret, private_detail)
            )
        )

    monkeypatch.setattr(
        config,
        "_load_google_calendar_token_exchange_configuration_from_env",
        fail_loader,
    )

    with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
        google_calendar_token_exchange_configuration_from_env()

    names, values = config_traceback(raised.value)
    output = " ".join(
        (
            str(raised.value),
            repr(raised.value),
            formatted_exception(raised.value),
            values,
            caplog.text,
        )
    )
    assert names == [
        "google_calendar_token_exchange_configuration_from_env",
        "_raise_google_calendar_token_exchange_error",
    ]
    assert "_load_google_calendar_token_exchange_configuration_from_env" not in names
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    for private in (
        private_id,
        private_redirect,
        private_secret,
        private_detail,
    ):
        assert private not in output


def test_invalid_environment_values_never_escape_diagnostics(monkeypatch, caplog):
    private_id = "private-client.apps.googleusercontent.com"
    private_redirect = (
        "https://private-api.example" + GOOGLE_CALENDAR_CALLBACK_PATH
    )
    private_secret = "private-invalid\nclient-secret"
    caplog.set_level("DEBUG")
    configure(
        monkeypatch,
        client_id=private_id,
        client_secret=private_secret,
        redirect_uri=private_redirect,
    )

    with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
        google_calendar_token_exchange_configuration_from_env()

    names, values = config_traceback(raised.value)
    output = " ".join(
        (
            str(raised.value),
            repr(raised.value),
            formatted_exception(raised.value),
            values,
            caplog.text,
        )
    )
    assert "_load_google_calendar_token_exchange_configuration_from_env" not in names
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    for private in (private_id, private_redirect, private_secret):
        assert private not in output


def test_constructor_failure_is_sanitized(monkeypatch, caplog):
    configure(monkeypatch)
    private_detail = "private-constructor-detail"
    caplog.set_level("DEBUG")

    def fail_constructor(**_kwargs):
        raise RuntimeError(
            " ".join((CLIENT_ID, REDIRECT_URI, CLIENT_SECRET, private_detail))
        )

    monkeypatch.setattr(
        google_calendar_token_exchange,
        "GoogleCalendarTokenExchangeConfiguration",
        fail_constructor,
    )

    with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
        google_calendar_token_exchange_configuration_from_env()

    _, values = config_traceback(raised.value)
    output = " ".join(
        (
            str(raised.value),
            repr(raised.value),
            formatted_exception(raised.value),
            values,
            caplog.text,
        )
    )
    for private in (CLIENT_ID, REDIRECT_URI, CLIENT_SECRET, private_detail):
        assert private not in output


def test_token_module_import_failure_uses_lightweight_fixed_error(
    monkeypatch,
    caplog,
):
    private_detail = "private-token-import-detail"
    caplog.set_level("DEBUG")
    real_import = builtins.__import__

    def fail_token_import(name, *args, **kwargs):
        if name == "app.google_calendar_token_exchange":
            raise ImportError(private_detail)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_token_import)

    with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
        google_calendar_token_exchange_configuration_from_env()

    _, values = config_traceback(raised.value)
    output = " ".join(
        (
            str(raised.value),
            repr(raised.value),
            formatted_exception(raised.value),
            values,
            caplog.text,
        )
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    assert private_detail not in output


def test_secret_bearing_getenv_failure_is_sanitized(monkeypatch, caplog):
    private_detail = "private-getenv-detail"
    caplog.set_level("DEBUG")

    def getenv(name):
        if name == GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV:
            return CLIENT_ID
        if name == GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV:
            return REDIRECT_URI
        if name == GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV:
            raise RuntimeError(private_detail)
        return None

    monkeypatch.setattr(config.os, "getenv", getenv)

    with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
        google_calendar_token_exchange_configuration_from_env()

    _, values = config_traceback(raised.value)
    output = " ".join(
        (
            str(raised.value),
            repr(raised.value),
            formatted_exception(raised.value),
            values,
            caplog.text,
        )
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    assert private_detail not in output


def test_wrong_type_and_subclass_authorization_values_fail_closed(monkeypatch):
    class TextSubclass(str):
        pass

    cases = (
        (42, REDIRECT_URI),
        (TextSubclass(CLIENT_ID), REDIRECT_URI),
        (CLIENT_ID, 42),
        (CLIENT_ID, TextSubclass(REDIRECT_URI)),
    )
    for client_id, redirect_uri in cases:
        values = {
            GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV: client_id,
            GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV: redirect_uri,
            GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV: CLIENT_SECRET,
        }
        monkeypatch.setattr(config.os, "getenv", values.get)
        with pytest.raises(GoogleCalendarTokenExchangeError):
            google_calendar_token_exchange_configuration_from_env()


def test_factory_clears_secret_bearing_ambient_exception_context(monkeypatch):
    configure(monkeypatch, client_secret="invalid\nsecret")
    ambient_secret = "ambient-private-detail"

    try:
        raise RuntimeError(ambient_secret)
    except RuntimeError:
        with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
            google_calendar_token_exchange_configuration_from_env()

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    _, values = config_traceback(raised.value)
    output = " ".join(
        (str(raised.value), repr(raised.value), formatted_exception(raised.value), values)
    )
    assert ambient_secret not in output


@pytest.mark.parametrize("client_secret", [None, "bad\nsecret", CLIENT_SECRET])
def test_exchange_environment_is_not_activated_at_application_startup(
    monkeypatch,
    client_secret,
):
    loader_calls = []

    def record_loader_call():
        loader_calls.append(True)
        raise AssertionError("token exchange configuration must remain call-time only")

    monkeypatch.setattr(
        config,
        "_load_google_calendar_token_exchange_configuration_from_env",
        record_loader_call,
    )
    monkeypatch.setenv(GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV, CLIENT_ID)
    monkeypatch.setenv(GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV, REDIRECT_URI)
    if client_secret is not None:
        monkeypatch.setenv(
            GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV,
            client_secret,
        )

    try:
        app = create_app()
        assert loader_calls == []
        assert not hasattr(
            app.state.settings,
            "google_calendar_token_exchange_configuration",
        )
        assert not hasattr(
            app.state,
            "google_calendar_token_exchange_configuration",
        )
        assert "google_calendar_token_exchange" not in app.state.routing_readiness

        monkeypatch.setattr(api.db, "init_db", lambda **_kwargs: None)

        async def enter_lifespan():
            async with app.router.lifespan_context(app):
                pass

        asyncio.run(enter_lifespan())
        assert loader_calls == []
    finally:
        tools.configure_default_routing_provider(HaversineRoutingProvider())
        tools.reset_routing_provider()


def test_clean_import_and_create_app_never_read_client_secret_environment():
    project_root = Path(__file__).resolve().parents[1]
    child_environment = os.environ.copy()
    child_environment["SWIM_ENV"] = "test"
    child_environment.pop(GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV, None)
    script = "\n".join(
        (
            "import os",
            "reads = []",
            "real_getenv = os.getenv",
            "def record(name, default=None):",
            "    reads.append(name)",
            "    return real_getenv(name, default)",
            "os.getenv = record",
            "import app.config",
            "from app.api import create_app",
            "create_app()",
            (
                "assert 'SWIM_GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET' "
                "not in reads, reads"
            ),
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
