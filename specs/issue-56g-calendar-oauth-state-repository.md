# Issue #56g: transactional calendar OAuth-state repository

## Goal

Add the smallest internal runtime boundary for issuing and consuming the digest-only Google OAuth
state introduced by issue #56f. This slice authenticates a future cross-site callback without
depending on the SameSite instructor cookie, but adds no authorization URL, callback route, Google
request, credential exchange, configuration, OpenAPI, or frontend behavior.

## Public contract

`app.calendar_oauth_states` exposes two commands. Both require a caller-owned explicit SQLAlchemy
transaction and never commit, roll back, open another session, read deployment configuration, or
log values.

- `issue_google_calendar_oauth_state` accepts trusted, server-derived provider account and session
  identifiers. It returns an `IssuedGoogleCalendarOAuthState` containing the one raw URL-safe bearer
  value needed by the later authorization URL and its UTC expiration.
- `consume_google_calendar_oauth_state` accepts only the returned raw bearer value. It returns a
  `ConsumedGoogleCalendarOAuthState` containing the bound account and session identifiers for the
  later callback service.

The raw value and both internal identifiers are excluded from object representations. Exported
errors use fixed, identifier-free messages. Malformed, unknown, expired, revoked, disabled,
version-stale, and replayed callback values all raise the same `CalendarOAuthStateInvalid` error.
Issuance against any missing, mismatched, revoked, expired, disabled, or version-stale owner raises
one `CalendarOAuthStateSessionInvalid` error.

## Issuance

Each candidate state is generated with `secrets.token_urlsafe(32)`: 32 cryptographically random
bytes encoded as exactly 43 URL-safe characters. SHA-256 of the ASCII value is the only bearer
representation persisted. The raw value exists only in the returned issuance object.

Issuance locks the provider account and then its exact `(account_id, session_id)` session row. This
matches the account-then-session order used by administrative revocation and serializes concurrent
issuance, sign-out, password reset, disablement, and replacement. After both locks are acquired, the
command requires:

- an enabled account;
- an unrevoked session owned by that account;
- a session version equal to the account version;
- session creation no later than the issuance instant; and
- session expiration strictly after the issuance instant.

Default time is captured after the locks are acquired. An explicit test/service time must be aware
and is canonicalized to UTC. Expiration is the earlier of ten minutes after issuance or the exact
session expiration, so state can never outlive the authenticating session.

The database permits one pending row per signed-in session. After locking the owner pair, the command
deletes the old row and inserts the replacement inside one nested savepoint, invalidating an older
browser tab only when the caller transaction commits. A global digest lookup first prevents
deliberate reuse of an existing bearer. The authoritative primary-key constraint closes a
cross-session race. The savepoint retries with fresh entropy up to three times on a digest collision;
exhaustion raises a sanitized persistence error and restores the prior pending row.

## Consumption and replay safety

Consumption validates the exact 43-character bearer shape before hashing it, then performs a
non-locking digest lookup only to discover the persisted owner pair. It locks and refreshes that
account and exact session in the same account-then-session order used by issuance and revocation.
The lookup is never trusted: after owner revalidation, one conditional PostgreSQL
`DELETE ... RETURNING` statement repeats every state predicate and deletes only when all of these are
true at the consumption instant:

- the digest and fixed `google` provider match;
- state creation is no later than the consumption instant;
- state expiration and session expiration are both strictly later than that instant;
- the state account/session pair still matches the persisted session owner;
- the session is unrevoked;
- the account is enabled; and
- the session version still equals the account version.

The mutation returns the persisted owner pair rather than trusting callback input. A committed first
consume removes the only matching row, so every replay receives the same generic invalid-state
result. Concurrent consumes serialize on that row and at most one commits successfully. Caller
rollback restores the row, making transaction ownership explicit and testable. Invalid or expired
rows are left for a later issuance replacement or cleanup slice; no separate mutation widens the
consume race.

Concurrent revocation and consumption serialize on the account/session locks: a revocation that wins
first rejects consumption; a consume that wins first commits before a later revocation can proceed.
The repository never holds a transaction open across Google network traffic. The future callback
service must commit consumption before exchanging the authorization code, then revalidate the
returned account/session before storing credentials. Later failure or revocation requires a fresh
authorization attempt.

## Privacy and failure boundary

- No raw state, digest, account ID, session ID, authorization code, token, redirect target, scope,
  provider response, or database parameters enter logs or exceptions.
- SQLAlchemy engines hide bound parameters. Database exceptions are translated outside their
  handlers so the sanitized persistence error has no recoverable cause or context.
- Issuance and consumption result representations omit bearer and tenant values.
- The module exports no ORM model, SQL statement, digest helper, or entropy source.

## Verification

PostgreSQL tests cover:

- 32-byte entropy, exact URL-safe shape, digest-only storage, UTC timestamps, and the session-capped
  ten-minute lifetime;
- enabled/active ownership and uniform rejection of every inactive or cross-account issue case;
- one pending state per session, old-state invalidation, digest collision retry/exhaustion, and
  caller rollback preserving the previous state;
- successful atomic consumption, malformed/unknown/expired/revoked/disabled/version-stale/replayed
  uniformity, and exact expiry boundaries;
- two-session tenant isolation, concurrent issue serialization, and one-winner concurrent consume;
- caller-owned transaction enforcement and rollback semantics; and
- safe exported surface, result representations, errors, tracebacks, and logs.

## Deferred slices

1. Google OAuth client configuration and fixed authorization URL construction.
2. Public callback handling, authorization-code exchange, and encrypted connection replacement.
3. Narrow credential use, token refresh, and Google FreeBusy transport.
4. Settings API/UI and busy-period integration with agenda and availability.

## Non-goals

- HTTP routes, cookies, redirects, CSRF middleware changes, OpenAPI, or frontend behavior.
- OAuth client IDs/secrets, authorization codes, PKCE, Google networking, or refresh credentials.
- Connection persistence changes, credential decryption, FreeBusy reads, or scheduling changes.
- Background cleanup, rate limiting, audit logging, multiple providers, or calendar write access.
