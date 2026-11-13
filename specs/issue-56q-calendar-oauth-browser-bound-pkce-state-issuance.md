# Issue #56q: browser-bound calendar OAuth PKCE-state issuance

## Goal

Atomically issue one Google OAuth state, encrypted PKCE verifier, and independent browser-binding
secret. PostgreSQL stores only the state digest, domain-separated browser-binding digest, and
encrypted verifier. The raw browser binding is returned only to the future start service for a
short-lived host cookie.

This slice remains an internal transactional repository boundary. It adds no binding-aware
consumer, cookie, route, redirect, Google request, configuration activation, OpenAPI contract, or
frontend behavior.

## Public command and result

`issue_google_calendar_oauth_state_with_pkce_and_browser_binding()` requires a caller-owned explicit
SQLAlchemy transaction and accepts only trusted account/session identifiers, an injected
`CalendarOAuthPKCECipher`, and an optional aware operation time.

It returns an immutable, redacted `IssuedGoogleCalendarOAuthBrowserBoundPKCEState` containing
exactly:

- `issued_state`: the exact existing `IssuedGoogleCalendarOAuthPKCEState`, so the fixed Google
  authorization builder can consume it without learning the browser secret; and
- `raw_browser_binding`: the one canonical 43-character Base64URL secret for the future cookie.

The result excludes both digests, owner identifiers, plaintext verifier, encrypted envelope, key
ID, nonce, ciphertext, provider configuration, authorization URL, and cookie attributes. Both the
outer and nested result representations redact their fields.

## Independent entropy and digest

Every candidate uses three independent CSPRNG requests: 32 bytes for OAuth state, 32 bytes for the
browser binding, and the existing 32-byte PKCE verifier generation. AES-GCM nonce generation remains
independent. Browser bytes are encoded as canonical unpadded Base64URL.

The stored browser digest is fixed to:

```text
SHA-256(
  ASCII("dayweave.calendar-oauth-browser-binding.sha256.v1")
  || 0x00
  || raw browser bytes
)
```

The digest intentionally excludes state and owner identifiers so reuse of one browser secret maps
to one globally unique digest. A candidate is discarded if the encoded browser binding equals the
raw OAuth state or if its binding digest equals the state digest. PostgreSQL never receives the raw
state, raw browser binding, or plaintext verifier.

## Atomic issuance

The command preserves account-then-session locking and active-owner validation. Expiry remains the
earlier of ten minutes after issuance and the session expiry. Each of at most three attempts:

1. generates a fresh state and rejects an occupied state digest;
2. generates a fresh browser binding and rejects equality with state, digest equality with state,
   or an occupied binding digest;
3. generates fresh PKCE material and rejects an occupied same-key nonce; and
4. inside one nested savepoint, deletes the current owner state, inserts the bound parent, and
   inserts the encrypted verifier child.

The owner delete intentionally has no null-binding predicate: this binding-aware command may replace
bare, unbound-PKCE, or already bound authorizations. Caller rollback restores the exact previous
parent/child pair; caller commit publishes one complete new triple.

## Collision and failure policy

Primary-key, partial binding-uniqueness, and same-key nonce constraints remain authoritative. After
an integrity race, the repository rechecks the candidate state digest, binding digest, and nonce. A
confirmed collision retries with an entirely new bundle. Any unrelated integrity failure stops
immediately. Entropy, hashing, PKCE, collision exhaustion, or persistence failures produce the
existing fixed, cause-free `CalendarOAuthStatePersistenceError` and preserve the prior pair.

Missing transactions, invalid cipher/clock values, and inactive owner/session pairs retain the
existing fixed command errors. The public-wrapper/private-helper boundary removes raw state,
browser binding, digest, owner, verifier, envelope, nonce, key material, and SQL details from
tracebacks and logs.

## Verification

Focused PostgreSQL tests cover the exact composed result and signature, frozen digest vector,
independent entropy, digest-only persistence, equal-bearer rejection, atomic commit and rollback,
replacement of all prior state forms, state/binding/nonce collisions and post-precheck races,
collision exhaustion, unrelated integrity failure, inactive owners before entropy, legacy-command
fences against a real bound issuance, and concurrent bound issuers leaving one matching complete
triple.

## Rollout

This command deploys inert and must not be called until the matching binding-aware consumer exists.
After that consumer and the cookie workflow deploy behind a disabled gate, old starts must stop and
the ten-minute state lifetime must drain before bound starts and callbacks are enabled together.
Rollback disables new starts first and retains the bound consumer for at least ten minutes.

## Deferred slices

1. Consume a bound state only after matching and locking its browser binding, then release the PKCE
   verifier.
2. Add the start/callback cookie and route workflow, including forwarded-link and browser/account
   swapping tests.
3. Exchange the authorization code, validate granted scope, and replace the encrypted Google
   connection.

## Non-goals

- Binding-aware consumption, cookies, sessions changes, routes, redirects, or feature gates.
- Models, migrations, configuration, OpenAPI, authorization-builder, or frontend changes.
- Google client secrets, authorization codes, token exchange/refresh, FreeBusy networking, or
  calendar writes.
- Multiple calendars, other providers, metrics, audit events, or cleanup jobs.
