# Issue 13C3: Strict client-create request schema

## Objective

Define the strict, normalized input contract for a future authenticated client-create endpoint.
This is a dependency-only slice of issue #13 built on the canonical client storage and transactional
create repository already merged.

This PR changes no HTTP route, generated OpenAPI, persistence behavior, geocoding, or frontend. The
service and route remain separate follow-ups so validation changes have a small review surface.

## Request shape

`CoachClientMutation` contains only provider-editable client fields:

- `name`
- nullable `phone`
- nullable `email`
- `notes`
- `location_mode`: `client_location` or `service_location`
- optional `client_location` containing only an address
- `default_duration_mins`
- ordered `availability` windows

The schema rejects unknown fields. It never accepts provider identity, client IDs, revisions,
coordinates, status, deactivation metadata, payment data, lesson history, or progress fields.

## Text and contact validation

- Trim name, notes, address, phone, and email.
- Require a nonblank name up to 120 characters.
- Require at least one nonblank phone or valid email.
- Allow display phone numbers with 7-20 total digits, ordinary separators, balanced non-nested
  parentheses, at most one leading plus, and an optional numeric `x`, `ext`, `ext.`, or `#`
  extension. Reject malformed punctuation and extension-only values.
- Limit email to 254 characters, notes to 4,000 characters, phone to 64 characters, and address to
  300 characters.
- Reject control characters, line/paragraph separators, bidirectional controls, zero-width
  formatting characters, Unicode surrogate code points, and other Unicode format characters in
  plain-text name, notes, address, and phone fields.

## Location and duration validation

- `client_location` mode requires an address.
- `service_location` mode may omit `client_location`; a saved client address is still allowed for a
  future mode switch.
- Client location input contains only `address`; coordinates remain server-owned.
- Duration is a strict integer from 15 to 240 minutes in 15-minute increments.

## Availability validation

- Accept zero to 64 ordered windows.
- Each window has one to seven unique weekday names and 24-hour `HH:MM` start/end labels with only
  optional surrounding ASCII spaces, which are trimmed during normalization.
- Normalize grouped weekdays to canonical Monday-Sunday order.
- Require end later than start on the same day; overnight windows are not represented.
- Reject windows that overlap on any shared weekday. Adjacent intervals and equal times on
  different weekdays remain valid.

## Compatibility boundary

- Keep every existing response model and generated API contract unchanged.
- Do not expose the new schema through a route in this PR.
- Do not add geocoding, persistence calls, update/deactivation input, frontend fields, or mocks.
- Do not close issue #13.

## Verification

- assert the exact JSON Schema properties, required set, limits, time patterns, and unknown-field
  rejection;
- prove normalization for text, contacts, weekday order, and time labels;
- prove both location modes and nullable contacts;
- reject malformed contacts, unsafe text, client coordinates, invalid durations, duplicate days,
  malformed/reversed times, overlapping windows, oversized collections, and unknown fields; and
- keep the complete backend and generated-contract checks green.
