# Issue 56an: provider-neutral busy periods in availability

## Goal

Teach the existing one-time availability and final booking-revalidation path to
consume an explicitly injected `CalendarBusySource`. Verified external busy
periods become opaque, locationless schedule blockers before the constraint
engine evaluates candidate slots.

This is a route-inert composition slice. Existing callers that do not inject a
source retain disconnected-calendar behavior.

## Contract

`find_available_swim_slots(...)` accepts an optional provider-neutral busy
source. After resolving the provider's timezone-correct working interval, it:

1. queries the source with the exact trusted instructor ID and UTC working
   interval;
2. validates and canonicalizes the snapshot through
   `query_calendar_busy_snapshot(...)`;
3. treats `disconnected` as no external blockers;
4. converts every verified interval into a locationless blocker containing
   only its start and end instants; and
5. evaluates internal events and external blockers together.

No Google type, credential, event identifier, title, attendee, description, or
location crosses this boundary. A timeout, authorization failure, malformed
response, or provider outage propagates as the existing sanitized
`ExternalCalendarUnavailable` family, so the caller cannot return unverified
slots. The injected source crosses validation through a cleared one-use handoff;
failure tracebacks retain neither that source nor its configuration and cipher.

`book_swim_lesson(...)` accepts and forwards the same source into its
authoritative availability recheck. An idempotent replay of an already-created
booking remains a read of that prior result and does not perform a new external
query.

Internal appointment coverage is subtracted from each external interval before
locationless fragments enter the engine. This preserves every internal event
and its travel location while ensuring the engine never receives overlapping
internal and external events that could create a false opening.

## Verification

Focused tests cover:

- exact tenant and timezone-correct UTC window forwarding;
- disconnected and verified-empty behavior;
- opaque busy intervals removing overlapping slots;
- overlapping internal and external blockers without false openings;
- fail-closed provider errors; and
- forwarding the same source into final booking revalidation.

The full backend suite must remain green on Python 3.11 and 3.12.

## Files and non-goals

This slice is limited to the provider-neutral scheduling path, focused tests,
and this specification.

It does not configure a process-wide source, load environment secrets,
instantiate `GoogleCalendarBusySource`, add OAuth or disconnect routes, change
OpenAPI or API error payloads, render settings UI, add agenda items, cache busy
periods, persist external events, expose provider details, or write to an
external calendar.
