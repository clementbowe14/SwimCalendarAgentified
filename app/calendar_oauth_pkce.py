"""Pure PKCE generation and verifier protection for Google Calendar OAuth.

The boundary is deliberately independent from environment configuration,
persistence, OAuth transport, and logging. It returns only the public S256
challenge and an opaque encrypted verifier envelope.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


__all__ = (
    "CalendarOAuthPKCECipher",
    "CalendarOAuthPKCEConfigurationError",
    "CalendarOAuthPKCEDecryptionError",
    "CalendarOAuthPKCEEncryptionError",
    "CalendarOAuthPKCEError",
    "CalendarOAuthPKCEGenerationError",
    "CalendarOAuthPKCEInputError",
    "CalendarOAuthPKCEScope",
    "EncryptedCalendarOAuthPKCEVerifier",
    "GeneratedGoogleCalendarPKCE",
    "derive_s256_code_challenge",
)


PKCE_ENVELOPE_VERSION = 1
PKCE_VERIFIER_ENTROPY_BYTES = 32
PKCE_VERIFIER_BYTES = 43
PKCE_CIPHERTEXT_BYTES = 59
AES_256_KEY_BYTES = 32
AES_GCM_NONCE_BYTES = 12
MAX_ROOT_KEYS = 8
MAX_SCOPE_COMPONENT_BYTES = 255
_KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_VERIFIER_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_AAD_NAMESPACE = b"dayweave.calendar-oauth-pkce"
_HKDF_ALGORITHM = b"aes-256-gcm"
_HKDF_SALT = hashlib.sha256(
    b"dayweave.calendar-oauth-pkce.hkdf-salt.v1"
).digest()


class CalendarOAuthPKCEError(Exception):
    """Base error for calendar OAuth PKCE failures."""


class CalendarOAuthPKCEConfigurationError(CalendarOAuthPKCEError):
    """The supplied root-key ring is not safe to use."""

    def __init__(self) -> None:
        super().__init__(
            "Calendar OAuth PKCE encryption is not configured safely."
        )


class CalendarOAuthPKCEInputError(CalendarOAuthPKCEError):
    """A verifier or trusted owner scope is malformed."""

    def __init__(self) -> None:
        super().__init__("Calendar OAuth PKCE input is invalid.")


class CalendarOAuthPKCEGenerationError(CalendarOAuthPKCEError):
    """Fresh verifier material could not be generated."""

    def __init__(self) -> None:
        super().__init__("Calendar OAuth PKCE material could not be generated.")


class CalendarOAuthPKCEEncryptionError(CalendarOAuthPKCEError):
    """A generated verifier could not be encrypted."""

    def __init__(self) -> None:
        super().__init__("Calendar OAuth PKCE verifier could not be encrypted.")


class CalendarOAuthPKCEDecryptionError(CalendarOAuthPKCEError):
    """A stored verifier could not be authenticated and decrypted."""

    def __init__(self) -> None:
        super().__init__("Calendar OAuth PKCE verifier could not be decrypted.")


class _PKCEInputFailure(Exception):
    pass


class _PKCEGenerationFailure(Exception):
    pass


class _PKCEEncryptionFailure(Exception):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class CalendarOAuthPKCEScope:
    """State and provider owner identity authenticated with one verifier."""

    state_digest: bytes
    account_id: str
    session_id: str
    provider: Literal["google"] = "google"

    def __repr__(self) -> str:
        return "CalendarOAuthPKCEScope(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class EncryptedCalendarOAuthPKCEVerifier:
    """Opaque schema-compatible verifier envelope."""

    version: int
    key_id: str
    nonce: bytes
    ciphertext: bytes

    def __repr__(self) -> str:
        return "EncryptedCalendarOAuthPKCEVerifier(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class GeneratedGoogleCalendarPKCE:
    """Public challenge plus encrypted verifier, never the raw verifier."""

    code_challenge: str
    encrypted_verifier: EncryptedCalendarOAuthPKCEVerifier

    @property
    def code_challenge_method(self) -> Literal["S256"]:
        return "S256"

    def __repr__(self) -> str:
        return "GeneratedGoogleCalendarPKCE(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, init=False, repr=False)
class CalendarOAuthPKCECipher:
    """Root-key ring deriving isolated AES-256-GCM keys for PKCE."""

    _active_key_id: str
    _derived_keys: Mapping[str, bytes]

    def __init__(
        self,
        *,
        active_key_id: str,
        root_keys: Mapping[str, bytes],
    ) -> None:
        try:
            derived_keys = _derive_key_ring(
                active_key_id=active_key_id,
                root_keys=root_keys,
            )
        except Exception:
            pass
        else:
            object.__setattr__(self, "_active_key_id", active_key_id)
            object.__setattr__(self, "_derived_keys", derived_keys)
            return
        raise CalendarOAuthPKCEConfigurationError() from None

    @property
    def active_key_id(self) -> str:
        """Return the non-secret identifier used for new envelopes."""

        return self._active_key_id

    def __repr__(self) -> str:
        return "CalendarOAuthPKCECipher(<redacted>)"

    __str__ = __repr__

    def generate_google_calendar_pkce(
        self,
        *,
        scope: CalendarOAuthPKCEScope,
    ) -> GeneratedGoogleCalendarPKCE:
        """Generate an S256 challenge and encrypt its one-time verifier."""

        error_type = None
        try:
            return self._generate_google_calendar_pkce(scope=scope)
        except _PKCEInputFailure:
            error_type = CalendarOAuthPKCEInputError
        except _PKCEGenerationFailure:
            error_type = CalendarOAuthPKCEGenerationError
        except _PKCEEncryptionFailure:
            error_type = CalendarOAuthPKCEEncryptionError
        except Exception:
            error_type = CalendarOAuthPKCEEncryptionError
        raise error_type() from None

    def _generate_google_calendar_pkce(
        self,
        *,
        scope: object,
    ) -> GeneratedGoogleCalendarPKCE:
        aad = _associated_data(
            scope,
            version=PKCE_ENVELOPE_VERSION,
            key_id=self._active_key_id,
        )
        verifier, challenge = _generate_verifier_and_challenge()
        try:
            nonce = os.urandom(AES_GCM_NONCE_BYTES)
            if not isinstance(nonce, bytes) or len(nonce) != AES_GCM_NONCE_BYTES:
                raise ValueError("invalid nonce source")
            ciphertext = AESGCM(
                self._derived_keys[self._active_key_id]
            ).encrypt(
                nonce,
                verifier.encode("ascii"),
                aad,
            )
            if (
                not isinstance(ciphertext, bytes)
                or len(ciphertext) != PKCE_CIPHERTEXT_BYTES
            ):
                raise ValueError("invalid ciphertext size")
        except Exception:
            raise _PKCEEncryptionFailure() from None
        return GeneratedGoogleCalendarPKCE(
            code_challenge=challenge,
            encrypted_verifier=EncryptedCalendarOAuthPKCEVerifier(
                version=PKCE_ENVELOPE_VERSION,
                key_id=self._active_key_id,
                nonce=nonce,
                ciphertext=ciphertext,
            ),
        )

    def decrypt_google_calendar_verifier(
        self,
        encrypted_verifier: EncryptedCalendarOAuthPKCEVerifier,
        *,
        scope: CalendarOAuthPKCEScope,
    ) -> str:
        """Authenticate and decrypt a verifier without exposing failure detail."""

        try:
            return self._decrypt_google_calendar_verifier(
                encrypted_verifier,
                scope=scope,
            )
        except Exception:
            pass
        raise CalendarOAuthPKCEDecryptionError() from None

    def _decrypt_google_calendar_verifier(
        self,
        encrypted_verifier: object,
        *,
        scope: object,
    ) -> str:
        _validate_envelope(encrypted_verifier)
        derived_key = self._derived_keys.get(encrypted_verifier.key_id)
        if derived_key is None:
            raise ValueError("unknown key")
        aad = _associated_data(
            scope,
            version=encrypted_verifier.version,
            key_id=encrypted_verifier.key_id,
        )
        plaintext = AESGCM(derived_key).decrypt(
            encrypted_verifier.nonce,
            encrypted_verifier.ciphertext,
            aad,
        )
        if not isinstance(plaintext, bytes) or len(plaintext) != PKCE_VERIFIER_BYTES:
            raise ValueError("invalid plaintext")
        try:
            verifier = plaintext.decode("ascii")
        except UnicodeDecodeError:
            raise ValueError("invalid plaintext") from None
        _validated_verifier_bytes(verifier)
        return verifier


def derive_s256_code_challenge(verifier: str) -> str:
    """Return the strict RFC 7636 S256 challenge for a canonical verifier."""

    try:
        verifier_bytes = _validated_verifier_bytes(verifier)
        return _base64url_no_padding(hashlib.sha256(verifier_bytes).digest())
    except Exception:
        pass
    raise CalendarOAuthPKCEInputError() from None


def _derive_key_ring(
    *,
    active_key_id: object,
    root_keys: object,
) -> Mapping[str, bytes]:
    if not _valid_key_id(active_key_id):
        raise ValueError("invalid active key")
    if (
        not isinstance(root_keys, Mapping)
        or not 1 <= len(root_keys) <= MAX_ROOT_KEYS
    ):
        raise ValueError("invalid root-key ring")

    copied_roots: dict[str, bytes] = {}
    for key_id, root_key in root_keys.items():
        if (
            not _valid_key_id(key_id)
            or not isinstance(root_key, bytes)
            or len(root_key) != AES_256_KEY_BYTES
        ):
            raise ValueError("invalid root key")
        copied_root = bytes(root_key)
        if copied_root in copied_roots.values():
            raise ValueError("duplicate root key")
        copied_roots[key_id] = copied_root
    if active_key_id not in copied_roots:
        raise ValueError("active key missing")

    derived_keys = {
        key_id: _derive_pkce_key(root_key, key_id=key_id)
        for key_id, root_key in copied_roots.items()
    }
    if len(set(derived_keys.values())) != len(derived_keys):
        raise ValueError("duplicate derived key")
    return MappingProxyType(derived_keys)


def _derive_pkce_key(root_key: bytes, *, key_id: str) -> bytes:
    info = b"".join(
        _length_prefixed(component)
        for component in (
            _AAD_NAMESPACE,
            _HKDF_ALGORITHM,
            PKCE_ENVELOPE_VERSION.to_bytes(2, "big"),
            key_id.encode("ascii"),
        )
    )
    return HKDF(
        algorithm=hashes.SHA256(),
        length=AES_256_KEY_BYTES,
        salt=_HKDF_SALT,
        info=info,
    ).derive(root_key)


def _generate_verifier_and_challenge() -> tuple[str, str]:
    try:
        entropy = secrets.token_bytes(PKCE_VERIFIER_ENTROPY_BYTES)
        if (
            not isinstance(entropy, bytes)
            or len(entropy) != PKCE_VERIFIER_ENTROPY_BYTES
        ):
            raise ValueError("invalid entropy source")
        verifier = _base64url_no_padding(entropy)
        challenge = _base64url_no_padding(
            hashlib.sha256(_validated_verifier_bytes(verifier)).digest()
        )
        if len(challenge) != PKCE_VERIFIER_BYTES:
            raise ValueError("invalid challenge")
        return verifier, challenge
    except Exception:
        raise _PKCEGenerationFailure() from None


def _validated_verifier_bytes(verifier: object) -> bytes:
    if (
        not isinstance(verifier, str)
        or _VERIFIER_PATTERN.fullmatch(verifier) is None
    ):
        raise ValueError("invalid verifier")
    try:
        encoded = verifier.encode("ascii")
        decoded = base64.b64decode(
            encoded + b"=",
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, ValueError):
        raise ValueError("invalid verifier") from None
    if (
        len(decoded) != PKCE_VERIFIER_ENTROPY_BYTES
        or _base64url_no_padding(decoded) != verifier
    ):
        raise ValueError("noncanonical verifier")
    return encoded


def _associated_data(
    scope: object,
    *,
    version: int,
    key_id: str,
) -> bytes:
    if not isinstance(scope, CalendarOAuthPKCEScope):
        raise _PKCEInputFailure()
    if (
        scope.provider != "google"
        or not _valid_key_id(key_id)
        or not isinstance(scope.state_digest, bytes)
        or len(scope.state_digest) != 32
    ):
        raise _PKCEInputFailure()

    components: list[bytes] = []
    for value in (scope.account_id, scope.session_id):
        if (
            not isinstance(value, str)
            or not value
            or len(value) > MAX_SCOPE_COMPONENT_BYTES
            or not value.strip()
        ):
            raise _PKCEInputFailure()
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            raise _PKCEInputFailure() from None
        if len(encoded) > MAX_SCOPE_COMPONENT_BYTES:
            raise _PKCEInputFailure()
        components.append(encoded)

    return b"".join(
        _length_prefixed(component)
        for component in (
            _AAD_NAMESPACE,
            version.to_bytes(2, "big", signed=False),
            key_id.encode("ascii"),
            scope.state_digest,
            components[0],
            components[1],
            scope.provider.encode("ascii"),
        )
    )


def _validate_envelope(envelope: object) -> None:
    if not isinstance(envelope, EncryptedCalendarOAuthPKCEVerifier):
        raise ValueError("invalid envelope")
    if type(envelope.version) is not int or envelope.version != PKCE_ENVELOPE_VERSION:
        raise ValueError("unsupported envelope")
    if not _valid_key_id(envelope.key_id):
        raise ValueError("invalid key id")
    if (
        not isinstance(envelope.nonce, bytes)
        or len(envelope.nonce) != AES_GCM_NONCE_BYTES
    ):
        raise ValueError("invalid nonce")
    if (
        not isinstance(envelope.ciphertext, bytes)
        or len(envelope.ciphertext) != PKCE_CIPHERTEXT_BYTES
    ):
        raise ValueError("invalid ciphertext")


def _valid_key_id(value: object) -> bool:
    return isinstance(value, str) and _KEY_ID_PATTERN.fullmatch(value) is not None


def _base64url_no_padding(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _length_prefixed(value: bytes) -> bytes:
    return len(value).to_bytes(4, "big") + value
