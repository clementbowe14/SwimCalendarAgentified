from fastapi.testclient import TestClient
import pytest

from app.api import create_app
from app.api_errors import CalendarLinksUnavailable, LessonRequestNotFound
from app.routes import public


@pytest.mark.parametrize("path,handler", [
    ("/api/public/lesson-requests/opaque-token", "get_client_lesson_request_status"),
    ("/api/public/calendar/opaque-token.ics", "get_client_calendar_ics"),
])
def test_public_errors_are_private_and_do_not_echo_internal_failures(monkeypatch, path, handler):
    def fail(_):
        raise RuntimeError("provider-secret recipient@example.com")

    monkeypatch.setattr(public, handler, fail)
    client = TestClient(create_app(), raise_server_exceptions=False)
    response = client.get(path)
    assert response.status_code == 500
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.json()["error"]["code"] == "INTERNAL_SERVER_ERROR"
    assert "provider-secret" not in response.text
    assert "recipient@" not in response.text


def test_public_domain_errors_are_private(monkeypatch):
    def fail(_):
        raise LessonRequestNotFound()

    monkeypatch.setattr(public, "get_client_lesson_request_status", fail)
    client = TestClient(create_app())
    response = client.get("/api/public/lesson-requests/opaque-token")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_public_validation_redirect_and_calendar_responses_are_private(monkeypatch):
    def unavailable(_):
        raise CalendarLinksUnavailable()

    monkeypatch.setattr(public, "get_client_calendar_links", unavailable)
    monkeypatch.setattr(public, "get_client_calendar_ics", lambda _: "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    client = TestClient(create_app())
    responses = [
        (client.post("/api/public/client-invites/token/slots", json={}), 422),
        (client.get("/api/public/lesson-requests/token/calendar-links/", follow_redirects=False), 307),
        (client.get("/api/public/lesson-requests/token/calendar-links"), 409),
        (client.get("/api/public/calendar/token.ics"), 200),
    ]
    for response, status in responses:
        assert response.status_code == status
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
