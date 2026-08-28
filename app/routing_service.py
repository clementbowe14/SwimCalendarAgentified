from typing import Optional

from app.config import RoutingSettings, routing_settings_from_env
from app.routing import BaseRoutingProvider, HaversineRoutingProvider, RoutingProviderNotConfigured


def build_routing_provider(settings: Optional[RoutingSettings] = None) -> BaseRoutingProvider:
    resolved = settings or routing_settings_from_env()

    if resolved.provider in {"haversine", "mock", "local"}:
        return HaversineRoutingProvider()

    if resolved.provider == "mapbox":
        raise RoutingProviderNotConfigured(
            "Mapbox routing provider support is configured in the next routing provider PR."
        )

    raise RoutingProviderNotConfigured(f"Unsupported routing provider: {resolved.provider}")
