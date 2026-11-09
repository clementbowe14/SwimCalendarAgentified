# Issue 55: Authoritative provider, service, location, and working-hour settings

## Objective

Give each authenticated pilot provider one persistent scheduling identity and one service configuration. Provider settings become the authoritative source for the command-center snapshot, public invite presentation, service-location bookings, shift-origin travel, and weekly availability.

The completed flow must:

- save and reload the provider display name, optional business name, and valid IANA timezone;
- distinguish the provider's private base/start-of-shift location from the client-facing service location;
- configure one service name and one default duration;
- configure one opening/closing interval for each enabled day of the week;
- geocode changed addresses before any settings mutation is committed;
- expose a derived setup state without storing a second, drift-prone boolean;
- prevent new scheduling calculations while required configuration is incomplete; and
- preserve the current pilot through a deterministic migration/backfill.

Multiple services, split shifts, staff, roles, payments, industry templates, and invite management are outside this issue.

## Current state

`instructors` currently mixes several concepts:

- `name` and `timezone` are provider identity;
- `main_address`, `lat`, and `lng` are used both as shift origin and service facility;
- one global string `work_start_time`/`work_end_time` pair is applied to every date; and
- business name is inferred at request time from an invite token.

The existing `/api/coach/settings` resource is scheduler buffer/payment policy and remains that resource. Issue 55 adds a separate current-provider resource rather than changing the meaning of the existing endpoint.

All availability implementations ultimately call `tools.find_available_swim_slots`, but the tool currently reads the legacy instructor location and global working-hour pair directly. Both public invite API generations call that shared tool through separate wrappers.

## Storage model

### Provider configuration columns

Keep provider identity on `instructors` and add nullable configuration columns:

- `business_name` (`varchar(160)`, nullable);
- `base_address` (`varchar(300)`, nullable);
- `base_lat` / `base_lng` (nullable floats, present as a pair with the address);
- `service_address` (`varchar(300)`, nullable);
- `service_lat` / `service_lng` (nullable floats, present as a pair with the address);
- `service_name` (`varchar(160)`, nullable); and
- `default_duration_mins` (nullable integer, 15–240 and divisible by 15 when present).

`name` remains the saved display name and `timezone` remains the saved IANA timezone. The existing `main_address`/`lat`/`lng` and `work_start_time`/`work_end_time` columns remain temporarily for rollback and old-reader compatibility, but issue 55 code must not use them as authoritative scheduling configuration. Successful provider updates mirror service location into the legacy main-location columns and mirror the first enabled weekday interval into the legacy work-time columns.

Database checks require location address/latitude/longitude triples to be either wholly present or wholly absent, coordinates to be within geographic bounds, and default duration to be in range and divisible by 15.

### Weekly hours

Add `provider_working_hours`:

- `instructor_id`, foreign key to `instructors`, cascade delete;
- `weekday`, integer 0–6 where Monday is 0;
- `enabled`, non-null boolean;
- `start_time` / `end_time`, nullable SQL `TIME`; and
- `created_at` / `updated_at`.

The composite primary key is `(instructor_id, weekday)`. Checks enforce the weekday range and exactly one of these row states:

- enabled with both times present and `end_time > start_time`; or
- disabled with both times null.

Overnight and split intervals are intentionally unsupported. The canonical API representation always contains exactly seven unique weekday rows in Monday-through-Sunday order. All seven disabled is a valid, complete configuration that intentionally yields no availability.

### Derived setup state

Do not persist `setup_complete`. Derive it from the authoritative row and weekly hours on every read. Required configuration is:

- non-blank display name;
- a valid IANA timezone;
- complete base and service location triples;
- non-blank service name;
- a valid default duration; and
- seven valid weekly-hour rows.

Business name is optional. The response includes both `setup_complete` and a stable `setup_missing_fields` list using request field names. Invalid legacy timezone data counts as missing/invalid configuration rather than crashing a read.

## Migration and backfill

Add Alembic revision `20261109_0010` after `20261109_0009`.

For every existing instructor:

1. copy `main_address`/`lat`/`lng` into both explicit locations;
2. copy the most recent invite's non-blank business name when one exists;
3. copy that invite's preferred duration when valid, otherwise use 60 minutes;
4. use `Swim lesson` as the one service name because no existing service-name field exists; and
5. create seven enabled working-hour rows from a valid legacy interval, preserving the prior behavior that the global interval applied to every date. If either legacy time is missing, malformed, or not strictly increasing, create no hour rows and leave that provider setup-incomplete rather than inventing availability.

The most recent invite is selected deterministically by `created_at DESC, token DESC`. Invite values are migration inputs only; runtime provider configuration is never inferred from invites after this revision.

