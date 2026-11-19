# Issue #56af: strict Google Calendar refresh-token exchange

## Goal

Exchange one sealed, persisted Google Calendar refresh-credential claim for a short-lived access
token while preserving the claim's complete tenant and connection-generation fence. This slice is
a pure, transport-injected protocol boundary. It performs no database mutation, environment load,
concrete HTTP request, FreeBusy query, route, OpenAPI change, or frontend work.

The existing authorization-code exchange remains authoritative for installing the offline refresh
credential. This slice reuses its strict response decoder and strengthens the shared secret-value
and control-exception boundaries without changing authorization-code protocol semantics. It
consumes the stored credential only after issue #56ae has authenticated and decrypted it, committed,
and released every database lock.

## Public contract

`exchange_google_calendar_refresh_token()` is keyword-only and accepts:

- an exact existing `GoogleCalendarTokenExchangeConfiguration`;
- an exact sealed `GoogleCalendarRefreshCredentialClaim`; and
- an application-owned `GoogleCalendarTokenTransport`.

It returns either:

- a sealed `GoogleCalendarRefreshTokenExchangeResult`; or
- a sealed `GoogleCalendarRefreshAuthorizationRequired` for one strictly validated revoked,
  expired, or otherwise invalid refresh grant.

Both outcomes contain exactly the claim's persisted account ID, instructor ID, connection ID, and
expected calendar-connection revision. The success result additionally contains the validated
access token and positive `expires_in_seconds`. Neither outcome retains the claim, refresh token,
scope, response body, provider diagnostics, configuration, or transport.

Each outcome is final, slotted, immutable, builder-only, fully redacted, and non-serializable. A
direct constructor call is rejected by the runtime without entering a Python constructor frame, so
caller-supplied tokens and identifiers cannot become locals in this module. This misuse raises a
fixed built-in `TypeError`; protocol and validation failures retain the generic exchange error.
Because this unsupported call never enters a module boundary, Python may attach an exception that
the caller already had active as the `TypeError` context. Sanitizing caller-owned ambient exception
content is outside this module's contract; supported exchange boundaries always replace dependency
errors and clear their context. A future FreeBusy operation must compare the result's complete
fence with its exact claim and must
successfully revalidate that claim before accepting provider busy periods. An access-token result
from one tenant or credential generation must never be interchangeable with another.

## Request contract

Before invoking the transport, the exchange revalidates the exact configuration, every claim field,
and the claim's keyed integrity tag. The claim refresh token must be an exact, nonblank visible-ASCII
string no larger than the existing 16 KiB credential limit. Invalid, subclassed, incomplete,
forcibly mutated, or cross-tagged claims and configuration values fail before the transport is
called. The complete claim is validated again after the injected transport returns and before an
access-token result or authorization-recovery state can be produced. Every post-transport field,
including the refresh token, must match the copied pre-transport snapshot exactly; a coherent
replacement with another otherwise valid claim fails closed.

The exchange performs exactly one form POST to the fixed endpoint:

```text
https://oauth2.googleapis.com/token
```

The ordered form contains exactly:

1. `client_id`;
2. `client_secret`;
3. `grant_type=refresh_token`; and
4. `refresh_token`.

It sends no scope, redirect URI, authorization code, PKCE verifier, account identifier, connection
identifier, cookie, query secret, or authorization header. The injected transport receives the
existing exact five-second timeout argument. It must not redirect or retry. This argument is not a
hard total wall-clock deadline; the later concrete async adapter owns that guarantee.

## Successful response contract

An HTTP 200 response must have the existing safe JSON content type and a body from 1 through 65,536
bytes. JSON is decoded as strict UTF-8, must contain one top-level object, must have unique member
names and finite numbers, and must contain no trailing content.

The object must contain:

- an exact nonblank visible-ASCII `access_token` no larger than 16 KiB;
- exact `token_type` value `Bearer`; and
- an exact integer `expires_in` from 1 through 2,147,483,647.

The `scope` member may be absent because OAuth treats an omitted refresh scope as the originally
granted scope. When present, it must equal only
`https://www.googleapis.com/auth/calendar.freebusy`, byte-for-byte. A broader, duplicate,
whitespace-modified, empty, or wrongly typed scope fails closed.

