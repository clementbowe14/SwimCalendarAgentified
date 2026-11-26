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
    CoachBlockerResponse,
    CoachClientListResponse,
    CoachClientProfileResponse,
    CoachLessonRequestConfirmResponse,
    CoachProviderSettingsResponse,
    CoachScheduleResponse,
    CoachSchedulerRules,
    CoachSchedulingPolicyResponse,
    ErrorResponse,
    InviteNotificationResponse,
)
from scripts.export_contracts import CONTRACT_ROOT, check_contracts
from tests.public_contract_safety import PUBLIC_FORBIDDEN_KEYS, flatten_keys


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
    "fixtures/coach/blocker_response.json": CoachBlockerResponse,
    "fixtures/coach/settings.json": CoachSchedulerRules,
    "fixtures/coach/scheduling_policy.json": CoachSchedulingPolicyResponse,
    "fixtures/coach/provider.json": CoachProviderSettingsResponse,
    "fixtures/coach/client.json": CoachClientProfileResponse,
    "fixtures/coach/clients.json": CoachClientListResponse,
    "fixtures/coach/command_center_snapshot.json": CoachCommandCenterSnapshot,
}
FORBIDDEN_PUBLIC_VALUE_PREFIXES = ("app_",)
FORBIDDEN_PUBLIC_VALUES = {"Alice", "Bob", "client_alice", "client_bob", "inst_001"}


def test_committed_contract_artifacts_are_current():
    assert check_contracts() == []


