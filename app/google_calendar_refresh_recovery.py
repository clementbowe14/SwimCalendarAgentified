"""Claim-fenced persistence for Google Calendar refresh recovery."""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import Callable
from typing import NoReturn

from sqlalchemy import select as _select, update as _update
from sqlalchemy.engine import Engine as _Engine
from sqlalchemy.orm import Session as _Session

from app.google_calendar_credential_handoff import (
    GoogleCalendarRefreshCredentialClaim,
    _validated_claim_fence,
    _validated_connection_id,
    _validated_identifier,
    _validated_instructor_id,
    _validated_revision,
)
from app.google_calendar_oauth import GoogleCalendarOAuthError
from app.google_calendar_token_exchange import (
    GoogleCalendarRefreshAuthorizationRequired,
)
from app.models import ProviderAccountDB, ProviderCalendarConnectionDB


__all__ = (
    "GoogleCalendarRefreshRecoveryError",
    "mark_google_calendar_refresh_credential_authorization_required",
)


_RECOVERY_MODELS = (ProviderAccountDB, ProviderCalendarConnectionDB)
_MAX_CONTROL_EXCEPTION_NODES = 4_096


class GoogleCalendarRefreshRecoveryError(GoogleCalendarOAuthError):
    """An exact refresh generation could not be marked safely."""

    def __init__(self) -> None:
        super().__init__(
            "Google Calendar refresh recovery could not be persisted safely."
        )


def mark_google_calendar_refresh_credential_authorization_required(
    session_factory: Callable[[], _Session],
    *,
    claim: GoogleCalendarRefreshCredentialClaim,
    outcome: GoogleCalendarRefreshAuthorizationRequired,
) -> None:
    """Mark only the exact credential generation that failed refresh."""

    # The first traced instruction is inside the handler. Keyword values are
    # captured before the final tuple clears the public frame's references.
    try: return _mark_google_calendar_refresh_credential_authorization_required(session_factory, claim=claim, outcome=outcome, _cleared=(session_factory := None, claim := None, outcome := None))
    except BaseException as boundary_raised:
        try: session_factory = claim = outcome = None; control_exception, system_exit_code = _safe_classified_recovery_control_exception(boundary_raised); boundary_raised = None
        except BaseException as cleanup_raised: session_factory = claim = outcome = boundary_raised = None; control_exception, system_exit_code = _safe_classified_recovery_control_exception(cleanup_raised); cleanup_raised = None
    if control_exception is not None:
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    _raise_recovery_error()


def _mark_google_calendar_refresh_credential_authorization_required(
    session_factory: object,
    *,
    claim: object,
    outcome: object,
    _cleared: object = None,
) -> None:
    del _cleared
    (
        account_id,
        instructor_id,
        connection_id,
        expected_revision,
    ) = _validated_authorization_required_fence(
        claim,
        outcome=outcome,
    )
    del claim, outcome
    if not callable(session_factory):
        raise ValueError("invalid session factory")

    database_session = session_factory()
    del session_factory
    if not isinstance(database_session, _Session):
        raise ValueError("invalid database session")
    primary_failure: tuple[str | None, int | None] | None = None
    cleanup_failures: list[tuple[str | None, int | None]] = []
    try:
        _require_owned_idle_session(database_session)
        transaction = database_session.begin()
        try:
            _require_transactional_connection(database_session)
            _mark_locked_refresh_credential_authorization_required(
                database_session,
                account_id=account_id,
                instructor_id=instructor_id,
                connection_id=connection_id,
                expected_revision=expected_revision,
            )
        except BaseException as operation_raised:
            primary_failure = _safe_classified_recovery_control_exception(
                operation_raised
            )
            operation_raised = None
            try:
                transaction.rollback()
            except BaseException as rollback_raised:
                cleanup_failures.append(
                    _safe_classified_recovery_control_exception(
                        rollback_raised
                    )
                )
                rollback_raised = None
        else:
            try:
                transaction.commit()
            except BaseException as commit_raised:
                primary_failure = _safe_classified_recovery_control_exception(
                    commit_raised
                )
                commit_raised = None
                try:
                    transaction.rollback()
                except BaseException as rollback_raised:
                    cleanup_failures.append(
                        _safe_classified_recovery_control_exception(
                            rollback_raised
                        )
                    )
                    rollback_raised = None
    except BaseException as transaction_raised:
        if primary_failure is None:
            primary_failure = _safe_classified_recovery_control_exception(
                transaction_raised
            )
        else:
            cleanup_failures.append(
                _safe_classified_recovery_control_exception(
                    transaction_raised
                )
            )
        transaction_raised = None
    finally:
        try:
            database_session.close()
        except BaseException as close_raised:
            cleanup_failures.append(
                _safe_classified_recovery_control_exception(close_raised)
            )
            close_raised = None
    del database_session
    _raise_recovery_failure(
        primary_failure,
        cleanup_failures=tuple(cleanup_failures),
    )


