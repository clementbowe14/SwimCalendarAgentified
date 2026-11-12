"""Authenticated encryption boundary for external-calendar credentials.

The boundary is deliberately independent from environment configuration and
persistence. Callers supply a decoded key ring, bind each credential to its
provider connection, and persist only the returned opaque envelope.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


CREDENTIAL_ENVELOPE_VERSION = 1
AES_256_KEY_BYTES = 32
AES_GCM_NONCE_BYTES = 12
AES_GCM_TAG_BYTES = 16
MAX_CREDENTIAL_KEYS = 8
MAX_REFRESH_TOKEN_BYTES = 16 * 1024
MAX_SCOPE_COMPONENT_BYTES = 255
_KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_AAD_NAMESPACE = b"dayweave.calendar-credential"


class CalendarCredentialError(Exception):
    """Base error for calendar credential protection failures."""


class CalendarCredentialConfigurationError(CalendarCredentialError):
    """The supplied credential key ring is not safe to use."""

    def __init__(self) -> None:
        super().__init__("Calendar credential encryption is not configured safely.")


class CalendarCredentialInputError(CalendarCredentialError):
    """A plaintext credential or provider scope is invalid."""

    def __init__(self) -> None:
        super().__init__("Calendar credential input is invalid.")


class CalendarCredentialEncryptionError(CalendarCredentialError):
    """A valid credential could not be encrypted."""

    def __init__(self) -> None:
        super().__init__("Calendar credential could not be encrypted.")


class CalendarCredentialDecryptionError(CalendarCredentialError):
    """A stored credential could not be authenticated and decrypted."""

    def __init__(self) -> None:
        super().__init__("Calendar credential could not be decrypted.")


@dataclass(frozen=True, slots=True)
class CalendarCredentialScope:
    """Stable provider-owned identity authenticated with a credential."""

    instructor_id: str
    connection_id: str
    provider: Literal["google"] = "google"


@dataclass(frozen=True, slots=True, repr=False)
class EncryptedCalendarCredential:
    """Opaque storage envelope; ciphertext and nonce are always redacted."""

    version: int
    key_id: str
    nonce: bytes
    ciphertext: bytes

    def __repr__(self) -> str:
        return "EncryptedCalendarCredential(<redacted>)"


@dataclass(frozen=True, slots=True, init=False, repr=False)
class CalendarCredentialCipher:
    """AES-256-GCM key ring with active-key encryption and rotated-key reads."""

    _active_key_id: str
    _keys: Mapping[str, bytes]

    def __init__(self, *, active_key_id: str, keys: Mapping[str, bytes]) -> None:
        if not _valid_key_id(active_key_id):
            raise CalendarCredentialConfigurationError()
        if not isinstance(keys, Mapping) or not 1 <= len(keys) <= MAX_CREDENTIAL_KEYS:
            raise CalendarCredentialConfigurationError()

        copied_keys: dict[str, bytes] = {}
        for key_id, key in keys.items():
            if (
                not _valid_key_id(key_id)
                or not isinstance(key, bytes)
                or len(key) != AES_256_KEY_BYTES
            ):
                raise CalendarCredentialConfigurationError()
            if key in copied_keys.values():
                raise CalendarCredentialConfigurationError()
            copied_keys[key_id] = bytes(key)
        if active_key_id not in copied_keys:
            raise CalendarCredentialConfigurationError()

        object.__setattr__(self, "_active_key_id", active_key_id)
        object.__setattr__(self, "_keys", MappingProxyType(copied_keys))

    @property
    def active_key_id(self) -> str:
        """Return the non-secret identifier used for new envelopes."""

        return self._active_key_id

    def __repr__(self) -> str:
        return "CalendarCredentialCipher(<redacted>)"

    def encrypt_refresh_token(
        self,
        refresh_token: str,
        *,
        scope: CalendarCredentialScope,
    ) -> EncryptedCalendarCredential:
        """Encrypt one opaque refresh credential for a stable provider scope."""

        plaintext = _encode_refresh_token(refresh_token)
        try:
            aad = _associated_data(
                scope,
                version=CREDENTIAL_ENVELOPE_VERSION,
                key_id=self._active_key_id,
            )
        except CalendarCredentialInputError:
            raise CalendarCredentialInputError() from None

        try:
            nonce = os.urandom(AES_GCM_NONCE_BYTES)
            if not isinstance(nonce, bytes) or len(nonce) != AES_GCM_NONCE_BYTES:
                raise RuntimeError("invalid nonce source")
            ciphertext = AESGCM(self._keys[self._active_key_id]).encrypt(
                nonce,
                plaintext,
                aad,
            )
        except Exception:
            raise CalendarCredentialEncryptionError() from None
        return EncryptedCalendarCredential(
            version=CREDENTIAL_ENVELOPE_VERSION,
            key_id=self._active_key_id,
            nonce=nonce,
            ciphertext=ciphertext,
        )

    def decrypt_refresh_token(
        self,
        envelope: EncryptedCalendarCredential,
        *,
        scope: CalendarCredentialScope,
    ) -> str:
        """Authenticate and decrypt an envelope without exposing failure detail."""

        try:
            _validate_envelope(envelope)
            key = self._keys.get(envelope.key_id)
            if key is None:
                raise ValueError("unknown key")
            aad = _associated_data(
                scope,
                version=envelope.version,
                key_id=envelope.key_id,
            )
            plaintext = AESGCM(key).decrypt(
                envelope.nonce,
                envelope.ciphertext,
                aad,
            )
            if not 1 <= len(plaintext) <= MAX_REFRESH_TOKEN_BYTES:
                raise ValueError("invalid plaintext size")
            refresh_token = plaintext.decode("utf-8")
            if not refresh_token.strip():
                raise ValueError("invalid plaintext")
            return refresh_token
        except Exception:
            raise CalendarCredentialDecryptionError() from None

    def needs_rewrap(
        self,
        envelope: EncryptedCalendarCredential,
        *,
        scope: CalendarCredentialScope,
    ) -> bool:
        """Return whether an authenticated envelope uses an inactive key."""

        self.decrypt_refresh_token(envelope, scope=scope)
        return envelope.key_id != self._active_key_id

    def rewrap_refresh_token(
        self,
        envelope: EncryptedCalendarCredential,
        *,
        scope: CalendarCredentialScope,
    ) -> EncryptedCalendarCredential:
        """Authenticate an existing token and re-encrypt it with the active key."""

        refresh_token = self.decrypt_refresh_token(envelope, scope=scope)
        return self.encrypt_refresh_token(refresh_token, scope=scope)


def _valid_key_id(value: object) -> bool:
    return isinstance(value, str) and _KEY_ID_PATTERN.fullmatch(value) is not None


def _encode_refresh_token(refresh_token: object) -> bytes:
    if not isinstance(refresh_token, str):
        raise CalendarCredentialInputError()
    if not refresh_token or len(refresh_token) > MAX_REFRESH_TOKEN_BYTES:
        raise CalendarCredentialInputError()
    if not refresh_token.strip():
        raise CalendarCredentialInputError()
    try:
        plaintext = refresh_token.encode("utf-8")
    except UnicodeEncodeError:
        raise CalendarCredentialInputError() from None
    if len(plaintext) > MAX_REFRESH_TOKEN_BYTES:
        raise CalendarCredentialInputError()
    return plaintext


def _associated_data(
    scope: object,
    *,
    version: int,
    key_id: str,
) -> bytes:
    if not isinstance(scope, CalendarCredentialScope):
        raise CalendarCredentialInputError()
    if scope.provider != "google" or not _valid_key_id(key_id):
        raise CalendarCredentialInputError()

    components: list[bytes] = []
    for value in (scope.instructor_id, scope.connection_id):
        if not isinstance(value, str):
            raise CalendarCredentialInputError()
        if not value or len(value) > MAX_SCOPE_COMPONENT_BYTES:
            raise CalendarCredentialInputError()
        if not value.strip():
            raise CalendarCredentialInputError()
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            raise CalendarCredentialInputError() from None
        if len(encoded) > MAX_SCOPE_COMPONENT_BYTES:
            raise CalendarCredentialInputError()
        components.append(encoded)

    provider = scope.provider.encode("ascii")
    key_identifier = key_id.encode("ascii")
    version_bytes = version.to_bytes(2, "big", signed=False)
    return b"".join(
        _length_prefixed(part)
        for part in (
            _AAD_NAMESPACE,
            version_bytes,
            key_identifier,
            components[0],
            provider,
            components[1],
        )
    )


def _length_prefixed(value: bytes) -> bytes:
    return len(value).to_bytes(4, "big") + value


def _validate_envelope(envelope: object) -> None:
    if not isinstance(envelope, EncryptedCalendarCredential):
        raise ValueError("invalid envelope")
    if type(envelope.version) is not int or envelope.version != CREDENTIAL_ENVELOPE_VERSION:
        raise ValueError("unsupported envelope")
    if not _valid_key_id(envelope.key_id):
        raise ValueError("invalid key id")
    if not isinstance(envelope.nonce, bytes) or len(envelope.nonce) != AES_GCM_NONCE_BYTES:
        raise ValueError("invalid nonce")
    if (
        not isinstance(envelope.ciphertext, bytes)
        or not AES_GCM_TAG_BYTES < len(envelope.ciphertext)
        or len(envelope.ciphertext) > MAX_REFRESH_TOKEN_BYTES + AES_GCM_TAG_BYTES
    ):
        raise ValueError("invalid ciphertext")
