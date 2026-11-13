# Issue #56j: Google Calendar OAuth PKCE cryptography boundary

## Goal

Define the pure cryptographic boundary for one Google Calendar OAuth Proof Key for Code Exchange
(PKCE) transaction. This slice generates an RFC 7636 `S256` challenge while protecting the matching
verifier in the encrypted envelope introduced by issue #56i. It remains independent from environment
configuration, persistence, OAuth URLs, callbacks, routes, and Google networking so its protocol and
secret-handling contract can be reviewed on its own.

## Public boundary

`app.calendar_oauth_pkce` exports:

- `CalendarOAuthPKCEScope`, binding one verifier to the SHA-256 state digest, provider account,
  provider session, and Google provider;
- `EncryptedCalendarOAuthPKCEVerifier`, the opaque versioned envelope stored by a later repository;
- `GeneratedGoogleCalendarPKCE`, containing only the public `S256` challenge, the fixed method, and
  encrypted verifier envelope—never the plaintext verifier;
- `CalendarOAuthPKCECipher`, an immutable root-key ring that derives purpose-specific PKCE keys,
  generates and encrypts new verifier material, and decrypts it for a later token exchange;
- `derive_s256_code_challenge`, a strict deterministic implementation of the RFC transformation; and
- fixed-message configuration, input, generation, encryption, and decryption errors.

All scope, cipher, envelope, and generated-result representations are redacted. There is no
caller-selectable challenge method and no `plain` fallback.

## Verifier and challenge profile

Every authorization attempt uses a new, independent 32-byte cryptographically random value. Its
canonical unpadded Base64URL encoding is exactly 43 ASCII characters matching
`[A-Za-z0-9_-]{43}`. Validation decodes the value to exactly 32 bytes and requires an identical
canonical re-encoding. Padding, whitespace, Unicode, noncanonical trailing bits, shorter or longer
RFC forms, and other unreserved characters are rejected deliberately because the persisted version-1
envelope is fixed to a 43-byte plaintext and 59-byte AES-GCM ciphertext.

The public challenge is:

```text
BASE64URL_NO_PADDING(SHA256(ASCII(code_verifier)))
```

and the method is always `S256`. The RFC recommends 32 random octets encoded to 43 characters and
requires capable clients to use `S256`; OAuth Security BCP applies PKCE guidance to confidential web
clients and identifies `S256` as the method that does not expose the verifier. See
[RFC 7636 sections 4.1–4.2](https://www.rfc-editor.org/rfc/rfc7636.html#section-4.1) and
[RFC 9700 section 2.1.1](https://www.rfc-editor.org/rfc/rfc9700.html#section-2.1.1).

## Purpose-specific key derivation

The cipher accepts the same decoded 32-byte calendar key-ring material used by the credential
boundary, but treats each value as a root key and never uses it directly for PKCE AES-GCM. For every
immutable key ID it derives one 32-byte key with HKDF-SHA256:

```text
IKM    = calendar root key
salt   = SHA256("dayweave.calendar-oauth-pkce.hkdf-salt.v1")
length = 32
info   = LP("dayweave.calendar-oauth-pkce")
       || LP("aes-256-gcm")
       || LP(uint16_be(1))
       || LP(ASCII(key_id))
```

`LP` is a four-byte unsigned big-endian length followed by the bytes. The domain, algorithm,
envelope version, and key ID prevent cross-purpose or cross-version derived-key reuse. Only derived
keys are retained by the cipher. The ring accepts one through eight unique roots, writes with one
active ID, and can decrypt with retained inactive IDs. IDs and their root material are immutable;
retired roots must remain configured until all pending ten-minute OAuth states have expired. PKCE
envelopes are short-lived, so this boundary does not rewrap them. See
[RFC 5869 section 3.2](https://www.rfc-editor.org/rfc/rfc5869.html#section-3.2).

## Authenticated envelope

Version `1` encrypts the validated 43-byte ASCII verifier using AES-256-GCM with a fresh 12-byte
nonce. The result is exactly 59 bytes: 43 encrypted bytes and the 16-byte authentication tag appended
by `cryptography`'s AES-GCM implementation. This matches the issue #56i database constraints.

Authenticated associated data is the concatenation of these length-prefixed components in order:

1. `dayweave.calendar-oauth-pkce`;
2. the two-byte unsigned envelope version;
3. the ASCII key ID;
4. the raw 32-byte state digest;
5. the UTF-8 provider account ID;
6. the UTF-8 provider session ID; and
7. the ASCII provider value `google`.

Account and session components must be nonblank and no larger than 255 UTF-8 bytes. Moving an
envelope to another state, tenant, session, provider, key ID, or version—or changing its nonce or
ciphertext—therefore fails closed. See the
[Cryptography AES-GCM contract](https://cryptography.io/en/stable/hazmat/primitives/aead/).

## Secret and failure handling

- The generated result never contains the verifier.
- Root and derived keys, verifiers, state digests, tenant IDs, nonces, and ciphertext are absent from
  representations, fixed error messages, causes, exception contexts, formatted tracebacks, and logs.
- Invalid scope or verifier input has one fixed input error.
- Entropy failure has one fixed generation error.
- Nonce or AES-GCM failure has one fixed encryption error.
- Malformed envelopes, unknown or removed keys, invalid tags, wrong AAD, and invalid decrypted
  verifiers have one indistinguishable decryption error.
- The module performs no logging, environment reads, persistence, or network access.

## Verification

Pure tests cover the RFC 7636 Appendix B challenge vector, exact entropy request and canonical
encoding, a frozen application-specific HKDF vector, root-key separation, schema-compatible envelope
sizes, fresh material, AAD binding, tampering, retained-key rotation, strict key-ring/scope/envelope
validation, fixed indistinguishable failures, dependency failure sanitization, immutable defensive
copies, redacted representations, and the absence of a plaintext verifier or `plain` method from the
generated result and exported surface.

## Deferred slices

1. Reuse the strict calendar key-ring environment parser to construct this cipher without caching or
   exposing root material.
2. Issue the OAuth state and encrypted verifier child atomically, then build the fixed Google
   authorization URL from the returned challenge.
3. Add a PKCE-aware callback consume operation that locks and decrypts the child before deleting the
   parent state; the current state consumer would cascade-delete the child too early.
4. Exchange the authorization code with the decrypted verifier, validate granted scopes, and replace
   the encrypted provider connection.

The runtime cutover retains issue #56i's feature gate, old-writer shutdown, and ten-minute drain.
PKCE augments rather than replaces the existing single-use state binding.

## Non-goals

- Environment variables, secret-manager integration, startup/readiness activation, or key caching.
- SQLAlchemy models, migrations, database reads or writes, or OAuth-state repository changes.
- Authorization URLs, client secrets, callbacks, token exchange/refresh, routes, OpenAPI, or UI.
- Google FreeBusy requests, scheduling integration, `plain` PKCE, rewrap, or non-Google providers.
