# Issue #56ak: claim-fenced Google Calendar authorization recovery

## Goal

Persist Google Calendar reauthorization state only for the exact credential generation that produced
an `invalid_grant` refresh outcome. This is a small, route-inert prerequisite for the later
refresh-to-FreeBusy orchestration service.

## Public contract

`app.google_calendar_refresh_recovery` exports only:

```python
mark_google_calendar_refresh_credential_authorization_required(
    session_factory,
    *,
    claim,
    outcome,
) -> None
```

The module also exports `GoogleCalendarRefreshRecoveryError`, a fixed-message subtype of
`GoogleCalendarOAuthError`. The function owns a fresh SQLAlchemy session and transaction. It accepts
the original sealed refresh credential claim plus the `GoogleCalendarRefreshAuthorizationRequired`
returned from refreshing that same claim. It returns `None` after either the first successful
transition or an idempotent repeat for the same still-current credential generation.

## Authority and validation

The authorization-required outcome is not an independent persistence capability. Before creating a
session, the boundary:

1. integrity-validates and snapshots the original secret-bearing claim;
2. requires the outcome's exact final class;
3. strictly validates the outcome's account, instructor, connection, and revision fields; and
4. requires every outcome field to equal the corresponding trusted claim snapshot.

Only values copied from the validated claim select or authorize database work. A mutated,
cross-tenant, malformed, or claim-mismatched outcome fails before session creation. The outcome is
not integrity-tagged, so a process-internal caller can construct an exact-class object whose four
fields match a valid claim. That object is deliberately equivalent at this boundary: it cannot
redirect persistence, and possession of the valid secret-bearing claim plus access to this internal
function is the trusted-process capability. Adding outcome provenance is deferred to the eventual
orchestration boundary rather than treating these four fields as independent authority.

## Transaction and race behavior

The boundary rejects a non-session value, a caller-active transaction, split mapper binds, and an
autocommit connection. It then:

1. row-locks the provider account;
2. verifies the account ID, instructor ID, enabled owner state, and exact positive calendar
   connection revision from the claim;
3. row-locks that account's calendar connection;
4. verifies the exact connection ID and Google provider; and
5. sets `authorization_required_at` and `updated_at` together without changing the account revision.

Account-before-connection lock order matches the existing connection lifecycle commands. A
replacement, disconnect, revision advance, owner disable, or ownership change that commits first
makes the claim stale. If recovery holds the account lock first, the lifecycle command waits and then
operates on the now-marked generation. Concurrent recovery attempts for the same generation are
idempotent. A repeated call does not change timestamps or revision.

The timestamp is generated internally in UTC and is never caller controlled. It is clamped to the
persisted `updated_at` value so wall-clock regression cannot violate connection timestamp checks.

## Failure and privacy contract

All ordinary validation, database, commit, and close failures become a fresh, cause-free
`GoogleCalendarRefreshRecoveryError`. Cancellation, `KeyboardInterrupt`, `SystemExit`, and
`GeneratorExit` remain controls but are re-raised fresh and cause-free. Primary transaction and
rollback/session cleanup outcomes are captured separately: any non-cancellation control wins, then
cancellation, then ordinary failure. Nested exception groups are traversed once per identity with a
4,096-node bound; overflow fails closed as cancellation. The boundary does not log.
Refresh tokens, tenant identifiers, connection identifiers, outcome values, SQL/provider detail,
and dependency exceptions must not appear in public messages, representations, traceback locals, or
logs.

## Files

- `app/google_calendar_refresh_recovery.py`
- `tests/test_google_calendar_credential_claim.py`
- `specs/issue-56ak-google-calendar-claim-fenced-recovery.md`

## Non-goals

- Refresh-token exchange, FreeBusy I/O, retries, or orchestration.
- Treating a FreeBusy 401 as a persisted recovery signal.
- Connection replacement, disconnect, revision advancement, OAuth state cancellation, or schema
  changes.
- Routes, API contracts, scheduling behavior, background work, or frontend changes.
