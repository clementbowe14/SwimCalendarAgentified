# Issue #56aj: hard-deadline Google FreeBusy query

## Goal

Expose one public asynchronous boundary that composes the pure claim-bound FreeBusy protocol with
the bounded HTTPX transport under a single hard five-second deadline. This remains an intermediate,
claim-bound query result; it is not yet a database-revalidated availability snapshot.

## Public contract

`app.google_calendar_freebusy_deadline` exports only:

```python
query_google_calendar_freebusy_with_deadline(
    *, claim, access_result, window_start, window_end
)
```

The function is async and keyword-only. It accepts no caller-selected endpoint, calendar, client,
transport, timeout, deadline, headers, or response policy.

## Execution order

1. Validate and snapshot the sealed refresh-credential claim, sealed access-token result, ownership
   fence, and bounded aware query window before reading the clock or creating any network resource.
2. Establish one absolute monotonic deadline exactly five seconds in the future.
3. Within one cancellation scope, call the private HTTPX adapter with the fixed endpoint, validated
   copied access token, canonical request body, fixed timeout, and identical absolute deadline. The
   adapter runs in a child task so a dependency cannot erase the caller's cancellation state with
   `uncancel()`. A two-phase gate prevents transport I/O from starting until task ownership is
   committed. The child converts every success, failure, or control into a redacted outcome envelope
   so raw `KeyboardInterrupt` or `SystemExit` values never escape through asyncio's task machinery.
   Any parent-side control or failure cancels and observes that owned child without propagating
   cancellation, bounded by the original absolute deadline; unfinished task outcomes are observed
   without extending the public operation.
4. Detect external cancellation that a dependency suppressed.
5. Within the same scope, run semantic response validation, busy normalization, and post-network
   claim/access-result integrity and mutation checks.
6. After leaving the scope, reject any result completed at or after the exact deadline.

The deadline therefore covers client and TLS setup, connect and request I/O, response streaming,
response/client cleanup, JSON parsing, interval normalization, ownership revalidation, and final
result acceptance.

## Failure precedence and mapping

Precedence is:

1. external cancellation or another direct/nested control exception;
2. internal deadline expiration; and
3. provider, transport, or protocol failure.

Mappings are:

- invalid preparation, request framing, malformed success responses, unexpected statuses, or
  mutation-fence failures → `invalid_response`;
- transport `TimeoutError` or absolute deadline expiration → `timeout`;
- 401 → `authorization_required`;
- 403, 408, 429, 5xx, valid calendar errors, arbitrary network/TLS failures, or ordinary cleanup
  failures → `provider_unavailable`.

If internal cancellation is suppressed and a dependency returns or raises something else, deadline
expiration still wins. If a real task cancellation is suppressed, the task's pending cancellation
state still wins over timeout, malformed data, or cleanup failure. Completion immediately before the
deadline is accepted; equality is rejected. A child outcome first observed by cleanup at or after the
hard cutoff cannot extend the public operation; it is consumed asynchronously and the deadline wins.

All public failures and controls are fresh, cause-free, logging-free, and sanitized by the existing
group-aware FreeBusy boundary. Tokens, tenant identifiers, request/response data, provider text, and
dependency exception objects must not appear in messages, representations, traceback locals, or
logs. Exception-group traversal deduplicates shared nodes by identity and stops after 4,096 unique
nodes, preventing adversarial shared graphs from expanding exponentially during sanitization. If
the graph exceeds that bound before classification completes, the boundary fails closed as a fresh
cancellation rather than assuming no control exists.

## Files

- `app/google_calendar_freebusy_deadline.py`
- `app/google_calendar_freebusy.py`
- `tests/test_google_calendar_freebusy_deadline.py`
- `specs/issue-56aj-google-calendar-freebusy-deadline.md`

The existing pure protocol receives only the shared bounded exception-graph traversal. No migration,
route, API contract, scheduling behavior, or frontend is changed.

## Deferred work

1. Refresh-token → FreeBusy orchestration with persisted credential revalidation and authorization
   recovery mutation.
2. A verified `CalendarBusySource` and fail-closed availability integration.
3. Caching, connection/recovery routes and settings, operational telemetry, and live Google tests.

## Non-goals

- Database access, sessions, locks, credential persistence, disconnect, or recovery mutation.
- Final verified calendar snapshot creation or availability consumption.
- Multiple calendars, groups, event details, writes, retries, or redirects.
- Scheduling, booking, agenda, background jobs, routes, or UI behavior.
