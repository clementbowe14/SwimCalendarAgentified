# API Contracts

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
instructor experiences.

Regenerate contracts with:

```bash
./venv/bin/python scripts/export_contracts.py
```

Check for drift with:

```bash
./venv/bin/python scripts/export_contracts.py --check
```
