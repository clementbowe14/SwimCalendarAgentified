# Issue #56ah: strict claim-bound Google FreeBusy protocol

## Goal

Translate one immediate-use, claim-bound Google access token into opaque normalized busy intervals
through a database-free and HTTP-client-injected protocol. This slice establishes the semantic
request, response, ownership, and privacy boundary before a concrete asynchronous HTTPX adapter is
added.

It does not create a final verified calendar snapshot. Persisted credential freshness must be
revalidated after the later network orchestration before availability may consume these periods.

## Public contract

`app.google_calendar_freebusy` exposes:

- the fixed `https://www.googleapis.com/calendar/v3/freeBusy` endpoint;
- a sealed transport response and a structural `post_json` transport protocol;
- a sealed `GoogleCalendarFreeBusyQueryResult`; and
- `query_google_calendar_freebusy()`, a keyword-only synchronous protocol boundary.

The query accepts the original refresh-credential claim, its exact claim-bound access-token result,
an aware bounded window, and a trusted injected transport. The result contains only the account,
instructor, connection, expected connection revision, canonical UTC window, and normalized opaque
`CalendarBusyInterval` values. It contains no access token, refresh token, calendar identifier, raw
response, provider diagnostic, title, attendee, description, location, or event ID.

## Ownership and mutation fence

Before transport construction or invocation, the protocol:

1. validates and snapshots the exact sealed credential claim;
2. validates the keyed integrity tag over every field of the exact sealed access-token result and
   snapshots it;
3. requires their account, instructor, connection, and revision fields to match; and
4. validates the query window and constructs one deterministic request.

After the transport returns, it revalidates both original objects and compares every field with the
pre-network snapshots, including the refresh token, access token, and expiry. Mutation or coherent
substitution across the transport gap fails closed.

## Exact request

The transport is called exactly once with:

- URL `https://www.googleapis.com/calendar/v3/freeBusy`;
- the validated access token as a dedicated argument;
- timeout `5.0`; and
- deterministic UTF-8 JSON containing exactly, in order:
  - `timeMin`;
  - `timeMax`;
  - `timeZone: "UTC"`;
  - `calendarExpansionMax: 1`; and
  - `items: [{"id": "primary"}]`.

The request contains no group expansion, alternate calendar, event fields, tenant identifiers,
refresh credential, retry policy, redirect policy, or caller-selected endpoint or timeout.

Google documents this endpoint as returning inclusive-start, exclusive-end busy ranges and permits
the least-privilege `calendar.freebusy` scope:
<https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query>.

## Strict response contract

Only HTTP 200 with an exact JSON content type and a non-empty body of at most 1 MiB is parsed. JSON
must be strict UTF-8 with unique member names, finite numbers, bounded nesting by shape, exact
`kind=calendar#freeBusy`, response bounds equal to the requested instants, no non-empty groups, and
exactly one calendar result. The returned calendar map key is validated and discarded rather than
assumed to remain the literal alias `primary`.

The calendar result accepts only a `busy` list and an optional `errors` list. A structurally valid
non-empty provider error list is `provider_unavailable`; malformed errors are `invalid_response`.
Busy entries contain exactly `start` and `end`, use strict RFC3339 timestamps with at most
microsecond precision, and are capped by the provider-neutral 10,000-interval limit.

The existing `normalize_busy_intervals()` boundary remains authoritative for UTC conversion,
clipping, sorting, overlap and adjacency merging, half-open semantics, and the 15-day window cap.
FreeBusy does not reveal whether an interval came from a timed or all-day event; 23- and 25-hour
DST all-day ranges are therefore preserved only as their returned instants.

## Failure and confidentiality

- HTTP 401 becomes `authorization_required`.
- A transport `TimeoutError` becomes `timeout`.
- HTTP 403, 408, 429, 5xx, valid calendar errors, and arbitrary transport failures become
  `provider_unavailable`.
- Invalid inputs, unexpected statuses, malformed bodies, and protocol mismatches become
  `invalid_response`.
- Cancellation, keyboard interruption, system exit, and generator exit—including controls nested
  anywhere in a `BaseExceptionGroup`—propagate as fresh, cause-free controls.

All outward failures are fresh, fixed-message, cause-free, logging-free, and contain no token,
request body, response body, provider text, calendar key, tenant identifier, or ambient exception
text. The public boundary protects its first emitted instruction and nested cleanup/classification
interruptions. Deliberately reentrant trace callbacks that raise while the already-sanitizing public
fallback handler itself is entered are outside the cause-free diagnostic guarantee, but credential
and tenant references must already be cleared.

## Files

- `app/google_calendar_freebusy.py`
- `app/google_calendar_token_exchange.py`
- `tests/test_google_calendar_freebusy.py`
- `specs/issue-56ah-google-calendar-freebusy-protocol.md`

The token-exchange result gains a private keyed integrity tag so downstream protocol boundaries can
authenticate its provenance. No migration, route, OpenAPI contract, or frontend is changed.

## Deferred work

1. Concrete async HTTPX/TLS FreeBusy transport with one hard overall deadline.
2. Claim → refresh → FreeBusy → persisted revalidation orchestration and recovery mutation.
3. A production `CalendarBusySource` and fail-closed availability/booking integration.
4. OAuth routes, disconnect/recovery settings, caching, and operational telemetry.

## Non-goals

- Database access, locks, sessions, credential persistence, or authorization-state mutation.
- Final `CalendarBusySnapshot(status="verified")` creation.
- Multiple calendars, groups, event metadata, recurrence expansion, writes, retries, or live Google
  calls.
- Availability, booking, agenda, routes, background jobs, or UI behavior.
