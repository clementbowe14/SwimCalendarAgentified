from __future__ import annotations

import asyncio
import dataclasses
import dis
import inspect
import json
import logging
import pickle
import subprocess
import sys
import traceback

import pytest
from fastapi.encoders import jsonable_encoder

from app import google_calendar_token_exchange as token_exchange
from app.calendar_credentials import MAX_REFRESH_TOKEN_BYTES
from app.google_calendar_authorization import GOOGLE_CALENDAR_FREEBUSY_SCOPE
from app.google_calendar_credential_claim import (
    GoogleCalendarRefreshCredentialClaim,
    _new_claim,
)
from app.google_calendar_credential_handoff import (
    validate_google_calendar_refresh_credential_claim,
)
from app.google_calendar_oauth import (
    GOOGLE_CALENDAR_CALLBACK_PATH,
    GoogleCalendarOAuthConfiguration,
)
from app.google_calendar_token_exchange import (
    GOOGLE_CALENDAR_TOKEN_ENDPOINT,
    GoogleCalendarRefreshAuthorizationRequired,
    GoogleCalendarRefreshTokenExchangeResult,
    GoogleCalendarTokenExchangeConfiguration,
    GoogleCalendarTokenExchangeError,
    GoogleCalendarTokenTransportResponse,
    exchange_google_calendar_refresh_token,
)


CLIENT_ID = "123456789-dayweave.apps.googleusercontent.com"
REDIRECT_URI = "https://api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH
CLIENT_SECRET = "client-secret-private-value"
ACCOUNT_ID = "acct_refresh_exchange_private"
INSTRUCTOR_ID = "inst_refresh_exchange_private"
CONNECTION_ID = "calconn_" + "a" * 32
REFRESH_TOKEN = "refresh-token-private-value"
ACCESS_TOKEN = "access-token-private-value"
PROVIDER_DETAIL = "provider-private-error-detail"
_DEFAULT = object()


class TextSubclass(str):
    pass


class BytesSubclass(bytes):
    pass


class PrivateBaseException(BaseException):
    pass


class HostileSystemExit(SystemExit):
    @property
    def code(self):
        raise RuntimeError("system-exit-getter-private")


class RecordingTransport:
    def __init__(self, response=_DEFAULT, error=None):
        self.response = _response() if response is _DEFAULT else response
        self.error = error
        self.calls = []

    def post_form(self, *, url, fields, timeout_seconds):
        self.calls.append((url, fields, timeout_seconds))
        if self.error is not None:
            raise self.error
        return self.response


def _configuration():
    return GoogleCalendarTokenExchangeConfiguration(
        authorization_configuration=GoogleCalendarOAuthConfiguration(
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
        ),
        client_secret=CLIENT_SECRET,
    )


def _claim(
    *,
    account_id=ACCOUNT_ID,
    instructor_id=INSTRUCTOR_ID,
    connection_id=CONNECTION_ID,
    revision=7,
    refresh_token=REFRESH_TOKEN,
):
    return _new_claim(
        account_id=account_id,
        instructor_id=instructor_id,
        connection_id=connection_id,
        expected_calendar_connection_revision=revision,
        refresh_token=refresh_token,
    )


def _forged_claim(**mutations):
    claim = _claim()
    field_names = {
        "revision": "expected_calendar_connection_revision",
    }
    for name, value in mutations.items():
        object.__setattr__(claim, field_names.get(name, name), value)
    return claim


