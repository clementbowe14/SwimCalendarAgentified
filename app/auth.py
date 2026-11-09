import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Response
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy.exc import IntegrityError

from app import db
from app.api_errors import InvalidAuthToken, InvalidCredentials
from app.config import ApplicationSettings, application_settings_from_env
from app.models import InstructorDB, ProviderAccountDB, ProviderSessionDB


SESSION_COOKIE_NAME = "swim_session"
SESSION_COOKIE_PATH = "/api"
CSRF_HEADER_NAME = "X-CSRF-Protection"
CSRF_HEADER_VALUE = "1"
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256
SESSION_TOKEN_COLLISION_RETRIES = 3
DEMO_PROVIDER_EMAIL = "coach@example.com"
DEMO_PROVIDER_PASSWORD = "swim-dev-pass"
DEMO_PROVIDER_INSTRUCTOR_ID = "inst_001"
_SESSION_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_EMAIL_ADAPTER = TypeAdapter(EmailStr)
_PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash("not-a-real-provider-password")


@dataclass(frozen=True)
class CoachPrincipal:
    account_id: str
    session_id: str
    instructor_id: str
    display_name: str
    expires_at: datetime


@dataclass(frozen=True)
class IssuedCoachSession:
    principal: CoachPrincipal
    raw_token: str


class ProviderAuthAdminError(RuntimeError):
    """A safe, operator-facing provider-auth administration error."""


def normalize_provider_email(email: str) -> str:
    try:
        validated = _EMAIL_ADAPTER.validate_python(email.strip())
    except ValidationError as exc:
        raise ProviderAuthAdminError("A valid provider email address is required.") from exc
    return str(validated).casefold()


def hash_provider_password(password: str) -> str:
    _validate_new_password(password)
    return _PASSWORD_HASHER.hash(password)


