import json
import os
import urllib.request

import pytest


BASE_URL = os.getenv("SWIM_E2E_BASE_URL", "").rstrip("/")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not BASE_URL,
        reason="Set SWIM_E2E_BASE_URL, for example http://127.0.0.1:8000, to run API e2e smoke tests.",
    ),
]


def test_running_api_health_endpoint():
    assert _get_json("/health") == {"status": "ok"}


def test_running_api_exposes_frontend_contract_routes():
    openapi = _get_json("/openapi.json")
    paths = set(openapi["paths"].keys())

    assert "/api/coach/schedule" in paths
    assert "/api/coach/availability" in paths
    assert "/api/public/client-invites/{token}" in paths
    assert "/api/public/client-invites/{token}/slots" in paths
    assert "/api/public/client-invites/{token}/lesson-requests" in paths


def _get_json(path: str) -> dict:
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        headers={"accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))