Downgrade must preflight before making any mutation. It refuses with an actionable error when base and service locations differ, all seven weekdays are not enabled with one identical interval, or provider business/default-duration values cannot be represented by existing invite snapshots. Representable rows are copied back to legacy instructor/invite fields before the weekly-hours table and explicit columns are dropped. Refusal leaves schema and data untouched. The rollback limitation is documented in the runbook.

## HTTP contract

### Current provider settings

Add authenticated, tenant-derived endpoints:

- `GET /api/coach/provider`
- `PUT /api/coach/provider`

No instructor/provider ID is accepted from the path or body.

The GET response contains:

```json
{
  "id": "inst_001",
  "display_name": "Coach Sarah",
  "business_name": "Swim Command",
  "timezone": "America/Chicago",
  "base_location": {
    "address": "100 Main St, Austin, TX",
    "lat": 30.2672,
    "lng": -97.7431
  },
  "service": {
    "name": "Swim lesson",
    "default_duration_mins": 45,
    "location": {
      "address": "100 Main St, Austin, TX",
      "lat": 30.2672,
      "lng": -97.7431
    }
  },
  "working_hours": [
    {"weekday": "monday", "enabled": true, "start_time": "08:00", "end_time": "17:00"},
    {"weekday": "tuesday", "enabled": true, "start_time": "08:00", "end_time": "17:00"}
  ],
  "setup_complete": true,
  "setup_missing_fields": []
}
```

The actual response always includes all seven weekdays. Locations and service fields may be `null` on GET for an incomplete provider.

PUT is a full replacement of user-editable provider configuration. It accepts:

```json
{
  "display_name": "Coach Sarah",
  "business_name": "Swim Command",
  "timezone": "America/Chicago",
  "base_address": "100 Main St, Austin, TX",
  "service_address": "Riverside Pool, Austin, TX",
  "service_name": "Private lesson",
  "default_duration_mins": 45,
  "working_hours": [
    {"weekday": "monday", "enabled": true, "start_time": "08:00", "end_time": "17:00"},
    {"weekday": "tuesday", "enabled": false, "start_time": null, "end_time": null}
  ]
}
```

The request must contain all seven weekdays. Display name, addresses, service name, timezone, and default duration are required to complete the replacement; business name may be null or blank and is normalized to null. Request and nested models reject unknown fields, including any client-supplied provider ID or coordinates.

Validation rules:

- trim human-entered text and reject blank required values;
- cap text lengths at the storage limits;
- accept timezone only when `zoneinfo.ZoneInfo` resolves it;
- accept only canonical weekday names, exactly once each;
- require `HH:MM` times for enabled days and null times for disabled days;
- reject `end_time <= start_time`; and
- require default duration to be 15–240 minutes in 15-minute increments.

Pydantic validation locations provide field-level `VALIDATION_ERROR` details. Address failures use the existing `GEOCODING_FAILED` error with `details.field` set to `base_address` or `service_address`. Routing configuration/unavailability/timeouts retain their stable 503 errors.

### Atomic address update

Geocode every changed or coordinate-missing requested address before opening the write transaction. When both addresses are identical, geocode once and reuse the result. Only after all geocodes succeed:

1. lock the authenticated instructor row;
2. replace identity, timezone, service, locations, and all seven working-hour rows in one transaction;
3. mirror the compatibility columns; and
4. commit once.

Any validation, geocoding, or persistence error leaves the prior provider configuration and weekly hours unchanged. Coordinates are server-owned. Issue 55 deliberately uses last-committer-wins updates after the row lock; optimistic revisions/ETags are deferred because they are not required for the single-provider pilot. Each committed address and coordinate pair always comes from the same validated request.

### Snapshot and invite projection

The command-center snapshot's `instructor` object gains the same authoritative base/service/default-duration/working-hours/setup fields. Existing legacy summary fields remain during this issue for compatibility but are projected from the new configuration rather than invite data. Business name is never looked up from `invite_tokens` at runtime.

Both public invite response families project the saved provider display name, optional business name (falling back to display name for presentation), service name, service location, and default duration. Existing invite-specific allowed-duration lists remain token-scoped for compatibility; the provider default is a preselected default rather than the only valid duration, and it must be present in the returned list. The private base location and coordinates are never exposed publicly. Facility selection ignores all client-supplied coordinates and resolves only to the saved service location.

## Scheduling behavior

Add one provider-configuration loader used by every scheduling path. `tools.find_available_swim_slots` must:

1. load the authoritative provider configuration;
2. raise a domain `ProviderSetupIncomplete` when required configuration is absent;
3. return an empty slot list when the requested weekday is intentionally disabled;
4. use that weekday's local interval and saved IANA timezone for day bounds;
5. use base location as the shift-start origin; and
6. use service location when the requested lesson is at the provider facility.

