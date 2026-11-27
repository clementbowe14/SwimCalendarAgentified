import dataclasses
import traceback
import typing

import pytest

from app import config, google_calendar_oauth, tools
from app.api import create_app
from app.config import (
    GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV,
    GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV,
    GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV,
    google_calendar_oauth_configuration_from_env,
)
from app.google_calendar_oauth import (
    GOOGLE_CALENDAR_CALLBACK_PATH,
    GoogleCalendarOAuthConfiguration,
    GoogleCalendarOAuthConfigurationError,
)
from app.routing import HaversineRoutingProvider


CLIENT_ID = "123456789-dayweave.apps.googleusercontent.com"
REDIRECT_URI = (
    "https://api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH
)
DNS_HOSTNAME_253 = ".".join(("a" * 63, "b" * 63, "c" * 63, "d" * 61))
DNS_HOSTNAME_254 = ".".join(("a" * 63, "b" * 63, "c" * 63, "d" * 62))


@pytest.fixture(autouse=True)
def clear_google_calendar_oauth_environment(monkeypatch):
    monkeypatch.delenv(GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV, raising=False)
    monkeypatch.delenv(GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV, raising=False)


def configure(monkeypatch, *, client_id=CLIENT_ID, redirect_uri=REDIRECT_URI):
    monkeypatch.setenv(GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV, client_id)
    monkeypatch.setenv(GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV, redirect_uri)


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


def test_public_oauth_configuration_surface_is_explicit():
    assert google_calendar_oauth.__all__ == (
        "GOOGLE_CALENDAR_AUTHORIZE_PATH",
        "GOOGLE_CALENDAR_CALLBACK_PATH",
        "GoogleCalendarOAuthConfiguration",
        "GoogleCalendarOAuthConfigurationError",
        "GoogleCalendarOAuthError",
        "GoogleCalendarTokenExchangeError",
        "google_calendar_oauth_origin",
    )


@pytest.mark.parametrize(
    "redirect_uri",
    [
        REDIRECT_URI,
        "https://api.dayweave.example:8443" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://api.dayweave.example:1" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://api.dayweave.example:65535" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://" + ("a" * 63) + ".example" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://" + DNS_HOSTNAME_253 + GOOGLE_CALENDAR_CALLBACK_PATH,
        "http://localhost" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "http://localhost:8000" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "http://127.0.0.1" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "http://127.0.0.1:8000" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "http://[::1]" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "http://[::1]:8000" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://localhost:8443" + GOOGLE_CALENDAR_CALLBACK_PATH,
    ],
)
def test_factory_accepts_exact_https_and_loopback_callbacks(monkeypatch, redirect_uri):
    configure(monkeypatch, redirect_uri=redirect_uri)

    configuration = google_calendar_oauth_configuration_from_env()

    assert configuration.client_id == CLIENT_ID
    assert configuration.redirect_uri == redirect_uri
    assert repr(configuration) == "GoogleCalendarOAuthConfiguration(<redacted>)"
    assert str(configuration) == "GoogleCalendarOAuthConfiguration(<redacted>)"
    assert CLIENT_ID not in repr(configuration)
    assert redirect_uri not in repr(configuration)


def test_configuration_is_immutable(monkeypatch):
    configure(monkeypatch)
    configuration = google_calendar_oauth_configuration_from_env()

    with pytest.raises(dataclasses.FrozenInstanceError):
        configuration.client_id = "other.apps.googleusercontent.com"


def test_factory_rereads_environment_without_caching(monkeypatch):
    configure(monkeypatch)
    first = google_calendar_oauth_configuration_from_env()
    second_client_id = "987654321-dayweave.apps.googleusercontent.com"
    second_redirect_uri = (
        "https://calendar.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH
    )
    configure(
        monkeypatch,
        client_id=second_client_id,
        redirect_uri=second_redirect_uri,
    )

    second = google_calendar_oauth_configuration_from_env()

    assert second is not first
    assert second.client_id == second_client_id
    assert second.redirect_uri == second_redirect_uri
    assert first.client_id == CLIENT_ID
    assert first.redirect_uri == REDIRECT_URI


