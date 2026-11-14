from __future__ import annotations

import inspect
import json
import ssl
import traceback

import httpx
import pytest

from app import api, google_calendar_token_httpx, tools
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
    exchange_google_calendar_authorization_code,
)
from app.google_calendar_token_httpx import HttpxGoogleCalendarTokenTransport
from app.routing import HaversineRoutingProvider


CLIENT_ID = "123456789-dayweave.apps.googleusercontent.com"
REDIRECT_URI = "https://api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH
CLIENT_SECRET = "client+secret/private&value="
AUTHORIZATION_CODE = "4/code+private&value="
CODE_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
ACCESS_TOKEN = "access-token-private-value"
REFRESH_TOKEN = "refresh-token-private-value"
PROVIDER_DETAIL = "provider-private-error-detail"
MAX_BODY_BYTES = 64 * 1024


class TextSubclass(str):
    pass


class RecordingStream(httpx.SyncByteStream):
    def __init__(self, chunks=(), *, error=None, close_error=None):
        self.chunks = tuple(chunks)
        self.error = error
        self.close_error = close_error
        self.iteration_count = 0
        self.close_count = 0

    def __iter__(self):
        self.iteration_count += 1
        yield from self.chunks
        if self.error is not None:
            raise self.error

    def close(self):
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


class RecordingTransport(httpx.BaseTransport):
    def __init__(self, handler, *, close_error=None):
        self.handler = handler
        self.close_error = close_error
        self.requests = []
        self.close_count = 0

    def handle_request(self, request):
        self.requests.append(request)
        return self.handler(request)

    def close(self):
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


def _fields(**overrides):
    values = {
        "code": AUTHORIZATION_CODE,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
        "code_verifier": CODE_VERIFIER,
    }
    values.update(overrides)
    return tuple((name, values[name]) for name in values)


def _payload():
    return {
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "expires_in": 3600,
        "scope": GOOGLE_CALENDAR_FREEBUSY_SCOPE,
        "token_type": "Bearer",
    }


def _response(
    *,
    status_code=200,
    body=b"{}",
    headers=None,
    stream=None,
):
    if stream is None:
        stream = RecordingStream((body,))
    if headers is None:
        headers = [
            ("content-type", "application/json"),
            ("content-length", str(len(body))),
        ]
    return httpx.Response(
        status_code,
        headers=headers,
        stream=stream,
    )


def _install_transport(monkeypatch, handler, *, close_error=None):
    transport = RecordingTransport(handler, close_error=close_error)
    factory_calls = []

    def factory():
        factory_calls.append(True)
        return transport

    monkeypatch.setattr(google_calendar_token_httpx, "_new_http_transport", factory)
    return transport, factory_calls


def _post(transport=None, **overrides):
    return (transport or HttpxGoogleCalendarTokenTransport()).post_form(
        url=overrides.get("url", GOOGLE_CALENDAR_TOKEN_ENDPOINT),
        fields=overrides.get("fields", _fields()),
        timeout_seconds=overrides.get("timeout_seconds", 5.0),
    )


def _formatted_exception(error):
    return "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )


def _module_traceback(error):
    names = []
    values = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith(
            "/app/google_calendar_token_httpx.py"
        ):
            names.append(current.tb_frame.f_code.co_name)
            values.extend(current.tb_frame.f_locals.values())
        current = current.tb_next
    return names, repr(values)


def test_public_surface_is_stateless_final_and_keyword_only():
    assert google_calendar_token_httpx.__all__ == (
        "HttpxGoogleCalendarTokenTransport",
    )
    adapter = HttpxGoogleCalendarTokenTransport()
    assert not hasattr(adapter, "__dict__")
    assert inspect.signature(HttpxGoogleCalendarTokenTransport) == inspect.Signature()

    signature = inspect.signature(HttpxGoogleCalendarTokenTransport.post_form)
    assert list(signature.parameters) == [
        "self",
        "url",
        "fields",
        "timeout_seconds",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for name, parameter in signature.parameters.items()
        if name != "self"
    )
    with pytest.raises(TypeError):
        type("UnsafeTransport", (HttpxGoogleCalendarTokenTransport,), {})