def _validated_authorization_required_fence(
    claim: object,
    *,
    outcome: object,
) -> tuple[str, str, str, int]:
    trusted_fence = _validated_claim_fence(claim)
    if type(outcome) is not GoogleCalendarRefreshAuthorizationRequired:
        raise ValueError("invalid authorization-required outcome")
    outcome_fence = (
        _validated_identifier(outcome.account_id),
        _validated_instructor_id(outcome.instructor_id),
        _validated_connection_id(outcome.connection_id),
        _validated_revision(
            outcome.expected_calendar_connection_revision,
            allow_zero=False,
        ),
    )
    if outcome_fence != trusted_fence:
        raise ValueError("authorization-required outcome changed")
    return trusted_fence


def _require_owned_idle_session(session: _Session) -> None:
    database_bind = session.get_bind()
    if session.get_transaction() is not None or not isinstance(
        database_bind,
        _Engine,
    ):
        raise ValueError("invalid database session")
    for model in _RECOVERY_MODELS:
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


def _mark_locked_refresh_credential_authorization_required(
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
            ProviderCalendarConnectionDB.updated_at,
        )
        .where(ProviderCalendarConnectionDB.account_id == account_id)
        .with_for_update()
    ).one_or_none()
    if (
        connection is None
        or _validated_connection_id(connection.connection_id) != connection_id
        or connection.provider != "google"
    ):
        raise ValueError("stale credential connection")
    if connection.authorization_required_at is not None:
        return

    persisted_updated_at = _validated_persisted_timestamp(
        connection.updated_at
    )
    marked_at = max(
        datetime.datetime.now(datetime.timezone.utc),
        persisted_updated_at,
    )
    result = session.execute(
        _update(ProviderCalendarConnectionDB)
        .where(
            ProviderCalendarConnectionDB.account_id == account_id,
            ProviderCalendarConnectionDB.id == connection_id,
            ProviderCalendarConnectionDB.provider == "google",
            ProviderCalendarConnectionDB.authorization_required_at.is_(None),
        )
        .values(
            authorization_required_at=marked_at,
            updated_at=marked_at,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise ValueError("stale credential connection")


def _validated_persisted_timestamp(value: object) -> datetime.datetime:
    if (
        type(value) is not datetime.datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("invalid credential timestamp")
    return value.astimezone(datetime.timezone.utc)


def _classified_recovery_control_exception(
    raised: BaseException,
) -> tuple[str | None, int | None]:
    return _emergency_classified_recovery_control_exception(raised)


def _safe_classified_recovery_control_exception(
    raised: BaseException,
) -> tuple[str | None, int | None]:
    try:
        return _classified_recovery_control_exception(raised)
    except BaseException as classifier_raised:
        primary_control = _emergency_classified_recovery_control_exception(
            raised
        )
        classifier_control = _emergency_classified_recovery_control_exception(
            classifier_raised
        )
        classifier_raised = None
    for outcome in (primary_control, classifier_control):
        if outcome[0] not in (None, "cancelled"):
            return outcome
    if (
        primary_control[0] == "cancelled"
        or classifier_control[0] == "cancelled"
    ):
        return "cancelled", None
    return None, None


def _emergency_classified_recovery_control_exception(
    raised: BaseException,
) -> tuple[str | None, int | None]:
    try:
        return _classify_recovery_control_exception_graph(raised)
    except BaseException as graph_raised:
        primary_control = _direct_recovery_control_exception(raised)
        graph_control = _direct_recovery_control_exception(graph_raised)
        graph_raised = None
    for outcome in (primary_control, graph_control):
        if outcome[0] not in (None, "cancelled"):
            return outcome
    return "cancelled", None


def _direct_recovery_control_exception(
    raised: BaseException,
) -> tuple[str | None, int | None]:
    try:
        if isinstance(raised, KeyboardInterrupt):
            return "keyboard_interrupt", None
        if isinstance(raised, SystemExit):
            if type(raised) is not SystemExit:
                return "system_exit", 1
            code = raised.code
            return (
                "system_exit",
                code if type(code) is int or code is None else 1,
            )
        if isinstance(raised, GeneratorExit):
            return "generator_exit", None
        if isinstance(raised, asyncio.CancelledError):
            return "cancelled", None
    except BaseException:
        return "cancelled", None
    return None, None


def _classify_recovery_control_exception_graph(
    raised: BaseException,
) -> tuple[str | None, int | None]:
    pending = [raised]
    seen: set[int] = set()
    cancellation_seen = False
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        if len(seen) >= _MAX_CONTROL_EXCEPTION_NODES:
            return "cancelled", None
        seen.add(identity)
        if isinstance(current, KeyboardInterrupt):
            return "keyboard_interrupt", None
        if isinstance(current, SystemExit):
            if type(current) is not SystemExit:
                return "system_exit", 1
            code = current.code
            return (
                "system_exit",
                code if type(code) is int or code is None else 1,
            )
        if isinstance(current, GeneratorExit):
            return "generator_exit", None
        if isinstance(current, asyncio.CancelledError):
            cancellation_seen = True
        if isinstance(current, BaseExceptionGroup):
            nested = BaseExceptionGroup.exceptions.__get__(
                current,
                BaseExceptionGroup,
            )
            pending.extend(reversed(nested))
    if cancellation_seen:
        return "cancelled", None
    return None, None


def _raise_recovery_failure(
    primary_failure: tuple[str | None, int | None] | None,
    *,
    cleanup_failures: tuple[tuple[str | None, int | None], ...],
) -> None:
    failures = (
        (() if primary_failure is None else (primary_failure,))
        + cleanup_failures
    )
    if not failures:
        return
    for control_exception, system_exit_code in failures:
        if control_exception not in (None, "cancelled"):
            _raise_control_exception(
                control_exception,
                system_exit_code=system_exit_code,
            )
    if any(
        control_exception == "cancelled"
        for control_exception, _system_exit_code in failures
    ):
        _raise_control_exception("cancelled", system_exit_code=None)
    raise ValueError("credential recovery failed") from None


def _raise_recovery_error() -> NoReturn:
    error = GoogleCalendarRefreshRecoveryError()
    try:
        raise error from None
    except GoogleCalendarRefreshRecoveryError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_control_exception(
    kind: str,
    *,
    system_exit_code: int | None,
) -> NoReturn:
    if kind == "cancelled":
        error: BaseException = asyncio.CancelledError()
    elif kind == "keyboard_interrupt":
        error = KeyboardInterrupt()
    elif kind == "system_exit":
        error = SystemExit(system_exit_code)
    elif kind == "generator_exit":
        error = GeneratorExit()
    else:
        _raise_recovery_error()
    try:
        raise error from None
    except BaseException as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise
