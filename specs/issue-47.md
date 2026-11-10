# Issue #47: authoritative provider scheduling policy

Status: implementation specification.

Issue: https://github.com/clementbowe14/SwimCalendarAgentified/issues/47

## Goal

Make one saved, provider-owned scheduling policy authoritative for every
availability search and every booking revalidation. Providers can turn
distance-based buffers on or off, adjust the minutes assigned to ordered
distance tiers, and set post-break recovery. Travel remains mandatory,
start-of-shift never adds a prior-appointment buffer, and starts remain fixed to
15-minute local-time boundaries.

## Current state and gaps

The repository already has a frozen `BufferPolicy`, persisted scheduler rows,
strict tier coverage validation, and a common `tools.find_available_swim_slots`
engine seam used by provider, legacy-public, and canonical client-invite paths.
The remaining behavior is not the issue #47 contract:

- `buffers_enabled` disables post-break recovery as well as distance buffers;
- a provider can enable a distance buffer at shift start;
- rounding can be changed to 5, 10, or 30 minutes;
- payment fields are embedded in and rewritten by scheduling-policy saves;
- the settings UI loses edited values after a failed save and leaves displayed
  availability stale after a successful save; and
- integration coverage observes buffer metadata but does not prove changed slot
  starts, public/provider parity, or post-break behavior.

## Canonical API contract

Add versioned, authenticated `GET/PUT /api/coach/scheduling-policy` endpoints.
The GET response and a new `scheduling_policy` field in the command-center
snapshot expose this shape:

```json
{
  "revision": 0,
  "distance_buffers_enabled": true,
  "post_break_recovery_mins": 15,
  "distance_buffer_tiers": [
    {"id":"near","label":"Under 5 mi","min_miles":0,"max_miles":4.999,"buffer_mins":10},
    {"id":"medium","label":"5-10 mi","min_miles":5,"max_miles":10,"buffer_mins":20},
    {"id":"far","label":"Over 10 mi","min_miles":10.001,"max_miles":null,"buffer_mins":30}
  ]
}
```

The PUT accepts only the provider-editable policy values and an optimistic
revision:

```json
{
  "expected_revision": 0,
  "distance_buffers_enabled": true,
  "post_break_recovery_mins": 15,
  "distance_buffer_tiers": [
    {"id":"near","buffer_mins":10},
    {"id":"medium","buffer_mins":20},
    {"id":"far","buffer_mins":30}
  ]
}
```

Tier labels and bounds are response-only. Update IDs must exactly match the
current server-owned IDs in the same order; missing, unknown, duplicate, or
reordered IDs return the standard atomic 422 envelope. Unknown top-level fields
are rejected. A stale `expected_revision` returns `409
SCHEDULING_POLICY_STALE` and changes nothing. A no-row default has revision zero;
its first successful write creates revision one. Every later successful write
increments by one.

Keep `GET /api/coach/settings` and the snapshot's existing `rules` field as
deprecated read compatibility contracts for #48. Once the canonical endpoint is
enabled, `PUT /api/coach/settings` returns `410
SCHEDULING_POLICY_ENDPOINT_MOVED` and performs no write; stale legacy clients
cannot bypass canonical optimistic concurrency. New instructor code uses only
`scheduling_policy` and never calls the legacy endpoint.

Payment columns already present in PostgreSQL are retained unchanged, but the
canonical scheduling-policy API, its TypeScript contract, fixtures, and settings
UI never read or write them. The deprecated compatibility contract may continue
to return them until #48 removes that surface. A future payment issue may
migrate or reuse the stored values.

No-row reads return deterministic defaults without creating a row. A write
validates the complete document before changing anything and remains atomic.
Malformed persisted policy fails closed through the existing sanitized
configuration error; it never falls back to zero buffers or defaults.

## Tier validation

Keep one to ten ordered tiers. IDs and labels are trimmed; IDs are unique and
match `[a-z][a-z0-9_-]{0,63}`. Buffer minutes and post-break recovery are
integers from 0 through 240. Bounds are finite, non-negative, and at most 10,000
miles.

Persisted full tiers and legacy full-document writes remain centrally validated.
The first tier starts at zero, only the final tier is unbounded, and each later
minimum is strictly greater than the prior finite maximum. Adjacent tiers must
cover the two-decimal routing-distance lattice exactly once:

`floor(current.max_miles * 100) + 1 == ceil(next.min_miles * 100)`.

This preserves the existing `4.999 → 5` and `10 → 10.001` defaults while also
rejecting raw sub-hundredth overlap. Empty, duplicated, unordered, overlapping,
gapped, non-finite, or non-terminating full policies are rejected before they
can enter the engine. Canonical updates cannot alter ranges and additionally
validate the exact ordered ID set.

## Domain behavior

`BufferPolicy` remains an immutable domain value and contains only:

