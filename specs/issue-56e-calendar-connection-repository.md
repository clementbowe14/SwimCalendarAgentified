# Issue #56e: transactional calendar connection repository

## Goal

Add the smallest runtime persistence boundary for one encrypted Google Calendar connection per
provider account. This slice consumes the credential cipher and schema merged in the preceding
issue #56 slices, but adds no OAuth, HTTP route, Google request, startup requirement, or UI.

## Public contract

`app.calendar_connections` exposes a safe `CalendarConnectionStatus` with only:

- provider `google`;
- state `disconnected`, `connected`, or `authorization_required`;
- connection, update, and authorization-recovery timestamps where applicable.

It never exposes the connection ID, account or instructor ID, key ID, nonce, ciphertext, refresh
credential, or a provider response. Its representation is therefore safe for ordinary internal
diagnostics and for later mapping to a settings response.

The module provides four provider-scoped operations:

- read status for a trusted, server-derived provider account ID;
- replace any existing connection with a freshly identified and encrypted Google connection;
- mark an expected current connection as requiring authorization; and
- disconnect, idempotently removing the encrypted row.

The persisted provider-account row is authoritative for the instructor ID placed in authenticated
encryption context; no caller-supplied instructor ID is trusted. An invalid account raises one fixed,
identifier-free `CalendarConnectionOwnerNotFound` error.
Commands require a caller-owned active SQLAlchemy transaction and never commit, roll back, open a
secondary session, or read deployment configuration themselves. An implicit SQLAlchemy autobegin is
rejected; command callers must establish an explicit `BEGIN` boundary.

## Transaction and concurrency rules

Every command locks the owning `provider_accounts` row before reading or changing its connection.
That one stable lock serializes replace, recovery, and disconnect operations even when no connection
row currently exists. The locked account is refreshed from PostgreSQL before its persisted instructor
ID is used as authenticated context, even when the Session identity map already contains that account.

Replacement:

1. verifies and locks the provider account;
2. generates a fresh `calconn_` identifier;
3. encrypts the supplied refresh credential with authenticated context containing that identifier
   and the persisted instructor owner;
4. rejects any generated `(key ID, nonce)` already present, including the current row, before any
   mutation rather than concealing a potentially broken entropy source;
5. only after encryption succeeds with unused envelope material, removes the previous row and
   inserts the replacement; and
6. flushes, while leaving commit or rollback to the caller.

The new row always has a fresh identifier, nonce, timestamps, and cleared recovery state. Encryption
or preflight collision failure occurs before mutation. The database unique index remains authoritative
for a cross-account race after preflight. Any later persistence failure or caller rollback restores
the prior connection atomically.

Marking authorization required requires the connection ID observed by the later credential-use path.
It preserves the first recovery timestamp and is idempotent. If replacement or disconnect wins the
account lock first, a stale mark does not affect the replacement and never recreates a credential.
Disconnect is idempotent. Replacement after recovery creates a new healthy connection.

All accepted and returned datetimes are timezone-aware and canonicalized to UTC. Default command
times are captured after the account lock is acquired. An explicitly supplied event time retains its
meaning and is rejected if it would move the stored update time backwards.

## Secret boundary

- Plaintext exists only as the replace argument and inside `CalendarCredentialCipher` while
  encrypting.
- The repository persists only the opaque envelope and never returns encrypted columns.
- No exception message, status value, representation, or log includes credential or tenant data.
- Sanitized persistence errors sever the underlying SQLAlchemy exception context so parameter values
  cannot be recovered through `__context__` or `__cause__`.
- The module exports only its safe status, fixed errors, and four operations; envelope and ORM types
  remain private implementation details.
- This slice intentionally has no credential-decryption read. The later Google transport will add a
  narrowly scoped internal use path rather than making plaintext part of the general repository API.

## Verification

PostgreSQL tests cover:

- disconnected and connected safe status projections;
- real cipher round-trip through the stored row and authenticated owner scope;
- replacement with a new ID and nonce, removal of the old row, and recovery reset;
- forced old-row and cross-tenant nonce collisions failing before mutation;
- a cross-account collision race reaching the authoritative unique index and restoring the loser;
- encryption failure and caller rollback preserving the prior credential;
- idempotent recovery marking and disconnect;
- stale recovery marking leaving a replacement healthy;
- account-scoped isolation across two providers;
- command rejection without a caller-owned transaction;
- serialization of concurrent commands through the provider-account lock; and
- status/error/repr inspection without plaintext or encrypted envelope material.

## Deferred slices

1. Single-use, expiring OAuth-state persistence bound to provider account and active session.
2. OAuth authorization, callback, status, and disconnect routes.
3. Narrow credential use, Google token refresh, and FreeBusy transport.
4. Startup activation of required credential configuration when an external credential path is
   reachable.
5. Settings UI and consumption by availability and agenda workflows.

## Non-goals

- OAuth state or authorization URLs, callback handling, access tokens, scopes, or Google networking.
- Credential decryption, key rewrap jobs, busy-period reads, caches, or scheduler integration.
- API schemas, routes, OpenAPI, startup/readiness, environment parsing, or frontend changes.
- Multiple connections, calendars, providers, or calendar write access.
