import datetime

import pytest

from app.routing import BaseRoutingProvider, RouteEstimate, RouteEstimateFailed, RoutingProviderNotConfigured
from app.routing_cache import (
    CachingRoutingProvider,
    InMemoryRouteEstimateCache,
    bucket_departure_time,
    coordinate_key,
)


class CountingProvider(BaseRoutingProvider):
    provider_name = "counting"

    def __init__(self):
        self.route_calls = []

    def estimate_route(self, origin_coords, dest_coords, departure_time=None, traffic_mode="none"):
        self.route_calls.append((origin_coords, dest_coords, departure_time, traffic_mode))
        return RouteEstimate(
            distance_miles=2.0,
            travel_time_mins=12,
            duration_seconds=720,
            provider=self.provider_name,
        )

    def geocode(self, address: str):
        return (30.2672, -97.7431)


class FailingProvider(CountingProvider):
    def estimate_route(self, origin_coords, dest_coords, departure_time=None, traffic_mode="none"):
        self.route_calls.append((origin_coords, dest_coords, departure_time, traffic_mode))
        raise RouteEstimateFailed("provider failed")


def test_route_cache_reuses_same_provider_bucket_and_traffic_mode():
    provider = CountingProvider()
    cached = CachingRoutingProvider(
        provider,
        InMemoryRouteEstimateCache(),
        bucket_minutes=15,
        traffic_mode="live",
    )
    departure = datetime.datetime(2026, 8, 20, 9, 7)

    first = cached.estimate_route((30.0, -97.0), (30.1, -97.1), departure_time=departure)
    second = cached.estimate_route((30.0, -97.0), (30.1, -97.1), departure_time=departure)

    assert first == second
    assert len(provider.route_calls) == 1
    assert provider.route_calls[0][2] == datetime.datetime(2026, 8, 20, 9, 0, tzinfo=datetime.timezone.utc)
    assert provider.route_calls[0][3] == "live"


def test_route_cache_misses_for_reversed_route():
    provider = CountingProvider()
    cached = CachingRoutingProvider(provider, InMemoryRouteEstimateCache())
    departure = datetime.datetime(2026, 8, 20, 9, 0)

    cached.estimate_route((30.0, -97.0), (30.1, -97.1), departure_time=departure)
    cached.estimate_route((30.1, -97.1), (30.0, -97.0), departure_time=departure)

    assert len(provider.route_calls) == 2


def test_route_cache_misses_for_different_traffic_mode():
    provider = CountingProvider()
    cached = CachingRoutingProvider(provider, InMemoryRouteEstimateCache())
    departure = datetime.datetime(2026, 8, 20, 9, 0)

    cached.estimate_route((30.0, -97.0), (30.1, -97.1), departure_time=departure, traffic_mode="none")
    cached.estimate_route((30.0, -97.0), (30.1, -97.1), departure_time=departure, traffic_mode="live")

    assert len(provider.route_calls) == 2


def test_route_cache_misses_when_departure_crosses_bucket_boundary():
    provider = CountingProvider()
    cached = CachingRoutingProvider(provider, InMemoryRouteEstimateCache(), bucket_minutes=15)

    cached.estimate_route(
        (30.0, -97.0),
        (30.1, -97.1),
        departure_time=datetime.datetime(2026, 8, 20, 9, 14),
    )
    cached.estimate_route(
        (30.0, -97.0),
        (30.1, -97.1),
        departure_time=datetime.datetime(2026, 8, 20, 9, 15),
    )

    assert len(provider.route_calls) == 2


def test_expired_route_cache_entry_is_ignored():
    provider = CountingProvider()
    now = [datetime.datetime(2026, 8, 20, 9, 0)]
    cached = CachingRoutingProvider(
        provider,
        InMemoryRouteEstimateCache(),
        ttl_hours=1,
        clock=lambda: now[0],
    )
    departure = datetime.datetime(2026, 8, 20, 9, 0)

    cached.estimate_route((30.0, -97.0), (30.1, -97.1), departure_time=departure)
    now[0] = datetime.datetime(2026, 8, 20, 11, 0)
    cached.estimate_route((30.0, -97.0), (30.1, -97.1), departure_time=departure)

    assert len(provider.route_calls) == 2


