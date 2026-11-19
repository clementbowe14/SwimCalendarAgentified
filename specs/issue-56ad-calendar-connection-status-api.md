# Issue #56ad: Google Calendar connection status API

## Goal

Expose the existing credential-free Google Calendar connection projection to the authenticated
provider application. This slice adds one read-only API boundary so later settings work can render
truthful disconnected, connected, and recovery states without loading OAuth configuration or
credential encryption keys.

This slice does not complete issue #56. It performs no OAuth redirect, Google request, credential
decryption, availability calculation, or frontend rendering.

## HTTP contract

`GET /api/coach/calendar-connections/google` returns
`CoachCalendarConnectionResponse`:

- `provider`: always `"google"`;
- `state`: `"disconnected"`, `"connected"`, or `"authorization_required"`;
- `connected_at`: the connection creation time, or `null` when disconnected;
- `updated_at`: the last connection lifecycle update, or `null` when disconnected; and
- `authorization_required_at`: the time reauthorization became necessary, otherwise `null`.

All timestamps are timezone-aware and serialized by the API in its normal ISO 8601 form. The
response contains no provider account ID, instructor ID, connection ID, revision, OAuth state,
credential metadata, nonce, ciphertext, refresh token, or external event data.

The endpoint accepts no path selection, query parameter, request body, provider identifier,
account identifier, instructor identifier, or credential material. The account ID is derived only
from the validated `CoachContext`. The fixed route selects Google server-side.

## Authentication and privacy

The existing provider-session cookie is required. An unauthenticated request receives the standard
credential-free `401 AUTHENTICATION_REQUIRED` response. Because this operation is a GET, it does
not require the mutation-only CSRF marker.

Successful and error responses remain `private, no-store` with a `no-referrer` policy through the
provider response middleware; the route also applies the existing private-response helper.
OpenAPI documents cookie authentication and the exact response schema.

The service opens and closes one service-owned SQLAlchemy session, calls only
`get_google_calendar_connection_status()`, and maps every response field explicitly. It does not
load calendar credential ciphers, PKCE ciphers, Google client configuration, redirect settings, or
token transports. A missing authenticated owner maps to the existing generic provider-not-found
response without including identifiers. Other persistence failures remain sanitized by the
existing repository and global unexpected-error handler.

## Verification

Focused PostgreSQL API tests prove:

- unauthenticated requests fail before the route executes;
- an authenticated provider with no connection receives the exact disconnected projection;
- connected and authorization-required lifecycle timestamps are projected exactly;
- one provider cannot observe another provider's state, even with spoofed query values;
- no persistence or credential fields appear anywhere in the response;
- privacy headers are present;
- the read works when all Google OAuth and credential-key environment variables are absent; and
- the generated OpenAPI contract includes the fixed route, cookie security, and exact schema.

The generated OpenAPI artifact is committed and checked by the existing contract suite. Existing
calendar repository tests remain authoritative for encryption, lifecycle commands, revision
fencing, concurrency, and credential isolation.

## Deferred work

1. Add the host-only browser-binding cookie lifecycle and authenticated OAuth start/callback routes.
2. Add a disconnect mutation and settings recovery actions.
3. Claim and decrypt the provider-scoped refresh credential for bounded Google use.
4. Refresh access, query and normalize Google FreeBusy intervals, and mark expired authorization.
5. Integrate external blockers fail-closed into search and final booking revalidation.
6. Render truthful connection and recovery states in provider settings.

## Non-goals

- Frontend components, buttons, loading states, or copy.
- OAuth cookies, redirects, callback query parsing, rollout modes, or Google denial handling.
- Disconnect, reconnect, credential replacement, credential decryption, refresh, or revocation.
- Google network traffic, FreeBusy parsing, retries, deadlines, or live provider tests.
- Scheduler, agenda, booking, or public API changes.
- Models, migrations, fixtures, environment configuration, multiple calendars, or other providers.
