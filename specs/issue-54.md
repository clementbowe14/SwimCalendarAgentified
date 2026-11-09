# Issue 54: Revocable pilot-provider sessions

## Objective

Replace environment-supplied coach credentials, self-contained bearer tokens, and browser `localStorage` authentication with production-grade pilot-provider accounts and revocable server-side sessions.

The completed flow must:

- store provider passwords as salted Argon2id hashes;
- issue an opaque high-entropy session only in an `HttpOnly` cookie;
- validate, expire, and revoke that session from the database on every protected request;
- derive the provider tenant from the authenticated account rather than frontend input;
- validate the cookie before the instructor app requests or renders protected data; and
- leave every public invite route unauthenticated and free of coach credentials.

This is a coordinated backend/frontend cutover. Existing stateless bearer credentials are intentionally not migrated and become invalid when the change deploys.

## Current state

The existing implementation is development-only:

1. `app/auth.py` reads `SWIM_COACH_CREDENTIALS`, permits plaintext or unsalted SHA-256 passwords, and falls back to a known demo login unless `SWIM_ENV` matches one of three strict values.
2. Successful login returns an eight-hour HMAC bearer token containing the instructor ID. The token has no server-side record and cannot be revoked individually.
3. Sign-out changes no server state, so a copied bearer remains usable.
4. `app/dependencies.py` accepts only `Authorization: Bearer` and protected coach routes opt into the dependency handler by handler.
5. The instructor frontend stores the bearer response in `localStorage`, reads it before bootstrap, and synthesizes authorization headers for every protected fetch and calendar download.
6. Browser bootstrap does not call `/session` unless JavaScript first finds a stored bearer, so it cannot discover an `HttpOnly` cookie.
7. `401` and `403` are both treated as session expiry even though a valid provider can receive a non-authentication authorization error.
8. There are no account or session tables, and startup demo seeding is not separated from authentication seeding.

The existing positive boundary should remain: protected services already receive the authenticated `instructor_id` and tenant-scoped resource misses generally return `404`; `/api/public/*` is a separate unauthenticated router.

## Security and storage decisions

### Provider accounts

Add `provider_accounts` with:

- `id`: opaque string primary key;
- `instructor_id`: non-null foreign key to `instructors.id` and unique, enforcing one pilot account per provider;
- `email_normalized`: non-null, lowercase normalized email and unique;
- `password_hash`: non-null Argon2id PHC string;
- `is_demo`: non-null provenance flag, false for every administratively provisioned account;
- `session_version`: non-null integer starting at `1`;
- `disabled_at`: nullable timestamp;
- `password_changed_at`, `created_at`, and `updated_at`: timezone-aware timestamps.

Every account maps to exactly one existing instructor. Provisioning must fail rather than silently remap a duplicate email or instructor.

Use `argon2-cffi` with Argon2id at OWASP's minimum interactive profile: 19 MiB memory, two iterations, one lane, a 16-byte random salt, and a 32-byte output. The PHC string carries its salt and cost parameters. Successful login transparently rehashes a password when the configured parameters change.

Normalize account emails by validating them with `EmailStr`, trimming surrounding whitespace, and applying Unicode `casefold()` before storage and lookup. Passwords are not trimmed or Unicode-normalized; the exact UTF-8 value entered at provisioning is verified at sign-in.

Unknown email, disabled account, and wrong password all return the same `INVALID_CREDENTIALS` response. Authentication performs a dummy Argon2 verification for an unknown email so the obvious account-enumeration timing shortcut is absent. Passwords never appear in command-line arguments, environment JSON, application logs, fixtures, or response bodies.

### Server-side sessions

Add `provider_sessions` with:

- `id`: opaque string primary key;
- `account_id`: non-null foreign key to `provider_accounts.id`;
- `token_digest`: unique 32-byte digest;
- `session_version`: the account version captured at issuance;
- `created_at` and `expires_at`: timezone-aware timestamps;
- `revoked_at` and `revocation_reason`: nullable revocation metadata; and
- indexes supporting digest lookup, account revocation, and expiry cleanup.

