"""Hard overall deadline for one concrete Google Calendar FreeBusy query."""

from __future__ import annotations

import asyncio
import datetime
from typing import NoReturn, final

import anyio

from app.external_calendar import (
    CalendarBusyFailureReason,
    ExternalCalendarInvalidResponse,
    ExternalCalendarTimeout,
    ExternalCalendarUnavailable,
)
from app.google_calendar_credential_handoff import (
    GoogleCalendarRefreshCredentialClaim,
)
from app.google_calendar_freebusy import (
    GOOGLE_CALENDAR_FREEBUSY_ENDPOINT,
    GoogleCalendarFreeBusyQueryResult,
    _bounded_unique_exception_graph,
    _classified_boundary_failure,
    _complete_google_calendar_freebusy_query,
    _prepare_google_calendar_freebusy_query,
    _raise_control_exception,
    _raise_freebusy_failure,
)
from app.google_calendar_freebusy_httpx import _post_json_async
from app.google_calendar_token_exchange import (
    GoogleCalendarRefreshTokenExchangeResult,
)


__all__ = ("query_google_calendar_freebusy_with_deadline",)


_FREEBUSY_DEADLINE_SECONDS = 5.0
_FREEBUSY_TIMEOUT_SECONDS = 5.0
_UNSET = object()


