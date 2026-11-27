"""Credential-free lifecycle fences for Google Calendar availability."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal, NoReturn, final

from sqlalchemy import select as _select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as _Session

from app.external_calendar import ExternalCalendarUnavailable
from app.google_calendar_busy_source_factory import (
    _raise_control_exception,
    _raise_fresh,
)
from app.google_calendar_credential_claim import (
    _require_owned_idle_session,
    _require_transactional_connection,
)
from app.google_calendar_credential_handoff import (
    _validated_connection_id,
    _validated_identifier,
    _validated_revision,
)
from app.models import ProviderAccountDB, ProviderCalendarConnectionDB


__all__ = (
    "GoogleCalendarAvailabilityFence",
    "GoogleCalendarAvailabilityFenceError",
    "claim_google_calendar_availability_fence",
    "revalidate_google_calendar_availability_fence",
    "check_current_google_calendar_availability_fence",
    "lock_current_google_calendar_availability_fence",
)


_MAX_CONTROL_EXCEPTION_NODES = 4_096
_MAX_CONTROL_EXCEPTION_ENTRIES = _MAX_CONTROL_EXCEPTION_NODES * 2

GoogleCalendarAvailabilityState = Literal[
    "disconnected",
    "connected",
    "authorization_required",
]


class GoogleCalendarAvailabilityFenceError(ExternalCalendarUnavailable):
    """A connection generation could not be fenced safely."""

    def __init__(self) -> None:
        super().__init__("provider_unavailable")


@final
class GoogleCalendarAvailabilityFence:
    """Immutable credential-free identity for one connection generation."""

    __slots__ = (
        "state",
        "_account_id",
        "_instructor_id",
        "_revision",
        "_connection_id",
    )

    state: GoogleCalendarAvailabilityState
    _account_id: str
    _instructor_id: str
    _revision: int
    _connection_id: str | None

    def __init_subclass__(cls, **_kwargs: object) -> NoReturn:
        del cls, _kwargs
        _raise_fresh(
            TypeError("GoogleCalendarAvailabilityFence cannot be subclassed.")
        )

    def __init__(
        self,
        *,
        state: object,
        account_id: object,
        instructor_id: object,
        revision: object,
        connection_id: object,
    ) -> None:
        del (
            self,
            state,
            account_id,
            instructor_id,
            revision,
            connection_id,
        )
        _raise_fresh(
            TypeError(
                "GoogleCalendarAvailabilityFence cannot be constructed directly."
            )
        )

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        del self, _name, _value
        _raise_fresh(
            AttributeError("GoogleCalendarAvailabilityFence is immutable.")
        )

    def __delattr__(self, _name: str) -> NoReturn:
        del self, _name
        _raise_fresh(
            AttributeError("GoogleCalendarAvailabilityFence is immutable.")
        )

    def __reduce__(self) -> NoReturn:
        del self
        _raise_fresh(
            TypeError("GoogleCalendarAvailabilityFence is not serializable.")
        )

    def __reduce_ex__(self, _protocol: int) -> NoReturn:
        del self, _protocol
        _raise_fresh(
            TypeError("GoogleCalendarAvailabilityFence is not serializable.")
        )

    def __getstate__(self) -> NoReturn:
        del self
        _raise_fresh(
            TypeError("GoogleCalendarAvailabilityFence is not serializable.")
        )

    def __repr__(self) -> str:
        return "GoogleCalendarAvailabilityFence(<redacted>)"

    __str__ = __repr__


def claim_google_calendar_availability_fence(
    session_factory: Callable[[], _Session],
    *,
    instructor_id: str,
) -> GoogleCalendarAvailabilityFence:
    """Capture one enabled owner's credential-free connection generation."""

    try: return _claim_fence(session_factory, instructor_id=instructor_id)
    except BaseException as boundary_raised:
        try: session_factory = instructor_id = None; control_exception, system_exit_code = _classify_control_exception(boundary_raised); boundary_raised = None
        except BaseException as cleanup_raised: session_factory = instructor_id = boundary_raised = None; control_exception, system_exit_code = _classify_control_exception(cleanup_raised); cleanup_raised = None
    if control_exception is not None:
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    _raise_fence_error()


def revalidate_google_calendar_availability_fence(
    session_factory: Callable[[], _Session],
    *,
    fence: GoogleCalendarAvailabilityFence,
) -> None:
    """Reject a lifecycle fence changed since it was captured."""

    try: return _revalidate_fence(session_factory, fence=fence)
    except BaseException as boundary_raised:
        try: session_factory = fence = None; control_exception, system_exit_code = _classify_control_exception(boundary_raised); boundary_raised = None
        except BaseException as cleanup_raised: session_factory = fence = boundary_raised = None; control_exception, system_exit_code = _classify_control_exception(cleanup_raised); cleanup_raised = None
    if control_exception is not None:
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    _raise_fence_error()


def lock_current_google_calendar_availability_fence(
    session: _Session,
    *,
    fence: GoogleCalendarAvailabilityFence,
) -> None:
    """Keep a checked calendar generation stable until the booking commits."""
    _require_matching_fence(session, fence, lock=True)


