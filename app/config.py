import os
from dataclasses import dataclass
from typing import Optional

from app.routing import RoutingProviderNotConfigured


SUPPORTED_ROUTING_PROVIDERS = {"haversine", "mock", "local", "mapbox"}
SUPPORTED_TRAFFIC_MODES = {"none", "typical", "live"}


@dataclass(frozen=True)
class RoutingSettings:
    provider: str = "haversine"
    traffic_mode: str = "none"
    timeout_seconds: float = 3.0
    route_cache_enabled: bool = False
    route_cache_ttl_hours: int = 24
    departure_bucket_minutes: int = 15
    mapbox_access_token: Optional[str] = None


def routing_settings_from_env() -> RoutingSettings:
    provider = os.getenv("SWIM_ROUTING_PROVIDER", "haversine").strip().lower()
    traffic_mode = os.getenv("SWIM_ROUTING_TRAFFIC_MODE", "none").strip().lower()
    timeout_seconds = _float_env("SWIM_ROUTING_TIMEOUT_SECONDS", 3.0)
    route_cache_enabled = _bool_env("SWIM_ROUTE_CACHE_ENABLED", False)
    route_cache_ttl_hours = _int_env("SWIM_ROUTE_CACHE_TTL_HOURS", 24)
    departure_bucket_minutes = _int_env("SWIM_ROUTE_DEPARTURE_BUCKET_MINUTES", 15)

    if provider not in SUPPORTED_ROUTING_PROVIDERS:
        raise RoutingProviderNotConfigured("Unsupported routing provider.")
    if traffic_mode not in SUPPORTED_TRAFFIC_MODES:
        raise RoutingProviderNotConfigured("Unsupported routing traffic mode.")
    if timeout_seconds <= 0:
        raise RoutingProviderNotConfigured("Routing timeout must be positive.")
    if route_cache_enabled and route_cache_ttl_hours <= 0:
        raise RoutingProviderNotConfigured("Route cache TTL must be positive.")
    if route_cache_enabled and departure_bucket_minutes <= 0:
        raise RoutingProviderNotConfigured("Route cache departure bucket must be positive.")

    return RoutingSettings(
        provider=provider,
        traffic_mode=traffic_mode,
        timeout_seconds=timeout_seconds,
        route_cache_enabled=route_cache_enabled,
        route_cache_ttl_hours=route_cache_ttl_hours,
        departure_bucket_minutes=departure_bucket_minutes,
        mapbox_access_token=os.getenv("SWIM_MAPBOX_ACCESS_TOKEN"),
    )


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    return float(value)
