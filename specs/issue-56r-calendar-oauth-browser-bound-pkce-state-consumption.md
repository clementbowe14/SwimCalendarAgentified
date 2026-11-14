# Issue #56r: browser-bound calendar OAuth PKCE-state consumption

## Goal

Consume one Google OAuth state exactly once only when the callback presents the
independent browser secret issued with it. After authenticating the encrypted
PKCE envelope, return the existing persisted owner and plaintext verifier for a
later token-exchange service.

This slice is an inert transactional repository boundary. It adds no cookie,
route, redirect, feature gate, Google request, configuration, OpenAPI contract,
schema, or frontend behavior.

## Public command

`consume_google_calendar_oauth_state_with_pkce_and_browser_binding()` requires
a caller-owned explicit SQLAlchemy transaction and accepts only:

- the raw callback state;
- the raw browser-held binding;
- an injected `CalendarOAuthPKCECipher`; and
- an optional aware operation time.

It reuses the immutable, redacted
`ConsumedGoogleCalendarOAuthPKCEState`, containing exactly the persisted account
ID, persisted session ID, and authenticated plaintext `code_verifier`. Callback
input never supplies owner identifiers.

## Canonical browser binding

The browser value must be an unpadded 43-character Base64URL string that
strictly decodes to 32 bytes and re-encodes to the exact original string. The
repository does not trim, normalize, accept padding, or accept aliases with
nonzero unused trailing bits.

The lookup digest is fixed to:

```text
SHA-256(
  ASCII("dayweave.calendar-oauth-browser-binding.sha256.v1")
  || 0x00
  || decoded 32-byte browser secret
)
```

The command rejects equal raw state and browser values and defensively rejects
equal derived state and browser digests. PostgreSQL receives only the two
digests. Stored digest matching is performed through exact SQL predicates; the
stored browser digest is never loaded for application-level comparison.

## Locking and authenticated deletion

The command preserves the global lock order:

1. validate the explicit transaction, aware clock, cipher, and both bearers;
2. discover the persisted owner using the exact state and browser digests;
3. lock and refresh the account, then its exact session;
4. capture the default operation time and validate owner activity;
5. lock the exact, current, unexpired state using both digests and the persisted
   owner;
6. lock the matching verifier child;
7. decrypt using the existing PKCE associated data of state digest, persisted
   account, persisted session, provider, envelope version, and key ID; and
8. inside one nested savepoint, conditionally delete the parent with every
   state predicate, the exact browser digest, and a positive child `EXISTS`
   check, then construct the result before releasing the savepoint.

The browser digest is not added to PKCE associated data because issue #56q
created envelopes with the existing scope. The locked parent column is the
browser-binding authority. PostgreSQL cascades a committed parent deletion to
the verifier child; the command never mutates the child directly.

## Single use, rollback, and failures

A committed consume removes both rows. Replays and state/browser pair swaps
receive the same fixed `CalendarOAuthStateInvalid` as malformed, unknown,
expired, future-created, inactive-owner, bare, unbound-PKCE, and childless
states. None reaches decryption or mutates the authorization.

Envelope, key, AAD, or unexpected persistence failures use the fixed
`CalendarOAuthStatePersistenceError` and preserve the pair. Missing transaction
and invalid trusted inputs retain their existing fixed errors. The public
wrapper removes both raw bearers, both digests, owner data, verifier material,
keys, SQL details, and private helper frames from exported failures.

Caller rollback restores the parent and child. A verifier returned by a caller
transaction that does not commit must be discarded. The future callback service
must commit and release database locks before any Google network request. A
later exchange failure starts a fresh authorization attempt rather than reviving
consumed state.

## Verification

Focused PostgreSQL tests cover the exact command contract, strict canonical
binding validation, uniform mismatch failures, forwarded-link and pair-swap
defense, authoritative digest predicates, lock order, cascade deletion, replay,
unbound-row rejection, representative inactive and incomplete states, wrong-key
recovery, owner AAD transplant, caller rollback, concurrent one-winner and
waiting-consumer behavior, issuance/replacement and revocation races, savepoint
rollback, and secret-free errors, tracebacks, frame locals, SQL parameters, and
logs.

## Rollout and deferred work

The command deploys inert. A later cookie/route slice must ship starts and
callbacks behind a disabled gate, stop legacy starts, drain the ten-minute state
lifetime, then enable bound starts and callbacks together. Rollback disables new
starts first and retains this consumer through the drain window.

Deferred work includes the host-cookie lifecycle, start/callback routes,
authorization-code exchange, encrypted credential replacement and refresh,
opaque FreeBusy ingestion, fail-closed scheduling integration, and settings UI.

## Non-goals

- Models, migrations, PKCE cryptography, AAD changes, or legacy-consumer changes.
- Cookies, routes, redirects, sessions, configuration, OpenAPI, or frontend.
- Google codes, tokens, credentials, refresh, FreeBusy, or calendar writes.
- Multiple calendars, other providers, metrics, audit events, or cleanup jobs.
