import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple


@dataclass(frozen=True)
class RouteEstimate:
    distance_miles: float
    travel_time_mins: int
    duration_seconds: int
    provider: str


class RoutingError(Exception):
    pass


class GeocodeFailed(RoutingError):
    pass


class RouteEstimateFailed(RoutingError):
    pass


class RoutingTimeout(RouteEstimateFailed):
    pass


class RoutingProviderUnavailable(RouteEstimateFailed):
    pass


class RoutingProviderNotConfigured(RoutingError):
    pass

class BaseRoutingProvider(ABC):
    provider_name = "base"

    @abstractmethod
    def estimate_route(
        self,
        origin_coords: Tuple[float, float],
        dest_coords: Tuple[float, float],
        departure_time: Optional[datetime] = None,
        traffic_mode: str = "none",
    ) -> RouteEstimate:
        """
        Returns a route estimate for the requested departure context.
        """
        pass

    def get_distance_and_travel_time(
        self,
        origin_coords: Tuple[float, float],
        dest_coords: Tuple[float, float],
        departure_time: Optional[datetime] = None,
        traffic_mode: str = "none",
    ) -> Tuple[float, int]:
        estimate = self.estimate_route(
            origin_coords,
            dest_coords,
            departure_time=departure_time,
            traffic_mode=traffic_mode,
        )
        return estimate.distance_miles, estimate.travel_time_mins

    @abstractmethod
    def geocode(self, address: str) -> Tuple[float, float]:
        """
        Returns:
            Tuple[float, float]: (lat, lng)
        """
        pass

class HaversineRoutingProvider(BaseRoutingProvider):
    """
    Offline Haversine distance calculator with estimated driving speed factor.
    Assumes average urban driving speed of 25 mph.
    """
    provider_name = "haversine"

    def __init__(self, avg_speed_mph: float = 25.0):
        self.avg_speed_mph = avg_speed_mph

    def estimate_route(
        self,
        origin_coords: Tuple[float, float],
        dest_coords: Tuple[float, float],
        departure_time: Optional[datetime] = None,
        traffic_mode: str = "none",
    ) -> RouteEstimate:
        lat1, lon1 = origin_coords
        lat2, lon2 = dest_coords

        if (lat1, lon1) == (lat2, lon2):
            return RouteEstimate(
                distance_miles=0.0,
                travel_time_mins=0,
                duration_seconds=0,
                provider=self.provider_name,
            )

        # Haversine formula
        R = 3958.8 # Earth radius in miles

        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2 +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
             math.sin(dlon / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        straight_distance = R * c

        # Road curvature multiplier (~1.3x straight line)
        road_distance = straight_distance * 1.3
        
        # Calculate travel time in minutes
        travel_time_hours = road_distance / self.avg_speed_mph
        travel_time_mins = math.ceil(travel_time_hours * 60)

        rounded_distance = round(road_distance, 2)
        travel_time_mins = max(1, travel_time_mins)
        return RouteEstimate(
            distance_miles=rounded_distance,
            travel_time_mins=travel_time_mins,
            duration_seconds=travel_time_mins * 60,
            provider=self.provider_name,
        )

    def geocode(self, address: str) -> Tuple[float, float]:
        """
        Deterministic mock geocoder for standard address inputs during MVP testing.
        Maps known address string keywords or generates hash-based coords.
        """
        address_lower = address.lower()
        if "main" in address_lower or "instructor" in address_lower:
            return (30.2672, -97.7431) # Downtown Austin Base
        elif "north" in address_lower or "north pool" in address_lower:
            return (30.3300, -97.7200) # ~5 miles north
        elif "west" in address_lower or "west pool" in address_lower:
            return (30.2700, -97.8200) # ~8 miles west
        elif "far" in address_lower or "suburb" in address_lower:
            return (30.4500, -97.7900) # ~14 miles north-west
        else:
            # Deterministic pseudo-coords based on string hash for testing
            val = sum(ord(c) for c in address)
            offset_lat = (val % 100) / 1000.0
            offset_lng = (val % 80) / 1000.0
            return (30.2672 + offset_lat, -97.7431 + offset_lng)