def test_exact_post_form_encoding_headers_and_timeout(monkeypatch):
    def handler(_request):
        return _response()

    network_transport, factory_calls = _install_transport(monkeypatch, handler)

    response = _post()

    assert response.status_code == 200
    assert response.content_type == "application/json"
    assert response.body == b"{}"
    assert factory_calls == [True]
    assert network_transport.close_count == 1
    assert len(network_transport.requests) == 1
    request = network_transport.requests[0]
    assert request.method == "POST"
    assert str(request.url) == GOOGLE_CALENDAR_TOKEN_ENDPOINT
    assert request.url.query == b""
    assert request.headers["accept"] == "application/json"
    assert request.headers["accept-encoding"] == "identity"
    assert request.headers["content-type"] == (
        "application/x-www-form-urlencoded"
    )
    assert "authorization" not in request.headers
    assert "proxy-authorization" not in request.headers
    assert "cookie" not in request.headers
    assert request.extensions["timeout"] == {
        "connect": 5.0,
        "read": 5.0,
        "write": 5.0,
        "pool": 5.0,
    }
    assert request.read() == (
        b"code=4%2Fcode%2Bprivate%26value%3D"
        b"&client_id=123456789-dayweave.apps.googleusercontent.com"
        b"&client_secret=client%2Bsecret%2Fprivate%26value%3D"
        b"&redirect_uri=https%3A%2F%2Fapi.dayweave.example%2Fapi%2Foauth"
        b"%2Fgoogle-calendar%2Fcallback"
        b"&grant_type=authorization_code"
        b"&code_verifier=dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    )
    header_text = repr(request.headers)
    url_text = str(request.url)
    for secret in (AUTHORIZATION_CODE, CLIENT_SECRET, CODE_VERIFIER):
        assert secret not in header_text
        assert secret not in url_text


def test_fixed_httpx_transport_and_client_policy_ignores_environment(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-private.invalid:9999")
    monkeypatch.setenv("SSL_CERT_FILE", "/private/provider-ca-secret.pem")
    keylog_path = tmp_path / "provider-tls-secrets.log"
    monkeypatch.setenv("SSLKEYLOGFILE", str(keylog_path))
    network_transport = RecordingTransport(lambda _request: _response())
    transport_kwargs = []
    client_kwargs = []
    real_client = httpx.Client

    def build_transport(**kwargs):
        transport_kwargs.append(kwargs)
        return network_transport

    def build_client(**kwargs):
        client_kwargs.append(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr(google_calendar_token_httpx.httpx, "HTTPTransport", build_transport)
    monkeypatch.setattr(google_calendar_token_httpx.httpx, "Client", build_client)

    _post()

    assert len(transport_kwargs) == 1
    transport_options = transport_kwargs[0]
    ssl_context = transport_options.pop("verify")
    assert isinstance(ssl_context, ssl.SSLContext)
    assert ssl_context.check_hostname is True
    assert ssl_context.verify_mode is ssl.CERT_REQUIRED
    assert ssl_context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert ssl_context.keylog_filename is None
    assert not keylog_path.exists()
    assert transport_options == {
        "trust_env": False,
        "proxy": None,
        "http1": True,
        "http2": False,
        "retries": 0,
    }
    assert len(client_kwargs) == 1
    options = client_kwargs[0]
    assert options["transport"] is network_transport
    assert "verify" not in options
    assert options["trust_env"] is False
    assert options["proxy"] is None
    assert options["http1"] is True
    assert options["http2"] is False
    assert options["follow_redirects"] is False
    assert options["timeout"].connect == 5.0
    assert options["timeout"].read == 5.0
    assert options["timeout"].write == 5.0
    assert options["timeout"].pool == 5.0


@pytest.mark.parametrize(
    ("url", "fields", "timeout_seconds"),
    [
        ("https://evil.example/token", _fields(), 5.0),
        (TextSubclass(GOOGLE_CALENDAR_TOKEN_ENDPOINT), _fields(), 5.0),
        (GOOGLE_CALENDAR_TOKEN_ENDPOINT, list(_fields()), 5.0),
        (GOOGLE_CALENDAR_TOKEN_ENDPOINT, _fields()[:-1], 5.0),
        (
            GOOGLE_CALENDAR_TOKEN_ENDPOINT,
            (list(_fields()[0]),) + _fields()[1:],
            5.0,
        ),
        (
            GOOGLE_CALENDAR_TOKEN_ENDPOINT,
            ((_fields()[1], _fields()[0]) + _fields()[2:]),
            5.0,
        ),
        (
            GOOGLE_CALENDAR_TOKEN_ENDPOINT,
            ((TextSubclass("code"), AUTHORIZATION_CODE),) + _fields()[1:],
            5.0,
        ),
        (
            GOOGLE_CALENDAR_TOKEN_ENDPOINT,
            _fields(client_secret=TextSubclass(CLIENT_SECRET)),
            5.0,
        ),
        (GOOGLE_CALENDAR_TOKEN_ENDPOINT, _fields(code=""), 5.0),
        (GOOGLE_CALENDAR_TOKEN_ENDPOINT, _fields(code=" bad"), 5.0),
        (GOOGLE_CALENDAR_TOKEN_ENDPOINT, _fields(code="bad\ncode"), 5.0),
        (GOOGLE_CALENDAR_TOKEN_ENDPOINT, _fields(code="café"), 5.0),
        (
            GOOGLE_CALENDAR_TOKEN_ENDPOINT,
            _fields(grant_type="refresh_token"),
            5.0,
        ),
        (
            GOOGLE_CALENDAR_TOKEN_ENDPOINT,
            _fields(client_secret="x" * MAX_BODY_BYTES),
            5.0,
        ),
        (GOOGLE_CALENDAR_TOKEN_ENDPOINT, _fields(), 5),
        (GOOGLE_CALENDAR_TOKEN_ENDPOINT, _fields(), 4.999),
        (GOOGLE_CALENDAR_TOKEN_ENDPOINT, _fields(), float("nan")),
    ],
)
def test_invalid_request_contract_fails_before_network(
    monkeypatch,
    url,
    fields,
    timeout_seconds,
):
    factory_calls = []

    def forbidden_factory():
        factory_calls.append(True)
        raise AssertionError("network objects must not be constructed")

    monkeypatch.setattr(
        google_calendar_token_httpx,
        "_new_http_transport",
        forbidden_factory,
    )

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _post(url=url, fields=fields, timeout_seconds=timeout_seconds)

    assert factory_calls == []


def test_field_size_is_rejected_before_copying_or_form_encoding(monkeypatch):
    validation_calls = []
    encoding_calls = []

    def forbidden_validation(_value):
        validation_calls.append(True)
        raise AssertionError("oversized values must fail before string validation")

    def forbidden_encoding(*_args, **_kwargs):
        encoding_calls.append(True)
        raise AssertionError("oversized values must fail before form encoding")

    monkeypatch.setattr(
        google_calendar_token_httpx,
        "_visible_ascii",
        forbidden_validation,
    )
    monkeypatch.setattr(
        google_calendar_token_httpx,
        "urlencode",
        forbidden_encoding,
    )

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _post(fields=_fields(code="%" * (8 * 1024 + 1)))

    assert validation_calls == []
    assert encoding_calls == []


@pytest.mark.parametrize("status_code", [302, 400, 429, 500, 503])
def test_non_200_response_is_not_followed_retried_or_read(
    monkeypatch,
    status_code,
):
    stream = RecordingStream(
        error=AssertionError("provider error body must not be read")
    )

    def handler(_request):
        return _response(
            status_code=status_code,
            headers=[
                ("location", "https://redirect-private.invalid/token"),
                ("content-type", "application/json"),
            ],
            stream=stream,
        )

    network_transport, _ = _install_transport(monkeypatch, handler)

    response = _post()

    assert response.status_code == status_code
    assert response.content_type is None
    assert response.body == b""
    assert len(network_transport.requests) == 1
    assert network_transport.close_count == 1
    assert stream.iteration_count == 0
    assert stream.close_count == 1


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda request: httpx.ConnectTimeout(PROVIDER_DETAIL, request=request),
        lambda request: httpx.ReadTimeout(PROVIDER_DETAIL, request=request),
        lambda request: httpx.WriteTimeout(PROVIDER_DETAIL, request=request),
        lambda request: httpx.PoolTimeout(PROVIDER_DETAIL, request=request),
        lambda request: httpx.RemoteProtocolError(
            PROVIDER_DETAIL,
            request=request,
        ),
        lambda _request: RuntimeError(PROVIDER_DETAIL),
    ],
)
def test_transport_failures_are_never_retried(monkeypatch, error_factory):
    def handler(request):
        raise error_factory(request)

    network_transport, factory_calls = _install_transport(monkeypatch, handler)

    with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
        _post()

    assert str(raised.value) == (
        "Google Calendar authorization could not be completed."
    )
    assert len(network_transport.requests) == 1
    assert network_transport.close_count == 1
    assert factory_calls == [True]


def test_exact_response_limit_is_streamed_and_accepted(monkeypatch):
    body = b"x" * MAX_BODY_BYTES
    stream = RecordingStream((body,))
    _install_transport(
        monkeypatch,
        lambda _request: _response(
            body=body,
            headers=[
                ("content-type", "application/json"),
                ("content-encoding", "Identity"),
                ("content-length", str(MAX_BODY_BYTES)),
            ],
            stream=stream,
        ),
    )

    response = _post()

    assert response.body == body
    assert stream.iteration_count == 1
    assert stream.close_count == 1


def test_chunked_response_over_limit_fails_and_closes(monkeypatch):
    stream = RecordingStream((b"x" * MAX_BODY_BYTES, b"y"))
    network_transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(
            headers=[("content-type", "application/json")],
            stream=stream,
        ),
    )

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _post()

    assert stream.iteration_count == 1
    assert stream.close_count == 1
    assert network_transport.close_count == 1


