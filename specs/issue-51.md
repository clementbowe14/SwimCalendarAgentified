# Issue 51: Canonical coach confirmation with selected-slot fidelity

## Objective

Make `POST /api/coach/lesson-requests/{request_token}/confirm` the single product confirmation contract and preserve the exact client-selected alternative slot from the coach snapshot, through the instructor UI and HTTP adapter, into the backend confirmation command.

The legacy `/approve` route remains temporarily available only as a documented, deprecated compatibility alias. It must delegate to the canonical confirmation command and adapt the response; it must not maintain a second confirmation implementation or response union.

## Current state

The repository has already completed part of the original issue description:

- the instructor HTTP adapter calls `/confirm`, not `/approve`;
- the canonical endpoint accepts `CoachLessonRequestConfirmRequest`, including optional `slot_id`;
- the canonical response includes `status: "confirmed"`, all appointment IDs, calendar links, and confirmation email-delivery state;
- instructor UI copy already says “Confirm.”

The remaining mismatch is still user-visible and contract-significant:

1. `InstructorCommandApi.confirmRequest` accepts only a request ID, and the HTTP adapter always sends `{}`.
2. The command-center snapshot emits `requested_slot.id` as `slot_{request_token}` instead of the persisted selected offer ID.
3. When a client selects an offered alternative, the backend persists that alternative as `original_slot_offer_id`, but the coach frontend cannot identify or submit it.
4. The backend service still returns `CoachLessonRequestActionResponse | CoachLessonRequestConfirmResponse` and switches behavior through a `legacy_response` flag.
5. Internal live-client coverage still confirms through `/approve`, masking canonical-contract regressions.

## Contract decisions

### Canonical confirmation

`InstructorCommandApi.confirmRequest` becomes:

```ts
confirmRequest(requestId: Id, slotId?: Id): Promise<CoachLessonRequestConfirmResponse>
```

The HTTP adapter posts to `/coach/lesson-requests/{id}/confirm` with:

- `{}` when confirming the request’s original/current non-alternative slot; or
- `{ "slot_id": "<selected-alternative-offer-id>" }` when the client selected one of the request-bound alternatives.

The adapter must never call `/approve`, and it must never discard a supplied slot ID.

### Snapshot slot identity

The coach snapshot’s `requested_slot.id` uses the persisted `original_slot_offer_id` when one exists. A deterministic synthetic ID remains only as a compatibility fallback for legacy seeded rows that have no offer ID.

After a client selects an alternative, that alternative ID is both:

- the snapshot’s `requested_slot.id`; and
- present in the request’s `alternative_slots` collection.

The instructor UI treats that equality as evidence that the current request came from an alternative selection. It includes `slot_id` only in that case. This avoids submitting an initial search-offer ID to the backend’s alternative-only explicit-selection validation.

The UI does not add a new slot picker in this issue. Choosing which alternatives to offer and the client selecting one remain existing workflows.

### Backend response typing

`confirm_coach_lesson_request` returns only `CoachLessonRequestConfirmResponse`. Remove its `legacy_response` switch and the confirmation/action response union.

The confirmation response always has the canonical shape:

- `status: "confirmed"`;
- `appointment_id` plus ordered `appointment_ids`;
- optional calendar links when handoff generation succeeds; and
- optional confirmation email-delivery state.

Requested-slot and selected-alternative confirmation use the same response type.

### Legacy compatibility alias

Retain `POST /api/coach/lesson-requests/{request_token}/approve` because repository tests and compatibility consumers still exercise it. Mark it deprecated in generated OpenAPI.

The alias:

1. calls the canonical confirmation service with an empty confirm request;
2. maps the canonical result to the existing `CoachLessonRequestActionResponse` (`status: "approved"`); and
3. accepts only a truly empty HTTP body; `{}` and any other supplied body return `422 VALIDATION_ERROR` with “This endpoint does not accept a request body.” instead of silently discarding caller intent; and
4. contains no confirmation, persistence, notification, or conflict logic of its own.

No production frontend or live product-flow test may call `/approve` after this change. Dedicated compatibility/auth tests may continue to prove the alias works and remains protected.

The repository has no non-test runtime caller of `/approve`; the remaining references are compatibility/auth tests, notification tests that should move to the canonical route, one client live test that should move to the canonical route, generated OpenAPI, and a stale internal authentication prompt. Issue #2 is closed and this issue does not remove a legacy endpoint, so it is not an implementation dependency.

## Confirmation semantics

### Requested slot

Omitting `slot_id` confirms the request’s current persisted start/end and related scheduling fields. It must not reload or reapply the original search offer. Existing requested-slot behavior, recurrence expansion, scheduling revalidation, appointment persistence, notifications, and calendar handoff remain unchanged.

### Selected alternative

When `slot_id` is present, it must identify an alternative offer bound to the same lesson request, invite, and instructor. Confirmation applies that offer before revalidation and appointment creation.

