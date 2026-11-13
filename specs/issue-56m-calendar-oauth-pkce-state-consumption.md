# Issue #56m: authenticated calendar OAuth PKCE-state consumption

## Goal

Consume one verifier-backed Google OAuth state exactly once, release its authenticated plaintext
`code_verifier` to a later token-exchange service, and remove the state plus encrypted verifier only
after decryption succeeds. This slice remains an internal transactional repository boundary.

It adds no HTTP route, authorization URL, Google request, credential replacement, configuration
activation, OpenAPI contract, or frontend behavior.

## Public command

`consume_google_calendar_oauth_state_with_pkce()` requires a caller-owned explicit SQLAlchemy
transaction and accepts only the raw callback state, an injected `CalendarOAuthPKCECipher`, and an
optional aware operation time. Callback input never supplies an account or session identifier.

It returns an immutable, redacted `ConsumedGoogleCalendarOAuthPKCEState` containing exactly:

- the persisted account ID;
- the persisted session ID; and
- the authenticated plaintext `code_verifier` required by Google's later token exchange.

The result excludes the raw state, state digest, encrypted envelope, key ID, nonce, ciphertext,
challenge, expiry, provider response, and root keys. Its string representations expose no fields.

## Locking and validation

The command preserves the existing global lock order:

1. validate the explicit transaction, injected cipher, aware clock, and canonical state shape;
2. hash the raw state and perform a non-authoritative parent lookup only to discover its persisted
   owner pair;
3. lock and refresh the account, then lock and refresh its exact session;
4. capture the default operation time and revalidate account and session activity;
5. re-read and lock the exact state parent with digest, Google provider, persisted owner, creation,
   and expiry predicates;
6. lock the matching verifier child separately;
7. reconstruct the encrypted envelope and decrypt it with associated data built only from the locked
   digest and persisted owner; and
8. inside one nested savepoint, execute a final conditional parent `DELETE ... RETURNING` with every
   state predicate and a positive correlated child `EXISTS` check, then construct the result before
   releasing that savepoint.

PostgreSQL cascades the committed parent deletion to the one-to-one verifier child. No mutation
occurs before authenticated decryption. Separate parent and child locks make the order explicit:
account, session, state, verifier.

## Single use and caller transaction

A committed consume removes both rows. Every later replay receives the generic invalid-state result.
Concurrent consumers serialize on the owner and state locks, so at most one can commit successfully.

Caller rollback restores both rows. A result returned by a transaction that does not commit must be
discarded. The future callback service must commit consumption before sending the authorization code
and verifier to Google, and must not hold these database locks across network I/O. A later exchange
failure requires a fresh authorization attempt rather than reviving consumed state.

## Failure policy

- Missing explicit transactions raise the existing fixed
  `CalendarOAuthStateTransactionRequired` error.
- Invalid injected ciphers or internal clocks raise the fixed `CalendarOAuthStateInputError`.
- Malformed, unknown, replayed, expired, future-created, inactive-owner, bare, or childless states
  raise the same fixed `CalendarOAuthStateInvalid` without mutation.
- Malformed or unauthentic encrypted envelopes, wrong roots, prematurely retired keys, and
  unexpected persistence failures raise the fixed `CalendarOAuthStatePersistenceError` without
  deleting the pair.

Decryption failure is treated as recoverable persistence failure rather than an absent callback. A
referenced root key restored before the ten-minute state expiry can therefore permit a safe retry.
All exported errors remain cause-free, context-free, identifier-free, and value-free.

## Key rotation and AAD binding

The envelope selects its stored key ID, so an active-key rotation between issuance and callback is
safe while the previous root remains in the injected ring. Operators must retain every referenced
root for at least the maximum state lifetime. No rewrap is needed for this short-lived record.

Authenticated associated data binds the verifier to its exact state digest, account, session, Google
provider, envelope version, and key ID. Moving a parent to another valid session or account cannot
release the verifier and does not delete the pair.

## Confidentiality boundary

The public command delegates sensitive work to a private helper. On every failure it discards public
inputs and raises a newly constructed fixed error outside the caught exception with `from None`.
Private frames containing the raw state, digest, envelope, owner IDs, cipher, or plaintext verifier
are therefore absent from the exported traceback.

The repository never logs, loads configuration, opens another session, commits, rolls back, or
performs network I/O. The plaintext verifier is never persisted.

## Verification

Focused PostgreSQL tests cover the exact public/result shape, transaction ownership, input safety,
successful locked decryption and cascade deletion, replay uniformity, bare and childless
compatibility, owner/session/state lifetime rejection before decryption, exact expiry boundaries,
envelope tampering, wrong and retired keys, retained-key rotation and recovery, persisted-owner AAD
transplants, caller rollback, one-winner concurrent consumption, waiting consumers after commit or
rollback, replacement and revocation races in both commit outcomes, post-delete savepoint rollback,
and secret-free messages, representations, tracebacks, repository-frame locals, and logs.

The unchanged issuance and legacy suites continue proving that bare states remain consumable only by
the legacy command and verifier-backed states remain protected from both legacy destructive paths.

## Rollout

This command is inert until a later service calls it. Runtime activation still follows the
schema-first cutover from issue #56l: deploy callback support disabled, stop legacy writers, drain
the maximum state lifetime, then enable verifier-backed starts and callbacks together. Rollback
disables new starts first and retains callback support plus all referenced roots through the drain
window.

## Deferred slices

1. Build the fixed Google authorization URL from committed PKCE issuance.
2. Add start and callback routes plus feature-gate activation.
3. Exchange the authorization code and verifier, validate granted scopes, and atomically replace the
   encrypted Google connection.
4. Refresh credentials and query normalized opaque Google busy intervals.
5. Integrate fail-closed external busy periods into agenda and availability, then expose settings
   and recovery status.

## Non-goals

- Google networking, client secrets, authorization codes, tokens, refresh, FreeBusy, or calendar
  writes.
- Models, migrations, environment parsing, startup/readiness, routes, cookies, redirects, OpenAPI,
  or UI.
- Metrics, audit events, background cleanup, verifier rewrap, multiple calendars, or other
  providers.
