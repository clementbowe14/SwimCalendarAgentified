# Frontend API Alignment and Test Skeleton

This note tracks what the two frontend prototypes need from the backend before mocks can be retired.

## Instructor Command

Current API calls wired or expected:

- `POST /api/auth/coach/sign-in` for provider login. The response includes `authenticated`,
  `expires_at`, and a coach display object; the opaque credential is set only in `swim_session`.
- `GET /api/auth/coach/session` for validating the HttpOnly cookie before protected bootstrap.
- `POST /api/auth/coach/sign-out` for revoking the current database session and clearing the cookie.
- `GET /api/coach/schedule?date=YYYY-MM-DD` for the Today schedule shell and a partial command snapshot.
- `POST /api/coach/availability` for the Availability Finder.
- `POST /api/coach/bookings` for coach-created bookings, with `idempotency_key` when available.
- `POST /api/coach/lesson-requests/{request_token}/confirm` for canonical request confirmation. Send an
  optional request-bound alternative `slot_id` when the client selected that alternative.
- `POST /api/coach/lesson-requests/{request_token}/approve` is retained only as a deprecated, bodyless
  compatibility alias; first-party frontend code and live product-flow tests use `/confirm`.

All auth and coach requests use `credentials: "include"`; unsafe methods also send
`X-CSRF-Protection: 1`. The frontend never creates an `Authorization` header and never stores a session
credential. On every load it removes the obsolete local-storage key, calls `/session`, and waits for a
successful response before requesting or rendering protected data. Only `401` is an expired/invalid
session signal; `403` remains a permission or CSRF error. The backend derives the provider tenant from
the validated account, never `VITE_INSTRUCTOR_ID` or request data.

Needed but not yet covered by the current backend contract:

- Coach client list/detail endpoint for Client Detail.
- Stable slot/hold IDs for coach-created bookings so the frontend does not synthesize IDs from times.

Neglected frontend tests by screen/component:

- `TodaySchedule`: loading, empty day, backend-only schedule rows, computed next lesson, open gaps, and schedule conflict states.
- `AvailabilityFinder`: no client fallback, facility vs home-pool requests, zero safe slots, booking retry with idempotency, and backend validation errors.
- `ClientDetail`: missing client profile, no upcoming lesson, partially loaded schedule-derived client, and contact action states.
- `RequestManagement`: empty queue, confirmed/needs-alternatives mutations, conflict error, stale request state, and optimistic update rollback.
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

Every public-client request uses `credentials: "omit"`. Public routes have no provider-cookie or CSRF
requirement, including when both frontends share an origin.

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
- Client invite Playwright suites with explicit mock, intercepted-HTTP, and live FastAPI modes. Live commands require `VITE_CLIENT_INVITE_API_URL` and fail closed when it is absent.
- Instructor Playwright suites with explicit mock, intercepted cookie-auth, and live FastAPI modes.
  Live commands require `VITE_INSTRUCTOR_API_URL` and fail closed when it is absent.
- Live confirmation coverage carries an API-issued selected-alternative ID through the instructor UI to
  `/confirm`, then compares coach schedule/calendar timestamps with
  `GET /api/public/lesson-requests/{request_token}`. The client live suite also confirms through
  `/confirm` in an isolated cookie context and compares its selected occurrence with refreshed
  confirmation events. Instructor live coverage also checks cookie flags, no browser credential
  storage, session-first reload ordering, sign-out revocation, and captured-cookie replay failure.

Still needed:

- Cross-app integration tests that seed Postgres, start FastAPI, start both frontends, and run coach/public workflows against the same data.
- Contract drift tests that compare generated OpenAPI/fixtures against TypeScript adapter assumptions.
