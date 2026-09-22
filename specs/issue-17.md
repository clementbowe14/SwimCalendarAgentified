# Issue #17: configurable buffer rules

Status: approved specification. Do not implement until the user gives a separate
explicit implementation instruction.

Issue: https://github.com/clementbowe14/SwimCalendarAgentified/issues/17

## Goal

Make every availability and booking-validation path use the authenticated
instructor's persisted buffer settings while preserving the current default
schedule exactly. Keep the constraint engine deterministic and independent of
the database. This is an extension of the existing `scheduler_settings` record
and coach settings API, not a replacement settings subsystem.

## Current state and gap

- `scheduler_settings` already persists `rounding_increment_mins`,
  `post_break_buffer_mins`, and JSON distance tiers per instructor.
- `GET/PUT /api/coach/settings` and the command-center snapshot already return
  those fields, but validation only constrains individual field ranges. It does
  not reject empty, overlapping, gapped, unordered, duplicated, or unterminated
  tiers.
- `SwimSchedulerConstraintEngine` still hard-codes the 10/20/30-minute tiers,
  15-minute post-break buffer, and 15-minute rounding. A module-global engine is
  shared by every instructor, so saved settings do not affect availability.
- The instructor settings screen renders tier inputs, but Save resubmits the
  unedited snapshot and therefore does not persist what the user typed.

## Behavior and data model

Extend the existing per-instructor `scheduler_settings` row with two additive,
non-null Boolean columns:

- `buffers_enabled`, default `true`: when false, distance and post-break buffer
  minutes are zero. Travel time, appointment overlap checks, working hours, and
  rounding remain enforced.
- `apply_distance_buffer_at_shift_start`, default `false`: when false, preserve
  the current start-of-shift exception: the first candidate starts after travel
  from the instructor's main location and rounding, without a distance buffer.
  When true, apply the matching distance tier before the first lesson as well.

Keep the existing tier JSON and numeric columns. Migration defaults must make
all existing rows behave exactly as they do before this change. Instructors
without a settings row receive the deterministic defaults below; the read path
must not create a row.

Default policy:

```json
{
  "buffers_enabled": true,
  "apply_distance_buffer_at_shift_start": false,
  "rounding_increment_mins": 15,
  "post_break_buffer_mins": 15,
  "distance_buffer_tiers": [
    {"id":"near","label":"Under 5 mi","min_miles":0,"max_miles":4.999,"buffer_mins":10},
    {"id":"medium","label":"5-10 mi","min_miles":5,"max_miles":10,"buffer_mins":20},
    {"id":"far","label":"Over 10 mi","min_miles":10.001,"max_miles":null,"buffer_mins":30}
  ]
}
```

Existing appointments, lesson requests, and slot-offer snapshots retain their
stored travel/buffer evidence and are never recalculated by a settings update.
Revalidation calculations are ephemeral and do not overwrite that historical
evidence. New searches use one settings snapshot loaded at request start.
Booking, request-creation, and approval paths must revalidate against the current
persisted policy, so a stale offered slot that is no longer safe is rejected
through the existing scheduling-conflict behavior rather than silently booked.

## Policy object and engine integration

Add one small immutable `BufferPolicy` value object owned by the scheduling
domain. It contains only the five scheduler fields above and has pure methods
for distance lookup, post-break buffer selection, start-of-shift selection, and
ceiling rounding. Rounding accepts the instructor timezone as an explicit input;
the policy does not look it up. It must not import SQLAlchemy, FastAPI, or API
schemas.

Construct the constraint engine with `(routing_provider, buffer_policy)`. The
application service loads `get_coach_scheduler_settings(instructor_id)` once per
availability or validation operation, converts it to `BufferPolicy`, and creates
an engine for that operation. Do not put database reads inside the engine and do
not add policy registries, plugin systems, caches, inheritance hierarchies, or
global mutable per-instructor state.

All callers must use the same policy-aware path, including:

- coach availability and direct booking validation;
- public legacy invite availability and request creation;
- client-invite slot search, request creation, recurrence validation, and coach
  approval/revalidation;
