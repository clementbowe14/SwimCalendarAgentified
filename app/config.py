import math
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from app.routing import RoutingProviderNotConfigured


SUPPORTED_ROUTING_PROVIDERS = {"haversine", "mock", "local", "mapbox"}
SUPPORTED_TRAFFIC_MODES = {"none", "typical", "live"}
SUPPORTED_ENVIRONMENTS = {"local", "development", "test", "staging", "production"}
LOCAL_ENVIRONMENTS = {"local", "development", "test"}
DEPLOYED_ENVIRONMENTS = {"staging", "production"}
DEFAULT_COACH_SESSION_TTL_SECONDS = 8 * 60 * 60
MIN_COACH_SESSION_TTL_SECONDS = 5 * 60
MAX_COACH_SESSION_TTL_SECONDS = 24 * 60 * 60
DEFAULT_CORS_ORIGINS = (
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "http://127.0.0.1:3001",
    "http://localhost:3001",
    "http://127.0.0.1:3002",
    "http://localhost:3002",
    "http://127.0.0.1:4173",
    "http://localhost:4173",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:5174",
    "http://localhost:5174",
)


@dataclass(frozen=True)
class RoutingSettings:
    provider: str = "haversine"
    traffic_mode: str = "none"
    timeout_seconds: float = 3.0
    route_cache_enabled: bool = False
    route_cache_ttl_hours: int = 24
    departure_bucket_minutes: int = 15
    mapbox_access_token: Optional[str] = None
    environment: str = "local"


@dataclass(frozen=True)
class ApplicationSettings:
    environment: str
    coach_session_ttl_seconds: int
    cookie_secure: bool
    cors_origins: tuple[str, ...]
    demo_data_enabled: bool
    demo_auth_enabled: bool


def demo_data_access_enabled_from_env() -> bool:
    """Fail closed for direct data access without validating unrelated settings."""

    environment = os.getenv("SWIM_ENV", "").strip().lower()
    return (
        environment in LOCAL_ENVIRONMENTS
        and _strict_bool_env("SWIM_ENABLE_DEMO_DATA", False)
    )


def application_settings_from_env() -> ApplicationSettings:
    environment = os.getenv("SWIM_ENV", "").strip().lower()
    if environment not in SUPPORTED_ENVIRONMENTS:
        supported = ", ".join(sorted(SUPPORTED_ENVIRONMENTS))
        raise RuntimeError(f"SWIM_ENV is required and must be one of: {supported}.")

    ttl_seconds = _int_env(
        "SWIM_COACH_SESSION_TTL_SECONDS",
        DEFAULT_COACH_SESSION_TTL_SECONDS,
    )
    if not MIN_COACH_SESSION_TTL_SECONDS <= ttl_seconds <= MAX_COACH_SESSION_TTL_SECONDS:
        raise RuntimeError(
            "SWIM_COACH_SESSION_TTL_SECONDS must be between "
            f"{MIN_COACH_SESSION_TTL_SECONDS} and {MAX_COACH_SESSION_TTL_SECONDS}."
        )

    configured_secure = os.getenv("SWIM_COACH_COOKIE_SECURE")
    cookie_secure = environment in DEPLOYED_ENVIRONMENTS
    if configured_secure is not None:
        cookie_secure = _strict_bool_value("SWIM_COACH_COOKIE_SECURE", configured_secure)
    if environment in DEPLOYED_ENVIRONMENTS and not cookie_secure:
        raise RuntimeError("Secure coach session cookies cannot be disabled in staging or production.")

    configured_origins = os.getenv("SWIM_API_CORS_ORIGINS")
    if environment in DEPLOYED_ENVIRONMENTS and not configured_origins:
        raise RuntimeError("SWIM_API_CORS_ORIGINS must be configured in staging and production.")
    cors_origins = tuple(
        origin.strip()
        for origin in (configured_origins.split(",") if configured_origins else DEFAULT_CORS_ORIGINS)
        if origin.strip()
    )
    if not cors_origins:
        raise RuntimeError("SWIM_API_CORS_ORIGINS must contain at least one origin.")
    if "*" in cors_origins and environment not in LOCAL_ENVIRONMENTS:
        raise RuntimeError("Wildcard credentialed CORS origins are forbidden in staging and production.")
    for origin in cors_origins:
        if origin == "*":
            continue
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError("CORS origins must be exact HTTP(S) origins without paths or credentials.")
        if environment in DEPLOYED_ENVIRONMENTS and parsed.scheme != "https":
            raise RuntimeError("CORS origins must use HTTPS in staging and production.")

    demo_data_enabled = _strict_bool_env("SWIM_ENABLE_DEMO_DATA", False)
    demo_auth_enabled = _strict_bool_env("SWIM_ENABLE_DEMO_AUTH", False)
    if environment in DEPLOYED_ENVIRONMENTS and (demo_data_enabled or demo_auth_enabled):
        raise RuntimeError("Demo data and demo authentication are forbidden in staging and production.")
    if demo_auth_enabled and not demo_data_enabled:
        raise RuntimeError("SWIM_ENABLE_DEMO_AUTH requires SWIM_ENABLE_DEMO_DATA=true.")

    return ApplicationSettings(
        environment=environment,
        coach_session_ttl_seconds=ttl_seconds,
        cookie_secure=cookie_secure,
        cors_origins=cors_origins,
        demo_data_enabled=demo_data_enabled,
        demo_auth_enabled=demo_auth_enabled,
    )


