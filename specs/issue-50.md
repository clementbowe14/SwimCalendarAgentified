# Issue 50: Explicit frontend data mode and fail-closed production configuration

## Objective

Prevent either frontend from silently becoming a plausible mock application when API configuration is absent. Mock data must be an explicit opt-in, while every other startup and production build uses the HTTP adapter or fails with a clear configuration error.

This issue applies symmetrically to:

- `frontend-instructor-command`
- `frontend-client-invite`

It preserves both mock adapters for deterministic UI work and keeps the two frontend implementations separate.

## Current failure mode

Both factories currently select their mock adapter whenever the corresponding API URL is empty. Their Playwright suites also infer mock versus live behavior from API URL presence, and CI production builds provide no API configuration. Consequently, a deployable build can succeed and render realistic mock records without an operator intentionally selecting mock mode.

## Configuration contract

Each app uses the same behavior without introducing a shared monorepo package:

| Command | `VITE_USE_MOCK_API` | App API URL | Result |
| --- | --- | --- | --- |
| serve/test | exact string `true` | absent | mock adapter |
| serve/test | exact string `true` | present | configuration error for contradictory settings |
| serve/build/test | absent, empty, or exact string `false` | non-blank | HTTP adapter |
| serve/build/test | absent, empty, or exact string `false` | absent or blank | configuration error before serve/build |
| build | exact string `true` | any | configuration error; deployable mock artifacts are prohibited |
| any | any other flag value | any | configuration error before serve/build |

App-specific API URL keys remain:

- instructor: `VITE_INSTRUCTOR_API_URL`
- client invite: `VITE_CLIENT_INVITE_API_URL`

Rules:

1. Only the lower-case string `true` opts into mock behavior.
2. `false` is accepted as an explicit HTTP selection; omission also defaults to HTTP.
3. Values are trimmed before evaluating blank API URLs, but flag values are not case-folded or coerced. Values such as `1`, `yes`, and `TRUE` are rejected.
4. Mock mode and an API URL together are rejected rather than resolving an ambiguous configuration. Mock test processes must explicitly remove or blank the app API URL.
5. HTTP base URLs continue to support the forms already accepted by the adapters, including same-origin root-relative paths such as `/api` and absolute local or deployed URLs. This issue validates presence, not deployment URL policy.
6. No network failure, authentication failure, malformed response, or runtime exception may switch an HTTP-configured app to mock data.
7. Mock mode is for local development and deterministic tests only. `vite build` rejects it so a deployable artifact cannot select or execute the mock adapter. Mock classes and fixtures may remain in source and are not subject to bundle-content scanning.

## Shared behavior within each app

Each frontend gets a small, pure data-mode resolver local to that frontend. It returns an immutable result containing:

- `mode`: `mock` or `http`
- a normalized non-blank API base URL only for HTTP mode

The same resolver is used by:

- the Vite config, so invalid or missing configuration stops both `vite dev` and `vite build`; and
- the runtime API factory, as defense in depth and to make adapter selection unit-testable.

Configuration errors must:

- identify the affected application;
- name `VITE_USE_MOCK_API` when the flag is invalid;
- name the app-specific API URL when HTTP mode lacks one;
- avoid printing credentials, tokens, or the configured URL value.

In development, each app exposes the resolved mode as `data-data-mode="mock|http"` on the document root. Mock mode renders a compact visible `Mock data` badge; HTTP mode never renders that badge. These tested DOM signals are the sole observability contract, avoiding terminal-output assertions and ensuring the mode is visible where the app is being exercised. Neither signal contains the API URL, tokens, credentials, or other configuration values.

## Runtime factory behavior

The adapter factories accept an optional environment-shaped input for unit tests and otherwise use `import.meta.env` in the browser.

- Explicit mock configuration returns the existing mock adapter.
- HTTP configuration returns the existing HTTP adapter with the normalized API URL.
- Invalid configuration throws instead of returning an adapter.

`frontend-instructor-command/src/api/instructorApi.ts` must remain safe to import in Node unit tests. Move creation of the browser singleton into `main.ts` (or an equivalent runtime-only module) so importing adapter classes does not evaluate browser environment configuration as a side effect.

The client app may continue creating its adapter at application-module startup because its adapter module itself has no eager singleton.

## Client HTTP bootstrap safety

Selecting the HTTP adapter is insufficient if the page still initializes from realistic fixtures. The client app must distinguish bootstrap state by resolved mode:

- Mock mode may initialize from `inviteEnvelope` and the seeded mock token.
- HTTP mode starts with no envelope, intake, client identity, business identity, availability, or controls sourced from fixtures.
- HTTP mode shows a neutral accessible loading state until the invite request succeeds.
- While that request is deliberately delayed, the loading state contains no mock identity, business, contact, location, availability, or enabled workflow controls.
- An initial HTTP failure shows a blocking error and retry action. It must not reveal `Sofia Martinez`, the mock instructor/business, seeded contact details, mock location, or enabled intake/submission controls.
- HTTP mode may read an invite token from the URL or an explicitly configured `VITE_CLIENT_INVITE_TOKEN`, but it never falls back to the seeded mock token. A missing token produces a non-sensitive invalid-link state without making an invite request.
- Retrying bootstrap reuses the same explicitly supplied token and remains in HTTP mode.

This is a fail-closed presentation change only; canonical request-status deep links remain owned by issue #61.

## Vite and package-script behavior

### Development

Document two deliberate commands per app:

