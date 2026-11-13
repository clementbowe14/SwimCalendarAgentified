# Issue #56h: Google Calendar OAuth authorization configuration

## Goal

Define the smallest deployment configuration boundary needed by a later Google Calendar authorization
request. This slice loads only the public OAuth client identifier and the exact server-owned callback
URI. It adds no client secret, authorization URL, PKCE material, route, Google request, token exchange,
credential persistence, OpenAPI, or frontend behavior.

## Environment contract

`google_calendar_oauth_configuration_from_env()` reads these required values on every call:

```text
SWIM_GOOGLE_CALENDAR_OAUTH_CLIENT_ID=<web-client-id>.apps.googleusercontent.com
SWIM_GOOGLE_CALENDAR_OAUTH_REDIRECT_URI=https://api.example.com/api/oauth/google-calendar/callback
```

The factory does not cache either value and does not add them to `ApplicationSettings`, application
state, readiness output, logs, or another normally represented object. There is no development or
test default. The returned immutable `GoogleCalendarOAuthConfiguration` has a fixed redacted string
representation.

The OAuth client secret is deliberately absent. It is not needed to create an authorization request
and will be introduced only with the later server-side code-exchange boundary, reducing its lifetime
and accidental exposure surface.

## Validation

The client identifier must be an exact, printable ASCII Google web-client identifier ending in
`.apps.googleusercontent.com`, without leading or trailing whitespace, and no larger than 512 bytes.

The redirect URI must be exact printable ASCII, no larger than 2 KiB, and must:

- use HTTPS, except that HTTP is allowed for `localhost`, `127.0.0.1`, or `[::1]` development
  callbacks;
- use the fixed path `/api/oauth/google-calendar/callback` with no trailing slash;
- contain no username, password, query, fragment, wildcard, encoded host, or backslash;
- use a valid loopback address or multi-label DNS hostname and, when present, a valid TCP port; and
- reject raw non-loopback IP addresses and any path variant or traversal.

The URI is preserved byte-for-byte after validation. It is never inferred from `Host`,
`X-Forwarded-*`, CORS, or another request-controlled value. Google requires the authorization request
redirect URI to exactly match a URI registered for the web OAuth client.

Every environment-factory failure, including an unexpected loader failure, becomes one fixed,
cause-free `GoogleCalendarOAuthConfigurationError`. Its string, representation, formatted traceback,
reachable exception context, configuration-factory traceback-frame locals, and logs contain neither
configured value nor parser detail. Direct construction also validates its two public, non-secret
values, but cannot remove values already present in its caller's own frame.

## Verification

Pure tests cover valid HTTPS and loopback configurations, live environment rereads, immutability,
bounded values, the invalid client/URI classes above, fixed cause-free failures, redacted diagnostics,
and unchanged application startup when Google Calendar OAuth configuration is absent or malformed.

## Deferred slices

1. Decide and persist state-bound PKCE verifier material before constructing an authorization URL.
2. Construct the fixed Google authorization URL from committed one-time state and this configuration.
3. Add the public callback, server-side code exchange, secret configuration, granted-scope checking,
   and encrypted connection replacement.
4. Add narrow token refresh and Google FreeBusy transport.

## Non-goals

- Client secrets, authorization endpoints, scopes, query parameters, PKCE, state issuance, or routes.
- Import-time parsing, global caching, startup/readiness activation, models, migrations, or networking.
- Dynamic callback paths, request-derived redirect URIs, multiple redirects, providers, or calendars.
