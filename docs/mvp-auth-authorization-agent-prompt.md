# Staff engineer prompt: pilot provider authentication

Implement and maintain the provider authentication boundary defined by `specs/issue-54.md`. Treat that
specification as binding. Do not reintroduce environment credentials, stateless bearer tokens, or a
browser-readable session.

## Architecture

- One `provider_accounts` row maps uniquely to one existing instructor.
- Passwords use Argon2id. Provisioning and reset use the interactive administrative command; passwords
  never come from command arguments, environment JSON, fixtures, logs, or responses.
- Each login creates an opaque random session. PostgreSQL stores only its SHA-256 digest and validates
  expiry, revocation, account status, session version, and instructor mapping on every protected request.
- The raw value is set only in the host-only `swim_session` cookie: `HttpOnly`, `SameSite=Strict`,
  `Path=/api`, and `Secure` in staging/production.
- Sign-out revokes the current session. Password reset, revoke-all, and disablement atomically increment
  the account session version and revoke every existing session.
- Every `/api/coach/*` route inherits authentication from the router. Services receive only the
  authenticated instructor ID, and cross-provider resource misses remain non-enumerating.
- `/api/public/*`, `/health`, and `/openapi.json` remain unauthenticated.

## HTTP and browser contract

- `POST /api/auth/coach/sign-in` returns only `authenticated`, `expires_at`, and the coach display
  object. `/login` is a deprecated alias.
- `GET /api/auth/coach/session` is the first live-mode request on every initial load and reload.
- `POST /api/auth/coach/sign-out` is bodyless, revokes the cookie session, and is idempotent after CSRF
  validation. `/logout` is a deprecated alias.
- Unsafe auth and coach requests require `X-CSRF-Protection: 1`. Present browser origins must match the
  exact configured allowlist or the request's own origin; `null` and untrusted origins fail.
- Instructor fetches use `credentials: "include"` and never `Authorization`. Public-client fetches use
  `credentials: "omit"`.
- The instructor app stores session metadata only in memory. It deletes the obsolete
  `swim-command:coach-session:v1` key without reading it, gates protected UI behind `/session`, treats
  only `401` as session expiry, and prevents stale in-flight responses from restoring protected state.

## Configuration and operations

`SWIM_ENV` is required and accepts only `local`, `development`, `test`, `staging`, or `production`.
Demo scheduling data and the known demo account are disabled by default. They require explicit
`SWIM_ENABLE_DEMO_DATA=true` and `SWIM_ENABLE_DEMO_AUTH=true` in local/development/test; either flag
fails startup in staging/production. Runtime reads of `SWIM_COACH_CREDENTIALS` and
`SWIM_COACH_AUTH_SECRET` are forbidden.

The supported deployment is same-origin or same-site. Wildcard credentialed CORS, unrelated-site
cookies, and `SameSite=None` are out of scope. Follow `docs/pilot-auth-runbook.md` for account
provisioning, coordinated cutover, emergency revocation, disablement, and rollback limitations.

## Required verification

- PostgreSQL migration/model tests for account/session constraints and provider-scoped appointment
  idempotency.
- Password hashing, generic credential failure, rehash, expiry boundary, revocation, sign-out replay,
  multi-session, reset, disablement, collision retry, and deterministic concurrency tests.
- Route-graph authentication coverage plus two-provider isolation across every protected resource class.
- CSRF/origin precedence, exact CORS, no-store, demo gating, and no request-triggered demo seeding.
- Generated OpenAPI with an `apiKey` cookie scheme named `swim_session`, no bearer schema, and no public
  security requirement. Auth fixtures contain metadata only.
- Instructor unit/intercepted/live coverage for credentials, reload ordering, no web-storage credential,
  `401` versus `403`, sign-out failure, stale responses, cookie flags, and revoked-cookie replay.
- Public client unit/intercepted/live coverage proving credentials are omitted and all invite flows still
  work without provider authentication.
- CI must run with explicit test/demo flags and zero silent live-suite skips.

Do not broaden this work into self-service signup/recovery, OAuth, MFA, staff roles, refresh tokens,
session-management UI, public-token redesign, or navigation changes. Login rate limiting is a separate
launch-blocking gateway requirement.
