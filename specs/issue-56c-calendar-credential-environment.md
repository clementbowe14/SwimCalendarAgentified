# Issue #56c: calendar credential key-ring environment contract

## Goal

Load the issue #56b credential cipher from deployment secrets through one strict, call-time factory.
This slice defines configuration and rotation behavior without activating calendar credentials at
application startup or adding persistence, OAuth, routes, or frontend behavior.

## Environment contract

`calendar_credential_cipher_from_env()` reads these required variables on every call:

```text
SWIM_CALENDAR_CREDENTIAL_ACTIVE_KEY_ID=k2026_11
SWIM_CALENDAR_CREDENTIAL_KEYS={"k2026_08":"<base64url-key>","k2026_11":"<base64url-key>"}
```

`SWIM_CALENDAR_CREDENTIAL_KEYS` is a JSON object mapping immutable key IDs to the canonical,
unpadded Base64URL encoding of exactly 32 random bytes. A valid encoded value is exactly 43 ASCII
characters. The active key ID selects the key used for new encryption; every other retained key is
decrypt-only.

There is no default development or test key. The factory returns an immutable, redacted
`CalendarCredentialCipher` directly. Decoded keys never enter `ApplicationSettings`, readiness state,
logs, or another normally represented object, and the factory does not cache secret material.

## Strict validation

Configuration fails closed when either variable is missing or blank, or when the key-ring value:

- exceeds 4 KiB before JSON parsing;
- is malformed JSON, is not an object, is empty, contains duplicate IDs, or contains more than eight
  keys;
- contains an invalid key ID, non-string value, duplicate decoded key material, or omits the active
  key;
- contains padding, whitespace, standard Base64 characters, invalid characters, a noncanonical
  representation, or a value that does not decode to exactly 32 bytes; or
- uses JSON non-finite values such as `NaN` or `Infinity`.

Base64URL values are checked for exact shape, decoded with strict validation, and re-encoded for an
exact canonical comparison. JSON duplicate IDs are rejected instead of accepting the normal
last-value-wins behavior.

Every parser, decoder, environment, or cipher-construction failure becomes the existing fixed,
cause-free `CalendarCredentialConfigurationError`. Error strings, representations, formatted
tracebacks, reachable exception context, traceback-frame locals, and logs contain no environment
value, key ID, encoded key, decoded key, or parser detail.

## Rotation procedure

1. Add a new immutable key ID while retaining the old key.
2. Deploy the complete ring, then select the new active key ID.
3. Encrypt new credentials with the active key; use retained keys only to decrypt old envelopes.
4. Rewrap old envelopes with a fresh nonce and the active key.
5. Verify that no stored envelope references the retiring key ID.
6. Only then remove the retired key.

A key ID must never be renamed or reused with different material because the ID is authenticated with
the credential envelope. The parser cannot prove that persisted envelopes have been rewrapped, so a
successful parse never means that removing a retained key is safe.

## Verification

Pure unit tests cover a working single-key ring, live environment rereads, two-key rotation,
canonical encoding boundaries, every malformed input class above, duplicate IDs and material,
bounded parsing, fixed cause-free errors, secret-free diagnostics, and unchanged application startup
when calendar configuration is absent or malformed.

## Deferred slices

1. Persist one encrypted provider connection and single-use OAuth state per provider account.
2. Activate configuration when the first credential persistence path is enabled, failing deployed
   startup before serving when its required key ring is invalid.
3. Add OAuth authorization, callback, status, disconnect, Google refresh, and FreeBusy transport.

## Non-goals

- Import-time parsing, global secret caching, default or generated application keys.
- Changes to application startup, `ApplicationSettings`, readiness, models, migrations, routes,
  OpenAPI, Google networking, or frontend code.
- Deciding whether a retained key can be safely removed from a deployment.
