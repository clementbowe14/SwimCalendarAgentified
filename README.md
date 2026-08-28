# Swim Calendar Agentified

Python prototype for travel-aware swim lesson scheduling.

## Local API

The repository exposes the scheduling prototype through a local FastAPI app.

### Setup

Create or use the existing virtual environment, then install dependencies:

```bash
./venv/bin/python -m pip install -r requirements.txt
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
- `http://localhost:3000`
- `http://127.0.0.1:3001`
- `http://localhost:3001`
- `http://127.0.0.1:3002`
- `http://localhost:3002`
- `http://127.0.0.1:4173`
- `http://localhost:4173`
- `http://127.0.0.1:5173`
- `http://localhost:5173`

Override the list with a comma-separated environment variable:

```bash
SWIM_API_CORS_ORIGINS=http://127.0.0.1:3001,http://127.0.0.1:3002 ./venv/bin/uvicorn app.api:app --host 127.0.0.1 --port 8000
```

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
