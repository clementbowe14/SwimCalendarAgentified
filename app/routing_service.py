import math
from typing import Optional

from app.config import (
    DEPLOYED_ENVIRONMENTS,
    SUPPORTED_ENVIRONMENTS,
    SUPPORTED_ROUTING_PROVIDERS,
    SUPPORTED_TRAFFIC_MODES,
    RoutingSettings,
    routing_settings_from_env,
)
from app.routing_cache import CachingRoutingProvider, DatabaseRouteEstimateCache
from app.routing_providers import MapboxRoutingProvider
from app.routing import BaseRoutingProvider, HaversineRoutingProvider, RoutingProviderNotConfigured


def build_routing_provider(settings: Optional[RoutingSettings] = None) -> BaseRoutingProvider:
    resolved = settings or routing_settings_from_env()

    if resolved.environment not in SUPPORTED_ENVIRONMENTS:
        raise RoutingProviderNotConfigured("Unsupported routing environment.")
    if resolved.provider not in SUPPORTED_ROUTING_PROVIDERS:
        raise RoutingProviderNotConfigured("Unsupported routing provider.")
    if resolved.environment in DEPLOYED_ENVIRONMENTS and resolved.provider != "mapbox":
        raise RoutingProviderNotConfigured(
            "Staging and production require an external routing provider."
        )
    if resolved.traffic_mode not in SUPPORTED_TRAFFIC_MODES:
        raise RoutingProviderNotConfigured("Unsupported routing traffic mode.")
    try:
        timeout_seconds = float(resolved.timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise RoutingProviderNotConfigured("Routing timeout is invalid.") from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise RoutingProviderNotConfigured("Routing timeout must be positive.")
    if resolved.route_cache_enabled and resolved.route_cache_ttl_hours <= 0:
        raise RoutingProviderNotConfigured("Route cache TTL must be positive.")
    if resolved.route_cache_enabled and resolved.departure_bucket_minutes <= 0:
        raise RoutingProviderNotConfigured("Route cache departure bucket must be positive.")

    if resolved.provider in {"haversine", "mock", "local"}:
        provider: BaseRoutingProvider = HaversineRoutingProvider()
    elif resolved.provider == "mapbox":
        provider = MapboxRoutingProvider(
            access_token=resolved.mapbox_access_token,
            timeout_seconds=timeout_seconds,
            traffic_mode=resolved.traffic_mode,
        )
    else:
        raise RoutingProviderNotConfigured("Unsupported routing provider.")

    if resolved.route_cache_enabled:
        return CachingRoutingProvider(
            provider=provider,
            cache=DatabaseRouteEstimateCache(),
            ttl_hours=resolved.route_cache_ttl_hours,
            bucket_minutes=resolved.departure_bucket_minutes,
            traffic_mode=resolved.traffic_mode,
        )
    return provider
