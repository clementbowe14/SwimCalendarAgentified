import json
import math
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Callable, Optional, Tuple

from app import timezones
from app.routing import (
    BaseRoutingProvider,
    GeocodeFailed,
    RouteEstimate,
    RouteEstimateFailed,
    RoutingProviderNotConfigured,
    RoutingProviderUnavailable,
    RoutingTimeout,
)


JsonFetcher = Callable[[str, float], dict]
GEOCODE_OPERATION = "geocode"
DIRECTIONS_OPERATION = "directions"


class MapboxRoutingProvider(BaseRoutingProvider):
    provider_name = "mapbox"

    def __init__(
        self,
        access_token: Optional[str],
        timeout_seconds: float = 3.0,
        fetch_json: Optional[JsonFetcher] = None,
    ):
        if not access_token:
            raise RoutingProviderNotConfigured("SWIM_MAPBOX_ACCESS_TOKEN is required for Mapbox routing.")
        self.access_token = access_token
        self.timeout_seconds = timeout_seconds
        self._fetch_json = fetch_json or self._urllib_fetch_json

    def geocode(self, address: str) -> Tuple[float, float]:
        if not address.strip():
            raise GeocodeFailed("Address is required.")
        encoded_address = urllib.parse.quote(address)
        query = urllib.parse.urlencode({"access_token": self.access_token, "limit": 1})
        url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{encoded_address}.json?{query}"
        payload = self._fetch(url, GEOCODE_OPERATION)
        features = payload.get("features") or []
        if not isinstance(features, list):
            raise RoutingProviderUnavailable("Mapbox geocoding response is malformed.")
        if not features:
            raise GeocodeFailed("Mapbox returned no geocoding matches.")
        if not isinstance(features[0], dict):
            raise RoutingProviderUnavailable("Mapbox geocoding response is malformed.")
        center = features[0].get("center") or []
        if len(center) < 2:
            raise RoutingProviderUnavailable("Mapbox geocoding response is malformed.")
        try:
            lng, lat = float(center[0]), float(center[1])
        except (TypeError, ValueError) as exc:
            raise RoutingProviderUnavailable("Mapbox geocoding response is malformed.") from exc
        if not _valid_coordinate(lat, lng):
            raise RoutingProviderUnavailable("Mapbox geocoding response is malformed.")
        return lat, lng

    def estimate_route(
        self,
        origin_coords: Tuple[float, float],
        dest_coords: Tuple[float, float],
        departure_time: Optional[datetime] = None,
        traffic_mode: str = "none",
    ) -> RouteEstimate:
        profile = "mapbox/driving-traffic" if traffic_mode in {"typical", "live"} else "mapbox/driving"
        coordinates = (
            f"{origin_coords[1]},{origin_coords[0]};"
            f"{dest_coords[1]},{dest_coords[0]}"
        )
        params = {"access_token": self.access_token, "overview": "false"}
        if profile == "mapbox/driving-traffic" and departure_time is not None:
            params["depart_at"] = timezones.canonical_instant_text(departure_time)
        query = urllib.parse.urlencode(params)
        url = f"https://api.mapbox.com/directions/v5/{profile}/{coordinates}?{query}"
        payload = self._fetch(url, DIRECTIONS_OPERATION)
        routes = payload.get("routes") or []
        if not isinstance(routes, list):
            raise RoutingProviderUnavailable("Mapbox route response is malformed.")
        if not routes:
            raise RouteEstimateFailed("Mapbox returned no routes.")
        route = routes[0]
        if not isinstance(route, dict):
            raise RouteEstimateFailed("Mapbox route response is malformed.")
        try:
            distance_meters = float(route["distance"])
            duration_raw = float(route["duration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RouteEstimateFailed("Mapbox route response is missing distance or duration.") from exc
        if (
            not math.isfinite(distance_meters)
            or not math.isfinite(duration_raw)
            or distance_meters < 0
            or duration_raw < 0
        ):
            raise RouteEstimateFailed("Mapbox route response has invalid distance or duration.")
        duration_seconds = int(math.ceil(duration_raw))
        return RouteEstimate(
            distance_miles=round(distance_meters / 1609.344, 2),
            travel_time_mins=max(1, math.ceil(duration_seconds / 60)),
            duration_seconds=duration_seconds,
            provider=self.provider_name,
        )

    def _fetch(self, url: str, operation: str) -> dict:
        try:
            payload = self._fetch_json(url, self.timeout_seconds)
        except TimeoutError as exc:
            raise RoutingTimeout("Mapbox request timed out.") from exc
        except socket.timeout as exc:
            raise RoutingTimeout("Mapbox request timed out.") from exc
        except json.JSONDecodeError as exc:
            raise RoutingProviderUnavailable("Mapbox returned malformed JSON.") from exc
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise RoutingProviderNotConfigured("Mapbox credentials were rejected.") from exc
            if exc.code == 429 or exc.code >= 500:
                raise RoutingProviderUnavailable("Mapbox is unavailable.") from exc
            if operation == GEOCODE_OPERATION:
                raise RoutingProviderUnavailable("Mapbox geocoding request failed.") from exc
            raise RouteEstimateFailed("Mapbox request failed.") from exc
        except urllib.error.URLError as exc:
            raise RoutingProviderUnavailable("Mapbox request failed.") from exc
        if not isinstance(payload, dict):
            raise RoutingProviderUnavailable("Mapbox returned malformed JSON.")
        return payload

    @staticmethod
    def _urllib_fetch_json(url: str, timeout_seconds: float) -> dict:
        request = urllib.request.Request(url, headers={"accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))


def _valid_coordinate(lat: float, lng: float) -> bool:
    return math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180
