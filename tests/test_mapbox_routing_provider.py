import datetime
import json
import urllib.error
import urllib.request
from contextlib import asynccontextmanager, nullcontext
from http.client import HTTPException, IncompleteRead
from time import monotonic
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import anyio
import httpcore
import pytest

from app.routing import (
    GeocodeFailed,
    RouteEstimateFailed,
    RoutingProviderNotConfigured,
    RoutingProviderUnavailable,
    RoutingTimeout,
)
from app.routing_providers import MapboxRoutingProvider, _mapbox_proxy, routing_request_deadline


def test_mapbox_geocode_parses_forward_geocoding_response():
    calls = []

    def fetch_json(url, timeout_seconds):
        calls.append((url, timeout_seconds))
        return {"features": [{"center": [-97.7431, 30.2672]}]}

    provider = MapboxRoutingProvider(
        access_token="test-token",
        timeout_seconds=2.5,
        fetch_json=fetch_json,
    )

    assert provider.geocode("100 Main St, Austin, TX") == (30.2672, -97.7431)
    assert "/geocoding/v5/mapbox.places/" in calls[0][0]
    assert calls[0][1] == 2.5


def test_range_routing_rejects_oversized_compressed_and_stalled_responses(monkeypatch):
    async def oversized():
        yield b" " * (300 * 1024)

    async def stalled():
        yield b"{"
        await anyio.sleep_forever()

    class TestPool:
        def __init__(self, **_options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_error):
            pass

        @asynccontextmanager
        async def stream(self, _method, _url, *, headers):
            assert (b"accept-encoding", b"identity") in headers
            yield response

    response = httpcore.Response(200, content=oversized())
    monkeypatch.setattr(httpcore, "AsyncConnectionPool", TestPool)
    provider = MapboxRoutingProvider("synthetic")
    deadline = routing_request_deadline.set(monotonic() + 2.0)
    try:
        with pytest.raises(RoutingProviderUnavailable, match="too large"):
            provider.geocode("Test address")
        response = httpcore.Response(200, content=stalled(), headers=[(b"Content-Encoding", b"gzip")])
        with pytest.raises(RoutingProviderUnavailable, match="encoding"):
            provider.geocode("Test address")
    finally:
        routing_request_deadline.reset(deadline)

    response = httpcore.Response(200, content=stalled())
    deadline = routing_request_deadline.set(monotonic() + 0.1)
    try:
        with pytest.raises(RoutingTimeout):
            provider.geocode("Test address")
    finally:
        routing_request_deadline.reset(deadline)


def test_range_routing_respects_the_https_proxy_and_bypass(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "getproxies",
        lambda: {"https": "http://test-user:test-pass@proxy.invalid:8080"},
    )
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: False)
    proxy = _mapbox_proxy()
    assert proxy is not None
    assert (proxy.url.host, proxy.url.port) == (b"proxy.invalid", 8080)
    assert proxy.auth == (b"test-user", b"test-pass")
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: True)
    assert _mapbox_proxy() is None


def test_mapbox_geocode_no_results_raises_geocode_failed():
    provider = MapboxRoutingProvider(
        access_token="test-token",
        fetch_json=lambda url, timeout_seconds: {"features": []},
    )

    with pytest.raises(GeocodeFailed):
        provider.geocode("No Such Address")


def test_mapbox_geocode_malformed_coordinates_maps_to_provider_unavailable():
    provider = MapboxRoutingProvider(
        access_token="test-token",
        fetch_json=lambda url, timeout_seconds: {"features": [{"center": ["nan", 30.2672]}]},
    )

    with pytest.raises(RoutingProviderUnavailable):
        provider.geocode("100 Main St, Austin, TX")


def test_mapbox_geocode_auth_error_maps_to_configuration_error():
    provider = MapboxRoutingProvider(
        access_token="test-token",
        fetch_json=lambda url, timeout_seconds: _http_error(401),
    )

    with pytest.raises(RoutingProviderNotConfigured):
        provider.geocode("100 Main St, Austin, TX")


def test_mapbox_geocode_rate_limit_maps_to_provider_unavailable():
    provider = MapboxRoutingProvider(
        access_token="test-token",
        fetch_json=lambda url, timeout_seconds: _http_error(429),
    )

    with pytest.raises(RoutingProviderUnavailable):
        provider.geocode("100 Main St, Austin, TX")


