from typing import Optional

from app.config import RoutingSettings, routing_settings_from_env
from app.routing_cache import CachingRoutingProvider, DatabaseRouteEstimateCache
from app.routing_providers import MapboxRoutingProvider
from app.routing import BaseRoutingProvider, HaversineRoutingProvider, RoutingProviderNotConfigured


def build_routing_provider(settings: Optional[RoutingSettings] = None) -> BaseRoutingProvider:
    resolved = settings or routing_settings_from_env()

    if resolved.provider in {"haversine", "mock", "local"}:
        provider: BaseRoutingProvider = HaversineRoutingProvider()
    elif resolved.provider == "mapbox":
        provider = MapboxRoutingProvider(
            access_token=resolved.mapbox_access_token,
            timeout_seconds=resolved.timeout_seconds,
        )
    else:
        raise RoutingProviderNotConfigured(f"Unsupported routing provider: {resolved.provider}")

    if resolved.route_cache_enabled:
        return CachingRoutingProvider(
            provider=provider,
            cache=DatabaseRouteEstimateCache(),
            ttl_hours=resolved.route_cache_ttl_hours,
            bucket_minutes=resolved.departure_bucket_minutes,
            traffic_mode=resolved.traffic_mode,
        )
    return provider
