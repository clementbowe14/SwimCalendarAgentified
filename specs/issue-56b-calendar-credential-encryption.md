# Issue #56b: external-calendar credential encryption boundary

## Goal

Define the authenticated-encryption boundary used by later Google Calendar connection persistence.
This slice remains database-free, environment-free, and route-free so the security contract can be
reviewed independently before any refresh credential can enter the application.

## Security contract

`app.calendar_credentials` provides:

- `CalendarCredentialScope`, binding a credential to one trusted instructor, provider, and generated
  connection ID;
- `EncryptedCalendarCredential`, an opaque versioned storage envelope containing only a key ID,
  random nonce, and ciphertext;
- `CalendarCredentialCipher`, an immutable AES-256-GCM key ring that encrypts with one active key and
  can decrypt with retained inactive keys during rotation; and
- fixed-message configuration, input, encryption, and decryption errors.

Each key is exactly 32 bytes. Every encryption uses a fresh 12-byte nonce and AES-GCM's 128-bit
authentication tag. Authenticated associated data includes a domain separator, envelope version, key
ID, instructor ID, provider, and connection ID using unambiguous length-prefix encoding. Moving an
envelope to another instructor, provider, connection, version, or key ID therefore fails closed.

The boundary accepts only the `google` provider for this pilot. Plaintext refresh credentials are
non-empty UTF-8 strings capped at 16 KiB. Key rings and external values have strict cardinality and
length limits.

The implementation follows the `cryptography` AES-GCM contract:
<https://cryptography.io/en/stable/hazmat/primitives/aead/>.

## Secret handling

- Plaintext refresh credentials and encryption keys never appear in an envelope.
- Envelope and cipher representations are fully redacted.
- The module performs no logging.
- Unknown key IDs, authentication failures, malformed envelopes, and wrong provider scopes produce
  the same fixed decryption error.
- Rewrap checks authenticate the complete envelope and provider scope before trusting its key ID.
- Cryptographic-library exceptions are replaced by cause-free fixed errors so provider or credential
  material cannot enter formatted tracebacks.
- Constructor inputs are defensively copied into an immutable mapping.
- Retained old-key envelopes can be rewrapped with a fresh nonce before the old key is removed.

## Verification

Pure unit tests cover Unicode round trips, nonce uniqueness, scope binding, envelope tampering, key
rotation, malformed configuration and envelopes, size limits, defensive copies, redacted
representations, indistinguishable decryption failures, and exception/log secrecy. These tests require
neither PostgreSQL nor network access.

## Deferred slices

1. Parse and validate the key ring from deployment secrets when credential persistence is enabled.
2. Add provider-connection and single-use OAuth-state persistence using this envelope.
3. Implement OAuth authorization, callback, status, and disconnect workflows.
4. Implement Google token refresh and FreeBusy transport behind the issue #56a boundary.
5. Add settings UI and consume verified busy periods in availability and agenda workflows.

Startup configuration is intentionally deferred: no route can currently store or read a calendar
credential, so requiring keys globally in this slice would break existing environments without
protecting an active credential path.

The direct cryptographic dependency is pinned to `cryptography==50.0.2`; dependency updates must be
reviewed explicitly rather than silently changing this security boundary during an unrelated build.

## Non-goals

- Reading encryption keys from environment variables or secret managers.
- Models, migrations, OAuth, Google API calls, routes, OpenAPI, or frontend changes.
- Logging, exposing, or inspecting credential contents.
- Writing appointments through Google Calendar, multiple calendars, or non-Google providers.