- mock UI development: `VITE_USE_MOCK_API=true npm run dev`
- HTTP development: app API URL plus `npm run dev` (the mock flag may be omitted or set to `false`)

Running `npm run dev` with neither explicit mock mode nor the required API URL fails immediately and tells the developer how to select a mode.

### Production builds

Running `npm run build` with neither explicit mock mode nor the required API URL fails at the Vite configuration phase. A build with `VITE_USE_MOCK_API=true` also fails. CI performs the real production build with the app-specific HTTP API URL configured, proving the deployed artifact always uses the HTTP adapter.

Type checking remains independent of runtime data-mode selection.

## Playwright mode selection

All browser servers select their mode explicitly, and spec inclusion follows the explicit flag rather than inferring application mode from API URL presence:

- instructor mock smoke: `VITE_USE_MOCK_API=true`
- client invite mock smoke: `VITE_USE_MOCK_API=true`
- intercepted HTTP/auth suites: `VITE_USE_MOCK_API=false` plus their root-relative or absolute API URL
- live FastAPI suites: `VITE_USE_MOCK_API=false` plus the live local API URL

The client calendar-handoff suite that intercepts `http://127.0.0.1:3109/api/public` is an HTTP-adapter test, not a mock-adapter test, and remains configured accordingly.

Live-only suites additionally require the live URL, and a command named `test:e2e:live` must set HTTP mode and fail if that URL is absent. Every mode-specific command selects concrete test files/projects and must execute at least one test. Environment-conditional `test.skip`/`describe.skip` gating is removed so a wrong mode cannot pass with an entirely skipped suite.

## CI contract

The frontend CI job must prove all of the following:

1. An instructor production build with both data-mode variables absent fails.
2. A client-invite production build with both data-mode variables absent fails.
3. Production builds with `VITE_USE_MOCK_API=true` fail for both apps.
4. Both production builds succeed with their HTTP API URL configured and without mock opt-in.
5. Both mock Playwright suites run with `VITE_USE_MOCK_API=true` and no app API URL.
6. Intercepted instructor authentication/HTTP tests run in HTTP mode.
7. Live FastAPI Playwright suites for both frontends run in HTTP mode.
8. The instructor live suite captures the appointment ID returned by a successful booking or confirmation, then independently reads schedule or appointment/calendar data from FastAPI and asserts that exact ID. A mutation response or rendered success modal alone is not persistence proof.
9. The client live suite captures the public request token returned by lesson-request creation, then independently reads request status from FastAPI and asserts the same token. The existing write path must remain enabled.

The negative build checks must treat an unexpected successful build as CI failure and must unset both the mock flag and app API URL explicitly so runner-level variables cannot mask the result.

## Documentation

Update the root README and both frontend READMEs to remove all statements that mock mode is the default. Document:

- explicit mock startup;
- HTTP startup;
- fail-closed behavior when neither mode is configured;
- production build requirements;
- the fact that mock adapters are for local/test use and never a runtime fallback.

Add a checked-in `.env.example` under each frontend with HTTP-mode local defaults and comments showing the explicit mock alternative. The examples contain no credentials or invite tokens.

## Required tests

### Resolver and factory tests for each frontend

- `true` with no API URL selects the mock adapter;
- `true` with an API URL is rejected as contradictory;
- missing/empty/`false` plus a non-blank URL selects the HTTP adapter;
- missing/empty/`false` plus a missing or whitespace-only URL throws and names the required URL variable;
- invalid flag values throw and name `VITE_USE_MOCK_API`;
- API URLs are trimmed before constructing the HTTP adapter;
- importing instructor adapter classes in Node does not require browser configuration;
- no factory path implicitly falls back to mock.

### Build and browser tests

- CI exercises the actual negative `vite build` path for both apps;
- CI proves mock-mode production builds are rejected;
- configured production builds pass;
- mock UI smoke tests still render deterministic data;
- intercepted HTTP suites still use their API routes rather than mock state;
- mock browser pages expose the development badge and `data-data-mode="mock"`;
- intercepted/live HTTP pages expose `data-data-mode="http"` and no mock badge;
- a deliberately delayed client HTTP bootstrap remains neutral and fixture-free until the response succeeds;
- client HTTP bootstrap failure and missing-token tests expose no mock fixture values or enabled workflow controls;
- existing live instructor and client write-and-read-back flows pass against FastAPI.

## Acceptance mapping

- Production cannot select mocks: Vite rejects mock builds and fails without HTTP configuration.
- Local mock workflow remains easy: one documented environment-prefixed command.
- Mode is observable: tested root DOM mode attribute and visible development-only mock badge.
- CI covers both modes: explicit mock smoke suites plus configured HTTP builds and live suites.
- Instructor live write exists: retain and run direct booking and/or request confirmation against FastAPI.

## Implementation slice

- pure data-mode resolver in each frontend;
- adapter factory wiring and instructor runtime singleton placement;
- client HTTP loading, missing-token, failure, and retry bootstrap states;
- both Vite configs;
- typed Vite environment declarations and per-app `.env.example` files;
- Playwright configs and package scripts as needed for explicit mode selection;
- adapter/configuration unit tests;
- frontend CI build and browser environment configuration;
- root and frontend documentation.

## Non-goals

- removing either mock adapter;
- merging the frontend frameworks or creating a shared package only for environment parsing;
- changing API paths, authentication, scheduling behavior, or backend data contracts;
- adding a runtime toggle after application startup;
- falling back to mocks after an HTTP error;
- validating deployment host allowlists or CORS policy.
