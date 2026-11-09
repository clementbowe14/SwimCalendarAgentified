# Issue 65: Instructor email delivery state and safe recovery

## Objective

Give an authenticated instructor a truthful, privacy-safe view of invitation and confirmation email delivery, plus safe recovery actions that cannot create duplicate notification rows or duplicate provider sends.

This work closes the product gap in issue #65 while respecting its open dependencies:

- #62 still owns the production email adapter, templates, and worker deployment.
- #13 still owns general client CRUD and the complete client-editing workflow.
- #23 still owns invite creation, listing, expiry, and revocation.

This issue will expose and operate on the notification ledger already present on `master`. It will add only the narrow contact correction needed to recover a specific notification. It will not claim that email can be delivered in an environment where #62 is not configured.

## Product truths

1. `accepted` means the email provider accepted the request. It does not mean the recipient received the email.
2. `delivered` is shown only when the ledger contains `delivered`.
3. Provider credentials, provider message IDs, raw failure text, raw recipient addresses, attempt counts, leases, and retry timestamps never reach the frontend.
4. SMS ledger rows remain backward compatible but are not shown in this email-focused instructor surface.
5. A resend requeues the same ledger row and therefore retains the same provider idempotency key. It never creates a second notification row.
6. An `outcome_unknown` failure is not manually resendable because a non-idempotent provider could have sent the original message.
7. Contact correction is limited to zero-attempt `skipped` events with `invalid_recipient` or `missing_recipient`. An event that may have reached a provider is never reused with a changed payload.
8. Historical delivery rows remain readable when their invite or appointment is inactive, but every recovery action fails closed.

## Provider API

### Privacy-safe delivery model

Add `CoachEmailDeliveryResponse`:

- `id`: opaque notification event identifier used only by authenticated coach actions.
- `kind`: `invite` or `confirmation`.
- `status`: `queued`, `processing`, `accepted`, `delivered`, `failed`, or `skipped`.
- `updated_at`: timezone-aware UTC timestamp.
- `client_id`: optional provider-scoped client identifier.
- `client_name`: display context for the authenticated provider.
- `request_token`: optional public request token for a confirmation. The internal notification/request database IDs never serialize in this field.
- `issue`: one of `invalid_address`, `missing_address`, `temporary_failure`, `permanent_failure`, `delivery_uncertain`, `retries_exhausted`, or null.
- `source_state`: `active`, `invite_inactive`, or `appointment_inactive`.
- `can_resend`: server-authoritative action flag.
- `can_edit_contact`: server-authoritative action flag.

The response must not expose `recipient`, `failure_code`, `provider_message_id`, `template_context`, `attempt_count`, `next_attempt_at`, `lease_expires_at`, or `attempt_token`.

### Read surfaces

- Add `email_deliveries` to `CoachCommandCenterSnapshot`, ordered newest update first.
- Add nullable `email_delivery` to the authenticated invite-send response.
- Add nullable `email_delivery` to the canonical lesson-request confirmation response, including idempotent confirmation replay.
- Add `GET /api/coach/email-deliveries` for an explicit authenticated refresh independent of the command-center payload.

All reads are tenant-scoped to the authenticated instructor. Unknown and cross-tenant identifiers return the same 404 response.

Legacy records with no ledger row return `email_delivery: null` or an empty `email_deliveries` list. Reads and idempotent confirmation replays never create missing notification history. If both intake and request context are absent, the display name falls back to `Client` without exposing another identifier.

### Safe resend

Add `POST /api/coach/email-deliveries/{delivery_id}/resend` with no request body.

- Lock the complete tenant-owned source graph and re-evaluate every condition after all locks are acquired. Both recovery mutations use the same deterministic order: event, invite, intake when present, request when present, request appointments ordered by ID, then linked client when present.
- If the event is `failed`, its failure code is one of `provider_retryable`, `provider_permanent`, or `attempts_exhausted`, and its source is still active, reset it to `queued`, clear stale provider/failure state, reset the attempt cycle, and set it due now.
- Preserve the event ID so the production provider receives the same idempotency key.
- If a replay sees `queued`, `processing`, `accepted`, or `delivered`, return the current representation without creating or requeuing anything.
- `skipped` requires contact correction.
- `outcome_unknown`, an unknown/null failure code, or an inactive source returns a safe 409 and is never manually requeued.
- Concurrent resend calls serialize on the row and produce one queued event.

