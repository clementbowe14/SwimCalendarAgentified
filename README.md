# Swim Calendar Agentified

Python prototype for travel-aware swim lesson scheduling.

## Local API

The repository exposes the scheduling prototype through a local FastAPI app.

### Setup

Create or use the existing virtual environment, then install dependencies:

```bash
./venv/bin/python -m pip install -r requirements.txt
```

Start the local Postgres database:

```bash
docker compose up -d postgres
```

This creates the local `swim_scheduler` database for the API and a `swim_scheduler_test` database for tests.

### Database Migrations

Run migrations before starting the API when creating or upgrading a local database:

```bash
./venv/bin/alembic upgrade head
```

The default database is:

```text
postgresql+psycopg://swim:swim@127.0.0.1:5432/swim_scheduler
```

Override it with:

```bash
SWIM_DATABASE_URL=postgresql+psycopg://user:password@host:5432/swim_scheduler ./venv/bin/alembic upgrade head
```

Issue #17 adds strict validation for persisted scheduler buffer policies. Use a
two-phase deployment: deploy the additive columns and strict write validation,
stop or drain old writers, then run the preflight before enabling the new
policy-aware readers:

```bash
./venv/bin/python scripts/check_scheduler_settings.py
```

The preflight reports only instructor IDs and stable reason codes. It never
rewrites custom settings. Remediate every reported row through the validated
coach settings API and rerun the command until it succeeds. Keep the application
read-only during remediation so an older process cannot recreate an invalid
policy. Downgrading migration `20260922_0008` removes the two buffer switches and
therefore loses their customized values.

### Start The API

```bash
./venv/bin/uvicorn app.api:app --host 127.0.0.1 --port 8000
```

The API runs at:

```text
http://127.0.0.1:8000
```

### Health Check

```bash
curl http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok"}
```

### API Docs

Interactive FastAPI docs are available at:

```text
http://127.0.0.1:8000/docs
```

### Coach Authentication

Coach command-center routes under `/api/coach/*` require bearer authentication. Local development
ships with a safe demo credential only:

```text
email: coach@example.com
password: swim-dev-pass
```

Sign in before calling protected coach routes:

```bash
curl -X POST http://127.0.0.1:8000/api/auth/coach/sign-in \
  -H 'content-type: application/json' \
  -d '{"email":"coach@example.com","password":"swim-dev-pass"}'
```

Use the returned token on coach requests:

```bash
curl http://127.0.0.1:8000/api/coach/me \
  -H 'authorization: Bearer <access_token>'
```

For shared or deployed environments, configure a non-demo secret and credentials:

```bash
SWIM_COACH_AUTH_SECRET='replace-with-random-secret'
SWIM_COACH_CREDENTIALS='[{"email":"coach@example.com","password_sha256":"<sha256>","instructor_id":"inst_001"}]'
```

`SWIM_COACH_CREDENTIALS` is a JSON array. Each entry needs `email`, `instructor_id`, and either
`password` for local-only development or `password_sha256` when plaintext passwords should not be
stored in process configuration. The SHA-256 option is an MVP configuration convenience, not
production-grade password storage. `SWIM_ENV=production` and `SWIM_ENV=staging` require explicit
credentials and `SWIM_COACH_AUTH_SECRET`; the demo login is local-development only. Public routes,
`/health`, and `/openapi.json` remain unauthenticated.

### Local Frontend CORS

By default, the API allows local frontend development origins on common ports:

- `http://127.0.0.1:3000`
- `http://127.0.0.1:3001`
- `http://127.0.0.1:3002`
- `http://127.0.0.1:4173`
- `http://127.0.0.1:5173`
- `http://127.0.0.1:5174`
- matching `localhost` origins

Override the list with a comma-separated environment variable:

```bash
SWIM_API_CORS_ORIGINS=http://127.0.0.1:3001,http://127.0.0.1:3002 ./venv/bin/uvicorn app.api:app --host 127.0.0.1 --port 8000
```

Set `SWIM_DATABASE_URL` to point the API at a non-default Postgres database.

### Routing Provider

Local development uses the deterministic Haversine routing provider by default:

```text
SWIM_ROUTING_PROVIDER=haversine
```

To use Mapbox for geocoding and route estimates, set:

```bash
SWIM_ROUTING_PROVIDER=mapbox SWIM_MAPBOX_ACCESS_TOKEN=... ./venv/bin/uvicorn app.api:app --host 127.0.0.1 --port 8000
```

Optional routing settings:

- `SWIM_ROUTING_TRAFFIC_MODE=none|typical|live`
- `SWIM_ROUTING_TIMEOUT_SECONDS=3`
- `SWIM_ROUTE_CACHE_ENABLED=true`
- `SWIM_ROUTE_CACHE_TTL_HOURS=24`
- `SWIM_ROUTE_DEPARTURE_BUCKET_MINUTES=15`

Routing configuration values are validated at startup/request time. Unsupported
providers or traffic modes, missing Mapbox credentials, and non-positive
timeouts/cache windows return a stable `ROUTING_CONFIGURATION_ERROR` from API
availability endpoints.

When route caching is enabled, route estimates are cached by provider, rounded
origin/destination coordinates, UTC departure bucket, and traffic mode.
Geocoding results are not cached. `live` and `typical` traffic estimates without
a departure time bypass the cache so a sentinel bucket cannot serve stale
traffic-dependent travel times.

