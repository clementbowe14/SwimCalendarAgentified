# Codebase audit: GitHub issue candidates

Audited: `master` at `78bd947b` (2026-09-22).

Two independent staff-level reviews covered observability and architectural
simplification. These are issue drafts, not an instruction to implement all of
them. Each item is intentionally separate unless the work shares one direct
technical boundary. Priorities describe risk and sequencing, not a commitment.

Issue #17 already covers persisted scheduling-policy injection and has its own
reviewed specification in `specs/issue-17.md`; do not create a duplicate issue
from the architecture audit.

## Immediate observability and privacy

### 1. Sanitize HTTP access logging and add request correlation

**Problem:** Public invite and request tokens are bearer capabilities embedded
in URL paths. Uvicorn's default access log can record concrete request targets,
while the application has no request ID for correlating failures. Relevant code:
`app/routes/public.py`, `app/api.py`, both frontend API adapters, and the startup
commands in `README.md`.

**Scope:** Add middleware that accepts a valid bounded correlation header or
generates an opaque ID, returns it on every response, and emits one structured
completion event with method, route template, status, duration, and request ID.
Disable or replace raw Uvicorn access records so concrete tokens/query strings
cannot be duplicated. Centralize redaction rules for authorization, cookies,
tokens, idempotency keys, bodies, addresses, emails, and phone numbers.

**Acceptance criteria:**

- Logs use `/api/public/client-invites/{token}`, never a concrete token.
- Success and handled/unhandled errors return the same request-ID header.
- Authorization, cookies, query strings, bodies, contact data, and addresses do
  not appear in logs.
- Malformed or oversized caller IDs are replaced, not reflected.
- Existing cache and referrer protections remain unchanged.

**Testing:** Capture structured logs for success, 4xx, validation, and 500 paths;
seed fake secrets and PII and assert absence; run both live frontend suites.

**Non-goals:** Distributed tracing, payload logging, or changing public URLs.

### 2. Log unexpected exceptions with safe structured context

**Problem:** `app/api_errors.py` returns a safe generic 500 but discards the
exception and traceback, leaving production failures undiagnosable.

**Scope:** Emit exactly one error event for an unexpected exception with
traceback, request ID, route template, method, and stable event name. Expected
domain/API errors should not produce misleading stack traces. Apply the common
redaction filter before output.

**Acceptance criteria:**

- Unexpected exceptions produce one useful error event and retain the existing
  sanitized `INTERNAL_SERVER_ERROR` response.
- Expected errors remain normal request-completion events.
- Exception messages containing secrets, tokens, contacts, or addresses cannot
  leak those values.

**Testing:** Extend public error tests with captured-log assertions for a seeded
exception containing every forbidden data class.

**Dependency:** Issue 1's request context and redaction foundation.

**Non-goals:** Hosted error tracking or client-visible debug details.

### 3. Split liveness from dependency-aware readiness

**Problem:** `/health` always returns OK and CI treats it as readiness even when
Postgres or required routing configuration is unavailable.

**Scope:** Keep a lightweight liveness endpoint; add bounded readiness checks
for database connectivity and selected routing-mode configuration. Return only
safe machine-readable component status. Update CI polling and deployment docs.

**Acceptance criteria:**

- Liveness remains successful during a simulated database outage.
- Readiness becomes non-2xx when Postgres is unavailable.
- Mapbox-selected mode reports missing configuration without exposing secrets;
  local Haversine mode remains ready without Mapbox.
- Probes have short timeouts and CI waits on readiness.

**Testing:** Healthy/unavailable database and routing-mode configuration tests,
with negative assertions for URLs, credentials, exception text, and host data.

**Non-goals:** Calling third parties on every probe or automating migrations.

### 4. Add operational visibility for notification processing

**Problem:** The durable notification ledger is strong, but provider exceptions,
retries, expired leases, stale completions, exhausted attempts, disabled
dispatch, and backlog growth are silent outside direct database inspection.

**Scope:** Emit privacy-safe state-transition events and provide one documented
operational query/command for queued count, oldest age, processing count, and
failed count by safe code. Explicitly report disabled dispatch. Include event ID,
channel, kind, attempt, safe failure code, and available request/correlation ID;
exclude recipient and message context.

**Acceptance criteria:**

- Every terminal failure has a corresponding safe operational event.
- Provider exceptions retain exception class/traceback without recipient,
  invitation URL, address, template context, or credentials.
