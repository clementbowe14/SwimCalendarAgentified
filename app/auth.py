import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from app import db
from app.api_errors import AuthorizationForbidden, InvalidAuthToken, InvalidCredentials


DEFAULT_COACH_CREDENTIALS = json.dumps(
    [
        {
            "email": "coach@example.com",
            "password": "swim-dev-pass",
            "instructor_id": "inst_001",
        }
    ]
)
TOKEN_TTL_SECONDS = 8 * 60 * 60
_DEV_SECRET = "swim-dev-only-auth-secret-change-for-shared-environments"
_STRICT_ENVIRONMENTS = {"prod", "production", "staging"}


@dataclass(frozen=True)
class CoachPrincipal:
    instructor_id: str
    display_name: str
    expires_at: datetime


@dataclass(frozen=True)
class CoachCredential:
    email: str
    instructor_id: str
    password: Optional[str] = None
    password_sha256: Optional[str] = None


def authenticate_coach(email: str, password: str) -> CoachPrincipal:
    normalized_email = email.strip().lower()
    for credential in _coach_credentials():
        if credential.email != normalized_email:
            continue
        if _credential_matches(credential, password):
            return _principal_for_instructor(credential.instructor_id)
    raise InvalidCredentials()


def issue_access_token(principal: CoachPrincipal) -> str:
    issued_at = int(time.time())
    expires_at = int(principal.expires_at.timestamp())
    payload = {
        "sub": principal.instructor_id,
        "name": principal.display_name,
        "iat": issued_at,
        "exp": expires_at,
        "typ": "coach_access",
    }
    body = _b64_json(payload)
    signature = _sign(body)
    return f"{body}.{signature}"


def verify_access_token(token: str) -> CoachPrincipal:
    try:
        body, signature = token.split(".", 1)
    except ValueError as exc:
        raise InvalidAuthToken() from exc

    expected_signature = _sign(body)
    if not hmac.compare_digest(signature, expected_signature):
        raise InvalidAuthToken()

    try:
        payload = _json_from_b64(body)
    except (ValueError, json.JSONDecodeError) as exc:
        raise InvalidAuthToken() from exc

    if payload.get("typ") != "coach_access":
        raise InvalidAuthToken()
    expires_at_raw = payload.get("exp")
    instructor_id = payload.get("sub")
    if not isinstance(expires_at_raw, int) or not isinstance(instructor_id, str):
        raise InvalidAuthToken()
    if expires_at_raw < int(time.time()):
        raise InvalidAuthToken()

    return _principal_for_instructor(
        instructor_id,
        expires_at=datetime.fromtimestamp(expires_at_raw, tz=timezone.utc),
    )


def _principal_for_instructor(
    instructor_id: str,
    expires_at: Optional[datetime] = None,
) -> CoachPrincipal:
    instructor = db.get_instructor_info(instructor_id)
    if not instructor:
        raise AuthorizationForbidden()
    return CoachPrincipal(
        instructor_id=instructor.id,
        display_name=instructor.name,
        expires_at=expires_at or datetime.fromtimestamp(time.time() + TOKEN_TTL_SECONDS, tz=timezone.utc),
    )


def _coach_credentials() -> list[CoachCredential]:
    raw = os.getenv("SWIM_COACH_CREDENTIALS")
    if not raw:
        if _is_strict_environment():
            raise RuntimeError("SWIM_COACH_CREDENTIALS must be configured outside local development.")
        raw = DEFAULT_COACH_CREDENTIALS
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("SWIM_COACH_CREDENTIALS must be a JSON array.") from exc
    credentials: list[CoachCredential] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise RuntimeError("SWIM_COACH_CREDENTIALS entries must be JSON objects.")
        email = str(item.get("email", "")).strip().lower()
        instructor_id = str(item.get("instructor_id", "")).strip()
        password = item.get("password")
        password_sha256 = item.get("password_sha256")
        if not email or not instructor_id or (not password and not password_sha256):
            raise RuntimeError(
                "Each SWIM_COACH_CREDENTIALS entry needs email, instructor_id, and password or password_sha256."
            )
        credentials.append(
            CoachCredential(
                email=email,
                instructor_id=instructor_id,
                password=str(password) if password is not None else None,
                password_sha256=str(password_sha256) if password_sha256 is not None else None,
            )
        )
    return credentials


def _credential_matches(credential: CoachCredential, password: str) -> bool:
    if credential.password_sha256:
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(digest, credential.password_sha256)
    if credential.password is None:
        return False
    return hmac.compare_digest(password, credential.password)


def _auth_secret() -> str:
    configured = os.getenv("SWIM_COACH_AUTH_SECRET")
    if configured:
        return configured
    if _is_strict_environment():
        raise RuntimeError("SWIM_COACH_AUTH_SECRET must be configured outside local development.")
    return _DEV_SECRET


def _is_strict_environment() -> bool:
    return os.getenv("SWIM_ENV", "").strip().lower() in _STRICT_ENVIRONMENTS


def _sign(body: str) -> str:
    digest = hmac.new(_auth_secret().encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return _b64(digest)


def _b64_json(payload: dict[str, Any]) -> str:
    return _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _json_from_b64(value: str) -> dict[str, Any]:
    decoded = base64.urlsafe_b64decode(_pad_b64(value)).decode("utf-8")
    payload = json.loads(decoded)
    if not isinstance(payload, dict):
        raise ValueError("Token payload must be a JSON object.")
    return payload


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _pad_b64(value: str) -> bytes:
    return (value + "=" * (-len(value) % 4)).encode("ascii")