@pytest.mark.parametrize(
    "headers",
    [
        [("content-length", str(MAX_BODY_BYTES + 1))],
        [("content-length", "-1")],
        [("content-length", "+1")],
        [("content-length", "1.0")],
        [("content-length", "1, 1")],
        [("content-length", "1234567")],
        [("content-length", "1"), ("content-length", "1")],
    ],
)
def test_invalid_declared_length_fails_before_body_iteration(
    monkeypatch,
    headers,
):
    stream = RecordingStream(error=AssertionError("body must not be read"))
    _install_transport(
        monkeypatch,
        lambda _request: _response(headers=headers, stream=stream),
    )

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _post()

    assert stream.iteration_count == 0
    assert stream.close_count == 1


def test_declared_length_must_match_streamed_length(monkeypatch):
    stream = RecordingStream((b"x",))
    _install_transport(
        monkeypatch,
        lambda _request: _response(
            headers=[("content-length", "2")],
            stream=stream,
        ),
    )

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _post()

    assert stream.iteration_count == 1
    assert stream.close_count == 1


@pytest.mark.parametrize(
    "encoding_headers",
    [
        [("content-encoding", "gzip")],
        [("content-encoding", "deflate")],
        [("content-encoding", "br")],
        [("content-encoding", "zstd")],
        [("content-encoding", "gzip, br")],
        [("content-encoding", "identity ")],
        [
            ("content-encoding", "identity"),
            ("content-encoding", "identity"),
        ],
    ],
)
def test_compressed_or_ambiguous_encoding_is_rejected_without_reading(
    monkeypatch,
    encoding_headers,
):
    stream = RecordingStream(error=AssertionError("body must not be read"))
    _install_transport(
        monkeypatch,
        lambda _request: _response(headers=encoding_headers, stream=stream),
    )

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _post()

    assert stream.iteration_count == 0
    assert stream.close_count == 1