def check_current_google_calendar_availability_fence(
    session: _Session,
    *,
    fence: GoogleCalendarAvailabilityFence,
) -> None:
    """Verify the calendar generation inside an existing consistent read."""
    _require_matching_fence(session, fence, lock=False)


def _require_matching_fence(session: _Session, fence: GoogleCalendarAvailabilityFence, *, lock: bool) -> None:
    try:
        expected = _validated_fence(fence)
        _, account_id, instructor_id, _, _ = expected
        with session.no_autoflush:
            if lock:
                current = _read_locked_fence(session, instructor_id=instructor_id, account_id=account_id)
            else:
                current = _read_calendar_fence(session, instructor_id=instructor_id, account_id=account_id, lock=False)
        if _validated_fence(current) == expected:
            return
    except (ValueError, SQLAlchemyError):
        pass
    raise GoogleCalendarAvailabilityFenceError() from None


def _claim_fence(
    session_factory: object,
    *,
    instructor_id: object,
) -> GoogleCalendarAvailabilityFence:
    trusted_instructor_id = _instructor_lookup_key(instructor_id)
    return _read_fence(
        session_factory,
        instructor_id=trusted_instructor_id,
        account_id=None,
    )


def _revalidate_fence(
    session_factory: object,
    *,
    fence: object,
) -> None:
    (
        expected_state,
        account_id,
        instructor_id,
        revision,
        connection_id,
    ) = _validated_fence(fence)
    current = _read_fence(
        session_factory,
        instructor_id=instructor_id,
        account_id=account_id,
    )
    if _validated_fence(current) != (
        expected_state,
        account_id,
        instructor_id,
        revision,
        connection_id,
    ):
        raise ValueError("stale calendar availability fence")


def _read_fence(
    session_factory: object,
    *,
    instructor_id: str,
    account_id: str | None,
) -> GoogleCalendarAvailabilityFence:
    if not callable(session_factory):
        raise ValueError("invalid calendar availability session factory")
    database_session = session_factory()
    if not isinstance(database_session, _Session):
        raise ValueError("invalid calendar availability database session")
    try:
        _require_owned_idle_session(database_session)
        with database_session.begin():
            _require_transactional_connection(database_session)
            return _read_locked_fence(
                database_session,
                instructor_id=instructor_id,
                account_id=account_id,
            )
    finally:
        database_session.close()


def _read_locked_fence(
    session: _Session,
    *,
    instructor_id: str,
    account_id: str | None,
) -> GoogleCalendarAvailabilityFence:
    return _read_calendar_fence(session, instructor_id=instructor_id, account_id=account_id, lock=True)


def _read_calendar_fence(
    session: _Session,
    *,
    instructor_id: str,
    account_id: str | None,
    lock: bool,
) -> GoogleCalendarAvailabilityFence:
    account_query = _select(
        ProviderAccountDB.id.label("account_id"),
        ProviderAccountDB.instructor_id,
        ProviderAccountDB.disabled_at,
        ProviderAccountDB.calendar_connection_revision,
    )
    if account_id is None:
        account_query = account_query.where(
            ProviderAccountDB.instructor_id == instructor_id
        )
    else:
        account_query = account_query.where(ProviderAccountDB.id == account_id)
    account = session.execute(account_query.with_for_update() if lock else account_query).one_or_none()
    if account is None:
        raise ValueError("missing calendar availability owner")
    persisted_account_id = _validated_identifier(account.account_id)
    persisted_instructor_id = _instructor_lookup_key(account.instructor_id)
    revision = _validated_revision(
        account.calendar_connection_revision,
        allow_zero=True,
    )
    if (
        persisted_instructor_id != instructor_id
        or account.disabled_at is not None
        or (account_id is not None and persisted_account_id != account_id)
    ):
        raise ValueError("inactive calendar availability owner")

    connection_query = (
        _select(
            ProviderCalendarConnectionDB.id.label("connection_id"),
            ProviderCalendarConnectionDB.account_id,
            ProviderCalendarConnectionDB.provider,
            ProviderCalendarConnectionDB.authorization_required_at,
        )
        .where(
            ProviderCalendarConnectionDB.account_id == persisted_account_id
        )
    )
    connection = session.execute(connection_query.with_for_update() if lock else connection_query).one_or_none()
    if connection is None:
        return _new_fence(
            state="disconnected",
            account_id=persisted_account_id,
            instructor_id=persisted_instructor_id,
            revision=revision,
            connection_id=None,
        )
    connection_id = _validated_connection_id(connection.connection_id)
    if (
        _validated_identifier(connection.account_id) != persisted_account_id
        or connection.provider != "google"
        or revision < 1
    ):
        raise ValueError("invalid calendar availability connection")
    return _new_fence(
        state=(
            "authorization_required"
            if connection.authorization_required_at is not None
            else "connected"
        ),
        account_id=persisted_account_id,
        instructor_id=persisted_instructor_id,
        revision=revision,
        connection_id=connection_id,
    )


