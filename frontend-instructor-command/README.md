# Instructor Command Frontend

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

The `VITE_USE_MOCK_API=false` value may be omitted because HTTP is the default. Starting or building
without an API URL fails instead of falling back to fixtures. Mock mode and an API URL cannot be set
together, and production builds reject mock mode. See `.env.example` for the local HTTP configuration.

When `VITE_INSTRUCTOR_API_URL` is set, the instructor app expects MVP coach auth and shows a compact
locked sign-in state before protected coach data loads. The frontend stores the returned bearer session
in browser `localStorage` under `swim-command:coach-session:v1`; the backend remains responsible for
token lifetime and revocation. Explicit mock mode remains unauthenticated for fast local UI work and
shows a development-only `Mock data` marker.

Expected auth endpoints:

- `POST /api/auth/coach/sign-in` with `{ "email": "...", "password": "..." }`
- `POST /api/auth/coach/sign-out` with bearer auth

Expected sign-in response:

```json
{
  "access_token": "...",
  "token_type": "bearer",
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
- `GET /api/coach/settings`
- `PUT /api/coach/settings`
- `POST /api/coach/lesson-requests/{request_token}/confirm`
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
`npm run test:e2e:auth` exercises bearer-authenticated calendar fallback and failure states against
intercepted API responses. `npm run test:e2e:live` requires `VITE_INSTRUCTOR_API_URL`; a missing URL fails
rather than executing mock tests or passing with a skipped live suite.
