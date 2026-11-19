"""Per-application factory for short-lived Google Calendar busy sources."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import NoReturn, final

from sqlalchemy.orm import Session as _Session

from app import config, db
from app.external_calendar import CalendarBusySource, ExternalCalendarUnavailable


__all__ = ("GoogleCalendarBusySourceFactory",)

_MAX_CONTROL_EXCEPTION_NODES = 4_096
_MAX_CONTROL_EXCEPTION_ENTRIES = _MAX_CONTROL_EXCEPTION_NODES * 2


@final
class GoogleCalendarBusySourceFactory:
    """Create fresh sources without retaining deployment secrets."""

    __slots__ = ()

    def __init_subclass__(cls, **_kwargs: object) -> NoReturn:
        del cls, _kwargs
        _raise_fresh(
            TypeError("GoogleCalendarBusySourceFactory cannot be subclassed.")
        )

    def create(self) -> CalendarBusySource:
        """Load current secrets and return one uncached source."""

        # The first traced instruction is already protected. The returned
        # source is handed to scheduling immediately; it is never app state.
        try: return _create_google_calendar_busy_source(db.SessionLocal)
        except BaseException as boundary_raised:
            try: self = None; control_exception, system_exit_code = _classify_control_exception(boundary_raised); boundary_raised = None
            except BaseException as cleanup_raised: self = boundary_raised = None; control_exception, system_exit_code = _classify_control_exception(cleanup_raised); cleanup_raised = None
        if control_exception is not None:
            _raise_control_exception(
                control_exception,
                system_exit_code=system_exit_code,
            )
        _raise_unavailable()

    def create_for(
        self,
        session_factory: Callable[[], _Session],
    ) -> CalendarBusySource:
        """Create one source bound to a caller-captured database factory."""

        try: return _create_google_calendar_busy_source(session_factory)
        except BaseException as boundary_raised:
            try: self = session_factory = None; control_exception, system_exit_code = _classify_control_exception(boundary_raised); boundary_raised = None
            except BaseException as cleanup_raised: self = session_factory = boundary_raised = None; control_exception, system_exit_code = _classify_control_exception(cleanup_raised); cleanup_raised = None
        if control_exception is not None:
            _raise_control_exception(
                control_exception,
                system_exit_code=system_exit_code,
            )
        _raise_unavailable()

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        del self, _name, _value
        _raise_fresh(
            AttributeError("GoogleCalendarBusySourceFactory is immutable.")
        )

    def __delattr__(self, _name: str) -> NoReturn:
        del self, _name
        _raise_fresh(
            AttributeError("GoogleCalendarBusySourceFactory is immutable.")
        )

    def __reduce__(self) -> NoReturn:
        del self
        _raise_fresh(
            TypeError(
                "GoogleCalendarBusySourceFactory is not serializable."
            )
        )

    def __reduce_ex__(self, _protocol: int) -> NoReturn:
        del self, _protocol
        _raise_fresh(
            TypeError(
                "GoogleCalendarBusySourceFactory is not serializable."
            )
        )

    def __getstate__(self) -> NoReturn:
        del self
        _raise_fresh(
            TypeError(
                "GoogleCalendarBusySourceFactory is not serializable."
            )
        )

    def __repr__(self) -> str:
        return "GoogleCalendarBusySourceFactory(<redacted>)"

    __str__ = __repr__


def _create_google_calendar_busy_source(
    session_factory: Callable[[], _Session],
) -> CalendarBusySource:
    if not callable(session_factory):
        raise ValueError("Calendar busy source session factory is invalid.")
    configuration = credential_cipher = source = None
    try:
        configuration = (
            config.google_calendar_token_exchange_configuration_from_env()
        )
        credential_cipher = config.calendar_credential_cipher_from_env()
        source = _new_google_calendar_busy_source(
            session_factory,
            configuration=configuration,
            credential_cipher=credential_cipher,
        )
        return source
    finally:
        try: configuration = credential_cipher = source = None
        except BaseException:
            configuration = credential_cipher = source = None
            raise


def _new_google_calendar_busy_source(
    session_factory: Callable[[], _Session],
    *,
    configuration: object,
    credential_cipher: object,
) -> CalendarBusySource:
    from app.calendar_credentials import CalendarCredentialCipher
    from app.google_calendar_busy_source import GoogleCalendarBusySource
    from app.google_calendar_token_exchange import (
        GoogleCalendarTokenExchangeConfiguration,
    )

    if (
        type(configuration) is not GoogleCalendarTokenExchangeConfiguration
        or type(credential_cipher) is not CalendarCredentialCipher
    ):
        raise ValueError("Calendar busy source configuration is invalid.")
    return GoogleCalendarBusySource(
        session_factory,
        configuration=configuration,
        credential_cipher=credential_cipher,
    )


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


def _raise_control_exception(
    kind: str,
    *,
    system_exit_code: int | None,
) -> NoReturn:
    if kind == "cancelled":
        error: BaseException = asyncio.CancelledError()
    elif kind == "keyboard_interrupt":
        error: BaseException = KeyboardInterrupt()
    elif kind == "system_exit":
        error = SystemExit(system_exit_code)
    else:
        error = GeneratorExit()
    _raise_fresh(error)


def _raise_unavailable() -> NoReturn:
    _raise_fresh(ExternalCalendarUnavailable("provider_unavailable"))


def _raise_fresh(error: BaseException) -> NoReturn:
    error.__cause__ = None
    error.__context__ = None
    error.__suppress_context__ = True
    error.__traceback__ = None
    try:
        raise error.with_traceback(None) from None
    except BaseException as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise
