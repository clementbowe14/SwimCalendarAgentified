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

### Local Frontend CORS

By default, the API allows local frontend development origins on common ports:

- `http://127.0.0.1:3000`
- `http://127.0.0.1:3001`
- `http://127.0.0.1:3002`
- `http://127.0.0.1:4173`
- `http://127.0.0.1:5173`
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

### MVP Public Invite Token

The MVP public API includes a seeded development invite token:

```text
demo-invite-token
```

Example public invite request:

```bash
curl http://127.0.0.1:8000/api/public/invites/demo-invite-token
```

Public routes are scoped by invite/request tokens and do not require clients to submit raw instructor or client IDs.

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

Errors use a stable JSON shape:

```json
{
  "error": {
    "code": "INVITE_NOT_FOUND",
    "message": "The invite could not be found."
  }
}
```

## Tests

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
