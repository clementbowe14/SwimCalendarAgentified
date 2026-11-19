"""Synchronous provider-neutral adapter for verified Google busy snapshots."""

from __future__ import annotations

import asyncio
import datetime
import threading
import types
from collections.abc import Callable, Coroutine
from typing import NoReturn, final

from sqlalchemy.orm import Session as _Session

from app.calendar_credentials import CalendarCredentialCipher
from app.external_calendar import (
    CalendarBusyFailureReason,
    CalendarBusyInterval,
    CalendarBusySnapshot,
    ExternalCalendarAuthorizationRequired,
    ExternalCalendarInvalidResponse,
    ExternalCalendarTimeout,
    ExternalCalendarUnavailable,
    normalize_busy_intervals,
)
from app.google_calendar_freebusy_service import (
    query_google_calendar_busy_snapshot,
)
from app.google_calendar_token_exchange import (
    GoogleCalendarTokenExchangeConfiguration,
)


__all__ = ("GoogleCalendarBusySource",)


_MAX_CONTROL_EXCEPTION_NODES = 4_096
_MALFORMED_COROUTINE_CLOSE_SECONDS = 0.1
_MALFORMED_COROUTINE_CLEANUP_CAPACITY = 4
_ACTIVE_MALFORMED_COROUTINE_LEASES: dict[
    int,
    _MalformedCoroutineCleanupLease,
] = {}
_FAILURE_REASONS = (
    "authorization_required",
    "timeout",
    "provider_unavailable",
    "invalid_response",
)


@final
class _MalformedCoroutineCleanupLease:
    """Single-owner permit and quarantine for one provider result."""

    __slots__ = ("_lock", "_state", "_candidate")
    __init__ = None

    def quarantine(self, candidate: object) -> bool:
        with self._lock:
            if self._state == "caller":
                self._candidate = candidate
                _ACTIVE_MALFORMED_COROUTINE_LEASES[id(self)] = self
                self._state = "quarantine"
                return True
            if self._candidate is candidate and self._state in (
                "quarantine",
                "worker",
                "caller_gone_quarantine",
                "caller_gone_worker",
            ):
                _ACTIVE_MALFORMED_COROUTINE_LEASES[id(self)] = self
                return True
            return False

    def claim_worker(self) -> object:
        with self._lock:
            if self._state == "quarantine":
                self._state = "worker"
            elif self._state == "caller_gone_quarantine":
                self._state = "caller_gone_worker"
            else:
                return None
            return self._candidate

    def release_caller(self) -> None:
        with self._lock:
            if self._state == "caller":
                self._candidate = None
                self._state = "returning"
            elif self._state == "quarantine":
                self._state = "caller_gone_quarantine"
            elif self._state == "worker":
                self._state = "caller_gone_worker"
            elif self._state == "worker_done":
                self._state = "returning"
            if self._state == "returning":
                self._state = "available"
            available = self._state == "available"
        if available:
            _return_malformed_coroutine_cleanup_lease(self)

    def release_worker(self) -> bool:
        with self._lock:
            if self._state == "worker":
                self._candidate = None
                _ACTIVE_MALFORMED_COROUTINE_LEASES.pop(id(self), None)
                self._state = "worker_done"
            elif self._state == "caller_gone_worker":
                self._candidate = None
                _ACTIVE_MALFORMED_COROUTINE_LEASES.pop(id(self), None)
                self._state = "returning"
            if self._state == "returning":
                self._state = "available"
            available = self._state == "available"
            released = available or self._state == "worker_done"
        if available:
            _return_malformed_coroutine_cleanup_lease(self)
        return released

    def __del__(self) -> None:
        try:
            with self._lock:
                replace = self._state in (
                    "available",
                    "caller",
                    "returning",
                    "worker_done",
                )
                if replace:
                    self._candidate = None
                    self._state = "finalized"
            if replace:
                _return_malformed_coroutine_cleanup_lease(
                    _new_available_malformed_coroutine_cleanup_lease()
                )
        except BaseException:
            pass


def _new_available_malformed_coroutine_cleanup_lease(
) -> _MalformedCoroutineCleanupLease:
    lease = object.__new__(_MalformedCoroutineCleanupLease)
    object.__setattr__(lease, "_lock", threading.Lock())
    object.__setattr__(lease, "_state", "available")
    object.__setattr__(lease, "_candidate", None)
    return lease


