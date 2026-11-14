# Issue #56u: bounded HTTPX Google Calendar token transport

## Goal

Provide the concrete network adapter for the already-defined Google Calendar authorization-code
exchange port. This slice performs one fixed, bounded HTTPX form POST and remains inert until a
later callback explicitly supplies the adapter to the exchange service. It adds no route, cookie,
environment loading, startup activation, database access, credential persistence, OpenAPI
contract, or frontend behavior.

## Public contract

`HttpxGoogleCalendarTokenTransport` is a final, stateless implementation of
`GoogleCalendarTokenTransport`. It has no configurable constructor, retained client, connection
pool, request data, or shared mutable state. Each `post_form()` call validates the existing
exchange contract before constructing any network object.

The URL must be the exact built-in string:

```text
https://oauth2.googleapis.com/token
```

The fields must be an exact tuple of exact two-tuples in this order:

1. `code`
2. `client_id`
3. `client_secret`
4. `redirect_uri`
5. `grant_type=authorization_code`
6. `code_verifier`

Every key and value must be an exact nonblank visible-ASCII string without surrounding
whitespace. Before any string copy or form encoding, values are bounded to the limits already
enforced by the exchange boundary: 8 KiB for the code, 512 bytes for the client ID, 4 KiB for the
secret, 2 KiB for the redirect URI, the fixed grant value, and 43 bytes for the verifier. The
timeout must be the exact built-in float `5.0`. The ordered fields are encoded with the
standard-library `application/x-www-form-urlencoded` encoder and sent as bytes. The encoded
request body is capped at 64 KiB. This avoids HTTPX 0.28's unsupported tuple-form path and prevents
a dictionary conversion from silently collapsing duplicate fields.

## Network policy

Every call creates one short-lived HTTPX transport and client with these fixed properties:

- verified TLS and normal CA-backed hostname verification through an explicit environment-free
  SSL context;
- TLS key logging disabled even when `SSLKEYLOGFILE` is present;
- `trust_env=False`, so proxy and certificate environment variables are ignored;
- `proxy=None`;
- HTTP/1.1 enabled and HTTP/2 disabled;
- transport retries set to zero;
- redirects disabled on both the client and request;
- one scalar five-second HTTPX timeout; and
- exactly one POST attempt.

The scalar HTTPX timeout applies independently to connect, pool acquisition, write, and read
inactivity. It is not a five-second total wall-clock deadline. This adapter must remain runtime
inert until callback orchestration supplies a hard overall deadline; that activation boundary is
not introduced here.

The request advertises `Accept: application/json` and `Accept-Encoding: identity`, and uses the
exact `Content-Type: application/x-www-form-urlencoded`. OAuth secrets appear only in the request
body. The adapter supplies no query parameters, authentication header, cookies, event hooks, or
application logs.

## Response policy

Responses are handled through HTTPX's streaming context manager, and the response, client, and
underlying transport are closed on every success or failure path. A client-construction failure
also explicitly closes a transport whose ownership was not transferred.

For a non-200 response, the provider body is never iterated or retained. The adapter returns only
the status with `content_type=None` and `body=b""`; the existing exchange boundary rejects the
status.

For an HTTP 200 response:

- `Content-Encoding` must be absent or one exact case-insensitive `identity` value;
- when present, `Content-Length` must be one unambiguous decimal value no larger than 64 KiB;
- an oversized or malformed declared length fails before body iteration;
- raw identity bytes are streamed in 8 KiB chunks and rejected before accumulation exceeds
  65,536 bytes;
- a declared length must equal the final streamed length; and
- the bounded bytes and provider `Content-Type` are returned in the existing redacted transport
  response container.

Compressed responses are rejected before body iteration to prevent decompression amplification.
JSON, media-type, token, scope, and provider-payload validation remains solely in the existing
exchange boundary.

## Failure and confidentiality policy

All caller, encoding, HTTPX, protocol, streaming, and cleanup failures become a newly constructed,
fixed, cause-free `GoogleCalendarTokenExchangeError`. Secret-bearing work occurs only in private
helpers. The public method deletes its URL, fields, and timeout before invoking a dedicated error
raiser that clears cause, context, and ambient caller exception context.

The client secret, authorization code, verifier, encoded form, provider body, HTTPX request, and
transport exception details never appear in exported messages, representations, cause/context
chains, public traceback-frame locals, or logs.

## Verification

Pure tests use an in-memory HTTPX base transport and recording byte streams; they never contact
Google. They cover the exact request, form ordering and escaping, timeout extensions, fixed network
options, redirect and retry refusal, pre-network input rejection, non-200 body suppression,
content encoding and length validation, exact response-size boundaries, chunked overflow, resource
closure, fixed secret-free diagnostics, ambient exception context, and successful composition
through `exchange_google_calendar_authorization_code()`.

Import, application construction, and startup remain network-free. Existing OAuth configuration,
authorization, PKCE, exchange, credential, route, and API-contract suites remain unchanged.

## Deferred slices

1. Add start/callback orchestration, commit-before-network handling, and cookie cleanup.
2. Encrypt and atomically replace the Google connection in a fresh transaction.
3. Refresh credentials and query normalized opaque FreeBusy intervals.
4. Integrate fail-closed external busy periods and expose settings/recovery behavior.

## Non-goals

- OAuth routes, callback parsing, cookies, authentication, or redirects.
- State consumption, sessions, transactions, credential encryption, or persistence.
- Token refresh, revocation, FreeBusy requests, calendar writes, or other providers.
- Retries, backoff, provider-error classification, or recovery UX.
- Shared clients, reusable pools, compressed responses, or a total wall-clock deadline.
- Custom proxies, CA bundles, mutual TLS, certificate pinning, or live-network tests.
- Requirements, migrations, OpenAPI, frontend, feature flags, metrics, or audit events.
