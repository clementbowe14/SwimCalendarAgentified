# Issue #56p: calendar OAuth browser-binding compatibility fences

## Goal

Protect browser-bound Google OAuth states from every existing unbound repository command before a
later slice begins writing browser bindings. This compatibility-only slice changes no public
signature or result and adds no bound issue/consume command, secret generation, hashing, cookie,
route, Google request, configuration, OpenAPI contract, or frontend behavior.

All existing commands continue to issue and consume unbound rows exactly as before. A row whose
`browser_binding_digest` is present is reserved for the future binding-aware commands and must not
be replaced, deleted, decrypted, or returned by an older path.

## Issuance fences

Both existing issue commands retain the account-then-session lock order and active-owner checks.
Before requesting state entropy or generating PKCE material, they inspect the current owner state:

- `issue_google_calendar_oauth_state()` rejects a verifier-backed or browser-bound state with the
  existing fixed `CalendarOAuthStatePersistenceError`.
- `issue_google_calendar_oauth_state_with_pkce()` rejects a browser-bound state with the same fixed
  persistence error. It may continue replacing an unbound verifier-backed state as before.

Each owner-state deletion also includes an authoritative
`browser_binding_digest IS NULL` predicate. If a binding appears after the precheck, the deletion
cannot remove it; the existing unique-owner constraint prevents the replacement insert, the nested
savepoint rolls back, and issuance fails closed while preserving the bound row.

## Consumption fences

Both existing consumers retain their public invalid-state behavior for a browser-bound bearer:

- `consume_google_calendar_oauth_state()` adds
  `browser_binding_digest IS NULL` to its final conditional `DELETE ... RETURNING`.
- `consume_google_calendar_oauth_state_with_pkce()` requires a null binding when it locks the state,
  so a bound row cannot reach verifier lookup or decryption. Its final conditional parent deletion
  repeats the null-binding predicate as the authoritative mutation boundary.

A rejected callback raises the existing fixed `CalendarOAuthStateInvalid`, mutates no parent or
PKCE child, and returns no owner or verifier material. The predicates remain inside the mutation or
locked-read statements rather than trusting the earlier owner lookup, preserving safety when a
binding is attached concurrently.

## Locking and transaction behavior

The commands still require a caller-owned explicit SQLAlchemy transaction and never commit, roll
back, open another session, log, or perform network I/O. The established global lock order remains
account, session, state, verifier. No new lock is introduced.

The schema's unique owner constraint and the new null-binding delete predicates are the
authoritative issuance race boundary. Consumption continues to rely on conditional row mutation;
PKCE consumption additionally locks the eligible unbound parent before touching its child.

## Verification

Focused PostgreSQL tests prove that all four legacy commands reject a browser-bound row, preserve
the exact parent and optional PKCE child, and retain their fixed public errors. Issuance tests prove
that static bound rows are rejected before state entropy or PKCE generation. Consumption tests
prove that PKCE decryption is not attempted. Deterministic two-transaction tests cover binding
commit and rollback against all four old commands, including stale consume owner discovery. A
stale-issuance-precheck test reaches the guarded delete and proves the post-integrity binding
recheck wins before collision retry. Successful unbound-consume tests also inspect the emitted
conditional SQL.

The existing OAuth state, PKCE issuance, and PKCE consumption suites continue proving unchanged
unbound behavior, collision handling, transaction recovery, concurrency, redaction, and lock order.

## Rollout

Deploy this slice to every application instance before enabling any browser-bound writer. The later
bound issue/consume commands may be added only after these fences are universal. Rollback disables
new bound starts first, retains binding-aware consumption for at least the maximum ten-minute state
lifetime, and must not restore an unfenced binary while any live bound row can exist.

## Deferred slices

1. Atomically issue state, encrypted PKCE material, and an independently generated browser binding.
2. Consume a bound state only after matching and locking its browser binding, then release the PKCE
   verifier.
3. Add the start/callback cookie and route workflow, including forwarded-link and browser/account
   swapping tests.
4. Exchange the authorization code, validate granted scope, and replace the encrypted Google
   connection.

## Non-goals

- Browser-secret generation, domain-separated hashing, bound command inputs/results, or collision
  retries for binding digests.
- Schema or migration changes.
- Cookies, sessions changes, routes, redirects, feature gates, OpenAPI, or UI.
- Google client secrets, authorization codes, token exchange/refresh, FreeBusy networking, or
  calendar writes.
- Multiple calendars, other providers, metrics, audit events, or cleanup jobs.