def test_factory_does_not_cache_failure_or_previous_success(monkeypatch):
    configure(monkeypatch, client_id="invalid-client")
    with pytest.raises(GoogleCalendarOAuthConfigurationError):
        google_calendar_oauth_configuration_from_env()

    configure(monkeypatch)
    recovered = google_calendar_oauth_configuration_from_env()
    assert recovered.client_id == CLIENT_ID

    monkeypatch.delenv(GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV)
    with pytest.raises(GoogleCalendarOAuthConfigurationError):
        google_calendar_oauth_configuration_from_env()


def test_public_factory_type_hints_are_runtime_resolvable():
    assert typing.get_type_hints(google_calendar_oauth_configuration_from_env) == {}


@pytest.mark.parametrize(
    ("client_id", "redirect_uri"),
    [
        (None, REDIRECT_URI),
        (CLIENT_ID, None),
        ("", REDIRECT_URI),
        ("   ", REDIRECT_URI),
        (CLIENT_ID, ""),
        (CLIENT_ID, "   "),
    ],
)
def test_missing_or_blank_values_fail_with_one_error(
    monkeypatch,
    client_id,
    redirect_uri,
):
    if client_id is not None:
        monkeypatch.setenv(GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV, client_id)
    if redirect_uri is not None:
        monkeypatch.setenv(GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV, redirect_uri)

    with pytest.raises(GoogleCalendarOAuthConfigurationError) as raised:
        google_calendar_oauth_configuration_from_env()

    assert str(raised.value) == "Google Calendar authorization is not configured safely."
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "client_id",
    [
        "client-id",
        "apps.googleusercontent.com",
        ".apps.googleusercontent.com",
        "client id.apps.googleusercontent.com",
        " client.apps.googleusercontent.com",
        "client.apps.googleusercontent.com ",
        "client\n.apps.googleusercontent.com",
        "client.apps.googleusercontent.example",
        "client.apps.googleusercontent.com.evil.example",
        "clïent.apps.googleusercontent.com",
        ("a" * 490) + ".apps.googleusercontent.com",
    ],
)
def test_malformed_client_identifiers_fail_closed(monkeypatch, client_id):
    configure(monkeypatch, client_id=client_id)

    with pytest.raises(GoogleCalendarOAuthConfigurationError):
        google_calendar_oauth_configuration_from_env()


@pytest.mark.parametrize(
    "redirect_uri",
    [
        GOOGLE_CALENDAR_CALLBACK_PATH,
        "ftp://api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "http://api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://api.dayweave.example/other/callback",
        REDIRECT_URI + "/",
        "https://api.dayweave.example/api/oauth/google-calendar/../callback",
        "https://api.dayweave.example/api/oauth/google-calendar/%2e%2e/callback",
        "https://user@api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://user:password@api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH,
        REDIRECT_URI + "?next=/settings",
        REDIRECT_URI + "?",
        REDIRECT_URI + "#fragment",
        REDIRECT_URI + "#",
        "https://*.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://%61pi.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://api.dayweave.example\\@evil.example" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://internal" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://192.0.2.10" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "http://192.0.2.10" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "http://127.0.0.2" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://127.255.255.255" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://[2001:db8::1]" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://[v1.a]" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://[v1.a]:8443" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://api.dayweave.example:0" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://api.dayweave.example:" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://api.dayweave.example:65536" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://api.dayweave.example:notaport" + GOOGLE_CALENDAR_CALLBACK_PATH,
        " https://api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH,
        REDIRECT_URI + " ",
        "https://api.däyweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH + "\t",
        "https://api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH + "\r\n",
        "https://api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH + "\x7f",
        "https://-api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://" + ("a" * 64) + ".example" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://" + DNS_HOSTNAME_254 + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://api..dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://api.dayweave.example." + GOOGLE_CALENDAR_CALLBACK_PATH,
        "https://" + ("a" * 2048) + ".example" + GOOGLE_CALENDAR_CALLBACK_PATH,
    ],
)
def test_malformed_redirect_uris_fail_closed(monkeypatch, redirect_uri):
    configure(monkeypatch, redirect_uri=redirect_uri)

    with pytest.raises(GoogleCalendarOAuthConfigurationError):
        google_calendar_oauth_configuration_from_env()


