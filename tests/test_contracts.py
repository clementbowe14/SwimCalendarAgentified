import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from app.api_schemas import (
    AvailabilitySearchResponse,
    BookLessonResponse,
    ClientInviteAvailabilityResponse,
    ClientInviteCalendarLinks,
    ClientInviteEnvelopeResponse,
    ClientInviteLessonRequestResponse,
    CoachCommandCenterSnapshot,
    CoachEmailDeliveryResponse,
    CoachAuthResponse,
    CoachLessonRequestConfirmResponse,
    CoachScheduleResponse,
    CoachSchedulerRules,
    ErrorResponse,
    InviteNotificationResponse,
)
from scripts.export_contracts import CONTRACT_ROOT, check_contracts


FIXTURE_SCHEMAS = {
    "fixtures/public/client_invite_envelope.json": ClientInviteEnvelopeResponse,
    "fixtures/public/availability_search_response.json": ClientInviteAvailabilityResponse,
    "fixtures/public/lesson_request_requested.json": ClientInviteLessonRequestResponse,
    "fixtures/public/lesson_request_confirmed.json": ClientInviteLessonRequestResponse,
    "fixtures/public/lesson_request_needs_new_times.json": ClientInviteLessonRequestResponse,
    "fixtures/public/calendar_links.json": ClientInviteCalendarLinks,
    "fixtures/public/error_invalid_token.json": ErrorResponse,
    "fixtures/coach/auth_response.json": CoachAuthResponse,
    "fixtures/coach/calendar_links.json": ClientInviteCalendarLinks,
    "fixtures/coach/booking_response.json": BookLessonResponse,
    "fixtures/coach/email_delivery.json": CoachEmailDeliveryResponse,
    "fixtures/coach/invite_notification_response.json": InviteNotificationResponse,
    "fixtures/coach/lesson_request_confirmation_response.json": CoachLessonRequestConfirmResponse,
    "fixtures/coach/availability_search_response.json": AvailabilitySearchResponse,
    "fixtures/coach/schedule_response.json": CoachScheduleResponse,
    "fixtures/coach/settings.json": CoachSchedulerRules,
    "fixtures/coach/command_center_snapshot.json": CoachCommandCenterSnapshot,
}
FORBIDDEN_PUBLIC_KEYS = {"instructor_id", "client_id"}
FORBIDDEN_PUBLIC_VALUE_PREFIXES = ("app_",)
FORBIDDEN_PUBLIC_VALUES = {"Alice", "Bob", "client_alice", "client_bob", "inst_001"}


def test_committed_contract_artifacts_are_current():
    assert check_contracts() == []


def test_openapi_contract_includes_public_and_coach_routes():
    openapi = _load_json(CONTRACT_ROOT / "openapi.json")
    paths = set(openapi["paths"].keys())

    assert "/api/public/client-invites/{token}" in paths
    assert "/api/public/client-invites/{token}/slots" in paths
    assert "/api/public/lesson-requests/{request_token}/calendar-links" in paths
    assert "/api/auth/coach/sign-in" in paths
    assert "/api/auth/coach/session" in paths
    assert "/api/auth/coach/sign-out" in paths
    assert "/api/coach/availability" in paths
    assert "/api/coach/command-center/snapshot" in paths
    assert "/api/coach/appointments/{appointment_id}/calendar-links" in paths
    assert "/api/coach/appointments/{appointment_id}/calendar.ics" in paths
    assert "text/calendar" in openapi["paths"][
        "/api/coach/appointments/{appointment_id}/calendar.ics"
    ]["get"]["responses"]["200"]["content"]
    assert "/api/coach/lesson-requests/{request_token}/approve" in paths
    assert "/api/coach/lesson-requests/{request_token}/alternatives" in paths
    assert "/api/coach/lesson-requests/{request_token}/decline" in paths
    assert "/api/coach/settings" in paths
    assert "/api/coach/invites/{invite_token}/notifications" in paths
    assert "/api/coach/email-deliveries" in paths
    assert "/api/coach/email-deliveries/{delivery_id}/resend" in paths
    assert "/api/coach/email-deliveries/{delivery_id}/contact" in paths
    assert "/api/public/command-center/snapshot" not in paths
    assert "/api/public/settings" not in paths


def test_fixtures_validate_against_response_schemas():
    for relative_path, schema in FIXTURE_SCHEMAS.items():
        schema.model_validate(_load_json(CONTRACT_ROOT / relative_path))
    TypeAdapter(list[CoachEmailDeliveryResponse]).validate_python(
        _load_json(CONTRACT_ROOT / "fixtures/coach/email_deliveries.json")
    )


def test_coach_email_delivery_fixture_is_allowlisted_and_privacy_safe():
    payload = _load_json(CONTRACT_ROOT / "fixtures/coach/email_delivery.json")
    assert set(payload) == {
        "id",
        "kind",
        "status",
        "updated_at",
        "client_id",
        "client_name",
        "request_token",
        "issue",
        "source_state",
        "can_resend",
        "can_edit_contact",
    }
    assert {
        "recipient",
        "failure_code",
        "provider_message_id",
        "template_context",
        "attempt_count",
        "next_attempt_at",
        "lease_expires_at",
        "attempt_token",
    }.isdisjoint(payload)


def test_confirmation_fixture_has_safe_delivery_states_and_occurrence_links():
    payload = _load_json(CONTRACT_ROOT / "fixtures/public/lesson_request_confirmed.json")
    assert [item["channel"] for item in payload["notifications"]] == ["sms", "email"]
    for item in payload["notifications"]:
        assert set(item) == {"kind", "channel", "status", "updated_at"}
    links = payload["confirmation"]["calendar_links"]
    assert links["google_events"][0]["url"] == links["google"]


def test_public_fixtures_do_not_leak_private_identifiers_or_schedule_details():
    for path in (CONTRACT_ROOT / "fixtures" / "public").glob("*.json"):
        payload = _load_json(path)
        keys = _flatten_keys(payload)
        values = _flatten_values(payload)
        assert FORBIDDEN_PUBLIC_KEYS.isdisjoint(keys), path
        assert FORBIDDEN_PUBLIC_VALUES.isdisjoint(values), path
        assert not any(
            isinstance(value, str) and value.startswith(FORBIDDEN_PUBLIC_VALUE_PREFIXES)
            for value in values
        ), path


def test_required_fixture_files_exist_for_frontend_contract_tests():
    required = set(FIXTURE_SCHEMAS.keys()) | {"fixtures/coach/email_deliveries.json"}

    for relative_path in required:
        assert (CONTRACT_ROOT / relative_path).exists()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _flatten_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.add(key)
            keys.update(_flatten_keys(nested))
    elif isinstance(value, list):
        for item in value:
            keys.update(_flatten_keys(item))
    return keys


def _flatten_values(value: Any) -> set[Any]:
    values = set()
    if isinstance(value, dict):
        for nested in value.values():
            values.update(_flatten_values(nested))
    elif isinstance(value, list):
        for item in value:
            values.update(_flatten_values(item))
    elif isinstance(value, (str, int, float, bool, type(None))):
        values.add(value)
    return values
