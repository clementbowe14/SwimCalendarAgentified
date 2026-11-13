# Issue #56i: encrypted calendar OAuth PKCE-verifier schema

## Goal

Add the smallest durable PostgreSQL shape needed to bind one protected PKCE verifier to one pending
Google Calendar OAuth state. This slice defines only the encrypted envelope model and migration so
its ownership, confidentiality, nonce, deletion, deployment, and downgrade invariants can be reviewed
before verifier generation, encryption, authorization URLs, callbacks, or token exchange are added.

## Record shape

`provider_calendar_oauth_pkce_verifiers` is a one-to-one child of
`provider_calendar_oauth_states`. It contains:

- the parent's 32-byte `state_digest` as both primary key and cascading foreign key;
- `verifier_version`, fixed to envelope version `1`;
- a bounded, validated `verifier_key_id` identifying the configured encryption key;
- a 12-byte `verifier_nonce`; and
- a 59-byte `verifier_ciphertext` containing the encrypted 43-byte PKCE verifier plus the 16-byte
  AES-GCM authentication tag.

The table has no independent account, session, provider, expiry, or timestamps. Its parent remains
the sole authority for ownership and lifetime, preventing those values from drifting. One state can
have at most one verifier envelope. A unique `(verifier_key_id, verifier_nonce)` index prevents nonce
reuse under the same key while permitting the same random nonce under distinct keys.

The relationship is optional at the database boundary for rolling-deployment compatibility: current
state issuance can continue while this schema is deployed. A later authorization service must create
the state and verifier in one transaction, and its callback must reject a state without a verifier.

The runtime rollout must be feature-gated. Deploy the schema first, then deploy PKCE-capable issue and
callback code to every application instance with new authorization starts disabled. After every old
writer is quiescent, wait the parent's maximum ten-minute lifetime so any bare state has expired;
only then enable the new atomic issue-and-encrypt path and callback. Rollback must disable new starts
before draining active verifier-backed states. The migration lock is DDL protection, not a substitute
for this application rollout gate.

## Confidentiality boundary

The raw PKCE verifier must never enter PostgreSQL, URLs, logs, diagnostics, or errors. The child table
contains only an authenticated-encryption envelope. It deliberately omits the verifier, code
challenge, authorization code, redirect URI, provider response, access token, refresh token,
calendar identity, account ID, and session ID.

The later crypto slice must use AES-256-GCM with a PKCE-specific key derived from each configured
calendar root key using HKDF-SHA256. It must not directly reuse the calendar credential encryption
key. Its authenticated associated data must bind the envelope purpose and version, key ID, state
digest, account ID, session ID, and provider. The fixed 59-byte ciphertext constraint is valid only
for a 43-byte verifier and the 16-byte GCM tag; changing verifier length requires a new envelope
version and schema review.

## Lifecycle invariants

Deleting a verifier leaves its state intact. Deleting a state cascades to its verifier. Session and
account deletion already cascade through the state and therefore remove the verifier as well.
Replacing or consuming the state also removes the old verifier through the same database cascade.

The schema intentionally permits an expired parent to retain its child until normal state cleanup.
The child cannot outlive its parent or reference an unknown state.

## Migration safety

Revision `20261112_0017` extends the single Alembic head `20261112_0016` and requires PostgreSQL.
Upgrade takes a non-waiting `SHARE ROW EXCLUSIVE` lock on the parent state table before creating the
child table and unique nonce index. It performs no backfill and does not rewrite existing states.

Downgrade takes non-waiting exclusive locks in parent-then-child order. It refuses while any child is
attached to an unexpired state, preserving an active authorization attempt. Children belonging only
to expired states may be discarded. Lock contention, a live-verifier refusal, or migration failure
leaves the schema, data, and Alembic revision unchanged through transactional rollback. Errors must
not include state digests, key IDs, ciphertext, or tenant identifiers.

## Verification

PostgreSQL migration tests verify:

- one linear Alembic head and the exact column, type, nullability, check, primary-key, foreign-key,
  and unique-index shape;
- absence of plaintext and unrelated OAuth, tenant, or calendar fields;
- rejection of malformed envelope versions, key IDs, digests, nonces, ciphertexts, unknown parents,
  duplicate state children, and same-key nonce reuse;
- allowance of the same nonce under a different key;
- relationship behavior and state, session, and account cascade cleanup without reverse deletion;
- compatibility with existing state rows that do not yet have a child; and
- empty and expired-only downgrade/re-upgrade, active-verifier refusal, transactional migration
  failure during both upgrade and destructive downgrade steps, and upgrade/downgrade lock contention
  without data or revision loss.

## Deferred slice

The next small PR will define the PKCE crypto boundary: generate an independent 43-character verifier,
derive an `S256` challenge, derive a purpose-specific encryption key, encrypt/decrypt with bound AAD,
and expose only redacted immutable envelope values. Repository integration will then issue the state
and encrypted verifier atomically before an authorization URL can be constructed.

## Non-goals

- PKCE verifier generation, challenge derivation, encryption, decryption, or key configuration.
- Changes to the current OAuth-state repository, issuance, consumption, or public API.
- OAuth authorization URLs, callbacks, routes, redirects, cookies, OpenAPI, or frontend behavior.
- Google client secrets, token exchange/refresh, FreeBusy requests, or scheduler integration.