- Disabled dispatch is distinguishable from an empty queue.
- Operators can determine backlog count and oldest age without schema research.
- Existing retry, lease, and idempotency behavior is unchanged.

**Testing:** Add captured-log assertions to existing retry/crash/lease tests and
validate the operational query against seeded queue states.

**Dependencies:** Issues 1 and 2.

**Non-goals:** A provider integration, new queue platform, or notification UI.

### 5. Persist a narrow audit trail for authenticated coach mutations

**Problem:** Settings updates, bookings, confirmations, alternatives, declines,
and invite sends do not durably record who performed the mutation or which
request initiated it.

**Scope:** Add an append-only audit table for those authenticated mutations.
Store event/time, instructor actor, action, target type and opaque ID, request ID,
and small allowlisted metadata. Write it in the domain transaction where
possible and distinguish idempotent replay from a new mutation.

**Acceptance criteria:**

- Each listed successful mutation creates exactly one appropriate audit fact.
- Failed/rolled-back mutations create no false success record.
- Idempotent replay does not duplicate a creation fact.
- No client name, contact, address, auth/invite token, free text, or full row
  snapshot is stored.

**Testing:** Postgres integration tests for atomicity, actor/target/action,
failure, rollback, and replay; document downgrade history loss.

**Dependency:** Issue 1 for request IDs.

**Non-goals:** Event sourcing, full diffs, compliance tooling, or read auditing.

### 6. Preserve backend diagnostics on CI smoke-test failure

**Problem:** CI redirects FastAPI output to `/tmp/swim-api.log` but neither shows
nor uploads it when startup or live browser checks fail.

**Scope:** Add failure-only steps to print a bounded redacted tail and optionally
upload a sanitized short-retention artifact. Include readiness status in startup
failure output.

**Acceptance criteria:** Controlled startup and live-test failures expose useful
diagnostics; successful jobs create no noise; tokens, credentials, contacts, and
addresses never appear.

**Testing:** Exercise controlled failures on a temporary branch and inspect job
output/artifacts for both diagnostic value and redaction.

**Dependency:** Issue 1 must land first so CI does not retain unsafe access logs.

**Non-goals:** Changing the test matrix or long-term log retention.

## Architecture and simplification

### 7. Establish one canonical public invite workflow

**Problem:** Legacy `/invites` and `/requests` paths in `app/api_services.py` and
the production `/client-invites` and `/lesson-requests` paths in
`app/client_invites.py` independently implement availability, location/error
translation, request creation, and status mapping.

**Scope:** Declare the client-invite API canonical, inventory legacy consumers,
and retire unused legacy behavior or reduce retained routes to thin adapters over
shared application functions. Consolidate status and routing-error mapping.

**Acceptance criteria:**

- Slot discovery and request creation have one authoritative implementation.
- Retained API shapes produce equivalent scheduling outcomes and safe errors.
- Compatibility routes are marked deprecated in OpenAPI if retained.
- Public token scoping and identifier privacy remain unchanged.

**Testing:** Canonical contracts, compatibility tests where needed, and the live
client browser flow; code search confirms no second lifecycle implementation.

**Dependency:** Prefer issue #17 first so one policy-aware scheduling path exists.

**Non-goals:** A generic workflow engine, UI redesign, or token changes.

### 8. Introduce explicit transaction and session ownership for commands

**Problem:** Global database rebinding, helper-owned sessions, and direct ORM
transactions across `db.py`, `api_services.py`, and `client_invites.py` obscure
atomicity and force tests to mutate module globals.

**Scope:** Add a narrow session-factory/unit-of-work boundary for booking and
lesson-request commands; pass the active session into helpers used inside those
transactions; consolidate test database fixtures; move demo seeding out of
general persistence code. Use SQLAlchemy directly rather than repository wrappers.

**Acceptance criteria:**

- Commands do not open hidden secondary sessions during an active transaction.
- Rollback is owned and documented at the command boundary.
- Tests can supply isolated session factories without global rebinding.
- Startup, migrations, and demo seeding are distinct concerns.

**Testing:** Confirmation conflict/idempotency, appointment-plus-notification
rollback, two isolated app instances, and the full Postgres integrity suite.

**Non-goals:** Repository interfaces, ORM replacement, CQRS, or event sourcing.

### 9. Make frontend data mode explicit and fail closed in production

**Problem:** Both frontends silently select realistic mock adapters when an API
environment variable is absent, so a misconfigured production build can appear
healthy while disconnected from the backend.

