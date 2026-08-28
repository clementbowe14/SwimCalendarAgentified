import pytest

from app.config import routing_settings_from_env
from app.routing import HaversineRoutingProvider, RoutingProviderNotConfigured
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


def test_mapbox_provider_is_reserved_for_production_provider_pr(monkeypatch):
    monkeypatch.setenv("SWIM_ROUTING_PROVIDER", "mapbox")

    with pytest.raises(RoutingProviderNotConfigured, match="next routing provider PR"):
        build_routing_provider(routing_settings_from_env())


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