def _payload(*, scope=GOOGLE_CALENDAR_FREEBUSY_SCOPE, **overrides):
    payload = {
        "access_token": ACCESS_TOKEN,
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    if scope is not _DEFAULT:
        payload["scope"] = scope
    payload.update(overrides)
    return payload


def _response(
    *,
    status_code=200,
    content_type="application/json",
    body=None,
):
    if body is None:
        body = json.dumps(_payload()).encode("utf-8")
    return GoogleCalendarTokenTransportResponse(
        status_code=status_code,
        content_type=content_type,
        body=body,
    )


def _exchange(
    *,
    configuration=_DEFAULT,
    claim=_DEFAULT,
    transport=_DEFAULT,
):
    selected_configuration = (
        _configuration()
        if configuration is _DEFAULT
        else configuration
    )
    selected_claim = _claim() if claim is _DEFAULT else claim
    selected_transport = (
        RecordingTransport() if transport is _DEFAULT else transport
    )
    return exchange_google_calendar_refresh_token(
        configuration=selected_configuration,
        claim=selected_claim,
        transport=selected_transport,
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


def _module_frame_locals(error):
    result = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith(
            "/app/google_calendar_token_exchange.py"
        ):
            result.append(dict(current.tb_frame.f_locals))
        current = current.tb_next
    return result


def _formatted_exception(error):
    return "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )


def test_public_surface_posts_one_exact_request_and_binds_result():
    signature = inspect.signature(exchange_google_calendar_refresh_token)
    assert list(signature.parameters) == [
        "configuration",
        "claim",
        "transport",
    ]
    assert all(
        value.kind is inspect.Parameter.KEYWORD_ONLY
        for value in signature.parameters.values()
    )
    transport = RecordingTransport()

    result = _exchange(transport=transport)

    assert type(result) is GoogleCalendarRefreshTokenExchangeResult
    assert result.account_id == ACCOUNT_ID
    assert result.instructor_id == INSTRUCTOR_ID
    assert result.connection_id == CONNECTION_ID
    assert result.expected_calendar_connection_revision == 7
    assert result.access_token == ACCESS_TOKEN
    assert result.expires_in_seconds == 3600
    assert len(transport.calls) == 1
    url, fields, timeout_seconds = transport.calls[0]
    assert url == GOOGLE_CALENDAR_TOKEN_ENDPOINT
    assert url == "https://oauth2.googleapis.com/token"
    assert "?" not in url
    assert fields == (
        ("client_id", CLIENT_ID),
        ("client_secret", CLIENT_SECRET),
        ("grant_type", "refresh_token"),
        ("refresh_token", REFRESH_TOKEN),
    )
    assert timeout_seconds == 5.0
    assert set(name for name, _ in fields) == {
        "client_id",
        "client_secret",
        "grant_type",
        "refresh_token",
    }
    request_text = repr((url, fields))
    for forbidden in (
        "redirect_uri",
        "code_verifier",
        "authorization_code",
        "scope",
        ACCOUNT_ID,
        INSTRUCTOR_ID,
        CONNECTION_ID,
        ACCESS_TOKEN,
    ):
        assert forbidden not in request_text


def test_result_is_builder_only_immutable_redacted_and_nonserializable():
    result = _exchange()

    assert not dataclasses.is_dataclass(result)
    assert not hasattr(result, "__dict__")
    assert repr(result) == "GoogleCalendarRefreshTokenExchangeResult(<redacted>)"
    assert str(result) == repr(result)
    assert not hasattr(result, "refresh_token")
    for private in (
        ACCOUNT_ID,
        INSTRUCTOR_ID,
        CONNECTION_ID,
        REFRESH_TOKEN,
        ACCESS_TOKEN,
    ):
        assert private not in repr(result)
    with pytest.raises(AttributeError):
        result.access_token = "replacement"
    with pytest.raises(AttributeError):
        del result.account_id
    with pytest.raises(TypeError):
        type("UnsafeResult", (GoogleCalendarRefreshTokenExchangeResult,), {})
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

    with pytest.raises(TypeError) as raised:
        GoogleCalendarRefreshTokenExchangeResult(
            account_id=ACCOUNT_ID,
            instructor_id=INSTRUCTOR_ID,
            connection_id=CONNECTION_ID,
            expected_calendar_connection_revision=7,
            access_token=ACCESS_TOKEN,
            expires_in_seconds=3600,
        )
    names, values = _module_traceback(raised.value)
    assert names == []
    for private in (ACCOUNT_ID, INSTRUCTOR_ID, CONNECTION_ID, ACCESS_TOKEN):
        assert private not in values


def test_claim_validation_returns_a_copied_redacted_snapshot():
    claim = _claim()

    snapshot = validate_google_calendar_refresh_credential_claim(claim)
    object.__setattr__(claim, "refresh_token", "mutated-after-validation")

    assert snapshot.account_id == ACCOUNT_ID
    assert snapshot.instructor_id == INSTRUCTOR_ID
    assert snapshot.connection_id == CONNECTION_ID
    assert snapshot.expected_calendar_connection_revision == 7
    assert snapshot.refresh_token == REFRESH_TOKEN
    assert repr(snapshot) == (
        "GoogleCalendarValidatedRefreshCredentialClaim(<redacted>)"
    )
    assert REFRESH_TOKEN not in repr(snapshot)
    with pytest.raises(AttributeError):
        snapshot.refresh_token = "replacement"


def test_distinct_claims_produce_noninterchangeable_complete_bindings():
    first_transport = RecordingTransport()
    first = _exchange(transport=first_transport)
    second_claim = _claim(
        account_id="acct_refresh_exchange_second",
        instructor_id="inst_refresh_exchange_second",
        connection_id="calconn_" + "b" * 32,
        revision=8,
        refresh_token="second-refresh-token-private",
    )
    second_access_token = "second-access-token-private"
    second_transport = RecordingTransport(
        _response(
            body=json.dumps(
                _payload(access_token=second_access_token)
            ).encode("utf-8")
        )
    )
    second = _exchange(
        claim=second_claim,
        transport=second_transport,
    )

    first_binding = (
        first.account_id,
        first.instructor_id,
        first.connection_id,
        first.expected_calendar_connection_revision,
    )
    second_binding = (
        second.account_id,
        second.instructor_id,
        second.connection_id,
        second.expected_calendar_connection_revision,
    )
    assert first_binding == (ACCOUNT_ID, INSTRUCTOR_ID, CONNECTION_ID, 7)
    assert second_binding == (
        "acct_refresh_exchange_second",
        "inst_refresh_exchange_second",
        "calconn_" + "b" * 32,
        8,
    )
    assert first_binding != second_binding
    assert dict(first_transport.calls[0][1])["refresh_token"] == REFRESH_TOKEN
    assert (
        dict(second_transport.calls[0][1])["refresh_token"]
        == "second-refresh-token-private"
    )
    assert first.access_token == ACCESS_TOKEN
    assert second.access_token == second_access_token


@pytest.mark.parametrize(
    "claim",
    [
        None,
        object(),
        _forged_claim(account_id=""),
        _forged_claim(instructor_id=" inst_refresh_exchange_private"),
        _forged_claim(connection_id="invalid-connection"),
        _forged_claim(revision=0),
        _forged_claim(refresh_token=""),
        _forged_claim(refresh_token=" refresh-token"),
        _forged_claim(refresh_token="refresh\ntoken"),
        _forged_claim(refresh_token="réfresh-token"),
        _forged_claim(refresh_token=TextSubclass(REFRESH_TOKEN)),
        _forged_claim(refresh_token="x" * (MAX_REFRESH_TOKEN_BYTES + 1)),
    ],
)
def test_invalid_or_forged_claims_fail_before_transport(claim):
    transport = RecordingTransport()

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _exchange(claim=claim, transport=transport)

    assert transport.calls == []


def test_mutated_configuration_and_invalid_transport_fail_before_call():
    for field, value in (
        ("_client_id", "attacker.example"),
        ("_redirect_uri", REDIRECT_URI + "?injected=yes"),
        ("_client_secret", "line\nbreak"),
    ):
        configuration = _configuration()
        object.__setattr__(configuration, field, value)
        transport = RecordingTransport()
        with pytest.raises(GoogleCalendarTokenExchangeError):
            _exchange(configuration=configuration, transport=transport)
        assert transport.calls == []

    for transport in (None, object(), lambda: None):
        with pytest.raises(GoogleCalendarTokenExchangeError):
            exchange_google_calendar_refresh_token(
                configuration=_configuration(),
                claim=_claim(),
                transport=transport,
            )


@pytest.mark.parametrize("scope", [_DEFAULT, GOOGLE_CALENDAR_FREEBUSY_SCOPE])
def test_missing_or_exact_freebusy_scope_and_extensions_are_accepted(scope):
    body = json.dumps(
        _payload(
            scope=scope,
            future_extension={"safe": True},
            future_number=10**100,
        )
    ).encode("utf-8")

    result = _exchange(
        transport=RecordingTransport(_response(body=body))
    )

    assert type(result) is GoogleCalendarRefreshTokenExchangeResult
    assert result.access_token == ACCESS_TOKEN


@pytest.mark.parametrize(
    "response",
    [
        _response(status_code=201),
        _response(status_code=500),
        _response(content_type="text/plain"),
        _response(body=b""),
        _response(body=b"\xff"),
        _response(body=b"{} trailing"),
        _response(body=b'{"access_token":"one","access_token":"two"}'),
        _response(body=b'{"future":NaN}'),
        _response(body=b"x" * (64 * 1024 + 1)),
    ],
)
def test_malformed_status_media_type_or_json_fails_closed(response):
    transport = RecordingTransport(response)

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _exchange(transport=transport)

    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        None,
        object(),
        _response(status_code=True),
        _response(content_type=TextSubclass("application/json")),
        _response(body=bytearray(b"{}")),
        _response(body=BytesSubclass(b"{}")),
    ],
)
def test_response_boundary_rejects_nonexact_container_and_member_types(
    response,
):
    transport = RecordingTransport(response)

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _exchange(transport=transport)

    assert transport.calls


