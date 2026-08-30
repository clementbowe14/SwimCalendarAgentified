# Instructor Command Frontend

Instructor command-center prototype for viewing a schedule, searching availability, and booking a safe start.

## Local Data Modes

By default the app uses `MockInstructorCommandApi`.

Set `VITE_INSTRUCTOR_API_URL` to use the FastAPI backend:

```bash
VITE_INSTRUCTOR_API_URL=http://127.0.0.1:8000/api VITE_INSTRUCTOR_COMMAND_DATE=2026-08-20 npm run dev
```

The current backend exposes:

- `GET /api/coach/command-center/snapshot?date=YYYY-MM-DD`
- `GET /api/coach/schedule?date=YYYY-MM-DD`
- `POST /api/coach/availability`
- `POST /api/coach/bookings`
- `GET /api/coach/settings`
- `PUT /api/coach/settings`

The command app still needs backend support for request approval/alternatives. Until those endpoints exist, the real API adapter surfaces `ENDPOINT_NOT_AVAILABLE` for those actions.

## Tests

```bash
npm test
npm run test:e2e
```

`npm test` covers screen models, renderers, and API adapter contracts. `npm run test:e2e` drives the browser prototype with the mock adapter by default, and includes skipped live-backend skeletons when `VITE_INSTRUCTOR_API_URL` is set.
