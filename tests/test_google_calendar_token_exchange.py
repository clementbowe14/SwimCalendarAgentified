from __future__ import annotations

import dataclasses
import inspect
import json
import pickle
import traceback

import pytest
from fastapi.encoders import jsonable_encoder

from app import google_calendar_token_exchange as token_exchange
from app.calendar_credentials import (
    CalendarCredentialCipher,
    CalendarCredentialScope,
    MAX_REFRESH_TOKEN_BYTES,
)
from app.calendar_oauth_pkce import derive_s256_code_challenge
from app.google_calendar_authorization import GOOGLE_CALENDAR_FREEBUSY_SCOPE
from app.google_calendar_oauth import (
    GOOGLE_CALENDAR_CALLBACK_PATH,
    GoogleCalendarOAuthConfiguration,
)
from app.google_calendar_token_exchange import (
    GOOGLE_CALENDAR_TOKEN_ENDPOINT,
    GoogleCalendarTokenExchangeConfiguration,
    GoogleCalendarTokenExchangeError,
    GoogleCalendarTokenExchangeResult,
    GoogleCalendarTokenTransportResponse,
    exchange_google_calendar_authorization_code,
)


CLIENT_ID = "123456789-dayweave.apps.googleusercontent.com"
REDIRECT_URI = "https://api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH
CLIENT_SECRET = "client-secret-private-value"
AUTHORIZATION_CODE = "4/authorization-code-private-value"
CODE_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
ACCESS_TOKEN = "access-token-private-value"
REFRESH_TOKEN = "refresh-token-private-value"
PROVIDER_DETAIL = "provider-private-error-detail"
_DEFAULT = object()


class TextSubclass(str):
    pass


class RecordingTransport:
    def __init__(self, response=None, error=None):
        self.response = response or _response()
        self.error = error
        self.calls = []

    def post_form(self, *, url, fields, timeout_seconds):
        self.calls.append((url, fields, timeout_seconds))
        if self.error is not None:
            raise self.error
        return self.response


def _authorization_configuration():
    return GoogleCalendarOAuthConfiguration(
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
    )


def _configuration(*, client_secret=CLIENT_SECRET):
    return GoogleCalendarTokenExchangeConfiguration(
        authorization_configuration=_authorization_configuration(),
        client_secret=client_secret,
    )


def _payload(**overrides):
    payload = {
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "expires_in": 3600,
        "scope": GOOGLE_CALENDAR_FREEBUSY_SCOPE,
        "token_type": "Bearer",
    }
    payload.update(overrides)
    return payload


def _response(*, status_code=200, content_type="application/json", body=None):
    if body is None:
        body = json.dumps(_payload()).encode("utf-8")
    return GoogleCalendarTokenTransportResponse(
        status_code=status_code,
        content_type=content_type,
        body=body,
    )


def _exchange(*, configuration=_DEFAULT, authorization_code=AUTHORIZATION_CODE,
              code_verifier=CODE_VERIFIER, transport=_DEFAULT):
    return exchange_google_calendar_authorization_code(
        configuration=(
            _configuration() if configuration is _DEFAULT else configuration
        ),
        authorization_code=authorization_code,
        code_verifier=code_verifier,
        transport=RecordingTransport() if transport is _DEFAULT else transport,
    )


def _module_traceback(error):
    names = []
    values = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith(
            "/app/google_calendar_token_exchange.py"
        ):
            names.append(current.tb_frame.f_code.co_name)
            values.extend(current.tb_frame.f_locals.values())
        current = current.tb_next
    return names, repr(values)