Generate each client credential with `secrets.token_urlsafe(32)`, providing 256 bits of CSPRNG entropy. Store only `SHA-256(raw_token)` in `token_digest`; the raw token exists only long enough to set the cookie. A fast hash is correct for this independently random credential, while provider passwords use Argon2id. Retry a unique-digest collision with a fresh token a bounded number of times and fail without issuing a cookie if persistence still cannot succeed.

Session lifetime remains an absolute eight hours by default. `SWIM_COACH_SESSION_TTL_SECONDS` may configure it within documented safe bounds; tests mutate timestamps directly and must not sleep. There is no sliding expiry or refresh token in this issue.

Session validation rejects a row when any of these are true:

- the cookie is malformed or its digest is unknown;
- `expires_at <= now`;
- `revoked_at` is set;
- the account is disabled;
- the account has demo provenance while demo authentication is disabled;
- the session version differs from the account version; or
- the mapped instructor no longer exists.

Sign-in locks the account while verifying credentials and inserting the new session. After valid credentials, any session cookie already presented by that browser is revoked as the fresh session is created; other devices are not affected. Password reset, administrative revocation, and disablement lock that same account row, increment `session_version`, and revoke existing rows in one transaction. Therefore a concurrent login either completes before the administrative action and is invalidated by it, or observes the new account state. Revocation guarantees that requests beginning validation after the revocation commit fail; it cannot cancel a request that already completed authentication.

Sign-out revokes only the presented session. Other active devices remain signed in until their own sign-out, expiry, password reset, administrative revoke-all, or account disablement.

## Cookie contract

Use one host-only cookie named `swim_session` with:

- `HttpOnly` always;
- `Secure` in staging and production, with no configuration that can disable it there;
- `SameSite=Strict` explicitly;
- `Path=/api`;
- no `Domain` attribute;
- `Max-Age` and `Expires` matching the absolute server-side expiry.

Local HTTP cookies are permitted only when `SWIM_ENV` is explicitly `local`, `development`, or `test`. `SWIM_ENV` is required and accepts only `local`, `development`, `test`, `staging`, or `production`; an unset or unknown value fails startup. The supported pilot topology is same-site frontend and API; unrelated-site deployment and `SameSite=None` are out of scope.

Deleting the cookie must repeat the same name and path. Authentication and coach responses carry `Cache-Control: private, no-store`; sign-out also clears the obsolete browser auth state through frontend code rather than relying on cookie deletion alone. A protected invalid-session `401` also expires the stale cookie while preserving the stable JSON error body.

## HTTP contract

### Sign in

`POST /api/auth/coach/sign-in` is canonical. Retain `/login` as a deprecated compatibility alias implemented by the same handler.

Request:

```json
{
  "email": "provider@example.com",
  "password": "provider password"
}
```

Successful response sets the cookie and exposes metadata only:

```json
{
  "authenticated": true,
  "expires_at": "2026-11-09T21:00:00Z",
  "coach": {
    "instructor_id": "inst_001",
    "display_name": "Coach Sarah"
  }
}
```

The JSON contract contains no `access_token`, `token_type`, raw session value, or reusable credential.

### Validate session

`GET /api/auth/coach/session` validates the cookie and returns the same non-secret session metadata. It is the mandatory first live-mode browser request on every initial load and reload.

- Missing cookie: `401 AUTHENTICATION_REQUIRED`.
- Malformed, unknown, expired, revoked, disabled, or stale-version cookie: the existing stable `401 INVALID_AUTH_TOKEN` response and message. The legacy code name remains temporarily for contract compatibility even though the transport is now a cookie.

### Sign out

`POST /api/auth/coach/sign-out` is canonical, accepts no request body, revokes the matching row when one exists, and expires the cookie. Retain `/logout` as a deprecated alias of the same handler.

Sign-out is idempotent: a missing, expired, already-revoked, or unknown cookie still returns `200` and clears the browser cookie. This is a deliberate exception to protected-route invalid-session handling. Replaying a captured cookie against `/session` or any `/api/coach/*` route after successful sign-out returns `401 INVALID_AUTH_TOKEN`.

### Protected and public routes

