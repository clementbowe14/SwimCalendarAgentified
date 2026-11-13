# Issue #56k: calendar OAuth PKCE environment configuration

## Goal

Construct the issue #56j `CalendarOAuthPKCECipher` from the existing calendar root-key deployment
contract. This slice completes configuration wiring only. It does not activate PKCE at application
startup or add state persistence, authorization URLs, callbacks, routes, Google requests, OpenAPI,
or frontend behavior.

## Environment contract

`calendar_oauth_pkce_cipher_from_env()` reads these existing values on every call:

```text
SWIM_CALENDAR_CREDENTIAL_ACTIVE_KEY_ID=<active-key-id>
SWIM_CALENDAR_CREDENTIAL_KEYS={"<key-id>":"<canonical-base64url-root>"}
```

The key-ring JSON maps one through eight immutable key IDs to canonical, unpadded Base64URL
encodings of exactly 32 bytes. The active ID must be present. The same root-key contract
intentionally supports both durable calendar credentials and short-lived PKCE verifiers. PKCE never
uses a root directly: `CalendarOAuthPKCECipher` derives a purpose- and key-ID-specific AES-256 key
with HKDF-SHA256 as defined in issue #56j.

No second PKCE-specific environment key is introduced. Splitting independently rotated root rings
would complicate deployment without improving cryptographic separation, which the mandatory HKDF
boundary already provides.

## Shared private parser

`app.config` parses the environment through one private root-key helper. The existing
`calendar_credential_cipher_from_env()` passes the decoded roots to `CalendarCredentialCipher`; the
new `calendar_oauth_pkce_cipher_from_env()` passes them to `CalendarOAuthPKCECipher` as `root_keys`.

Together, the shared parser and each purpose-specific cipher preserve the existing credential
configuration rules:

- a 4 KiB maximum JSON payload checked before parsing;
- one active member and one through eight entries;
- duplicate JSON-key and non-finite JSON-constant rejection;
- exact 43-character canonical unpadded Base64URL encodings;
- exactly 32 decoded bytes per root; and
- printable valid key IDs and unique root material, enforced by each cipher boundary.

The helper, decoded roots, and raw environment values remain private. They are not cached, exported,
logged, placed in `ApplicationSettings`, attached to application state, or evaluated during startup.
Every factory call returns a fresh cipher based on the current environment.

## Failure and confidentiality boundary

Every PKCE parser, environment, decoding, validation, HKDF, or constructor failure becomes the same
cause- and context-free `CalendarOAuthPKCEConfigurationError` with its existing fixed message. The
public error is raised outside the caught dependency failure, so its traceback and reachable frame
locals contain no raw JSON, key ID, encoded root, decoded root, derived key, or parser detail.

The parser extraction preserves the existing fixed `CalendarCredentialConfigurationError` boundary
for the credential factory. Neither public factory logs configuration failures.

## Rotation

New PKCE envelopes use only the active key ID. A cipher can decrypt an envelope created with any
retained root. Operators must keep an old root configured until both conditions are true:

1. durable calendar credentials using it have been rewrapped; and
2. every pending OAuth state using it has been consumed or passed the ten-minute maximum lifetime.

Key IDs must remain bound to the same root material on every instance. Reassigning an existing ID is
not rotation and causes authentication failures.

## Verification

Pure tests cover working PKCE generation/decryption from the environment, the shared credential and
PKCE contract, call-time rereads, active/retained-key rotation, strict malformed-input rejection,
pre-parse size checks, constructor-failure sanitization, redacted errors/tracebacks/logs, and inert
application startup when configuration is missing or malformed. Existing credential configuration
tests remain unchanged and verify that extracting the shared parser does not alter that boundary.

## Deferred slices

1. Atomically issue one OAuth state and its encrypted verifier child, then return the public
   challenge for fixed authorization URL construction.
2. Add a PKCE-aware callback consume operation that locks and decrypts the child before deleting its
   parent; the current state consumer would cascade-delete the child too early.
3. Exchange the authorization code with the decrypted verifier, validate granted scopes, and replace
   the encrypted Google connection.

The later runtime activation still requires issue #56i's schema-first deployment, old-writer
shutdown, ten-minute state drain, and feature-gated cutover.

## Non-goals

- New environment variables, secret-manager integration, caching, startup/readiness activation, or
  key-ring exposure.
- SQLAlchemy models, migrations, state/verifier repository changes, or transaction behavior.
- Authorization URLs, client secrets, callback routes, code exchange, token refresh, or networking.
- Google FreeBusy queries, scheduler integration, OpenAPI, settings UI, or frontend changes.
