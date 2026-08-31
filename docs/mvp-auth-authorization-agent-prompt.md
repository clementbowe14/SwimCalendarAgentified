# Staff Engineer Prompt: MVP Authentication and Authorization

You are a staff-level engineer implementing the MVP authentication and authorization flow for `github.com/clementbowe14/SwimCalendarAgentified`.

Start from the latest remote `master`. Keep the work focused, reviewable, and compatible with the current backend/frontend contract style. Do not weaken meaningful tests, do not expose private schedule data through public routes, and do not change scheduling behavior unless it is necessary for authorization correctness.

## Current Repo Context

- Backend coach routes live under `app/routes/coach.py` and already depend on `get_current_coach` from `app/dependencies.py`.
- `get_current_coach` is currently an MVP stub that always resolves `inst_001`; this is the main auth gap.
- Public client invite routes live under `app/routes/public.py` and are intentionally scoped by invite/request tokens. They should remain public and should not require coach login.
- Coach request approval exists on current `master`:
  - `POST /api/coach/lesson-requests/{request_token}/approve`
  - `POST /api/coach/lesson-requests/{request_token}/alternatives`
  - `POST /api/coach/lesson-requests/{request_token}/decline`
- `frontend-instructor-command` is a plain TypeScript/Vite app using `src/main.ts`, `src/api/instructorApi.ts`, and CSS in `src/styles.css`.
- `frontend-client-invite` is a React/Vite public invite flow and should remain token based.

## Frontend Screen Decision

The frontend does need MVP auth UI for the instructor command app.

Add or modify `frontend-instructor-command` screens/states so a coach can:

- See a compact sign-in/locked screen before protected coach data loads when live backend auth is enabled.
- Submit MVP credentials or a configured development secret without raw instructor IDs in normal UI interactions.
- Persist the authenticated session/token in a simple, documented browser storage strategy appropriate for the MVP.
- Recover from `401` or `403` responses by clearing local auth state, showing a session-expired/locked state, and allowing sign-in again.
- Sign out from the existing command-center chrome after authentication.

Preserve the current instructor app styling. Reuse the existing visual language: sidebar/topbar structure, 8px radii, water/teal palette, compact operational layout, `.error-banner`/toast patterns, and existing button/input density. Do not replace the app with a marketing-style landing page. Any new auth surface should feel like part of the existing command center.

The public `frontend-client-invite` app should not gain a login screen. It should continue to work from invite/request tokens only. Update it only if shared API/error handling changes would otherwise break public invite behavior.

## MVP Auth Scope

Implement a pragmatic MVP that is suitable for local development and CI:

- Add backend auth endpoints for coach sign-in, sign-out or session validation as appropriate for the chosen MVP pattern.
- Replace the hardcoded `get_current_coach` behavior with real request-based authentication.
- Keep all `/api/coach/*` routes protected.
- Keep `/health`, `/openapi.json`, and `/api/public/*` routes publicly reachable.
- Return stable JSON API errors for unauthenticated and unauthorized requests, preferably `401` for missing/invalid credentials and `403` for valid credentials without access.
- Ensure authenticated coaches can only access their own instructor data. Do not trust frontend-supplied instructor IDs for authorization.
- Seed or configure one development coach credential for tests without committing real secrets.
- Store password material safely if passwords are persisted. For MVP, environment-configured credentials or seeded test credentials are acceptable if clearly documented and tested.
- Avoid introducing a heavy auth framework unless it meaningfully simplifies this repo. Prefer a small dependency-light implementation using FastAPI dependencies, secure tokens/sessions, and explicit tests.

## Contract and Documentation Requirements

- Update `app/api_schemas.py` with request/response DTOs for auth.
- Update OpenAPI and contract fixtures through `scripts/export_contracts.py`.
- Update contract tests so drift is caught.
- Document required environment variables and local development behavior in `README.md` and/or `docs/frontend-api-alignment.md`.
- Explain in plain terms what changed in any PR description.

## Required Backend Acceptance Criteria

- Unauthenticated requests to every `/api/coach/*` endpoint return a stable `401` JSON error.
- Invalid credentials fail with a stable `401` JSON error.
- A valid coach login/session can access `/api/coach/me`, schedule, command-center snapshot, settings, availability, bookings, and lesson request approval actions.
- A coach cannot access another instructor's protected resources. Add a test with at least two instructors or credentials.
- Public invite endpoints still work without authentication and still do not expose raw private IDs in public responses.
- Existing Postgres-backed migrations and tests still pass.

## Required Frontend Acceptance Criteria

- `frontend-instructor-command` works with the live backend auth flow using `VITE_INSTRUCTOR_API_URL`.
- The app shows a styled locked/sign-in state before protected data loads when no valid session exists.
- Successful sign-in loads the existing command center without changing its layout or visual style.
- Sign-out returns to the locked/sign-in state.
- A `401`/`403` from a protected API call clears the session and shows the session-expired/sign-in state.
- Existing mock-mode behavior remains useful for local frontend development.
- `frontend-client-invite` remains public-token scoped and does not require auth.

## Required Validation

Run and report exact results for:

- Backend unit/API tests.
- Postgres-backed integration tests with skipping disabled where applicable.
- `python scripts/export_contracts.py --check`.
- `npm ci`, unit tests, production build, and Playwright smoke tests for `frontend-instructor-command`.
- `npm ci`, unit tests, production build, and Playwright smoke tests for `frontend-client-invite`.
- Live-backend Playwright tests for the instructor app with auth enabled.
- Public client invite live-backend smoke to prove auth changes did not lock public routes.

## Implementation Guidance

- Keep auth boundaries centralized in dependencies/middleware rather than scattering checks through route handlers.
- Do not authorize using `VITE_INSTRUCTOR_ID`; treat it as display/test input only or remove the need for it in live mode.
- Prefer token/session storage and fetch wrapper behavior that is easy to test.
- Avoid broad frontend rewrites. Add auth state around the current boot/render flow and reuse existing styles.
- Avoid broad schema churn. Add only the DB tables or config necessary for the MVP.
- Add tests before or alongside implementation for auth failures, successful auth, cross-coach isolation, and public-route regression.

## Suggested Work Split

Backend implementation engineer:

- Own `app/dependencies.py`, backend auth schemas/services/routes, optional models/migrations, backend tests, contracts, and docs for backend env vars.
- Do not edit frontend UI except to keep contracts aligned if absolutely necessary.

Frontend implementation engineer:

- Own `frontend-instructor-command` auth UI/state/API client integration and frontend tests.
- Preserve existing styling and do not add auth to `frontend-client-invite` unless needed for regression handling.

Backend validation engineer:

- Review backend auth design for correctness, authorization gaps, public-route regressions, secret handling, and overengineering.
- Run Postgres-backed tests and inspect OpenAPI/fixtures.

Frontend validation engineer:

- Review instructor auth UX for current styling consistency, error/session behavior, accessibility, and live-backend compatibility.
- Run unit/build/Playwright checks for both frontends.