Document authentication with an OpenAPI `apiKey` cookie security scheme. Apply session validation at the `/api/coach` router level so future coach routes are protected by default; retain the cached handler dependency wherever the `CoachContext` value is needed. A provider-security middleware validates authentication and CSRF before FastAPI decodes request bodies, caches the principal on request state, and therefore preserves the stable `401`/`403` precedence even for malformed JSON without duplicate session queries.

Only the server-authenticated `instructor_id` reaches protected services. Frontend-supplied provider IDs never authorize data. Cross-provider resource lookups remain non-enumerating `404` responses where that is the established contract.

Close the two tenant gaps exposed by exhaustive auth testing without broad service refactoring:

- filter request appointments by both `request_id` and authenticated `instructor_id`; and
- scope every appointment idempotency lookup and uniqueness by `(instructor_id, idempotency_key)` rather than a globally unique key. This includes direct booking in `app/db.py` and `app/tools.py` plus confirmation replay lookup in `app/api_services.py`.

The auth migration replaces `uq_appointments_idempotency_key` with the exact partial unique index `uq_appointments_instructor_idempotency_key` on `(instructor_id, idempotency_key) WHERE idempotency_key IS NOT NULL`, because the provider-isolation acceptance criterion otherwise permits a cross-provider conflict/existence oracle. Downgrade drops the composite index and restores the original global partial unique index only after a preflight confirms there are no duplicate non-null keys across providers; if duplicates exist, downgrade aborts with an actionable error rather than deleting or rewriting data. This rollback limitation belongs in the runbook.

`/api/public/*`, `/health`, and `/openapi.json` remain unauthenticated and carry no cookie security requirement. The client-invite frontend explicitly uses `credentials: "omit"` so it does not transmit a coach cookie even when both apps share an origin.

Bearer headers, URL tokens, and request-body session tokens are not accepted as alternate authentication mechanisms.

## CSRF and browser-origin policy

Cookie authentication requires an explicit CSRF posture. Every instructor/auth fetch using an unsafe method (`POST`, `PUT`, `PATCH`, or `DELETE`) sends `X-CSRF-Protection: 1`. For unsafe methods on auth and coach routes:

1. require the exact custom header value, including for bodyless mutations and sign-in/sign-out;
2. validate a present `Origin` header against the exact configured CORS origins plus the request's own origin;
3. reject a missing/incorrect custom header, `Origin: null`, or an untrusted origin with stable `403 CSRF_VALIDATION_FAILED` before executing the command; and
4. allow a missing `Origin` for non-browser clients only when the custom header is present.

Coach authentication takes precedence: a missing/invalid session on `/api/coach/*` still returns its stable `401` before CSRF validation. Sign-in has no prior session and validates CSRF first. Sign-out remains idempotent only after CSRF validation succeeds.

Malformed JSON cannot bypass that ordering: coach mutations authenticate before parsing, and unsafe auth mutations validate CSRF before parsing. Validation errors on both sign-in aliases omit submitted input entirely so a password cannot be echoed when another field is invalid.

These controls complement `SameSite=Strict`; they do not apply to unauthenticated public-invite commands. CORS continues to use exact origins with `allow_credentials=True`, rejects wildcard origins outside local/test mode, and replaces `allow_headers=["*"]` with `Content-Type`, `Accept`, `Idempotency-Key`, and `X-CSRF-Protection`.

## Demo and production configuration

Remove all runtime reads of `SWIM_COACH_CREDENTIALS` and `SWIM_COACH_AUTH_SECRET`. There is no password or session backfill from those values.

Startup scheduling/client/invite demo data is also explicit: `db.init_db()` calls `seed_data()` only when `SWIM_ENABLE_DEMO_DATA=true` in an allowed local/test mode. Provider accounts and invite tokens persist an `is_demo` provenance flag. Existing demo accounts, sessions, invite tokens, and request-token descendants are rejected whenever their respective demo flag is disabled. Staging and production startup additionally fails if any demo-provenance account or invite remains in the database, protecting deployments restored from a development snapshot. Direct test helpers may still call `seed_data()` deliberately against isolated databases. Remove the request-time fallback in `app/public_tokens.py`: resolving the known demo invite must never call `seed_data()` or create records. When demo data is disabled or absent, that token returns the ordinary non-enumerating invite-not-found response.

