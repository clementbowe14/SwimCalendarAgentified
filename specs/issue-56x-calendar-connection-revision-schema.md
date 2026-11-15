# Issue #56x: calendar connection revision fence schema

## Goal

Add the smallest durable prerequisite for safely finalizing a Google Calendar OAuth callback after
its network gap. A callback that started earlier must not install credentials after the instructor
disconnects, replaces the connection, or has a newer callback claimed. This slice stores a
per-provider-account generation that later commands can use to invalidate that stale work.

The slice is schema-only and runtime-inert. It changes no connection repository operation, callback,
route, cookie, rollout gate, API contract, startup behavior, or frontend flow.

## Account revision

`provider_accounts.calendar_connection_revision` is a non-null PostgreSQL `BIGINT` with a server and
model default of zero. A named check constraint requires the value to remain nonnegative. Existing
accounts are initialized to zero during migration, and newly created accounts receive zero when the
caller omits the field.

The revision belongs to the provider account rather than the optional connection row. That location
allows a later disconnect to invalidate an in-flight first connection even when no encrypted
connection exists. It also reuses the account row that all calendar connection commands already lock
to serialize mutations.

This migration does not add a trigger. Later repository commands must explicitly advance the
revision while holding the provider-account lock so each security-sensitive transition has a clear,
testable transaction boundary.

## Deferred command contract

The following behavior is intentionally deferred to focused runtime slices:

1. A successful callback claim advances the revision and captures the resulting value.
2. Post-network finalization opens a fresh transaction, locks the provider-account row, revalidates
   the exact initiating session and claimed revision, and holds that lock through credential
   replacement and commit. A successful finalization advances the revision again to consume the
   claim.
3. A newer callback claim, direct replacement, and disconnect advance the same revision. Disconnect
   advances it even when already disconnected so it cancels an in-flight initial connection.
4. Revision exhaustion fails closed without wrapping or reusing a prior value.

These rules ensure a callback cannot reconnect an account after a later user or system action has
invalidated it. They do not belong in this schema-only pull request.

The runtime verification must include both transaction orderings for finalization racing a
disconnect, direct replacement, or newer claim. A revision comparison performed without holding the
provider-account lock through the write is not sufficient.

## Migration safety

Revision `20261114_0019` extends the single Alembic head `20261113_0018` and requires PostgreSQL.
Upgrade takes a `NOWAIT` access-exclusive lock on `provider_accounts`, adds the constant-zero column,
and adds its named invariant in one transactional migration. Lock contention fails without a partial
column, constraint, or Alembic revision change.

Downgrade takes the same lock and refuses when any account revision is nonzero. Once calendar
connection activity has advanced the fence, silently dropping it would erase ordering information
that stale in-flight work may still rely on. An untouched installation whose values are all zero can
downgrade and re-upgrade without losing account data.

## Privacy boundary

The revision is only an opaque ordering counter. It contains no refresh token, access token,
authorization code, OAuth state, PKCE verifier, browser binding, provider identity, calendar ID,
event data, client data, or location. Downgrade errors are fixed and disclose no tenant identifier or
counter value.

## Verification

Migration tests verify the linear Alembic head, exact `BIGINT` column and named constraint, zero
backfill and defaults, preservation of existing account data, accepted nonnegative values, rejection
of negative values, model metadata, safe zero-valued downgrade/re-upgrade, non-destructive refusal
after activity, transactional failure cleanup, and `NOWAIT` lock behavior on upgrade and downgrade.

## Non-goals

- Claiming, consuming, or finalizing an OAuth callback.
- Session revalidation, credential encryption, connection replacement, recovery, or disconnect.
- Automatic revision triggers or changes to existing calendar repository semantics.
- HTTP routes, redirects, browser cookies, rollout modes, OpenAPI, or UI.
- Google token exchange, refresh, FreeBusy reads, calendar writes, or additional providers.
