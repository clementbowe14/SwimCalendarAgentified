import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CONTRACT_ROOT = Path("contracts")

from app.api import create_app
from app.api_schemas import (
    BookLessonResponse,
    CoachAuthResponse,
    CoachBlockerResponse,
    CoachClientListResponse,
    CoachClientProfileResponse,
    CoachCommandCenterSnapshot,
    CoachEmailDeliveryResponse,
    CoachLessonRequestConfirmResponse,
    CoachProviderSettingsResponse,
    CoachSchedulingPolicyResponse,
    CoachSchedulerRules,
    InviteNotificationResponse,
)


def build_contract_artifacts() -> dict[str, Any]:
    coach_settings_fixture = _coach_settings_fixture()
    coach_scheduling_policy_fixture = _coach_scheduling_policy_fixture()
    coach_provider_fixture = _coach_provider_fixture()
    coach_client_fixture = CoachClientProfileResponse.model_validate(
        {
            "id": "client_sofia",
            "name": "Sofia Martinez",
            "phone": "(512) 555-0194",
            "email": "ana.martinez@example.com",
            "notes": "Prefers weekday afternoons.",
            "location_mode": "client_location",
            "client_location": {
                "address": "820 Lakeview Dr, Austin, TX",
                "lat": 30.289,
                "lng": -97.739,
            },
            "effective_location": {
                "address": "820 Lakeview Dr, Austin, TX",
                "lat": 30.289,
                "lng": -97.739,
            },
            "default_duration_mins": 45,
            "availability": [
                {
                    "id": "availability_weekday",
                    "days": ["monday", "wednesday", "friday"],
                    "start_time": "16:00",
                    "end_time": "19:00",
                }
            ],
            "revision": 1,
        }
    ).model_dump(mode="json")
    coach_clients_fixture = CoachClientListResponse.model_validate(
        {"clients": [coach_client_fixture]}
    ).model_dump(mode="json")
    coach_email_delivery = CoachEmailDeliveryResponse.model_validate(
        {
            "id": "delivery_contract_confirmation_email",
            "kind": "confirmation",
            "status": "queued",
            "updated_at": "2026-08-28T16:35:00Z",
            "client_id": "client_sofia",
            "client_name": "Sofia Martinez",
            "request_token": "req_contract_confirmed",
            "issue": None,
            "source_state": "active",
            "can_resend": False,
            "can_edit_contact": False,
        }
    ).model_dump(mode="json")
    google = "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE", "text": "Swim lesson",
        "dates": "20260829T173000Z/20260829T181500Z",
        "location": "820 Lakeview Dr, Austin, TX", "details": "Confirmed swim lesson.",
    })
    calendar_links = {
        "google": google,
        "apple": "/api/public/calendar/req_contract_confirmed.ics",
        "ics": "/api/public/calendar/req_contract_confirmed.ics",
        "google_events": [{"start_iso": "2026-08-29T17:30:00Z", "end_iso": "2026-08-29T18:15:00Z", "url": google}],
    }
    coach_google = "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE", "text": "Appointment",
        "dates": "20260829T173000Z/20260829T181500Z",
        "location": "820 Lakeview Dr, Austin, TX", "details": "Confirmed appointment.",
    })
    coach_calendar_links = {
        "google": coach_google,
        "apple": "/api/coach/appointments/app_contract_confirmed/calendar.ics",
        "ics": "/api/coach/appointments/app_contract_confirmed/calendar.ics",
        "google_events": [
            {
                "start_iso": "2026-08-29T17:30:00Z",
                "end_iso": "2026-08-29T18:15:00Z",
                "url": coach_google,
            }
        ],
    }
    return {
        "openapi.json": create_app().openapi(),
        "fixtures/public/client_invite_envelope.json": {
            "invite_token": "invite_sofia_0826",
            "instructor_name": "Coach Sarah",
            "business_name": "Swim Command",
            "service_name": "Swim lesson",
            "service_location": {
                "address": "100 Main St, Austin, TX",
                "lat": 30.2672,
                "lng": -97.7431,
            },
            "expires_at_iso": "2026-09-27T16:20:44",
            "intake": {
                "client_name": "Sofia Martinez",
                "guardian_phone": "(512) 555-0194",
                "guardian_email": "ana.martinez@example.com",
                "notes": "Beginner swimmer. Prefers shallow end warmup.",
                "location_type": "home",
                "location": {
                    "address": "820 Lakeview Dr, Austin, TX",
                    "lat": 30.289,
                    "lng": -97.739,
                    "is_instructor_facility": False,
                },
            },
            "preferred_duration_mins": 45,
            "allowed_durations_mins": [30, 45, 60],
        },
        "fixtures/public/availability_search_response.json": {
            "status": "PRESENTING",
            "dialogue_response": "Here are the appointment times available to request.",
            "slots": [
                {
                    "id": "slot_contract_0829_1230",
                    "start_iso": "2026-08-29T12:30:00-05:00",
                    "end_iso": "2026-08-29T13:15:00-05:00",
                    "display": "Sat, Aug 29 at 12:30 PM",
                    "location_address": "820 Lakeview Dr, Austin, TX",
                    "recurrence_available": True,
                }
            ],
            "unavailable_windows": [],
        },
        "fixtures/public/lesson_request_requested.json": {
            "notifications": [],
            "id": "req_contract_pending",
            "invite_token": "invite_sofia_0826",
            "slot": {
                "id": "slot_contract_0829_1230",
                "start_iso": "2026-08-29T12:30:00-05:00",
                "end_iso": "2026-08-29T13:15:00-05:00",
                "display": "Sat, Aug 29 at 12:30 PM",
                "location_address": "820 Lakeview Dr, Austin, TX",
                "recurrence_available": True,
            },
            "status": "requested",
            "requested_at_iso": "2026-08-28T16:30:00",
            "confirmation": None,
        },
        "fixtures/public/lesson_request_confirmed.json": {
            "notifications": [
                {"kind": "confirmation", "channel": "sms", "status": "skipped", "updated_at": "2026-08-28T16:35:00Z"},
                {"kind": "confirmation", "channel": "email", "status": "queued", "updated_at": "2026-08-28T16:35:00Z"},
            ],
            "id": "req_contract_confirmed",
            "invite_token": "invite_sofia_0826",
            "slot": {
                "id": "slot_contract_0829_1230",
                "start_iso": "2026-08-29T12:30:00-05:00",
                "end_iso": "2026-08-29T13:15:00-05:00",
                "display": "Sat, Aug 29 at 12:30 PM",
                "location_address": "820 Lakeview Dr, Austin, TX",
                "recurrence_available": True,
            },
            "status": "confirmed",
            "requested_at_iso": "2026-08-28T16:30:00",
            "confirmation": {
                "appointment_id": "req_contract_confirmed",
                "message": "Your swim lesson is confirmed for Sat, Aug 29 at 12:30 PM.",
                "confirmed_at_iso": "2026-08-28T16:35:00",
                "calendar_links": calendar_links,
            },
        },
        "fixtures/public/lesson_request_needs_new_times.json": {
            "notifications": [],
            "id": "req_contract_alternatives",
            "invite_token": "invite_sofia_0826",
            "slot": {
                "id": "slot_contract_0829_1230",
                "start_iso": "2026-08-29T12:30:00-05:00",
                "end_iso": "2026-08-29T13:15:00-05:00",
                "display": "Sat, Aug 29 at 12:30 PM",
                "location_address": "820 Lakeview Dr, Austin, TX",
                "recurrence_available": True,
            },
            "status": "needs_new_times",
            "requested_at_iso": "2026-08-28T16:30:00",
            "confirmation": None,
        },
        "fixtures/public/calendar_links.json": calendar_links,
        "fixtures/public/error_invalid_token.json": {
            "error": {
                "code": "INVITE_NOT_FOUND",
                "message": "The invite could not be found.",
            }
        },
        "fixtures/coach/auth_response.json": CoachAuthResponse.model_validate(
            {
                "authenticated": True,
                "expires_at": "2026-08-31T18:00:00Z",
                "coach": {
                    "instructor_id": "inst_001",
                    "display_name": "Coach Sarah",
                },
            }
        ).model_dump(mode="json"),
        "fixtures/coach/calendar_links.json": coach_calendar_links,
        "fixtures/coach/booking_response.json": BookLessonResponse.model_validate(
            {
                "success": True,
                "appointment_id": "app_contract_confirmed",
                "message": "Appointment booked.",
                "calendar_links": coach_calendar_links,
            }
        ).model_dump(mode="json"),
        "fixtures/coach/email_delivery.json": coach_email_delivery,
        "fixtures/coach/email_deliveries.json": [coach_email_delivery],
        "fixtures/coach/invite_notification_response.json": (
            InviteNotificationResponse.model_validate(
                {
                    "notifications": [
                        {
                            "kind": "invite",
                            "channel": "sms",
                            "status": "skipped",
                            "updated_at": "2026-08-28T16:30:00Z",
                        },
                        {
                            "kind": "invite",
                            "channel": "email",
                            "status": "queued",
                            "updated_at": "2026-08-28T16:30:00Z",
                        },
                    ],
                    "email_delivery": {
                        **coach_email_delivery,
                        "id": "delivery_contract_invite_email",
                        "kind": "invite",
                        "request_token": None,
                    },
                }
            ).model_dump(mode="json")
        ),
        "fixtures/coach/lesson_request_confirmation_response.json": (
            CoachLessonRequestConfirmResponse.model_validate(
                {
                    "id": "req_contract_confirmed",
                    "status": "confirmed",
                    "appointment_id": "app_contract_confirmed",
                    "appointment_ids": ["app_contract_confirmed"],
                    "message": "Lesson request is confirmed.",
                    "calendar_links": coach_calendar_links,
                    "email_delivery": coach_email_delivery,
                }
            ).model_dump(mode="json")
        ),
        "fixtures/coach/availability_search_response.json": {
            "status": "PRESENTING",
            "client_name": "Charlie",
            "requested_date": "2026-08-20",
            "duration_mins": 60,
            "location": {
                "address": "450 North Blvd, Austin, TX",
                "lat": 30.33,
                "lng": -97.72,
                "is_instructor_facility": False,
            },
            "available_slots": [
                {
                    "start_iso": "2026-08-20T10:30:00-05:00",
                    "end_iso": "2026-08-20T11:30:00-05:00",
                    "display": "10:30 AM - 11:30 AM (Travel: 0m, Buffer: 10m, Dist: 0.0mi)",
                    "travel_time_mins": 0,
                    "buffer_mins": 10,
                    "distance_miles": 0.0,
                }
            ],
        },
        "fixtures/coach/schedule_response.json": {
            "instructor": {
                "id": "inst_001",
                "name": "Coach Sarah",
                "main_address": "100 Main St, Austin, TX",
                "lat": 30.2672,
                "lng": -97.7431,
                "work_start_time": "08:00",
                "work_end_time": "17:00",
                "timezone": "America/Chicago",
            },
            "date": "2026-08-20",
            "appointments": [
                {
                    "id": "app_101",
                    "title": "Lesson with Alice",
                    "start_time": "2026-08-20T09:00:00-05:00",
                    "end_time": "2026-08-20T10:00:00-05:00",
                    "location_address": "450 North Blvd, Austin, TX",
                    "lat": 30.33,
                    "lng": -97.72,
                    "is_break": False,
                }
            ],
        },
        "fixtures/coach/blocker_response.json": CoachBlockerResponse.model_validate(
            {
                "id": "block_contract_lunch",
                "kind": "break",
                "title": "Lunch",
                "start_time": "2026-08-20T12:00:00-05:00",
                "end_time": "2026-08-20T12:30:00-05:00",
                "local_date": "2026-08-20",
                "local_start_time": "12:00",
                "local_end_time": "12:30",
                "timezone": "America/Chicago",
                "revision": 1,
                "location": None,
            }
        ).model_dump(mode="json"),
        "fixtures/coach/settings.json": coach_settings_fixture,
        "fixtures/coach/scheduling_policy.json": coach_scheduling_policy_fixture,
        "fixtures/coach/provider.json": coach_provider_fixture,
        "fixtures/coach/client.json": coach_client_fixture,
        "fixtures/coach/clients.json": coach_clients_fixture,
        "fixtures/coach/command_center_snapshot.json": _coach_command_center_snapshot_fixture(
            coach_settings_fixture,
            coach_scheduling_policy_fixture,
            coach_email_delivery,
        ),
    }