The known demo account may be seeded only when all conditions hold:

- `SWIM_ENABLE_DEMO_DATA=true`;
- `SWIM_ENABLE_DEMO_AUTH=true`; and
- `SWIM_ENV` is explicitly `local`, `development`, or `test`.

Demo data and auth are disabled by default. Enabling either in staging or production fails startup rather than silently creating known provider/client/invite data or a known credential. CI and local-live commands set the test/development mode and both flags explicitly.

Application startup with no provider accounts is valid and fails closed: every sign-in returns generic invalid credentials until an administrator provisions an account.

## Administrative workflow

Add an interactive, non-HTTP command such as `scripts/manage_provider_auth.py` with:

- `provision --instructor-id <id> --email <email>`;
- `reset-password --email <email>`;
- `revoke-sessions --email <email>`; and
- `disable --email <email>` (plus an explicit re-enable operation if disablement is implemented as reversible).

Provision and password-reset commands read the password twice with `getpass`; there is no password command-line option. Enforce a documented pilot minimum of 12 characters and a maximum of 256 characters. Reset, revoke-all, and disable operations increment `session_version` and revoke sessions atomically. Commands return concise success/failure output without hashes, tokens, or passwords.

All application SQLAlchemy engines enable parameter hiding. Administrative commands translate database failures into generic, non-chained output so password hashes and session digests cannot escape through statement-error parameters or tracebacks.

Document migration, provider provisioning, password reset, emergency session revocation, disablement, and rollback limitations in a pilot-auth runbook. Because old bearer tokens cannot be revoked or imported, backend and instructor frontend deploy as one cutover after accounts are provisioned.

## Instructor frontend behavior

### Bootstrap gate

Remove the auth store and all bearer/local-storage parsing. On every live-mode load:

1. remove the obsolete `swim-command:coach-session:v1` key without reading or using it;
2. render a dedicated “Checking your session…” state;
3. call `/api/auth/coach/session` unconditionally with `credentials: "include"`; and
4. request/render `/api/coach/*` data only after validation succeeds.

A session-validation `401` renders the existing sign-in screen. A network or `5xx` failure renders a retryable “Session check unavailable” state instead of claiming the instructor is signed out. Neither state renders stale protected data.

### Authenticated requests

Every auth and coach fetch, including `.ics` downloads, uses `credentials: "include"` and never creates an `Authorization` header. The frontend stores only non-secret coach/session metadata in memory.

Only `401` is an authentication failure. A protected `401` clears all protected in-memory state and shows “Session expired.” A `403` remains an actionable permission/security error and must not relabel the session as expired.

### Sign in and sign out

Successful sign-in trusts the cookie response, stores only returned metadata in memory, and then loads the command center.

Sign-out immediately hides protected UI and starts the bodyless revocation request. It reports success only after a `200`. If revocation cannot be confirmed, show a protected-data-free retry state; do not restore the command center from stale in-flight responses.

Use an authentication generation counter or equivalent cancellation guard. Increment it when sign-in starts, sign-out starts, or a `401` expires the session. Async snapshot, availability, calendar, and mutation responses from an older generation must not repopulate protected state.

Mock mode remains explicitly unauthenticated and does not emulate browser credential storage.

## Required implementation surface

### Backend and database

- `app/models.py`: provider account/session models and relationships.
- New Alembic migration after `20260922_0008` with auth tables, foreign keys, uniqueness, checks, indexes, and the provider-scoped appointment idempotency index replacement.
- `app/auth.py`: account authentication, Argon2 rehash, opaque session issue/validation/revocation, and administrative operations.
- `app/config.py`: environment mode, cookie/session settings, explicit demo-auth validation, and shared trusted origins.
- `app/dependencies.py`: cookie security scheme, centralized session context, and unsafe-request CSRF/origin enforcement.
- `app/routes/auth.py`: metadata-only sign-in/session responses and set/delete-cookie behavior.
- `app/routes/coach.py`: router-level protection.
- `app/api_schemas.py` and `app/api_errors.py`: metadata contract and stable origin error.
- `app/api.py`: shared CORS settings and sensitive-response no-store policy.
- `app/db.py`, `app/tools.py`, and `app/api_services.py`: explicit local/test scheduling/auth demo seeding and provider-scoped appointment/idempotency reads.
- `app/public_tokens.py`: remove request-triggered demo seeding.
- `requirements.txt`: Argon2 dependency.
- `scripts/manage_provider_auth.py`: interactive administrative workflow.

