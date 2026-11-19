# Issue #56ae: Google Calendar refresh-credential claim

## Goal

Create the smallest safe handoff between a stored Google Calendar connection and one future bounded
provider request. The service locks and authenticates the persisted owner and connection, decrypts
the refresh credential from persisted scope, commits and releases every database lock, and only
then returns a sealed credential claim. After network work, the caller must revalidate that exact
claim in a fresh transaction before accepting any provider result.

This slice is internal and route-inert. It adds no Google request, access-token refresh, FreeBusy
parsing, scheduler integration, route, configuration loading, OpenAPI contract, migration, or
frontend behavior.

## Public contract

`claim_google_calendar_refresh_credential()` accepts:

- a SQLAlchemy session factory owned by the service;
- the trusted instructor ID selected by authenticated application context; and
- an exact `CalendarCredentialCipher`.

It returns exactly one of:

- `"disconnected"` when the enabled persisted owner has no Google connection;
- `"authorization_required"` when the exact connection is already marked for recovery, without
  attempting decryption; or
- a sealed `GoogleCalendarRefreshCredentialClaim` containing the persisted provider account ID,
  instructor ID, connection ID, current calendar-connection revision, and decrypted refresh token.

`revalidate_google_calendar_refresh_credential_claim()` accepts a service-owned session factory and
an exact claim. It returns `None` only when the same enabled owner, connection, provider, revision,
and healthy authorization state still exist after the network gap.

The claim is final, slotted, immutable, builder-only, fully redacted, and non-serializable. Every
malformed dependency, missing or disabled owner, corrupt or unauthentic envelope, database failure,
stale claim, commit failure, or close failure becomes the same newly constructed, fixed, cause-free
`GoogleCalendarRefreshCredentialClaimError`. No error distinguishes tenant existence or lifecycle
details.

The sealed value and its supported validator live in a database-free handoff module. Issuance adds
a process-local keyed integrity tag over every owner, connection, revision, and refresh-token field.
The supported validator checks that complete tag and returns an independent sealed snapshot, so a
consumer never validates and then rereads mutable source fields. Future network consumers must
validate before and after an injected collaborator runs. A field changed with low-level Python
mutation therefore cannot retag one tenant's credential as another's. The tag is an in-process
misuse and corruption defense, not a boundary against arbitrary code execution inside the trusted
application process.

## Claim transaction

Inputs and exact dependency types are validated before session creation. The factory must return a
service-owned idle SQLAlchemy `Session` backed directly by one transaction-capable `Engine`.
Provider-account and calendar-connection mappers must resolve to that engine. Active or external
transactions, autocommit, and split mapper binds fail closed. The owned session is always closed.

One explicit transaction performs the claim in invariant lock order:

1. select and lock only the provider account ID, instructor ID, disabled timestamp, and calendar
   connection revision by the trusted instructor ID;
2. require the persisted owner to exist, be enabled, and have a valid positive connection revision
   when connected;
3. select and lock only the Google connection identity, provider, credential envelope, and recovery
   timestamp by the persisted account ID;
4. return the distinct disconnected or authorization-required state when applicable;
5. construct encryption scope only from the persisted instructor ID, connection ID, and fixed
   Google provider;
6. authenticate and decrypt the persisted envelope without rewrapping or mutating it; and
7. commit and close the session before constructing the sealed claim.

The claim transaction never changes the envelope, connection, owner, revision, or timestamps and
never performs network I/O.

## Post-network revalidation

Revalidation validates the exact sealed claim before session creation, then opens a separate owned
transaction and locks in the same account-then-connection order. It selects no credential columns.
The claim remains current only when:

- the exact account ID still belongs to the exact persisted instructor and remains enabled;
- `calendar_connection_revision` exactly equals the claim revision;
- the exact connection ID still belongs to that account;
- its provider is exactly Google; and
- `authorization_required_at` remains null.

Replacement and disconnect advance the shared revision. Recovery marking is intentionally
revision-neutral, so exact connection identity and a null recovery timestamp are independent,
mandatory fences. Revalidation itself is read-only and does not increment the revision.

This boundary is a freshness fence across one network operation, not a durable lease. A caller must
not cache or reuse a claim, and no provider result may reach availability or booking logic unless
its claim has just revalidated successfully.

## Concurrency and confidentiality

All connection lifecycle operations and both functions in this slice lock the provider account
before the connection. A lifecycle transition that commits before revalidation invalidates the old
claim. Revalidation that obtains the locks first may accept the result, after which a waiting
transition linearizes. No network call occurs while either lock is held.

The refresh token, key material, nonce, ciphertext, envelope metadata, tenant identifiers, SQL
details, dependency exception text, and stored row values never appear in exported messages,
representations, cause/context chains, public traceback locals, or logs. The module performs no
logging, environment access, or configuration loading and returns no ORM or session object.

## Verification

Pure tests cover the exact API, validation before session creation, exact cipher and claim types,
owned transaction and single-engine enforcement, commit/close-before-return ordering, sealed claim
behavior, fixed secret-free failures, and runtime-inert imports.

PostgreSQL tests cover tenant-isolated persisted scope, disconnected and recovery states, retained
rotation keys without rewrap, missing keys and corrupted or transplanted envelopes, read-only
behavior, unchanged revalidation, invalidation by replacement, disconnect, recovery marking, and
owner disablement, plus an ordered claim-versus-replacement race proving that the account lock
prevents a mixed revision/envelope snapshot. No test contacts Google.

Existing credential-encryption and connection-lifecycle suites remain authoritative for exhaustive
AES-GCM validation, schema constraints, lifecycle commands, and their broader concurrency matrix.

## Deferred work

1. Exchange a claimed refresh credential for an access token under one hard deadline.
2. Query and normalize opaque Google FreeBusy intervals, revalidate before use, and mark expired
   authorization against the exact observed connection.
3. Integrate external blockers fail-closed into search and final booking revalidation.
4. Add the complete browser OAuth route lifecycle and provider settings recovery actions.

## Non-goals

- Models, migrations, constraints, triggers, cleanup jobs, or credential rewrapping.
- Google networking, access tokens, FreeBusy requests, retries, parsing, or provider mocks.
- Routes, authentication middleware, cookies, rollout gates, OpenAPI, or frontend UI.
- Scheduler, agenda, availability, booking, calendar writes, event details, or notifications.
- Multiple calendars, other providers, metrics, audit events, or secret-memory zeroization claims.
- Protection against arbitrary code execution inside the trusted application process.