### Contact correction

Add `PUT /api/coach/email-deliveries/{delivery_id}/contact` with `{ "email": "..." }`.

- Normalize and validate the address server-side.
- Lock, in deterministic order and in one transaction, the tenant-scoped event, invite, intake, optional request, request appointments ordered by ID, and optional client rows. Re-evaluate status, attempt count, failure code, source lifecycle, and ownership after locking and commit all changes atomically.
- Permit correction only for an active-source event with `status=skipped`, `attempt_count=0`, and failure code `invalid_recipient` or `missing_recipient`.
- Require the existing tenant-owned invite intake that originally sourced the event. A missing intake makes the row non-editable; recovery must not fabricate an intake. Invite-only/no-intake rows fail closed.
- Resolve a linked client from `InviteTokenDB.client_id` and/or `LessonRequestDB.client_id`. If both exist they must match. Load the client by `(client_id, instructor_id)` and fail closed on missing or cross-tenant associations.
- Update the event recipient snapshot, its source `InviteIntakeDB.guardian_email`, `LessonRequestDB.client_email` when present, and the tenant-owned `ClientDB.email` when linked.
- Requeue the same event ID, reset its attempt cycle, and clear stale failure/provider fields.
- Repeating the same correction while queued returns the same representation and does not create a second row.
- A different correction after the event leaves the eligible skipped state returns a safe 409.
- The worker and contact mutation acquire the event lock first; worker-vs-correction and concurrent-different-correction races have deterministic tests.

## Source lifecycle eligibility

Action flags and mutations use the same server-side source check:

- An invitation is active only when its tenant-owned invite exists, is not revoked, and has not expired. Otherwise `source_state=invite_inactive`, both action flags are false, and mutations return a safe 409.
- A confirmation is active only when its tenant-owned lesson request remains `confirmed` and its locked appointment rows exactly match the notification snapshot: identical occurrence count and sorted start/end instant set, every row is a scheduled, non-cancelled, non-break lesson, and every row retains the snapshotted location address. Missing, extra, cancelled, completed, relocated, or rescheduled/stale occurrences produce `source_state=appointment_inactive`, both action flags are false, and mutations return a safe 409.
- Provider projections show inactive historical state but never repair, recreate, or mutate source lifecycle records.

### Recovery eligibility matrix

| Ledger/source condition | Resend | Edit contact |
| --- | --- | --- |
| failed + known failure + active source | yes | no |
| failed + `outcome_unknown`/unknown failure | no | no |
| skipped + invalid/missing + zero attempts + active source + existing intake | no | yes |
| skipped + missing intake | no | no |
| queued/processing/accepted/delivered | no-op replay/current state | no |
| any inactive source | no | no |

## Failure-code projection

Internal failure codes are projected to a stable, privacy-safe enum:

| Internal value | Coach API issue |
| --- | --- |
| `invalid_recipient` | `invalid_address` |
| `missing_recipient` | `missing_address` |
| `provider_retryable` | `temporary_failure` |
| `provider_permanent` | `permanent_failure` |
| `outcome_unknown` | `delivery_uncertain` |
| `attempts_exhausted` | `retries_exhausted` |
| unknown/null | null |

Unknown internal values must project to null, not leak through.

## Instructor UI

Add an **Email delivery** section to Request management.

Every state has distinct language:

- queued without `delivery_uncertain`: `Queued to send`
- queued + `delivery_uncertain`: `Delivery status uncertain`, with copy that it may already have sent and an automatic retry is queued
- processing: `Sending`
- accepted: `Accepted by email service` with supporting copy that delivery is not confirmed
- delivered: `Delivered`
- failed + `delivery_uncertain`: `Delivery status uncertain`, with copy that it may have sent and must not be resent
- other failed: `Delivery failed`
- skipped: `Not sent`

The section distinguishes invitations from confirmations and identifies the client without displaying a raw email address.

Recovery behavior:

- Render `Resend safely` only when `can_resend` is true.
- Render an inline, labeled email correction form only when `can_edit_contact` is true.
- Show `Invite link is no longer active` for `source_state=invite_inactive`; do not fabricate #23 invite-management controls. Show similarly truthful appointment-inactive copy.
- Provide a one-tap `Refresh delivery status` action backed by the dedicated read endpoint.
- Merge refresh and mutation results by delivery ID and `updated_at`; a stale response cannot overwrite a newer local delivery. Failed refreshes retain every last-known card.
- Keep per-delivery email draft, pending, result, and error state outside the replaced DOM. Mark the active card `aria-busy`, associate field errors with the input, use card-local live regions, restore focus to the initiating control after success, and focus the email input after validation failure.
- Use visible focus, text in addition to color, and controls at least 44 by 44 CSS pixels at 320px width.
- Disable only the active delivery card while its mutation is pending.
- Handle validation, stale-state conflict, uncertain outcome, inactive source, network failure, and authentication with safe card-level copy; authentication still follows the global locked-session flow.
- A resend success says `Queued to send again`, never `Sent`. Contact success says `Email updated and queued`.
- Patch the returned delivery into local state before any broader refresh. If a later refresh fails, keep the last known delivery state and show a non-destructive warning.
- Confirmation mutation responses are patched into local state immediately so a successful booking does not lose its queued/skipped delivery result when schedule refresh fails.

## Backend implementation slice

- `app/api_schemas.py`: coach delivery and contact-update contracts.
- `app/notifications.py`: privacy projection, tenant-scoped list/read, safe resend, and contact correction.
- `app/routes/coach.py`: authenticated read/resend/contact endpoints and enriched invite response.
- `app/api_services.py`: snapshot and confirmation response enrichment.
- contract export/fixtures and backend tests.

## Frontend implementation slice

- instructor API types and mock/live adapters.
- request-management delivery cards and recovery controls.
- app state/event wiring that preserves last-known results.
- unit, adapter, and Playwright coverage for every state and both recovery actions.

## Required tests

### API and persistence

- all six ledger states project correctly for email and omit every private/internal field;
- SMS rows are excluded from the coach email surface;
- invite send and confirmation responses contain their email delivery state;
- idempotent confirmation replay returns the current state;
- unknown and cross-tenant IDs return indistinguishable 404 responses;
- failed known outcome requeues the same row and event ID;
- repeat and concurrent resend do not create additional rows or extra queued mutations;
- `outcome_unknown`, unknown failure, skipped, accepted, and delivered states cannot be unsafely resent;
- expired/revoked invites and stale/cancelled/rescheduled confirmations disable actions and reject mutations;
- resend and contact correction lock and revalidate the full invite/request/appointment graph against revocation, exact occurrence instants, and location address;
- contact correction validates, locks all source rows, verifies tenant linkage, updates authoritative contact records, and requeues the same row;
- missing-intake rows remain non-editable and no recovery read/mutation fabricates source data;
- repeat contact correction is idempotent;
- worker-vs-correction, concurrent resend, concurrent same correction, and concurrent different correction races are covered;
- legacy missing-ledger reads/replays remain empty/null and never create history;
- the API projects `request_token`, never the internal request ID;
- internal failure/provider data never serializes.

### Components and browser

- every status renders distinct text;
- accepted explicitly says delivery is not confirmed;
- uncertain outcome says it may have sent and exposes no manual resend whether its ledger status is queued or failed;
- failed safe delivery renders resend and updates to queued;
- invalid/missing address renders the email editor and updates to queued;
- inactive invite/appointment copy is truthful and exposes no action;
- delivery actions preserve drafts/focus, remain keyboard and screen-reader accessible, and are usable at 320px;
- refresh merges monotonically and cannot overwrite a newer mutation result;
- a refresh/action failure preserves the previous delivery cards and exposes a safe error;
- authentication and tenant protections remain intact.

## Acceptance mapping

- Distinguish every state: privacy-safe model plus delivery cards.
- Acceptance is not delivery: separate label and explanatory text.
- Safe resend: same-row requeue with outcome-unknown guard and concurrency tests.
- Invalid email recovery: notification-scoped contact editor updates authoritative records.
- No duplicate messages: stable event ID/provider key and no new ledger rows.
- No internal leakage: allowlisted projection and response-shape tests.
- Coverage: API, component, adapter, and browser suites.

## Non-goals

- production provider credentials or transport (#62);
- worker deployment or delivery webhooks (#62);
- general client create/list/edit/remove (#13);
- invite creation/list/revoke UI (#23);
- SMS resend or SMS delivery UI;
- notification analytics, bulk resend, or marketing email.