def test_public_surface_signatures_and_redacted_immutable_values():
    assert token_exchange.__all__ == (
        "GOOGLE_CALENDAR_TOKEN_ENDPOINT",
        "GoogleCalendarTokenExchangeConfiguration",
        "GoogleCalendarTokenExchangeError",
        "GoogleCalendarTokenExchangeResult",
        "GoogleCalendarTokenTransport",
        "GoogleCalendarTokenTransportResponse",
        "exchange_google_calendar_authorization_code",
    )
    signature = inspect.signature(exchange_google_calendar_authorization_code)
    assert list(signature.parameters) == [
        "configuration",
        "authorization_code",
        "code_verifier",
        "transport",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )

    configuration = _configuration()
    response = _response()
    result = _exchange()
    assert not dataclasses.is_dataclass(configuration)
    assert not dataclasses.is_dataclass(response)
    assert not dataclasses.is_dataclass(result)
    assert not hasattr(configuration, "__dict__")
    assert not hasattr(response, "__dict__")
    assert not hasattr(result, "__dict__")
    assert repr(configuration) == (
        "GoogleCalendarTokenExchangeConfiguration(<redacted>)"
    )
    assert str(result) == "GoogleCalendarTokenExchangeResult(<redacted>)"
    assert repr(response) == "GoogleCalendarTokenTransportResponse(<redacted>)"
    for private in (
        CLIENT_ID,
        REDIRECT_URI,
        CLIENT_SECRET,
        ACCESS_TOKEN,
        REFRESH_TOKEN,
    ):
        assert private not in repr(configuration)
        assert private not in repr(response)
        assert private not in repr(result)
    with pytest.raises(AttributeError):
        result.refresh_token = "replacement"
    with pytest.raises(AttributeError):
        result.pkce_code_challenge = "replacement"
    with pytest.raises(AttributeError):
        response.body = b"replacement"
    with pytest.raises(AttributeError):
        configuration._client_secret = "replacement"


def test_secret_values_reject_standard_and_application_serializers():
    values = (_configuration(), _response(), _exchange())

    for value in values:
        with pytest.raises(TypeError):
            dataclasses.asdict(value)
        with pytest.raises(TypeError):
            dataclasses.astuple(value)
        with pytest.raises(TypeError):
            vars(value)
        with pytest.raises(TypeError):
            pickle.dumps(value)
        with pytest.raises(TypeError):
            json.dumps(value)
        with pytest.raises((TypeError, ValueError)):
            jsonable_encoder(value)


def test_secret_value_types_reject_subclassing():
    for base in (
        GoogleCalendarTokenExchangeConfiguration,
        GoogleCalendarTokenTransportResponse,
        GoogleCalendarTokenExchangeResult,
    ):
        with pytest.raises(TypeError):
            type("UnsafeSecretSubclass", (base,), {})


