# Issue #56l: atomic calendar OAuth PKCE-state issuance

## Goal

Atomically issue one provider-bound Google OAuth state and its encrypted PKCE verifier. This slice
returns only the raw state, public S256 challenge, and expiry needed by a later authorization
service.
It also prevents the two legacy state commands from deleting or replacing verifier-backed states.

PKCE-aware callback consumption remains deliberately deferred so plaintext-verifier release,
decryption failure, replay, and consume concurrency can be reviewed in their own PR. No runtime
route calls the new command in this slice.

## Public command

`issue_google_calendar_oauth_state_with_pkce()` requires a caller-owned explicit SQLAlchemy
transaction and accepts trusted, server-derived account/session identifiers plus an injected
`CalendarOAuthPKCECipher`. It never reads deployment configuration, opens another session, commits,
rolls back, logs, or performs network I/O.

It returns an immutable, redacted `IssuedGoogleCalendarOAuthPKCEState` containing only:

- the 43-character raw OAuth state bearer;
- the UTC expiry;
- the public S256 code challenge; and
- a fixed `code_challenge_method` property equal to `S256`.

The result never contains the plaintext verifier, encrypted envelope, state digest, key ID, nonce,
ciphertext, account ID, or session ID.

## Atomic issuance

The command preserves the existing account-then-session lock order. After validating the injected
cipher and optional aware clock, it locks and revalidates the enabled account and exact active
session. Expiry remains the earlier of ten minutes after issuance and the session expiry.

Each of at most three attempts creates a complete independent candidate:

1. generate a fresh 32-byte state bearer and its SHA-256 digest;
2. reject an already stored digest before encryption;
3. bind a `CalendarOAuthPKCEScope` to that digest and the persisted account/session IDs;
4. generate a fresh verifier, S256 challenge, and encrypted envelope;
5. reject a live same-key nonce collision before mutation; and
6. inside one nested savepoint, delete the previous owner state, insert the new parent, and insert
   the encrypted verifier child.

The previous parent and child therefore remain intact until a complete replacement can be inserted.
Caller rollback restores the previous pair; caller commit publishes the replacement pair. No
supported application operation can commit a new parent without its verifier child.

## Collision and integrity handling

The state primary key and unique `(verifier_key_id, verifier_nonce)` index remain authoritative for
concurrent races. If either constraint wins after the prechecks, the nested savepoint rolls back and
the command retries with a completely fresh state, verifier, challenge, and nonce.

After an integrity error, the repository rechecks only the candidate digest and same-key nonce. An
occupied candidate is a retryable collision. Any other integrity failure is not disguised as a
collision and fails closed immediately. Entropy exhaustion, PKCE generation/encryption failure,
unrelated database failure, or three exhausted attempts produce the existing fixed, cause-free
`CalendarOAuthStatePersistenceError` and preserve the prior authorization.

## Legacy safety fences

Bare states remain compatible with the existing issue and consume commands during rollout. A state
with a verifier child is protected from both legacy destructive paths:

- `issue_google_calendar_oauth_state()` checks under the locked owner pair and refuses to replace a
  verifier-backed state with a bare state, returning the generic persistence error before requesting
  new state entropy.
- `consume_google_calendar_oauth_state()` includes an authoritative correlated `NOT EXISTS` child
  condition in its final `DELETE ... RETURNING`. A verifier-backed state returns the same generic
  invalid-state error as an unknown or replayed bearer, while both parent and child remain stored.

The consume fence is part of the delete predicate rather than relying on the earlier owner lookup,
so a child cannot be cascade-deleted by that command.

## Confidentiality and failure boundary

- PostgreSQL stores only the state digest and issue #56i encrypted envelope.
- The plaintext verifier, raw state, and public challenge are never persisted.
- The envelope is authenticated to its digest, persisted owner IDs, Google provider, key ID, and
  version by the issue #56j cipher.
- Configuration and root keys stay outside the repository; callers inject the already-built cipher.
- Result and error representations are redacted and fixed.
- Exceptions expose no underlying cause or context and the module emits no logs.

## Verification

PostgreSQL tests cover explicit transaction ownership, exact redacted result shape, atomic
parent/child commit and rollback, replacement of prior pairs, challenge verification against the
decrypted stored envelope, absence of plaintext persistence, state-digest and same-key nonce retry,
nonce exhaustion, unrelated-integrity fail-fast behavior, generation-failure sanitization, persisted
owner/AAD binding, inactive-owner and expiry boundaries, cross-tenant rejection, both legacy safety
fences under sequential and concurrent commit/rollback orderings, transaction recovery, and two
concurrent issuers leaving one complete matching pair. Error tests inspect repository traceback
locals as well as messages, representations, causes, contexts, and logs. The unchanged legacy
repository suite proves bare-state compatibility.

## Rollout

This code remains inert until a later service calls it. Runtime activation must still use the
schema-first cutover from issue #56i:

1. deploy the safety fences to every instance;
2. deploy PKCE-aware callback consumption behind a disabled gate;
3. stop every legacy state writer;
4. wait the maximum ten-minute state lifetime; and
5. enable verifier-backed starts and callbacks together.

For rollback, disable new starts first and retain PKCE callback support plus every referenced root
key for at least the maximum state lifetime. A pre-#56l binary must never process a live
verifier-backed state because it lacks these deletion fences.

## Deferred slices

1. Lock the persisted parent and child, reconstruct the encrypted envelope, decrypt it with
   persisted owner scope, and delete the pair only after successful authentication.
2. Build the fixed Google authorization URL only after this issuance transaction commits.
3. Add the callback route, authorization-code exchange, granted-scope validation, and encrypted
   connection replacement.

## Non-goals

- PKCE-aware consumption, plaintext-verifier return, callback replay handling, or token exchange.
- Authorization URLs, client secrets, Google networking, refresh, or FreeBusy requests.
- Models, migrations, configuration, startup/readiness, routes, OpenAPI, or frontend changes.
- Feature-gate activation, background cleanup, audit logging, or multiple providers.
