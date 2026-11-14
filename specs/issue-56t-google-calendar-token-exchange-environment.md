# Issue #56t: Google Calendar token-exchange environment configuration

## Goal

Load the confidential Google OAuth client secret only when a later callback explicitly needs to
exchange an authorization code. This slice composes the public authorization configuration and the
sealed exchange configuration introduced by earlier issue #56 slices. It adds no HTTP adapter,
route, cookie, startup activation, database access, credential persistence, OpenAPI contract, or UI.

## Environment contract

`google_calendar_token_exchange_configuration_from_env()` reads these exact required values on
every call:

```text
SWIM_GOOGLE_CALENDAR_OAUTH_CLIENT_ID=<web-client-id>.apps.googleusercontent.com
SWIM_GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET=<confidential-client-secret>
SWIM_GOOGLE_CALENDAR_OAUTH_REDIRECT_URI=https://api.example.com/api/oauth/google-calendar/callback
```

The factory returns a fresh `GoogleCalendarTokenExchangeConfiguration`. It does not cache values,
provide development or test defaults, add configuration to `ApplicationSettings`, retain it in
application state, or expose it through readiness output.

The existing authorization-only factory remains independent of the client secret. Building an
authorization URL therefore does not read or retain confidential exchange material.

## Validation and confidentiality

Client-ID and redirect validation remains owned by `GoogleCalendarOAuthConfiguration`. Client-secret
validation remains owned by `GoogleCalendarTokenExchangeConfiguration`: the value must be an exact
built-in string, nonblank visible ASCII without trimming or normalization, and no larger than 4 KiB.

All missing, malformed, or unexpected loader/constructor failures become the existing fixed
`GoogleCalendarTokenExchangeError`. Environment reads and construction occur only in private helper
frames that are absent from the exported traceback. A dedicated raiser clears cause and context,
including a secret-bearing exception already active in the caller.

The fixed exchange error lives in the lightweight, standard-library-only OAuth core and remains
re-exported by the exchange module. Sanitizing a dependency import failure therefore does not retry
the dependency-heavy token-module import that failed.

The client ID, redirect URI, client secret, dependency exception text, and validation detail never
appear in error messages or representations, formatted tracebacks, cause/context chains,
`app/config.py` traceback-frame locals, application state, or logs. The returned sealed value keeps
the non-serialization and redacted-representation guarantees from issue #56s.

## Runtime lifecycle

Environment values are read at call time so deployment-secret rotation and recovery do not require
process-global cache invalidation. Invalid-to-valid and valid-to-invalid transitions are observed on
the next call. Application import, `create_app()`, and lifespan startup do not invoke this factory
or require any Google Calendar OAuth variable.

## Verification

Pure tests cover the exact environment name, successful behavioral use through a fake exchange
transport, live rereads and failure recovery, the 4 KiB boundary, malformed and wrong-type secrets,
authorization-configuration failure translation, unexpected dependency failures, ambient exception
context, secret-free diagnostics and logs, authorization-only independence, and unchanged startup
when the secret is absent, malformed, or valid.

The existing authorization configuration suite is updated only to recognize the newly approved
secret environment name. Requirements, database schema, routes, and API contracts remain unchanged.

## Deferred slices

1. Add a bounded, non-redirecting, non-retrying concrete HTTPX token transport.
2. Add start/callback orchestration, commit-before-network handling, and cookie cleanup.
3. Encrypt and atomically replace the Google connection in a fresh transaction.
4. Refresh credentials and query normalized opaque FreeBusy intervals.
5. Integrate fail-closed external busy periods and expose settings/recovery behavior.

## Non-goals

- HTTP clients, network calls, proxies, TLS, redirects, retries, streaming, or response limits.
- Startup/readiness activation, feature flags, global caches, or application-state storage.
- Routes, cookies, callback parsing, authentication, OpenAPI, or frontend behavior.
- OAuth-state consumption, transactions, credential encryption, or persistence.
- Token refresh, revocation, FreeBusy requests, calendar writes, or other providers.
