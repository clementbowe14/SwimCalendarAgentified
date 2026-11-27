from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from app.config import RoutingSettings, routing_settings_from_env
from app.routing import HaversineRoutingProvider, RoutingProviderNotConfigured
from app.routing_cache import CachingRoutingProvider
from app.routing_providers import MapboxRoutingProvider
from app.routing_service import build_routing_provider


def test_default_routing_provider_is_deterministic_haversine(monkeypatch):
    monkeypatch.delenv("SWIM_ROUTING_PROVIDER", raising=False)

    provider = build_routing_provider(routing_settings_from_env())

    assert isinstance(provider, HaversineRoutingProvider)


def test_explicit_mock_routing_provider_uses_haversine(monkeypatch):
    monkeypatch.setenv("SWIM_ROUTING_PROVIDER", "mock")

    provider = build_routing_provider(routing_settings_from_env())

    assert isinstance(provider, HaversineRoutingProvider)


def test_invalid_routing_provider_fails_with_configuration_error(monkeypatch):
    monkeypatch.setenv("SWIM_ROUTING_PROVIDER", "not-a-provider")

    with pytest.raises(RoutingProviderNotConfigured, match="Unsupported routing provider"):
        build_routing_provider(routing_settings_from_env())


def test_mapbox_provider_requires_access_token(monkeypatch):
    monkeypatch.setenv("SWIM_ROUTING_PROVIDER", "mapbox")
    monkeypatch.delenv("SWIM_MAPBOX_ACCESS_TOKEN", raising=False)

    with pytest.raises(RoutingProviderNotConfigured, match="SWIM_MAPBOX_ACCESS_TOKEN"):
        build_routing_provider(routing_settings_from_env())


def test_mapbox_provider_can_be_selected_by_configuration(monkeypatch):
    monkeypatch.setenv("SWIM_ROUTING_PROVIDER", "mapbox")
    monkeypatch.setenv("SWIM_MAPBOX_ACCESS_TOKEN", "test-token")
    monkeypatch.delenv("SWIM_ROUTE_CACHE_ENABLED", raising=False)

    provider = build_routing_provider(routing_settings_from_env())

    assert isinstance(provider, MapboxRoutingProvider)


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize("provider_name", ["haversine", "mock", "local"])
def test_deployed_environments_reject_synthetic_routing(monkeypatch, environment, provider_name):
    monkeypatch.setenv("SWIM_ENV", environment)
    monkeypatch.setenv("SWIM_ROUTING_PROVIDER", provider_name)

    with pytest.raises(RoutingProviderNotConfigured, match="external routing provider"):
        routing_settings_from_env()


def test_production_rejects_implicit_haversine_default(monkeypatch):
    monkeypatch.setenv("SWIM_ENV", "production")
    monkeypatch.delenv("SWIM_ROUTING_PROVIDER", raising=False)

    with pytest.raises(RoutingProviderNotConfigured, match="external routing provider"):
        routing_settings_from_env()


def test_production_mapbox_requires_non_whitespace_credentials(monkeypatch):
    monkeypatch.setenv("SWIM_ENV", "production")
    monkeypatch.setenv("SWIM_ROUTING_PROVIDER", "mapbox")
    monkeypatch.setenv("SWIM_MAPBOX_ACCESS_TOKEN", "   ")

    with pytest.raises(RoutingProviderNotConfigured, match="SWIM_MAPBOX_ACCESS_TOKEN"):
        build_routing_provider(routing_settings_from_env())


def test_production_mapbox_builds_with_configured_credentials(monkeypatch):
    monkeypatch.setenv("SWIM_ENV", "production")
    monkeypatch.setenv("SWIM_ROUTING_PROVIDER", "mapbox")
    monkeypatch.setenv("SWIM_MAPBOX_ACCESS_TOKEN", "configured-token")

    provider = build_routing_provider(routing_settings_from_env())

    assert isinstance(provider, MapboxRoutingProvider)


def test_provider_builder_defends_against_synthetic_deployed_settings():
    settings = RoutingSettings(environment="production", provider="haversine")

    with pytest.raises(RoutingProviderNotConfigured, match="external routing provider"):
        build_routing_provider(settings)


def test_routing_settings_preserve_existing_positional_provider_construction():
    settings = RoutingSettings("mapbox", mapbox_access_token="configured-token")

    provider = build_routing_provider(settings)

    assert isinstance(provider, MapboxRoutingProvider)


def test_explicit_mapbox_settings_reject_whitespace_credentials():
    settings = RoutingSettings(provider="mapbox", mapbox_access_token="   ")

    with pytest.raises(RoutingProviderNotConfigured, match="SWIM_MAPBOX_ACCESS_TOKEN"):
        build_routing_provider(settings)


