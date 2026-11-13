"""Redacted configuration value for Google Calendar OAuth authorization.

This module is intentionally network-free. It validates only the public client
identifier and exact callback URI needed by later authorization orchestration.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


__all__ = (
    "GOOGLE_CALENDAR_CALLBACK_PATH",
    "GoogleCalendarOAuthConfiguration",
    "GoogleCalendarOAuthConfigurationError",
    "GoogleCalendarOAuthError",
)


GOOGLE_CALENDAR_CALLBACK_PATH = "/api/oauth/google-calendar/callback"
_MAX_CLIENT_ID_BYTES = 512
_MAX_REDIRECT_URI_BYTES = 2 * 1024
_GOOGLE_WEB_CLIENT_ID_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*\.apps\.googleusercontent\.com\Z"
)
_DNS_LABEL_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)


class GoogleCalendarOAuthError(RuntimeError):
    """Base error for Google Calendar OAuth configuration."""


class GoogleCalendarOAuthConfigurationError(GoogleCalendarOAuthError):
    """Deployment authorization configuration is absent or unsafe."""

    def __init__(self) -> None:
        super().__init__("Google Calendar authorization is not configured safely.")


@dataclass(frozen=True, slots=True, repr=False)
class GoogleCalendarOAuthConfiguration:
    """Validated public OAuth values with a disclosure-safe representation."""

    client_id: str
    redirect_uri: str

    def __post_init__(self) -> None:
        if not _valid_client_id(self.client_id) or not _valid_redirect_uri(
            self.redirect_uri
        ):
            raise GoogleCalendarOAuthConfigurationError()

    def __repr__(self) -> str:
        return "GoogleCalendarOAuthConfiguration(<redacted>)"

    __str__ = __repr__


def _valid_client_id(value: object) -> bool:
    if not _bounded_visible_ascii(value, maximum_bytes=_MAX_CLIENT_ID_BYTES):
        return False
    return _GOOGLE_WEB_CLIENT_ID_PATTERN.fullmatch(value) is not None


def _valid_redirect_uri(value: object) -> bool:
    if not _bounded_visible_ascii(value, maximum_bytes=_MAX_REDIRECT_URI_BYTES):
        return False
    if "\\" in value or "?" in value or "#" in value:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.netloc.endswith(":")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != GOOGLE_CALENDAR_CALLBACK_PATH
        or "%" in parsed.netloc
    ):
        return False
    if port is not None and not 1 <= port <= 65535:
        return False

    hostname = parsed.hostname
    if hostname is None:
        return False
    normalized_host = hostname.lower()
    bracketed_host = parsed.netloc.startswith("[")
    is_loopback = normalized_host == "localhost"
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        if bracketed_host or (
            not is_loopback and not _valid_dns_hostname(normalized_host)
        ):
            return False
    else:
        if str(address) not in {"127.0.0.1", "::1"}:
            return False
        is_loopback = True

    return parsed.scheme == "https" or is_loopback


def _bounded_visible_ascii(value: object, *, maximum_bytes: int) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return len(encoded) <= maximum_bytes and all(0x21 <= byte <= 0x7E for byte in encoded)


def _valid_dns_hostname(hostname: str) -> bool:
    if (
        len(hostname) > 253
        or "." not in hostname
        or hostname.endswith(".")
        or not any(character.isalpha() for character in hostname)
    ):
        return False
    return all(
        _DNS_LABEL_PATTERN.fullmatch(label) is not None
        for label in hostname.split(".")
    )