@pytest.mark.parametrize("include_binding", [False, True])
def test_result_cannot_be_forged_through_public_constructor(
    include_binding,
):
    arguments = {"refresh_token": REFRESH_TOKEN}
    if include_binding:
        arguments["pkce_code_challenge"] = derive_s256_code_challenge(
            CODE_VERIFIER
        )

    with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
        GoogleCalendarTokenExchangeResult(**arguments)

    assert str(raised.value) == (
        "Google Calendar authorization could not be completed."
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    names, values = _module_traceback(raised.value)
    assert names == ["__init__", "_raise_token_exchange_error"]
    assert REFRESH_TOKEN not in values


def test_success_posts_one_exact_fixed_request_and_binds_result_to_verifier():
    transport = RecordingTransport()

    result = _exchange(transport=transport)

    assert result.refresh_token == REFRESH_TOKEN
    assert result.pkce_code_challenge == derive_s256_code_challenge(
        CODE_VERIFIER
    )
    assert result.pkce_code_challenge != CODE_VERIFIER
    assert len(transport.calls) == 1
    url, fields, timeout_seconds = transport.calls[0]
    assert url == GOOGLE_CALENDAR_TOKEN_ENDPOINT
    assert url == "https://oauth2.googleapis.com/token"
    assert "?" not in url
    assert fields == (
        ("code", AUTHORIZATION_CODE),
        ("client_id", CLIENT_ID),
        ("client_secret", CLIENT_SECRET),
        ("redirect_uri", REDIRECT_URI),
        ("grant_type", "authorization_code"),
        ("code_verifier", CODE_VERIFIER),
    )
    assert timeout_seconds == 5.0
    assert isinstance(fields, tuple)
    assert all(isinstance(field, tuple) for field in fields)
    assert set(name for name, _ in fields) == {
        "code",
        "client_id",
        "client_secret",
        "redirect_uri",
        "grant_type",
        "code_verifier",
    }
    request_text = repr((url, fields))
    for forbidden in (
        "state",
        "browser",
        "account_id",
        "session_id",
        "scope",
        ACCESS_TOKEN,
        REFRESH_TOKEN,
    ):
        assert forbidden not in request_text


def test_distinct_verifiers_produce_distinct_result_bindings():
    other_verifier = "A" * 43

    first = _exchange(code_verifier=CODE_VERIFIER)
    second = _exchange(code_verifier=other_verifier)

    assert first.pkce_code_challenge == derive_s256_code_challenge(
        CODE_VERIFIER
    )
    assert second.pkce_code_challenge == derive_s256_code_challenge(
        other_verifier
    )
    assert first.pkce_code_challenge != second.pkce_code_challenge


@pytest.mark.parametrize(
    "outcome",
    [CODE_VERIFIER, "A" * 43, None, RuntimeError("private derivation failure")],
)
def test_malformed_pkce_derivation_fails_before_transport(
    monkeypatch,
    outcome,
):
    transport = RecordingTransport()

    def derive(_verifier):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(
        token_exchange,
        "derive_s256_code_challenge",
        derive,
    )

    with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
        _exchange(transport=transport)

    assert transport.calls == []
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    rendered = "".join(traceback.format_exception(raised.value))
    _, values = _module_traceback(raised.value)
    output = " ".join((str(raised.value), rendered, values))
    assert CODE_VERIFIER not in output
    assert "private derivation failure" not in output


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "Application/JSON",
        "application/json; charset=utf-8",
        'application/json; charset="utf-8"',
        " application/json ; charset=UTF-8",
    ],
)
def test_safe_json_content_types_and_unknown_extensions_are_accepted(
    content_type,
):
    body = json.dumps(
        _payload(
            refresh_token_expires_in=31_536_000,
            future_extension={"safe": True},
        )
    ).encode("utf-8")

    result = _exchange(
        transport=RecordingTransport(
            _response(content_type=content_type, body=body)
        )
    )

    assert result.refresh_token == REFRESH_TOKEN


def test_large_finite_json_exponent_is_preserved_without_float_overflow():
    body = (
        b'{"access_token":"' + ACCESS_TOKEN.encode("ascii")
        + b'","refresh_token":"' + REFRESH_TOKEN.encode("ascii")
        + b'","expires_in":3600,"scope":"'
        + GOOGLE_CALENDAR_FREEBUSY_SCOPE.encode("ascii")
        + b'","token_type":"Bearer","future_number":1e400}'
    )

    result = _exchange(
        transport=RecordingTransport(_response(body=body))
    )

    assert result.refresh_token == REFRESH_TOKEN


def test_returned_refresh_token_is_compatible_with_credential_encryption():
    result = _exchange()
    cipher = CalendarCredentialCipher(
        active_key_id="key-current",
        keys={"key-current": b"k" * 32},
    )
    scope = CalendarCredentialScope(
        instructor_id="inst_001",
        connection_id="calconn_" + ("a" * 32),
    )

    envelope = cipher.encrypt_refresh_token(result.refresh_token, scope=scope)

    assert cipher.decrypt_refresh_token(envelope, scope=scope) == REFRESH_TOKEN