def test_provider_failures_are_not_cached():
    provider = FailingProvider()
    cached = CachingRoutingProvider(provider, InMemoryRouteEstimateCache())
    departure = datetime.datetime(2026, 8, 20, 9, 0)

    with pytest.raises(RouteEstimateFailed):
        cached.estimate_route((30.0, -97.0), (30.1, -97.1), departure_time=departure)
    with pytest.raises(RouteEstimateFailed):
        cached.estimate_route((30.0, -97.0), (30.1, -97.1), departure_time=departure)

    assert len(provider.route_calls) == 2


def test_coordinate_keys_and_departure_buckets_are_stable():
    assert coordinate_key((30.123456, -97.765432)) == "30.12346,-97.76543"
    assert coordinate_key((-0.0, -0.0000001)) == "0.00000,0.00000"
    assert bucket_departure_time(
        datetime.datetime(2026, 8, 20, 9, 29, 59),
        15,
    ) == datetime.datetime(2026, 8, 20, 9, 15)


def test_route_cache_canonicalizes_aware_departure_time_to_utc_bucket():
    provider = CountingProvider()
    cached = CachingRoutingProvider(provider, InMemoryRouteEstimateCache(), bucket_minutes=15)
    central_departure = datetime.datetime.fromisoformat("2026-08-20T09:07:00-05:00")
    utc_departure = datetime.datetime(2026, 8, 20, 14, 7, tzinfo=datetime.timezone.utc)

    first = cached.estimate_route((30.0, -97.0), (30.1, -97.1), departure_time=central_departure)
    second = cached.estimate_route((30.0, -97.0), (30.1, -97.1), departure_time=utc_departure)

    assert first == second
    assert len(provider.route_calls) == 1
    assert provider.route_calls[0][2] == datetime.datetime(2026, 8, 20, 14, 0, tzinfo=datetime.timezone.utc)
    assert bucket_departure_time(central_departure, 15) == datetime.datetime(2026, 8, 20, 14, 0)


def test_live_no_departure_route_estimates_bypass_cache():
    provider = CountingProvider()
    cached = CachingRoutingProvider(
        provider,
        InMemoryRouteEstimateCache(),
        traffic_mode="live",
    )

    cached.estimate_route((30.0, -97.0), (30.1, -97.1))
    cached.estimate_route((30.0, -97.0), (30.1, -97.1))

    assert len(provider.route_calls) == 2
    assert provider.route_calls[0][2] is None
    assert provider.route_calls[0][3] == "live"


def test_static_no_departure_route_estimates_can_use_cache():
    provider = CountingProvider()
    cached = CachingRoutingProvider(provider, InMemoryRouteEstimateCache(), traffic_mode="none")

    cached.estimate_route((30.0, -97.0), (30.1, -97.1))
    cached.estimate_route((30.0, -97.0), (30.1, -97.1))

    assert len(provider.route_calls) == 1


def test_invalid_route_cache_settings_fail_fast():
    provider = CountingProvider()

    with pytest.raises(RoutingProviderNotConfigured, match="TTL"):
        CachingRoutingProvider(provider, InMemoryRouteEstimateCache(), ttl_hours=0)
    with pytest.raises(RoutingProviderNotConfigured, match="bucket"):
        CachingRoutingProvider(provider, InMemoryRouteEstimateCache(), bucket_minutes=0)
    with pytest.raises(RoutingProviderNotConfigured, match="bucket"):
        bucket_departure_time(datetime.datetime(2026, 8, 20, 9, 0), 0)
    with pytest.raises(RoutingProviderNotConfigured, match="traffic mode"):
        CachingRoutingProvider(provider, InMemoryRouteEstimateCache(), traffic_mode="rush-hour")
