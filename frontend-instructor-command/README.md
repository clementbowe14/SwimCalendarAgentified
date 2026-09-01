# Instructor Command Frontend

Instructor command-center prototype for viewing a schedule, searching availability, and booking a safe start.

## Local Data Modes

By default the app uses `MockInstructorCommandApi`.

Set `VITE_INSTRUCTOR_API_URL` to use the FastAPI backend:

```bash
VITE_INSTRUCTOR_API_URL=http://127.0.0.1:8000/api VITE_INSTRUCTOR_COMMAND_DATE=2026-08-20 npm run dev
```

When `VITE_INSTRUCTOR_API_URL` is set, the instructor app expects MVP coach auth and shows a compact
locked sign-in state before protected coach data loads. The frontend stores the returned bearer session
in browser `localStorage` under `swim-command:coach-session:v1`; the backend remains responsible for
token lifetime and revocation. Mock mode remains unauthenticated for fast local UI work.

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
- `GET /api/coach/settings`
- `PUT /api/coach/settings`
- `POST /api/coach/lesson-requests/{request_token}/approve`
- `POST /api/coach/lesson-requests/{request_token}/alternatives`
- `POST /api/coach/lesson-requests/{request_token}/decline`

## Tests

```bash
npm test
npm run test:e2e
```

`npm test` covers screen models, renderers, and API adapter contracts. `npm run test:e2e` drives the browser prototype with the mock adapter by default, and includes skipped live-backend skeletons when `VITE_INSTRUCTOR_API_URL` is set.