- agent/tool entry points that call `find_available_swim_slots`.

Every appointment-creating entry point, including
`SwimSchedulingAgent.confirm_booking` and direct calls to
`tools.book_swim_lesson`, must invoke the policy-aware validation service
immediately before persistence or become private to a transactional workflow
that already did so. Previously returned slot data is not authorization to
bypass current rules.

Legacy and client-invite request creation must rerun availability with a newly
loaded policy snapshot before creating the request. Every occurrence in a weekly
series is revalidated, not only the first. Coach confirmation repeats validation
under the existing schedule locks.

The global routing-provider test hook may remain, but no global constraint
engine may retain one instructor's settings for another request.

## API contract and compatibility

Keep `GET /api/coach/settings`, `PUT /api/coach/settings`, and the snapshot
endpoint. Add the two Boolean fields to `CoachSchedulerRules` with the defaults
above. Requests from existing clients that omit the new fields remain valid.
For an existing settings row, an omitted new Boolean preserves its persisted
value; for a missing row it receives the deterministic default. Responses always
include both fields. The service must use the request's explicitly supplied field
set rather than treating Pydantic defaults as user intent. For an existing row,
omitted Boolean columns are not included in the SQL update, so they retain their
database value at commit time rather than a value read earlier. Explicit fields
update atomically; missing-row creation applies defaults atomically. Do not rename
or remove existing fields or change existing error envelopes.

The existing `payments` object, its validation, persistence columns, and API
shape remain behaviorally unchanged. `_default_scheduler_rules()` remains the
canonical complete no-row default, including `enabled_methods=["card", "ach",
"cash"]`, `cancellation_window_hours=12`, and
`require_payment_method_for_new_clients=true`. Buffer-policy conversion ignores
payments. A PUT persists the supplied validated payment object atomically and a
buffer-only UI save must not reset non-default payment values.

Supported rounding increments are exactly `5`, `10`, `15`, and `30` minutes.
They all divide an hour and are compatible with the existing working-window
model. The chosen increment controls ceiling rounding and candidate iteration.
Lesson duration validation remains unchanged.

Validate the whole settings document before any write; updates are atomic. A
validation failure changes nothing and returns the existing FastAPI 422 error
shape with field locations. Centralize cross-tier validation in
`CoachSchedulerRules` so the API and defaults use one definition.

Tier validation rules:

- one to ten tiers;
- trim IDs and labels before persistence. IDs must match
  `[a-z][a-z0-9_-]{0,63}` and are unique after trimming; labels are non-empty and
  at most 120 characters;
- finite numbers only; `min_miles >= 0`, `buffer_mins` is an integer from 0 to
  240, tier bounds are at most 10,000 miles, and a finite `max_miles` is greater
  than or equal to `min_miles`;
- tiers must be supplied in strictly ascending `min_miles` order;
- the first tier starts at `0`; only the last tier has `max_miles = null`;
- tier intervals are closed: normalized distance `d` matches when
  `min_miles <= d` and either `d <= max_miles` or `max_miles` is null;
- routing distance must be finite and non-negative, then is normalized with the
  routing layer's existing two-decimal `round` behavior before lookup. Stored
  bounds are neither rounded nor rewritten;
- adjacent tiers cover the discrete hundredth-mile lattice exactly once. Parse
  finite bounds into exact decimal values from their JSON/Python string
  representation; never multiply binary floats for validation. For
  adjacent finite/current and next tiers,
  `floor(current.max_miles * 100) + 1 == ceil(next.min_miles * 100)`. This makes
  the existing `4.999/5.0` and `10.0/10.001` boundaries valid without overlap;
- every non-negative, finite normalized distance resolves to exactly one tier;
  negative or non-finite distances are rejected as invalid scheduling input.

At a boundary, preserve current behavior: distances below 5 use 10 minutes,
5 through 10 inclusive use 20, and values above 10 use 30. Add explicit tests
for `4.99`, `5.00`, `10.00`, and `10.01`, plus custom boundaries such as
`4.994/4.995`, floating-point inputs, and values beyond every finite boundary.

