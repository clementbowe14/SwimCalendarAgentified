# Issue #56z: cancel pending calendar OAuth state on lifecycle changes

## Goal

Close the pre-claim lifecycle gap before adding callback orchestration. A Google Calendar
authorization state issued before a credential replacement or disconnect must not remain usable
after that lifecycle command commits.

This is a route-inert persistence slice. It changes no schema, public function signature, status
shape, API, cookie, redirect, configuration, startup behavior, Google request, or frontend flow.

## Lifecycle semantics

`replace_google_calendar_connection()` and `disconnect_google_calendar()` cancel every pending
Google OAuth state owned by the provider account while holding that account's authoritative row
lock. Deleting each state relies on the existing database cascade to delete its encrypted PKCE
verifier child.

Cancellation is account-wide across provider sessions because any older callback could otherwise
install credentials after the lifecycle intent. Other provider accounts remain isolated.

`mark_google_calendar_authorization_required()` and
`get_google_calendar_connection_status()` do not cancel authorization state.

## Ordering and atomicity

Both lifecycle commands preserve the account-first lock order used by issuance and consumption:

1. reject pending direct lifecycle ORM mutations;
2. lock and refresh the provider account;
3. read and refresh the current connection;
4. for replacement, validate time, encrypt the new token, and finish nonce-collision preflight;
5. delete pending Google OAuth states for the account;
6. advance the connection revision and mutate the connection; and
7. flush inside the caller-owned transaction.

Replacement does not cancel state before timestamp validation, encryption, and nonce preflight
succeed. A failure at any of those stages therefore preserves the pending authorization exactly.

State cancellation, PKCE cascade deletion, connection mutation, and revision advancement commit or
roll back together. A persistence failure or caller rollback restores the prior connection,
revision, OAuth states, and verifier rows. A successful repeated disconnect still advances the
revision once and cancels any newly issued state.

Lifecycle commands require the account, connection, OAuth-state, and verifier mappers to resolve to
one transactional bind before executing SQL. A split-bind session fails closed, preventing a
connection mutation from committing independently of state cancellation.

Pending `ProviderCalendarOAuthStateDB` and `ProviderCalendarOAuthPKCEVerifierDB` mutations are
rejected before account locking, just like pending provider-account and connection mutations.
Unrelated caller writes remain permitted and cannot flush before the account lock.

Affected live OAuth-state and verifier objects already cached in the caller's identity map enter
SQLAlchemy's rollback-aware deleted state and detach after commit. A caller rollback restores both
their rows and their persistent identity-map state. Objects whose rows disappeared before the
lifecycle command acquired the account lock are already stale and detach immediately; mutating an
old Python reference therefore cannot reflush a deleted row or roll back the lifecycle command. A
cached verifier whose parent state has already disappeared is detached conservatively because its
former owner can no longer be established; a cached verifier with a live parent owned by another
account remains attached and untouched. Classification uses ORM identity metadata rather than
mapped attributes, so an `expire_on_commit` session can be safely reused after a competing
transaction consumes the cached state.

## Concurrency contract

OAuth issuance, consumption, replacement, and disconnect all serialize through the provider
account lock.

- If issuance commits first, a waiting lifecycle command cancels the newly committed state.
- If replacement or disconnect commits first, a later authorization start may issue a new state
  representing a new user intent.
- If callback consumption commits first, a waiting lifecycle command advances the revision after
  the one-time state is consumed; the future claimed handoff will therefore be stale.
- If replacement or disconnect commits first, a waiting consumer observes the committed deletion
  and fails uniformly.
- If the lifecycle command rolls back, its cancellation and revision change disappear together.
- No cross-account state is deleted or blocked by the cancellation predicate.

This ordering removes the known state-issued-before-lifecycle race without adding an issuance
revision column.

## Verification

PostgreSQL tests cover account-wide cancellation for replacement and disconnect, PKCE cascade
deletion, tenant isolation, caller rollback, failure preservation, pending-ORM mutation rejection,
single-bind enforcement, cached-identity eviction before and after competing consumption (including
verifier-only and expired reused-session caches), and deterministic issuance- and
consumption-versus-lifecycle ordering with the expected PostgreSQL blocker.
Existing connection encryption, revision, transaction ownership, OAuth issuance, consumption,
replay, and secret-hygiene suites remain authoritative.

## Deferred work

- Sealed callback claim handoff and claim-time revision advancement.
- Commit-before-network callback orchestration.
- Hard-deadline token exchange composition.
- Fresh post-network initiating-session and expected-revision validation.
- Atomic encrypted credential finalization.
- Browser-binding cookie lifecycle, routes, redirects, rollout mode, OpenAPI, and settings UI.

Callback routes remain prohibited until the claim, network-gap finalizer, and coordinated route
lifecycle are implemented and tested.

## Non-goals

- Models, migrations, new columns, triggers, or cleanup jobs.
- Authorization-code input, Google networking, token exchange, refresh, or FreeBusy ingestion.
- Credential finalization, routes, cookies, redirects, feature gates, OpenAPI, or frontend.
- Calendar writes, event details, multiple calendars, or additional providers.
