# Issue #56w: hard overall Google Calendar token-exchange deadline

## Goal

Close the slow-operation gap in the concrete Google Calendar authorization-code exchange. The
existing HTTPX timeout applies independently to connect, pool, write, and read inactivity, so a
provider that continues making partial progress can exceed five seconds overall. This slice adds
one cancellation-capable, monotonic five-second deadline around the complete concrete exchange.

The slice remains internal and runtime-inert. It adds no OAuth route, callback parser, browser
cookie, rollout gate, environment loading, database access, credential persistence, OpenAPI
contract, or frontend behavior.

## Public contract

`exchange_google_calendar_authorization_code_with_deadline()` is the only new exported symbol. It
is an async, keyword-only service accepting the existing exact
`GoogleCalendarTokenExchangeConfiguration`, one authorization code, and one PKCE verifier. It
returns the existing sealed `GoogleCalendarTokenExchangeResult` containing only the validated
refresh token.

The existing synchronous transport-injected exchange and concrete synchronous HTTPX adapter remain
unchanged for compatibility and focused protocol testing. The new service owns its exact private
async HTTPX path; callers cannot inject a transport, endpoint, timeout, retry policy, redirect
policy, proxy, client, event hook, or response parser.

Input validation and form preparation complete before a timeout scope or network object is created.
The authorization code, client secret, verifier, and form remain bounded by the existing exchange
contract. The provider response is completed through the existing strict response validator, so
scope, token type, refresh-token presence, JSON shape, and response bounds have one authority.

## Deadline and cancellation contract

After validation, the service snapshots the running event loop's monotonic clock and computes one
fixed deadline exactly five seconds later. One AnyIO cancel scope with level cancellation covers:

1. async HTTP transport and client construction;
2. connect, TLS, request write, and response-header work;
3. bounded raw response streaming;
4. response and client closure; and
5. strict provider-response validation.

The existing five-second HTTPX connect, pool, write, and read timeouts remain defense in depth. The
outer deadline is authoritative. After it fires, every later async cleanup checkpoint remains
cancelled. HTTPcore shields connection cleanup from outer cancellation, so the exact production
transport also wraps every connected network stream with the same absolute deadline. Stream close
uses an inner deadline scope that remains effective inside HTTPcore's shield and force-closes the
underlying AnyIO resource when no time remains. Response, client, pool, or socket closure therefore
cannot begin an unbounded post-deadline wait. A response is rejected when cancellation wins, when
the cancel scope reports expiry, or when the monotonic clock reaches the deadline before a result
is accepted. This final check rejects a dependency that briefly suppresses cancellation and returns
late.

The implementation uses native async HTTPX I/O. It does not place the synchronous transport in a
worker thread, because returning from a thread wait cannot cancel an in-flight secret-bearing
request. It uses no process-global signal, background task, retry, redirect, or detached worker.

An external task cancellation remains cancellation rather than becoming a provider failure. It is
re-raised as a fresh, cause-free `asyncio.CancelledError` only after secret-bearing private frames
have unwound. The internal deadline and all other failures become the existing fixed, cause-free
`GoogleCalendarTokenExchangeError`.

## Network and resource policy

The private async adapter preserves the merged concrete transport policy:

- exact `https://oauth2.googleapis.com/token` endpoint and one ordered form POST;
- verified TLS through an explicit environment-free CA context with key logging disabled;
- `trust_env=False`, `proxy=None`, HTTP/1.1 only, redirects disabled, and zero retries;
- one non-reusing HTTPcore connection pool with a deadline-aware AnyIO network backend;
- no query parameters, authentication header, cookies, hooks, compression, or shared pool;
- exact request and response size limits;
- no provider error-body reads for non-200 responses; and
- one short-lived response, client, and transport closed on success, failure, timeout, and external
  cancellation.

No request, response, client, task, or transport survives a completed call. Concurrent calls own
independent clients, transports, timeout scopes, and cancellation state.

## Confidentiality and diagnostics

The client secret, authorization code, verifier, encoded form, access token, refresh token,
provider body, URL-bearing request object, and dependency exception text never appear in exported
messages, representations, cause/context chains, public traceback-frame locals, or logs. The module
performs no logging.

Internal deadline failures use a newly constructed `GoogleCalendarTokenExchangeError`. External
cancellation uses a newly constructed `CancelledError`. Both dedicated raisers clear cause,
context, and ambient caller exception context after the public wrapper has deleted its
configuration, code, and verifier references.

## Verification

Pure tests use in-memory async HTTPX transports and streams and never contact Google. They cover:

- success through the existing strict response validator;
- exact one-attempt async request and network policy;
- invalid input rejection before timeout or network construction;
- cancellation of a stalled request and stalled response stream;
- slow-progress streaming that exceeds the overall deadline;
- response, client, and transport closure on timeout and external cancellation;
- bounded socket cleanup from inside an HTTPcore-style shield;
- rejection of a late result after cancellation was briefly suppressed;
- fixed secret-free deadline diagnostics and sanitized external cancellation;
- isolation between concurrent calls and recovery on a later call; and
- runtime-inert imports, application construction, and startup.

Existing OAuth configuration, authorization, PKCE, token-exchange, concrete sync transport,
credential, route, and API-contract suites remain unchanged.

## Deferred slices

1. Add callback orchestration that commits browser-bound state consumption before network access.
2. Revalidate the exact initiating provider session after the network gap, then encrypt and replace
   credentials in a fresh explicit transaction.
3. Add the host-only browser cookie, start/callback routes, and coordinated
   `disabled|enabled|drain` rollout mode.
4. Refresh credentials and query normalized opaque Google FreeBusy intervals.
5. Integrate fail-closed external busy periods and expose settings/recovery behavior.

## Non-goals

- OAuth routes, cookies, redirects, callback parsing, authentication middleware, or rollout gates.
- State consumption, sessions, transactions, credential encryption/replacement, or disconnect.
- Environment loading, startup activation, shared clients, connection pools, or live-network tests.
- Token refresh, revocation, FreeBusy requests, event details, calendar writes, or other providers.
- Retries, backoff, provider-error classification, metrics, audit events, OpenAPI, or UI.
