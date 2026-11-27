"""Network-free Google Calendar OAuth configuration and URL validation."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


__all__ = (
    "GOOGLE_CALENDAR_AUTHORIZE_PATH",
    "GOOGLE_CALENDAR_CALLBACK_PATH",
    "GoogleCalendarOAuthConfiguration",
    "GoogleCalendarOAuthConfigurationError",
    "GoogleCalendarOAuthError",
    "GoogleCalendarTokenExchangeError",
    "google_calendar_oauth_origin",
    "google_calendar_oauth_route_path",
)


GOOGLE_CALENDAR_AUTHORIZE_PATH = "/api/coach/calendar-connections/google/authorize"
GOOGLE_CALENDAR_CALLBACK_PATH = "/api/oauth/google-calendar/callback"
_MAX_CLIENT_ID_BYTES = 512
_MAX_OAUTH_URL_BYTES = 2 * 1024
_GOOGLE_WEB_CLIENT_ID_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*\.apps\.googleusercontent\.com\Z"
)


def google_calendar_oauth_route_path(path: str, root_path: str) -> str:
    if root_path and (path == root_path or path.startswith(f"{root_path}/")):
        return path[len(root_path):]
    return path
_DNS_LABEL_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)


class GoogleCalendarOAuthError(RuntimeError):
    """Base error for Google Calendar OAuth configuration."""


class GoogleCalendarTokenExchangeError(GoogleCalendarOAuthError):
    """An authorization code could not be exchanged safely."""

    def __init__(self) -> None:
        super().__init__("Google Calendar authorization could not be completed.")


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
        origin = google_calendar_oauth_origin(self.redirect_uri, path=GOOGLE_CALENDAR_CALLBACK_PATH)
        if not _valid_client_id(self.client_id) or origin is None:
            raise GoogleCalendarOAuthConfigurationError()

    def __repr__(self) -> str:
        return "GoogleCalendarOAuthConfiguration(<redacted>)"

    __str__ = __repr__


def _valid_client_id(value: object) -> bool:
    if not _bounded_visible_ascii(value, maximum_bytes=_MAX_CLIENT_ID_BYTES):
        return False
    return _GOOGLE_WEB_CLIENT_ID_PATTERN.fullmatch(value) is not None


def google_calendar_oauth_origin(value: object, *, path: str) -> str | None:
    """Return the origin of a safe OAuth URL with the expected exact path."""

    if not _bounded_visible_ascii(value, maximum_bytes=_MAX_OAUTH_URL_BYTES):
        return None
    if "\\" in value or "?" in value or "#" in value:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.netloc.endswith(":")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != path
        or "%" in parsed.netloc
    ):
        return None
    if port is not None and not 1 <= port <= 65535:
        return None

    hostname = parsed.hostname
    if hostname is None:
        return None
    normalized_host = hostname.lower()
    bracketed_host = parsed.netloc.startswith("[")
    if bracketed_host:
        remainder = parsed.netloc.partition("]")[2]
        if remainder and not remainder.startswith(":"):
            return None
    is_loopback = normalized_host == "localhost"
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        if bracketed_host or (
            not is_loopback and not _valid_dns_hostname(normalized_host)
        ):
            return None
    else:
        if str(address) not in {"127.0.0.1", "::1"}:
            return None
        is_loopback = True

    if parsed.scheme != "https" and not is_loopback:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


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