def _new_fence(
    *,
    state: object,
    account_id: object,
    instructor_id: object,
    revision: object,
    connection_id: object,
) -> GoogleCalendarAvailabilityFence:
    trusted_state = _validated_state(state)
    trusted_account_id = _validated_identifier(account_id)
    trusted_instructor_id = _instructor_lookup_key(instructor_id)
    trusted_revision = _validated_revision(
        revision,
        allow_zero=trusted_state == "disconnected",
    )
    trusted_connection_id = (
        None
        if connection_id is None
        else _validated_connection_id(connection_id)
    )
    if (trusted_state == "disconnected") != (
        trusted_connection_id is None
    ):
        raise ValueError("invalid calendar availability fence state")
    result = object.__new__(GoogleCalendarAvailabilityFence)
    object.__setattr__(result, "state", trusted_state)
    object.__setattr__(result, "_account_id", trusted_account_id)
    object.__setattr__(result, "_instructor_id", trusted_instructor_id)
    object.__setattr__(result, "_revision", trusted_revision)
    object.__setattr__(result, "_connection_id", trusted_connection_id)
    return result


def _validated_fence(
    value: object,
) -> tuple[GoogleCalendarAvailabilityState, str, str, int, str | None]:
    if type(value) is not GoogleCalendarAvailabilityFence:
        raise ValueError("invalid calendar availability fence")
    state = _validated_state(value.state)
    account_id = _validated_identifier(value._account_id)
    instructor_id = _instructor_lookup_key(value._instructor_id)
    revision = _validated_revision(
        value._revision,
        allow_zero=state == "disconnected",
    )
    connection_id = (
        None
        if value._connection_id is None
        else _validated_connection_id(value._connection_id)
    )
    if (state == "disconnected") != (connection_id is None):
        raise ValueError("invalid calendar availability fence state")
    return state, account_id, instructor_id, revision, connection_id


def _instructor_lookup_key(value: object) -> str:
    # Use the stored key as-is when looking up connection state.
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError("invalid instructor lookup key")
    return value


def _validated_state(value: object) -> GoogleCalendarAvailabilityState:
    if type(value) is not str or value not in (
        "disconnected",
        "connected",
        "authorization_required",
    ):
        raise ValueError("invalid calendar availability state")
    return value


def _classify_control_exception(
    error: BaseException,
) -> tuple[str | None, int | None]:
    cancellation_found = False
    overflowed = False
    pending = [error]
    seen: set[int] = set()
    traversed_entries = 0
    while pending:
        current = pending.pop()
        traversed_entries += 1
        if traversed_entries > _MAX_CONTROL_EXCEPTION_ENTRIES:
            return "cancelled", None
        identity = id(current)
        if identity in seen:
            continue
        if len(seen) >= _MAX_CONTROL_EXCEPTION_NODES:
            return "cancelled", None
        seen.add(identity)
        control_exception, system_exit_code = (
            _classify_direct_control_exception(current)
        )
        if control_exception not in (None, "cancelled"):
            return control_exception, system_exit_code
        cancellation_found = cancellation_found or (
            control_exception == "cancelled"
        )
        if isinstance(current, BaseExceptionGroup):
            nested = BaseExceptionGroup.exceptions.__get__(
                current,
                BaseExceptionGroup,
            )
        else:
            nested = ()
        lineage = tuple(
            value
            for value in (
                BaseException.__cause__.__get__(current, BaseException),
                BaseException.__context__.__get__(current, BaseException),
            )
            if value is not None
        )
        linked_count = len(nested) + len(lineage)
        if linked_count:
            available_entries = max(
                _MAX_CONTROL_EXCEPTION_ENTRIES
                - traversed_entries
                - len(pending),
                0,
            )
            lineage_to_schedule = min(len(lineage), available_entries)
            nested_to_schedule = min(
                len(nested),
                available_entries - lineage_to_schedule,
            )
            overflowed = overflowed or (
                lineage_to_schedule + nested_to_schedule < linked_count
            )
            for index in range(nested_to_schedule - 1, -1, -1):
                pending.append(nested[index])
            for index in range(lineage_to_schedule - 1, -1, -1):
                pending.append(lineage[index])
    if overflowed or cancellation_found:
        return "cancelled", None
    return None, None


def _classify_direct_control_exception(
    error: BaseException,
) -> tuple[str | None, int | None]:
    if isinstance(error, asyncio.CancelledError):
        return "cancelled", None
    if isinstance(error, KeyboardInterrupt):
        return "keyboard_interrupt", None
    if isinstance(error, SystemExit):
        code = error.code if type(error) is SystemExit else 1
        if type(code) is not int and code is not None:
            code = 1
        return "system_exit", code
    if isinstance(error, GeneratorExit):
        return "generator_exit", None
    return None, None


def _raise_fence_error() -> NoReturn:
    _raise_fresh(GoogleCalendarAvailabilityFenceError())