def write_contracts(output_dir: Path = CONTRACT_ROOT) -> None:
    for relative_path, payload in build_contract_artifacts().items():
        path = output_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json(payload), encoding="utf-8")
    readme = output_dir / "README.md"
    readme.write_text(_contracts_readme(), encoding="utf-8")


def check_contracts(output_dir: Path = CONTRACT_ROOT) -> list[str]:
    mismatches = []
    expected_artifacts = build_contract_artifacts()
    for relative_path, payload in expected_artifacts.items():
        path = output_dir / relative_path
        if not path.exists():
            mismatches.append(str(path))
            continue
        if path.read_text(encoding="utf-8") != _json(payload):
            mismatches.append(str(path))
    readme = output_dir / "README.md"
    if not readme.exists() or readme.read_text(encoding="utf-8") != _contracts_readme():
        mismatches.append(str(readme))
    return mismatches


def _json(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _coach_settings_fixture() -> dict[str, Any]:
    return CoachSchedulerRules.model_validate(
        {
            "rounding_increment_mins": 15,
            "post_break_buffer_mins": 15,
            "distance_buffer_tiers": [
                {"id": "near", "label": "Under 5 mi", "min_miles": 0, "max_miles": 4.999, "buffer_mins": 10},
                {"id": "medium", "label": "5-10 mi", "min_miles": 5, "max_miles": 10, "buffer_mins": 20},
                {"id": "far", "label": "Over 10 mi", "min_miles": 10.001, "max_miles": None, "buffer_mins": 30},
            ],
            "payments": {
                "enabled_methods": ["card", "ach", "cash"],
                "cancellation_window_hours": 12,
                "require_payment_method_for_new_clients": True,
            },
        }
    ).model_dump(mode="json")


def _coach_scheduling_policy_fixture() -> dict[str, Any]:
    return CoachSchedulingPolicyResponse.model_validate(
        {
            "revision": 0,
            "distance_buffers_enabled": True,
            "post_break_recovery_mins": 15,
            "distance_buffer_tiers": [
                {"id": "near", "label": "Under 5 mi", "min_miles": 0, "max_miles": 4.999, "buffer_mins": 10},
                {"id": "medium", "label": "5-10 mi", "min_miles": 5, "max_miles": 10, "buffer_mins": 20},
                {"id": "far", "label": "Over 10 mi", "min_miles": 10.001, "max_miles": None, "buffer_mins": 30},
            ],
        }
    ).model_dump(mode="json")


def _coach_provider_fixture() -> dict[str, Any]:
    return CoachProviderSettingsResponse.model_validate(
        {
            "id": "inst_001",
            "display_name": "Coach Sarah",
            "business_name": "Swim Command",
            "timezone": "America/Chicago",
            "base_location": {
                "address": "100 Main St, Austin, TX",
                "lat": 30.2672,
                "lng": -97.7431,
            },
            "service": {
                "name": "Swim lesson",
                "default_duration_mins": 45,
                "location": {
                    "address": "100 Main St, Austin, TX",
                    "lat": 30.2672,
                    "lng": -97.7431,
                },
            },
            "working_hours": [
                {
                    "weekday": weekday,
                    "enabled": True,
                    "start_time": "08:00",
                    "end_time": "17:00",
                }
                for weekday in (
                    "monday",
                    "tuesday",
                    "wednesday",
                    "thursday",
                    "friday",
                    "saturday",
                    "sunday",
                )
            ],
            "setup_complete": True,
            "setup_missing_fields": [],
        }
    ).model_dump(mode="json")


def _coach_command_center_snapshot_fixture(
    rules: dict[str, Any],
    scheduling_policy: dict[str, Any],
    email_delivery: dict[str, Any],
) -> dict[str, Any]:
    return CoachCommandCenterSnapshot.model_validate(
        {
            "instructor": {
                "id": "inst_001",
                "name": "Coach Sarah",
                "business_name": "Swim Command",
                "base_location": {
                    "address": "100 Main St, Austin, TX",
                    "lat": 30.2672,
                    "lng": -97.7431,
                },
                "service": {
                    "name": "Swim lesson",
                    "default_duration_mins": 45,
                    "location": {
                        "address": "100 Main St, Austin, TX",
                        "lat": 30.2672,
                        "lng": -97.7431,
                    },
                },
                "working_hours": _coach_provider_fixture()["working_hours"],
                "setup_complete": True,
                "setup_missing_fields": [],
                "main_address": "100 Main St, Austin, TX",
                "lat": 30.2672,
                "lng": -97.7431,
                "work_start_time": "08:00",
                "work_end_time": "17:00",
                "timezone": "America/Chicago",
            },
            "date": "2026-08-20",
            "clients": [
                {
                    "id": "client_alice",
                    "name": "Alice",
                    "phone": "",
                    "email": "",
                    "default_location": {
                        "address": "450 North Blvd, Austin, TX",
                        "lat": 30.33,
                        "lng": -97.72,
                        "is_instructor_facility": False,
                    },
                    "location_mode": "client_home",
                    "availability": [],
                    "lesson_duration_mins": 45,
                    "notes": "",
                    "history_summary": "Loaded from client profile",
                    "progress_focus": "",
                }
            ],
            "schedule": [
                {
                    "id": "app_101",
                    "kind": "lesson",
                    "client_id": "client_alice",
                    "title": "Alice",
                    "start_time": "2026-08-20T09:00:00-05:00",
                    "end_time": "2026-08-20T10:00:00-05:00",
                    "location": {
                        "address": "450 North Blvd, Austin, TX",
                        "lat": 30.33,
                        "lng": -97.72,
                        "is_instructor_facility": False,
                    },
                    "status": "scheduled",
                }
            ],
            "requests": [
                {
                    "id": "req_sofia_pending_0820",
                    "client_id": "client_sofia",
                    "client_name": "Sofia Martinez",
                    "requested_slot": {
                        "id": "slot_req_sofia_pending_0820",
                        "start_time": "2026-08-20T16:00:00-05:00",
                        "end_time": "2026-08-20T16:45:00-05:00",
                        "travel_time_mins": 12,
                        "buffer_mins": 10,
                        "distance_miles": 2.1,
                        "target_location": {
                            "address": "820 Lakeview Dr, Austin, TX",
                            "lat": 30.289,
                            "lng": -97.739,
                            "is_instructor_facility": False,
                        },
                    },
                    "source": "client_invite",
                    "status": "pending",
                    "risk": "clear",
                    "submitted_at": "2026-08-19T16:30:00",
                    "note": "Client selected this time from an invite.",
                    "alternative_slots": [],
                }
            ],
            "email_deliveries": [email_delivery],
            "rules": rules,
            "scheduling_policy": scheduling_policy,
        }
    ).model_dump(mode="json")


def _contracts_readme() -> str:
    return """# API Contracts

This directory contains generated OpenAPI and curated JSON fixtures for frontend contract tests.

Public fixtures represent token-scoped client invite flows. They must not include raw instructor IDs,
raw client IDs, private schedule details, internal appointment IDs, or provider error payloads.

## Client Invite Public API

The `frontend-client-invite` fetch adapter uses `VITE_CLIENT_INVITE_API_URL` as its base URL. For
local development, set it to `http://127.0.0.1:8000/api/public`. When the backend is deliberately
started with `SWIM_ENABLE_DEMO_DATA=true`, the seeded invite token is `invite_sofia_0826`.

| Adapter method | Route | Request body | 200 response |
| --- | --- | --- | --- |
| `getInvite` | `GET /client-invites/{token}` | none | `ClientInviteEnvelopeResponse` |
| `submitIntake` | `POST /client-invites/{token}/intake` | `ClientInviteIntake` | `ClientInviteEnvelopeResponse` |
| `findAvailableSlots` | `POST /client-invites/{token}/slots` | `ClientInviteAvailabilityRequest` | `ClientInviteAvailabilityResponse` |
| `requestLessonSlot` | `POST /client-invites/{token}/lesson-requests` | `ClientInviteLessonRequestCreate` | `ClientInviteLessonRequestResponse` |
| `getLessonRequestStatus` | `GET /lesson-requests/{request_token}` | none | `ClientInviteLessonRequestResponse` |
| `getCalendarLinks` | `GET /lesson-requests/{request_token}/calendar-links` | none | `ClientInviteCalendarLinks` |

Calendar clients can download `GET /calendar/{request_token}.ics` after the request is confirmed.
Public errors use the shared `{ "error": { "code", "message", "details" } }` envelope and should not
include private coach schedule identifiers or route provider payloads.

Coach fixtures may include coach-only operational data and should only be consumed by authenticated
instructor experiences. Instructor apps sign in with `POST /api/auth/coach/sign-in` (or the deprecated
`/login` alias). The response contains metadata only; the server issues the opaque credential in the
host-only `swim_session` cookie. Browser requests use `credentials: "include"`, and unsafe auth/coach
requests send `X-CSRF-Protection: 1`. Bearer headers and browser credential storage are unsupported.

`GET /api/auth/coach/session` validates the cookie before any protected data is requested or rendered.
`POST /api/auth/coach/sign-out` revokes the current database session and clears the cookie. Public
invite requests remain unauthenticated and explicitly omit credentials.

Authoritative provider identity, timezone, private base location, client-facing service location,
default duration, and weekly hours use authenticated `GET /api/coach/provider` and full-replacement
`PUT /api/coach/provider`. Provider-owned availability rules use authenticated
`GET /api/coach/scheduling-policy` and optimistic full-replacement
`PUT /api/coach/scheduling-policy`. The deprecated `GET /api/coach/settings` response remains for
read compatibility, while its legacy `PUT` returns 410 and cannot mutate policy or payment data.
Public invite payloads expose only the service location, never the private base.

Canonical active client profiles use authenticated `GET /api/coach/clients` and
`GET /api/coach/clients/{client_id}`. These reads expose persisted contact details, notes, generic
location mode, client availability, duration, and revision without changing the legacy
command-center client projection. Unknown, deactivated, and cross-provider IDs share one generic
not-found response.

Provider-managed breaks, personal events, and unavailable windows use authenticated
`GET /api/coach/blockers`, `POST /api/coach/blockers`, and
`PUT`/`DELETE /api/coach/blockers/{blocker_id}`. Mutations use optimistic revisions; creates also
require an `Idempotency-Key` header so a retry cannot duplicate a schedule event.

Instructor calendar handoffs use authenticated
`GET /api/coach/appointments/{appointment_id}/calendar-links` and
`GET /api/coach/appointments/{appointment_id}/calendar.ics` routes. The `.ics` download requires the
same cookie session; the `apple` field is an alias of this provider-neutral download, not a direct
calendar-provider write.

Instructor email delivery state is available through authenticated
`GET /api/coach/email-deliveries`. Safe recovery uses
`POST /api/coach/email-deliveries/{delivery_id}/resend` and
`PUT /api/coach/email-deliveries/{delivery_id}/contact`. Coach delivery responses intentionally omit
recipient addresses, provider identifiers, raw failures, attempts, leases, and retry timestamps.

Regenerate contracts with:

```bash
SWIM_ENV=test ./venv/bin/python scripts/export_contracts.py
```

Check for drift with:

```bash
SWIM_ENV=test ./venv/bin/python scripts/export_contracts.py --check
```
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Export OpenAPI and frontend contract fixtures.")
    parser.add_argument("--check", action="store_true", help="Fail if committed contracts are stale.")
    args = parser.parse_args()

    if args.check:
        mismatches = check_contracts()
        if mismatches:
            print("Contract artifacts are out of date:", file=sys.stderr)
            for mismatch in mismatches:
                print(f"  - {mismatch}", file=sys.stderr)
            return 1
        return 0

    write_contracts()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