An explicit alternative must match the request, invite, instructor, and `offer_kind="alternative"`. An unselected alternative remains eligible only while its offer is active. Once the client selects an alternative through the public endpoint, that ID becomes the request’s persisted `original_slot_offer_id`; expiry is waived only for that already-selected alternative, and the request expiration then governs actionability even if the discovery offer’s shorter TTL passes. This prevents the UI from making a valid client selection unconfirmable merely because the instructor responds more than 30 minutes later. The scheduler still performs fresh feasibility and conflict validation at confirmation time.

The persisted appointment occurrence and public request status must use the selected alternative’s start and end, not the original requested occurrence.

`confirmed_slot_offer_id` records only a real request-bound alternative. It remains `null` for original search offers and synthetic fallback IDs. Omitted confirmation after a client selection records the selected alternative ID without reapplying offer fields.

### Replay and conflict

- Repeating confirmation for the same request with the same selected slot is idempotent and returns the same appointment IDs.
- Repeating without `slot_id` remains a successful read of the already-confirmed request, matching current compatibility behavior.
- Replaying an already-confirmed request with a different explicit slot ID returns `LESSON_REQUEST_CONFIRM_CONFLICT` and creates no additional appointments.
- A foreign, expired-and-unselected, or otherwise invalid alternative ID remains rejected and cannot alter the request.

The optional request `idempotency_key` field is not redesigned here; persisted request/slot identity remains the confirmation replay boundary.

### Atomicity and failure classes

Applying a selected alternative, creating all appointment occurrences, and enqueueing confirmation notifications remain one database transaction. A newly unavailable occurrence returns `SCHEDULING_CONFLICT`, rolls back appointments and notifications, leaves the request actionable, and records `SLOT_CONFLICT`. This is distinct from `LESSON_REQUEST_CONFIRM_CONFLICT`, which means an already-confirmed request was replayed with a different explicit slot.

On scheduling conflict, every slot field copied during that confirmation attempt—including start/end, date, travel, buffer, distance, and `original_slot_offer_id`—returns to its pre-confirm value. Only the validation-failure marker may persist through the existing follow-up transaction.

Public alternative selection and coach confirmation must lock the same lesson-request row. Selection after confirmation or appointment creation returns `400 INVALID_SCHEDULING_REQUEST` and cannot reset a confirmed request to pending. A selection-versus-confirmation race must end in one internally consistent terminal state: either selection commits before confirmation and both return `200` for the selected occurrence, or confirmation commits first and the losing selection returns that deterministic `400` error.

Calendar handoff generation remains post-persistence presentation work: a handoff-generation failure returns a successful canonical confirmation with `calendar_links: null` and does not undo the committed appointment.

## Mock adapter parity

The mock adapter accepts the same optional slot ID and returns the canonical response shape.

For an explicit slot ID it must:

- require a matching request-bound alternative;
- use that alternative’s occurrence for generated appointment/calendar data;
- update the mock request’s selected slot before marking it confirmed;
- return the same deterministic result on replay; and
- reject a conflicting explicit slot after confirmation.

This prevents mock mode from hiding a slot-propagation regression that HTTP mode would reject.

## UI behavior

Request cards keep the existing one-handed `Confirm` action and calendar handoff behavior.

For a pending request whose current selected slot is a request-bound alternative, the rendered confirm control carries that offer ID as non-visible action data. The event handler forwards it to `confirmRequest`.

No user-facing “Approve” action or success copy is introduced. Existing internal `approved` snapshot status may remain for backward-compatible display mapping to “Confirmed.”

## Required implementation

### Backend

- expose `original_slot_offer_id` in the internal pending-request projection;
- use it for coach `requested_slot.id`, with the current synthetic fallback only when absent;
- make canonical confirmation service and response helper return only `CoachLessonRequestConfirmResponse`;
- convert `/approve` to a thin mapping alias and mark it deprecated;
- reject non-empty `/approve` bodies so the compatibility path cannot silently drop a slot selection;
- preserve existing confirmation/advisory locking and add lesson-request row locking to public alternative selection;
- keep authorization, tenant scoping, schedule revalidation, notifications, and transaction behavior unchanged apart from that required selection lock;
- regenerate and check in the OpenAPI contract.

### Instructor frontend

- add optional `slotId` to the API interface and both adapters;
- send `slot_id` from the HTTP adapter when supplied and `{}` otherwise;
- detect the currently selected alternative from request data and attach it to the confirm action;
- forward it from the UI event handler;
- preserve calendar handoff and confirmation email-delivery handling.

### Client/live callers

- migrate the client-invite live Playwright coach action from `/approve` to `/confirm`;
- retain public status refresh and calendar verification against the canonical confirmation result.

The production client refresh implementation already replaces the complete server request object and requires no behavior change for this issue.

## Required tests

### Backend API tests

1. Requested-slot confirmation through `/confirm` returns the canonical response and persists the occurrence.
2. Alternative workflow:
   - create at least one request-bound alternative;
   - select it through the public client endpoint;
   - verify the coach snapshot exposes that exact ID as `requested_slot.id`;
   - confirm with that exact `slot_id`;
   - verify the created appointment and public confirmation occurrence match the selected alternative start/end.
