import datetime
import math
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Protocol, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import db, timezones
from app.models import RouteEstimateDB
from app.routing import BaseRoutingProvider, RouteEstimate, RoutingProviderNotConfigured


NO_DEPARTURE_BUCKET = datetime.datetime(1970, 1, 1)
SUPPORTED_TRAFFIC_MODES = {"none", "typical", "live"}
route_cache_session_factory: ContextVar[Callable[[], Session] | None] = ContextVar(
    "route_cache_session_factory", default=None,
)


@dataclass(frozen=True)
class RouteCacheKey:
    provider: str
    origin_key: str
    destination_key: str
    departure_bucket_start: datetime.datetime
    traffic_mode: str


class RouteEstimateCache(Protocol):
    def get(self, key: RouteCacheKey, now: datetime.datetime) -> Optional[RouteEstimate]:
        ...

    def set(
        self,
        key: RouteCacheKey,
        estimate: RouteEstimate,
        origin_coords: Tuple[float, float],
        destination_coords: Tuple[float, float],
        expires_at: datetime.datetime,
    ) -> RouteEstimate:
        ...


class InMemoryRouteEstimateCache:
    def __init__(self):
        self._entries: Dict[RouteCacheKey, tuple[RouteEstimate, datetime.datetime]] = {}

    def get(self, key: RouteCacheKey, now: datetime.datetime) -> Optional[RouteEstimate]:
        now = timezones.to_utc_storage(now)
        cached = self._entries.get(key)
        if not cached:
            return None
        estimate, expires_at = cached
        if expires_at <= now:
            self._entries.pop(key, None)
            return None
        return estimate

    def set(
        self,
        key: RouteCacheKey,
        estimate: RouteEstimate,
        origin_coords: Tuple[float, float],
        destination_coords: Tuple[float, float],
        expires_at: datetime.datetime,
    ) -> RouteEstimate:
        expires_at = timezones.to_utc_storage(expires_at)
        self._entries[key] = (estimate, expires_at)
        return estimate


class DatabaseRouteEstimateCache:
    def get(self, key: RouteCacheKey, now: datetime.datetime) -> Optional[RouteEstimate]:
        now = timezones.to_utc_storage(now)
        session = (route_cache_session_factory.get() or db.SessionLocal)()
        try:
            row = _query_cache_row(session, key)
            if row is None or row.expires_at <= now:
                return None
            return _row_to_estimate(row)
        finally:
            session.close()

    def set(
        self,
        key: RouteCacheKey,
        estimate: RouteEstimate,
        origin_coords: Tuple[float, float],
        destination_coords: Tuple[float, float],
        expires_at: datetime.datetime,
    ) -> RouteEstimate:
        expires_at = timezones.to_utc_storage(expires_at)
        session = (route_cache_session_factory.get() or db.SessionLocal)()
        try:
            row = RouteEstimateDB(
                id=f"route_{uuid.uuid4().hex[:12]}",
                provider=key.provider,
                origin_lat=origin_coords[0],
                origin_lng=origin_coords[1],
                destination_lat=destination_coords[0],
                destination_lng=destination_coords[1],
                origin_key=key.origin_key,
                destination_key=key.destination_key,
                departure_bucket_start=key.departure_bucket_start,
                traffic_mode=key.traffic_mode,
                distance_miles=estimate.distance_miles,
                travel_time_mins=estimate.travel_time_mins,
                duration_seconds=estimate.duration_seconds,
                expires_at=expires_at,
            )
            session.add(row)
            session.commit()
            return estimate
        except IntegrityError:
            session.rollback()
            existing = _query_cache_row(session, key)
            now = timezones.to_utc_storage(datetime.datetime.utcnow())
            if existing is not None and existing.expires_at > now:
                return _row_to_estimate(existing)
            if existing is not None:
                existing.origin_lat = origin_coords[0]
                existing.origin_lng = origin_coords[1]
                existing.destination_lat = destination_coords[0]
                existing.destination_lng = destination_coords[1]
                existing.distance_miles = estimate.distance_miles
                existing.travel_time_mins = estimate.travel_time_mins
                existing.duration_seconds = estimate.duration_seconds
                existing.expires_at = expires_at
                existing.updated_at = now
                session.commit()
                return estimate
            raise
        finally:
            session.close()