def provision_provider_account(
    instructor_id: str,
    email: str,
    password: str,
    *,
    is_demo: bool = False,
) -> str:
    normalized_email = normalize_provider_email(email)
    password_hash = hash_provider_password(password)
    now = _utc_now()
    session = db.SessionLocal()
    try:
        with session.begin():
            instructor = session.get(InstructorDB, instructor_id)
            if instructor is None:
                raise ProviderAuthAdminError("The provider does not exist.")
            account = ProviderAccountDB(
                id=f"acct_{uuid4().hex}",
                instructor_id=instructor_id,
                email_normalized=normalized_email,
                password_hash=password_hash,
                is_demo=is_demo,
                session_version=1,
                password_changed_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(account)
            session.flush()
            account_id = account.id
        return account_id
    except IntegrityError as exc:
        session.rollback()
        raise ProviderAuthAdminError(
            "That email or provider already has a pilot account."
        ) from exc
    finally:
        session.close()


def authenticate_coach(
    email: str,
    password: str,
    *,
    presented_token: Optional[str] = None,
    now: Optional[datetime] = None,
) -> IssuedCoachSession:
    requested_at = _coerce_utc(now or _utc_now())
    try:
        normalized_email = normalize_provider_email(email)
    except ProviderAuthAdminError:
        _verify_password(_DUMMY_PASSWORD_HASH, password)
        raise InvalidCredentials()

    session = db.SessionLocal()
    try:
        with session.begin():
            account = (
                session.query(ProviderAccountDB)
                .filter(ProviderAccountDB.email_normalized == normalized_email)
                .with_for_update()
                .one_or_none()
            )
            password_hash = account.password_hash if account is not None else _DUMMY_PASSWORD_HASH
            password_valid = _verify_password(password_hash, password)
            instructor = (
                session.get(InstructorDB, account.instructor_id)
                if account is not None
                else None
            )
            settings = application_settings_from_env()
            if (
                account is None
                or not password_valid
                or account.disabled_at is not None
                or (account.is_demo and not settings.demo_auth_enabled)
                or instructor is None
            ):
                raise InvalidCredentials()

            if _PASSWORD_HASHER.check_needs_rehash(account.password_hash):
                account.password_hash = _PASSWORD_HASHER.hash(password)
                account.updated_at = requested_at

            if presented_token:
                _revoke_token_in_session(
                    session,
                    presented_token,
                    requested_at,
                    "replaced_on_sign_in",
                )

            expires_at = requested_at + timedelta(seconds=settings.coach_session_ttl_seconds)
            raw_token, provider_session = _insert_provider_session(
                session,
                account,
                requested_at,
                expires_at,
            )
            principal = CoachPrincipal(
                account_id=account.id,
                session_id=provider_session.id,
                instructor_id=instructor.id,
                display_name=instructor.name,
                expires_at=expires_at,
            )
        return IssuedCoachSession(principal=principal, raw_token=raw_token)
    finally:
        session.close()


def validate_coach_session(
    raw_token: str,
    *,
    now: Optional[datetime] = None,
) -> CoachPrincipal:
    if not _valid_session_token(raw_token):
        raise InvalidAuthToken()
    requested_at = _coerce_utc(now or _utc_now())
    digest = _session_digest(raw_token)
    session = db.SessionLocal()
    try:
        row = (
            session.query(ProviderSessionDB, ProviderAccountDB, InstructorDB)
            .join(ProviderAccountDB, ProviderSessionDB.account_id == ProviderAccountDB.id)
            .join(InstructorDB, ProviderAccountDB.instructor_id == InstructorDB.id)
            .filter(ProviderSessionDB.token_digest == digest)
            .one_or_none()
        )
        if row is None:
            raise InvalidAuthToken()
        provider_session, account, instructor = row
        if (
            provider_session.revoked_at is not None
            or _coerce_utc(provider_session.expires_at) <= requested_at
            or account.disabled_at is not None
            or (
                account.is_demo
                and not application_settings_from_env().demo_auth_enabled
            )
            or provider_session.session_version != account.session_version
        ):
            raise InvalidAuthToken()
        return CoachPrincipal(
            account_id=account.id,
            session_id=provider_session.id,
            instructor_id=instructor.id,
            display_name=instructor.name,
            expires_at=_coerce_utc(provider_session.expires_at),
        )
    finally:
        session.close()


def revoke_presented_session(
    raw_token: Optional[str],
    *,
    reason: str = "sign_out",
    now: Optional[datetime] = None,
) -> bool:
    if not raw_token or not _valid_session_token(raw_token):
        return False
    revoked_at = _coerce_utc(now or _utc_now())
    session = db.SessionLocal()
    try:
        with session.begin():
            return _revoke_token_in_session(session, raw_token, revoked_at, reason)
    finally:
        session.close()


def reset_provider_password(email: str, new_password: str) -> None:
    normalized_email = normalize_provider_email(email)
    password_hash = hash_provider_password(new_password)
    now = _utc_now()
    session = db.SessionLocal()
    try:
        with session.begin():
            account = _locked_account_by_email(session, normalized_email)
            account.password_hash = password_hash
            account.password_changed_at = now
            account.updated_at = now
            _invalidate_all_sessions(session, account, now, "password_reset")
    finally:
        session.close()


def revoke_all_provider_sessions(email: str) -> None:
    normalized_email = normalize_provider_email(email)
    now = _utc_now()
    session = db.SessionLocal()
    try:
        with session.begin():
            account = _locked_account_by_email(session, normalized_email)
            account.updated_at = now
            _invalidate_all_sessions(session, account, now, "administrative_revocation")
    finally:
        session.close()


def disable_provider_account(email: str) -> None:
    normalized_email = normalize_provider_email(email)
    now = _utc_now()
    session = db.SessionLocal()
    try:
        with session.begin():
            account = _locked_account_by_email(session, normalized_email)
            account.disabled_at = now
            account.updated_at = now
            _invalidate_all_sessions(session, account, now, "account_disabled")
    finally:
        session.close()


def enable_provider_account(email: str) -> None:
    normalized_email = normalize_provider_email(email)
    now = _utc_now()
    session = db.SessionLocal()
    try:
        with session.begin():
            account = _locked_account_by_email(session, normalized_email)
            account.disabled_at = None
            account.updated_at = now
            _invalidate_all_sessions(session, account, now, "account_enabled")
    finally:
        session.close()


def seed_demo_provider_account() -> None:
    settings = application_settings_from_env()
    if not (settings.demo_data_enabled and settings.demo_auth_enabled):
        return
    session = db.SessionLocal()
    try:
        existing = (
            session.query(ProviderAccountDB)
            .filter(
                (ProviderAccountDB.email_normalized == DEMO_PROVIDER_EMAIL)
                | (ProviderAccountDB.instructor_id == DEMO_PROVIDER_INSTRUCTOR_ID)
            )
            .first()
        )
    finally:
        session.close()
    if existing is not None:
        if (
            existing.email_normalized != DEMO_PROVIDER_EMAIL
            or existing.instructor_id != DEMO_PROVIDER_INSTRUCTOR_ID
            or not existing.is_demo
        ):
            raise RuntimeError("The configured demo provider account conflicts with existing data.")
        return
    if existing is None:
        provision_provider_account(
            DEMO_PROVIDER_INSTRUCTOR_ID,
            DEMO_PROVIDER_EMAIL,
            DEMO_PROVIDER_PASSWORD,
            is_demo=True,
        )


def set_session_cookie(
    response: Response,
    issued: IssuedCoachSession,
    *,
    settings: Optional[ApplicationSettings] = None,
) -> None:
    active_settings = settings or application_settings_from_env()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=issued.raw_token,
        max_age=active_settings.coach_session_ttl_seconds,
        expires=issued.principal.expires_at,
        path=SESSION_COOKIE_PATH,
        secure=active_settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )


def clear_session_cookie(
    response: Response,
    *,
    settings: Optional[ApplicationSettings] = None,
) -> None:
    active_settings = settings or application_settings_from_env()
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=SESSION_COOKIE_PATH,
        secure=active_settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )


def _insert_provider_session(
    session,
    account: ProviderAccountDB,
    created_at: datetime,
    expires_at: datetime,
) -> tuple[str, ProviderSessionDB]:
    for _ in range(SESSION_TOKEN_COLLISION_RETRIES):
        raw_token = _generate_session_token()
        provider_session = ProviderSessionDB(
            id=f"sess_{uuid4().hex}",
            account_id=account.id,
            token_digest=_session_digest(raw_token),
            session_version=account.session_version,
            created_at=created_at,
            expires_at=expires_at,
        )
        try:
            with session.begin_nested():
                session.add(provider_session)
                session.flush()
            return raw_token, provider_session
        except IntegrityError:
            continue
    raise RuntimeError("A unique provider session could not be created.")


def _locked_account_by_email(session, normalized_email: str) -> ProviderAccountDB:
    account = (
        session.query(ProviderAccountDB)
        .filter(ProviderAccountDB.email_normalized == normalized_email)
        .with_for_update()
        .one_or_none()
    )
    if account is None:
        raise ProviderAuthAdminError("The provider account could not be found.")
    return account


def _invalidate_all_sessions(
    session,
    account: ProviderAccountDB,
    revoked_at: datetime,
    reason: str,
) -> None:
    account.session_version += 1
    (
        session.query(ProviderSessionDB)
        .filter(
            ProviderSessionDB.account_id == account.id,
            ProviderSessionDB.revoked_at.is_(None),
        )
        .update(
            {
                ProviderSessionDB.revoked_at: revoked_at,
                ProviderSessionDB.revocation_reason: reason,
            },
            synchronize_session=False,
        )
    )


def _revoke_token_in_session(
    session,
    raw_token: str,
    revoked_at: datetime,
    reason: str,
) -> bool:
    if not _valid_session_token(raw_token):
        return False
    provider_session = (
        session.query(ProviderSessionDB)
        .filter(ProviderSessionDB.token_digest == _session_digest(raw_token))
        .with_for_update()
        .one_or_none()
    )
    if provider_session is None:
        return False
    if provider_session.revoked_at is None:
        provider_session.revoked_at = revoked_at
        provider_session.revocation_reason = reason
    return True


def _verify_password(password_hash: str, password: str) -> bool:
    try:
        return bool(_PASSWORD_HASHER.verify(password_hash, password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _validate_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ProviderAuthAdminError(
            f"Provider passwords must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ProviderAuthAdminError(
            f"Provider passwords must be at most {MAX_PASSWORD_LENGTH} characters."
        )


def _generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def _valid_session_token(raw_token: str) -> bool:
    return bool(_SESSION_TOKEN_PATTERN.fullmatch(raw_token))


def _session_digest(raw_token: str) -> bytes:
    return hashlib.sha256(raw_token.encode("ascii")).digest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