- `distance_buffers_enabled`;
- `post_break_recovery_mins`; and
- an immutable tuple of distance tiers.

The slot increment is a domain constant of 15 minutes, not provider state.
`SwimSchedulerConstraintEngine` requires an explicit policy; it has no silent
default-policy constructor path in production or tests.

Inbound precedence is normative:

1. The virtual start-of-shift checkpoint always uses zero buffer.
2. A preceding break uses `post_break_recovery_mins`, independently of the
   distance-buffer toggle.
3. Other transitions use the matching distance tier when distance buffers are
   enabled, otherwise zero.

Travel time is always enforced. Turning distance buffers off does not disable
travel, overlap protection, working hours, post-break recovery, or 15-minute
rounding. Return legs before a following event reserve travel plus the outbound
distance buffer when enabled. `TimeSlotDTO.buffer_mins` reports the inbound
buffer actually enforced.

Rounding retains the existing timezone- and DST-safe implementation, but every
ceiling and candidate increment uses the fixed 15-minute constant.

## Persistence and migration

Add Alembic revision `20261109_0011_provider_scheduling_policy`.

The migration is additive and reversible. It adds only a non-null
`policy_revision` bigint with server default zero, a non-negative check, and a
PostgreSQL `BEFORE UPDATE` trigger. When any physical policy field changes and
the writer leaves `policy_revision` unchanged, the trigger increments the
revision. A canonical CAS update that explicitly advances the revision is not
incremented again. This makes policy writes from a pre-0011 binary visible while
the migration-first rollout is in progress. Downgrade removes the trigger,
function, check, and column. It does not rename, normalize, or drop any existing
scheduling or payment column, so a rollback restores the prior binary's exact
persisted behavior.

The repository maps physical `buffers_enabled` to canonical
`distance_buffers_enabled` and physical `post_break_buffer_mins` to canonical
`post_break_recovery_mins`. New engine behavior ignores persisted
`rounding_increment_mins` and `apply_distance_buffer_at_shift_start`; the values
remain intact for rollback. Payment columns are never touched by a canonical
write.

Canonical saves use one PostgreSQL upsert/CAS statement. It inserts only when
`expected_revision == 0`, or updates only when the current revision matches,
while atomically incrementing the revision. A racing first insert and concurrent
updates therefore produce one success and one stable 409 instead of an integrity
error or lost update.

Rollout order is mandatory:

1. Apply revision 0011 while the old application is still serving. The trigger
   tracks any remaining old policy writes.
2. Drain every old application process and stop legacy write traffic.
3. Deploy the new API and instructor frontend together. Only then enable the
   canonical PUT; the legacy PUT is read-only/410 in this release.
4. Verify canonical GET/PUT and revision increments before resuming scheduling
   writes.

Old and new application binaries must never serve policy writes concurrently.
Rollback drains the new binary before downgrading the schema and restoring the
old application. Add these steps to the operations runbook.

Update `scripts/check_scheduler_settings.py` to validate only the canonical
scheduling policy and emit the same safe reason codes used by rollout
documentation.

## Shared availability and revalidation

Every active path must use one policy snapshot loaded at operation start:

- provider availability and direct booking;
- legacy public availability and request creation;
- canonical client-invite availability, request creation, and recurrence
  validation;
- alternative-offer validation and provider confirmation; and
- agent/tool search and booking entry points.

Provider and public adapters continue to share the existing engine seam. Issue
#47 adds parity tests but does not consolidate duplicate lifecycle adapters;
that remains issue #48. Existing offer/request evidence is historical and is not
rewritten after a policy change. A booking or confirmation revalidates with the
current policy and rejects a now-unsafe stale start.

The policy read is the scheduling operation's linearization point. A policy
update committed before that read is observed. One committed after that read
affects the next scheduling operation, not the already-running one. Recurrence
and confirmation loops reuse that single immutable snapshot and never mix
revisions. Settings CAS prevents lost settings edits; issue #47 does not add
policy-wide locks or make settings writes participate in appointment
transactions. Existing appointment overlap/advisory-lock behavior remains
unchanged.

Public payload projection is unchanged in this issue. Removing provider-only
feasibility evidence belongs to issue #19.

## Instructor experience

Keep the current visual system and render a scheduling-only settings section:

- a large switch labeled “Distance-based buffers”;
- one editable buffer-minute control for each server-ordered tier;
- an editable “Post-break recovery” minute control; and
- read-only explanatory copy that starts align to 15-minute boundaries and the
  first lesson includes travel but no distance buffer.

Tier labels and bounds are server-owned for the pilot. The UI edits tier minutes
but does not add, remove, drag, or reorder tiers. It sends the exact ordered
`{id, buffer_mins}` update list returned by the server.