Availability endpoints return stable, sanitized routing errors:

- `GEOCODING_FAILED` for empty or unmatched client addresses.
- `ROUTING_TIMEOUT` for provider timeouts.
- `ROUTING_PROVIDER_UNAVAILABLE` for provider outages, rate limits, network
  errors, malformed geocode responses, or malformed provider JSON.
- `ROUTING_ESTIMATE_FAILED` when a directions response cannot produce a usable
  route estimate.

### MVP Public Invite Token

The MVP public API includes seeded development invite tokens:

```text
demo-invite-token
invite_sofia_0826
```

Example public invite request:

```bash
curl http://127.0.0.1:8000/api/public/invites/demo-invite-token
```

The `frontend-client-invite` app can disable its mock adapter by setting:

```bash
VITE_CLIENT_INVITE_API_URL=http://127.0.0.1:8000/api/public
```

It defaults to the seeded `invite_sofia_0826` token. The backend exposes the matching fetch adapter
routes under `/api/public/client-invites/{token}` and `/api/public/lesson-requests/{request_token}`.
Public routes are scoped by invite/request tokens and do not require clients to submit raw instructor
or client IDs.

### Representative Routes

- `GET /health`
- `GET /api/coach/me`
- `GET /api/coach/schedule?date=2026-08-20`
- `POST /api/coach/availability`
- `POST /api/coach/bookings`
- `GET /api/public/invites/{token}`
- `POST /api/public/invites/{token}/availability`
- `POST /api/public/invites/{token}/requests`
- `GET /api/public/requests/{request_token}`
- `GET /api/public/client-invites/{token}`
- `POST /api/public/client-invites/{token}/intake`
- `POST /api/public/client-invites/{token}/slots`
- `POST /api/public/client-invites/{token}/lesson-requests`
- `GET /api/public/lesson-requests/{request_token}`
- `GET /api/public/lesson-requests/{request_token}/calendar-links`
- `GET /api/public/calendar/{request_token}.ics`

Errors use a stable JSON shape:

```json
{
  "error": {
    "code": "INVITE_NOT_FOUND",
    "message": "The invite could not be found."
  }
}
```

## Notifications and Calendar Handoff

New lesson confirmations atomically record one SMS event and one email event,
including weekly confirmations. Missing or invalid invite-intake contacts produce
`skipped` events; valid contacts produce `queued` events. Phone numbers must be
E.164 (for example `+15125550194`); the server does not guess a country code.
`accepted` means accepted by a provider, not delivered. No SMS/email provider or
background dispatcher is configured by default, so no API keys are required and
queued events do not actually send messages.

Authenticated coaches can explicitly queue invite notifications with
`POST /api/coach/invites/{invite_token}/notifications` and an `Idempotency-Key`
header. Reusing the key for the same invite replays the original result; using it
for a different invite under the same instructor returns a conflict. Contacts
come only from that invite's persisted intake. An initial invitation before
intake needs a future coach contact-capture workflow.

Set `SWIM_CLIENT_INVITE_ORIGIN` to the trusted HTTPS origin of the client app
(for example `https://lessons.example.com`). Invitation records snapshot a link
on that origin so adapters can render an actionable message without querying
application storage. Development defaults to `http://127.0.0.1:3001`; staging and
production reject new invite-notification commands if the origin is missing or
invalid. Replays keep their original recipient and invitation URL.

Both public request-status endpoints return only channel, kind, status and UTC
update time. They do not expose recipient details, provider IDs or failure
payloads. Legacy requests have no fabricated delivery history. Client screens
can refresh delivery state after confirmation.

Confirmed, active lessons have Google Calendar links per occurrence and an ICS
download containing all active occurrences. Calendar exports preserve stored
UTC instants, escape text and use stable opaque event identifiers. Downloads are
snapshots: previously imported events do not automatically update when cancelled.
Opening Google Calendar or downloading a file does not prove it was imported.

Apply Alembic migrations before deploying. The notification migration adds the
event ledger and backfills calendar identifiers without sending historical
messages. Downgrading it destroys notification history and calendar identifiers.
Real providers, webhooks, background dispatch and calendar subscriptions are
intentionally outside this implementation.

## Running Tests

Run the full test suite with:

```bash
./venv/bin/python -m pytest -q
```

Database-backed tests require Postgres. By default they use:

```text
postgresql+psycopg://swim:swim@127.0.0.1:5432/swim_scheduler_test
```

Override the test database with `SWIM_TEST_DATABASE_URL`.

Useful backend test slices:

```bash
./venv/bin/python -m pytest -q -m integration
./venv/bin/python -m pytest -q -m "not e2e"
```

The e2e smoke tests are opt-in and run against an already-started API:

```bash
SWIM_E2E_BASE_URL=http://127.0.0.1:8000 ./venv/bin/python -m pytest -q -m e2e
```

Frontend prototype tests:

```bash
cd frontend-instructor-command && npm test
cd frontend-instructor-command && npm run test:e2e
cd frontend-client-invite && npm run test:unit
cd frontend-client-invite && npm run test:e2e
```

Set `VITE_INSTRUCTOR_API_URL=http://127.0.0.1:8000/api` or
`VITE_CLIENT_INVITE_API_URL=http://127.0.0.1:8000/api/public` before running
the frontend e2e commands to exercise a live local backend.
