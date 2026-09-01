from fastapi.testclient import TestClient

from app import db, tools
from app.api import create_app
from app.routing import BaseRoutingProvider, GeocodeFailed, RouteEstimate, RoutingTimeout
from tests.conftest import login_coach
from tests.db_test_utils import reset_postgres_database


class FailingGeocodeProvider(BaseRoutingProvider):
    provider_name = "failing-geocode"

    def estimate_route(self, origin_coords, dest_coords, departure_time=None, traffic_mode="none"):
        return RouteEstimate(
            distance_miles=1.0,
            travel_time_mins=5,
            duration_seconds=300,
            provider=self.provider_name,
        )

    def geocode(self, address: str):
        raise GeocodeFailed("No geocode match")


class TimeoutRouteProvider(BaseRoutingProvider):
    provider_name = "timeout-route"

    def estimate_route(self, origin_coords, dest_coords, departure_time=None, traffic_mode="none"):
        raise RoutingTimeout("Route provider timed out")

    def geocode(self, address: str):
        return (30.3300, -97.7200)


def test_coach_availability_geocode_failure_returns_stable_error():
    reset_postgres_database()
    tools.configure_routing_provider(FailingGeocodeProvider())
    try:
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            client.headers.update(login_coach(client))
            response = client.post(
                "/api/coach/availability",
                json={
                    "client_name": "Charlie",
                    "client_address": "Unknown bad address",
                    "requested_date": "2026-08-20",
                    "duration_mins": 60,
                },
            )
    finally:
        tools.reset_routing_provider()
        db.configure_database()

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "GEOCODING_FAILED"


def test_public_availability_route_timeout_returns_stable_error():
    reset_postgres_database()
    tools.configure_routing_provider(TimeoutRouteProvider())
    try:
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            response = client.post(
                "/api/public/invites/demo-invite-token/availability",
                json={
                    "client_name": "Sofia Martinez",
                    "client_address": "450 North Blvd, Austin, TX",
                    "requested_date": "2026-08-20",
                    "location_type": "home",
                    "duration_mins": 45,
                },
            )
    finally:
        tools.reset_routing_provider()
        db.configure_database()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ROUTING_TIMEOUT"
