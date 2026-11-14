# Issue #56s: strict Google Calendar authorization-code exchange

## Goal

Exchange one already-authenticated Google authorization code and PKCE verifier for a validated
offline refresh credential. This slice is a pure, transport-injected protocol boundary: it adds no
route, cookie, environment loader, concrete HTTP client, database access, credential encryption,
OpenAPI contract, or frontend behavior.

## Public contract

`GoogleCalendarTokenExchangeConfiguration` snapshots an exact validated
`GoogleCalendarOAuthConfiguration` plus one confidential client secret. It is immutable and fully
redacted. Configuration, transport-response, and result values use sealed, non-subclassable slotted
containers rather than dataclasses and reject pickle, standard JSON, dataclass, and application JSON serialization.
The secret must be nonblank visible ASCII, is never trimmed or normalized, and is bounded to 4 KiB.
Environment loading is deferred.

`exchange_google_calendar_authorization_code()` accepts only that exact configuration, one bounded
visible-ASCII authorization code, one canonical 43-character PKCE verifier, and an injected
`GoogleCalendarTokenTransport`. It performs exactly one transport call with a five-second timeout,
no retry, and the fixed endpoint:

```text
https://oauth2.googleapis.com/token
```

The ordered form allowlist contains exactly `code`, `client_id`, `client_secret`, `redirect_uri`,
`grant_type=authorization_code`, and `code_verifier`. The endpoint cannot be supplied by a caller,
and no secret is placed in a URL or header. The transport contract must not follow redirects or
retry; a later concrete adapter will enforce those network properties.

The exchange returns an immutable, builder-only, redacted `GoogleCalendarTokenExchangeResult`
containing only the validated refresh token. Access-token material, expiry, scopes, raw JSON, and
provider diagnostics are discarded.

## Response validation

The provider response must be an exact HTTP 200 JSON response with a bounded body no larger than
64 KiB. JSON must be UTF-8, have one top-level object, contain no duplicate keys or non-finite
constants, and include:

- a nonblank visible-ASCII access token no larger than 16 KiB;
- a nonblank visible-ASCII refresh token compatible with the 16 KiB credential-encryption limit;
- exact `token_type=Bearer`;
- a positive integer `expires_in`; and
- exactly the single granted scope `https://www.googleapis.com/auth/calendar.freebusy`.

An OAuth error object, identity token, missing refresh token, missing or broader scope, malformed
field, non-200 status, redirect, transport failure, or malformed response fails closed. Unknown
non-identity extension members are ignored. A refresh token is mandatory because this product must
verify availability while the instructor is offline.

## Failure and confidentiality policy

All caller, transport, HTTP, parsing, and provider-response failures become one newly constructed,
fixed, cause-free `GoogleCalendarTokenExchangeError`. A dedicated raiser clears cause and context,
including a secret-bearing exception already active in the caller. The public wrapper discards its
configuration, code, verifier, and transport before raising, while sensitive parsing remains in
private helper frames absent from the exported traceback.

The client secret, authorization code, verifier, access token, refresh token, response body, error
description, and transport exception details never appear in result/configuration/response string
representations, exported exception messages, causes, contexts, traceback locals, or module logs.
The module performs no logging.

There are no automatic retries. After the callback has committed state consumption, any exchange
failure starts a fresh authorization attempt rather than reviving state or risking a duplicate code
exchange with an outcome-unknown first request.

## Transaction boundary

This module accepts no database session. The future callback service must commit browser-bound state
consumption and release all database locks before calling it. A later persistence step must encrypt
and replace the refresh credential inside a new explicit transaction.

## Verification

Pure fake-transport tests cover the exact endpoint and form allowlist, one-call behavior, strict
configuration/code/verifier validation, successful minimal output, response bounds and JSON shape,
exact least-privilege scope, provider and transport failures, constructor hardening, immutability,
and secret-free representations, errors, tracebacks, frame locals, and logs. Existing OAuth
configuration, authorization-request, PKCE, credential, and connection suites remain unchanged.
Tests also cover ambient exception chains and rejection by standard and application serializers.

## References

- Google OAuth 2.0 for web server applications:
  <https://developers.google.com/identity/protocols/oauth2/web-server>
- RFC 7636 PKCE authorization-code exchange:
  <https://www.rfc-editor.org/rfc/rfc7636.html#section-4.6>

## Deferred slices

1. Load the client secret at call time and add a bounded, non-redirecting concrete HTTP adapter.
2. Add start/callback orchestration, commit-before-network transaction handling, and cookie cleanup.
3. Encrypt and atomically replace the Google connection in a fresh transaction.
4. Refresh credentials and query normalized opaque FreeBusy intervals.
5. Integrate fail-closed external busy periods and expose settings/recovery behavior.

## Non-goals

- Environment or secret-manager loading, startup/readiness activation, or feature flags.
- Routes, cookies, redirects, callback parsing, authentication, OpenAPI, or UI.
- OAuth-state consumption, sessions, transactions, credential encryption, or persistence.
- Access-token use, token refresh, revocation, FreeBusy networking, or calendar writes.
- Retries, DPoP, identity discovery, multiple calendars, other providers, metrics, or audit events.
