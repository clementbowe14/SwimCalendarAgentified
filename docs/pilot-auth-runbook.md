# Pilot provider authentication runbook

This runbook covers the database-backed provider accounts and revocable cookie sessions introduced
for the pilot. The instructor frontend and API are one coordinated security boundary: deploy and roll
them forward together.

## Security contract

- Provider passwords are stored only as salted Argon2id hashes in `provider_accounts`.
- A login creates a random opaque session. PostgreSQL stores only its SHA-256 digest.
- The raw credential is carried only in the host-only `swim_session` cookie with `HttpOnly`,
  `SameSite=Strict`, and `Path=/api`. Staging and production also require `Secure`.
- Sessions expire absolutely after eight hours by default and do not slide or refresh.
- Unsafe auth and coach requests require `X-CSRF-Protection: 1`; browser origins must match the exact
  CORS allowlist or the request's own origin.
- The instructor frontend and API must be same-origin or same-site. Unrelated-site deployment and
  `SameSite=None` are unsupported.
- Public invite routes remain unauthenticated, and the public frontend omits credentials.

Never log passwords, request `Cookie` headers, response `Set-Cookie` headers, session values or
digests, or password hashes. Configure the reverse proxy and observability pipeline accordingly.

## Pre-deployment checklist

1. Back up PostgreSQL and confirm the current migration revision.
2. Inventory every pilot instructor ID and the administrator-approved login email for it. One account
   maps to one existing instructor, and an instructor may have only one account.
3. Confirm the instructor frontend and API deployment are same-site.
4. Configure an exact origin allowlist. Do not use `*` with credentialed CORS.
5. Put gateway rate limiting on `POST /api/auth/coach/sign-in`. Application-level login throttling is
   outside issue #54 but remains launch-blocking.
6. Remove `SWIM_COACH_CREDENTIALS` and `SWIM_COACH_AUTH_SECRET` from deployment configuration and
   secret stores. The application no longer reads them.
7. Confirm the target database contains no rows marked as demo accounts or demo invites. Staging and
   production deliberately refuse to start when demo-provenance artifacts are present; restore or
   migrate a clean production database rather than relabeling test records.

Required production settings include:

```text
SWIM_ENV=production
SWIM_DATABASE_URL=postgresql+psycopg://...
SWIM_API_CORS_ORIGINS=https://instructor.example.com
SWIM_ENABLE_DEMO_DATA=false
SWIM_ENABLE_DEMO_AUTH=false
```

`SWIM_ENV` accepts only `local`, `development`, `test`, `staging`, or `production`. An unset or unknown
value fails startup. Demo flags are off by default; enabling either in staging or production also fails
startup. `SWIM_COACH_SESSION_TTL_SECONDS` can override the eight-hour lifetime from 300 through 86,400
seconds; values outside that range fail startup.

## Google Calendar OAuth

Apply the calendar migrations and deploy both OAuth routes before configuring starts. The flow
requires `SWIM_GOOGLE_CALENDAR_OAUTH_CLIENT_ID`, `SWIM_GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET`,
`SWIM_GOOGLE_CALENDAR_OAUTH_REDIRECT_URI`, `SWIM_CALENDAR_CREDENTIAL_ACTIVE_KEY_ID`, and
`SWIM_CALENDAR_CREDENTIAL_KEYS`. Register the exact callback URI ending in
`/api/oauth/google-calendar/callback` and use HTTPS in deployed environments. Set
`SWIM_GOOGLE_CALENDAR_COACH_APP_ORIGIN` only when the coach app has a different origin.
Use an exact same-site origin from `SWIM_API_CORS_ORIGINS` with the same scheme as the callback.
With the override unset, the callback returns to the root of its configured origin.
Send authorization starts to the callback's scheme and hostname so the browser can return the
host-only flow cookie. Configure a trusted proxy to preserve the public Host and scheme.

Configure ingress, reverse proxy, CDN, and tracing to log the callback path without its query
string while forwarding the full request to the API. For example, on NGINX use `$uri` in the
callback access-log format and omit `$request`, `$request_uri`, `$args`, and `$query_string`.
Apply the same exclusion to error logs and request capture. Keep raw ASGI scope tracing off.
Exclude the start response body and flow cookie from observability captures. Verify with a
synthetic callback that only the path appears in request logs.

To drain, block `POST /api/coach/calendar-connections/google/authorize` at ingress. Keep the callback
and its keys available for at least ten minutes after the last permitted start before removing
OAuth configuration or rolling back the routes.

## Migration and provisioning

Apply the additive migration before the application cutover:

```bash
SWIM_DATABASE_URL=postgresql+psycopg://... ./venv/bin/alembic upgrade head
```

The migration does not import legacy environment credentials. A deployment with zero provider
accounts is valid and fails closed: every sign-in returns the generic invalid-credentials response
until an administrator provisions an account.

