import json

import pytest

from app import tools
from app.api import create_app
from app.routing import HaversineRoutingProvider, RoutingProviderNotConfigured
from app.routing_providers import MapboxRoutingProvider


def test_production_app_creation_fails_before_serving_with_synthetic_routing(monkeypatch):
    with monkeypatch.context() as deployed:
        _configure_deployed_environment(deployed)
        deployed.setenv("SWIM_ROUTING_PROVIDER", "haversine")

        with pytest.raises(RoutingProviderNotConfigured, match="external routing provider"):
            create_app()


def test_production_app_creation_requires_mapbox_credentials(monkeypatch):
    with monkeypatch.context() as deployed:
        _configure_deployed_environment(deployed)
        deployed.setenv("SWIM_ROUTING_PROVIDER", "mapbox")
        deployed.delenv("SWIM_MAPBOX_ACCESS_TOKEN", raising=False)

        with pytest.raises(RoutingProviderNotConfigured, match="SWIM_MAPBOX_ACCESS_TOKEN"):
            create_app()


def test_production_app_creation_exposes_mapbox_readiness_without_leaking_credentials(monkeypatch):
    secret = "production-mapbox-secret"
    try:
        with monkeypatch.context() as deployed:
            _configure_deployed_environment(deployed)
            deployed.setenv("SWIM_ROUTING_PROVIDER", "mapbox")
            deployed.setenv("SWIM_MAPBOX_ACCESS_TOKEN", secret)

            app = create_app()

            assert app.state.routing_readiness == {
                "provider": "mapbox",
                "mode": "external",
                "configured": True,
            }
            assert secret not in json.dumps(app.state.routing_readiness)
    finally:
        tools.configure_default_routing_provider(HaversineRoutingProvider())
        tools.reset_routing_provider()


def test_reset_restores_the_last_app_validated_provider_across_environment_changes(monkeypatch):
    try:
        with monkeypatch.context() as deployed:
            _configure_deployed_environment(deployed)
            deployed.setenv("SWIM_ROUTING_PROVIDER", "mapbox")
            deployed.setenv("SWIM_MAPBOX_ACCESS_TOKEN", "validated-production-token")

            create_app()
            validated_provider = tools.routing_provider
            assert isinstance(validated_provider, MapboxRoutingProvider)
            tools.configure_routing_provider(HaversineRoutingProvider())

        tools.reset_routing_provider()

        assert tools.routing_provider is validated_provider
        assert isinstance(tools.routing_provider, MapboxRoutingProvider)
    finally:
        tools.configure_default_routing_provider(HaversineRoutingProvider())
        tools.reset_routing_provider()


def _configure_deployed_environment(monkeypatch):
    monkeypatch.setenv("SWIM_ENV", "production")
    monkeypatch.setenv("SWIM_ENABLE_DEMO_DATA", "false")
    monkeypatch.setenv("SWIM_ENABLE_DEMO_AUTH", "false")
    monkeypatch.setenv("SWIM_COACH_COOKIE_SECURE", "true")
    monkeypatch.setenv("SWIM_API_CORS_ORIGINS", "https://scheduler.example.test")