The distance toggle is a native checkbox exposed as `role="switch"`, with a
visible focus ring, accessible name, and at least a 44×44px target. Each numeric
value supports direct entry plus `type="button"` decrement/increment actions
with value-specific accessible names, 44×44px targets, focus preservation, and
0–240 clamping. Controls have visible minute units and programmatic labels. The
save action is a sticky, safe-area-aware mobile bar at least 48px tall. The form
is keyboard-submittable, carries `aria-busy` while saving, and disables mutable
controls during the request. Errors focus an alert summary; success uses a live
status region.

Maintain a scheduling-policy draft separate from the last saved snapshot.
Re-rendering during save or after an error must preserve the values the provider
entered. Save failure leaves the prior current slots untouched because policy did
not change. A 409 leaves the draft intact, focuses a conflict message, and offers
a “Reload saved policy” action rather than silently overwriting another tab.

After a successful save, replace canonical policy and draft with the response.
If there is an active search context, immediately clear its old slots so they
cannot be booked under stale evidence, mark availability `refreshing`, and issue
a new search. Success marks the new slots `current`. Failure leaves no bookable
slots, marks availability `stale`, and reports that policy saved but safe starts
need a retry. If no search has run, remain `idle` and do not invent one. A
monotonic request token prevents older save/search responses from overwriting a
newer policy, date, client, or navigation context. Navigating away does not
re-enable stale booking controls.

At 320px and 375px, the page and scheduling section have no horizontal overflow,
the sticky bar does not cover the final control, and every switch, stepper, save,
error-recovery, and retry action remains reachable.

Payment UI is removed from this section. No replacement payment workflow is
created in issue #47.

## Test plan

### Unit and validation

- enabled, disabled, and custom distance tiers change the first eligible slot;
- disabling distance buffers preserves travel and post-break recovery;
- changing recovery changes the first slot after a real break;
- shift start always applies travel with `buffer_mins == 0`;
- return-leg protection remains active with distance buffers enabled;
- fixed 15-minute rounding remains correct across ordinary, non-hour-offset,
  spring-forward, and fall-back cases;
- every tier validation rule, including raw sub-hundredth overlap, is covered;
- the engine cannot be constructed without an explicit policy.

### Persistence, API, and migration

- defaults do not create a row;
- policy fields round-trip and payment columns remain byte-for-byte unchanged;
- invalid PUTs are 422 and preserve the prior row;
- stale revisions and racing first writes return one success and one stable 409;
- two providers with different policies remain isolated;
- direct SQL updates shaped like a pre-0011 writer increment revision through
  the database trigger;
- the deprecated legacy PUT returns 410 and cannot mutate policy or payment
  data;
- upgrade/downgrade/upgrade preserves every legacy setting and payment value.

### Cross-surface integration

- save settings, repeat the same provider search, and assert changed start
  instants—not only changed metadata;
- provider, legacy-public, and canonical client-invite searches use one seeded
  provider, date, client location, duration, deterministic routing result, and
  full-working-window client availability with `recurrence="single"`; compare
  UTC start/end instants only;
- stale direct booking, public request creation, recurrence selection,
  alternatives, and confirmation cannot bypass the current policy;
- stored historical slot evidence is unchanged after revalidation.

### Frontend and contracts

- form parsing submits only canonical fields;
- draft values survive failed saves, pending re-renders, and validation errors;
- successful save replaces state and triggers an availability refresh;
- save without a prior query remains idle; refresh failure clears bookable
  stale slots and exposes retry; stale async responses cannot replace newer
  context;
- switch semantics, stepper clamping/names/focus, `aria-busy`, focused errors,
  live success, sticky-bar clearance, and 320/375px overflow are covered in
  Playwright;
- a live FastAPI/PostgreSQL browser test changes policy, observes changed
  availability, reloads to verify persistence, and restores the original policy;
- OpenAPI and coach fixtures are regenerated and payment/legacy controls are
  absent from the scheduling contract.

## Acceptance traceability

- **Toggle/tier changes affect availability:** policy integration tests compare
  actual start instants before and after saves.
- **Post-break recovery:** persisted-break tests exercise independent recovery.
- **Start-of-shift behavior:** engine and API tests require travel and zero
  buffer.
- **One policy for provider/public:** parity tests cover all three surfaces.
- **Invalid tiers rejected:** centralized schema tests and atomic API tests cover
  ordering, gaps, overlaps, and termination.
- **Save → search:** backend integration and live browser tests exercise the
  complete workflow.

## Non-goals

- Payment configuration or payment-data migration.
- Configurable rounding, configurable shift-start buffers, multiple services,
  or multiple scheduling policies.
- Tier creation/reordering UI or a generic rules engine.
- Public response redesign (#19), public lifecycle consolidation (#48), managed
  breaks/blockers (#16), or Google Calendar ingestion (#56).
- Recalculating existing appointments, requests, offers, notifications, or
  calendar exports.
