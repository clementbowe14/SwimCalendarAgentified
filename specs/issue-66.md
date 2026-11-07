# Issue #66: instructor calendar handoff

Status: approved for implementation. Baseline: `master` at `78bd947b`.
Issue: https://github.com/clementbowe14/SwimCalendarAgentified/issues/66

## Goal

After an authenticated instructor directly books a lesson or confirms a client
request, show a clear post-success handoff for Google Calendar and an RFC 5545
`.ics` download. Both actions must describe the exact persisted appointment
instant and duration. Calendar actions are handoffs only: the product must not
claim that an event was added merely because the instructor opened or
downloaded it.

## Scope and existing behavior

The client invite experience already has tested Google URL and ICS serializers.
The instructor API persists direct bookings and request confirmations, but its
mutation responses do not expose calendar artifacts and its UI only displays a
short toast. Request confirmations can persist one or several weekly
occurrences. The instructor frontend currently calls the legacy `approve`
route; this issue moves that interaction to the canonical `confirm` route that
already exists, without otherwise expanding request-management behavior.

Keep the existing client calendar contract and public routes compatible. Do not
add native Google/Apple authorization, direct calendar writes, background sync,
calendar-provider webhooks, or cancellation/update propagation. The `.ics`
file is the Apple Calendar and provider-neutral path.

## Backend contract

Reuse the tested calendar serialization primitives for both public and
instructor handoffs. Generalize the primitive so its download URL, summary, and
description are explicit inputs; retain `Swim lesson` and `Confirmed swim
lesson.` for the existing public client flow. Until a configurable service
label exists, instructor artifacts use the privacy-safe fallback `Appointment`
and `Confirmed appointment.` rather than a client name or hard-coded coaching
language.

Keep booking and confirmation persistence success independent from calendar
artifact generation: `BookLessonResponse.appointment_id` and
`CoachLessonRequestConfirmResponse.appointment_ids` remain authoritative. Add
`calendar_links: ClientInviteCalendarLinks | null` to both responses. A newly
persisted active lesson normally includes links; an idempotent replay whose
appointments are no longer eligible returns the successful mutation with
`calendar_links: null`, never a fabricated link or a misleading mutation
failure. The link object keeps the established shape:

```json
{
  "google": "https://calendar.google.com/calendar/render?...",
  "google_events": [
    {
      "start_iso": "2026-08-29T17:30:00Z",
      "end_iso": "2026-08-29T18:15:00Z",
      "url": "https://calendar.google.com/calendar/render?..."
    }
  ],
  "apple": "/api/coach/appointments/app_persisted/calendar.ics",
  "ics": "/api/coach/appointments/app_persisted/calendar.ics"
}
```

The first persisted appointment ID anchors a handoff. Add authenticated routes:

- `GET /api/coach/appointments/{appointment_id}/calendar-links`
- `GET /api/coach/appointments/{appointment_id}/calendar.ics`

The authenticated coach must own the anchor appointment. For a direct booking,
the handoff contains that one active scheduled lesson. For a request-backed
booking, it contains every active scheduled lesson for the same request in
chronological order, so weekly confirmations expose one Google action per
persisted occurrence and one multi-event ICS file. Any owned request-backed
anchor may locate its request siblings even when the anchor is cancelled,
completed, or otherwise ineligible; the anchor itself is exported only when it
meets the active eligibility rules. Unknown or cross-instructor anchors return
the normal not-found response. No eligible appointments returns
`CALENDAR_LINKS_UNAVAILABLE` (409). A handoff-read failure must never roll back
or misreport the already successful booking/confirmation.

An eligible appointment has the authenticated instructor ID, `event_type`
`lesson`, status `scheduled`, `is_break` false, and no `cancelled_at`. Holds,
blocks, completed lessons, cancelled lessons, and unrelated appointments are
excluded. If all occurrences are unavailable, read/download routes return 409.
Both instructor routes return
`Cache-Control: private, no-store` and `Referrer-Policy: no-referrer`; ICS uses
`text/calendar; charset=utf-8` and a fixed `appointment.ics` attachment name.
Request-backed appointments are eligible only while their parent
`LessonRequestDB.status` is `confirmed`; pending, alternatives-sent, declined,
or cancelled parents export nothing even if a scheduled child row remains.

Direct-booking idempotent retries and already-confirmed request retries return
the same persisted IDs; the subsequent handoff read rebuilds artifacts from
those existing records. Google and ICS timestamps come only from stored UTC
start/end values and preserve stable calendar UIDs.

## Instructor frontend behavior