def test_openapi_contract_includes_public_and_coach_routes():
    openapi = _load_json(CONTRACT_ROOT / "openapi.json")
    paths = set(openapi["paths"].keys())

    assert "/ready" in paths
    assert "/api/public/client-invites/{token}" in paths
    assert "/api/public/client-invites/{token}/slots" in paths
    assert "/api/public/lesson-requests/{request_token}/calendar-links" in paths
    assert "/api/auth/coach/sign-in" in paths
    assert "/api/auth/coach/login" in paths
    assert "/api/auth/coach/session" in paths
    assert "/api/auth/coach/sign-out" in paths
    assert "/api/auth/coach/logout" in paths
    assert "/api/coach/availability" in paths
    assert "/api/coach/blockers" in paths
    assert "/api/coach/blockers/{blocker_id}" in paths
    assert "/api/coach/command-center/snapshot" in paths
    assert "/api/coach/calendar-connections/google" in paths
    assert "/api/coach/appointments/{appointment_id}/calendar-links" in paths
    assert "/api/coach/appointments/{appointment_id}/calendar.ics" in paths
    assert "text/calendar" in openapi["paths"][
        "/api/coach/appointments/{appointment_id}/calendar.ics"
    ]["get"]["responses"]["200"]["content"]
    assert "/api/coach/lesson-requests/{request_token}/approve" in paths
    assert "/api/coach/lesson-requests/{request_token}/confirm" in paths
    approve_operation = openapi["paths"][
        "/api/coach/lesson-requests/{request_token}/approve"
    ]["post"]
    confirm_operation = openapi["paths"][
        "/api/coach/lesson-requests/{request_token}/confirm"
    ]["post"]
    assert approve_operation["deprecated"] is True
    assert "requestBody" not in approve_operation
    assert confirm_operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CoachLessonRequestConfirmResponse"
    }
    assert "/api/coach/lesson-requests/{request_token}/alternatives" in paths
    assert "/api/coach/lesson-requests/{request_token}/decline" in paths
    assert "/api/coach/settings" in paths
    assert "/api/coach/scheduling-policy" in paths
    calendar_connections = openapi["paths"]["/api/coach/calendar-connections/google"]
    calendar_status = calendar_connections["get"]
    assert calendar_status["security"] == [{"CoachSessionCookie": []}]
    assert calendar_status["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {
        "$ref": "#/components/schemas/CoachCalendarConnectionResponse"
    }
    disconnect = calendar_connections["delete"]
    assert disconnect["security"] == calendar_status["security"]
    assert disconnect["responses"]["200"] == calendar_status["responses"]["200"]
    assert any(
        parameter["name"] == "X-CSRF-Protection" and parameter["required"]
        for parameter in disconnect["parameters"]
    )
    assert set(
        openapi["components"]["schemas"]["CoachCalendarConnectionResponse"][
            "required"
        ]
    ) == {
        "provider",
        "state",
        "connected_at",
        "updated_at",
        "authorization_required_at",
    }
    assert openapi["paths"]["/api/coach/settings"]["get"]["deprecated"] is True
    legacy_policy_put = openapi["paths"]["/api/coach/settings"]["put"]
    assert legacy_policy_put["deprecated"] is True
    assert "200" not in legacy_policy_put["responses"]
    assert legacy_policy_put["responses"]["410"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    canonical_policy_put = openapi["paths"]["/api/coach/scheduling-policy"]["put"]
    for status in ("409", "422"):
        assert canonical_policy_put["responses"][status]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }
    assert "/api/coach/provider" in paths
    assert "/api/coach/clients" in paths
    assert "/api/coach/clients/{client_id}" in paths
    assert "/api/coach/invites/{invite_token}/notifications" in paths
    assert "/api/coach/email-deliveries" in paths
    assert "/api/coach/email-deliveries/{delivery_id}/resend" in paths
    assert "/api/coach/email-deliveries/{delivery_id}/contact" in paths
    assert "/api/public/command-center/snapshot" not in paths
    assert "/api/public/settings" not in paths
    assert "/api/public/provider" not in paths
    assert "/api/public/clients" not in paths
    client_list = openapi["paths"]["/api/coach/clients"]["get"]
    client_detail = openapi["paths"]["/api/coach/clients/{client_id}"]["get"]
    assert client_list["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CoachClientListResponse"
    }
    assert client_detail["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CoachClientProfileResponse"
    }
    for operation in (client_list, client_detail):
        assert operation["responses"]["404"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }
    client_profile = openapi["components"]["schemas"]["CoachClientProfileResponse"]
    assert {
        "id",
        "name",
        "phone",
        "email",
        "notes",
        "location_mode",
        "client_location",
        "effective_location",
        "default_duration_mins",
        "revision",
    }.issubset(client_profile["required"])
    assert client_profile["properties"]["location_mode"]["enum"] == [
        "client_location",
        "service_location",
    ]
    assert client_profile["properties"]["revision"]["minimum"] == 1
    for field in ("phone", "email", "client_location", "effective_location"):
        assert {"type": "null"} in client_profile["properties"][field]["anyOf"]
    blocker_create = openapi["paths"]["/api/coach/blockers"]["post"]
    assert blocker_create["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CoachBlockerResponse"
    }
    blocker_delete = openapi["paths"]["/api/coach/blockers/{blocker_id}"]["delete"]
    assert "204" in blocker_delete["responses"]
    blocker_operations = (
        openapi["paths"]["/api/coach/blockers"]["get"],
        blocker_create,
        openapi["paths"]["/api/coach/blockers/{blocker_id}"]["put"],
        blocker_delete,
    )
    for operation in blocker_operations:
        for status in ("404", "409", "422"):
            assert operation["responses"][status]["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/ErrorResponse"
            }
    schedule_items = openapi["components"]["schemas"]["CoachCommandCenterSnapshot"][
        "properties"
    ]["schedule"]["items"]
    assert schedule_items["discriminator"]["propertyName"] == "kind"
    assert schedule_items["discriminator"]["mapping"] == {
        "lesson": "#/components/schemas/CoachLessonScheduleItemResponse",
        "break": "#/components/schemas/CoachBlockerScheduleItemResponse",
        "personal": "#/components/schemas/CoachBlockerScheduleItemResponse",
        "unavailable": "#/components/schemas/CoachBlockerScheduleItemResponse",
        "travel": "#/components/schemas/CoachUtilityScheduleItemResponse",
        "buffer": "#/components/schemas/CoachUtilityScheduleItemResponse",
        "open_gap": "#/components/schemas/CoachUtilityScheduleItemResponse",
    }
    assert {
        item["$ref"] for item in schedule_items["oneOf"]
    } == {
        "#/components/schemas/CoachLessonScheduleItemResponse",
        "#/components/schemas/CoachBlockerScheduleItemResponse",
        "#/components/schemas/CoachUtilityScheduleItemResponse",
    }
    lesson_schedule_item = openapi["components"]["schemas"][
        "CoachLessonScheduleItemResponse"
    ]
    assert {"kind", "client_id", "location", "status"}.issubset(
        lesson_schedule_item["required"]
    )
    blocker_schedule_item = openapi["components"]["schemas"][
        "CoachBlockerScheduleItemResponse"
    ]
    assert {"revision", "location"}.issubset(blocker_schedule_item["required"])
    assert blocker_schedule_item["properties"]["revision"]["minimum"] == 1
    routing_readiness = openapi["components"]["schemas"]["RoutingReadinessResponse"]
    assert set(routing_readiness["required"]) == {"provider", "mode", "configured"}

    sign_in = openapi["paths"]["/api/auth/coach/sign-in"]["post"]
    login_alias = openapi["paths"]["/api/auth/coach/login"]["post"]
    sign_out = openapi["paths"]["/api/auth/coach/sign-out"]["post"]
    logout_alias = openapi["paths"]["/api/auth/coach/logout"]["post"]
    assert sign_in["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CoachAuthResponse"
    }
    assert login_alias["deprecated"] is True
    assert logout_alias["deprecated"] is True
    assert "requestBody" not in sign_out
    assert "requestBody" not in logout_alias


