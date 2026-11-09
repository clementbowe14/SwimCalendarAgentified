# API Contracts

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
