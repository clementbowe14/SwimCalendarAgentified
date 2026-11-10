# Issue 16: Provider-managed breaks and schedule blockers

## Objective

Let an authenticated provider create, edit, and remove non-lesson schedule events so every availability path accounts for the provider's real day. The pilot supports three visibly distinct blocker kinds:

- `break`: protected recovery time that also activates the saved post-break recovery rule;
- `personal`: a personal event, optionally tied to a physical address for adjacent travel calculations; and
- `unavailable`: locationless time that cannot be booked.

All three kinds exclude overlapping lesson starts. Only `break` applies post-break recovery. A locationless blocker consumes time without pretending the provider travelled or changing the provider's last known location.

This issue covers one-day agenda management. Week view, automatic travel-hold persistence, recurring blockers, external-calendar sync, lesson rescheduling, and arbitrary all-day or overnight events are out of scope.

## Canonical storage model

Continue storing every confirmed lesson and provider blocker in `appointments` so the existing PostgreSQL active-overlap exclusion constraint remains the single concurrency boundary.

Add:

- `blocker_kind`, nullable string constrained to `break`, `personal`, or `unavailable`; and
- `blocker_create_payload`, nullable JSON allowed only on blocker rows and containing the immutable
  canonical create envelope needed to replay the original response; and
- `revision`, non-null bigint with a server default of `1` and a positive-value check.

Make `client_id`, `location_address`, `lat`, and `lng` nullable. Location columns must be an all-present or all-null tuple. Lessons and holds retain a complete location and client. Blockers have no client or lesson request; `personal` may have a complete location, `unavailable` has none, and newly created `break` rows have none. A migrated legacy break may retain its complete saved location to preserve existing route behavior, but the blocker write API never accepts a break location.

Database checks enforce:

- `event_type = 'block'` has a non-null `blocker_kind` and null client/request IDs;
- active non-block appointments have a null `blocker_kind`, a non-null client, and a complete
  location; cancelled historical tombstones may be clientless for compatibility with existing
  notification/calendar history;
- `is_break` is true exactly for a `break` blocker; and
- `unavailable` blockers are locationless.

Existing `event_type = 'block'` rows are backfilled as `break`, keep a complete legacy location when one exists, and have their placeholder client cleared. Existing lesson/hold rows remain non-blockers.

Downgrade first refuses without mutation if any `personal`, `unavailable`, or locationless blocker
exists because the old schema could only represent located breaks. It also refuses while a
clientless historical tombstone exists because the old client column was non-null. Otherwise it
restores the legacy placeholder client, drops the new checks/columns, and returns the
location/client columns to non-null.

## Time and location semantics

The blocker API accepts a provider-local calendar date and `HH:MM` start/end labels. Both labels must:

- be valid in the provider's saved IANA timezone;
- fall on the same local date;
- use strict 24-hour minute precision; and
- resolve to an end instant after the start instant.

Ambiguous or nonexistent DST labels return a field-level `422 VALIDATION_ERROR`; the server never guesses a fold or silently shifts a time. Valid instants are stored as naive UTC using the existing canonical storage convention. Responses return timezone-aware provider-local ISO datetimes.

`personal` may include an optional address. When present, the server geocodes it before opening the write transaction and stores the server-owned coordinates atomically with the address. `break` and `unavailable` reject a location. A locationless personal event is valid.

For availability calculations:

- every blocker ends the bookable gap before it and starts the next gap after it;
- a location-aware personal event participates in inbound and outbound route calculations like a physical appointment;
- a locationless event requires no inbound route and preserves the last known physical location for the next route;
- consecutive locationless events preserve that same last known physical location, and travel from
  a candidate before them can begin only after the final locationless event and must still reach the
  next physical event on time; and
- only a just-ended `break` sets `is_after_break` for post-break recovery.

The provider's base location still anchors the start of a shift. Issue 16 does not persist generated travel rows.

## HTTP contract

All endpoints are under the authenticated, CSRF-protected `/api/coach` router. Provider identity comes only from the session; no instructor ID is accepted in the path or body.

### List

`GET /api/coach/blockers?date=YYYY-MM-DD`

Returns all active blockers overlapping the requested provider-local day in start-time order. It returns an empty list for a valid day with none.