**Scope:** Require explicit mock opt-in, default deployed builds to HTTP, validate
production configuration, preserve mock adapters for isolated UI tests, and add
one successful live write flow for the instructor app.

**Acceptance criteria:**

- Production cannot silently enter mock mode.
- Local mock mode remains a documented one-command workflow.
- CI runs both mock UI tests and one live workflow per frontend.
- The instructor live suite covers a successful mutation.

**Testing:** Adapter-selection unit tests, production-mode build validation, and
live Playwright workflows.

**Non-goals:** Removing mocks, sharing frontend frameworks, or a new config package.

### 10. Use the canonical confirmation contract in the instructor frontend

**Problem:** The UI supplies a selected slot ID, but the HTTP adapter discards it
and calls legacy `/approve`; `/confirm` already supports explicit selection.

**Scope:** Add a typed `confirmRequest(requestId, slotId?)` adapter method, call
the canonical confirmation endpoint, align mock/live response types, and retain
`/approve` only as a documented compatibility alias if external use requires it.

**Acceptance criteria:** Selected alternatives reach the backend; no adapter
discards `slot_id`; mock and HTTP adapters return one shape; idempotent replay and
conflict behavior remain intact.

**Testing:** Adapter request-body test, backend requested/alternative confirmation
tests, instructor live Playwright confirmation, and client status refresh.

**Dependency:** Coordinate with Issue 7 if compatibility routes change.

**Non-goals:** A generic state machine or request-card redesign.

### 11. Split backend modules along demonstrated capability boundaries

**Problem:** `api_services.py` mixes coach reads, settings, direct booking,
request lifecycle, legacy public behavior, mapping, and error translation;
`db.py` mixes runtime configuration, persistence, serialization, and demo data.

**Scope:** After Issues 7 and 8 establish real seams, extract coach snapshot/read
mapping, lesson-request commands, the scheduling/error adapter, legacy
compatibility endpoints, and demo seed data. Keep routes thin and contracts
unchanged.

**Acceptance criteria:** Responsibilities are separated without import cycles;
focused tests own each capability; `db.py` no longer builds demos; no one-method
service classes or pass-through repositories are introduced.

**Testing:** Full backend suite, import smoke test, contract freshness, and both
live frontend suites.

**Dependencies:** Issues 7 and 8; issue #17 should also land before moving its
scheduling seam.

**Non-goals:** A clean-architecture rewrite, DI container, universal interfaces,
or API renaming.

## Optional later-stage observability

### 12. Add a minimal low-cardinality operational metrics surface

**Problem:** Logs explain individual failures but not rates, latency regressions,
routing degradation, or notification backlog trends.

**Scope:** Add request count/latency/errors by method and route template; routing
count/duration/outcome; cache hit/miss; notification state/outcome and oldest
queued age. Document initial alert conditions.

**Acceptance criteria:** No IDs, tokens, addresses, recipients, or raw messages
in labels; operators can detect readiness failure, 5xx elevation, routing
degradation, and stalled notifications; overhead remains small.

**Testing:** Label normalization/forbidden-cardinality tests and representative
API/routing/notification exercises.

**Dependencies:** Observability Issues 1-4.

**Non-goals:** Distributed tracing, business analytics, or vendor selection.

### 13. Add privacy-safe frontend error reporting hooks

**Problem:** Frontend failures are user-visible but cannot be correlated to
backend incidents.

**Scope:** Preserve backend request IDs on typed errors, show a short support ID
for server failures, centralize unhandled error hooks, and define a disabled-by-
default transport interface with strict redaction.

**Acceptance criteria:** A backend 500 reference matches server logs; expected
validation remains ordinary UI feedback; hooks never send URL queries, tokens,
form values, session contents, headers, or DOM captures; no third-party call
occurs without explicit configuration.

**Testing:** Redaction unit tests in both frontends and browser correlation tests.

**Dependency:** Observability Issue 1.

**Non-goals:** Selecting a telemetry SaaS, session replay, analytics, or DOM capture.

## Deliberately leave as-is

- Keep the two frontends separate; they serve different actors and do not yet
  justify a shared component system.
- Keep the deterministic constraint engine independent of FastAPI and Postgres;
  issue #17 adds only explicit policy input.
- Keep `BaseRoutingProvider`, route caching, database exclusion constraints, and
  advisory locks; each addresses demonstrated external-service or concurrency risk.
- Keep the notification ledger and worker protocol; its retry/lease complexity
  is covered by real failure modes and tests.
- Do not rewrite historical migrations or create a generated TypeScript package
  solely for stylistic consistency.