Buffer precedence is normative:

- with buffers enabled, a preceding break uses `post_break_buffer_mins` instead
  of, not in addition to, the inbound distance tier;
- every candidate before a following protected event, including a break,
  reserves return travel plus the outbound distance tier;
- with buffers disabled, inbound, post-break, shift-start, and outbound buffers
  are all zero, while travel and rounding remain enforced;
- zero miles resolves through the tier containing zero. Same-facility
  transitions therefore receive that tier except when the virtual shift-start
  exception applies;
- `apply_distance_buffer_at_shift_start=false` applies only to the virtual
  opening checkpoint and reports an enforced buffer of zero. An event that
  happens to end at `day_start` is not the virtual checkpoint;
- `TimeSlotDTO.buffer_mins` always reports the inbound buffer actually enforced,
  never a configured but bypassed value.

Ceiling rounding first converts the source instant to the instructor timezone
and computes the smallest naive local wall-clock boundary label greater than or
equal to that source local label, with seconds and microseconds zero. Resolve
that label in the instructor timezone using the source `fold` when the label is
ambiguous and that fold is valid. If the label is nonexistent during
spring-forward, advance through subsequent local boundary labels until one
exists. Only then convert the resolved aware value to UTC. Do not revisit a lower
repeated wall-clock label merely because its second-fold instant occurs later;
for example, first-fold `01:58` ceilings to the real `02:00`, not second-fold
`01:00`. The UTC offset may change across the transition. Candidate iteration
advances on the UTC timeline and applies the same local-boundary predicate, so
both distinct aware instants for a repeated boundary are discoverable and remain
distinguishable by offset. Do not deduplicate by naive wall time. User-supplied
naive ambiguous times remain rejected. Tests must cover a non-hour-offset IANA
timezone, independent rounding in both repeated folds, first-fold `01:58` to the
real `02:00`, exact boundaries immediately before and after each transition, a
nominal spring-forward ceiling inside the missing hour, and booking each aware
repeated-hour candidate by its exact UTC instant.

## Instructor frontend

Preserve the current visual system. Implement the settings editor as a semantic
form and add controls for buffer enablement,
start-of-shift behavior, rounding increment, post-break minutes, and each tier's
buffer minutes. Read values from the form before calling `saveRules`; disable
all mutable controls while pending; show backend validation using the existing
error surface; on success, replace the local snapshot rules with the server
response. Every control has a programmatic label and stable name keyed by tier
ID; Boolean controls expose checked state; numeric controls use `min`, `max`, and
`step`; rounding uses a constrained select. Keyboard submission works, pending
status is announced via `aria-live`, double submission is prevented, field
errors are associated where possible, an error summary receives focus, and
rejected values remain editable. Do not build a general form framework or add
tier creation/reordering in this issue.

Frontend mock data and TypeScript contracts must include the additive fields.
Mock and live adapters must behave consistently. Regenerate OpenAPI and contract
fixtures after the backend schema changes.

## Concurrency and failure behavior

- Settings are read directly from Postgres for each scheduling operation; there
  is no settings cache or stale-cache fallback in this issue.
- A direct booking, lesson-request creation, recurrence series, or coach
  confirmation loads and validates exactly one policy before its first
  availability check. That object is passed through every internal check and
  recurrence occurrence; policy is never reloaded per occurrence.
- The operation linearizes when it reads the settings row/defaults. An update
  committed before that read is observed; one committed afterward affects only
  later operations. Settings writes are not serialized by schedule locks.
- Last completed settings write wins, matching the existing PUT contract. An
  optimistic-lock/version protocol is out of scope because the current API does
  not expose partial updates and issue #17 does not require collaborative edits.
- Missing settings use defaults. The settings repository translates malformed
  JSON, row-shape errors, and persisted-policy validation failures into a
  dedicated internal configuration exception, always closes its session, and is
  not caught as invalid client input by availability adapters. GET settings,
  snapshots, searches, booking/request/recurrence validation, and approval fail
  closed through the existing sanitized 500 response. Emit a structured
  `scheduler_policy_invalid` error with instructor ID, operation, and stable
  reason codes only; never log serialized settings, labels, client data,
  addresses, coordinates, or request bodies. Never fall back to zero buffers.