def test_midstream_and_cleanup_failures_fail_closed(monkeypatch):
    stream = RecordingStream(
        (b"partial",),
        error=RuntimeError(PROVIDER_DETAIL),
    )
    network_transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(
            headers=[("content-type", "application/json")],
            stream=stream,
        ),
        close_error=RuntimeError("transport-close-private-detail"),
    )

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _post()

    assert stream.iteration_count == 1
    assert stream.close_count == 1
    assert network_transport.close_count == 1


def test_response_close_failure_is_sanitized_and_closes_transport(monkeypatch):
    stream = RecordingStream(
        (b"{}",),
        close_error=RuntimeError("response-close-private-detail"),
    )
    network_transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(stream=stream),
    )

    with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
        _post()

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert stream.iteration_count == 1
    assert stream.close_count == 1
    assert network_transport.close_count == 1


def test_client_construction_failure_closes_created_transport(monkeypatch):
    network_transport = RecordingTransport(lambda _request: _response())
    monkeypatch.setattr(
        google_calendar_token_httpx,
        "_new_http_transport",
        lambda: network_transport,
    )

    def fail_client(**_kwargs):
        raise RuntimeError(PROVIDER_DETAIL)

    monkeypatch.setattr(google_calendar_token_httpx.httpx, "Client", fail_client)

    with pytest.raises(GoogleCalendarTokenExchangeError):
        _post()

    assert network_transport.requests == []
    assert network_transport.close_count == 1


