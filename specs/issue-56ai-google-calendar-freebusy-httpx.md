# Issue #56ai: bounded Google FreeBusy HTTPX transport

## Goal

Add the concrete asynchronous HTTP transport required by the pure, claim-bound FreeBusy protocol
without yet exposing a public network orchestration service. This is a deliberately small transport
slice: it validates one already-prepared primary-calendar request, performs one fixed POST, and
returns a sealed raw transport response for the existing semantic protocol to complete.

## Contract

`app.google_calendar_freebusy_httpx` has no public exports. Its private `_post_json_async()` seam
accepts only the fixed FreeBusy endpoint, one visible-ASCII access token, the exact canonical request
shape produced by `_prepare_google_calendar_freebusy_query()`, the fixed `5.0` timeout, and a future
absolute deadline.

The request body must contain exactly, in order:

- canonical UTC `timeMin` and `timeMax` values;
- `timeZone: "UTC"`;
- `calendarExpansionMax: 1`; and
- `items: [{"id": "primary"}]`.

The interval must be positive and no longer than 15 days. No caller-selected endpoint, calendar,
headers, timeout, redirect, retry, query parameter, cookie, authentication object, or proxy is
accepted.

## Network policy

The adapter reuses the existing hardened async HTTP client factory. That factory provides certifi
trust roots, required certificate and hostname verification, TLS 1.2 or newer, disabled TLS key
logging, ignored environment/proxy configuration, HTTP/1.1 only, one non-reusing connection, no
redirects, and no retries.

The adapter sends exactly one POST with:

- `Authorization: Bearer <access token>`;
- `Accept: application/json`;
- `Accept-Encoding: identity`; and
- `Content-Type: application/json`.

The access token is never placed in the URL or body.

## Response boundary

Only HTTP 200 bodies are read. Every non-200 status is returned with no content type and an empty
body, preventing provider diagnostics or redirect targets from crossing the transport boundary.

Successful bodies are consumed only through `aiter_raw()` and are capped at exactly 1 MiB. The
adapter accepts no content encoding or one exact case-insensitive `identity` value. It rejects
compressed or ambiguous encodings, duplicate or noncanonical `Content-Length` values, declared
oversize bodies, streamed overflow, length mismatches, and non-byte chunks. The response stream and
client are closed on every path. Ordinary cleanup errors cannot replace an earlier failure or
cancellation, while cancellation raised during cleanup takes precedence. HTTPX and HTTPcore timeout
failures are normalized to `TimeoutError`; no retry is attempted.

## Files

- `app/google_calendar_freebusy_httpx.py`
- `tests/test_google_calendar_freebusy_httpx.py`
- `specs/issue-56ai-google-calendar-freebusy-httpx.md`

No existing production module, route, migration, API contract, scheduling behavior, or frontend is
changed.

## Deferred work

1. A public async boundary that prepares the claim-bound query, establishes one hard overall
   deadline, invokes this adapter, completes the pure protocol, and sanitizes all outward failures.
2. Refresh-to-FreeBusy orchestration with persisted credential revalidation and recovery mutation.
3. A verified `CalendarBusySource`, fail-closed availability integration, caching, routes, settings,
   and operational telemetry.

## Non-goals

- Database access, sessions, locks, credential mutation, or authorization recovery.
- JSON interpretation, provider-status classification, or final busy-interval normalization.
- Calendar event details, multiple calendars, groups, writes, redirects, retries, or live Google
  calls.
- Availability, booking, agenda, background jobs, routes, or UI behavior.
