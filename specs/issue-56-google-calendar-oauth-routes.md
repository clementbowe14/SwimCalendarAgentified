# Issue #56 Google Calendar OAuth routes

`POST /api/coach/calendar-connections/google/authorize` requires the existing
provider session and CSRF protection. It accepts an empty body and returns
`{"authorization_url":"https://accounts.google.com/o/oauth2/v2/auth?..."}`.
The existing start service commits state and PKCE before issuing the URL for
`https://www.googleapis.com/auth/calendar.freebusy`. Missing or invalid OAuth,
encryption, or redirect settings return a generic 503.

`GET /api/oauth/google-calendar/callback` accepts one well-formed code and state.
It uses an independent host-only, HttpOnly, SameSite=Lax flow cookie. The cookie
expires with state, within ten minutes, and uses Secure and a `__Host-` name
over HTTPS. The existing provider cookie stays SameSite=Strict. The callback
service claims and consumes state for the active initiating session, exchanges
the code with a deadline, and revalidates the session before saving the
encrypted connection. Successful callbacks clear the flow cookie. Failed
callbacks leave it to expire or be replaced so an invalid callback cannot clear
a newer flow.

The callback returns a 303 to the coach SPA root with only
`?calendar=connected` or `?calendar=failed`. Denials, malformed queries, wrong
bindings, replays, and exchange failures share the failure redirect. The origin
defaults to the validated callback URI origin. An optional
`SWIM_GOOGLE_CALENDAR_COACH_APP_ORIGIN` must be an exact, safe origin from
`SWIM_API_CORS_ORIGINS` on the same scheme. An invalid override blocks starts
and sends callbacks to the validated fallback with the failure marker. If no
safe callback origin is configured, the callback returns a generic 503.

The API removes the callback query from its request scope before routing and
access logging. Start and callback responses have `Cache-Control: private, no-store`
and `Referrer-Policy: no-referrer`. The pilot auth runbook covers redaction at
ingress and how to drain callbacks while blocking new starts.

Tests exercise route contracts, auth, CSRF, cookie and redirect security,
malformed input, and access-log redaction. Disposable PostgreSQL and a fake
Google exchange exercise the real state, binding, session and encrypted
connection path.
