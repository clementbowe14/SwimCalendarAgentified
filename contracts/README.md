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
instructor experiences. Instructor apps sign in with `POST /api/auth/coach/sign-in` or
`POST /api/auth/coach/login`, persist the returned bearer token locally, and send it on `/api/coach/*`
requests as `Authorization: Bearer <token>`.

Instructor calendar handoffs use authenticated
`GET /api/coach/appointments/{appointment_id}/calendar-links` and
`GET /api/coach/appointments/{appointment_id}/calendar.ics` routes. The `.ics` download requires the
same bearer session; the `apple` field is an alias of this provider-neutral download, not a direct
calendar-provider write.

Instructor email delivery state is available through authenticated
`GET /api/coach/email-deliveries`. Safe recovery uses
`POST /api/coach/email-deliveries/{delivery_id}/resend` and
`PUT /api/coach/email-deliveries/{delivery_id}/contact`. Coach delivery responses intentionally omit
recipient addresses, provider identifiers, raw failures, attempts, leases, and retry timestamps.

Regenerate contracts with:

```bash
./venv/bin/python scripts/export_contracts.py
```

Check for drift with:

```bash
./venv/bin/python scripts/export_contracts.py --check
```
