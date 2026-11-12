# Issue 13C1: Client-create idempotency storage

## Objective

Add the smallest persistence dependency needed for a later authenticated client mutation API.
This slice changes no HTTP route or frontend behavior. It gives each provider a durable namespace
for replaying a client-create request without duplicating a canonical client profile.

## Schema contract

Add two nullable columns to `clients`:

- `create_idempotency_key`: the provider-supplied create key, limited to 160 characters;
- `client_create_payload`: the versioned normalized request and original canonical response used by
  the later mutation service.

Legacy and non-API-created rows keep both columns null. A named check constraint requires the two
columns to be null or populated together. A partial unique index on
`(instructor_id, create_idempotency_key)` applies only when the key is non-null, so:

- one provider cannot persist two client creates under the same key;
- two providers may independently use the same key; and
- unlimited legacy null-key rows remain valid.

The SQLAlchemy model must declare the same columns, check, and partial unique index as Alembic so
fresh metadata and migrated production storage do not drift.

## Migration behavior

Alembic revision `20261111_0014` follows canonical client profile revision `20261110_0013`.
Upgrade and downgrade require PostgreSQL and acquire `ACCESS EXCLUSIVE NOWAIT` on `clients` before
schema mutation. This keeps lock contention fail-closed and avoids a partially changed contract.

Upgrade is additive and does not rewrite client/profile/window data. Downgrade removes only replay
metadata; all canonical profile and availability data remains representable by the prior revision.
Because PostgreSQL DDL is transactional, a later downgrade failure must roll the database back to
the current head revision rather than leave revision `0013` partially applied.

## Compatibility boundary

- Do not add create, update, or deactivate routes in this PR.
- Do not change existing client reads or command-center projections.
- Do not add replay service methods before this migration merges.
- Do not close issue #13; authenticated mutations and the Dayweave UI remain later slices.

## Verification

- inspect the migrated columns, all-or-none check, and provider-scoped partial unique index;
- prove each half-populated replay state is rejected;
- prove upgrade/downgrade preserves canonical profile and availability values;
- keep the prior client-profile downgrade safety and lock-contention tests aligned with the new
  head revision; and
- run the complete backend migration/regression suite.
