# Frontend API Alignment and Test Skeleton

This note tracks what the two frontend prototypes need from the backend before mocks can be retired.

## Instructor Command

Current API calls wired or expected:

- `GET /api/coach/schedule?date=YYYY-MM-DD` for the Today schedule shell and a partial command snapshot.
- `POST /api/coach/availability` for the Availability Finder.
- `POST /api/coach/bookings` for coach-created bookings, with `idempotency_key` when available.

Needed but not yet covered by the current backend contract:

- Coach command-center snapshot with clients, requests, settings, schedule, open gaps, travel rows, and buffer rows.
- Coach client list/detail endpoint for Client Detail.
- Coach scheduler settings read/write endpoints for Settings/Rules.
- Coach request queue, approve request, and request alternatives endpoints for Request Management.
- Stable slot/hold IDs for coach-created bookings so the frontend does not synthesize IDs from times.

Neglected frontend tests by screen/component:

- `TodaySchedule`: loading, empty day, backend-only schedule rows, computed next lesson, open gaps, and schedule conflict states.
- `AvailabilityFinder`: no client fallback, facility vs home-pool requests, zero safe slots, booking retry with idempotency, and backend validation errors.
- `ClientDetail`: missing client profile, no upcoming lesson, partially loaded schedule-derived client, and contact action states.
- `RequestManagement`: empty queue, approved/needs-alternatives mutations, conflict error, stale request state, and optimistic update rollback.
- `SettingsRules`: backend settings load, validation for invalid buffer tiers, save failure, and disabled state while saving.
- `Layout`: navigation state, pending request count from backend, mobile layout, and keyboard focus order.

## Client Invite

Current API calls wired or expected:

- `GET /api/public/client-invites/{token}` for invite envelope loading.
- `POST /api/public/client-invites/{token}/intake` for client-entered intake details.
- `POST /api/public/client-invites/{token}/slots` for token-scoped availability search.
- `POST /api/public/client-invites/{token}/lesson-requests` for idempotent request creation.
- `GET /api/public/lesson-requests/{request_token}` for request status.
- `GET /api/public/lesson-requests/{request_token}/calendar-links` for calendar handoff.
- `GET /api/public/calendar/{request_token}.ics` for direct ICS download.

Neglected frontend tests by screen/component:

- `InviteLandingScreen`: invalid invite token, expired invite, required client name/address validation, submit failure, and facility/home address switching.
- `AvailabilityPreferencesScreen`: no selected windows, invalid time ranges, duration not allowed by invite, recurring availability filtering, and loading/disabled states.
- `SlotResultsScreen`: zero slots, failed search response, recurrence unavailable slots, keyboard selection, and duplicate submit protection.
- `LessonRequestStatusScreen`: requested, confirmed, needs-new-times, failed refresh, and polling/manual refresh behavior.
- `CalendarHandoffScreen`: missing confirmation, each calendar provider link, unavailable calendar links, and download behavior.
- `FlowProgress` and shared controls: step transitions, focus management, disabled states, and responsive/mobile rendering.

## Test Skeleton

Implemented now:

- Instructor unit contract tests around `HttpInstructorCommandApi` path selection, DTO mapping, and stable error parsing.
- Client invite unit contract tests around public-token path scoping, no raw internal IDs in request bodies, idempotency keys, and stable error parsing.
- Client invite Playwright skeleton for real-backend e2e checks, skipped until `VITE_CLIENT_INVITE_API_URL` is configured.
- Instructor Playwright skeleton for command-center navigation, with live-backend checks skipped until `VITE_INSTRUCTOR_API_URL` is configured.

Still needed:

- Cross-app integration tests that seed Postgres, start FastAPI, start both frontends, and run coach/public workflows against the same data.
- Contract drift tests that compare generated OpenAPI/fixtures against TypeScript adapter assumptions.
