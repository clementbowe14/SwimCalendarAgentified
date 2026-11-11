# Dayweave Provider Frontend

Instructor command-center prototype for viewing a schedule, searching availability, and booking a safe start.

## Local Data Modes

Data mode is fail-closed. Mock data is available only when explicitly selected:

```bash
VITE_USE_MOCK_API=true npm run dev
```

HTTP is the default mode and requires the FastAPI base URL:

```bash
VITE_USE_MOCK_API=false VITE_INSTRUCTOR_API_URL=http://127.0.0.1:8000/api VITE_INSTRUCTOR_COMMAND_DATE=2026-08-20 npm run dev
```

The backend must be running with a provisioned provider account, or with both explicit local/test demo
flags described in the root README. The frontend never supplies or selects an instructor ID.

The `VITE_USE_MOCK_API=false` value may be omitted because HTTP is the default. Starting or building
without an API URL fails instead of falling back to fixtures. Mock mode and an API URL cannot be set
together, and production builds reject mock mode. See `.env.example` for the local HTTP configuration.

When `VITE_INSTRUCTOR_API_URL` is set, the instructor app expects MVP coach auth and shows a compact
session-checking state before protected coach data loads. It validates the server-managed
`swim_session` cookie on every load and reload, then requests coach data only after validation succeeds.
The opaque credential is `HttpOnly`: frontend code neither reads nor stores it. On boot, the app removes
the obsolete `swim-command:coach-session:v1` local-storage key without using its value. Explicit mock
mode remains unauthenticated for fast local UI work and shows a development-only `Mock data` marker.

Expected auth endpoints:

- `POST /api/auth/coach/sign-in` with `{ "email": "...", "password": "..." }`
- `GET /api/auth/coach/session` to validate the cookie before protected bootstrap
- `POST /api/auth/coach/sign-out` to revoke the current server-side session

All requests use `credentials: "include"`. Unsafe auth and coach calls send
`X-CSRF-Protection: 1`; no request constructs an `Authorization` header. Only a `401` expires the
frontend session. A `403` remains a permission/security error and does not sign the provider out.

Expected sign-in response:

```json
{
  "authenticated": true,
  "coach": {
    "instructor_id": "inst_001",
    "display_name": "Coach Sarah"
  },
  "expires_at": "2026-08-31T20:00:00Z"
}
```

Protected coach endpoints:

- `GET /api/coach/command-center/snapshot?date=YYYY-MM-DD`
- `GET /api/coach/schedule?date=YYYY-MM-DD`
- `POST /api/coach/availability`
- `POST /api/coach/bookings`
- `GET /api/coach/appointments/{appointment_id}/calendar-links`
- `GET /api/coach/appointments/{appointment_id}/calendar.ics`
- `GET /api/coach/scheduling-policy`
- `PUT /api/coach/scheduling-policy`
- `GET /api/coach/settings` is a deprecated read-only compatibility endpoint.
- `PUT /api/coach/settings` returns `410 SCHEDULING_POLICY_ENDPOINT_MOVED`; new product code never uses it.
- `POST /api/coach/lesson-requests/{request_token}/confirm` is the canonical confirmation endpoint. The
  instructor app sends `{}` for the current requested occurrence or `{ "slot_id": "..." }` when the
  client selected a request-bound alternative.
- `POST /api/coach/lesson-requests/{request_token}/approve` is a deprecated, bodyless compatibility
  alias only. New product code and live product-flow tests must use `/confirm`.
- `POST /api/coach/lesson-requests/{request_token}/alternatives`
- `POST /api/coach/lesson-requests/{request_token}/decline`

## Tests

```bash
npm test
npm run test:e2e
npm run test:e2e:auth
```

`npm test` covers screen models, renderers, calendar URL safety, API adapter contracts, and exact data-mode
selection. `npm run test:e2e` explicitly drives the browser prototype with the mock adapter.
`npm run test:e2e:auth` exercises cookie-authenticated bootstrap, reload, expiry, sign-out, calendar
fallback, CSRF, scheduling-policy editing, and failure states against intercepted API responses.
`npm run test:e2e:live` requires
`VITE_INSTRUCTOR_API_URL`; a missing URL fails
rather than executing mock tests or passing with a skipped live suite. The live suite discovers and
selects a real request-bound alternative, confirms it through the UI, independently checks the coach
schedule plus public confirmation timestamps, verifies that no credential is script-readable, proves
session validation precedes protected requests after reload, and replays a captured cookie after sign-out
to verify server-side revocation.