@pytest.mark.parametrize(
    "client_secret",
    [
        None,
        b"secret",
        "",
        " ",
        " leading",
        "trailing ",
        "line\nbreak",
        "nul\x00byte",
        "sëcret",
        TextSubclass(CLIENT_SECRET),
        "x" * (4 * 1024 + 1),
    ],
)
def test_invalid_client_secret_is_rejected_by_redacted_configuration(
    client_secret,
):
    with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
        _configuration(client_secret=client_secret)

    assert str(raised.value) == (
        "Google Calendar authorization could not be completed."
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, values = _module_traceback(raised.value)
    assert "_validated_configuration_values" not in names
    if (
        isinstance(client_secret, str)
        and client_secret.strip()
        and len(client_secret) < 100
    ):
        assert client_secret not in values


def test_wrong_mutated_or_subclassed_authorization_configuration_fails():
    class ConfigurationSubclass(GoogleCalendarOAuthConfiguration):
        pass

    supplied = [
        None,
        object(),
        ConfigurationSubclass(client_id=CLIENT_ID, redirect_uri=REDIRECT_URI),
    ]
    mutated = _authorization_configuration()
    object.__setattr__(mutated, "redirect_uri", REDIRECT_URI + "?injected=yes")
    supplied.append(mutated)
    partial = object.__new__(GoogleCalendarOAuthConfiguration)
    object.__setattr__(partial, "client_id", CLIENT_ID)
    supplied.append(partial)

    for authorization_configuration in supplied:
        with pytest.raises(GoogleCalendarTokenExchangeError):
            GoogleCalendarTokenExchangeConfiguration(
                authorization_configuration=authorization_configuration,
                client_secret=CLIENT_SECRET,
            )


@pytest.mark.parametrize(
    "authorization_code",
    [
        None,
        b"code",
        "",
        " ",
        " leading",
        "trailing ",
        "line\nbreak",
        "nul\x00byte",
        "cöde",
        "x" * (8 * 1024 + 1),
    ],
)
def test_invalid_authorization_code_fails_before_transport(authorization_code):
    transport = RecordingTransport()

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _exchange(authorization_code=authorization_code, transport=transport)

    assert transport.calls == []


@pytest.mark.parametrize(
    "code_verifier",
    [
        None,
        b"verifier",
        "",
        "A" * 42,
        "A" * 44,
        CODE_VERIFIER + "=",
        CODE_VERIFIER + " ",
        CODE_VERIFIER[:-1] + "+",
        CODE_VERIFIER[:-1] + "/",
        "é" * 43,
        CODE_VERIFIER[:-1] + "l",
    ],
)
def test_invalid_or_noncanonical_verifier_fails_before_transport(code_verifier):
    transport = RecordingTransport()

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _exchange(code_verifier=code_verifier, transport=transport)

    assert transport.calls == []


def test_string_subclasses_and_invalid_transport_fail_before_call():
    for authorization_code, verifier in (
        (TextSubclass(AUTHORIZATION_CODE), CODE_VERIFIER),
        (AUTHORIZATION_CODE, TextSubclass(CODE_VERIFIER)),
    ):
        transport = RecordingTransport()
        with pytest.raises(GoogleCalendarTokenExchangeError):
            _exchange(
                authorization_code=authorization_code,
                code_verifier=verifier,
                transport=transport,
            )
        assert transport.calls == []

    for transport in (None, object(), lambda: None):
        with pytest.raises(GoogleCalendarTokenExchangeError):
            _exchange(transport=transport)


def test_forcibly_mutated_exchange_configuration_is_revalidated_before_call():
    mutations = (
        ("_client_id", "attacker.example"),
        ("_redirect_uri", REDIRECT_URI + "?next=attacker"),
        ("_client_secret", "line\nbreak"),
        ("_client_secret", 42),
    )
    for field, value in mutations:
        configuration = _configuration()
        object.__setattr__(configuration, field, value)
        transport = RecordingTransport()

        with pytest.raises(GoogleCalendarTokenExchangeError):
            _exchange(configuration=configuration, transport=transport)

        assert transport.calls == []


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(PROVIDER_DETAIL),
        OSError(PROVIDER_DETAIL),
        RuntimeError(PROVIDER_DETAIL),
    ],
)
def test_transport_failures_are_single_attempt_and_cause_free(error):
    transport = RecordingTransport(error=error)

    with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
        _exchange(transport=transport)

    assert len(transport.calls) == 1
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    assert PROVIDER_DETAIL not in str(raised.value)
    assert PROVIDER_DETAIL not in repr(raised.value)


