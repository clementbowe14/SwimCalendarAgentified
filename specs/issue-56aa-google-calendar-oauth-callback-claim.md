# Issue #56aa: Google Calendar OAuth callback claim

## Goal

Create the smallest committed handoff between a browser-bound Google OAuth callback and later
network orchestration. A successful claim consumes the one-time state and encrypted PKCE verifier,
advances the provider account's calendar-connection revision, commits both changes, releases every
database lock, and only then returns the authenticated owner, verifier, and expected revision.

This slice is internal and route-inert. It adds no callback route, cookie, authorization-code
parsing, Google request, configuration loading, credential persistence, schema, OpenAPI contract,
startup behavior, or frontend flow.

## Public contract

`claim_google_calendar_oauth_callback()` accepts:

- a SQLAlchemy session factory owned by the service;
- the raw OAuth state returned by Google;
- the independent raw browser-binding value;
- an exact `CalendarOAuthPKCECipher`; and
- an optional aware operation time.

It returns a sealed `GoogleCalendarOAuthCallbackClaim` containing exactly:

- the persisted provider account ID;
- the persisted initiating session ID;
- the authenticated plaintext PKCE verifier; and
- the expected calendar-connection revision captured by this claim.

The handoff is final, slotted, immutable, builder-only, redacted, and non-serializable. It contains
no raw state, browser binding, digest, authorization code, token, provider response, configuration,
credential envelope, connection row, session object, or ORM object.

Every failure is the same fixed, cause-free `GoogleCalendarOAuthCallbackClaimError`. Malformed,
unknown, expired, replayed, browser-mismatched, inactive-owner, exhausted-revision, persistence,
commit, and close failures disclose no distinction or secret.

## Input and session boundary

Both callback bearers must be canonical unpadded 43-character Base64URL encodings of exactly 32
bytes and must differ. The cipher type and optional aware operation time are validated before a
database session is created.

The factory must return a fresh SQLAlchemy `Session` with no active transaction, backed directly by
one `Engine`. Provider account, provider session, OAuth state, and PKCE verifier mappers must all
resolve to that same engine. Externally transactional connections and split mapper binds fail before
state consumption. The owned session is always closed.

## Transaction and lock order

One service-owned root transaction performs the complete claim:

1. call the existing browser-bound PKCE consumer;
2. preserve its account, initiating-session, state, and verifier lock order;
3. validate the exact consumed snapshot;
4. refresh and re-lock the already-held provider-account row;
5. fail closed if its PostgreSQL `BIGINT` revision is exhausted;
6. advance `calendar_connection_revision` exactly once and flush; and
7. commit the state deletion, verifier cascade, and revision increment together.

The consumer's nested transaction is only a savepoint. The service never performs Google I/O while
the root transaction or any database lock is active. The sealed claim is constructed only after the
commit succeeds and the session closes.

Rollback before commit restores the state, verifier, and previous revision. Commit uncertainty or a
post-commit close failure returns no handoff; the caller must start a fresh authorization and must
not compensate, revive state, or retry an authorization code.

## Concurrency contract

- Two callbacks for the same state have exactly one successful claim and one revision increment.
- Different valid states for one account serialize on the account lock and receive consecutive
  revisions; the later claim makes the earlier handoff stale.
- If a claim commits before replacement or disconnect, that later lifecycle command advances the
  revision again and invalidates the handoff.
- If replacement or disconnect commits first, issue #56z deletes the pending state and the claim
  fails without another increment.
- A later session revocation or account security change may invalidate an already committed claim;
  the future finalizer must revalidate the exact initiating session after the network gap.
- Claims for other provider accounts remain isolated.

The claim does not delete sibling states. A later successful sibling claim intentionally supersedes
an earlier handoff through the shared revision fence.

## Verification

Pure tests cover the exact public surface, validation before session creation, owned transaction and
single-engine enforcement, commit/close-before-return ordering, sealed-handoff behavior, failure
sanitization, and runtime-inert imports.

PostgreSQL tests cover committed deletion and cascade, exact revision advancement, replay,
rollback and commit failure, exhaustion, inactive sessions, account isolation, concurrent same- and
different-state claims, and both claim-versus-replacement and claim-versus-disconnect orderings.
Every concurrency case forces overlap and verifies the exact blocking PostgreSQL backend.

Existing browser-bound consumption, lifecycle cancellation, connection revision, encryption,
OAuth-start, token-exchange, and deadline suites remain authoritative.

## Deferred work

1. Compose the committed claim with authorization-code exchange under the hard network deadline.
2. Open a fresh transaction after the network gap, revalidate the exact initiating session and
   expected revision, then atomically encrypt and install the refresh credential.
3. Add the host-only browser cookie, start/callback routes, cookie cleanup, and coordinated
   disabled/enabled/drain rollout mode.
4. Refresh credentials and query normalized opaque Google FreeBusy intervals.
5. Integrate fail-closed external busy periods and expose settings and recovery behavior.

Callback routes remain prohibited until the post-network finalizer and coordinated route lifecycle
are implemented and tested.

## Non-goals

- Models, migrations, columns, constraints, triggers, or cleanup jobs.
- Authorization codes, Google networking, token exchange, refresh tokens, or credentials.
- Session revocation, password changes, connection replacement, or disconnect behavior changes.
- Routes, cookies, redirects, rollout gates, environment loading, OpenAPI, or frontend UI.
- FreeBusy ingestion, event details, calendar writes, multiple calendars, or other providers.