### Frontends

- `frontend-instructor-command/src/api/types.ts`: non-secret session DTO and simplified interface.
- `frontend-instructor-command/src/api/instructorApi.ts`: credentialed-cookie fetches, unsafe-request CSRF header, no auth store/authorization header, bodyless sign-out, and `401`-only expiry classification.
- `frontend-instructor-command/src/main.ts` and minimal styles: unconditional validation, retry/checking/sign-out states, state clearing, and stale-response protection.
- Existing settings sign-out placement remains; no navigation redesign is required.
- `frontend-client-invite/src/api/clientInviteApi.ts`: explicit `credentials: "omit"` for public calls.
- Live client setup uses a cookie-preserving API request context when it must perform a coach-only test action.

### Contracts and operations

- Regenerate `contracts/openapi.json` and coach auth fixture.
- Update contract, root, instructor, frontend-alignment, and internal auth documentation to remove bearer/local-storage guidance.
- Update CI and documented live-server commands to enable demo auth only through explicit test/development settings.

## Required tests

### Backend and migration

1. Migration creates both tables with provider/account uniqueness, token-digest uniqueness, foreign keys, account/session-version constraints, and required indexes. It replaces the global appointment idempotency index with the exact composite partial index. Downgrade restores the global index when safe and aborts without data loss when cross-provider duplicate keys make restoration impossible.
2. Provisioning stores an Argon2id PHC hash, uses unique salts for equal passwords, rejects a missing instructor, duplicate email, and second account for one provider, and handles concurrent duplicate provisioning without leaking database errors.
3. Correct password signs in; wrong password, unknown email, and disabled account share the exact generic `401`. Unknown email still executes the dummy verification path. A stale Argon2 profile is rehashed on successful sign-in.
4. Sign-in JSON contains no bearer or reusable credential. The database contains only the digest, never the raw cookie.
5. Cookie tests assert name, path, host-only scope, `HttpOnly`, explicit `SameSite=Strict`, expiry/max-age, `Secure` in production, and the explicit local/test exception.
6. Missing cookies return `AUTHENTICATION_REQUIRED`; malformed, unknown, expired-at-boundary, revoked, disabled-account, and stale-version sessions return the exact stable invalid-session `401` and expire the stale browser cookie.
7. Sign-out revokes the matching row and clears the cookie. Replaying the captured value fails. Repeated sign-out succeeds. Signing out one session does not revoke another session for the same account.
8. Password reset, revoke-all, and disablement invalidate every earlier session, including a login racing the administrative action.
9. Introspect the FastAPI route graph to prove every `/api/coach/*` operation inherits the router dependency. A valid session reaches representative reads and mutations; bearer-only requests fail.
10. Two provisioned providers remain isolated across profile, schedule, snapshot, settings, booking/request, notification, email-delivery, and calendar-handoff boundaries. Established cross-tenant misses remain `404`. Request-appointment and every appointment-idempotency lookup include provider scope; two providers can safely use the same idempotency key without conflict or disclosure.
11. Public invite, intake, availability, request, alternative-selection, status, and calendar routes remain usable without a coach cookie.
12. Correct custom-header plus trusted-origin mutations succeed. Missing/incorrect headers and untrusted/null origins receive `403 CSRF_VALIDATION_FAILED` before persistence. Non-browser requests without `Origin` work only with the custom header. Public mutations require neither.
13. Production never reads environment credential JSON, implicit demo data/auth are off by default, explicit local/test demo seeding works, and enabling either demo flag outside an allowed mode fails closed. Demo provenance prevents previously seeded credentials, sessions, invites, and request descendants from working after the flags are disabled, and deployed startup rejects a copied database containing demo artifacts.
14. Missing/invalid coach sessions and invalid auth CSRF fail with their stable `401`/`403` before malformed JSON is decoded. Sign-in validation errors never include submitted password input. SQL and CLI failures never serialize bound hashes or digests.
15. Startup rejects unset/unknown environments, insecure deployed cookie settings, and wildcard credentialed CORS. Captured application logs contain no passwords, raw cookies, hashes, token digests, or `Set-Cookie` values.