def test_httpx_exception_and_public_traceback_never_disclose_secrets(
    monkeypatch,
    caplog,
):
    def handler(request):
        encoded_body = request.read().decode("ascii")
        raise RuntimeError(
            " ".join(
                (
                    PROVIDER_DETAIL,
                    AUTHORIZATION_CODE,
                    CLIENT_SECRET,
                    CODE_VERIFIER,
                    encoded_body,
                )
            )
        )

    _install_transport(monkeypatch, handler)

    with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
        _post()

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    names, frame_values = _module_traceback(raised.value)
    assert names == ["post_form", "_raise_token_exchange_error"]
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
        PROVIDER_DETAIL,
        AUTHORIZATION_CODE,
        CLIENT_SECRET,
        CODE_VERIFIER,
        "4%2Fcode%2Bprivate%26value%3D",
        "client%2Bsecret%2Fprivate%26value%3D",
    ):
        assert private not in output


def test_failure_clears_secret_bearing_ambient_exception_context():
    ambient_secret = "ambient-private-exception-detail"

    try:
        raise RuntimeError(ambient_secret)
    except RuntimeError:
        with pytest.raises(GoogleCalendarTokenExchangeError) as raised:
            _post(url="https://evil.example/token")

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    _, frame_values = _module_traceback(raised.value)
    assert ambient_secret not in " ".join(
        (_formatted_exception(raised.value), frame_values)
    )


def test_adapter_composes_with_strict_token_exchange(monkeypatch):
    payload = json.dumps(_payload(), separators=(",", ":")).encode("utf-8")
    network_transport, _ = _install_transport(
        monkeypatch,
        lambda _request: _response(body=payload),
    )
    configuration = GoogleCalendarTokenExchangeConfiguration(
        authorization_configuration=GoogleCalendarOAuthConfiguration(
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
        ),
        client_secret=CLIENT_SECRET,
    )
    assert derive_s256_code_challenge(CODE_VERIFIER)

    result = exchange_google_calendar_authorization_code(
        configuration=configuration,
        authorization_code=AUTHORIZATION_CODE,
        code_verifier=CODE_VERIFIER,
        transport=HttpxGoogleCalendarTokenTransport(),
    )

    assert result.refresh_token == REFRESH_TOKEN
    assert len(network_transport.requests) == 1


def test_construction_and_application_startup_remain_network_free(monkeypatch):
    calls = []

    def forbidden_factory():
        calls.append(True)
        raise AssertionError("token transport must remain call-time only")

    monkeypatch.setattr(
        google_calendar_token_httpx,
        "_new_http_transport",
        forbidden_factory,
    )

    HttpxGoogleCalendarTokenTransport()
    try:
        app = api.create_app()
        assert not hasattr(app.state, "google_calendar_token_transport")
        assert "google_calendar_token_transport" not in app.state.routing_readiness
        assert calls == []
    finally:
        tools.configure_default_routing_provider(HaversineRoutingProvider())
        tools.reset_routing_provider()