def test_openapi_uses_cookie_security_only_for_provider_routes():
    openapi = _load_json(CONTRACT_ROOT / "openapi.json")
    schemes = openapi["components"]["securitySchemes"]
    cookie_schemes = {
        name
        for name, scheme in schemes.items()
        if scheme.get("type") == "apiKey"
        and scheme.get("in") == "cookie"
        and scheme.get("name") == "swim_session"
    }

    assert len(cookie_schemes) == 1
    assert all(scheme.get("type") != "http" for scheme in schemes.values())
    cookie_scheme = next(iter(cookie_schemes))

    for path, operations in openapi["paths"].items():
        for method, operation in operations.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            if path.startswith("/api/coach/") or path == "/api/auth/coach/session":
                assert {cookie_scheme: []} in operation.get("security", []), (method, path)
            if path.startswith("/api/public/") or path in {"/health", "/ready", "/openapi.json"}:
                assert not operation.get("security"), (method, path)


def test_openapi_documents_csrf_header_on_every_unsafe_provider_operation_only():
    openapi = _load_json(CONTRACT_ROOT / "openapi.json")
    unsafe_methods = {"post", "put", "patch", "delete"}
    provider_prefixes = ("/api/auth/coach", "/api/coach")

    for path, operations in openapi["paths"].items():
        for method, operation in operations.items():
            if method not in {"get", *unsafe_methods}:
                continue
            csrf_parameters = [
                parameter
                for parameter in operation.get("parameters", [])
                if parameter.get("in") == "header"
                and parameter.get("name", "").casefold() == "x-csrf-protection"
            ]
            expected = path.startswith(provider_prefixes) and method in unsafe_methods
            if not expected:
                assert csrf_parameters == [], (method, path)
                continue

            assert len(csrf_parameters) == 1, (method, path)
            csrf_parameter = csrf_parameters[0]
            assert csrf_parameter["required"] is True
            assert csrf_parameter["schema"]["const"] == "1"
            assert csrf_parameter["schema"]["enum"] == ["1"]


def test_auth_contract_is_metadata_only_and_contains_no_bearer_schema():
    fixture = _load_json(CONTRACT_ROOT / "fixtures/coach/auth_response.json")
    assert set(fixture) == {"authenticated", "expires_at", "coach"}
    assert fixture["authenticated"] is True

    openapi = _load_json(CONTRACT_ROOT / "openapi.json")
    serialized = json.dumps(openapi)
    assert "access_token" not in serialized
    assert "token_type" not in serialized
    assert "HTTPBearer" not in serialized


def test_fixtures_validate_against_response_schemas():
    for relative_path, schema in FIXTURE_SCHEMAS.items():
        schema.model_validate(_load_json(CONTRACT_ROOT / relative_path))
    TypeAdapter(list[CoachEmailDeliveryResponse]).validate_python(
        _load_json(CONTRACT_ROOT / "fixtures/coach/email_deliveries.json")
    )


def test_blocker_fixture_uses_provider_local_response_instants():
    payload = _load_json(CONTRACT_ROOT / "fixtures/coach/blocker_response.json")
    assert payload["timezone"] == "America/Chicago"
    assert payload["start_time"] == "2026-08-20T12:00:00-05:00"
    assert payload["end_time"] == "2026-08-20T12:30:00-05:00"
    openapi = _load_json(CONTRACT_ROOT / "openapi.json")
    assert "location" in openapi["components"]["schemas"]["CoachBlockerResponse"]["required"]


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
        keys = flatten_keys(payload)
        values = _flatten_values(payload)
        assert PUBLIC_FORBIDDEN_KEYS.isdisjoint(keys), path
        assert "base_location" not in keys, path
        assert FORBIDDEN_PUBLIC_VALUES.isdisjoint(values), path
        assert not any(
            isinstance(value, str) and value.startswith(FORBIDDEN_PUBLIC_VALUE_PREFIXES)
            for value in values
        ), path


def test_public_slot_schema_omits_internal_scheduling_evidence():
    openapi = _load_json(CONTRACT_ROOT / "openapi.json")
    expected_properties = {
        "ClientInviteSchedulerSlot": {
            "id",
            "start_iso",
            "end_iso",
            "display",
            "location_address",
            "recurrence_available",
            "recurrence",
            "recurrence_count",
        },
        "PublicTimeSlotResponse": {"start_iso", "end_iso", "display"},
        "ClientInviteUnavailableWindow": {"id", "label"},
    }

    for schema_name, properties in expected_properties.items():
        actual = set(openapi["components"]["schemas"][schema_name]["properties"])
        assert actual == properties, schema_name


def test_required_fixture_files_exist_for_frontend_contract_tests():
    required = set(FIXTURE_SCHEMAS.keys()) | {"fixtures/coach/email_deliveries.json"}

    for relative_path in required:
        assert (CONTRACT_ROOT / relative_path).exists()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


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