### Create

`POST /api/coach/blockers`

Request:

```json
{
  "kind": "personal",
  "title": "School pickup",
  "date": "2026-11-10",
  "start_time": "15:00",
  "end_time": "15:45",
  "location_address": "1200 Main St, Austin, TX"
}
```

The endpoint returns `201` with the canonical blocker. The request rejects unknown fields. Title is trimmed, required, and at most 160 characters. Create uses an `Idempotency-Key` header (8-160 characters) scoped to the authenticated provider. Replaying an identical request returns the original blocker; reusing the key for different canonical input returns `409 IDEMPOTENCY_KEY_CONFLICT`.
An exact replay is resolved from the persisted canonical address and UTC interval before invoking the
geocoder again, so a routing-provider outage cannot turn a completed create into an uncertain retry.
The immutable create envelope also retains the original server-owned coordinates and creation
timezone, so retries after later edits or soft deletion return the exact original create response.
The service resolves that envelope and validates an exact replay with its immutable creation
timezone before loading the provider's current timezone or setup, so later configuration changes
cannot invalidate a completed create.
Concurrent identical requests compare kind, title, UTC interval, and normalized address; coordinate
differences from concurrent geocoder responses are server-owned output and do not create a false key
conflict.

### Update

`PUT /api/coach/blockers/{blocker_id}`

Uses the same full-replacement body as create plus required `expected_revision`. It row-locks a blocker owned by the authenticated provider, rejects a stale revision with `409 BLOCKER_STALE`, and excludes its own row from conflict checks. Unknown, deleted, lesson, and cross-provider IDs all return the same generic `404 RESOURCE_NOT_FOUND`.

### Remove

`DELETE /api/coach/blockers/{blocker_id}?expected_revision=N`

Soft-cancels the owned blocker, increments its revision, and sets `cancelled_at`; success returns `204`. A stale revision returns `409 BLOCKER_STALE`. Repeated removal and cross-provider access return the same generic `404` and expose no ownership information.

### Response

```json
{
  "id": "app_1234abcd",
  "kind": "personal",
  "title": "School pickup",
  "start_time": "2026-11-10T15:00:00-06:00",
  "end_time": "2026-11-10T15:45:00-06:00",
  "local_date": "2026-11-10",
  "local_start_time": "15:00",
  "local_end_time": "15:45",
  "timezone": "America/Chicago",
  "revision": 2,
  "location": {
    "address": "1200 Main St, Austin, TX",
    "lat": 30.2672,
    "lng": -97.7431,
    "is_instructor_facility": false
  }
}
```

`location` is null for locationless blockers. Conflict responses use `409 SCHEDULING_CONFLICT`. Stale writes use `409 BLOCKER_STALE`. Geocoding/routing configuration, timeout, and provider-unavailable failures retain the existing stable error codes. A failed validation, geocode, stale revision, conflict, or persistence operation leaves the prior blocker unchanged.
The response always includes the `location` key, with a nullable value.
The generated OpenAPI contract declares the stable `ErrorResponse` envelope for blocker `404`,
`409`, and `422` responses.

## Agenda projection

The command-center schedule projection is a discriminated union keyed by `kind`. It adds `personal`
and `unavailable`, requires a positive blocker revision and a required-but-nullable location field,
and allows blocker locations to be null. Lesson items
retain required client, location, and status fields in both the runtime model and generated contract.

`GET /api/coach/schedule` keeps its compatibility shape but adds `blocker_kind` and optional blocker location semantics so old consumers can distinguish blockers without inferring from `is_break`.

## Instructor mobile workflow

Extend the existing Today agenda; do not add a navigation tab.

- Put a thumb-sized `Add schedule event` action in the schedule hero.
- Open a modal sheet with large segmented controls for Break, Personal, and Unavailable; native date/time controls; title; and an optional `This event has a location` address field available only for Personal.
- Reuse the sheet for edits, prefilled from the canonical schedule item.
- Show a clearly labelled edit button on every blocker row. Lessons remain unchanged for issue 16.
- Put the save action at the reachable bottom of the sheet, disable repeated submission, preserve values on failure, and announce pending/success/error status through `aria-live`.
- When a save or removal request fails while the sheet remains open, move focus to the announced
  error status so a keyboard or screen-reader user receives the result immediately.