Provision each account through the interactive administrative command:

```bash
SWIM_ENV=production SWIM_DATABASE_URL=postgresql+psycopg://... \
  ./venv/bin/python -m scripts.manage_provider_auth provision \
  --instructor-id inst_001 --email provider@example.com
```

The command reads the password twice with `getpass`. Do not pass passwords in command arguments,
environment variables, shell history, tickets, or chat. Pilot passwords are 12–256 characters and
must be delivered to the provider over a separate approved channel.

Provisioning fails for a missing instructor, a duplicate normalized email, or a second account for the
same instructor. It never silently remaps an identity.

## Coordinated cutover

1. Apply migrations.
2. Provision and verify every pilot account.
3. Deploy the cookie-auth API and instructor frontend in the same release window.
4. Restart all API workers so startup configuration validation runs everywhere.
5. Require providers to sign in again. Old bearer tokens are intentionally not imported and become
   invalid at cutover.
6. Verify that `/api/auth/coach/sign-in` returns metadata only and sets `swim_session`; verify that a
   reload calls `/api/auth/coach/session` before any `/api/coach/*` request.
7. Verify public invite intake, availability, request, status, and calendar flows without a provider
   session.

Example metadata-only validation (use a protected temporary cookie jar and delete it afterward):

```bash
curl -sS -X POST https://api.example.com/api/auth/coach/sign-in \
  -H 'X-CSRF-Protection: 1' \
  -H 'content-type: application/json' \
  -c /tmp/swim-session.cookies \
  -d '{"email":"provider@example.com","password":"REDACTED"}'

curl -sS https://api.example.com/api/auth/coach/session \
  -b /tmp/swim-session.cookies
```

Do not paste real passwords into shared shell history; the example body is illustrative. Prefer the
browser login for production verification.

## Account operations

All commands lock the account and commit their session changes atomically. The examples below assume
the deployment's `SWIM_ENV` and `SWIM_DATABASE_URL` are exported in the administrative shell.

Reset a password and revoke every existing session:

```bash
./venv/bin/python -m scripts.manage_provider_auth reset-password --email provider@example.com
```

Emergency revoke-all without changing the password:

```bash
./venv/bin/python -m scripts.manage_provider_auth revoke-sessions --email provider@example.com
```

Disable an account and revoke all sessions:

```bash
./venv/bin/python -m scripts.manage_provider_auth disable --email provider@example.com
```

If the disablement is reversible, re-enable it explicitly after identity verification:

```bash
./venv/bin/python -m scripts.manage_provider_auth enable --email provider@example.com
```

Sign-out revokes only the presented browser session; other devices stay active. Reset, revoke-all, and
disablement increment the account session version and invalidate every earlier session. Revocation
guarantees that requests beginning authentication after the transaction commits fail; it cannot cancel
a request that already passed authentication and is executing.

## Incident response

For a suspected stolen session, use `revoke-sessions` immediately. For suspected password compromise,
use `reset-password`; disable the account as well if the provider should have no access. Confirm that a
captured old cookie receives `401 INVALID_AUTH_TOKEN` from both `/api/auth/coach/session` and a coach
route. Rotate unrelated infrastructure secrets only when the incident scope requires it—the session
credential is random and only its digest is stored.

## Rollback limitations

Roll forward whenever possible. The previous application expects environment credentials and bearer
tokens, while the new database accounts cannot be converted back into those credentials. Restoring
`SWIM_COACH_CREDENTIALS` or `SWIM_COACH_AUTH_SECRET` is prohibited as a rollback shortcut.

Migration `20261109_0009` also changes appointment idempotency uniqueness from a global key to the
partial unique index `uq_appointments_instructor_idempotency_key` on
`(instructor_id, idempotency_key) WHERE idempotency_key IS NOT NULL`. Its downgrade first checks for
the same non-null key used by multiple providers. If any exist, downgrade aborts without rewriting or
deleting data because the old global index cannot be restored safely. Resolve the duplicate-key
rollback plan explicitly before retrying.

Downgrading the auth migration removes provider account/session records. Take a verified backup and
schedule provider reprovisioning before any database downgrade. A frontend-only or backend-only
rollback is unsupported because it breaks the shared cookie contract.

## Local and CI demo mode

Known demo records and credentials exist only when explicitly enabled:

```bash
SWIM_ENV=test \
SWIM_ENABLE_DEMO_DATA=true \
SWIM_ENABLE_DEMO_AUTH=true \
./venv/bin/uvicorn app.api:app --host 127.0.0.1 --port 8000
```

Requesting a known demo invite token does not seed data. When the demo flags are disabled or records
are absent, it returns the standard non-enumerating not-found response. Demo accounts and invite
tokens retain explicit provenance, so turning the flags off also disables artifacts left by an earlier
demo run. A staging or production process fails startup if those artifacts are present in a copied or
restored database.
