# Issue #56f: single-use calendar OAuth-state schema

## Goal

Add the smallest durable PostgreSQL shape needed to authenticate a future Google OAuth callback
without relying on the instructor's SameSite cookie. This slice defines only the digest-only state
model and migration so ownership, expiry, replay, privacy, and downgrade invariants can be reviewed
before any repository, route, authorization URL, callback, or Google request is added.

## State record

`provider_calendar_oauth_states` contains:

- a SHA-256 `state_digest` primary key; the raw bearer value is never persisted;
- the initiating provider `account_id` and `session_id`;
- a provider fixed to `google` for the pilot;
- timezone-aware `created_at` and `expires_at` timestamps.

The state digest is the natural lookup key for the future callback and is globally unique. A separate
surrogate identifier would add storage and another value that could escape through diagnostics
without improving ownership or replay safety. A unique `(account_id, session_id)` constraint permits
one pending authorization flow per signed-in session. A later issue command will replace that pending
flow, so an older tab cannot complete after the instructor starts a newer authorization attempt.
Successful consumption will use one `DELETE ... RETURNING` statement; replay then finds no row and
receives the same generic invalid-state result as an unknown or expired value.

## Ownership and lifetime invariants

The state row uses one composite foreign key from `(account_id, session_id)` to
`provider_sessions(account_id, id)`. A supporting unique constraint is added to the session table.
This makes it impossible to pair an existing account with another account's session, which two
independent foreign keys would permit. Session deletion cascades to its state rows; account deletion
continues to cascade through its sessions and then their states. Deleting a state does not affect its
session or account.

PostgreSQL checks require:

- a digest of exactly 32 bytes;
- provider exactly `google`;
- expiration strictly after creation and no more than ten minutes later.

An expiration index supports cleanup and downgrade checks. The unique account/session constraint
also supports the composite foreign key and later account/session-scoped maintenance. The schema
does not limit a state to the provider session's remaining lifetime; the future issue/consume service
must cap expiration to the earlier of ten minutes or session expiry and revalidate that the account
and session are still active when consuming it.

## Privacy boundary

Only a SHA-256 digest of a high-entropy raw state may enter this table. The schema contains no raw
state, authorization code, access token, refresh token, PKCE verifier, redirect target, scope,
provider response, email, instructor ID, calendar identity, or event metadata. Errors and migration
messages must not include a digest or tenant identifier.

SHA-256 is safe here only because the later issuer will generate at least 32 cryptographically random
bytes. If PKCE is added later, its verifier needs a separately reviewed protected lifecycle and must
not be placed in this table as plaintext.

## Migration safety

Revision `20261112_0016` extends the single Alembic head `20261112_0015` and requires PostgreSQL.
Upgrade adds the supporting session uniqueness constraint, creates the state table, and creates its
indexes.

Upgrade first takes a non-waiting exclusive lock on the session table before adding its supporting
constraint. Downgrade takes exclusive, non-waiting locks on both affected tables. It refuses while
any unexpired state exists, preserving an in-flight authorization attempt; expired rows may be
discarded. After the state table is dropped, downgrade removes the supporting session uniqueness
constraint. Lock contention or a live-state refusal leaves the schema, rows, and Alembic revision
unchanged through transactional rollback.

## Verification

PostgreSQL migration tests verify:

- one Alembic head and the exact column, type, default, check, index, primary-key, and foreign-key
  shape;
- digest-only persistence and the absence of sensitive OAuth or calendar fields;
- exact ten-minute expiry, one pending state per session, and global digest uniqueness across
  accounts;
- rejection of malformed digests, provider values, lifetimes, unknown owners, and cross-account
  session substitution;
- state, session, and account deletion behavior without cross-tenant effects; and
- empty and expired-only downgrade/re-upgrade, live-state refusal, and upgrade/downgrade lock
  contention without data or revision loss.

## Deferred slice

The next small PR will add an internal issue/consume repository. It must generate at least 32 random
bytes, persist only their SHA-256 digest, cap the state lifetime to the active session, and consume by
one conditional database mutation that rejects unknown, expired, revoked, disabled, version-stale,
or replayed attempts without logging either representation of the state.

## Non-goals

- OAuth authorization URLs, callbacks, routes, cookies, redirects, OpenAPI, or frontend behavior.
- Raw-state generation, hashing, issue/consume commands, cleanup jobs, or application logging.
- Credential decryption, Google token exchange/refresh, FreeBusy requests, or scheduler integration.
- PKCE storage, multiple calendar providers, multiple calendars, or calendar write access.
