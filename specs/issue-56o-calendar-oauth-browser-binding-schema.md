# Issue #56o: calendar OAuth browser-binding schema

## Goal

Add the smallest durable PostgreSQL shape needed to bind a future Google OAuth callback to the
browser that started it. This schema-only slice stores one optional digest on the existing OAuth
state. It adds no secret generation, repository command, cookie, route, redirect, Google request,
configuration, OpenAPI contract, or frontend behavior.

No runtime code writes a browser binding in this slice. Existing state and PKCE commands continue
to create unbound rows until compatibility fences and bound issue/consume commands are deployed in
later PRs.

## Threat boundary

The existing random state authenticates a server-side authorization attempt and PKCE protects the
authorization code. Neither proves that the browser returning from Google is the browser that
started the attempt. Without a second browser-held value, an attacker can begin a connection for
their Dayweave account and forward the Google authorization URL to another person, causing that
person's Google Calendar to be linked to the attacker's account.

The future start flow will generate an independent 32-byte browser secret and place only its
domain-separated SHA-256 digest in PostgreSQL. The raw secret will exist only in a short-lived,
host-only, `Secure`, `HttpOnly`, `SameSite=Lax` flow cookie. The callback must validate that cookie
before consuming state, decrypting the PKCE verifier, or contacting Google.

## Record shape

Revision `20261113_0018` adds nullable `browser_binding_digest` to
`provider_calendar_oauth_states`.

- `NULL` preserves existing unbound state rows during schema-first rollout.
- A present value must be exactly 32 bytes and must differ from the state digest, rejecting direct
  reuse of the state digest. The future writer remains responsible for independent entropy and
  domain-separated hashing.
- A partial unique index over present digests prevents one browser secret from binding two pending
  states and gives future issuance an authoritative collision boundary.
- The existing state row remains the sole authority for account, session, provider, creation, and
  expiry; no duplicate ownership or lifetime fields are added.

The column is intended only for the future digest. PostgreSQL cannot distinguish a 32-byte digest
from a mistakenly supplied 32-byte raw secret, so the later repository boundary must prove
digest-only persistence. The schema adds no state bearer, PKCE verifier, authorization code, token,
cookie, calendar identity, event detail, or user profile field.

## Migration safety

Upgrade requires PostgreSQL and takes a non-waiting `ACCESS EXCLUSIVE` lock on the state table
before adding the column, check, and partial unique index. It performs no backfill and does not
rewrite existing values.

Downgrade takes the same non-waiting lock and refuses while any unexpired row has a binding digest.
This prevents an active browser-bound authorization from silently losing its required binding.
Bindings attached only to expired rows may be discarded. Lock contention, refusal, or failure while
adding or removing schema objects leaves the row data and Alembic revision unchanged through
transactional rollback. Errors expose no digest or tenant identifier.

## Rollout and compatibility fences

This migration must deploy before any bound writer. The next repository PR must first make every
existing unbound issue and consume command refuse to replace or consume a row whose binding digest
is present. Only then may a new command atomically create the state digest, encrypted PKCE verifier,
and browser-binding digest.

Bound issuance must remain disabled until every instance has both the compatibility fences and a
consumer that requires the matching raw browser secret. Rollback disables new starts, retains the
bound consumer for at least the maximum ten-minute state lifetime, and only then removes the schema.
An older PKCE consumer must never process a live bound row because it would not validate the
browser-held secret.

## Verification

PostgreSQL migration tests cover the linear revision, exact nullable column/check/index shape,
legacy-null upgrade compatibility, digest-length and direct state-digest reuse checks, uniqueness
across states, model round-tripping, empty and expired-only downgrade/re-upgrade, live-binding
refusal, transactional upgrade and downgrade failures, and non-waiting lock contention.

The unchanged repository and application suites prove that nullable schema addition does not alter
current unbound OAuth behavior.

## Deferred slices

1. Add compatibility fences to every existing state issue/consume command.
2. Atomically issue state, encrypted PKCE material, and an independently generated browser binding.
3. Consume a bound state only after matching and locking its browser binding, then release the PKCE
   verifier.
4. Add the start/callback cookie and route workflow, including forwarded-link and browser/account
   swapping tests.
5. Exchange the code, validate granted scope, and replace the encrypted Google connection.

## Non-goals

- Browser-secret generation, hashing implementation, repository writes, issue/consume commands, or
  collision retries.
- Cookies, sessions changes, routes, redirects, feature gates, OpenAPI, or UI.
- Google client secrets, authorization codes, token exchange/refresh, FreeBusy networking, or
  calendar writes.
- Multiple calendars, other providers, metrics, audit events, or cleanup jobs.

## References

- OAuth 2.0 Security Best Current Practice: <https://www.rfc-editor.org/rfc/rfc9700.html>
- OAuth 2.0 CSRF guidance: <https://www.rfc-editor.org/rfc/rfc6749.html#section-10.12>