_MALFORMED_COROUTINE_CLEANUP_POOL_LOCK = threading.RLock()
_MALFORMED_COROUTINE_CLEANUP_POOL: list[
    _MalformedCoroutineCleanupLease
] = []


def _return_malformed_coroutine_cleanup_lease(
    lease: _MalformedCoroutineCleanupLease,
) -> None:
    with _MALFORMED_COROUTINE_CLEANUP_POOL_LOCK:
        if not any(
            pooled_lease is lease
            for pooled_lease in _MALFORMED_COROUTINE_CLEANUP_POOL
        ):
            _MALFORMED_COROUTINE_CLEANUP_POOL.append(lease)


for _lease_index in range(_MALFORMED_COROUTINE_CLEANUP_CAPACITY):
    _available_lease = _new_available_malformed_coroutine_cleanup_lease()
    _return_malformed_coroutine_cleanup_lease(_available_lease)
del _available_lease, _lease_index


def _acquire_malformed_coroutine_cleanup_lease(
) -> _MalformedCoroutineCleanupLease | None:
    with _MALFORMED_COROUTINE_CLEANUP_POOL_LOCK:
        if not _MALFORMED_COROUTINE_CLEANUP_POOL:
            return None
        lease = _MALFORMED_COROUTINE_CLEANUP_POOL.pop()
    with lease._lock:
        if lease._state != "available":
            raise RuntimeError("invalid malformed coroutine cleanup lease")
        lease._state = "caller"
    return lease