@pytest.mark.parametrize(
    "prepared_claim",
    [
        None,
        object(),
        _forged_claim(account_id=TextSubclass(ACCOUNT_ID)),
        _forged_claim(connection_id=TextSubclass(CONNECTION_ID)),
        _forged_claim(revision=True),
        _forged_claim(revision=0),
        _forged_claim(revision=9_223_372_036_854_775_808),
        _forged_claim(refresh_token="swapped-refresh-token-private"),
        _forged_claim(_integrity_tag=b"x" * 32),
    ],
)
def test_completion_revalidates_the_prepared_claim(
    monkeypatch,
    prepared_claim,
):
    fields = (
        ("client_id", CLIENT_ID),
        ("client_secret", CLIENT_SECRET),
        ("grant_type", "refresh_token"),
        ("refresh_token", REFRESH_TOKEN),
    )
    monkeypatch.setattr(
        token_exchange,
        "_prepare_google_calendar_refresh_token_exchange",
        lambda **_kwargs: (
            fields,
            prepared_claim,
            validate_google_calendar_refresh_credential_claim(_claim()),
        ),
    )
    transport = RecordingTransport()

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _exchange(transport=transport)

    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        _response(),
        _response(
            status_code=400,
            body=b'{"error":"invalid_grant"}',
        ),
    ],
)
def test_complete_valid_claim_substitution_during_transport_fails_closed(
    response,
):
    original_claim = _claim()
    replacement_claim = _claim(
        account_id="acct_replacement_private",
        instructor_id="inst_replacement_private",
        connection_id="calconn_" + "b" * 32,
        revision=19,
        refresh_token="replacement-refresh-token-private",
    )

    class SubstitutingTransport(RecordingTransport):
        def post_form(self, *, url, fields, timeout_seconds):
            result = super().post_form(
                url=url,
                fields=fields,
                timeout_seconds=timeout_seconds,
            )
            for field in (
                "account_id",
                "instructor_id",
                "connection_id",
                "expected_calendar_connection_revision",
                "refresh_token",
                "_integrity_tag",
            ):
                object.__setattr__(
                    original_claim,
                    field,
                    getattr(replacement_claim, field),
                )
            return result

    transport = SubstitutingTransport(response)

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _exchange(claim=original_claim, transport=transport)

    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("access_token", None),
        ("access_token", ""),
        ("access_token", "line\nbreak"),
        ("access_token", "x" * (16 * 1024 + 1)),
        ("token_type", "bearer"),
        ("token_type", "DPoP"),
        ("expires_in", True),
        ("expires_in", 0),
        ("expires_in", "3600"),
        ("expires_in", 2_147_483_648),
        ("scope", ""),
        ("scope", GOOGLE_CALENDAR_FREEBUSY_SCOPE + " "),
        (
            "scope",
            GOOGLE_CALENDAR_FREEBUSY_SCOPE
            + " https://www.googleapis.com/auth/calendar.readonly",
        ),
        ("id_token", "identity-token-private"),
        ("refresh_token", "replacement-refresh-private"),
        ("error", "unexpected-error"),
    ],
)
def test_malformed_or_identity_bearing_success_payload_fails(field, value):
    body = json.dumps(_payload(**{field: value})).encode("utf-8")

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _exchange(
            transport=RecordingTransport(_response(body=body))
        )


