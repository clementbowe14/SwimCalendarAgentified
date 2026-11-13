from __future__ import annotations

import dataclasses
import datetime
import inspect
import traceback
from urllib.parse import parse_qsl, urlsplit

import pytest

from app import google_calendar_authorization
from app.calendar_oauth_states import (
    IssuedGoogleCalendarOAuthPKCEState,
    IssuedGoogleCalendarOAuthState,
)
from app.google_calendar_authorization import (
    GOOGLE_CALENDAR_AUTHORIZATION_ENDPOINT,
    GOOGLE_CALENDAR_FREEBUSY_SCOPE,
    GoogleCalendarAuthorizationError,
    GoogleCalendarAuthorizationRequest,
    build_google_calendar_authorization_request,
)
from app.google_calendar_oauth import (
    GOOGLE_CALENDAR_CALLBACK_PATH,
    GoogleCalendarOAuthConfiguration,
    GoogleCalendarOAuthError,
)


UTC = datetime.timezone.utc
CLIENT_ID = "123456789-dayweave.apps.googleusercontent.com"
REDIRECT_URI = (
    "https://api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH
)
RAW_STATE = "c3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3M"
CODE_CHALLENGE = "Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M"
EXPIRES_AT = datetime.datetime(2026, 11, 13, 15, 0, tzinfo=UTC)
EXPECTED_PAIRS = [
    ("client_id", CLIENT_ID),
    ("redirect_uri", REDIRECT_URI),
    ("response_type", "code"),
    ("scope", GOOGLE_CALENDAR_FREEBUSY_SCOPE),
    ("access_type", "offline"),
    ("prompt", "consent select_account"),
    ("state", RAW_STATE),
    ("code_challenge", CODE_CHALLENGE),
    ("code_challenge_method", "S256"),
]
EXPECTED_URL = (
    "https://accounts.google.com/o/oauth2/v2/auth?"
    "client_id=123456789-dayweave.apps.googleusercontent.com&"
    "redirect_uri=https%3A%2F%2Fapi.dayweave.example%2Fapi%2Foauth%2F"
    "google-calendar%2Fcallback&response_type=code&"
    "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcalendar.freebusy&"
    "access_type=offline&prompt=consent%20select_account&"
    "state=c3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3M&"
    "code_challenge=Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M&"
    "code_challenge_method=S256"
)


def test_public_surface_signature_and_redacted_result():
    assert google_calendar_authorization.__all__ == (
        "GOOGLE_CALENDAR_AUTHORIZATION_ENDPOINT",
        "GOOGLE_CALENDAR_FREEBUSY_SCOPE",
        "GoogleCalendarAuthorizationError",
        "GoogleCalendarAuthorizationRequest",
        "build_google_calendar_authorization_request",
    )
    signature = inspect.signature(
        build_google_calendar_authorization_request
    )
    assert list(signature.parameters) == ["configuration", "issued_state"]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )
    assert [
        field.name
        for field in dataclasses.fields(GoogleCalendarAuthorizationRequest)
    ] == ["authorization_url"]
    assert issubclass(GoogleCalendarAuthorizationError, GoogleCalendarOAuthError)

    request = _build()
    assert repr(request) == "GoogleCalendarAuthorizationRequest(<redacted>)"
    assert str(request) == "GoogleCalendarAuthorizationRequest(<redacted>)"
    for private in (request.authorization_url, RAW_STATE, CODE_CHALLENGE):
        assert private not in repr(request)
        assert private not in str(request)
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.authorization_url = "https://example.invalid"