@final
class GoogleCalendarBusySource:
    """Run the async verified Google boundary for synchronous schedulers."""

    __slots__ = (
        "_session_factory",
        "_configuration",
        "_credential_cipher",
    )

    def __init_subclass__(cls, **_kwargs: object) -> NoReturn:
        del cls, _kwargs
        _raise_subclass_error()

    def __init__(
        self,
        session_factory: Callable[[], _Session],
        *,
        configuration: GoogleCalendarTokenExchangeConfiguration,
        credential_cipher: CalendarCredentialCipher,
    ) -> None:
        # The first traced instruction must already be inside the handler.
        try: return _install_source(self, session_factory, configuration, credential_cipher)
        except BaseException as boundary_raised:
            try: self = session_factory = configuration = credential_cipher = None; control_exception, system_exit_code, _failure_reason = _safe_classified_failure(boundary_raised); boundary_raised = None
            except BaseException as cleanup_raised: self = session_factory = configuration = credential_cipher = boundary_raised = None; control_exception, system_exit_code, _failure_reason = _safe_classified_failure(cleanup_raised); cleanup_raised = None
        if control_exception is not None:
            _raise_control_exception(
                control_exception,
                system_exit_code=system_exit_code,
            )
        _raise_configuration_error()

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        del self, _name, _value
        _raise_immutable_error()

    def __delattr__(self, _name: str) -> NoReturn:
        del self, _name
        _raise_immutable_error()

    def __reduce__(self) -> NoReturn:
        del self
        _raise_serialization_error()

    def __reduce_ex__(self, _protocol: int) -> NoReturn:
        del self, _protocol
        _raise_serialization_error()

    def __getstate__(self) -> NoReturn:
        del self
        _raise_serialization_error()

    def __repr__(self) -> str:
        return "GoogleCalendarBusySource(<redacted>)"

    __str__ = __repr__

    def query_busy_periods(
        self,
        *,
        instructor_id: str,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> CalendarBusySnapshot:
        """Return one canonical verified snapshot without provider details."""

        # Capture every argument before clearing secret-bearing public locals.
        try: return _guarded_query_busy_periods(self, instructor_id=instructor_id, window_start=window_start, window_end=window_end, _cleared=(self := None, instructor_id := None, window_start := None, window_end := None))
        except BaseException as boundary_raised:
            try: self = instructor_id = window_start = window_end = None; control_exception, system_exit_code, failure_reason = _safe_classified_failure(boundary_raised); boundary_raised = None
            except BaseException as cleanup_raised: self = instructor_id = window_start = window_end = boundary_raised = None; control_exception, system_exit_code, failure_reason = _safe_classified_failure(cleanup_raised); cleanup_raised = None
        if control_exception not in (None, "cancelled"):
            _raise_control_exception(
                control_exception,
                system_exit_code=system_exit_code,
            )
        _raise_calendar_failure(failure_reason)


def _install_source(
    source: object,
    session_factory: object,
    configuration: object,
    credential_cipher: object,
) -> None:
    if (
        type(source) is not GoogleCalendarBusySource
        or not callable(session_factory)
        or type(configuration)
        is not GoogleCalendarTokenExchangeConfiguration
        or type(credential_cipher) is not CalendarCredentialCipher
    ):
        raise ValueError("Google Calendar busy source is not configured safely.")
    for slot_name in (
        "_session_factory",
        "_configuration",
        "_credential_cipher",
    ):
        try:
            object.__getattribute__(source, slot_name)
        except AttributeError:
            continue
        raise ValueError("Google Calendar busy source is already configured.")
    object.__setattr__(source, "_session_factory", session_factory)
    object.__setattr__(source, "_configuration", configuration)
    object.__setattr__(source, "_credential_cipher", credential_cipher)


def _guarded_query_busy_periods(
    source: object,
    *,
    instructor_id: object,
    window_start: object,
    window_end: object,
    _cleared: object,
) -> CalendarBusySnapshot:
    del _cleared
    try:
        try: return _query_busy_periods(source, instructor_id=instructor_id, window_start=window_start, window_end=window_end)
        except BaseException as failure:
            try: source = instructor_id = window_start = window_end = None; control_exception, system_exit_code, failure_reason = _safe_classified_failure(failure); failure = None
            except BaseException as cleanup_raised: source = instructor_id = window_start = window_end = failure = None; control_exception, system_exit_code, failure_reason = _safe_classified_failure(cleanup_raised); cleanup_raised = None
    except BaseException as boundary_raised:
        try: source = instructor_id = window_start = window_end = None; control_exception, system_exit_code, failure_reason = _safe_classified_failure(boundary_raised); boundary_raised = None
        except BaseException as cleanup_raised: source = instructor_id = window_start = window_end = boundary_raised = None; control_exception, system_exit_code, failure_reason = _safe_classified_failure(cleanup_raised); cleanup_raised = None
    if control_exception not in (None, "cancelled"):
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    _raise_calendar_failure(failure_reason)


def _query_busy_periods(
    source: object,
    *,
    instructor_id: object,
    window_start: object,
    window_end: object,
) -> CalendarBusySnapshot:
    if type(source) is not GoogleCalendarBusySource:
        raise ExternalCalendarUnavailable("provider_unavailable")
    session_factory = source._session_factory
    configuration = source._configuration
    credential_cipher = source._credential_cipher
    source = None
    result: object = None
    cleanup_lease: _MalformedCoroutineCleanupLease | None = None
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise ExternalCalendarUnavailable("provider_unavailable")

        cleanup_lease = _acquire_malformed_coroutine_cleanup_lease()
        if cleanup_lease is None:
            raise ExternalCalendarUnavailable("provider_unavailable")
        result = _run_service_in_owned_runner(
            session_factory,
            instructor_id=instructor_id,
            window_start=window_start,
            window_end=window_end,
            configuration=configuration,
            credential_cipher=credential_cipher,
        )
        snapshot = _validated_snapshot(
            result,
            window_start=window_start,
            window_end=window_end,
        )
        result = None
        return snapshot
    finally:
        try:
            if isinstance(result, Coroutine):
                if cleanup_lease is None or not cleanup_lease.quarantine(result):
                    raise ExternalCalendarUnavailable("provider_unavailable")
                _close_malformed_coroutine(cleanup_lease)
        finally:
            if cleanup_lease is not None:
                if isinstance(result, Coroutine):
                    cleanup_lease.quarantine(result)
                cleanup_lease.release_caller()
            session_factory = configuration = credential_cipher = None
            instructor_id = window_start = window_end = None
            result = cleanup_lease = None


def _run_service_in_owned_runner(
    session_factory: object,
    *,
    instructor_id: object,
    window_start: object,
    window_end: object,
    configuration: object,
    credential_cipher: object,
) -> object:
    driver: object = None
    handoff = [
        session_factory,
        instructor_id,
        window_start,
        window_end,
        configuration,
        credential_cipher,
    ]
    try:
        with asyncio.Runner() as runner:
            driver = _query_service_driver(handoff)
            return runner.run(driver)
    finally:
        try:
            handoff.clear()
        finally:
            try:
                if (
                    type(driver) is types.CoroutineType
                    and driver.cr_frame is not None
                ):
                    types.CoroutineType.close(driver)
            finally:
                driver = handoff = None
                session_factory = configuration = credential_cipher = None
                instructor_id = window_start = window_end = None


async def _query_service_driver(
    handoff: list[object],
) -> object:
    values: tuple[object, ...] = ()
    try:
        if type(handoff) is not list or len(handoff) != 6:
            raise ExternalCalendarUnavailable("provider_unavailable")
        values = tuple(handoff)
    finally:
        handoff.clear()
        handoff = None
    (
        session_factory,
        instructor_id,
        window_start,
        window_end,
        configuration,
        credential_cipher,
    ) = values
    values = None
    try:
        return await query_google_calendar_busy_snapshot(
            session_factory,
            instructor_id=instructor_id,
            window_start=window_start,
            window_end=window_end,
            configuration=configuration,
            credential_cipher=credential_cipher,
        )
    finally:
        session_factory = configuration = credential_cipher = None
        instructor_id = window_start = window_end = None


def _validated_snapshot(
    candidate: object,
    *,
    window_start: object,
    window_end: object,
) -> CalendarBusySnapshot:
    if type(candidate) is not CalendarBusySnapshot:
        raise ExternalCalendarInvalidResponse()
    status = candidate.status
    periods = candidate.periods
    candidate = None
    if (
        type(status) is not str
        or type(periods) is not tuple
        or type(window_start) is not datetime.datetime
        or type(window_end) is not datetime.datetime
    ):
        raise ExternalCalendarInvalidResponse()
    if status == "disconnected":
        if periods:
            raise ExternalCalendarInvalidResponse()
    elif status != "verified" or any(
        type(interval) is not CalendarBusyInterval
        or type(interval.start) is not datetime.datetime
        or type(interval.end) is not datetime.datetime
        for interval in periods
    ):
        raise ExternalCalendarInvalidResponse()
    try:
        normalized = normalize_busy_intervals(
            periods,
            window_start=window_start,
            window_end=window_end,
        )
    except BaseException as raised:
        control_exception, system_exit_code, _failure_reason = (
            _safe_classified_failure(raised)
        )
        raised = periods = window_start = window_end = None
        if control_exception not in (None, "cancelled"):
            _raise_control_exception(
                control_exception,
                system_exit_code=system_exit_code,
            )
        if control_exception == "cancelled":
            raise ExternalCalendarUnavailable("provider_unavailable") from None
        raise ExternalCalendarInvalidResponse() from None
    if any(
        type(interval) is not CalendarBusyInterval
        or type(interval.start) is not datetime.datetime
        or type(interval.end) is not datetime.datetime
        for interval in normalized
    ):
        raise ExternalCalendarInvalidResponse()
    if status == "disconnected":
        return CalendarBusySnapshot(status="disconnected", periods=())
    return CalendarBusySnapshot(status="verified", periods=normalized)


def _close_malformed_coroutine(
    lease: _MalformedCoroutineCleanupLease,
) -> None:
    outcome: list[tuple[bool, bool, str | None, int | None] | None] = [None]
    worker = threading.Thread(
        target=_close_malformed_coroutine_worker,
        args=(lease, outcome),
        daemon=True,
        name="calendar-malformed-coroutine-cleanup",
    )
    worker.start()
    lease = None
    worker.join(_MALFORMED_COROUTINE_CLOSE_SECONDS)
    if worker.is_alive() or outcome[0] is None:
        worker = outcome = None
        _raise_calendar_failure("provider_unavailable")
    closed, failed, control_exception, system_exit_code = outcome[0]
    worker = outcome = None
    if control_exception not in (None, "cancelled"):
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    if not closed:
        _raise_calendar_failure("provider_unavailable")
    if failed:
        _raise_calendar_failure("provider_unavailable")


def _close_malformed_coroutine_worker(
    lease: _MalformedCoroutineCleanupLease,
    outcome: list[tuple[bool, bool, str | None, int | None] | None],
) -> None:
    try: return _guarded_close_malformed_coroutine_worker(lease, outcome)
    except BaseException as boundary_raised:
        try: lease = None; control_exception, system_exit_code, _failure_reason = _safe_classified_failure(boundary_raised); boundary_raised = None
        except BaseException as cleanup_raised: lease = boundary_raised = None; control_exception, system_exit_code, _failure_reason = _safe_classified_failure(cleanup_raised); cleanup_raised = None
    try: return _publish_malformed_coroutine_cleanup_outcome(outcome, closed=False, failed=True, control_exception=control_exception, system_exit_code=system_exit_code)
    except BaseException: return


def _guarded_close_malformed_coroutine_worker(
    lease: _MalformedCoroutineCleanupLease,
    outcome: list[tuple[bool, bool, str | None, int | None] | None],
) -> None:
    candidate = lease.claim_worker()
    failed = False
    closed = False
    control_exception = None
    system_exit_code = None
    try:
        for _attempt in range(3):
            try:
                _close_coroutine_once(candidate)
                closed = True
                break
            except BaseException as raised:
                (
                    current_control,
                    current_system_exit_code,
                    _failure_reason,
                ) = _safe_classified_failure(raised)
                raised = None
                failed = True
                if (
                    current_control not in (None, "cancelled")
                    and control_exception in (None, "cancelled")
                ):
                    control_exception = current_control
                    system_exit_code = current_system_exit_code
    except BaseException as raised:
        (
            control_exception,
            system_exit_code,
            _failure_reason,
        ) = _safe_classified_failure(raised)
        raised = None
        closed = False
        failed = True
    finally:
        candidate = None
        released = not closed
        if closed:
            released = False
            for _release_attempt in range(3):
                try:
                    released = lease.release_worker()
                    if released:
                        break
                except BaseException as raised:
                    (
                        current_control,
                        current_system_exit_code,
                        _failure_reason,
                    ) = _safe_classified_failure(raised)
                    raised = None
                    failed = True
                    if (
                        current_control not in (None, "cancelled")
                        and control_exception in (None, "cancelled")
                    ):
                        control_exception = current_control
                        system_exit_code = current_system_exit_code
            if not released:
                closed = False
        lease = None
        _publish_malformed_coroutine_cleanup_outcome(
            outcome,
            closed=closed,
            failed=failed,
            control_exception=control_exception,
            system_exit_code=system_exit_code,
        )


def _publish_malformed_coroutine_cleanup_outcome(
    outcome: list[tuple[bool, bool, str | None, int | None] | None],
    *,
    closed: bool,
    failed: bool,
    control_exception: str | None,
    system_exit_code: int | None,
) -> None:
    try:
        for _publish_attempt in range(3):
            try:
                outcome[0] = (closed, failed, control_exception, system_exit_code); return
            except BaseException as raised:
                (
                    current_control,
                    current_system_exit_code,
                    _failure_reason,
                ) = _safe_classified_failure(raised)
                raised = None
                failed = True
                if (
                    current_control not in (None, "cancelled")
                    and control_exception in (None, "cancelled")
                ):
                    control_exception = current_control
                    system_exit_code = current_system_exit_code
    except BaseException:
        return


def _close_coroutine_once(candidate: object) -> None:
    if type(candidate) is types.CoroutineType:
        types.CoroutineType.close(candidate)
    elif isinstance(candidate, Coroutine):
        candidate.close()


def _safe_classified_failure(
    raised: BaseException,
) -> tuple[str | None, int | None, CalendarBusyFailureReason]:
    try:
        return _classified_failure(raised)
    except BaseException as classifier_raised:
        control_exception, system_exit_code = _bounded_control_exception(
            raised,
            classifier_raised,
        )
        classifier_raised = None
    return (
        control_exception or "cancelled",
        system_exit_code,
        "provider_unavailable",
    )


def _classified_failure(
    raised: BaseException,
) -> tuple[str | None, int | None, CalendarBusyFailureReason]:
    nodes, overflowed = _bounded_exception_graph(raised)
    for current in nodes:
        control_exception, system_exit_code = _direct_control_exception(
            current
        )
        if control_exception not in (None, "cancelled"):
            return (
                control_exception,
                system_exit_code,
                "provider_unavailable",
            )
    if overflowed or any(
        isinstance(current, asyncio.CancelledError) for current in nodes
    ):
        return "cancelled", None, "provider_unavailable"
    if type(raised) is ExternalCalendarAuthorizationRequired:
        return None, None, "authorization_required"
    if type(raised) is ExternalCalendarTimeout:
        return None, None, "timeout"
    if type(raised) is ExternalCalendarInvalidResponse:
        return None, None, "invalid_response"
    if type(raised) is ExternalCalendarUnavailable:
        reason = raised.reason_code
        if type(reason) is str and reason in _FAILURE_REASONS:
            return None, None, reason
    return None, None, "provider_unavailable"


def _direct_control_exception(
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


def _bounded_control_exception(
    *failures: BaseException,
) -> tuple[str | None, int | None]:
    cancellation_found = False
    for failure in failures:
        control_exception, system_exit_code = _direct_control_exception(
            failure
        )
        if control_exception not in (None, "cancelled"):
            return control_exception, system_exit_code
        cancellation_found = cancellation_found or (
            control_exception == "cancelled"
        )

    nodes: list[BaseException] = []
    traversal_raised: BaseException | None = None
    overflowed = False
    try:
        pending = list(reversed(failures))
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            identity = id(current)
            if identity in seen:
                continue
            if len(seen) >= _MAX_CONTROL_EXCEPTION_NODES:
                overflowed = True
                break
            seen.add(identity)
            nodes.append(current)
            if isinstance(current, BaseExceptionGroup):
                nested = BaseExceptionGroup.exceptions.__get__(
                    current,
                    BaseExceptionGroup,
                )
                pending.extend(reversed(nested))
    except BaseException as failure:
        traversal_raised = failure
        overflowed = True

    for current in nodes:
        control_exception, system_exit_code = _direct_control_exception(
            current
        )
        if control_exception not in (None, "cancelled"):
            return control_exception, system_exit_code
        cancellation_found = cancellation_found or (
            control_exception == "cancelled"
        )
    if traversal_raised is not None:
        control_exception, system_exit_code = _direct_control_exception(
            traversal_raised
        )
        traversal_raised = None
        if control_exception not in (None, "cancelled"):
            return control_exception, system_exit_code
        cancellation_found = cancellation_found or (
            control_exception == "cancelled"
        )
    if overflowed or cancellation_found:
        return "cancelled", None
    return None, None


def _bounded_exception_graph(
    raised: BaseException,
) -> tuple[tuple[BaseException, ...], bool]:
    pending = [raised]
    seen: set[int] = set()
    nodes: list[BaseException] = []
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        if len(seen) >= _MAX_CONTROL_EXCEPTION_NODES:
            return tuple(nodes), True
        seen.add(identity)
        nodes.append(current)
        if isinstance(current, BaseExceptionGroup):
            nested = BaseExceptionGroup.exceptions.__get__(
                current,
                BaseExceptionGroup,
            )
            pending.extend(reversed(nested))
    return tuple(nodes), False


def _raise_calendar_failure(reason: CalendarBusyFailureReason) -> NoReturn:
    if reason == "authorization_required":
        error: BaseException = ExternalCalendarAuthorizationRequired()
    elif reason == "timeout":
        error = ExternalCalendarTimeout()
    elif reason == "invalid_response":
        error = ExternalCalendarInvalidResponse()
    else:
        error = ExternalCalendarUnavailable("provider_unavailable")
    try:
        raise error from None
    except BaseException as raised:
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
        _raise_calendar_failure("provider_unavailable")
    try:
        raise error from None
    except BaseException as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_configuration_error() -> NoReturn:
    error = ValueError("Google Calendar busy source is not configured safely.")
    try:
        raise error from None
    except ValueError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_immutable_error() -> NoReturn:
    error = AttributeError("GoogleCalendarBusySource is immutable.")
    try:
        raise error from None
    except AttributeError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_serialization_error() -> NoReturn:
    error = TypeError("GoogleCalendarBusySource is not serializable.")
    try:
        raise error from None
    except TypeError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_subclass_error() -> NoReturn:
    error = TypeError("GoogleCalendarBusySource cannot be subclassed.")
    try:
        raise error from None
    except TypeError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise
