# Issue 13A: Canonical client profile persistence

## Objective

Create the provider-owned persistence boundary needed for end-to-end client management without
changing the current command-center response, instructor UI, or booking behavior. This is the first
small, independently deployable slice of issue #13.

This slice adds storage for generic client profile data, a client-specific session-duration
override, structured weekly availability, optimistic revisions, and soft-deactivation metadata.
It deliberately does not add client mutation endpoints or expose the new fields through the
existing command-center snapshot. Those changes belong in separate follow-up PRs so regressions can
be isolated to one layer at a time.

## Storage model

### Client profile

Keep `clients.id` as the existing primary key and add:

- `notes`, non-null text with an empty-string default;
- `location_mode`, one of `client_location` or `service_location`;
- `default_duration_mins`, a non-null client-specific session duration;
- `revision`, a positive bigint beginning at `1`; and
- `deactivated_at`, nullable UTC timestamp for future soft removal.

Add a unique `(id, instructor_id)` key so child records can use a tenant-preserving composite
foreign key. Backfill the mode with the existing projection rule: a missing saved location or a
complete location exactly matching the provider's configured service location becomes
`service_location`; every other complete saved location becomes `client_location`.

Database checks enforce:

- a non-blank client name;
- `active` or `deactivated` status;
- a complete-or-empty address/latitude/longitude tuple with geographic coordinate bounds;
- a valid location mode, with a complete saved location whenever `client_location` is selected;
- a 15-240 minute duration in 15-minute increments;
- a positive revision; and
- a deactivation timestamp only on an inactive row.

Duration is backfilled from the owning provider's authoritative default, falling back to the
existing 45-minute client projection only when that provider configuration is incomplete.

The migration does not invent contact information. Existing rows without phone or email remain
readable, and this storage-only slice deliberately leaves the current demo snapshot unchanged. The
future authenticated create/update API will require at least one contact method for every new or
edited active profile. A database contact constraint can be validated after legacy/demo rows are
remediated without coupling that rollout to this migration.

### Availability windows

Add `client_availability_windows` with:

- globally unique `id`;
- `client_id` plus `instructor_id` as a composite foreign key to the owning client;
- `day_mask`, an integer bit mask where bit 0 is Monday and bit 6 is Sunday;
- `start_time` and `end_time` SQL `TIME` values;
- non-negative `position` for deterministic presentation ordering; and
- created/updated timestamps.

The day mask must be between `1` and `127`, and `end_time` must be later than `start_time` on the
same day. Overnight windows are not supported. The composite foreign key rejects cross-provider
client/window combinations at the database boundary. Hard deletion cascades only for
administrator-level cleanup; the product workflow will deactivate clients and preserve their
windows and scheduling history.

Each row is one logical window and may cover several selected weekdays through its mask. This
preserves the user's grouped weekday/weekend choices without storing an unvalidated JSON day list.

## Migration and rollback

Add Alembic revision `20261110_0013` after `20261110_0012`.

Before changing schema or data, upgrade validates every legacy client. It refuses with an
actionable error if a row has a blank name, unsupported status, partial/blank location tuple, or
out-of-range coordinates. Refusal leaves the previous revision and data unchanged. Valid rows are
backfilled deterministically as described above. A legacy deactivated row receives its existing
`updated_at` as the best available deactivation timestamp.

Upgrade and downgrade first take fail-fast PostgreSQL `ACCESS EXCLUSIVE ... NOWAIT` locks on the
profile tables. Migrations therefore run only while application traffic is quiescent; contention
aborts the transaction before preflight or mutation so the deploy can retry without a multi-table
foreign-key lock-order deadlock.

Downgrade preflights before dropping anything. It refuses if any availability window exists or any
client contains profile state the old schema cannot reconstruct: non-empty notes, a duration that
differs from the provider default used by the backfill, a revision other than `1`, a deactivation
timestamp that differs from `updated_at`, or a location-mode choice that does not match the old
saved-location/service-location comparison. A representable downgrade drops the child table,
checks, composite key, and new columns while preserving every legacy column and row.

## Compatibility boundary

- Existing command-center and scheduling reads continue to use their current projection.
- No route, request model, response model, OpenAPI fixture, or frontend adapter changes in this PR.
- Existing `ClientDB(...)` callers that omit `location_mode` infer `client_location` for a complete
  non-service saved location and `service_location` otherwise. They infer duration from the owning
  provider, preserving deterministic seed/test behavior.
- No current client, invite, lesson-request, appointment, or notification row is deleted or
  relinked.
- Do not add an appointment-to-client foreign key in this slice. Existing direct booking can still
  create name-derived client IDs; that writer must be converted before such a constraint is safe.

## Follow-up slices

1. Add authenticated, provider-scoped list/create/update/deactivate contracts and transactions,
   including contact validation, location geocoding, availability replacement, revision checks,
   and safe history preservation.
2. Replace the read-only client detail with the mobile list/create/edit/deactivate workflow and add
   live/mock adapter plus browser coverage.

Only the final slice should close issue #13.

## Test contract

- exact client columns, defaults, checks, unique key, child table, index, and composite foreign key;
- deterministic location-mode, duration, deactivation, and default-value backfill;
- database rejection of malformed locations, invalid modes/durations/revisions/statuses, invalid
  availability masks/times/positions, and cross-provider window ownership;
- ORM relationship ordering and cascade metadata alignment;
- representable upgrade/downgrade round trip;
- upgrade refusal on malformed legacy data without partial schema mutation;
- downgrade refusal on new profile or availability data without partial schema mutation; and
- the complete existing backend suite remains green, proving the storage-only slice did not change
  runtime API behavior.