def test_mapbox_route_uses_traffic_profile_departure_and_parses_response():
    calls = []

    def fetch_json(url, timeout_seconds):
        calls.append(url)
        return {"routes": [{"distance": 3218.688, "duration": 780.1}]}

    provider = MapboxRoutingProvider(access_token="test-token", fetch_json=fetch_json)
    departure = datetime.datetime.fromisoformat("2026-08-20T09:15:00-05:00")

    estimate = provider.estimate_route(
        (30.2672, -97.7431),
        (30.3300, -97.7200),
        departure_time=departure,
        traffic_mode="live",
    )

    parsed = urlparse(calls[0])
    query = parse_qs(parsed.query)
    assert "/directions/v5/mapbox/driving-traffic/" in calls[0]
    assert query["depart_at"] == ["2026-08-20T14:15:00Z"]
    assert estimate.distance_miles == 2.0
    assert estimate.travel_time_mins == 14
    assert estimate.duration_seconds == 781


def test_mapbox_route_timeout_maps_to_domain_timeout():
    def timeout_fetch(url, timeout_seconds):
        raise TimeoutError("slow")

    provider = MapboxRoutingProvider(access_token="test-token", fetch_json=timeout_fetch)

    with pytest.raises(RoutingTimeout):
        provider.estimate_route(
            (30.2672, -97.7431),
            (30.3300, -97.7200),
            departure_time=datetime.datetime(2026, 8, 20, 9, 15),
            traffic_mode="live",
        )


def test_mapbox_route_no_routes_maps_to_estimate_failure():
    provider = MapboxRoutingProvider(
        access_token="test-token",
        fetch_json=lambda url, timeout_seconds: {"routes": []},
    )

    with pytest.raises(RouteEstimateFailed):
        provider.estimate_route((30.2672, -97.7431), (30.3300, -97.7200))


def test_mapbox_route_invalid_duration_is_not_accepted():
    provider = MapboxRoutingProvider(
        access_token="test-token",
        fetch_json=lambda url, timeout_seconds: {"routes": [{"distance": 1000, "duration": -5}]},
    )

    with pytest.raises(RouteEstimateFailed):
        provider.estimate_route((30.2672, -97.7431), (30.3300, -97.7200))


def test_mapbox_route_422_maps_to_estimate_failure():
    provider = MapboxRoutingProvider(
        access_token="test-token",
        fetch_json=lambda url, timeout_seconds: _http_error(422),
    )

    with pytest.raises(RouteEstimateFailed):
        provider.estimate_route((30.2672, -97.7431), (30.3300, -97.7200))


def test_mapbox_route_5xx_maps_to_provider_unavailable():
    provider = MapboxRoutingProvider(
        access_token="test-token",
        fetch_json=lambda url, timeout_seconds: _http_error(503),
    )

    with pytest.raises(RoutingProviderUnavailable):
        provider.estimate_route((30.2672, -97.7431), (30.3300, -97.7200))


def test_mapbox_malformed_json_maps_to_provider_unavailable():
    def fetch_json(url, timeout_seconds):
        raise json.JSONDecodeError("bad json", "", 0)

    provider = MapboxRoutingProvider(access_token="test-token", fetch_json=fetch_json)

    with pytest.raises(RoutingProviderUnavailable):
        provider.estimate_route((30.2672, -97.7431), (30.3300, -97.7200))


@pytest.mark.parametrize(
    ("read_error", "body", "message"),
    [
        pytest.param(
            HTTPException("private provider response"), None, "Mapbox request failed.", id="http-protocol"
        ),
        pytest.param(
            IncompleteRead(b"private provider response", 10), None, "Mapbox request failed.", id="truncated-body"
        ),
        pytest.param(
            ConnectionResetError("private provider response"), None, "Mapbox request failed.", id="socket-reset"
        ),
        pytest.param(None, b"\xff", "Mapbox returned malformed JSON.", id="invalid-utf8"),
    ],
)
def test_mapbox_urllib_response_failures_raise_sanitized_errors(monkeypatch, read_error, body, message):
    response = Mock()
    response.read.side_effect = read_error
    response.read.return_value = body
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: nullcontext(response))
    provider = MapboxRoutingProvider(access_token="test-token")

    with pytest.raises(RoutingProviderUnavailable) as raised:
        provider.estimate_route((30.2672, -97.7431), (30.3300, -97.7200))

    assert str(raised.value) == message
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


def _http_error(status_code: int):
    raise urllib.error.HTTPError(
        url="https://api.mapbox.com/test?access_token=secret",
        code=status_code,
        msg="provider failed",
        hdrs=None,
        fp=None,
    )
