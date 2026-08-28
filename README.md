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
