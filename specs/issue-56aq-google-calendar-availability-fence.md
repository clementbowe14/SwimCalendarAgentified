# Issue 56aq: credential-free availability lifecycle fence

## Goal

Capture and later revalidate one Google Calendar connection generation without
loading credential material. This is a route-inert prerequisite for the lazy
availability source and keeps database race handling separate from provider
composition.

## Contract

`claim_google_calendar_availability_fence()` owns a fresh SQLAlchemy session
and transaction. It locks the provider account selected by trusted instructor
ID before locking that account's optional calendar connection. It returns one
immutable, redacted, non-serializable `GoogleCalendarAvailabilityFence` whose
only public value is the semantic lifecycle state: `disconnected`,
`connected`, or `authorization_required`.

The fence privately captures only:

- account and instructor identity;
- calendar connection revision;
- connection identity, when present; and
- semantic lifecycle state.

It never selects credential version, key ID, nonce, ciphertext, refresh token,
deployment configuration, or provider event data.

`revalidate_google_calendar_availability_fence()` owns a second fresh session
and transaction, repeats the same account-before-connection lock order, and
returns `None` only when account identity, instructor identity, enabled-owner
state, revision, connection identity, Google provider, and semantic state are
unchanged. A missing, disabled, reassigned, corrupt, or stale owner or
connection fails closed.

Both boundaries require owned, idle, non-autocommit sessions whose account and
connection mappers share one engine. Every session is closed. Ordinary input,
database, transaction, commit, rollback, and cleanup failures become a fresh,
cause-free fixed-message fence error. Cancellation and process controls are
re-emitted as fresh values after private references are cleared.

## Race behavior

Exact revalidation detects:

- disconnected-to-connected and connected-to-disconnected transitions;
- connection replacement, including connection-ID ABA;
- repeated disconnect revision advances while already disconnected;
- authorization-required marking despite its unchanged account revision;
- OAuth callback replacement or revision advancement; and
- owner disable, deletion, or ownership change.

The fence never holds database locks across configuration loading, credential
decryption, token refresh, or Google I/O. The later availability source will
perform provider work between claim and revalidation.

## Files and non-goals

This slice is limited to:

- `app/google_calendar_availability_fence.py`;
- focused unit and PostgreSQL race tests; and
- this specification.

It does not create a provider source, load secrets, query Google, change
routes, application state, OpenAPI, availability or booking behavior,
settings, frontends, OAuth lifecycle commands, or credential storage. The lazy
availability adapter and coach/public route composition remain separate PRs.