@pytest.mark.parametrize(
    "untrusted_url",
    [None, 1, "javascript:alert(1)", "https://attacker.example"],
)
def test_result_cannot_be_forged_through_public_constructor(untrusted_url):
    with pytest.raises(GoogleCalendarAuthorizationError) as raised:
        GoogleCalendarAuthorizationRequest(authorization_url=untrusted_url)

    assert str(raised.value) == (
        "Google Calendar authorization request is invalid."
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_golden_request_has_one_exact_ordered_encoded_allowlist():
    first = _build()
    second = _build()

    assert first.authorization_url == EXPECTED_URL
    assert second.authorization_url == first.authorization_url
    assert GOOGLE_CALENDAR_AUTHORIZATION_ENDPOINT == (
        "https://accounts.google.com/o/oauth2/v2/auth"
    )
    parsed = urlsplit(first.authorization_url)
    assert parsed.scheme == "https"
    assert parsed.hostname == "accounts.google.com"
    assert parsed.port is None
    assert parsed.username is None
    assert parsed.password is None
    assert parsed.path == "/o/oauth2/v2/auth"
    assert parsed.fragment == ""
    assert "+" not in parsed.query

    pairs = parse_qsl(
        parsed.query,
        keep_blank_values=True,
        strict_parsing=True,
    )
    assert pairs == EXPECTED_PAIRS
    names = [name for name, _ in pairs]
    assert len(names) == len(set(names)) == 9
    assert all(value for _, value in pairs)


def test_request_is_offline_least_privilege_without_identity_or_scope_union():
    authorization_url = _build().authorization_url
    pairs = dict(
        parse_qsl(
            urlsplit(authorization_url).query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    )

    assert pairs["scope"] == GOOGLE_CALENDAR_FREEBUSY_SCOPE
    assert pairs["access_type"] == "offline"
    assert pairs["prompt"] == "consent select_account"
    assert pairs["code_challenge_method"] == "S256"
    assert set(pairs) == {name for name, _ in EXPECTED_PAIRS}
    for forbidden_name in (
        "include_granted_scopes",
        "login_hint",
        "hd",
        "authuser",
        "nonce",
        "response_mode",
    ):
        assert forbidden_name not in pairs
    for forbidden_scope in (
        "/auth/calendar.readonly",
        "/auth/calendar.events",
        "openid",
        "email",
        "profile",
    ):
        assert forbidden_scope not in pairs["scope"]
    assert str(EXPIRES_AT) not in authorization_url


@pytest.mark.parametrize(
    "redirect_uri",
    [
        REDIRECT_URI,
        "https://api.dayweave.example:8443" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "http://localhost:8000" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "http://127.0.0.1:8000" + GOOGLE_CALENDAR_CALLBACK_PATH,
        "http://[::1]:8000" + GOOGLE_CALENDAR_CALLBACK_PATH,
    ],
)
def test_valid_redirect_variants_round_trip_once_without_normalization(
    redirect_uri,
):
    configuration = GoogleCalendarOAuthConfiguration(
        client_id=CLIENT_ID,
        redirect_uri=redirect_uri,
    )
    request = _build(configuration=configuration)
    pairs = parse_qsl(
        urlsplit(request.authorization_url).query,
        keep_blank_values=True,
        strict_parsing=True,
    )

    assert pairs.count(("redirect_uri", redirect_uri)) == 1
    assert request.authorization_url.count("redirect_uri=") == 1
    assert redirect_uri not in request.authorization_url


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("raw_state", RAW_STATE[:-1]),
        ("raw_state", RAW_STATE + "A"),
        ("raw_state", RAW_STATE[:-1] + "B"),
        ("raw_state", RAW_STATE[:-1] + "="),
        ("raw_state", RAW_STATE[:-1] + "+"),
        ("raw_state", RAW_STATE[:-1] + "/"),
        ("raw_state", RAW_STATE[:-1] + "é"),
        ("raw_state", RAW_STATE + " "),
        ("raw_state", RAW_STATE + "&prompt=none"),
        ("code_challenge", CODE_CHALLENGE[:-1]),
        ("code_challenge", CODE_CHALLENGE + "A"),
        ("code_challenge", CODE_CHALLENGE[:-1] + "B"),
        ("code_challenge", CODE_CHALLENGE + "#fragment"),
    ],
)
def test_malformed_or_noncanonical_pkce_values_cannot_inject_query(
    field,
    invalid_value,
):
    issued_state = _issued_state()
    object.__setattr__(issued_state, field, invalid_value)

    with pytest.raises(GoogleCalendarAuthorizationError) as raised:
        _build(issued_state=issued_state)

    assert str(raised.value) == (
        "Google Calendar authorization request is invalid."
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "invalid_expiry",
    [
        None,
        "2026-11-13T15:00:00Z",
        EXPIRES_AT.replace(tzinfo=None),
        EXPIRES_AT.astimezone(
            datetime.timezone(datetime.timedelta(hours=1))
        ),
    ],
)
def test_issuance_expiry_must_be_an_exact_utc_instant(invalid_expiry):
    issued_state = _issued_state()
    object.__setattr__(issued_state, "expires_at", invalid_expiry)

    with pytest.raises(GoogleCalendarAuthorizationError):
        _build(issued_state=issued_state)


def test_builder_is_time_free_and_does_not_reject_an_old_utc_expiry():
    issued_state = _issued_state()
    object.__setattr__(
        issued_state,
        "expires_at",
        datetime.datetime(2000, 1, 1, tzinfo=UTC),
    )

    assert _build(issued_state=issued_state).authorization_url == EXPECTED_URL


@pytest.mark.parametrize("wrong_value", [None, {}, object()])
def test_wrong_configuration_and_issuance_types_fail_closed(wrong_value):
    with pytest.raises(GoogleCalendarAuthorizationError):
        build_google_calendar_authorization_request(
            configuration=wrong_value,
            issued_state=_issued_state(),
        )
    with pytest.raises(GoogleCalendarAuthorizationError):
        build_google_calendar_authorization_request(
            configuration=_configuration(),
            issued_state=wrong_value,
        )


