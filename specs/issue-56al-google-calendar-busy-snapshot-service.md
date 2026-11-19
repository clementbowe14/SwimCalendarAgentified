# Issue 56al: Google Calendar busy-snapshot service

## Goal

Compose the merged credential-claim, refresh, FreeBusy, recovery, and
revalidation boundaries into one route-inert async operation that returns only
provider-neutral busy periods.

This slice is intentionally not a route, scheduler adapter, or rollout change.

## Public contract

`query_google_calendar_busy_snapshot(...)` accepts:

- one service-owned SQLAlchemy session factory;
- an authenticated instructor identifier;
- a bounded timezone-aware query window;
- the validated Google token-exchange configuration; and
- the calendar credential cipher.

It returns `CalendarBusySnapshot(status="disconnected", periods=())` when no
Google connection exists, or `CalendarBusySnapshot(status="verified", ...)`
after a complete verified query. It never returns claims, credentials, access
tokens, provider payloads, or provider event metadata.

## Required sequence

The connected path is exactly:

1. Validate and copy static inputs before any side effect.
2. Claim the current refresh credential in an owned, joined database worker.
3. Exchange that exact claim under the existing refresh deadline.
4. Revalidate the claim in an owned, joined database worker before using the
   new access token.
5. Query the fixed primary-calendar FreeBusy endpoint under its existing
   deadline.
6. Validate and copy the complete result fence, canonical window, and opaque
   periods into a credential-free snapshot candidate.
7. Revalidate the original claim again in an owned, joined database worker.
8. Clear secret-bearing references and return the already-built candidate.

No database transaction may remain open during either provider request. The
second revalidation is only a freshness point; it is not a durable lease, and
future booking integration must query again before committing a booking.

## State and failure behavior

- `disconnected` returns a distinct empty disconnected snapshot without a
  provider request.
- An existing authorization-required state raises the canonical
  authorization-required availability error without a provider request.
- Refresh `invalid_grant` invokes the claim-fenced recovery finalizer exactly
  once. Authorization-required is reported only after that exact generation is
  marked successfully.
- A stale or failed recovery is provider-unavailable and must not poison a
  replacement credential.
- A FreeBusy 401 raises authorization-required but never persists recovery.
- Refresh failures whose lower boundary intentionally erases their cause map to
  provider-unavailable; this layer must not guess that they were timeouts.
- FreeBusy timeout, provider-unavailable, invalid-response, and
  authorization-required reasons retain their canonical domain meaning.
- Storage, decryption, revalidation, and configuration failures are
  provider-unavailable.
- Malformed collaborator values, crossed tenant/generation fences, or an
  invalid public query window are invalid-response.
- No uncertain path may return verified-empty periods.

## Concurrency and cancellation

Every synchronous database phase runs in an owned worker. If the calling task
is cancelled after a phase starts, the worker must settle and close before a
direct cancellation is re-emitted. No child task or transaction may be
abandoned.

Cancellation is checked before each new side effect. The exact direct external
cancellation object and arguments are preserved so caller-owned deadline scopes
can classify their own timeout; synthesized child or grouped controls are fresh
and cause-free. Identity and arguments are retained only for an exact
`CancelledError` observed while the parent task is actively cancelling; a
collaborator-raised or subclassed cancellation is freshly sanitized. External
cancellation wins over ordinary worker failures, while nested non-cancellation
controls retain precedence. No automatic retry follows a commit-uncertain
recovery or any provider failure.

Replacement, disconnect, disablement, ownership reassignment, or revision
change during either network gap must prevent provider periods from escaping.
The persisted claim remains the sole database authority.

## Confidentiality

Claims, refresh tokens, access tokens, client secrets, cipher material,
provider bodies, and tenant fences must not enter returned values, logs,
metrics, task names, serialization, or public exception text/chains. Public
failures and reconstructed controls are fresh and cause-free. A direct external
cancellation keeps its identity and arguments after its traceback, cause, and
context are cleared.

The access result is used once for FreeBusy and cleared. The refresh claim is
cleared after its final revalidation or recovery branch.

## Files and non-goals

This PR is limited to:

- `app/google_calendar_freebusy_service.py`;
- `tests/test_google_calendar_freebusy_service.py`; and
- this specification.

It does not add routes, OpenAPI, environment loading, startup wiring, schema,
migrations, caching, retries, telemetry, a synchronous `CalendarBusySource`,
availability or booking integration, UI, multiple calendars, event details,
calendar writes, or refresh-token rotation. The refresh and FreeBusy phases
retain their independent five-second deadlines.