### Instructor unit and intercepted browser tests

1. Adapter sign-in/session/protected/sign-out/calendar requests all use `credentials: "include"`, never `Authorization`, add `X-CSRF-Protection: 1` exactly to unsafe auth/coach requests, and sign-out sends no body.
2. Auth responses contain metadata only; no code reads or writes an access token. The old local-storage key is removed and no replacement credential is script-readable.
3. A delayed `/session` response shows only the checking state and causes zero `/coach/*` requests or protected render.
4. Initial `401` shows sign-in; successful sign-in sets an `HttpOnly` cookie and loads the command center; invalid credentials remain locked. Browser calls with missing CSRF headers or untrusted origins are rejected without optimistic UI state.
5. Reload validates the cookie first and then restores the command center without a second login.
6. Session expiry and administrative revocation responses clear protected UI. A provider `403` shows a permission error without signing out.
7. Successful sign-out clears the browser cookie and locks the app; replaying a captured pre-sign-out cookie receives `401`.
8. A failed sign-out displays a retry state with no protected data.
9. A delayed protected response cannot repopulate data after sign-out or expiry.
10. Authenticated booking, confirmation, delivery, and calendar handoff flows remain green under cookie auth.
11. Public client fetches explicitly omit credentials.

### Live FastAPI/browser coverage

1. Sign in through the UI, assert the response body has no token, inspect the browser cookie attributes, and assert `document.cookie`/`localStorage` expose no session credential.
2. Reload and prove `/session` finishes before the first protected coach call/render.
3. Exercise representative schedule, availability, booking, confirmation, and calendar read-back through cookie-authenticated browser/context requests.
4. Sign out, restore/replay the captured cookie through an isolated request context, and verify `/session` and a coach route return the stable invalid-session `401`.
5. Keep backend provider-isolation tests paired with an intercepted browser authorization-failure test so the UI does not convert a valid-session `403` into expiry.
6. Run the public client live suite without a coach session and verify the workflow remains unchanged.

## Acceptance mapping

- No production environment credentials: old credential/secret variables are unused and provider accounts are provisioned in the database.
- Production-grade secrets: Argon2id passwords with unique salts; opaque 256-bit sessions stored only as SHA-256 digests.
- Revocation and expiry: every protected request checks server state, sign-out commits revocation, and replay fails.
- Browser-safe transport: the credential exists only in a host-only `Secure`, `HttpOnly`, `SameSite=Strict` cookie and never in JSON or web storage; unsafe commands require the CSRF header and a trusted browser origin.
- Protected bootstrap: `/session` gates all coach data requests and rendering.
- Provider isolation: account-to-instructor mapping is unique and every coach route derives tenant scope from it.
- Public workflow: public routers remain security-free and the public frontend omits cookies.
- Operational pilot support: administrators can provision, reset, revoke, and disable without exposing passwords or tokens.

## Non-goals

- self-service account creation, password recovery, or email change;
- OTP, phone-number authentication, social login, SSO, or managed identity-provider integration;
- staff accounts, roles, delegated access, or multi-provider accounts;
- refresh tokens, sliding sessions, remembered devices, or session-management UI;
- unrelated-site frontend/API deployment or `SameSite=None`;
- client authentication or changes to public invite-token semantics;
- application-level login throttling or public-endpoint rate limiting; login throttling remains a launch-blocking gateway concern and must be tracked separately;
- expired/revoked session-row cleanup, audit dashboards, or session/device-management UI;
- redesigning command-center navigation or scheduling behavior.