@pytest.mark.parametrize(
    ("client_id", "redirect_uri"),
    [
        (42, REDIRECT_URI),
        (CLIENT_ID, 42),
        ([], REDIRECT_URI),
        (CLIENT_ID, {}),
    ],
)
def test_direct_configuration_rejects_wrong_types(client_id, redirect_uri):
    with pytest.raises(GoogleCalendarOAuthConfigurationError):
        GoogleCalendarOAuthConfiguration(
            client_id=client_id,
            redirect_uri=redirect_uri,
        )


def test_direct_configuration_rejects_embedded_nul():
    with pytest.raises(GoogleCalendarOAuthConfigurationError):
        GoogleCalendarOAuthConfiguration(
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI + "\x00",
        )


def test_exact_client_identifier_size_boundary_is_enforced(monkeypatch):
    suffix = ".apps.googleusercontent.com"
    exact = ("a" * (512 - len(suffix))) + suffix
    configure(monkeypatch, client_id=exact)

    configuration = google_calendar_oauth_configuration_from_env()

    assert len(configuration.client_id.encode("ascii")) == 512

    configure(monkeypatch, client_id="a" + exact)
    with pytest.raises(GoogleCalendarOAuthConfigurationError):
        google_calendar_oauth_configuration_from_env()


def test_configuration_failures_expose_no_configured_values(monkeypatch, caplog):
    private_client_id = "private-client-id.apps.googleusercontent.com"
    private_redirect_uri = (
        "https://private-calendar.example/not-the-approved-callback"
    )
    configure(
        monkeypatch,
        client_id=private_client_id,
        redirect_uri=private_redirect_uri,
    )

    with pytest.raises(GoogleCalendarOAuthConfigurationError) as raised:
        google_calendar_oauth_configuration_from_env()

    output = (
        str(raised.value)
        + repr(raised.value)
        + formatted_exception(raised.value)
        + config_traceback_locals(raised.value)
        + caplog.text
    )
    assert private_client_id not in output
    assert private_redirect_uri not in output
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True


def test_unexpected_loader_failures_are_sanitized(monkeypatch, caplog):
    private_client_id = "unexpected-private.apps.googleusercontent.com"
    private_redirect_uri = (
        "https://unexpected-private.example" + GOOGLE_CALENDAR_CALLBACK_PATH
    )
    caplog.set_level("DEBUG")

    def fail_unexpectedly():
        raise RuntimeError(private_client_id + " " + private_redirect_uri)

    monkeypatch.setattr(
        config,
        "_load_google_calendar_oauth_configuration_from_env",
        fail_unexpectedly,
    )

    with pytest.raises(GoogleCalendarOAuthConfigurationError) as raised:
        google_calendar_oauth_configuration_from_env()

    output = (
        str(raised.value)
        + repr(raised.value)
        + formatted_exception(raised.value)
        + config_traceback_locals(raised.value)
        + caplog.text
    )
    assert private_client_id not in output
    assert private_redirect_uri not in output
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True


@pytest.mark.parametrize(
    ("client_id", "redirect_uri"),
    [
        (None, None),
        ("not-a-google-client", "not-a-uri"),
        (CLIENT_ID, REDIRECT_URI),
    ],
)
def test_oauth_environment_is_not_activated_at_application_startup(
    monkeypatch,
    client_id,
    redirect_uri,
):
    if client_id is not None:
        monkeypatch.setenv(GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV, client_id)
    if redirect_uri is not None:
        monkeypatch.setenv(GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV, redirect_uri)

    try:
        app = create_app()
        assert not hasattr(app.state.settings, "google_calendar_oauth_configuration")
        assert not hasattr(app.state, "google_calendar_oauth_configuration")
        assert "google_calendar_oauth" not in app.state.routing_readiness
    finally:
        tools.configure_default_routing_provider(HaversineRoutingProvider())
        tools.reset_routing_provider()


def test_environment_names_are_exact_for_authorization_and_exchange():
    exported_names = {
        name
        for name in vars(config)
        if name.startswith("GOOGLE_CALENDAR_OAUTH_")
    }

    assert exported_names == {
        "GOOGLE_CALENDAR_OAUTH_CLIENT_ID_ENV",
        "GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET_ENV",
        "GOOGLE_CALENDAR_OAUTH_REDIRECT_URI_ENV",
    }