Authenticated coach wrappers translate the domain error to `409 PROVIDER_SETUP_REQUIRED` with `details.missing_fields`. Public wrappers return the same stable code and generic scheduling-unavailable message without missing-field names or any private address details. Because coach availability, direct booking, public availability, public request validation, client-invite slot search, alternative validation, and confirmation all converge on the shared tool, the gate applies consistently. A pure idempotent replay of an already-committed mutation may still return its original result.

Weekly times are provider-local, strict minute-precision `HH:MM` labels with half-open `[start, end)` bounds. On a DST date where a configured opening or closing label is nonexistent or ambiguous, availability fails closed with no slots for that date rather than inventing an instant. Existing confirmed UTC appointments are never rewritten when timezone or settings change.

Unconfirmed facility offers and requests remain live projections, not immutable appointments. Before request creation or confirmation, their stored service address/coordinates must still match the current provider service location and the selected time must still fit current timezone/hours. Otherwise return the existing scheduling-conflict/new-times behavior. Confirmed appointments retain their historical location snapshot.

Reads of existing schedules, clients, requests, settings, and account state remain available while setup is incomplete.

## Instructor mobile workflow

Extend the existing Settings screen; do not create another navigation tab.

- Put a setup-status card first. Incomplete setup names the remaining sections and uses the primary save action to resolve them.
- Add a provider form with display name, optional business name, timezone, base address, service name, service address, and default duration.
- Render seven large weekday rows. Each row has a thumb-sized enable switch and native start/end time controls that disable when the day is off.
- Keep the existing scheduler-rule and payment sections below the provider form with their own save action.
- Use a full-width/sticky mobile save action, 44px minimum controls, and no horizontally scrolling grid.
- Disable repeated submission while saving and announce progress/success through `aria-live`.
- Render server field errors next to their corresponding input and focus the first invalid field; retain a summary alert for non-field failures.
- Preserve entered form values on save failure.

On live bootstrap, an incomplete snapshot opens Settings and skips the eager availability request. Schedule reads remain visible through navigation, but Find and request-confirmation controls show/route to the setup-required state until setup is complete. After a successful provider save, replace the snapshot provider projection, clear field errors, and resume availability loading only if the returned configuration is complete. All async work remains guarded by the authentication generation so stale saves cannot repopulate state after sign-out or session expiry. At 320px and 375px the page must have no body-level horizontal overflow, all interactive controls remain at least 44px high, and the sticky action cannot cover the Sunday row.

Mock mode implements the same provider read/update interface in memory.

## Test contract

### Migration/database

- exact columns, checks, foreign key, composite key, and SQL `TIME` columns;
- deterministic backfill of identity/business/default duration/locations and seven weekday rows;
- existing providers are setup-complete after upgrade;
- safe downgrade round-trip for representable data plus refusal-without-mutation for divergent locations or weekly hours; and
- two instructors' settings remain isolated.

### Backend/API

- GET/PUT round-trip identity, optional business name, timezone, locations, service, duration, and hours;
- invalid IANA timezone and each invalid working-hour shape return field-level 422 errors;
- a failure on either geocode leaves every prior field and weekday unchanged;
- same-address updates perform one geocode;
- tenant identity comes only from the authenticated session;
- snapshot and both invite response families use saved provider configuration, not stale invite presentation fields;
- incomplete setup returns stable 409 for coach/public/client-invite availability and booking/confirmation validation, with private missing fields only on coach responses;
- disabled weekdays return no slots;
- different weekday intervals and timezone/DST boundaries control real slot windows;
- base and service coordinates are exercised independently in route calculations;
- stale facility offers/requests cannot commit the prior service location;
- public payloads recursively exclude the base location and client-submitted facility coordinates; and
- existing settings, auth, idempotency, notification, and calendar contracts remain green.

### Frontend/browser

- adapter GET/PUT paths, cookie credentials, CSRF header, payload, and error details;
- setup-incomplete bootstrap opens Settings without issuing availability;
- all fields save and reload from a real backend;
- weekday enable/time controls serialize canonically;
- loading, success, inline validation, geocoding failure, and retry states;
- one-handed mobile viewport has reachable controls and no horizontal overflow;
- scheduling controls remain gated until setup completes; and
- delayed provider responses cannot restore protected state after sign-out/401.

Regenerate OpenAPI and committed JSON fixtures. Add the new current-provider fixture to exhaustive contract validation and verify that `/api/coach/provider` inherits cookie authentication and unsafe-method CSRF documentation while no public provider-settings route exists.