@pytest.mark.parametrize(
    "status_code",
    [100, 199, 201, 204, 301, 302, 307, 308, 400, 401, 403, 408, 429, 500, 503],
)
def test_only_exact_http_200_is_accepted_without_retry(status_code):
    transport = RecordingTransport(_response(status_code=status_code))

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _exchange(transport=transport)

    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "",
        "text/plain",
        "application/problem+json",
        "application/json\r\nX-Injected: yes",
        "application/json\x00",
        "\x01application/json",
        "\x0bapplication/json",
        "\x0capplication/json",
        "\x1fapplication/json",
        "application/json\x7f",
        "application/json; garbage",
        "application/json; charset=latin-1",
        "application/json; charset=utf-8; charset=utf-8",
        "application/json;",
        "application/json;" + ("x" * 300),
        "äpplication/json",
    ],
)
def test_invalid_content_type_is_rejected(content_type):
    with pytest.raises(GoogleCalendarTokenExchangeError):
        _exchange(
            transport=RecordingTransport(
                _response(content_type=content_type)
            )
        )


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"\xff",
        b"\xef\xbb\xbf{}",
        b"{} trailing",
        b"null",
        b"[]",
        b'"value"',
        b'{"expires_in": NaN}',
        b'{"expires_in": Infinity}',
        b'{"access_token":"one","access_token":"two"}',
        b"x" * (64 * 1024 + 1),
    ],
)
def test_malformed_bounded_json_is_rejected(body):
    with pytest.raises(GoogleCalendarTokenExchangeError):
        _exchange(transport=RecordingTransport(_response(body=body)))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("access_token", None),
        ("access_token", ""),
        ("access_token", " "),
        ("access_token", "line\nbreak"),
        ("access_token", "x" * (16 * 1024 + 1)),
        ("refresh_token", None),
        ("refresh_token", ""),
        ("refresh_token", " "),
        ("refresh_token", "line\nbreak"),
        ("refresh_token", "x" * (MAX_REFRESH_TOKEN_BYTES + 1)),
        ("token_type", "bearer"),
        ("token_type", "DPoP"),
        ("token_type", None),
        ("expires_in", None),
        ("expires_in", True),
        ("expires_in", "3600"),
        ("expires_in", 0),
        ("expires_in", -1),
        ("expires_in", 2_147_483_648),
        ("scope", None),
        ("scope", ""),
        ("scope", GOOGLE_CALENDAR_FREEBUSY_SCOPE.upper()),
        ("scope", GOOGLE_CALENDAR_FREEBUSY_SCOPE + " "),
        (
            "scope",
            GOOGLE_CALENDAR_FREEBUSY_SCOPE
            + " https://www.googleapis.com/auth/calendar.readonly",
        ),
        (
            "scope",
            GOOGLE_CALENDAR_FREEBUSY_SCOPE
            + " "
            + GOOGLE_CALENDAR_FREEBUSY_SCOPE,
        ),
    ],
)
def test_malformed_tokens_expiry_or_granted_scope_fail_closed(field, value):
    body = json.dumps(_payload(**{field: value})).encode("utf-8")

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _exchange(transport=RecordingTransport(_response(body=body)))


def test_oauth_error_and_unrequested_identity_token_fail_closed():
    payloads = (
        {
            "error": "invalid_grant",
            "error_description": PROVIDER_DETAIL,
        },
        _payload(id_token="identity-token-private-value"),
    )
    for payload in payloads:
        with pytest.raises(GoogleCalendarTokenExchangeError):
            _exchange(
                transport=RecordingTransport(
                    _response(body=json.dumps(payload).encode("utf-8"))
                )
            )


def test_wrong_or_forcibly_mutated_transport_response_fails_closed():
    class IntSubclass(int):
        pass

    class BytesSubclass(bytes):
        pass

    responses = [
        None,
        object(),
        {},
        b"response",
    ]
    for field, value in (
        ("status_code", True),
        ("status_code", "200"),
        ("status_code", IntSubclass(200)),
        ("content_type", b"application/json"),
        ("content_type", TextSubclass("application/json")),
        ("body", bytearray(_response().body)),
        ("body", BytesSubclass(_response().body)),
    ):
        response = _response()
        object.__setattr__(response, field, value)
        responses.append(response)

    for response in responses:
        transport = RecordingTransport()
        transport.response = response
        with pytest.raises(GoogleCalendarTokenExchangeError):
            _exchange(transport=transport)
        assert len(transport.calls) == 1