def test_exact_invalid_grant_is_the_only_authorization_required_state(
    caplog,
):
    caplog.set_level(logging.DEBUG)
    body = json.dumps(
        {
            "error": "invalid_grant",
            "error_description": PROVIDER_DETAIL,
            "error_uri": "https://accounts.google.example/private",
            "error_subtype": "invalid_rapt",
            "expires_in": 0,
            "scope": "",
            "token_type": "",
            "future_extension": True,
        }
    ).encode("utf-8")
    transport = RecordingTransport(
        _response(status_code=400, body=body)
    )

    result = _exchange(transport=transport)

    assert type(result) is GoogleCalendarRefreshAuthorizationRequired
    assert result.account_id == ACCOUNT_ID
    assert result.instructor_id == INSTRUCTOR_ID
    assert result.connection_id == CONNECTION_ID
    assert result.expected_calendar_connection_revision == 7
    assert repr(result) == (
        "GoogleCalendarRefreshAuthorizationRequired(<redacted>)"
    )
    assert not hasattr(result, "__dict__")
    assert not hasattr(result, "refresh_token")
    with pytest.raises(AttributeError):
        result.connection_id = "calconn_" + "d" * 32
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
    with pytest.raises(TypeError):
        GoogleCalendarRefreshAuthorizationRequired(
            account_id=ACCOUNT_ID,
            instructor_id=INSTRUCTOR_ID,
            connection_id=CONNECTION_ID,
            expected_calendar_connection_revision=7,
        )
    assert len(transport.calls) == 1
    assert PROVIDER_DETAIL not in caplog.text
    assert REFRESH_TOKEN not in caplog.text


