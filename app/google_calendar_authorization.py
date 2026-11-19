"""Pure construction of one fixed Google Calendar authorization request."""

from __future__ import annotations

import base64
import datetime
import re
from dataclasses import dataclass
from urllib.parse import quote as _quote
from urllib.parse import urlencode as _urlencode

from app.calendar_oauth_states import (
    IssuedGoogleCalendarOAuthPKCEState as _IssuedGoogleCalendarOAuthPKCEState,
)
from app.google_calendar_oauth import (
    GoogleCalendarOAuthConfiguration as _GoogleCalendarOAuthConfiguration,
    GoogleCalendarOAuthError,
)
from app.google_calendar_protocol import GOOGLE_CALENDAR_FREEBUSY_SCOPE


__all__ = (
    "GOOGLE_CALENDAR_AUTHORIZATION_ENDPOINT",
    "GOOGLE_CALENDAR_FREEBUSY_SCOPE",
    "GoogleCalendarAuthorizationError",
    "GoogleCalendarAuthorizationRequest",
    "build_google_calendar_authorization_request",
)


GOOGLE_CALENDAR_AUTHORIZATION_ENDPOINT = (
    "https://accounts.google.com/o/oauth2/v2/auth"
)
_CANONICAL_BASE64URL_32_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_UTC = datetime.timezone.utc


class GoogleCalendarAuthorizationError(GoogleCalendarOAuthError):
    """A Google authorization request could not be built safely."""

    def __init__(self) -> None:
        super().__init__("Google Calendar authorization request is invalid.")


@dataclass(frozen=True, slots=True, repr=False, init=False)
class GoogleCalendarAuthorizationRequest:
    """A sensitive redirect URL with disclosure-safe representations."""

    authorization_url: str

    def __init__(self, *, authorization_url: object) -> None:
        del authorization_url
        raise GoogleCalendarAuthorizationError()

    def __repr__(self) -> str:
        return "GoogleCalendarAuthorizationRequest(<redacted>)"

    __str__ = __repr__


def build_google_calendar_authorization_request(
    *,
    configuration: _GoogleCalendarOAuthConfiguration,
    issued_state: _IssuedGoogleCalendarOAuthPKCEState,
) -> GoogleCalendarAuthorizationRequest:
    """Build the fixed request from one validated configuration/state pair."""

    try:
        return _build_google_calendar_authorization_request(
            configuration=configuration,
            issued_state=issued_state,
        )
    except Exception:
        pass
    del configuration, issued_state
    raise GoogleCalendarAuthorizationError() from None


def _build_google_calendar_authorization_request(
    *,
    configuration: object,
    issued_state: object,
) -> GoogleCalendarAuthorizationRequest:
    if type(configuration) is not _GoogleCalendarOAuthConfiguration:
        raise ValueError("invalid configuration type")
    client_id = configuration.client_id
    redirect_uri = configuration.redirect_uri
    if type(client_id) is not str or type(redirect_uri) is not str:
        raise ValueError("invalid configuration values")
    _GoogleCalendarOAuthConfiguration(
        client_id=client_id,
        redirect_uri=redirect_uri,
    )

    if type(issued_state) is not _IssuedGoogleCalendarOAuthPKCEState:
        raise ValueError("invalid issuance type")
    raw_state = issued_state.raw_state
    code_challenge = issued_state.code_challenge
    code_challenge_method = issued_state.code_challenge_method
    expires_at = issued_state.expires_at
    if (
        not _valid_canonical_base64url_32(raw_state)
        or not _valid_canonical_base64url_32(code_challenge)
        or code_challenge_method != "S256"
        or not _valid_utc_expiry(expires_at)
    ):
        raise ValueError("invalid issuance")

    query = _urlencode(
        (
            ("client_id", client_id),
            ("redirect_uri", redirect_uri),
            ("response_type", "code"),
            ("scope", GOOGLE_CALENDAR_FREEBUSY_SCOPE),
            ("access_type", "offline"),
            ("prompt", "consent select_account"),
            ("state", raw_state),
            ("code_challenge", code_challenge),
            ("code_challenge_method", "S256"),
        ),
        doseq=False,
        safe="",
        encoding="ascii",
        errors="strict",
        quote_via=_quote,
    )
    authorization_url = f"{GOOGLE_CALENDAR_AUTHORIZATION_ENDPOINT}?{query}"
    authorization_url.encode("ascii")
    return _new_google_calendar_authorization_request(
        authorization_url=authorization_url,
    )


def _valid_canonical_base64url_32(value: object) -> bool:
    if (
        type(value) is not str
        or _CANONICAL_BASE64URL_32_PATTERN.fullmatch(value) is None
    ):
        return False
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(
            encoded + b"=",
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, ValueError):
        return False
    return (
        len(decoded) == 32
        and base64.urlsafe_b64encode(decoded).rstrip(b"=") == encoded
    )


def _valid_utc_expiry(value: object) -> bool:
    return (
        type(value) is datetime.datetime
        and value.tzinfo is _UTC
        and value.utcoffset() == datetime.timedelta(0)
    )


def _new_google_calendar_authorization_request(
    *,
    authorization_url: str,
) -> GoogleCalendarAuthorizationRequest:
    request = object.__new__(GoogleCalendarAuthorizationRequest)
    object.__setattr__(request, "authorization_url", authorization_url)
    return request
