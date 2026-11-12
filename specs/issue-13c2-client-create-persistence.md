# Issue 13C2: Transactional client-create persistence

## Objective

Add the smallest runtime layer that can atomically persist a canonical client profile and its
availability windows using the provider-scoped idempotency storage merged in PR #81.

This slice changes no HTTP route, request schema, response schema, or frontend behavior. The
authenticated create API remains a separate follow-up so persistence races and API validation can
be reviewed independently.

## Repository contract

Add two provider-scoped database operations:

- `get_client_creation_by_idempotency_key(instructor_id, key)` returns the immutable replay
  envelope for that provider and key, or `None` when no create exists.
- `create_client_profile(...)` inserts the active client, all ordered availability windows, the
  idempotency key, and its replay envelope in one transaction.

The caller supplies server-generated client/window IDs and a versioned replay envelope. This layer
does not interpret HTTP input or construct the envelope; it enforces only the persistence and
concurrency boundary.

## Idempotency behavior

The merged partial unique index on `(instructor_id, create_idempotency_key)` is authoritative.

- A first create commits one profile and its complete window collection.
- Repeating the same provider/key with the same normalized request returns the original stored
  envelope, including the winner's generated IDs and response.
- Reusing the same provider/key for a different normalized request raises
  `IdempotencyKeyConflictError` and changes nothing.
- Different providers may reuse a key independently.
- Concurrent inserts race on the database unique index. Exactly one commits; a same-request loser
  returns the winner's envelope, while a different-request loser receives the conflict error.

Request equivalence is determined by the replay envelope's normalized `request` member. Generated
IDs, geocoder output, and response fields do not participate in that comparison because concurrent
equivalent requests can produce different tentative server output before one transaction wins.

## Atomicity

The client row, every availability window, idempotency key, and replay envelope commit together.
Any profile/window constraint failure rolls back the complete create. The same key remains reusable
after a rolled-back transaction.

The existing tenant-preserving client/window foreign key remains authoritative. Unexpected
integrity errors that are not explained by a committed row for the same provider/key are re-raised;
they must not be mislabeled as an idempotent replay.

## Compatibility boundary

- Keep legacy rows with null replay metadata unchanged.
- Keep existing list/detail reads and command-center projections unchanged.
- Do not add request validation, geocoding, HTTP routes, OpenAPI changes, update/deactivation logic,
  or frontend behavior in this PR.
- Do not close issue #13.

## Verification

- persist and read a complete active profile with ordered availability;
- return the exact original envelope on sequential same-request replay;
- reject sequential same-key/different-request reuse;
- allow the same key for two providers;
- force concurrent same-request inserts through unique-conflict recovery and prove both callers see
  one exact winner;
- force concurrent different-request inserts and prove one winner plus one conflict;
- force a window constraint failure, prove complete rollback, and successfully reuse the key; and
- keep the complete backend and migration suites green.