class CachingRoutingProvider(BaseRoutingProvider):
    def __init__(
        self,
        provider: BaseRoutingProvider,
        cache: RouteEstimateCache,
        ttl_hours: int = 24,
        bucket_minutes: int = 15,
        traffic_mode: str = "none",
        clock: Callable[[], datetime.datetime] = datetime.datetime.utcnow,
    ):
        if ttl_hours <= 0:
            raise RoutingProviderNotConfigured("Route cache TTL must be positive.")
        if bucket_minutes <= 0:
            raise RoutingProviderNotConfigured("Route cache departure bucket must be positive.")
        if traffic_mode not in SUPPORTED_TRAFFIC_MODES:
            raise RoutingProviderNotConfigured("Unsupported routing traffic mode.")
        self.provider = provider
        self.cache = cache
        self.ttl_hours = ttl_hours
        self.bucket_minutes = bucket_minutes
        self.traffic_mode = traffic_mode
        self.provider_name = provider.provider_name
        self.clock = clock

    def estimate_route(
        self,
        origin_coords: Tuple[float, float],
        dest_coords: Tuple[float, float],
        departure_time: Optional[datetime.datetime] = None,
        traffic_mode: str = "none",
    ) -> RouteEstimate:
        resolved_traffic_mode = traffic_mode if traffic_mode != "none" else self.traffic_mode
        if resolved_traffic_mode not in SUPPORTED_TRAFFIC_MODES:
            raise RoutingProviderNotConfigured("Unsupported routing traffic mode.")
        if departure_time is None and resolved_traffic_mode in {"typical", "live"}:
            return self.provider.estimate_route(
                origin_coords,
                dest_coords,
                departure_time=None,
                traffic_mode=resolved_traffic_mode,
            )

        departure_bucket = bucket_departure_time(departure_time, self.bucket_minutes)
        key = RouteCacheKey(
            provider=self.provider.provider_name,
            origin_key=coordinate_key(origin_coords),
            destination_key=coordinate_key(dest_coords),
            departure_bucket_start=departure_bucket,
            traffic_mode=resolved_traffic_mode,
        )
        now = timezones.to_utc_storage(self.clock())
        cached = self.cache.get(key, now)
        if cached:
            return cached

        estimate = self.provider.estimate_route(
            origin_coords,
            dest_coords,
            departure_time=_provider_departure_time(departure_bucket),
            traffic_mode=resolved_traffic_mode,
        )
        expires_at = now + datetime.timedelta(hours=self.ttl_hours)
        return self.cache.set(key, estimate, origin_coords, dest_coords, expires_at)

    def get_distance_and_travel_time_at_exact_departure(
        self,
        origin_coords: Tuple[float, float],
        dest_coords: Tuple[float, float],
        *,
        departure_time: datetime.datetime,
    ) -> Tuple[float, int]:
        """Quote the requested departure without bucketing traffic estimates."""
        if self.traffic_mode == "none":
            return self.get_distance_and_travel_time(
                origin_coords, dest_coords, departure_time=departure_time
            )
        return self.provider.get_distance_and_travel_time(
            origin_coords,
            dest_coords,
            departure_time=departure_time,
            traffic_mode=self.traffic_mode,
        )

    def geocode(self, address: str) -> Tuple[float, float]:
        return self.provider.geocode(address)


def coordinate_key(coords: Tuple[float, float]) -> str:
    return f"{_coordinate_component(coords[0]):.5f},{_coordinate_component(coords[1]):.5f}"


def bucket_departure_time(
    departure_time: Optional[datetime.datetime],
    bucket_minutes: int,
) -> datetime.datetime:
    if bucket_minutes <= 0:
        raise RoutingProviderNotConfigured("Route cache departure bucket must be positive.")
    if departure_time is None:
        return NO_DEPARTURE_BUCKET
    departure_time = timezones.to_utc_storage(departure_time)
    discard = datetime.timedelta(
        minutes=departure_time.minute % bucket_minutes,
        seconds=departure_time.second,
        microseconds=departure_time.microsecond,
    )
    return departure_time - discard


def _coordinate_component(value: float) -> float:
    rounded = round(value, 5)
    if rounded == 0 or math.isclose(rounded, 0.0, abs_tol=0.000005):
        return 0.0
    return rounded


def _provider_departure_time(departure_bucket: datetime.datetime) -> Optional[datetime.datetime]:
    if departure_bucket == NO_DEPARTURE_BUCKET:
        return None
    return timezones.storage_to_aware_utc(departure_bucket)


def _query_cache_row(session, key: RouteCacheKey) -> Optional[RouteEstimateDB]:
    return (
        session.query(RouteEstimateDB)
        .filter_by(
            provider=key.provider,
            origin_key=key.origin_key,
            destination_key=key.destination_key,
            departure_bucket_start=key.departure_bucket_start,
            traffic_mode=key.traffic_mode,
        )
        .first()
    )


def _row_to_estimate(row: RouteEstimateDB) -> RouteEstimate:
    return RouteEstimate(
        distance_miles=row.distance_miles,
        travel_time_mins=row.travel_time_mins,
        duration_seconds=row.duration_seconds,
        provider=row.provider,
    )
