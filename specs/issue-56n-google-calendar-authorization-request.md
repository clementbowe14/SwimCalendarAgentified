# Issue #56n: fixed Google Calendar authorization request

## Goal

Build one deterministic Google Calendar OAuth authorization URL from the validated public OAuth
configuration and one verifier-backed PKCE-state issuance. This is a pure construction boundary:
it adds no route, persistence, transaction, feature gate, Google request, token exchange, OpenAPI
contract, or frontend behavior.

## Public builder

`build_google_calendar_authorization_request()` accepts only an exact
`GoogleCalendarOAuthConfiguration` and an exact `IssuedGoogleCalendarOAuthPKCEState`. It returns an
immutable, redacted `GoogleCalendarAuthorizationRequest` containing the authorization URL.
The result cannot be constructed through its public initializer; only the validated builder can
create it.

The builder revalidates both inputs instead of trusting frozen objects that could have been mutated
through low-level Python operations. It snapshots exact primitive strings once and rejects string
subclasses so values cannot change between validation and encoding. State and challenge must each
be canonical, unpadded, 43-character base64url encodings of exactly 32 bytes. The issuance must use
`S256` and carry an exact UTC expiry. The builder deliberately does not read a clock: freshness
remains the issuing service's responsibility, and a future start service must build immediately
after a successful commit.

## Fixed Google request

The URL uses Google's web-server authorization endpoint:

```text
https://accounts.google.com/o/oauth2/v2/auth
```

Its query is an ordered allowlist containing exactly these parameters:

1. `client_id` from the validated configuration;
2. `redirect_uri` from the validated configuration, preserved byte-for-byte;
3. `response_type=code`;
4. `scope=https://www.googleapis.com/auth/calendar.freebusy`;
5. `access_type=offline`;
6. `prompt=consent select_account`;
7. the raw one-time `state` bearer;
8. the public PKCE `code_challenge`; and
9. `code_challenge_method=S256`.

The narrow FreeBusy scope permits reading availability without calendar-event access. Offline
access is required for later refresh. `consent` lets a reconnect obtain a replacement refresh grant,
while `select_account` reduces accidental connection of the wrong Google account.

The builder omits incremental-scope union, identity scopes, login hints, hosted-domain hints,
provider account selectors, nonces, and arbitrary caller-supplied parameters. In particular,
`include_granted_scopes` is absent so a prior broader grant is not silently folded into this fixed
request.

## Transaction boundary

This pure builder cannot prove that the issued state and encrypted verifier were committed. The
future authorization-start service must:

1. issue the verifier-backed state inside its explicit database transaction;
2. commit that transaction successfully;
3. discard the issuance on rollback or commit uncertainty; and
4. only then call this builder and redirect the user.

It must never return a redirect for uncommitted state and must not hold database locks while
performing external work.

## Browser binding required before activation

The random state bearer protects the callback endpoint but, by itself, does not prove that the
browser returning from Google is the browser that started the connection. PKCE also does not supply
that browser binding. No runtime route may activate this builder until a separate slice extends the
state flow with a short-lived browser-bound secret.

The future start route must set a host-only, `Secure`, `HttpOnly`, `SameSite=Lax` flow cookie and
atomically bind a digest of its independent random secret to the issued state. The callback must
validate that binding before consuming the state or exchanging the authorization code, then clear
the cookie on every outcome. The existing `SameSite=Strict` application session cookie is not a
substitute because it is intentionally absent on the top-level cross-site return from Google.

Activation tests must cover a normal same-browser return, a shared authorization URL opened in a
different browser, a missing or altered flow cookie, and session/account swapping. The latter cases
must fail without consuming the state or contacting Google.

## Failure and confidentiality policy

Every invalid input or encoding failure becomes one newly constructed, fixed, cause-free
`GoogleCalendarAuthorizationError`. The public wrapper removes the private helper frame before
raising, so malformed values, configuration, raw state, challenge, and encoder details do not leak
through messages, representations, exception context, traceback locals, or logs.

The result's string representations redact the complete URL because it contains the state bearer.
The module reads no environment, clock, database, request, application state, or network resource
and emits no logs.

## Verification

Pure tests cover the exact public/result surface, golden URL and parameter ordering, strict
least-privilege allowlist, offline and account-selection policy, redirect variants, canonical state
and challenge validation, query-injection attempts, UTC expiry structure, time-free behavior,
legacy and subclass rejection, post-validation substitution attempts, builder-only result
construction, forcibly mutated and partially initialized objects, fixed failures, and secret-free
representations, tracebacks, helper-frame locals, and logs.

## References

- Google OAuth 2.0 for web server applications:
  <https://developers.google.com/identity/protocols/oauth2/web-server>
- Google Calendar authorization scopes:
  <https://developers.google.com/workspace/calendar/api/auth>
- Google Calendar FreeBusy query:
  <https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query>

## Deferred slices

1. Persist and validate the required browser-flow binding without weakening the application
   session cookie, then add a start service and route that commit issuance before constructing this
   redirect.
2. Add the callback route, browser/account-swapping tests, authorization-code exchange,
   granted-scope validation, and encrypted connection replacement.
3. Refresh credentials and query normalized opaque Google busy intervals.
4. Integrate fail-closed external busy periods into agenda and availability, then expose settings
   and recovery status.

## Non-goals

- Models, migrations, repositories, environment parsing, startup/readiness, routes, cookies,
  redirects, OpenAPI, or UI.
- Client secrets, authorization codes, token exchange, token refresh, credential persistence,
  FreeBusy networking, or calendar writes.
- Dynamic scopes, caller-provided Google parameters, multiple calendars, or other providers.