## Test plan

### Migration and deployment compatibility

Before strict read-time validation is enabled, migration or a mandatory
deployment preflight validates every existing `scheduler_settings` row with the
new schema. It aborts with instructor IDs and non-sensitive reason codes if any
row is invalid; it must not silently rewrite custom tiers. Document a remediation
procedure. This is required because the current API accepted shapes, including a
single finite tier, that the new complete-coverage rules reject.

Use a two-phase deployment: first deploy write validation and additive columns
while readers retain old compatibility; then stop or drain every old writer, run
the preflight, remediate invalid rows, and only afterward enable strict readers
and policy-aware scheduling. A maintenance/read-only window or equivalent
database-enforced gate is acceptable. Strict readers must never run while a
legacy writer can create a policy that fails the new contract.

The migration adds both Boolean columns with PostgreSQL server defaults (`true`
and `false`), backfills existing rows, makes them non-null, and keeps matching ORM
defaults. Retain server defaults for rolling-deployment compatibility with an old
application process. Downgrade removes only these columns and explicitly
documents loss of customized Boolean values.

### Unit tests

- default policy reproduces every existing deterministic engine assertion;
- enabled near/medium/far and exact boundary lookup;
- disabled buffers produce zero distance and post-break buffers while retaining
  travel, overlap protection, working bounds, and configured rounding;
- configurable post-break recovery and each supported rounding increment;
- start-of-shift exception off/on, same-facility zero-distance behavior, final
  gap behavior, and return-leg buffer before a following event;
- naive/aware datetime behavior remains covered by existing timezone tests;
- every tier validation rule, including NaN/infinity, duplicates, order,
  overlap, representable gaps, missing catch-all, empty tiers, and atomic
  rejection.
- post-break-versus-distance precedence, return-leg buffers before both lessons
  and breaks, zero-distance outputs, and the virtual-checkpoint distinction;
- custom fractional boundaries and distance normalization using the routing
  layer's exact rounding behavior, including `0.29/0.30`, binary-float artifact
  inputs, the 10,000-mile bound, and oversized finite values returning 422 rather
  than an internal error;
- fall-back returns both eligible repeated-hour instants with distinct offsets,
  either aware instant books its exact UTC value, naive ambiguity is rejected,
  and spring-forward creates no nonexistent candidate.

### Postgres integration tests

- two instructors with different rules receive different availability from the
  same schedule and route estimates, with no cross-instructor leakage;
- no-row defaults match pre-change results and do not persist a row;
- GET, PUT, and command-center snapshot round-trip both new fields;
- toggling buffers, changing post-break recovery, changing rounding, and
  changing a tier alter availability as specified;
- coach, legacy public invite, client-invite, direct booking, recurrence, and
  approval/revalidation paths all consume persisted policy;
- agent confirmation and direct tool booking cannot persist a stale slot after a
  policy change;
- a setting changed after slot presentation causes unsafe stale booking or
  approval to fail while historical appointments and stored offer evidence stay
  unchanged;
- settings changed between search/request creation, request/approval,
  alternative creation/selection/approval, and weekly occurrences produce one
  internally consistent result using the operation's single policy snapshot;
- a controlled update immediately before the policy read is observed, while one
  after the read affects only the next operation;
- concurrent PUTs consisting of one old payload omitting the new Booleans and
  one explicit Boolean update preserve the explicit database value regardless
  of commit ordering;
- invalid PUT is 422, preserves the prior row, and follows the existing error
  envelope; malformed persisted configuration fails closed without leaking it;
- no-row GET/snapshot payment defaults and non-default payment values survive
  buffer edits unchanged;
- each persisted-configuration read surface returns a sanitized 500 and emits
  only the allowlisted structured error fields;
- migration/preflight tests start with representative rows from every shape
  previously accepted by `CoachSchedulerRules`, including a single finite tier;
  rows valid under the new contract preserve effective behavior, while newly
  invalid rows abort deployment with remediation evidence and no rewrite;