- Put `Remove event` in edit mode. It opens a second confirmation state naming the persisted event and its persisted date/time, while `Keep event` restores any unsaved draft; only the explicit destructive confirmation calls DELETE.
- Distinguish break, personal, and unavailable rows through label, color, and text rather than color alone.
- Close on successful save/removal, persist the saved event's date in the URL before refreshing the snapshot, preserve a sensible focus target, and mark currently shown availability stale so a new search is required before booking. If refresh fails, reload must still open the saved date.

At 320px and 375px, every interactive control is at least 44px high, the sheet scrolls independently without horizontal overflow, and its primary/destructive actions remain reachable above the safe-area inset. Escape/backdrop closes a clean editor but must not discard a pending request or bypass delete confirmation. Any dirty close attempt opens a named discard confirmation with `Keep editing` and `Discard changes`; only the latter loses the draft.

Mock mode implements the same CRUD interface in memory, resolves retries with the original creation
timezone, stores schedule instants canonically, reprojects ordinary reads after a provider-timezone
change, includes every event overlapping the requested local day, and filters availability against
the current lesson/blocker schedule using canonical instants.
The live adapter sends cookie credentials and the CSRF header for every mutation,
URL-encodes IDs and dates, and never accepts coordinates from the UI.

## Security and concurrency

- Authentication and CSRF protections are inherited from the coach router.
- Every read and mutation filters by both blocker ID and authenticated instructor ID.
- The PostgreSQL active-overlap exclusion constraint is authoritative for races; the service precheck exists only for a friendly response.
- Create idempotency is provider-scoped and compares the complete canonical blocker input, including
  the normalized address and UTC interval; coordinates remain server-owned output. The immutable
  create envelope is never changed by update or soft deletion.
- Update and delete row-lock the target, compare the required revision, increment it, and commit once.
- Private responses use `Cache-Control: private, no-store` and `Referrer-Policy: no-referrer`.
- No public invite/client endpoint exposes blocker titles, kinds, addresses, IDs, or coordinates.

## Test contract

### Migration and database

- exact columns, nullability changes, defaults, checks, and backfill;
- old break rows remain breaks after upgrade;
- invalid kind/location/is-break combinations fail at the database boundary;
- active blockers participate in the existing overlap exclusion constraint;
- representable downgrade round-trip and refusal-without-mutation for new blocker kinds.

### Backend and scheduling

- provider-scoped list/create/update/delete and indistinguishable cross-tenant `404`s;
- create idempotent replay before and after edit/delete or provider-timezone changes, concurrent coordinate drift, and key/payload conflict;
- update/delete optimistic concurrency, atomicity, and soft-delete visibility;
- overlap rejection against lessons and every blocker kind, including a database race;
- local-to-UTC persistence and provider-local responses across ordinary, spring-forward, and fall-back dates;
- location geocoding success/failure without client-owned coordinates;
- every kind excludes availability;
- only break activates post-break recovery;
- location-aware personal events affect adjacent travel; locationless and consecutive locationless events preserve the last physical location;
- command-center and compatibility schedule projections distinguish all kinds without leaking sentinel coordinates; and
- generated schedule contracts preserve the discriminated union and required lesson location/client/status fields;
- existing booking, request-confirmation, provider-settings, and routing tests remain green.

### Frontend and browser

- adapter paths, date/ID encoding, cookie credentials, CSRF headers, payloads, and server errors;
- create each kind, edit all fields, and delete only after named confirmation;
- cancel and failure preserve data without mutating the agenda, and delete confirmation describes persisted rather than unsaved values;
- kind-specific fields and distinct agenda presentation;
- stale availability after every successful mutation;
- mock availability immediately excludes newly created blockers;
- save and removal request failures focus their announced status while retaining the draft/confirmation;
- real-backend create/edit/remove round trip, including reload after a failed cross-date refresh;
- mock overlap checks compare canonical instants across seeded naive timestamps and offset timestamps;
- keyboard focus trapping/restoration and Escape behavior; and
- one-handed 320px/375px controls, safe-area reachability, scrolling, and no horizontal overflow.

