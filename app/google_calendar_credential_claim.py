"""Sealed refresh-credential handoff for bounded Google Calendar use."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal, TypeAlias

from sqlalchemy import select as _select
from sqlalchemy.engine import Engine as _Engine
from sqlalchemy.orm import Session as _Session

from app.calendar_credentials import (
    CalendarCredentialCipher,
    CalendarCredentialScope,
    EncryptedCalendarCredential,
)
from app.google_calendar_credential_handoff import (
    GoogleCalendarRefreshCredentialClaim,
    GoogleCalendarRefreshCredentialClaimError,
    _new_claim,
    _raise_claim_error,
    _raise_control_exception,
    _validated_claim_fence,
    _validated_connection_id,
    _validated_identifier,
    _validated_instructor_id,
    _validated_revision,
)
from app.models import ProviderAccountDB, ProviderCalendarConnectionDB


__all__ = (
    "GoogleCalendarRefreshCredentialClaim",
    "GoogleCalendarRefreshCredentialClaimError",
    "GoogleCalendarRefreshCredentialClaimResult",
    "GoogleCalendarRefreshCredentialState",
    "claim_google_calendar_refresh_credential",
    "revalidate_google_calendar_refresh_credential_claim",
)


_CLAIM_MODELS = (ProviderAccountDB, ProviderCalendarConnectionDB)


GoogleCalendarRefreshCredentialState: TypeAlias = Literal[
    "disconnected",
    "authorization_required",
]


GoogleCalendarRefreshCredentialClaimResult: TypeAlias = (
    GoogleCalendarRefreshCredentialClaim
    | GoogleCalendarRefreshCredentialState
)


def claim_google_calendar_refresh_credential(
    session_factory: Callable[[], _Session],
    *,
    instructor_id: str,
    credential_cipher: CalendarCredentialCipher,
) -> GoogleCalendarRefreshCredentialClaimResult:
    """Claim one persisted refresh credential after releasing every lock."""

    control_exception: str | None = None
    system_exit_code: int | None = None
    try:
        return _claim_google_calendar_refresh_credential(
            session_factory,
            instructor_id=instructor_id,
            credential_cipher=credential_cipher,
        )
    except asyncio.CancelledError:
        control_exception = "cancelled"
    except KeyboardInterrupt:
        control_exception = "keyboard_interrupt"
    except SystemExit as raised:
        control_exception = "system_exit"
        if type(raised) is SystemExit:
            system_exit_code = (
                raised.code
                if type(raised.code) is int or raised.code is None
                else 1
            )
        else:
            system_exit_code = 1
    except GeneratorExit:
        control_exception = "generator_exit"
    except BaseException:
        pass
    del session_factory, instructor_id, credential_cipher
    if control_exception is not None:
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    _raise_claim_error()


def _claim_google_calendar_refresh_credential(
    session_factory: object,
    *,
    instructor_id: object,
    credential_cipher: object,
) -> GoogleCalendarRefreshCredentialClaimResult:
    trusted_instructor_id = _validated_instructor_id(instructor_id)
    if type(credential_cipher) is not CalendarCredentialCipher:
        raise ValueError("invalid credential cipher")
    if not callable(session_factory):
        raise ValueError("invalid session factory")

    database_session = session_factory()
    if not isinstance(database_session, _Session):
        raise ValueError("invalid database session")
    try:
        _require_owned_idle_session(database_session)
        with database_session.begin():
            _require_transactional_connection(database_session)
            result = _claim_locked_refresh_credential(
                database_session,
                instructor_id=trusted_instructor_id,
                credential_cipher=credential_cipher,
            )
    finally:
        database_session.close()

    if type(result) is str:
        if result not in ("disconnected", "authorization_required"):
            raise ValueError("invalid credential state")
        return result
    (
        account_id,
        persisted_instructor_id,
        connection_id,
        expected_revision,
        refresh_token,
    ) = result
    return _new_claim(
        account_id=account_id,
        instructor_id=persisted_instructor_id,
        connection_id=connection_id,
        expected_calendar_connection_revision=expected_revision,
        refresh_token=refresh_token,
    )


def revalidate_google_calendar_refresh_credential_claim(
    session_factory: Callable[[], _Session],
    *,
    claim: GoogleCalendarRefreshCredentialClaim,
) -> None:
    """Reject a credential claim changed during its network gap."""

    control_exception: str | None = None
    system_exit_code: int | None = None
    try:
        _revalidate_google_calendar_refresh_credential_claim(
            session_factory,
            claim=claim,
        )
        return
    except asyncio.CancelledError:
        control_exception = "cancelled"
    except KeyboardInterrupt:
        control_exception = "keyboard_interrupt"
    except SystemExit as raised:
        control_exception = "system_exit"
        if type(raised) is SystemExit:
            system_exit_code = (
                raised.code
                if type(raised.code) is int or raised.code is None
                else 1
            )
        else:
            system_exit_code = 1
    except GeneratorExit:
        control_exception = "generator_exit"
    except BaseException:
        pass
    del session_factory, claim
    if control_exception is not None:
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    _raise_claim_error()


def _revalidate_google_calendar_refresh_credential_claim(
    session_factory: object,
    *,
    claim: object,
) -> None:
    (
        account_id,
        instructor_id,
        connection_id,
        expected_revision,
    ) = _validated_claim_fence(claim)
    if not callable(session_factory):
        raise ValueError("invalid session factory")

    database_session = session_factory()
    if not isinstance(database_session, _Session):
        raise ValueError("invalid database session")
    try:
        _require_owned_idle_session(database_session)
        with database_session.begin():
            _require_transactional_connection(database_session)
            _revalidate_locked_refresh_credential_claim(
                database_session,
                account_id=account_id,
                instructor_id=instructor_id,
                connection_id=connection_id,
                expected_revision=expected_revision,
            )
    finally:
        database_session.close()


def _require_owned_idle_session(session: _Session) -> None:
    database_bind = session.get_bind()
    if session.get_transaction() is not None or not isinstance(
        database_bind,
        _Engine,
    ):
        raise ValueError("invalid database session")
    for model in _CLAIM_MODELS:
        if session.get_bind(mapper=model) is not database_bind:
            raise ValueError("invalid database session")


def _require_transactional_connection(session: _Session) -> None:
    connection = session.connection()
    configured_isolation = connection.get_execution_options().get(
        "isolation_level"
    )
    proxied_connection = connection.connection
    driver_connection = getattr(
        proxied_connection,
        "driver_connection",
        proxied_connection,
    )
    driver_autocommit = getattr(driver_connection, "autocommit", None)
    sqlite_isolation = getattr(driver_connection, "isolation_level", object())
    if (
        type(configured_isolation) is str
        and configured_isolation.upper() == "AUTOCOMMIT"
    ) or driver_autocommit is True or (
        connection.dialect.name == "sqlite" and sqlite_isolation is None
    ):
        raise ValueError("autocommit database session")


def _claim_locked_refresh_credential(
    session: _Session,
    *,
    instructor_id: str,
    credential_cipher: CalendarCredentialCipher,
) -> (
    GoogleCalendarRefreshCredentialState
    | tuple[str, str, str, int, str]
):
    (
        account_id,
        persisted_instructor_id,
        current_revision,
    ) = _lock_refresh_credential_owner(
        session,
        instructor_id=instructor_id,
    )

    connection = session.execute(
        _select(
            ProviderCalendarConnectionDB.id.label("connection_id"),
            ProviderCalendarConnectionDB.provider,
            ProviderCalendarConnectionDB.credential_version,
            ProviderCalendarConnectionDB.credential_key_id,
            ProviderCalendarConnectionDB.credential_nonce,
            ProviderCalendarConnectionDB.credential_ciphertext,
            ProviderCalendarConnectionDB.authorization_required_at,
        )
        .where(ProviderCalendarConnectionDB.account_id == account_id)
        .with_for_update()
    ).one_or_none()
    if connection is None:
        return "disconnected"

    connection_id = _validated_connection_id(connection.connection_id)
    if connection.provider != "google" or current_revision < 1:
        raise ValueError("invalid credential connection")
    if connection.authorization_required_at is not None:
        return "authorization_required"

    envelope = EncryptedCalendarCredential(
        version=connection.credential_version,
        key_id=connection.credential_key_id,
        nonce=bytes(connection.credential_nonce),
        ciphertext=bytes(connection.credential_ciphertext),
    )
    scope = CalendarCredentialScope(
        instructor_id=persisted_instructor_id,
        connection_id=connection_id,
        provider="google",
    )
    refresh_token = credential_cipher.decrypt_refresh_token(
        envelope,
        scope=scope,
    )
    if type(refresh_token) is not str or not refresh_token:
        raise ValueError("invalid refresh credential")
    return (
        account_id,
        persisted_instructor_id,
        connection_id,
        current_revision,
        refresh_token,
    )


def _lock_refresh_credential_owner(
    session: _Session,
    *,
    instructor_id: str,
) -> tuple[str, str, int]:
    account = session.execute(
        _select(
            ProviderAccountDB.id.label("account_id"),
            ProviderAccountDB.instructor_id,
            ProviderAccountDB.disabled_at,
            ProviderAccountDB.calendar_connection_revision,
        )
        .where(ProviderAccountDB.instructor_id == instructor_id)
        .with_for_update()
    ).one_or_none()
    if account is None:
        raise ValueError("missing credential owner")

    account_id = _validated_identifier(account.account_id)
    persisted_instructor_id = _validated_instructor_id(account.instructor_id)
    current_revision = _validated_revision(
        account.calendar_connection_revision,
        allow_zero=True,
    )
    if (
        persisted_instructor_id != instructor_id
        or account.disabled_at is not None
    ):
        raise ValueError("inactive credential owner")
    return account_id, persisted_instructor_id, current_revision


def _revalidate_locked_refresh_credential_claim(
    session: _Session,
    *,
    account_id: str,
    instructor_id: str,
    connection_id: str,
    expected_revision: int,
) -> None:
    account = session.execute(
        _select(
            ProviderAccountDB.id.label("account_id"),
            ProviderAccountDB.instructor_id,
            ProviderAccountDB.disabled_at,
            ProviderAccountDB.calendar_connection_revision,
        )
        .where(ProviderAccountDB.id == account_id)
        .with_for_update()
    ).one_or_none()
    if account is None:
        raise ValueError("missing credential owner")
    persisted_account_id = _validated_identifier(account.account_id)
    persisted_instructor_id = _validated_instructor_id(account.instructor_id)
    current_revision = _validated_revision(
        account.calendar_connection_revision,
        allow_zero=False,
    )
    if (
        persisted_account_id != account_id
        or persisted_instructor_id != instructor_id
        or account.disabled_at is not None
        or current_revision != expected_revision
    ):
        raise ValueError("stale credential owner")

    connection = session.execute(
        _select(
            ProviderCalendarConnectionDB.id.label("connection_id"),
            ProviderCalendarConnectionDB.provider,
            ProviderCalendarConnectionDB.authorization_required_at,
        )
        .where(ProviderCalendarConnectionDB.account_id == account_id)
        .with_for_update()
    ).one_or_none()
    if (
        connection is None
        or _validated_connection_id(connection.connection_id) != connection_id
        or connection.provider != "google"
        or connection.authorization_required_at is not None
    ):
        raise ValueError("stale credential connection")


# Imported handoff helpers remain module attributes for internal compatibility.
