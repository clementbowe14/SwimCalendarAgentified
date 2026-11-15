# Issue #56y: calendar connection revision lifecycle

## Goal

Make every successful Google Calendar connection replacement and disconnect advance the durable
provider-account revision fence added in issue #56x. A later callback finalizer can compare its
claimed revision with the current account revision and reject work made stale by either lifecycle
command during the Google network gap.

This slice remains internal and route-inert. It preserves every public function signature and
credential-free status shape, and it adds no callback, network request, cookie, API, configuration,
startup behavior, or frontend flow.

## Command semantics

The existing provider-account row remains the first and authoritative command lock.

- `replace_google_calendar_connection()` advances the revision exactly once for every successful
  first connection or replacement.
- `disconnect_google_calendar()` advances the revision exactly once for every successful call,
  including a call made while already disconnected.
- `mark_google_calendar_authorization_required()` does not advance the revision. It records the
  health of the current credential and must not invalidate legitimate reauthorization work.
- `get_google_calendar_connection_status()` remains read-only and never advances the revision.
  Its unlocked queries do not autoflush or overwrite pending account changes in the caller's
  session.

Successful, non-exhausted disconnect therefore remains externally idempotent: it returns the same
disconnected status and leaves no credential row. It is intentionally no longer storage-idempotent
because every user disconnect intent must invalidate older claimed work, including an in-flight
initial connection for an account that has no credential row yet.

The revision remains private persistence state. It is not added to status values, API schemas,
errors, logs, metrics, or user-visible responses.

## Atomicity and ordering

Mutation commands lock and refresh the provider account before reading its revision, then refresh
the connection row while holding that lock. They compute the next revision while holding the lock
and persist the connection mutation plus revision in the caller-owned transaction. Unlocked status
reads intentionally do not refresh identity-mapped rows, so they cannot overwrite pending caller
changes.

Mutation commands reject a session that already contains pending `ProviderAccountDB` or
`ProviderCalendarConnectionDB` changes. Account and connection discovery queries run with ORM
autoflush disabled, so unrelated pending caller writes cannot acquire database locks before the
provider-account `FOR UPDATE`; the later explicit command flush occurs only after that lock is held.
This preserves the account-first lock order and prevents callers from bypassing the fence through an
unflushed direct lifecycle mutation.

Replacement performs timestamp validation, refresh-token encryption, and nonce-collision preflight
before assigning the next revision. This ordering matters because the collision query can autoflush
pending ORM changes. Validation, encryption, or preflight failure therefore leaves both the prior
connection and revision unchanged. The next revision is assigned only once the opaque credential
envelope is ready, then it commits or rolls back atomically with deletion of the prior row and
insertion of the replacement.

Disconnect assigns the next revision unconditionally, deletes the connection when present, and
always flushes. Concurrent commands serialize on the account lock, so no successful increment is
lost. Caller rollback and persistence failure restore both the connection and revision together.

## Exhaustion

The PostgreSQL `BIGINT` ceiling is `9,223,372,036,854,775,807`. The final available increment from
one less than that ceiling succeeds. At the ceiling, replacement and disconnect fail closed with the
existing fixed `CalendarConnectionPersistenceError` before mutating connection state. The counter
never wraps, saturates while reporting success, or reuses an earlier value.

An exhausted already-disconnected account also rejects disconnect. Reporting success without a new
generation would fail to invalidate stale claimed work. Status reads and authorization-recovery
marking remain available at exhaustion.

## Deferred safety boundary

This revision fence invalidates a callback that was claimed before a later replacement or
disconnect. It cannot by itself reject an authorization state issued before a disconnect but first
claimed afterward, because the existing OAuth-state row does not store its issuance revision.

Before callback routes can activate, a later inert slice must either cancel pending browser-bound
OAuth state as part of disconnect or bind state issuance and claim to an expected revision. Route
activation remains prohibited until that pre-claim disconnect case, callback claim, post-network
session revalidation/finalization, and transaction-free network gap are all implemented and tested.

A future finalizer must advance the revision exactly once in total when it installs credentials.
If it delegates to the replacement command, it must not add a second increment of its own.

## Verification

PostgreSQL tests verify first and repeated replacement increments, repeated disconnect increments
without a row, revision-neutral reads and recovery marks, failure and rollback preservation, final
available increment and fail-closed exhaustion, account isolation, and exact revisions after
concurrent replacements and disconnects. Existing credential encryption, nonce collision,
connection replacement, stale recovery, transaction ownership, and sanitized-error tests remain
authoritative.

## Non-goals

- OAuth callback claim, pending-state cancellation, or issuance-revision binding.
- Post-network session revalidation, credential finalization, or callback orchestration.
- Authorization start/callback routes, browser cookies, redirects, rollout gates, OpenAPI, or UI.
- Google token refresh, FreeBusy ingestion, calendar writes, or additional providers.
- New schema, triggers, API fields, audit events, or direct SQL lifecycle mutations.