Any `error`, `id_token`, or replacement `refresh_token` member is rejected regardless of value.
OAuth allows refresh-token rotation, but silently discarding a replacement could invalidate the
stored credential; supporting rotation requires a separate claim-fenced atomic persistence design.
Unknown non-identity extension members are ignored.

## Authorization recovery classification

Only all of the following together return a claim-bound
`GoogleCalendarRefreshAuthorizationRequired`:

- exact HTTP status 400;
- a valid bounded JSON content type and strict bounded object body;
- exact string member `"error": "invalid_grant"`; and
- no access-token, ID-token, or refresh-token member.

Descriptions, URIs, subtypes, and unknown fields are ignored and never returned or logged. Any
other status, OAuth error, malformed body, duplicate member, or token-bearing error payload becomes
the same generic exchange failure. The recovery outcome must remain internal and must not appear
directly in public API diagnostics.

Before a later service marks recovery state, it must compare all four recovery fields with a newly
validated snapshot of the original sealed claim and then atomically target the exact account and
connection. A stale or cross-tenant invalid-grant result must never mark a replacement credential.

## Failure and confidentiality policy

Every protocol or validation failure except the sealed recovery outcome becomes one newly
constructed, fixed, cause-free `GoogleCalendarTokenExchangeError`. Direct attempts to construct
builder-only result values fail before module Python code executes. There are no retries, including
for timeout, 408, 429, 5xx, malformed responses, or transport exceptions.

For supported configuration, transport-response, and exchange boundaries, the client secret,
refresh token, access token, response body, provider detail, tenant identifiers, dependency
exception text, and malformed values never appear in exported messages, representations,
cause/context chains, public traceback-frame locals, or logs. Direct-construction misuse adds no
module frame and never renders its supplied arguments, but retains Python's caller-owned ambient
exception chaining described above. This guarantee covers one primary dependency, validation, or
control failure crossing a supported boundary. A reentrant process-level interruption after that
boundary has begun classifying, clearing, or re-raising the primary failure is outside the
pure-Python threat model. Cancellation is preserved as a newly constructed, cause-free cancellation
after sensitive arguments are cleared. The module performs no logging.

## Verification

Pure fake-transport tests cover:

- the exact public API, endpoint, ordered form, timeout, one-attempt behavior, and absence of extra
  request fields;
- complete result binding for distinct tenants and connection generations;
- exact claim/configuration validation before transport use;
- result constructor hardening, immutability, redaction, and serializer rejection;
- access-token, token-type, expiry, optional exact scope, and identity/rotation rejection;
- strict status, content type, body bound, UTF-8, JSON shape, duplicate-key, and finite-number
  handling;
- strict invalid-grant recovery classification versus generic failures; and
- fixed, cause-free, secret-free diagnostics for transport, parser, and provider failures.

The existing authorization-code exchange suite remains authoritative for exhaustive configuration,
PKCE, refresh-credential, and shared JSON/media-type primitives. No test contacts Google or
PostgreSQL.

## References

- Google OAuth 2.0 for web server applications:
  <https://developers.google.com/identity/protocols/oauth2/web-server>
- OAuth 2.0 refresh grant and response rules, RFC 6749 sections 5 and 6:
  <https://www.rfc-editor.org/rfc/rfc6749.html>

## Deferred work

1. Add a concrete environment-free async HTTPX refresh adapter with one hard overall deadline.
2. Query and normalize opaque Google FreeBusy intervals using the bound access-token result.
3. Revalidate before accepting busy periods and mark exact invalid grants for reauthorization.
4. Integrate external blockers fail-closed into search and final booking revalidation.
5. Add the complete browser OAuth route lifecycle and settings recovery actions.

## Non-goals

- HTTPX, sockets, TLS, proxies, hard-deadline cancellation machinery, retries, or live Google calls.
- Database sessions, credential reads or writes, connection lifecycle mutations, or key rotation.
- FreeBusy requests, busy-period parsing, scheduling, availability, agenda, or booking behavior.
- Routes, cookies, authentication middleware, rollout gates, OpenAPI, or frontend UI.
- Sanitizing caller-owned ambient exceptions around unsupported direct result construction.
- Reentrant process-level interruption after supported exception handling has already begun,
  including during failure classification, cleanup, or sanitized re-raise.
- Refresh-token rotation persistence, DPoP-bound grants, DPoP nonce handling, multiple calendars,
  other providers, metrics, or audit events.