- old-shape inserts still work after upgrade; upgrade/downgrade/upgrade behavior
  and downgrade data loss are documented and tested.

### Frontend and contract tests

- unit-test form parsing, save payloads, server-response replacement, pending
  state, keyboard submission, double-submit prevention, accessible field/error
  association, focus management, rejected-value preservation, and validation
  display;
- contract tests send an old payload without the new Booleans, verify preservation
  for an existing row/defaults for a missing row, assert response fields are
  always present, compare mock/live save shapes, and keep settings/snapshot
  fixtures identical in rules shape;
- one Playwright scenario changes settings, refreshes availability, and observes
  a changed result against live FastAPI/Postgres while retaining styling and auth;
- both frontend production builds and existing browser flows pass;
- regenerate and check OpenAPI/fixtures; run the full Python suite with required
  Postgres tests and live-backend smoke tests.

## Acceptance criteria traceability

- **Engine uses persisted rules per instructor:** policy is loaded once per
  operation and injected into every scheduling and appointment-creation entry
  point; multi-instructor integration tests prove isolation.
- **Toggling rules changes availability:** API and live frontend tests exercise
  enabled/disabled, tier, post-break, rounding, and shift-start behavior.
- **Invalid tier ranges or unsupported rounding increments are rejected:**
  centralized whole-document validation returns 422 and writes nothing.
- **Same-facility, near, medium, far, post-break, and start-of-shift are tested:**
  explicit unit and Postgres scenarios cover each case and exact boundaries.

## Non-goals

- Recalculating existing bookings, requests, notification payloads, or calendar
  exports.
- A generic rules engine, strategy registry, settings cache, event sourcing,
  policy history/versioning, PATCH endpoint, or UI for arbitrary tier creation.
- Changing routing-provider selection, travel-time computation, instructor
  working hours, timezone behavior, payment settings, or public API ownership.
- Weakening scheduling conflict checks or deleting deterministic tests.

## Implementation gate

No implementation work may start until two staff-level reviewers independently
inspect this spec for scheduling, persistence, API-compatibility, concurrency,
timezone, frontend, migration, and testing edge cases and both explicitly state
`APPROVE`.

Both staff-level reviewers approved revision 5. Spec approved by both staff
reviewers; ready for implementation only after separate user authorization.

## Review record

Revision 1 received `REQUEST CHANGES` from both staff reviewers. Revision 2
incorporates their requests: discrete tier-lattice semantics; revalidation at
request creation and every appointment-creating path; unchanged payment
behavior; explicit buffer precedence and reported evidence; a dedicated invalid-
policy exception and safe log event; one policy snapshot per recurrence/command;
legacy-row deployment preflight; rolling-safe server defaults; old-client PUT
preservation; instructor-local DST-aware rounding; accessible form behavior; and
proportionate test allocation with one live UI scenario.

Revision 2 also received `REQUEST CHANGES` from both reviewers. Revision 3 makes
omitted-field writes atomic at SQL update time, defines a two-phase deployment
that excludes legacy writers before strict reads, uses exact decimal lattice
arithmetic with bounded tier values, normalizes bounded IDs/labels, and specifies
both DST fall-back folds and spring-forward gaps without naive-time deduplication.

Revision 3 was approved by the maintainability reviewer. The scheduling reviewer
requested one final correction because an offset cannot always be preserved when
rounding crosses a DST transition. Revision 4 defines the result as the earliest
real qualifying instant, preserves a fold only when valid, skips nonexistent
boundaries, and permits the UTC offset to change across a transition.

Revision 4 was approved by the scheduling reviewer. The maintainability reviewer
identified that instant ordering could incorrectly choose a lower repeated local
label during fall-back. Revision 5 computes the ceiling in monotonic local labels
first and resolves the chosen label to a real aware instant afterward; UTC-based
candidate iteration remains separate so both folds can still be offered.

Both reviewers returned `APPROVE` for revision 5 with no remaining findings.