3. Replay the same selected slot and assert identical appointment IDs and no duplicate appointments.
4. Cover both replay directions: explicit selected-slot confirmation followed by an omitted replay, and selected-alternative confirmation first performed with omitted `slot_id` followed by replay with that same explicit ID. Both return the original appointment IDs, and the omitted-first path persists the alternative as `confirmed_slot_offer_id`.
5. Replay with a different explicit slot ID and assert `LESSON_REQUEST_CONFIRM_CONFLICT` with no mutation.
6. Expire an already-selected alternative offer while leaving the request active; confirmation must still succeed after fresh schedule validation.
7. Reject expired alternatives that were never selected, plus foreign, search-only, and unbound IDs.
8. Keep a focused `/approve` compatibility test, reject a non-empty compatibility body, and verify the generated operation is deprecated.
9. Prove an alternative that has become unavailable returns `SCHEDULING_CONFLICT`, restores every pre-confirm slot field and `original_slot_offer_id`, creates no appointment or notification, and leaves the request actionable with only `SLOT_CONFLICT` recorded.
10. In a PostgreSQL concurrency test, competing confirmations for different alternatives produce one `200`, one `409 LESSON_REQUEST_CONFIRM_CONFLICT`, and one persisted appointment set.
11. In a PostgreSQL selection-versus-confirmation race test, the shared request lock prevents a confirmed request from being reopened and leaves one consistent persisted occurrence. If confirmation wins, the losing selection returns `400 INVALID_SCHEDULING_REQUEST`; if selection wins, both return `200` and confirmation uses that selection.
12. Confirm a weekly selected alternative and verify ordered coach and public occurrence timestamps.
13. Assert canonical-route security directly: unauthenticated `/confirm` returns `401`; another instructor’s request is indistinguishable from nonexistent and returns `404`; foreign/cross-request alternatives return the same generic invalid-slot response without mutation or identifier disclosure.
14. Keep requested-slot authorization and tenant-scope coverage green.

### Frontend unit and intercepted browser tests

1. HTTP adapter posts an explicit selected slot in the canonical request body.
2. HTTP adapter posts `{}` when no alternative was selected.
3. Mock adapter confirms the selected alternative and returns the canonical response shape.
4. Request rendering includes the slot action data only when the requested slot matches a request-bound alternative.
5. Intercepted authenticated Playwright captures the confirm request and asserts the exact alternative `slot_id`; no `/approve` call occurs.
6. An intercepted `LESSON_REQUEST_CONFIRM_CONFLICT` leaves the request actionable, opens no calendar handoff, and renders the server error without an optimistic success state.
7. Existing calendar handoff and email-delivery behavior remain green.

### Live FastAPI tests

The instructor live suite must confirm a pending request through the UI and independently verify the persisted occurrence through FastAPI. For an alternative-selection setup, it must assert:

- the setup discovers the generated alternative ID from API responses rather than hardcoding its hash-derived value;
- the browser posts the selected alternative ID to `/confirm`;
- the response appointment ID is independently readable from the coach schedule/calendar API; and
- `GET /api/public/lesson-requests/{request_token}` returns `slot.start_iso`/`slot.end_iso` and confirmation calendar-event timestamps matching the selected occurrence. Public and coach appointment identifiers are intentionally different and must not be compared. The legacy `/api/public/requests/{request_token}` shape cannot satisfy this check.

The client-invite live suite must use `/confirm`, then refresh the public request status and verify that its selected slot start/end match the confirmed calendar occurrence. Live browser assertions compare ISO values rather than localized display text and preserve the suite’s existing multi-browser date isolation.

## Documentation and contract

- Document `/confirm` as the product endpoint and `/approve` as a deprecated compatibility alias.
- Update stale internal docs or tests that present `/approve` as the preferred action.
- Regenerate `contracts/openapi.json` and keep contract drift checks green. The existing canonical request/response schemas remain; the expected contract change is deprecation metadata for `/approve` plus any fixture identity correction required by the snapshot projection.

## Acceptance mapping

- Selected alternative reaches backend: real offer ID survives snapshot projection, UI action data, adapter body, and backend request.
- Mock and HTTP shapes match: both return `CoachLessonRequestConfirmResponse` and selected occurrence data.
- No slot is discarded: adapter and browser tests assert the exact JSON body.
- Replay/conflict remain enforced: backend tests prove same-slot idempotency and different-slot rejection without duplicate appointments.
- Canonical contract is singular: canonical service has no legacy response switch/union; compatibility alias only maps its result.
- Client sees the selected occurrence: public status is independently read after confirmation and matched to the selected alternative.

## Non-goals

- removing the `/approve` route in this issue;
- adding an instructor-side alternative picker or redesigning request cards;
- changing how alternative offers are generated or how clients choose them;
- changing recurrence semantics;
- replacing the request lifecycle with a generic state machine;
- redesigning notification delivery, calendar providers, or transaction ownership;
- changing the public token model.
