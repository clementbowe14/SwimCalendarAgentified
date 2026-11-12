# Issue #56d: encrypted calendar connection schema

## Goal

Add the durable PostgreSQL shape for one encrypted Google primary-calendar connection per provider
account. This slice defines only the migration and SQLAlchemy model so ownership, credential-at-rest,
and lifecycle invariants can be reviewed before persistence services or OAuth can write a row.

## Connection record

`provider_calendar_connections` contains:

- an application-generated connection ID used by the credential envelope as authenticated context;
- one owning `account_id` with a cascading foreign key and a unique constraint;
- a provider fixed to `google` for the pilot;
- the encrypted refresh credential envelope: format version, key ID, 12-byte nonce, and ciphertext;
- a nullable `authorization_required_at` recovery timestamp; and
- timezone-aware `created_at` and `updated_at` timestamps.

The row is connected when it exists and `authorization_required_at` is null. A non-null recovery time
means authorization recovery is required. No row means disconnected. A later successful
reauthorization replaces the connection with a new ID and freshly encrypted credential; those state
transitions belong to the persistence service, not this schema slice.

The unique provider-account constraint enforces the pilot limit of one connected primary calendar.
Deleting the owning provider account cascades to its credential row. Deleting an instructor remains
restricted by the existing provider-account ownership boundary.

## Database invariants

PostgreSQL checks require:

- provider exactly `google`;
- a connection ID matching `calconn_` followed by 32 lowercase hexadecimal characters;
- credential format version exactly one;
- a key ID matching `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`;
- a nonce of exactly 12 bytes;
- ciphertext between 17 and 16,400 bytes, including the AES-GCM authentication tag;
- `updated_at` to be no earlier than `created_at`; and
- `authorization_required_at`, when present, to fall between `created_at` and `updated_at`.

A unique `(credential_key_id, credential_nonce)` index prevents AES-GCM nonce reuse across stored
connections under the same key and provides the leading key-ID index needed for rotation audits.

Every credential envelope column is non-null. A connection row can only be created after the OAuth
callback supplies a refresh credential and the issue #56b cipher encrypts it.

## Privacy boundary

The schema contains no refresh-token plaintext, access token, provider account email, calendar ID,
event ID, title, attendee, description, location, raw OAuth response, or provider error. The fixed
Google FreeBusy scope and fixed `primary` calendar remain code-level policy rather than persisted
metadata.

The model must never define a plaintext credential property or representation. SQLAlchemy engines
already hide bound parameters; later services must continue that behavior and reconstruct the opaque
envelope only inside the credential boundary.

## Migration safety

Revision `20261112_0015` extends the single Alembic head `20261111_0014` and requires PostgreSQL.
Upgrade creates only the new table and key/nonce index. Downgrade takes an exclusive lock and refuses to drop the table
while any connection row exists, preventing silent credential loss. Operators must disconnect or
otherwise explicitly remove every connection before retrying a downgrade.

## Verification

Migration tests verify exact columns and types, named checks and indexes, the unique ownership
constraint, cascading foreign key, accepted encrypted-envelope shape, rejected malformed
provider/credential/time values, one-row-per-provider enforcement, model relationships, safe empty
downgrade/upgrade, and non-destructive refusal when credential rows exist.

## Deferred slices

1. Transactional connection persistence that consumes `CalendarCredentialCipher` and never returns
   raw encrypted columns through an API.
2. Single-use, expiring OAuth-state persistence bound to the initiating provider account and session.
3. OAuth authorization, callback, status, disconnect, Google token refresh, and FreeBusy transport.

## Non-goals

- Reading configuration, encrypting credentials, generating IDs, or implementing lifecycle services.
- OAuth state, access tokens, routes, OpenAPI, Google networking, busy-period caching, or frontend UI.
- Multiple connections, calendars, providers, or persisted Google account/event metadata.
