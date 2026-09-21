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
from app.api_schemas import CoachCommandCenterSnapshot, CoachSchedulerRules


def build_contract_artifacts() -> dict[str, Any]:
    coach_settings_fixture = _coach_settings_fixture()
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
    return {
        "openapi.json": create_app().openapi(),
        "fixtures/public/client_invite_envelope.json": {
            "invite_token": "invite_sofia_0826",
            "instructor_name": "Coach Sarah",
            "business_name": "Swim Command",
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
            "dialogue_response": "Here are the safe lesson times that fit the instructor schedule, travel time, and buffers.",
            "slots": [
                {
                    "id": "slot_contract_0829_1230",
                    "start_iso": "2026-08-29T12:30:00-05:00",
                    "end_iso": "2026-08-29T13:15:00-05:00",
                    "display": "Sat, Aug 29 at 12:30 PM",
                    "location_address": "820 Lakeview Dr, Austin, TX",
                    "travel_time_mins": 12,
                    "buffer_mins": 10,
                    "distance_miles": 2.1,
                    "previous_event": "Prior commitment",
                    "next_event": "Next commitment",
                    "safety_reason": "Travel and buffer time fit before the next protected schedule item.",
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
                "travel_time_mins": 12,
                "buffer_mins": 10,
                "distance_miles": 2.1,
                "previous_event": "Prior commitment",
                "next_event": "Next commitment",
                "safety_reason": "This requested time was created from a server-issued slot offer.",
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
                "travel_time_mins": 12,
                "buffer_mins": 10,
                "distance_miles": 2.1,
                "previous_event": "Prior commitment",
                "next_event": "Next commitment",
                "safety_reason": "This requested time was created from a server-issued slot offer.",
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
                "travel_time_mins": 12,
                "buffer_mins": 10,
                "distance_miles": 2.1,
                "previous_event": "Prior commitment",
                "next_event": "Next commitment",
                "safety_reason": "This requested time was created from a server-issued slot offer.",
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
        "fixtures/coach/auth_response.json": {
            "access_token": "contract-coach-access-token",
            "token_type": "bearer",
            "expires_at": "2026-08-31T18:00:00Z",
            "coach": {
                "instructor_id": "inst_001",
                "display_name": "Coach Sarah",
            },
        },
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
        "fixtures/coach/settings.json": coach_settings_fixture,
        "fixtures/coach/command_center_snapshot.json": _coach_command_center_snapshot_fixture(coach_settings_fixture),
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


def _coach_command_center_snapshot_fixture(rules: dict[str, Any]) -> dict[str, Any]:
    return CoachCommandCenterSnapshot.model_validate(
        {
            "instructor": {
                "id": "inst_001",
                "name": "Coach Sarah",
                "business_name": "Swim Command",
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
            "rules": rules,
        }
    ).model_dump(mode="json", exclude_none=True)


def _contracts_readme() -> str:
    return """# API Contracts

This directory contains generated OpenAPI and curated JSON fixtures for frontend contract tests.

Public fixtures represent token-scoped client invite flows. They must not include raw instructor IDs,
raw client IDs, private schedule details, internal appointment IDs, or provider error payloads.

## Client Invite Public API

The `frontend-client-invite` fetch adapter uses `VITE_CLIENT_INVITE_API_URL` as its base URL. For
local development, set it to `http://127.0.0.1:8000/api/public` and use the seeded invite token
`invite_sofia_0826`.

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
instructor experiences. Instructor apps sign in with `POST /api/auth/coach/sign-in` or
`POST /api/auth/coach/login`, persist the returned bearer token locally, and send it on `/api/coach/*`
requests as `Authorization: Bearer <token>`.

Regenerate contracts with:

```bash
./venv/bin/python scripts/export_contracts.py
```

Check for drift with:

```bash
./venv/bin/python scripts/export_contracts.py --check
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