@pytest.mark.parametrize("value", ["not-a-timeout", float("nan"), float("inf")])
def test_explicit_routing_settings_reject_malformed_timeout(value):
    settings = RoutingSettings(timeout_seconds=value)

    with pytest.raises(RoutingProviderNotConfigured, match="timeout"):
        build_routing_provider(settings)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_non_finite_routing_timeout_fails_closed(monkeypatch, value):
    monkeypatch.setenv("SWIM_ROUTING_TIMEOUT_SECONDS", value)

    with pytest.raises(RoutingProviderNotConfigured, match="timeout"):
        routing_settings_from_env()


@pytest.mark.parametrize("value", ["sometimes", "enabled", "2"])
def test_invalid_route_cache_boolean_fails_closed(monkeypatch, value):
    monkeypatch.setenv("SWIM_ROUTE_CACHE_ENABLED", value)

    with pytest.raises(RoutingProviderNotConfigured, match="boolean"):
        routing_settings_from_env()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("SWIM_ROUTING_TIMEOUT_SECONDS", "soon", "timeout"),
        ("SWIM_ROUTE_CACHE_TTL_HOURS", "tomorrow", "integer"),
        ("SWIM_ROUTE_DEPARTURE_BUCKET_MINUTES", "quarter-hour", "integer"),
    ],
)
def test_malformed_numeric_routing_settings_use_configuration_error(monkeypatch, name, value, message):
    monkeypatch.setenv(name, value)

    with pytest.raises(RoutingProviderNotConfigured, match=message):
        routing_settings_from_env()


def test_configured_cache_wraps_selected_provider(monkeypatch):
    monkeypatch.setenv("SWIM_ROUTE_CACHE_ENABLED", "true")
    monkeypatch.setenv("SWIM_ROUTING_PROVIDER", "haversine")

    provider = build_routing_provider(routing_settings_from_env())

    assert isinstance(provider, CachingRoutingProvider)


def test_uncached_mapbox_uses_the_configured_traffic_mode(monkeypatch):
    calls = []

    def transport(url, _timeout):
        calls.append(urlsplit(url))
        return {"routes": [{"distance": 1200, "duration": 600}]}

    monkeypatch.setattr(MapboxRoutingProvider, "_urllib_fetch_json", staticmethod(transport))
    provider = build_routing_provider(RoutingSettings(
        provider="mapbox", mapbox_access_token="fixture", traffic_mode="typical", route_cache_enabled=False,
    ))
    provider.estimate_route(
        (30.2, -97.7), (30.3, -97.8), departure_time=datetime(2026, 9, 12, 13, 52, tzinfo=timezone.utc)
    )

    request, = calls
    assert "/mapbox/driving-traffic/" in request.path
    assert parse_qs(request.query)["depart_at"] == ["2026-09-12T13:52:00Z"]


def test_routing_settings_parse_cache_and_traffic_env(monkeypatch):
    monkeypatch.setenv("SWIM_ROUTE_CACHE_ENABLED", "true")
    monkeypatch.setenv("SWIM_ROUTE_CACHE_TTL_HOURS", "12")
    monkeypatch.setenv("SWIM_ROUTE_DEPARTURE_BUCKET_MINUTES", "30")
    monkeypatch.setenv("SWIM_ROUTING_TRAFFIC_MODE", "live")

    settings = routing_settings_from_env()

    assert settings.route_cache_enabled is True
    assert settings.route_cache_ttl_hours == 12
    assert settings.departure_bucket_minutes == 30
    assert settings.traffic_mode == "live"


def test_invalid_traffic_mode_fails_with_configuration_error(monkeypatch):
    monkeypatch.setenv("SWIM_ROUTING_TRAFFIC_MODE", "rush-hour")

    with pytest.raises(RoutingProviderNotConfigured, match="traffic mode"):
        routing_settings_from_env()


def test_invalid_timeout_fails_with_configuration_error(monkeypatch):
    monkeypatch.setenv("SWIM_ROUTING_TIMEOUT_SECONDS", "0")

    with pytest.raises(RoutingProviderNotConfigured, match="timeout"):
        routing_settings_from_env()


def test_invalid_enabled_cache_values_fail_with_configuration_error(monkeypatch):
    monkeypatch.setenv("SWIM_ROUTE_CACHE_ENABLED", "true")
    monkeypatch.setenv("SWIM_ROUTE_CACHE_TTL_HOURS", "0")

    with pytest.raises(RoutingProviderNotConfigured, match="TTL"):
        routing_settings_from_env()

    monkeypatch.setenv("SWIM_ROUTE_CACHE_TTL_HOURS", "24")
    monkeypatch.setenv("SWIM_ROUTE_DEPARTURE_BUCKET_MINUTES", "0")

    with pytest.raises(RoutingProviderNotConfigured, match="bucket"):
        routing_settings_from_env()
