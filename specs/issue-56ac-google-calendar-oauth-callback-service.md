# Issue #56ac: Google Calendar OAuth callback service

## Goal

Compose the committed callback claim, bounded Google token exchange, and transactional credential
finalizer into one route-inert async service. A successful invocation has exactly three phases:

1. consume browser-bound state and commit its revision fence;
2. exchange the one-time authorization code while no database transaction is open; and
3. revalidate the owner in a new transaction and install the encrypted refresh credential.

This slice composes existing boundaries without changing their validation, lock, encryption,
deadline, or persistence semantics. It does not complete issue #56 and exposes no HTTP route.

## Public contract

`complete_google_calendar_oauth_callback()` accepts:

- a service-owned SQLAlchemy session factory;
- the raw OAuth state and independent browser-binding value;
- the one-time authorization code;
- an exact sealed `GoogleCalendarTokenExchangeConfiguration`;
- an exact `CalendarOAuthPKCECipher`; and
- an exact `CalendarCredentialCipher`.

It returns only the existing credential-free `CalendarConnectionStatus`. It accepts no clock,
transport, endpoint, timeout, retry policy, database session, transaction, claim, token result,
account identifier, or session identifier.

The module exports only the service and
`GoogleCalendarOAuthCallbackCompletionError`. Every ordinary failure has the same fresh,
cause-free message: `Google Calendar authorization could not be completed.` The service never
discloses whether claiming, Google exchange, or finalization failed.

## Ordering and transaction boundary

Static collaborators are validated before side effects. Callback-bearer validation remains owned
by the claim boundary, while authorization-code validation remains owned by the exchange boundary.
A callback that proves possession of valid state and browser binding is intentionally one-shot even
if its authorization code is malformed.

The service then performs exactly:

1. one joined worker call to `claim_google_calendar_oauth_callback()`;
2. one awaited `exchange_google_calendar_authorization_code_with_deadline()` using the exact
   verifier from the committed sealed claim; and
3. one joined worker call to `finalize_google_calendar_oauth_callback()` using the unchanged claim
   and exact sealed token result.

Each synchronous database phase creates and closes its own service-owned session inside its worker.
The async orchestration layer never owns a session, connection, transaction, or lock. The claim
worker must settle and close before network work begins; finalization is not scheduled until the
network operation and cleanup have returned. Physical connection-pool reuse is permitted.

Each database call runs in a child task whose worker thread uses `abandon_on_cancel=False`. The
parent shields and joins that child after cancellation, consumes its outcome, and only then lets a
fresh cancellation escape. A raw `asyncio.Task.cancel()` therefore cannot detach a transaction.
The service proceeds directly from a successful exchange into the finalization worker without an
added cancellation checkpoint.

## Cancellation, replay, and uncertainty

External `asyncio.CancelledError` remains cancellation. After secret-bearing private frames unwind,
the service raises a fresh, cause-free cancellation. Internal deadline expiry is an ordinary fixed
completion failure.

- Cancellation or failure before a successful claim mutates nothing.
- Cancellation or failure after claim commit leaves state and verifier consumed and the revision
  advanced. Recovery requires a fresh authorization start.
- Cancellation during either database worker waits for that worker to settle and close before it
  escapes.
- If finalization committed before cancellation or close uncertainty, the connection is
  authoritative even though no success status was returned. Callers must query status and must not
  retry the used authorization code.
- Replay fails during claiming and performs no Google request.

The service performs no retry, state revival, revision decrement, compensating delete, token cache,
detached task, or background transaction. It never extracts a standalone refresh token from the
sealed exchange result.

## Confidentiality

Raw state, browser binding, authorization code, verifier, challenge, client secret, provider
response, access token, refresh token, account and session identifiers, encryption keys, SQL, and
dependency details never appear in public messages, cause/context chains, formatted tracebacks,
public-frame locals, or logs. The module performs no logging.

## Verification

Focused pure tests cover the exact async public surface, static dependency validation before side
effects, argument identity and call ordering, worker-thread execution, malformed collaborator
results, stage failure short-circuiting, joined-worker cancellation, fixed secret-free failures,
and runtime-inert import/startup behavior.

Two PostgreSQL composition tests remain intentionally narrow:

- success proves the claimed state is committed and its session closed during the fake network
  gap, then verifies connection installation and revision progression from zero to one to two; and
- exchange failure proves the committed claim remains consumed without credential installation or
  compensation.

The existing claim, deadline, HTTP transport, finalizer, revision, encryption, and concurrency
suites remain authoritative for their exhaustive matrices.

## Deferred work

1. Add the host-only browser-binding cookie lifecycle, start/callback routes, redirects, Google
   error handling, and coordinated `disabled|enabled|drain` rollout mode.
2. Refresh credentials and query normalized opaque Google FreeBusy intervals.
3. Integrate fail-closed external busy periods into availability and agenda reads.
4. Expose connection and recovery behavior in provider settings.

## Non-goals

- Routes, query parsing, cookies, redirects, authentication middleware, OpenAPI, or frontend UI.
- Environment loading, startup wiring, rollout gates, metrics, or audit events.
- Models, migrations, columns, repositories, or changes to existing primitive contracts.
- Retries, backoff, detached tasks, live Google tests, refresh, revocation, or FreeBusy requests.
- Calendar writes, event details, multiple calendars, or non-Google providers.