def test_bare_legacy_issuance_and_subclasses_are_rejected():
    legacy = IssuedGoogleCalendarOAuthState(
        raw_state=RAW_STATE,
        expires_at=EXPIRES_AT,
    )

    class ConfigurationSubclass(GoogleCalendarOAuthConfiguration):
        pass

    class IssuanceSubclass(IssuedGoogleCalendarOAuthPKCEState):
        pass

    with pytest.raises(GoogleCalendarAuthorizationError):
        _build(issued_state=legacy)
    with pytest.raises(GoogleCalendarAuthorizationError):
        _build(
            configuration=ConfigurationSubclass(
                client_id=CLIENT_ID,
                redirect_uri=REDIRECT_URI,
            )
        )
    with pytest.raises(GoogleCalendarAuthorizationError):
        _build(
            issued_state=IssuanceSubclass(
                raw_state=RAW_STATE,
                expires_at=EXPIRES_AT,
                code_challenge=CODE_CHALLENGE,
            )
        )


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("client_id", CLIENT_ID + "&prompt=none"),
        ("client_id", "private\n.apps.googleusercontent.com"),
        ("redirect_uri", REDIRECT_URI + "?prompt=none"),
        ("redirect_uri", REDIRECT_URI + "#fragment"),
        ("redirect_uri", REDIRECT_URI + "\r\nX-Injected: yes"),
    ],
)
def test_forcibly_mutated_configuration_cannot_inject_parameters(
    field,
    invalid_value,
):
    configuration = _configuration()
    object.__setattr__(configuration, field, invalid_value)

    with pytest.raises(GoogleCalendarAuthorizationError):
        _build(configuration=configuration)


def test_side_effecting_string_subclasses_cannot_substitute_after_validation():
    configuration = _configuration()
    issued_state = _issued_state()

    class MutatingString(str):
        def encode(self, *args, **kwargs):
            object.__setattr__(issued_state, "raw_state", "substituted-state")
            object.__setattr__(
                configuration,
                "redirect_uri",
                "https://attacker.example/callback",
            )
            return super().encode(*args, **kwargs)

    malicious_state = MutatingString(RAW_STATE)
    object.__setattr__(issued_state, "raw_state", malicious_state)

    with pytest.raises(GoogleCalendarAuthorizationError):
        _build(
            configuration=configuration,
            issued_state=issued_state,
        )

    assert issued_state.raw_state is malicious_state
    assert configuration.redirect_uri == REDIRECT_URI


def test_partially_initialized_exact_objects_fail_with_fixed_error():
    configuration = object.__new__(GoogleCalendarOAuthConfiguration)
    object.__setattr__(configuration, "client_id", CLIENT_ID)
    issued_state = object.__new__(IssuedGoogleCalendarOAuthPKCEState)
    object.__setattr__(issued_state, "raw_state", RAW_STATE)

    for supplied_configuration, supplied_issuance in (
        (configuration, _issued_state()),
        (_configuration(), issued_state),
    ):
        with pytest.raises(GoogleCalendarAuthorizationError) as raised:
            build_google_calendar_authorization_request(
                configuration=supplied_configuration,
                issued_state=supplied_issuance,
            )
        assert str(raised.value) == (
            "Google Calendar authorization request is invalid."
        )


def test_encoder_failure_exposes_no_request_material_or_private_frame(
    monkeypatch,
    caplog,
):
    configuration = _configuration()
    issued_state = _issued_state()
    private_detail = "encoder-private-detail"
    caplog.set_level("DEBUG")

    def fail_encoding(*args, **kwargs):
        raise RuntimeError(
            " ".join(
                (
                    private_detail,
                    configuration.client_id,
                    configuration.redirect_uri,
                    issued_state.raw_state,
                    issued_state.code_challenge,
                )
            )
        )

    monkeypatch.setattr(
        google_calendar_authorization,
        "_urlencode",
        fail_encoding,
    )
    with pytest.raises(GoogleCalendarAuthorizationError) as raised:
        build_google_calendar_authorization_request(
            configuration=configuration,
            issued_state=issued_state,
        )

    assert str(raised.value) == (
        "Google Calendar authorization request is invalid."
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    rendered = "".join(traceback.format_exception(raised.value))
    frame_names = []
    frame_locals = []
    current = raised.value.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith(
            "/app/google_calendar_authorization.py"
        ):
            frame_names.append(current.tb_frame.f_code.co_name)
            frame_locals.extend(current.tb_frame.f_locals.values())
        current = current.tb_next
    assert "_build_google_calendar_authorization_request" not in frame_names
    output = " ".join(
        (
            str(raised.value),
            repr(raised.value),
            rendered,
            repr(frame_locals),
            caplog.text,
        )
    )
    for private in (
        private_detail,
        configuration.client_id,
        configuration.redirect_uri,
        issued_state.raw_state,
        issued_state.code_challenge,
    ):
        assert private not in output


def _configuration() -> GoogleCalendarOAuthConfiguration:
    return GoogleCalendarOAuthConfiguration(
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
    )


def _issued_state() -> IssuedGoogleCalendarOAuthPKCEState:
    return IssuedGoogleCalendarOAuthPKCEState(
        raw_state=RAW_STATE,
        expires_at=EXPIRES_AT,
        code_challenge=CODE_CHALLENGE,
    )


def _build(
    *,
    configuration: GoogleCalendarOAuthConfiguration | None = None,
    issued_state: IssuedGoogleCalendarOAuthPKCEState | None = None,
) -> GoogleCalendarAuthorizationRequest:
    return build_google_calendar_authorization_request(
        configuration=configuration or _configuration(),
        issued_state=issued_state or _issued_state(),
    )
