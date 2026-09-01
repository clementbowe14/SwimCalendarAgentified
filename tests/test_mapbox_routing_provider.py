import datetime
from urllib.parse import parse_qs, urlparse

import pytest

from app.routing import GeocodeFailed, RoutingTimeout
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