Replace the direct-booking `Hold` label with `Book lesson`. On a successful
direct booking or request confirmation, render an accessible modal from the
returned persisted handoff instead of relying on a transient success toast. If
the optional links are absent, use the exact returned appointment ID to check
the authenticated handoff route; a 409 opens the unavailable success state.

The modal contains:

- a success heading and concise confirmation message;
- one Google Calendar link for a single appointment, or a dated list for
  multiple persisted occurrences;
- a `Download .ics` button that performs an authenticated fetch before creating
  a local download;
- truthful copy explaining that opening/downloading is a snapshot handoff and
  does not prove the event was saved;
- a prominent `Done` action.

Only HTTPS `calendar.google.com/calendar/*` URLs may open externally. ICS paths
must resolve to the configured instructor API origin and are fetched with the
current bearer session, `no-store`, `no-referrer`, and redirect rejection. Check
the response status and `text/calendar` content type before starting a download.
Unsafe or failed artifacts remain non-navigable and produce an inline alert.

The modal uses `role="dialog"`, `aria-modal="true"`, labelled heading and
description, initial focus, Escape close, contained Tab/Shift+Tab navigation,
and focus restoration to the initiating action when it still exists (otherwise
the nearest stable screen action/heading). Calendar controls remain at least
44px tall and stack without horizontal overflow on a 320px viewport. Closing
the modal never repeats a mutation.

## Failure and state model

`idle -> submitting -> persisted -> loading_handoff -> ready | unavailable -> closed`

- A scheduling/auth/confirmation error follows the existing error banner path
  and never opens the modal.
- A handoff-read failure opens an unavailable success modal with no calendar
  actions; it must not make a persisted lesson look unbooked or invite a second
  booking attempt.
- A 401/403 from the fallback handoff read follows the existing session-expired
  lock flow, with copy stating that the lesson was persisted but calendar
  options could not load before the session expired. It must not be collapsed
  into the ordinary unavailable modal or cause a repeated mutation.
- A successful booking remains successful if a later calendar download fails;
  show the download error inside the modal and permit retry.
- Links are rendered only from a successful artifact response. Refreshing or
  reopening an unavailable/cancelled handoff through the authenticated endpoint
  yields no active actions.
- Do not log bearer tokens, calendar URLs, client names, locations, or ICS
  payloads.

## Implementation slices

### Backend

- Generalize the shared calendar serializer without changing public output.
- Add instructor-owned active-appointment loading and authenticated link/ICS
  services and routes.
- Add the authenticated handoff read/download contract for direct bookings,
  idempotent retries, and recurring confirmations.
- Enrich successful booking and canonical confirmation responses with nullable
  links after re-querying the persisted records; artifact failure must not
  change persistence success.
- Regenerate OpenAPI and add coach calendar contract fixtures.
- Test auth isolation, persisted timestamps/duration, partial/all cancellation,
  ineligible statuses, idempotent retries, and public serializer compatibility.

### Instructor frontend

- Extend API types for calendar links and canonical confirmation.
- Validate Google/ICS destinations and fetch ICS with bearer authentication.
- Add the keyboard/screen-reader/mobile-safe post-success modal.
- Wire both direct booking and request confirmation to their returned links,
  falling back to the exact persisted ID when links are absent; keep persistence
  success distinct from handoff errors and keep duplicate submissions safe.
- Add adapter unit tests and Playwright coverage for both entry points,
  multiple Google occurrences, truthful copy, keyboard close/focus, download
  errors, and mobile overflow/control size.

## Verification

1. Backend unit/integration tests parse Google queries and ICS events and assert
   identical stored UTC instants and durations, including weekly occurrences.
2. Authentication tests prove missing/invalid tokens and another coach cannot
   read or download an appointment handoff.
3. Cancellation tests prove partial series export only remaining occurrences
   and all unavailable occurrences produce 409/no actions.
4. Contract generation/check and the complete Python suite pass.
5. Instructor unit tests, typecheck, production build, and Playwright pass.
6. Browser checks cover direct booking and request confirmation at desktop and
   320px mobile width using keyboard-only operation and no horizontal overflow.

## Review record

Backend review required nullable mutation links, a generic unavailable error,
re-querying after commit, request-confirmed filtering, cross-instructor 404
isolation, and explicit partial-series behavior. Frontend review required the
canonical confirmation route, authenticated ICS fetch, strict URL validation,
an unavailable success state, modal focus containment/restoration, and mobile
coverage. Both reviews emphasized that a calendar failure must never make an
already-persisted lesson appear unbooked. These findings are incorporated;
both reviewers approved this revision before implementation.
