# Issue #25: notifications and calendar handoff

Approved revision 3, recovered from the task's recorded specification and reviews.
Both staff reviewers (Bohr and Confucius) approved the spec; the user authorized implementation.
Baseline: master 4129bc11. Issue: https://github.com/clementbowe14/SwimCalendarAgentified/issues/25

## Scope and existing behavior

Complete per-channel notification records, a provider-neutral service, safe public delivery status and calendar handoff. Existing master has request-level queued flags, Google/ICS links and UTC timestamps, but no delivery ledger; client buttons and sent/imported claims are mocked. Preserve scheduling rules and frontend styling. Real SMS/email providers, credentials, background workers, webhooks, contact-capture UI, subscriptions and cancellation distribution are follow-ups. Never report a fake send or calendar import as real.

## Persistence and notification contracts

Add notification_events with opaque UUID id; instructor/invite/request ownership; kind invite/confirmation; channel sms/email; logical key; six-state status; validated recipient snapshot; template version/context; attempt count; next attempt, lease expiry, attempt token; safe internal failure code/provider message id; aware UTC created/updated times. Enforce per-channel uniqueness, composite tenant foreign keys, status/kind checks and indexes. Both channels get rows; missing/invalid contacts become skipped. Resolve only that invite's persisted intake, never another client's details. SMS requires E.164 without guessed country; standard email validation without DNS delivery probing. No private notes, credentials or provider exceptions in context/public data.

Confirmation and legacy approval atomically commit appointments, confirmed state and two notification rows. Weekly confirmation has one message per channel listing all occurrences. Lock/retry behavior must prevent duplicates and orphan events; already-confirmed legacy requests get no synthetic historical records on retry. No events from reads/seeding or failed booking validation.

POST /api/coach/invites/{invite_token}/notifications (authenticated) requires Idempotency-Key, accepts no recipient override, returns 202 with notifications. Validate ownership/nonrevocation/expiry before replay. Lock instructor row; logical key derives from instructor+operation+client key, excludes invite. Atomically create both rows; same key/different invite conflicts with 409 IDEMPOTENCY_KEY_CONFLICT. Different instructors may reuse keys. Same-key retry retains snapshots despite intake changes; new key deliberately resends with updated contacts. No intake still yields two skipped rows. Initial invites before intake require future contact capture.

## Provider processing

Protocol send(message, idempotency_key) returns accepted, delivered, retryable_failure, permanent_failure or unknown. Default unconfigured service makes no calls/attempts, leaves queued. Deterministic fakes are test-only. States: queued, processing, accepted, delivered, failed, skipped. accepted is terminal for this MVP and does not mean delivered.

Claim a due queued event under row lock, increment attempt count durably before I/O, set random attempt token and 120-second lease. Adapter timeout <=30 seconds. Release DB transaction before provider call; stable event UUID is external idempotency key. Maximum five attempts; backoff 60/120/240/480 seconds. Permanent failure terminates; retryable definite failure retries; unknown outcome/expired lease retries only when provider supports idempotent send, otherwise fails with internal outcome_unknown. A crash before I/O consumes the claim; fifth-attempt expired lease fails without sixth send. Stale attempt tokens cannot overwrite reclaimed results. No daemon or public dispatch endpoint.

## Public responses and access

Both /api/public/requests/{token} and /api/public/lesson-requests/{token} and shared create DTOs expose notifications: [{kind,channel,status,updated_at}]. Confirmation entries only, stable sms/email order; legacy empty []. Never expose recipient/provider/event id/attempts/errors. Revoked parent invites and unknown request tokens yield existing not-found errors consistently on status/calendar routes. Expired invite does not invalidate confirmed-request calendar access. Export only confirmed requests' active scheduled lesson appointments, not holds/blocks/cancelled/other requests. No eligible appointments => CALENDAR_LINKS_UNAVAILABLE 409; no links in status. Cache-Control private,no-store and Referrer-Policy no-referrer.

## Calendar payloads

Use maintained icalendar serializer: RFC5545 CRLF, UTF-8 75-octet folding, TEXT escaping. VEVENT: random stable opaque UID independent of bearer/internal ids, UTC DTSTAMP from persisted creation, UTC DTSTART/exclusive DTEND from stored instants, SUMMARY Swim lesson, LOCATION appointment address, DESCRIPTION Confirmed swim lesson. No intake/private notes/contacts. Add/backfill unique calendar UID on appointments. Do not recalculate instants using instructor's current timezone. Reject invalid duration with sanitized error. Download text/calendar;charset=utf-8, fixed attachment filename swim-lessons.ics.

Google URLs use structured query encoding with matching safe title, dates, location, details. Keep google/apple/ics strings; add google_events [{start_iso,end_iso,url}] for every active occurrence. Legacy google points to first chronological occurrence; ICS contains all. No RRULE: persisted occurrences are authoritative. Calendar changes are snapshots, not automatic updates of previously imported events.

## Frontend

Replace selection-only buttons with real trusted Google navigation and Apple/ICS downloads; resolve API-relative paths against configured backend origin without duplicate /api. Only trusted backend downloads and Google HTTPS calendar host. Check HTTP errors before download; retain style/accessibility. Weekly Google action exposes dated occurrence links. Never claim imported merely because user clicked. Replace hardcoded sent claims with all six API states, no claim for empty history. Manual refresh remains available after confirmation and on handoff; failure retains last known status with error. HTML document no-referrer plus external noreferrer/noopener; bearer token must not leak in Referer. No polling infrastructure.

## Migration and verification

Add migration after actual head with constraints/indexes/backfill. No synthetic delivery history or historical enqueue. Keep old request flags for compatibility, no longer a delivery source. Document destructive downgrade. Regenerate OpenAPI/fixtures, update DTOs and explicit demo data.

AC1: Postgres tests for confirmation/approval, single/weekly, no intake/one invalid/both invalid, duplicates/concurrency/rollback/no events on failed validation or reads, legacy confirmations and tenant integrity.
AC2: Both public routes/all six states/empty history/order/recursive privacy; revoke/unknown/expired-confirmed access; invite key races, different invite conflict, instructor isolation, intake edit replay and rollback between channels.
AC3: Parse ICS and Google queries; validate fields, UIDs, timestamps, Unicode/fold boundaries, delimiter/newline/property/URL/header injection, long addresses, no private notes/tokens. Real browser clicks, cross-origin backend, downloads/errors/referrers, weekly links, no false success. Partial cancellation preserves remaining UIDs; all cancelled returns 409.
AC4: Both formats match stored UTC through spring-forward, both fall-back folds, midnight/leap day/half-quarter-hour offsets and post-booking timezone changes. Check durations.
AC5: Migration old data with lessons/holds/blocks and new inserts, no synthetic history; provider tests for transient/permanent/unknown/fifth attempt, before-I/O/after-acceptance crashes, leases, future due times, stale completion, concurrent claims, unconfigured zero attempts. Required-Postgres tests, full Python suite, contract check, frontend unit/build and live-backend Playwright. Report real-provider/manual-import checks separately.

## Review record

Reviewer 1 requested tenant-scoped idempotency, explicit missing recipients and durable retry accounting; incorporated and approved revision 3. Reviewer 2 requested real browser actions, truthful status, post-confirmation refresh, document referrer policy and expanded migration/calendar tests; incorporated and approved revision 3. Implementation review and test results are tracked separately.