@final
class _OwnedTransportOutcome:
    """Redacted child-task result consumed only by the parent boundary."""

    __slots__ = (
        "value",
        "control_exception",
        "control_is_cancellation_only",
        "system_exit_code",
        "failure_reason",
    )

    __init__ = None

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("owned transport outcome is immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("owned transport outcome is immutable")

    def __reduce__(self) -> NoReturn:
        raise TypeError("owned transport outcome is not serializable")

    def __reduce_ex__(self, _protocol: int) -> NoReturn:
        del _protocol
        raise TypeError("owned transport outcome is not serializable")

    def __getstate__(self) -> NoReturn:
        raise TypeError("owned transport outcome is not serializable")

    def __repr__(self) -> str:
        return "_OwnedTransportOutcome(<redacted>)"

    __str__ = __repr__


def _new_owned_transport_outcome(
    *,
    value: object = _UNSET,
    control_exception: str | None = None,
    control_is_cancellation_only: bool = False,
    system_exit_code: int | None = None,
    failure_reason: CalendarBusyFailureReason | None = None,
) -> _OwnedTransportOutcome:
    outcome = object.__new__(_OwnedTransportOutcome)
    object.__setattr__(outcome, "value", value)
    object.__setattr__(outcome, "control_exception", control_exception)
    object.__setattr__(
        outcome,
        "control_is_cancellation_only",
        control_is_cancellation_only,
    )
    object.__setattr__(outcome, "system_exit_code", system_exit_code)
    object.__setattr__(outcome, "failure_reason", failure_reason)
    return outcome


async def query_google_calendar_freebusy_with_deadline(
    *,
    claim: GoogleCalendarRefreshCredentialClaim,
    access_result: GoogleCalendarRefreshTokenExchangeResult,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
) -> GoogleCalendarFreeBusyQueryResult:
    """Query the fixed primary calendar within one hard overall deadline."""

    # The first traced instruction must already be inside the handler.
    # Clear public-frame references only after the guarded helper captures them.
    try: return await _guarded_google_calendar_freebusy_with_deadline(claim=claim, access_result=access_result, window_start=window_start, window_end=window_end, _cleared=(claim := None, access_result := None, window_start := None, window_end := None))
    except BaseException as boundary_raised:
        try: claim = access_result = window_start = window_end = None; control_exception, system_exit_code, failure_reason = _classified_deadline_failure(boundary_raised); boundary_raised = None
        except BaseException as cleanup_raised: claim = access_result = window_start = window_end = boundary_raised = None; control_exception, system_exit_code, failure_reason = _classified_deadline_failure(cleanup_raised)
    if control_exception == "cancelled":
        _raise_control_exception("cancelled", system_exit_code=None)
    if control_exception is None and _current_task_is_cancelling():
        _raise_control_exception("cancelled", system_exit_code=None)
    if control_exception is not None:
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    _raise_freebusy_failure(failure_reason)


async def _guarded_google_calendar_freebusy_with_deadline(
    *,
    claim: object,
    access_result: object,
    window_start: object,
    window_end: object,
    _cleared: object,
) -> GoogleCalendarFreeBusyQueryResult:
    del _cleared
    try:
        try: return await _query_google_calendar_freebusy_with_deadline(claim=claim, access_result=access_result, window_start=window_start, window_end=window_end)
        except BaseException as failure:
            try: claim = access_result = window_start = window_end = None; control_exception, system_exit_code, failure_reason = _classified_deadline_failure(failure); failure = None
            except BaseException as cleanup_raised: claim = access_result = window_start = window_end = failure = None; control_exception, system_exit_code, failure_reason = _classified_deadline_failure(cleanup_raised)
    except BaseException as boundary_raised:
        try: claim = access_result = window_start = window_end = None; control_exception, system_exit_code, failure_reason = _classified_deadline_failure(boundary_raised); boundary_raised = None
        except BaseException as cleanup_raised: claim = access_result = window_start = window_end = boundary_raised = None; control_exception, system_exit_code, failure_reason = _classified_deadline_failure(cleanup_raised)
    if control_exception == "cancelled":
        _raise_control_exception("cancelled", system_exit_code=None)
    if control_exception is None and _current_task_is_cancelling():
        _raise_control_exception("cancelled", system_exit_code=None)
    if control_exception is not None:
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    _raise_freebusy_failure(failure_reason)


async def _query_google_calendar_freebusy_with_deadline(
    *,
    claim: object,
    access_result: object,
    window_start: object,
    window_end: object,
) -> GoogleCalendarFreeBusyQueryResult:
    try:
        prepared = _prepare_google_calendar_freebusy_query(
            claim=claim,
            access_result=access_result,
            window_start=window_start,
            window_end=window_end,
        )
    except BaseException as raised:
        _raise_preparation_failure(raised)

    deadline = _monotonic_time() + _FREEBUSY_DEADLINE_SECONDS
    timeout_scope = _new_deadline_scope(deadline=deadline)
    result: object = _UNSET
    failure: BaseException | None = None
    with timeout_scope:
        try:
            try:
                response = await _post_json_in_owned_task(
                    access_token=(
                        prepared.expected_access_result.access_token
                    ),
                    body=prepared.request_body,
                    deadline=deadline,
                )
            except BaseException as raised:
                _raise_transport_failure(raised)

            if not timeout_scope.cancel_called and _current_task_is_cancelling():
                raise asyncio.CancelledError
            try:
                result = _complete_google_calendar_freebusy_query(
                    response,
                    prepared=prepared,
                )
            except BaseException as raised:
                _raise_completion_failure(raised)
        except asyncio.CancelledError:
            raise
        except BaseException as raised:
            failure = raised

    task_is_cancelling = _current_task_is_cancelling()
    if failure is not None:
        control_exception, _system_exit_code, _reason = (
            _classified_deadline_failure(failure)
        )
        if timeout_scope.cancel_called and not task_is_cancelling:
            non_cancel_control, system_exit_code = (
                _classified_non_cancel_control_exception(failure)
            )
            if non_cancel_control is not None:
                _raise_control_exception(
                    non_cancel_control,
                    system_exit_code=system_exit_code,
                )
            if control_exception not in (None, "cancelled"):
                raise failure
        elif control_exception is not None:
            raise failure
    if task_is_cancelling:
        raise asyncio.CancelledError
    if timeout_scope.cancel_called or _monotonic_time() >= deadline:
        raise ExternalCalendarTimeout() from None
    if failure is not None:
        raise failure
    if type(result) is not GoogleCalendarFreeBusyQueryResult:
        raise ExternalCalendarInvalidResponse() from None
    return result


async def _post_json_in_owned_task(
    *,
    access_token: str,
    body: bytes,
    deadline: float,
) -> object:
    start_gate = asyncio.Event()
    task = None
    try:
        task = asyncio.create_task(
            _gated_post_json(
                start_gate=start_gate,
                access_token=access_token,
                body=body,
                deadline=deadline,
            )
        )
        start_gate.set()
        outcome = await _await_owned_transport_task(task)
        if type(outcome) is not _OwnedTransportOutcome:
            raise ExternalCalendarInvalidResponse() from None
        if outcome.control_exception is not None:
            _raise_control_exception(
                outcome.control_exception,
                system_exit_code=outcome.system_exit_code,
            )
        if outcome.failure_reason is not None:
            _raise_freebusy_failure(outcome.failure_reason)
        return outcome.value
    except BaseException:
        if task is not None:
            cleanup_result = await _cancel_owned_transport_task(
                task,
                deadline=deadline,
            )
            if type(cleanup_result) is _OwnedTransportOutcome:
                if (
                    cleanup_result.control_exception is not None
                    and not cleanup_result.control_is_cancellation_only
                ):
                    _raise_control_exception(
                        cleanup_result.control_exception,
                        system_exit_code=(
                            cleanup_result.system_exit_code
                        ),
                    )
            elif isinstance(cleanup_result, BaseException):
                control_exception, _system_exit_code = (
                    _classified_non_cancel_control_exception(
                        cleanup_result
                    )
                )
                if control_exception is not None:
                    _raise_control_exception(
                        control_exception,
                        system_exit_code=_system_exit_code,
                    )
        raise


async def _gated_post_json(
    *,
    start_gate: asyncio.Event,
    access_token: str,
    body: bytes,
    deadline: float,
) -> _OwnedTransportOutcome:
    try:
        return await _gated_post_json_inner(
            start_gate=start_gate,
            access_token=access_token,
            body=body,
            deadline=deadline,
        )
    except BaseException as boundary_raised:
        try:
            outcome = _owned_transport_failure_outcome(boundary_raised)
            boundary_raised = None
        except BaseException as secondary_raised:
            outcome = _owned_transport_combined_emergency_outcome(
                boundary_raised,
                secondary_raised,
            )
            boundary_raised = secondary_raised = None
        return outcome


async def _gated_post_json_inner(
    *,
    start_gate: asyncio.Event,
    access_token: str,
    body: bytes,
    deadline: float,
) -> _OwnedTransportOutcome:
    try:
        response = await _run_gated_post_json(
            start_gate=start_gate,
            access_token=access_token,
            body=body,
            deadline=deadline,
        )
    except BaseException as raised:
        try:
            outcome = _owned_transport_failure_outcome(raised)
            raised = None
        except BaseException as secondary_raised:
            outcome = _owned_transport_combined_emergency_outcome(
                raised,
                secondary_raised,
            )
            raised = secondary_raised = None
        return outcome
    return _new_owned_transport_outcome(value=response)


async def _run_gated_post_json(
    *,
    start_gate: asyncio.Event,
    access_token: str,
    body: bytes,
    deadline: float,
) -> object:
    async with asyncio.timeout_at(deadline):
        await start_gate.wait()
    return await _post_json_async(
        url=GOOGLE_CALENDAR_FREEBUSY_ENDPOINT,
        access_token=access_token,
        body=body,
        timeout_seconds=_FREEBUSY_TIMEOUT_SECONDS,
        deadline=deadline,
    )


async def _await_owned_transport_task(
    task: asyncio.Task[_OwnedTransportOutcome],
) -> _OwnedTransportOutcome:
    return await asyncio.shield(task)


async def _cancel_owned_transport_task(
    task: asyncio.Task[_OwnedTransportOutcome],
    *,
    deadline: float,
) -> _OwnedTransportOutcome | BaseException | None:
    try:
        task.cancel()
    except BaseException as raised:
        control_exception, _system_exit_code = (
            _classified_non_cancel_control_exception(raised)
        )
        if control_exception is not None:
            raise
    remaining = max(0.0, deadline - _monotonic_time())
    if remaining > 0.0:
        try:
            with anyio.CancelScope(shield=True):
                await asyncio.wait({task}, timeout=remaining)
        except BaseException as raised:
            control_exception, _system_exit_code = (
                _classified_non_cancel_control_exception(raised)
            )
            if control_exception is not None:
                raise
    if task.done():
        result = _owned_task_result(task)
        if _monotonic_time() >= deadline:
            return None
        if type(result) is _OwnedTransportOutcome or isinstance(
            result,
            BaseException,
        ):
            return result
        return None
    try:
        task.cancel()
    except BaseException as raised:
        control_exception, _system_exit_code = (
            _classified_non_cancel_control_exception(raised)
        )
        if control_exception is not None:
            raise
    task.add_done_callback(_consume_owned_task_result)
    return None


def _owned_task_result(
    task: asyncio.Future[object],
) -> object:
    try:
        failure = task.exception()
    except asyncio.CancelledError:
        return None
    except BaseException as raised:
        return raised
    if failure is not None:
        return failure
    try:
        return task.result()
    except BaseException as raised:
        return raised


def _consume_owned_task_result(task: asyncio.Future[object]) -> None:
    _owned_task_result(task)


def _owned_transport_failure_outcome(
    raised: BaseException,
) -> _OwnedTransportOutcome:
    non_cancel_control, non_cancel_system_exit_code = (
        _classified_non_cancel_control_exception(raised)
    )
    if non_cancel_control is not None:
        return _new_owned_transport_outcome(
            control_exception=non_cancel_control,
            system_exit_code=non_cancel_system_exit_code,
        )
    control_exception, system_exit_code, _failure_reason = (
        _classified_deadline_failure(raised)
    )
    if control_exception is not None:
        return _new_owned_transport_outcome(
            control_exception=control_exception,
            control_is_cancellation_only=(
                control_exception == "cancelled"
            ),
            system_exit_code=system_exit_code,
        )
    if isinstance(raised, TimeoutError):
        failure_reason: CalendarBusyFailureReason = "timeout"
    elif isinstance(raised, ValueError):
        failure_reason = "invalid_response"
    else:
        failure_reason = "provider_unavailable"
    return _new_owned_transport_outcome(failure_reason=failure_reason)


def _owned_transport_emergency_outcome(
    raised: BaseException,
) -> _OwnedTransportOutcome:
    try:
        nodes, overflowed = _bounded_unique_exception_graph(raised)
    except BaseException as secondary_raised:
        original_outcome = _owned_transport_direct_emergency_outcome(
            raised
        )
        secondary_outcome = _owned_transport_direct_emergency_outcome(
            secondary_raised
        )
        for outcome in (original_outcome, secondary_outcome):
            if outcome.control_exception not in (None, "cancelled"):
                return outcome
        return _new_owned_transport_outcome(
            control_exception="cancelled"
        )
    for current in nodes:
        if isinstance(current, KeyboardInterrupt):
            return _new_owned_transport_outcome(
                control_exception="keyboard_interrupt"
            )
        if isinstance(current, SystemExit):
            code = current.code if type(current) is SystemExit else 1
            return _new_owned_transport_outcome(
                control_exception="system_exit",
                system_exit_code=(
                    code if type(code) is int or code is None else 1
                ),
            )
        if isinstance(current, GeneratorExit):
            return _new_owned_transport_outcome(
                control_exception="generator_exit"
            )
    if overflowed:
        return _new_owned_transport_outcome(
            control_exception="cancelled",
        )
    if any(isinstance(current, asyncio.CancelledError) for current in nodes):
        return _new_owned_transport_outcome(
            control_exception="cancelled",
            control_is_cancellation_only=True,
        )
    return _owned_transport_direct_emergency_outcome(raised)


def _owned_transport_combined_emergency_outcome(
    original: BaseException,
    secondary: BaseException,
) -> _OwnedTransportOutcome:
    original_outcome = _owned_transport_emergency_outcome(original)
    secondary_outcome = _owned_transport_emergency_outcome(secondary)
    for outcome in (original_outcome, secondary_outcome):
        if outcome.control_exception not in (None, "cancelled"):
            return outcome
    for outcome in (original_outcome, secondary_outcome):
        if outcome.control_exception is not None:
            return outcome
    return original_outcome


def _owned_transport_direct_emergency_outcome(
    raised: BaseException,
) -> _OwnedTransportOutcome:
    if isinstance(raised, asyncio.CancelledError):
        return _new_owned_transport_outcome(
            control_exception="cancelled",
            control_is_cancellation_only=True,
        )
    if isinstance(raised, KeyboardInterrupt):
        return _new_owned_transport_outcome(
            control_exception="keyboard_interrupt"
        )
    if isinstance(raised, SystemExit):
        code = raised.code if type(raised) is SystemExit else 1
        return _new_owned_transport_outcome(
            control_exception="system_exit",
            system_exit_code=(
                code if type(code) is int or code is None else 1
            ),
        )
    if isinstance(raised, GeneratorExit):
        return _new_owned_transport_outcome(
            control_exception="generator_exit"
        )
    if isinstance(raised, TimeoutError):
        failure_reason: CalendarBusyFailureReason = "timeout"
    elif isinstance(raised, ValueError):
        failure_reason = "invalid_response"
    else:
        failure_reason = "provider_unavailable"
    return _new_owned_transport_outcome(failure_reason=failure_reason)


def _new_deadline_scope(*, deadline: float) -> anyio.CancelScope:
    return anyio.CancelScope(deadline=deadline)


def _monotonic_time() -> float:
    return asyncio.get_running_loop().time()


def _current_task_is_cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _classified_deadline_failure(
    raised: BaseException,
) -> tuple[str | None, int | None, CalendarBusyFailureReason]:
    control_exception, system_exit_code, failure_reason = (
        _classified_boundary_failure(raised)
    )
    if control_exception is not None:
        return control_exception, system_exit_code, failure_reason
    if isinstance(raised, TimeoutError):
        return None, None, "timeout"
    return None, None, failure_reason


def _classified_non_cancel_control_exception(
    raised: BaseException,
) -> tuple[str | None, int | None]:
    nodes, overflowed = _bounded_unique_exception_graph(raised)
    for current in nodes:
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
    if overflowed:
        return "cancelled", None
    return None, None


def _raise_preparation_failure(raised: BaseException) -> NoReturn:
    control_exception, _system_exit_code, _failure_reason = (
        _classified_boundary_failure(raised)
    )
    if control_exception is not None:
        raise raised
    if isinstance(raised, Exception):
        raise ExternalCalendarInvalidResponse() from None
    raise raised


def _raise_transport_failure(raised: BaseException) -> NoReturn:
    control_exception, _system_exit_code, _failure_reason = (
        _classified_boundary_failure(raised)
    )
    if control_exception is not None:
        raise raised
    if isinstance(raised, ExternalCalendarUnavailable):
        raise raised
    if isinstance(raised, TimeoutError):
        raise ExternalCalendarTimeout() from None
    if isinstance(raised, ValueError):
        raise ExternalCalendarInvalidResponse() from None
    if isinstance(raised, Exception):
        raise ExternalCalendarUnavailable("provider_unavailable") from None
    raise raised


def _raise_completion_failure(raised: BaseException) -> NoReturn:
    control_exception, _system_exit_code, _failure_reason = (
        _classified_boundary_failure(raised)
    )
    if control_exception is not None:
        raise raised
    if isinstance(raised, ExternalCalendarUnavailable):
        raise raised
    if isinstance(raised, Exception):
        raise ExternalCalendarInvalidResponse() from None
    raise raised