def test_authorization_required_outcomes_are_claim_bound_and_noninterchangeable():
    response = _response(
        status_code=400,
        body=b'{"error":"invalid_grant"}',
    )
    first = _exchange(transport=RecordingTransport(response))
    second_claim = _claim(
        account_id="acct_refresh_recovery_second",
        instructor_id="inst_refresh_recovery_second",
        connection_id="calconn_" + "c" * 32,
        revision=9,
        refresh_token="second-recovery-refresh-private",
    )
    second = _exchange(
        claim=second_claim,
        transport=RecordingTransport(response),
    )

    first_binding = (
        first.account_id,
        first.instructor_id,
        first.connection_id,
        first.expected_calendar_connection_revision,
    )
    second_binding = (
        second.account_id,
        second.instructor_id,
        second.connection_id,
        second.expected_calendar_connection_revision,
    )
    assert first_binding == (ACCOUNT_ID, INSTRUCTOR_ID, CONNECTION_ID, 7)
    assert second_binding == (
        "acct_refresh_recovery_second",
        "inst_refresh_recovery_second",
        "calconn_" + "c" * 32,
        9,
    )
    assert first_binding != second_binding


@pytest.mark.parametrize(
    "response",
    [
        _response(
            status_code=401,
            body=b'{"error":"invalid_grant"}',
        ),
        _response(
            status_code=400,
            body=b'{"error":"invalid_client"}',
        ),
        _response(
            status_code=400,
            body=b'{"error":"invalid_grant","error":"invalid_client"}',
        ),
        _response(
            status_code=400,
            body=b'{"error":"invalid_grant","access_token":"private"}',
        ),
        _response(
            status_code=400,
            body=b'{"error":"invalid_grant","refresh_token":"private"}',
        ),
        _response(
            status_code=400,
            body=b'{"error":"invalid_grant","id_token":"private"}',
        ),
        _response(
            status_code=400,
            content_type="text/plain",
            body=b'{"error":"invalid_grant"}',
        ),
        _response(
            status_code=400,
            body=b"x" * (64 * 1024 + 1),
        ),
    ],
)
def test_other_or_malformed_provider_errors_remain_generic(response):
    with pytest.raises(GoogleCalendarTokenExchangeError):
        _exchange(transport=RecordingTransport(response))


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(PROVIDER_DETAIL),
        OSError(PROVIDER_DETAIL),
        RuntimeError(PROVIDER_DETAIL),
    ],
)
def test_transport_failures_are_single_attempt_and_secret_free(
    error,
    caplog,
):
    caplog.set_level(logging.DEBUG)
    transport = RecordingTransport(error=error)

    with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
        _exchange(transport=transport)

    assert len(transport.calls) == 1
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, frame_values = _module_traceback(raised.value)
    assert names == [
        "exchange_google_calendar_refresh_token",
        "_raise_token_exchange_error",
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
    for private in (
        CLIENT_ID,
        REDIRECT_URI,
        CLIENT_SECRET,
        ACCOUNT_ID,
        INSTRUCTOR_ID,
        CONNECTION_ID,
        REFRESH_TOKEN,
        ACCESS_TOKEN,
        PROVIDER_DETAIL,
    ):
        assert private not in output


def test_transport_cancellation_is_preserved_and_sanitized(caplog):
    caplog.set_level(logging.DEBUG)
    transport = RecordingTransport(
        error=asyncio.CancelledError(PROVIDER_DETAIL)
    )

    with pytest.raises(asyncio.CancelledError) as raised:
        _exchange(transport=transport)

    assert len(transport.calls) == 1
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, frame_values = _module_traceback(raised.value)
    assert names == [
        "exchange_google_calendar_refresh_token",
        "_raise_control_exception",
    ]
    for frame_locals in _module_frame_locals(raised.value):
        assert all(
            not isinstance(
                value,
                (
                    GoogleCalendarTokenExchangeConfiguration,
                    GoogleCalendarRefreshCredentialClaim,
                ),
            )
            for value in frame_locals.values()
        )
    output = " ".join(
        (
            repr(raised.value),
            _formatted_exception(raised.value),
            frame_values,
            caplog.text,
        )
    )
    for private in (
        CLIENT_SECRET,
        REFRESH_TOKEN,
        ACCOUNT_ID,
        CONNECTION_ID,
        PROVIDER_DETAIL,
    ):
        assert private not in output


@pytest.mark.parametrize(
    (
        "error",
        "expected_type",
        "expected_helper",
        "expected_exit_code",
    ),
    [
        (
            PrivateBaseException(PROVIDER_DETAIL),
            GoogleCalendarTokenExchangeError,
            "_raise_token_exchange_error",
            None,
        ),
        (
            KeyboardInterrupt(PROVIDER_DETAIL),
            KeyboardInterrupt,
            "_raise_control_exception",
            None,
        ),
        (
            SystemExit(7),
            SystemExit,
            "_raise_control_exception",
            7,
        ),
        (
            SystemExit(PROVIDER_DETAIL),
            SystemExit,
            "_raise_control_exception",
            1,
        ),
        (
            HostileSystemExit(PROVIDER_DETAIL),
            SystemExit,
            "_raise_control_exception",
            1,
        ),
        (
            GeneratorExit(PROVIDER_DETAIL),
            GeneratorExit,
            "_raise_control_exception",
            None,
        ),
    ],
)
def test_non_exception_control_paths_cannot_leak_transport_secrets(
    error,
    expected_type,
    expected_helper,
    expected_exit_code,
    caplog,
):
    caplog.set_level(logging.DEBUG)
    transport = RecordingTransport(error=error)

    with pytest.raises(expected_type) as raised:
        _exchange(transport=transport)

    assert len(transport.calls) == 1
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    if expected_type is SystemExit:
        assert raised.value.code == expected_exit_code
    names, frame_values = _module_traceback(raised.value)
    assert names == [
        "exchange_google_calendar_refresh_token",
        expected_helper,
    ]
    for frame_locals in _module_frame_locals(raised.value):
        assert all(
            not isinstance(
                value,
                (
                    GoogleCalendarTokenExchangeConfiguration,
                    GoogleCalendarRefreshCredentialClaim,
                ),
            )
            for value in frame_locals.values()
        )
    output = " ".join(
        (
            repr(raised.value),
            _formatted_exception(raised.value),
            frame_values,
            caplog.text,
        )
    )
    for private in (
        CLIENT_SECRET,
        REFRESH_TOKEN,
        ACCOUNT_ID,
        CONNECTION_ID,
        PROVIDER_DETAIL,
    ):
        assert private not in output


def test_refresh_exchange_entry_interruption_is_sanitized(caplog):
    caplog.set_level(logging.DEBUG)
    guarded = exchange_google_calendar_refresh_token
    source_lines, first_line = inspect.getsourcelines(guarded)
    target_line = first_line + next(
        offset
        for offset, line in enumerate(source_lines)
        if line.lstrip().startswith("try: return")
    )
    interrupted = False

    def interrupt_entry(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is guarded.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt("refresh-entry-control-private")
        return interrupt_entry

    try:
        sys.settrace(interrupt_entry)
        with pytest.raises(KeyboardInterrupt) as raised:
            _exchange()
    finally:
        sys.settrace(None)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, frame_values = _module_traceback(raised.value)
    assert names == [
        "exchange_google_calendar_refresh_token",
        "_raise_control_exception",
    ]
    output = " ".join(
        (
            repr(raised.value),
            _formatted_exception(raised.value),
            frame_values,
            caplog.text,
        )
    )
    for private in (
        "refresh-entry-control-private",
        CLIENT_SECRET,
        REFRESH_TOKEN,
        ACCOUNT_ID,
        INSTRUCTOR_ID,
        CONNECTION_ID,
    ):
        assert private not in output


def test_refresh_exchange_cleanup_interruption_is_sanitized(caplog):
    caplog.set_level(logging.DEBUG)
    guarded = exchange_google_calendar_refresh_token
    source_lines, first_line = inspect.getsourcelines(guarded)
    target_line = first_line + next(
        offset
        for offset, line in enumerate(source_lines)
        if line.lstrip().startswith("try:") and " = None" in line
    )
    cleanup_instructions = [
        instruction
        for instruction in dis.get_instructions(guarded)
        if instruction.positions.lineno == target_line
    ]
    assert "DELETE_FAST" not in {
        instruction.opname for instruction in cleanup_instructions
    }
    assert sum(
        instruction.opname == "STORE_FAST"
        for instruction in cleanup_instructions
    ) >= 2
    interrupted = False

    def interrupt_cleanup(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is guarded.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt("refresh-cleanup-control-private")
        return interrupt_cleanup

    try:
        sys.settrace(interrupt_cleanup)
        with pytest.raises(KeyboardInterrupt) as raised:
            _exchange(
                transport=RecordingTransport(
                    error=ValueError(PROVIDER_DETAIL)
                )
            )
    finally:
        sys.settrace(None)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, frame_values = _module_traceback(raised.value)
    assert names == [
        "exchange_google_calendar_refresh_token",
        "_raise_control_exception",
    ]
    output = " ".join(
        (
            repr(raised.value),
            _formatted_exception(raised.value),
            frame_values,
            caplog.text,
        )
    )
    for private in (
        "refresh-cleanup-control-private",
        CLIENT_SECRET,
        REFRESH_TOKEN,
        ACCOUNT_ID,
        INSTRUCTOR_ID,
        CONNECTION_ID,
        PROVIDER_DETAIL,
    ):
        assert private not in output


def test_parser_failure_and_ambient_context_are_sanitized(
    monkeypatch,
    caplog,
):
    caplog.set_level(logging.DEBUG)
    parser_detail = "refresh-parser-private-detail"

    def fail_parser(*_args, **_kwargs):
        raise RuntimeError(
            " ".join((parser_detail, ACCESS_TOKEN, REFRESH_TOKEN))
        )

    monkeypatch.setattr(token_exchange.json, "loads", fail_parser)
    try:
        raise RuntimeError("ambient-refresh-private-detail")
    except RuntimeError:
        with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
            _exchange()

    names, frame_values = _module_traceback(raised.value)
    output = " ".join(
        (
            str(raised.value),
            repr(raised.value),
            _formatted_exception(raised.value),
            frame_values,
            caplog.text,
        )
    )
    assert names == [
        "exchange_google_calendar_refresh_token",
        "_raise_token_exchange_error",
    ]
    for private in (
        parser_detail,
        "ambient-refresh-private-detail",
        ACCESS_TOKEN,
        REFRESH_TOKEN,
    ):
        assert private not in output


def test_import_is_runtime_inert_without_calendar_environment():
    code = """
import os
import sys
for name in tuple(os.environ):
    if name.startswith('SWIM_GOOGLE_CALENDAR_'):
        os.environ.pop(name)
from app import google_calendar_token_exchange
assert callable(google_calendar_token_exchange.exchange_google_calendar_refresh_token)
assert 'app.models' not in sys.modules
assert 'sqlalchemy' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
