# Issue #56v: internal Google Calendar OAuth start service

## Goal

Compose the existing browser-bound PKCE state issuer and fixed Google authorization-request
builder behind one commit-before-redirect service boundary. This slice remains internal and
runtime-inert. It adds no route, browser cookie, rollout gate, callback, Google network request,
token exchange, credential persistence, OpenAPI contract, or frontend behavior.

## Public contract

`prepare_google_calendar_authorization_start()` accepts a caller-supplied SQLAlchemy session
factory plus trusted account and provider-session identifiers, an exact validated
`GoogleCalendarOAuthConfiguration`, an exact `CalendarOAuthPKCECipher`, and an optional aware
operation time. Configuration and key material must be loaded and validated by the future caller
before invoking the service, so no environment read occurs while database locks are held.

The service validates its dependencies before opening a database session. Account and session
identifiers must be exact, bounded, nonblank visible-ASCII strings without normalization. The
session factory must return a fresh, engine-bound SQLAlchemy `Session` with no existing transaction;
the service owns and closes that session. A session bound to an externally managed `Connection` is
rejected because exiting its inner transaction cannot prove the outer connection transaction was
committed. Every model used during issuance must resolve to that same engine, so entity-specific
binds cannot route part of the transaction through another engine or externally managed connection.

On success, the service returns a final, immutable, builder-only, redacted, non-serializable
`GoogleCalendarAuthorizationStart` containing exactly:

- the existing redacted `GoogleCalendarAuthorizationRequest`;
- the raw independent browser-binding value for a later host cookie; and
- the exact UTC state expiry for that cookie.

It does not expose account or session identifiers, state or binding digests, plaintext PKCE
verifier, encrypted envelope, keys, database objects, provider token configuration, or HTTP
transport.

## Transaction and ordering contract

The service performs these steps in order:

1. Validate and snapshot trusted inputs and injected dependencies.
2. Create one fresh database session and begin one explicit root transaction.
3. Call `issue_google_calendar_oauth_state_with_pkce_and_browser_binding()` with the trusted owner,
   cipher, and optional operation time.
4. Validate and snapshot the issued state, challenge, binding, and expiry while still inside the
   transaction.
5. Exit the transaction successfully, committing the complete state, browser-binding digest, and
   encrypted verifier.
6. Close the database session, releasing the connection and every lock.
7. Only then call `build_google_calendar_authorization_request()`.
8. Return the sealed start handoff.

No authorization request is constructed or returned after issuance rollback, commit failure,
uncertain commit, or session-close failure. The service performs no network operation.

If authorization construction unexpectedly fails after a successful commit, the committed state
is left to expire within its existing ten-minute lifetime or to be atomically replaced by a later
start. The service must not issue a compensating delete that could race with a newer flow.

## Validation and confidentiality

The service accepts only exact canonical 32-byte unpadded Base64URL state, challenge, and browser
binding values from the issuer, requires distinct state and binding values, and requires an exact
UTC expiry. This defensive validation occurs before commit; the authorization builder revalidates
the public state and challenge after commit.

All caller, dependency, session, transaction, issuance, validation, close, and authorization
builder failures become one newly constructed, fixed, cause-free
`GoogleCalendarAuthorizationStartError`. The public wrapper deletes its session factory, owner
identifiers, configuration, cipher, and operation time before invoking a dedicated error raiser.
Secret-bearing work remains in private helper frames absent from the exported traceback.

The authorization URL, raw state, browser binding, account/session identifiers, PKCE material,
root keys, persistence details, and dependency exception text never appear in public messages,
representations, cause/context chains, traceback-frame locals, automatic serialization, or logs.
The module performs no logging.

## Runtime lifecycle

Importing the module, constructing no values, `create_app()`, and application startup do not load
OAuth configuration, key rings, create a database session, issue state, or construct HTTP clients.
The future route must dereference the currently configured `db.SessionLocal` at call time and pass
validated call-time configuration and cipher values into this service.

This service must remain unwired until the callback path, cookie lifecycle, disabled-by-default
`disabled|enabled|drain` rollout mode, overall token-exchange deadline, and post-network initiating
session revalidation are ready for coordinated activation.

## Verification

Pure orchestration tests cover the exact public surface, dependency validation before session
creation, rejection of preexisting session, default external-connection transactions, and
entity-specific external-connection transactions, explicit root transaction, commit and session
close before authorization construction, rollback and commit failure, the default runtime clock
path, builder failure after commit without compensating cleanup, resource closure, canonical
issuance validation, result immutability and serialization rejection, fixed secret-free diagnostics,
ambient exception context, and runtime-inert imports/startup.

PostgreSQL integration tests prove that authorization construction sees the committed state from a
second session, only digests and encrypted verifier material are stored, a repeated start replaces
the prior pending flow, and an inactive initiating session returns no handoff.

Existing state, PKCE, configuration, authorization, token-exchange, credential, route, and contract
suites remain unchanged.

## Deferred slices

1. Add a cancellable hard overall deadline around the concrete token exchange.
2. Add callback orchestration that consumes and commits state before network access, then
   revalidates the initiating session and replaces encrypted credentials in a fresh transaction.
3. Add the host-only browser cookie, start and callback routes, and coordinated
   `disabled|enabled|drain` rollout mode.
4. Refresh credentials and ingest normalized opaque FreeBusy intervals.
5. Integrate fail-closed external busy periods and expose settings/recovery behavior.

## Non-goals

- Environment loading, application settings, feature gates, startup/readiness activation, or
  global caches.
- FastAPI routes, cookies, redirects, callback parsing, authentication middleware, OpenAPI, or UI.
- Authorization-code exchange, HTTP clients, deadlines, refresh, revocation, or FreeBusy requests.
- Credential encryption/replacement, connection status, disconnect, schema, or migrations.
- Calendar event details, writes, multiple calendars, other providers, metrics, or audit events.
