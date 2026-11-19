# Issue #56ab: Google Calendar OAuth callback finalizer

## Goal

Install one refresh credential only after proving that the post-network token result still belongs
to the committed callback claim, exact initiating session, and current connection revision. The
finalizer opens a fresh transaction after Google I/O has completed, revalidates all mutable owner
state under locks, and delegates the encrypted replacement to the existing connection repository.

This slice is internal and route-inert. It adds no callback route, cookie, redirect, authorization
code parsing, Google request, environment loading, rollout mode, OpenAPI contract, startup behavior,
or frontend flow.

## Public contract

`finalize_google_calendar_oauth_callback()` accepts:

- a SQLAlchemy session factory owned by the service;
- an exact sealed `GoogleCalendarOAuthCallbackClaim` returned by the committed claim boundary;
- an exact sealed `GoogleCalendarTokenExchangeResult` returned after the network gap; and
- an exact `CalendarCredentialCipher`.

It returns the existing credential-free `CalendarConnectionStatus` only after the transaction has
committed and the owned session has closed. It accepts no caller-supplied clock, database session,
connection revision, account, session, token, challenge, endpoint, transport, or retry policy.

Every failure becomes one newly constructed, fixed, cause-free
`GoogleCalendarOAuthCallbackFinalizationError`. Missing, stale, inactive, expired, crossed,
exhausted, encryption, persistence, commit, and close failures disclose no distinction or secret.

## Claim and token-result binding

Before creating a database session, the service revalidates every sealed handoff field. It derives
the PKCE S256 challenge from the claim's exact canonical verifier and constant-time compares that
value with the token result's challenge. It independently recomputes S256 before trusting the PKCE
helper output, matching the token-exchange defense against raw-verifier, constant, malformed, or
exceptional collaborator output.

A crossed or malformed result therefore fails without SQL. The finalizer never accepts a refresh
token separately from its exchange result, and the raw verifier is never copied into persistence.

## Session and transaction boundary

The factory must return a service-owned idle SQLAlchemy `Session` with no active transaction, backed
directly by one transaction-capable `Engine`. Provider account, provider session, calendar
connection, OAuth state, and PKCE verifier mappers must resolve to that same engine. Autocommit
connections, externally transactional connections, and split binds fail before owner lookup. The
owned session is always closed. Previously used but idle sessions remain safe because every
owner row is refreshed under `FOR UPDATE`; connection replacement and pending-state cancellation
then run under the locked account invariant. Callers are nevertheless required to supply a
service-owned session that the finalizer may close.

One service-owned root transaction performs the complete finalization:

1. lock and refresh the claimed provider-account row;
2. lock and refresh the exact `(account_id, session_id)` initiating session;
3. sample the private trusted UTC clock only after both locks are held;
4. require an enabled account and an unrevoked, current-version session whose lifetime contains the
   sampled time;
5. require the locked account revision to equal the claim's expected revision and leave room for
   one final increment;
6. call `replace_google_calendar_connection()` exactly once while retaining both locks; and
7. commit the encrypted replacement, pending-state cancellation, and revision increment together.

The finalizer never increments the revision itself. The existing replacement command is the sole
authority that advances the locked revision from the claimed value to the next value. No network
work occurs while the transaction or any database lock is active.

## Concurrency and rollback contract

- A newer callback claim, direct replacement, or disconnect that commits first advances the shared
  revision and makes the older finalizer stale.
- A finalizer that commits first installs its credential; a later lifecycle command remains
  authoritative and advances the revision again.
- Session revocation or account security change that commits first makes finalization fail. A
  finalizer holding the exact session lock linearizes before a waiting revocation.
- Two attempts using one claim serialize on the account lock; exactly one can install a credential.
- Different provider accounts do not share locks or credentials.
- Expiry is evaluated from time sampled after lock acquisition, so lock waiting cannot use a stale
  pre-wait timestamp and callers cannot backdate an expired session.

Stale or inactive validation mutates nothing. Encryption, nonce collision, SQL, flush, or commit
failure rolls back the connection replacement, OAuth-state cancellation, and revision increment
together. A post-commit close failure returns no success status; callers must not retry the used
authorization code, revive state, or compensate. Recovery requires a fresh authorization attempt.

## Confidentiality

The refresh token, verifier, PKCE challenge, claim identifiers, credential envelope, SQL details,
dependency exception text, and stored row values never appear in the exported message,
representation, cause/context chain, public traceback locals, or logs. The module performs no
logging and returns no ORM object or credential material.

## Verification

Pure tests cover the exact public surface, no public time override, handoff validation before session
creation, independent PKCE binding, exact cipher type, idle-session and single-bind enforcement,
transaction/commit/close ordering, trusted post-lock time, delegated single replacement, fixed
secret-free failures, and runtime-inert imports.

PostgreSQL tests cover successful encrypted replacement and one revision increment, exact session
activity boundaries, stale and exhausted revisions, rollback preservation, pending-state
cancellation, same-claim serialization, both orderings against newer claims and lifecycle commands,
session-revocation ordering, and different-tenant nonblocking isolation. No test contacts Google.

Existing callback-claim, token-exchange, deadline, connection lifecycle, credential encryption,
OAuth-state, session-authentication, and migration suites remain authoritative.

## Deferred work

1. Compose claim, hard-deadline exchange, and finalization in callback orchestration.
2. Add the host-only browser-binding cookie lifecycle, start/callback routes, redirects, and
   coordinated `disabled|enabled|drain` rollout mode.
3. Refresh credentials and query normalized opaque Google FreeBusy intervals.
4. Integrate fail-closed external busy periods and expose settings and recovery behavior.

Callback routes remain prohibited until transaction-free network orchestration and coordinated
route lifecycle are implemented and tested.

## Non-goals

- Models, migrations, columns, constraints, triggers, or cleanup jobs.
- Authorization-code exchange, Google networking, token refresh, revocation, or FreeBusy requests.
- Routes, cookies, redirects, authentication middleware, rollout gates, OpenAPI, or frontend UI.
- Calendar writes, event details, multiple calendars, other providers, metrics, or audit events.
