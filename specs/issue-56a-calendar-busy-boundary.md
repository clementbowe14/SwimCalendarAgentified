# Issue #56a: opaque external-calendar busy boundary

## Goal

Define the provider-neutral boundary that Dayweave will use to consume one connected Google
Calendar without importing event details. This first issue #56 slice is deliberately database-free
and network-free so availability, booking revalidation, and the future agenda share one small,
testable contract before OAuth or Google transport is added.

Issues #54 and #55, which provide provider sessions and authoritative provider settings, are merged.

## Domain contract

`app.external_calendar` exposes:

- `CalendarBusyInterval(start, end)`: one opaque half-open busy interval, `[start, end)`;
- `CalendarBusySnapshot(status, periods)`: either `disconnected` or `verified`;
- `CalendarBusySource.query_busy_periods(...)`: a provider-scoped source that accepts only a trusted
  server-derived instructor ID and aware query-window instants; and
- typed unavailable errors with the safe reason codes `authorization_required`, `timeout`,
  `provider_unavailable`, and `invalid_response`.

`disconnected` and `verified` with zero periods are intentionally different. A disconnected provider
does not imply a provider failure; a connected source that cannot verify availability must raise a
typed failure and must never return a verified-empty snapshot.

The domain objects contain no title, attendee, description, location, Google event ID, calendar email,
credential, or raw provider error.

## Normalization and safety

The boundary:

- requires timezone-aware query and interval datetimes and canonicalizes them to UTC;
- rejects zero-length, reversed, naive, malformed, or wrongly shaped periods and snapshots;
- clips periods to the requested window;
- sorts and coalesces overlapping or adjacent periods;
- excludes an interval ending at the window start or beginning at the window end;
- permits at most fifteen elapsed query days, a hard cap that accommodates the 14-local-day pilot
  horizon across a daylight-saving transition;
- accepts at most 10,000 source intervals before normalization; and
- forwards the trusted instructor ID on every query without caching or mixing tenant results.

An adapter translates known failures to domain errors. The boundary canonicalizes typed errors into
fresh cause-free errors, maps raw timeouts to `timeout`, and maps every other unexpected source error
to `provider_unavailable`. It likewise sanitizes errors raised while iterating periods. Provider error
text cannot appear in the outward message, cause, or formatted traceback.

## Google direction

The later Google adapter will call the FreeBusy API for the fixed `primary` calendar and request only
`https://www.googleapis.com/auth/calendar.freebusy`. FreeBusy returns occupied start/end ranges rather
than event content, preventing event details from entering Dayweave in the first place.

Official references:

- <https://developers.google.com/workspace/calendar/api/auth>
- <https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query>
- <https://developers.google.com/identity/protocols/oauth2/web-server>

## Verification

Unit tests cover:

- disconnected versus verified-empty snapshots;
- stateful provider isolation and UTC query forwarding;
- clipping, ordering, overlap and adjacency merging, and exact half-open boundaries;
- spring-forward and fall-back all-day shapes with their real 23/25-hour durations;
- malformed windows, periods, containers, snapshots, failing iterators, and cardinality limits; and
- raw and chained source failures without secret-bearing exception output.

These tests require neither PostgreSQL nor network access. The full backend suite must remain green.

## Deferred slices

1. Encrypted provider-connection and single-use OAuth-state persistence.
2. OAuth authorization, callback, status, and disconnect services and routes.
3. Google token refresh and FreeBusy transport.
4. Settings UI and live recovery/disconnect tests.
5. Consumption by issue #18 availability and issue #15 agenda.

The OAuth callback must live outside the cookie-protected `/api/coach` namespace because the existing
provider cookie is `SameSite=Strict` and is not sent on Google's cross-site return. A later slice will
authenticate that callback by atomically consuming a short-lived state digest bound to the initiating
provider account and still-active provider session, then redirect only to a fixed settings destination.

## Non-goals

- OAuth, credential storage, migrations, routes, OpenAPI, or frontend changes in this slice.
- Reading or persisting Google event metadata or busy-period caches.
- Writing Dayweave appointments to Google Calendar.
- Multiple calendars, non-Google providers, webhooks, or background synchronization.
- Changing the existing outbound Google/ICS calendar handoff.