def test_transport_and_response_secrets_never_escape_error_diagnostics(caplog):
    caplog.set_level("DEBUG")
    configuration = _configuration()
    provider_body = json.dumps(
        {
            "error": "invalid_grant",
            "error_description": PROVIDER_DETAIL,
            "access_token": ACCESS_TOKEN,
            "refresh_token": REFRESH_TOKEN,
        }
    ).encode("utf-8")
    cases = (
        RecordingTransport(
            error=RuntimeError(
                " ".join(
                    (
                        CLIENT_SECRET,
                        AUTHORIZATION_CODE,
                        CODE_VERIFIER,
                        PROVIDER_DETAIL,
                    )
                )
            )
        ),
        RecordingTransport(_response(body=provider_body)),
    )

    for transport in cases:
        with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
            _exchange(configuration=configuration, transport=transport)

        rendered = "".join(traceback.format_exception(raised.value))
        names, values = _module_traceback(raised.value)
        output = " ".join(
            (
                str(raised.value),
                repr(raised.value),
                rendered,
                values,
                caplog.text,
            )
        )
        assert names == [
            "exchange_google_calendar_authorization_code",
            "_raise_token_exchange_error",
        ]
        assert "_exchange_google_calendar_authorization_code" not in names
        assert "_validated_refresh_token" not in names
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
        assert raised.value.__suppress_context__ is True
        for private in (
            CLIENT_ID,
            REDIRECT_URI,
            CLIENT_SECRET,
            AUTHORIZATION_CODE,
            CODE_VERIFIER,
            ACCESS_TOKEN,
            REFRESH_TOKEN,
            PROVIDER_DETAIL,
        ):
            assert private not in output


def test_parser_failure_is_sanitized_without_private_frames(monkeypatch, caplog):
    caplog.set_level("DEBUG")
    private_parser_detail = "parser-private-detail"

    def fail_parser(*args, **kwargs):
        raise RuntimeError(
            " ".join(
                (
                    private_parser_detail,
                    ACCESS_TOKEN,
                    REFRESH_TOKEN,
                    CLIENT_SECRET,
                )
            )
        )

    monkeypatch.setattr(token_exchange.json, "loads", fail_parser)

    with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
        _exchange()

    rendered = "".join(traceback.format_exception(raised.value))
    names, values = _module_traceback(raised.value)
    output = " ".join(
        (
            str(raised.value),
            repr(raised.value),
            rendered,
            values,
            caplog.text,
        )
    )
    assert names == [
        "exchange_google_calendar_authorization_code",
        "_raise_token_exchange_error",
    ]
    for private in (
        private_parser_detail,
        ACCESS_TOKEN,
        REFRESH_TOKEN,
        CLIENT_SECRET,
    ):
        assert private not in output


@pytest.mark.parametrize(
    "operation",
    [
        lambda: GoogleCalendarTokenExchangeConfiguration(
            authorization_configuration=_authorization_configuration(),
            client_secret="invalid\nsecret",
        ),
        lambda: GoogleCalendarTokenExchangeResult(
            refresh_token=REFRESH_TOKEN,
            pkce_code_challenge=derive_s256_code_challenge(CODE_VERIFIER),
        ),
        lambda: _exchange(authorization_code="invalid\ncode"),
    ],
)
def test_public_failures_clear_secret_bearing_ambient_exception_context(
    operation,
):
    ambient_secret = "ambient-caller-private-detail"
    try:
        raise RuntimeError(ambient_secret)
    except RuntimeError:
        with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
            operation()

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    rendered = "".join(traceback.format_exception(raised.value))
    names, values = _module_traceback(raised.value)
    output = " ".join((str(raised.value), repr(raised.value), rendered, values))
    assert "_raise_token_exchange_error" in names
    assert ambient_secret not in output
