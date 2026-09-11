import datetime
import json
import urllib.error
from urllib.parse import parse_qs, urlparse

import pytest

from app.routing import (
    GeocodeFailed,
    RouteEstimateFailed,
    RoutingProviderNotConfigured,
    RoutingProviderUnavailable,
    RoutingTimeout,
)
from app.routing_providers import MapboxRoutingProvider


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


def _http_error(status_code: int):
    raise urllib.error.HTTPError(
        url="https://api.mapbox.com/test?access_token=secret",
        code=status_code,
        msg="provider failed",
        hdrs=None,
        fp=None,
    )