def routing_settings_from_env(environment: Optional[str] = None) -> RoutingSettings:
    resolved_environment = (
        environment if environment is not None else os.getenv("SWIM_ENV", "local")
    ).strip().lower()
    provider = os.getenv("SWIM_ROUTING_PROVIDER", "haversine").strip().lower()
    traffic_mode = os.getenv("SWIM_ROUTING_TRAFFIC_MODE", "none").strip().lower()
    timeout_seconds = _routing_float_env("SWIM_ROUTING_TIMEOUT_SECONDS", 3.0)
    route_cache_enabled = _routing_bool_env("SWIM_ROUTE_CACHE_ENABLED", False)
    route_cache_ttl_hours = _routing_int_env("SWIM_ROUTE_CACHE_TTL_HOURS", 24)
    departure_bucket_minutes = _routing_int_env("SWIM_ROUTE_DEPARTURE_BUCKET_MINUTES", 15)

    if resolved_environment not in SUPPORTED_ENVIRONMENTS:
        raise RoutingProviderNotConfigured("Unsupported routing environment.")
    if provider not in SUPPORTED_ROUTING_PROVIDERS:
        raise RoutingProviderNotConfigured("Unsupported routing provider.")
    if resolved_environment in DEPLOYED_ENVIRONMENTS and provider != "mapbox":
        raise RoutingProviderNotConfigured(
            "Staging and production require an external routing provider."
        )
    if traffic_mode not in SUPPORTED_TRAFFIC_MODES:
        raise RoutingProviderNotConfigured("Unsupported routing traffic mode.")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise RoutingProviderNotConfigured("Routing timeout must be positive.")
    if route_cache_enabled and route_cache_ttl_hours <= 0:
        raise RoutingProviderNotConfigured("Route cache TTL must be positive.")
    if route_cache_enabled and departure_bucket_minutes <= 0:
        raise RoutingProviderNotConfigured("Route cache departure bucket must be positive.")

    configured_mapbox_token = os.getenv("SWIM_MAPBOX_ACCESS_TOKEN")
    mapbox_access_token = (
        configured_mapbox_token.strip()
        if configured_mapbox_token and configured_mapbox_token.strip()
        else None
    )

    return RoutingSettings(
        environment=resolved_environment,
        provider=provider,
        traffic_mode=traffic_mode,
        timeout_seconds=timeout_seconds,
        route_cache_enabled=route_cache_enabled,
        route_cache_ttl_hours=route_cache_ttl_hours,
        departure_bucket_minutes=departure_bucket_minutes,
        mapbox_access_token=mapbox_access_token,
    )


def _routing_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RoutingProviderNotConfigured("Route cache enabled must be a boolean.")


def _routing_int_env(name: str, default: int) -> int:
    try:
        return _int_env(name, default)
    except ValueError as exc:
        raise RoutingProviderNotConfigured("Routing integer setting is invalid.") from exc


def _routing_float_env(name: str, default: float) -> float:
    try:
        return _float_env(name, default)
    except ValueError as exc:
        raise RoutingProviderNotConfigured("Routing timeout is invalid.") from exc


def _strict_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return _strict_bool_value(name, value)


def _strict_bool_value(name: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean value.")


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
