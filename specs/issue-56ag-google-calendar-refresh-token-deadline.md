# Issue #56ag: hard-deadline Google Calendar refresh exchange

## Goal

Connect the pure, claim-bound refresh-token protocol to the existing environment-free async HTTPX
transport. Perform one exact refresh request under one absolute five-second deadline and return the
existing sealed success or authorization-recovery outcome.

This slice performs no database work, credential mutation, route handling, FreeBusy request,
scheduling operation, environment load, retry, redirect, or live Google call.

## Public contract

`exchange_google_calendar_refresh_token_with_deadline()` is an async, keyword-only service that
accepts one exact `GoogleCalendarTokenExchangeConfiguration` and one exact sealed
`GoogleCalendarRefreshCredentialClaim`. It accepts no caller-controlled client, transport, timeout,
deadline, endpoint, or response policy.

The service returns the existing `GoogleCalendarRefreshTokenExchangeOutcome` and exports no new
result or error type.

## Validation and claim continuity

The service runs the existing refresh preparation boundary before constructing a deadline scope or
network object. It retains all three preparation values: the exact ordered form, the original sealed
claim, and the copied integrity-checked pre-network snapshot.

Completion runs inside the same absolute deadline. It revalidates the original claim and compares
every post-network field, including the refresh token, with the pre-network snapshot before
returning either outcome. Mutation or coherent replacement during the network gap fails closed.

## Request and network policy

The request is one form POST to `https://oauth2.googleapis.com/token` containing exactly, in order:

1. `client_id`;
2. `client_secret`;
3. `grant_type=refresh_token`; and
4. `refresh_token`.

The shared HTTPX boundary recognizes two exact, noninterchangeable schemas: the existing
authorization-code form and this refresh form. Missing, extra, duplicate, reordered, hybrid,
subclassed, non-ASCII, control-character, or oversized fields fail before network construction.
Response-body policy is derived only from the validated schema and is never caller supplied.

The existing network policy remains authoritative: verified certifi trust, TLS key logging disabled,
environment and proxies ignored, HTTP/1.1 only, redirects disabled, zero retries, one connection,
no keep-alive reuse, identity encoding, no cookies, and no authorization header.

## Status-aware bounded responses

Authorization-code requests retain their current policy: only an HTTP 200 body may be read.

Refresh requests may read HTTP 200 and 400 bodies. Both use the same identity-encoding,
unambiguous content-length, streamed 64 KiB limit, response cleanup, and client cleanup rules.
Every other status is closed without reading its body and is represented with no content type and
an empty body. Redirects, 408, 429, and 5xx responses are never followed or retried.

The HTTPX layer performs no OAuth parsing. The existing pure completion boundary remains
authoritative for strict success and `invalid_grant` classification.

## Hard overall deadline

After validation, the service calculates one absolute deadline from the running loop's monotonic
clock. That same deadline covers client and TLS setup, connection, request writing, response
headers, body streaming, response and client cleanup, strict response completion, and final result
acceptance.

Per-phase HTTPX timeouts are clamped to the remaining overall time. A result completed at or after
the exact deadline is rejected even if a dependency suppresses cancellation.

## Failure, cancellation, and confidentiality

Invalid input, provider failure, timeout, malformed response, cleanup failure, and internal deadline
expiration become one fresh, fixed, cause-free `GoogleCalendarTokenExchangeError`. Exact valid
HTTP 400 `invalid_grant` returns the existing claim-bound recovery outcome.

External cancellation propagates as a fresh, cause-free `asyncio.CancelledError` after cleanup.
Cancellation during response or client cleanup takes precedence over an earlier ordinary failure.

The client secret, refresh token, access token, encoded form, response body, provider diagnostics,
tenant identifiers, integrity tag, dependency exception text, and ambient exception text never
appear in public diagnostics, traceback locals, exception chains, representations, or logs. The
modules perform no logging.

The supported diagnostic boundary includes ordinary dependency failures, control exceptions, the
public function's first emitted line, and one nested cleanup/classification interruption. A
deliberately reentrant `sys.settrace` callback that raises again while the already-sanitizing public
fallback handler itself is being entered is outside the cause-free diagnostic guarantee. Before
that fallback can be reached, public credential arguments are rebound to `None` and
credential-bearing references are cleared from inner exception frames; regression tests require
this out-of-scope interruption to remain credential- and tenant-data-free.

## Verification

Fake HTTPX transports and streams cover:

- the public coroutine signature and exact exports;
- exact refresh encoding, one request, one client, and complete claim binding;
- bounded HTTP 400 `invalid_grant` recovery;
- authorization-code HTTP 400 regression and unread noneligible statuses;
- malformed, compressed, oversized, truncated, and duplicate-length 200/400 responses;
- invalid input before deadline and network construction;
- claim mutation across the await;
- stalled request, response stream, response close, and client close cancellation;
- suppressed cancellation and exact-deadline result rejection;
- external cancellation and secret-free diagnostics; and
- concurrent operation isolation.

No test contacts Google or PostgreSQL.

## Deferred work

1. Query and normalize opaque Google FreeBusy intervals using the bound access-token result.
2. Revalidate persisted credential freshness before accepting busy periods or recovery state.
3. Integrate external blockers fail-closed into availability search and booking revalidation.
4. Add browser OAuth routes and settings recovery actions.

## Non-goals

- Database sessions, connection-state mutation, key rotation, or refresh-token rotation persistence.
- FreeBusy parsing, time-zone normalization, availability, booking, agenda, or frontend behavior.
- Routes, cookies, authentication middleware, environment configuration, metrics, or audit events.
- Multiple calendars, write access, non-Google providers, DPoP, retries, or live network tests.
