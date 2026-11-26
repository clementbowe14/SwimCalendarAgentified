"""Route-inert orchestration for one verified Google busy snapshot."""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import Callable, Coroutine
from functools import partial
from typing import NoReturn, TypeVar, final

import anyio
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
    MAX_BUSY_INTERVALS,
    MAX_BUSY_QUERY_WINDOW,
    normalize_busy_intervals,
)
from app.google_calendar_credential_claim import (
    claim_google_calendar_refresh_credential,
    revalidate_google_calendar_refresh_credential_claim,
)
from app.google_calendar_credential_handoff import (
    GoogleCalendarRefreshCredentialClaim,
    GoogleCalendarValidatedRefreshCredentialClaim,
    _validated_instructor_id,
    validate_google_calendar_refresh_credential_claim,
)
from app.google_calendar_freebusy import (
    GoogleCalendarFreeBusyQueryResult,
    _validated_access_result,
)
from app.google_calendar_freebusy_deadline import (
    query_google_calendar_freebusy_with_deadline,
)
from app.google_calendar_refresh_recovery import (
    mark_google_calendar_refresh_credential_authorization_required,
)
from app.google_calendar_token_deadline import (
    exchange_google_calendar_refresh_token_with_deadline,
)
from app.google_calendar_token_exchange import (
    GoogleCalendarRefreshAuthorizationRequired,
    GoogleCalendarRefreshTokenExchangeResult,
    GoogleCalendarTokenExchangeConfiguration,
)


__all__ = ("query_google_calendar_busy_snapshot",)


_Result = TypeVar("_Result")
_UTC = datetime.timezone.utc
_MAX_IDENTIFIER_BYTES = 255
_CONNECTION_ID_PREFIX = "calconn_"
_MAX_CONTROL_EXCEPTION_NODES = 4_096
_UNSET = object()
_BASE_TASK = asyncio.Task
_TASK_CANCEL = asyncio.Task.cancel
_TASK_CANCELLING = asyncio.Task.cancelling
_FAILURE_REASONS = frozenset(
    {
        "authorization_required",
        "timeout",
        "provider_unavailable",
        "invalid_response",
    }
)


@final
class _OwnedWorkerOutcome:
    """Redacted result returned by an owned database worker task."""

    __slots__ = (
        "value",
        "failed",
        "control_exception",
        "system_exit_code",
    )
    __init__ = None

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("owned worker outcome is immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("owned worker outcome is immutable")

    def __reduce__(self) -> NoReturn:
        raise TypeError("owned worker outcome is not serializable")

    def __reduce_ex__(self, _protocol: int) -> NoReturn:
        del _protocol
        raise TypeError("owned worker outcome is not serializable")

    def __getstate__(self) -> NoReturn:
        raise TypeError("owned worker outcome is not serializable")

    def __repr__(self) -> str:
        return "_OwnedWorkerOutcome(<redacted>)"

    __str__ = __repr__


@final
class _JoinedWorkerTask(asyncio.Task[_OwnedWorkerOutcome]):
    """A structurally owned worker that cannot be abandoned after commit."""

    def cancel(self, msg: object = None) -> bool:
        del msg
        return False


@final
class _OwnedTaskGroup(asyncio.TaskGroup):
    """Keep joined database workers alive while cancelling their guardian."""

    def __init__(self) -> None:
        super().__init__()
        self._owned_worker: _JoinedWorkerTask | None = None
        self._guardian: asyncio.Task[_OwnedWorkerOutcome] | None = None

    def _abort(self) -> None:
        self._aborting = True
        guardian = self._guardian
        if guardian is not None and not guardian.done():
            _TASK_CANCEL(guardian)


def _new_owned_worker_outcome(
    *,
    value: object = _UNSET,
    failed: bool = False,
    control_exception: str | None = None,
    system_exit_code: int | None = None,
) -> _OwnedWorkerOutcome:
    outcome = object.__new__(_OwnedWorkerOutcome)
    object.__setattr__(outcome, "value", value)
    object.__setattr__(outcome, "failed", failed)
    object.__setattr__(outcome, "control_exception", control_exception)
    object.__setattr__(outcome, "system_exit_code", system_exit_code)
    return outcome


async def query_google_calendar_busy_snapshot(
    session_factory: Callable[[], _Session],
    *,
    instructor_id: str,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
    configuration: GoogleCalendarTokenExchangeConfiguration,
    credential_cipher: CalendarCredentialCipher,
) -> CalendarBusySnapshot:
    """Return only a disconnected state or freshly verified busy periods."""

    # Capture every argument before clearing secret-bearing public-frame locals.
    try: return await _guarded_query_google_calendar_busy_snapshot(session_factory, instructor_id=instructor_id, window_start=window_start, window_end=window_end, configuration=configuration, credential_cipher=credential_cipher, _cleared=(session_factory := None, instructor_id := None, window_start := None, window_end := None, configuration := None, credential_cipher := None))
    except asyncio.CancelledError as direct_cancellation:
        session_factory = instructor_id = window_start = window_end = configuration = credential_cipher = None
        _raise_boundary_cancellation(direct_cancellation)
    except BaseException as boundary_raised:
        try: session_factory = instructor_id = window_start = window_end = configuration = credential_cipher = None; control_exception, system_exit_code, failure_reason = _safe_classified_service_failure(boundary_raised); boundary_raised = None
        except BaseException as cleanup_raised: session_factory = instructor_id = window_start = window_end = configuration = credential_cipher = boundary_raised = None; control_exception, system_exit_code, failure_reason = _safe_classified_service_failure(cleanup_raised); cleanup_raised = None
    if control_exception is not None:
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    _raise_service_failure(failure_reason)


async def _guarded_query_google_calendar_busy_snapshot(
    session_factory: object,
    *,
    instructor_id: object,
    window_start: object,
    window_end: object,
    configuration: object,
    credential_cipher: object,
    _cleared: object,
) -> CalendarBusySnapshot:
    del _cleared
    try:
        try: return await _query_google_calendar_busy_snapshot(session_factory, instructor_id=instructor_id, window_start=window_start, window_end=window_end, configuration=configuration, credential_cipher=credential_cipher)
        except asyncio.CancelledError as direct_cancellation:
            session_factory = instructor_id = window_start = window_end = configuration = credential_cipher = None
            _raise_boundary_cancellation(direct_cancellation)
        except BaseException as failure:
            try: session_factory = instructor_id = window_start = window_end = configuration = credential_cipher = None; control_exception, system_exit_code, failure_reason = _safe_classified_service_failure(failure); failure = None
            except BaseException as cleanup_raised: session_factory = instructor_id = window_start = window_end = configuration = credential_cipher = failure = None; control_exception, system_exit_code, failure_reason = _safe_classified_service_failure(cleanup_raised); cleanup_raised = None
    except asyncio.CancelledError as direct_cancellation:
        session_factory = instructor_id = window_start = window_end = configuration = credential_cipher = None
        _raise_boundary_cancellation(direct_cancellation)
    except BaseException as boundary_raised:
        try: session_factory = instructor_id = window_start = window_end = configuration = credential_cipher = None; control_exception, system_exit_code, failure_reason = _safe_classified_service_failure(boundary_raised); boundary_raised = None
        except BaseException as cleanup_raised: session_factory = instructor_id = window_start = window_end = configuration = credential_cipher = boundary_raised = None; control_exception, system_exit_code, failure_reason = _safe_classified_service_failure(cleanup_raised); cleanup_raised = None
    if control_exception is not None:
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    _raise_service_failure(failure_reason)


async def _query_google_calendar_busy_snapshot(
    session_factory: object,
    *,
    instructor_id: object,
    window_start: object,
    window_end: object,
    configuration: object,
    credential_cipher: object,
) -> CalendarBusySnapshot:
    (
        trusted_instructor_id,
        normalized_window_start,
        normalized_window_end,
    ) = _validated_static_inputs(
        session_factory=session_factory,
        instructor_id=instructor_id,
        window_start=window_start,
        window_end=window_end,
        configuration=configuration,
        credential_cipher=credential_cipher,
    )

    await _checkpoint_cancellation()
    claim = await _run_joined(
        partial(
            claim_google_calendar_refresh_credential,
            session_factory,
            instructor_id=trusted_instructor_id,
            credential_cipher=credential_cipher,
        )
    )
    credential_cipher = None
    await _checkpoint_cancellation()
    if type(claim) is str:
        configuration = session_factory = None
        if claim == "disconnected":
            claim = None
            return CalendarBusySnapshot(status="disconnected", periods=())
        if claim == "authorization_required":
            claim = None
            raise ExternalCalendarAuthorizationRequired()
        raise ExternalCalendarInvalidResponse()
    if type(claim) is not GoogleCalendarRefreshCredentialClaim:
        raise ExternalCalendarInvalidResponse()
    trusted_claim = _validated_claim_snapshot(claim)
    if trusted_claim.instructor_id != trusted_instructor_id:
        raise ExternalCalendarInvalidResponse()
    trusted_claim = None

    await _checkpoint_cancellation()
    try:
        refresh_outcome = (
            await exchange_google_calendar_refresh_token_with_deadline(
                configuration=configuration,
                claim=claim,
            )
        )
    except Exception:
        raise ExternalCalendarUnavailable("provider_unavailable") from None
    configuration = None
    if type(refresh_outcome) is GoogleCalendarRefreshAuthorizationRequired:
        _require_matching_authorization_required(claim, refresh_outcome)
        await _checkpoint_cancellation()
        recovery_result = await _run_joined(
            partial(
                mark_google_calendar_refresh_credential_authorization_required,
                session_factory,
                claim=claim,
                outcome=refresh_outcome,
            )
        )
        await _checkpoint_cancellation()
        claim = refresh_outcome = session_factory = None
        if recovery_result is not None:
            raise ExternalCalendarInvalidResponse()
        raise ExternalCalendarAuthorizationRequired()
    if type(refresh_outcome) is not GoogleCalendarRefreshTokenExchangeResult:
        raise ExternalCalendarInvalidResponse()
    _require_matching_access_result(claim, refresh_outcome)

    await _checkpoint_cancellation()
    preflight_result = await _run_joined(
        partial(
            revalidate_google_calendar_refresh_credential_claim,
            session_factory,
            claim=claim,
        )
    )
    if preflight_result is not None:
        raise ExternalCalendarInvalidResponse()

    await _checkpoint_cancellation()
    query_result = await query_google_calendar_freebusy_with_deadline(
        claim=claim,
        access_result=refresh_outcome,
        window_start=normalized_window_start,
        window_end=normalized_window_end,
    )
    refresh_outcome = None
    snapshot = _validated_snapshot_candidate(
        query_result,
        claim=claim,
        window_start=normalized_window_start,
        window_end=normalized_window_end,
    )
    query_result = None

    await _checkpoint_cancellation()
    final_result = await _run_joined(
        partial(
            revalidate_google_calendar_refresh_credential_claim,
            session_factory,
            claim=claim,
        )
    )
    await _checkpoint_cancellation()
    claim = session_factory = None
    if final_result is not None:
        raise ExternalCalendarInvalidResponse()
    return snapshot


def _validated_static_inputs(
    *,
    session_factory: object,
    instructor_id: object,
    window_start: object,
    window_end: object,
    configuration: object,
    credential_cipher: object,
) -> tuple[str, datetime.datetime, datetime.datetime]:
    if (
        not callable(session_factory)
        or type(configuration) is not GoogleCalendarTokenExchangeConfiguration
        or type(credential_cipher) is not CalendarCredentialCipher
    ):
        raise ExternalCalendarUnavailable("provider_unavailable")
    try:
        trusted_instructor_id = _validated_instructor_id(instructor_id)
    except ValueError:
        raise ExternalCalendarInvalidResponse() from None
    normalized_start, normalized_end = _validated_window(
        window_start,
        window_end,
    )
    return trusted_instructor_id, normalized_start, normalized_end


def _validated_identifier(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
    ):
        raise ExternalCalendarInvalidResponse()
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise ExternalCalendarInvalidResponse() from None
    if len(encoded) > _MAX_IDENTIFIER_BYTES or any(
        byte < 0x21 or byte > 0x7E for byte in encoded
    ):
        raise ExternalCalendarInvalidResponse()
    return value


def _validated_window(
    window_start: object,
    window_end: object,
) -> tuple[datetime.datetime, datetime.datetime]:
    start = _aware_utc(window_start)
    end = _aware_utc(window_end)
    if end <= start or end - start > MAX_BUSY_QUERY_WINDOW:
        raise ExternalCalendarInvalidResponse()
    return start, end


def _aware_utc(value: object) -> datetime.datetime:
    if type(value) is not datetime.datetime or value.tzinfo is None:
        raise ExternalCalendarInvalidResponse()
    try:
        if value.utcoffset() is None:
            raise ExternalCalendarInvalidResponse()
        return value.astimezone(_UTC)
    except ExternalCalendarInvalidResponse:
        raise
    except Exception:
        raise ExternalCalendarInvalidResponse() from None


def _validated_claim_snapshot(
    claim: object,
) -> GoogleCalendarValidatedRefreshCredentialClaim:
    try:
        return validate_google_calendar_refresh_credential_claim(claim)
    except Exception:
        raise ExternalCalendarInvalidResponse() from None


def _require_matching_access_result(
    claim: GoogleCalendarRefreshCredentialClaim,
    access_result: GoogleCalendarRefreshTokenExchangeResult,
) -> None:
    trusted_claim = _validated_claim_snapshot(claim)
    try:
        trusted_access = _validated_access_result(access_result)
    except Exception:
        raise ExternalCalendarInvalidResponse() from None
    if (
        trusted_access.account_id != trusted_claim.account_id
        or trusted_access.instructor_id != trusted_claim.instructor_id
        or trusted_access.connection_id != trusted_claim.connection_id
        or trusted_access.expected_calendar_connection_revision
        != trusted_claim.expected_calendar_connection_revision
    ):
        raise ExternalCalendarInvalidResponse()


def _require_matching_authorization_required(
    claim: GoogleCalendarRefreshCredentialClaim,
    outcome: GoogleCalendarRefreshAuthorizationRequired,
) -> None:
    trusted_claim = _validated_claim_snapshot(claim)
    try:
        outcome_fence = (
            _validated_identifier(outcome.account_id),
            _validated_instructor_id(outcome.instructor_id),
            _validated_connection_id(outcome.connection_id),
            _validated_revision(
                outcome.expected_calendar_connection_revision
            ),
        )
    except ExternalCalendarInvalidResponse:
        raise
    except Exception:
        raise ExternalCalendarInvalidResponse() from None
    claim_fence = (
        trusted_claim.account_id,
        trusted_claim.instructor_id,
        trusted_claim.connection_id,
        trusted_claim.expected_calendar_connection_revision,
    )
    if outcome_fence != claim_fence:
        raise ExternalCalendarInvalidResponse()


def _validated_snapshot_candidate(
    result: object,
    *,
    claim: GoogleCalendarRefreshCredentialClaim,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
) -> CalendarBusySnapshot:
    try:
        return _validated_snapshot_candidate_inner(
            result,
            claim=claim,
            window_start=window_start,
            window_end=window_end,
        )
    except ExternalCalendarInvalidResponse:
        raise
    except Exception:
        raise ExternalCalendarInvalidResponse() from None


def _validated_snapshot_candidate_inner(
    result: object,
    *,
    claim: GoogleCalendarRefreshCredentialClaim,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
) -> CalendarBusySnapshot:
    if type(result) is not GoogleCalendarFreeBusyQueryResult:
        raise ExternalCalendarInvalidResponse()
    trusted_claim = _validated_claim_snapshot(claim)
    result_fence = (
        _validated_identifier(result.account_id),
        _validated_instructor_id(result.instructor_id),
        _validated_connection_id(result.connection_id),
        _validated_revision(result.expected_calendar_connection_revision),
    )
    claim_fence = (
        trusted_claim.account_id,
        trusted_claim.instructor_id,
        trusted_claim.connection_id,
        trusted_claim.expected_calendar_connection_revision,
    )
    if result_fence != claim_fence:
        raise ExternalCalendarInvalidResponse()
    if (
        _aware_utc(result.window_start) != window_start
        or _aware_utc(result.window_end) != window_end
        or type(result.periods) is not tuple
        or len(result.periods) > MAX_BUSY_INTERVALS
    ):
        raise ExternalCalendarInvalidResponse()

    copied_periods: list[CalendarBusyInterval] = []
    for interval in result.periods:
        if type(interval) is not CalendarBusyInterval:
            raise ExternalCalendarInvalidResponse()
        copied_periods.append(
            CalendarBusyInterval(
                start=_aware_utc(interval.start),
                end=_aware_utc(interval.end),
            )
        )
    normalized_periods = normalize_busy_intervals(
        copied_periods,
        window_start=window_start,
        window_end=window_end,
    )
    if tuple(copied_periods) != normalized_periods:
        raise ExternalCalendarInvalidResponse()
    return CalendarBusySnapshot(
        status="verified",
        periods=normalized_periods,
    )


def _validated_connection_id(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != len(_CONNECTION_ID_PREFIX) + 32
        or not value.startswith(_CONNECTION_ID_PREFIX)
    ):
        raise ExternalCalendarInvalidResponse()
    suffix = value[len(_CONNECTION_ID_PREFIX):]
    if any(character not in "0123456789abcdef" for character in suffix):
        raise ExternalCalendarInvalidResponse()
    return value


def _validated_revision(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 9_223_372_036_854_775_807:
        raise ExternalCalendarInvalidResponse()
    return value


async def _run_joined(operation: Callable[[], _Result]) -> _Result:
    if not callable(operation):
        raise ValueError("invalid joined operation")
    start_gate = asyncio.Event()
    worker: asyncio.Task[_OwnedWorkerOutcome] | None = None
    guardian: asyncio.Task[_OwnedWorkerOutcome] | None = None
    guardian_ready: asyncio.Future[None] | None = None
    ownership_committed: asyncio.Future[None] | None = None
    start_committed_signal: asyncio.Future[None] | None = None
    induced_worker_cancellation = [False]
    start_committed = False
    try:
        async with _OwnedTaskGroup() as owner:
            try:
                ownership_committed = (
                    asyncio.get_running_loop().create_future()
                )
                start_committed_signal = (
                    asyncio.get_running_loop().create_future()
                )
                worker = _create_owned_worker_task(
                    owner,
                    _run_worker(
                        start_gate,
                        ownership_committed,
                        start_committed_signal,
                        operation,
                    ),
                )
                guardian_ready = (
                    asyncio.get_running_loop().create_future()
                )
                guardian = _create_owned_base_task(
                    owner,
                    _guard_owned_worker(
                        worker,
                        guardian_ready,
                        start_committed_signal,
                        induced_worker_cancellation,
                    ),
                )
                guardian.add_done_callback(start_committed_signal.cancel)
                ownership_committed.set_result(None)
                operation = None
                ready_or_finished, _pending = await asyncio.wait(
                    (guardian_ready, guardian),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if guardian in ready_or_finished:
                    outcome = guardian.result()
                    return _unwrap_worker_outcome(outcome)
                start_gate.set()
                start_committed_signal.set_result(None)
                start_committed = True
                outcome = await asyncio.shield(guardian)
                await _checkpoint_cancellation()
            except BaseException as parent_raised:
                operation = None
                if not start_committed and worker is not None:
                    if guardian is not None:
                        return await _finish_precommit_owned_worker(
                            worker,
                            guardian,
                            parent_raised,
                            start_committed_signal,
                        )
                    return await _finish_interrupted_join(
                        worker,
                        parent_raised,
                        cancel_worker=True,
                    )
                if guardian is not None:
                    return await _finish_interrupted_join(
                        guardian,
                        parent_raised,
                    )
                if worker is not None:
                    return await _finish_interrupted_join(
                        worker,
                        parent_raised,
                    )
                _raise_parent_interruption(parent_raised)
    except BaseException as owner_raised:
        operation = None
        if not start_committed and worker is not None:
            if guardian is not None:
                return await _finish_precommit_owned_worker(
                    worker,
                    guardian,
                    owner_raised,
                    start_committed_signal,
                )
            return await _finish_interrupted_join(
                worker,
                owner_raised,
                cancel_worker=True,
            )
        if guardian is not None:
            return await _finish_interrupted_join(
                guardian,
                owner_raised,
            )
        if worker is not None:
            return await _finish_interrupted_join(
                worker,
                owner_raised,
            )
        _raise_parent_interruption(owner_raised)
    return _unwrap_worker_outcome(outcome)


def _create_owned_base_task(
    owner: _OwnedTaskGroup,
    coroutine: Coroutine[object, object, _OwnedWorkerOutcome],
) -> asyncio.Task[_OwnedWorkerOutcome]:
    """Create and structurally own a base Task without factory mutation."""

    if owner._guardian is not None:
        coroutine.close()
        raise RuntimeError("guardian already adopted")
    loop = asyncio.get_running_loop()
    guardian = _BASE_TASK(coroutine, loop=loop)
    _adopt_owned_task(owner, guardian)
    return guardian


def _create_owned_worker_task(
    owner: _OwnedTaskGroup,
    coroutine: Coroutine[object, object, _OwnedWorkerOutcome],
) -> _JoinedWorkerTask:
    if owner._owned_worker is not None:
        coroutine.close()
        raise RuntimeError("joined worker already adopted")
    loop = asyncio.get_running_loop()
    worker = _JoinedWorkerTask(coroutine, loop=loop)
    _adopt_owned_task(owner, worker)
    return worker


def _adopt_owned_task(
    owner: _OwnedTaskGroup,
    task: _JoinedWorkerTask | asyncio.Task[_OwnedWorkerOutcome],
) -> None:
    try:
        if type(task) is _JoinedWorkerTask:
            if owner._owned_worker is not None:
                raise RuntimeError("joined worker already adopted")
            owner._owned_worker = task
        elif type(task) is _BASE_TASK:
            if owner._guardian is not None:
                raise RuntimeError("guardian already adopted")
            owner._guardian = task
        else:
            raise TypeError("invalid owned task type")
        # TaskGroup has no public adoption API. Keeping this compatibility
        # shim here avoids mutating the loop-global task factory while using
        # TaskGroup's repeated-cancellation-safe exit path on Python 3.11+.
        task.add_done_callback(owner._on_task_done)
        owner._tasks.add(task)
    except BaseException:
        if owner._owned_worker is task:
            owner._owned_worker = None
        if owner._guardian is task:
            owner._guardian = None
        _TASK_CANCEL(task)
        raise


async def _finish_precommit_owned_worker(
    worker: asyncio.Task[_OwnedWorkerOutcome],
    guardian: asyncio.Task[_OwnedWorkerOutcome],
    parent_raised: BaseException,
    start_committed: asyncio.Future[None],
) -> NoReturn:
    try:
        start_committed.cancel()
    except BaseException as cancel_raised:
        raise BaseExceptionGroup(
            "owned worker startup interruptions",
            [parent_raised, cancel_raised],
        ) from None
    worker = None
    return await _finish_interrupted_join(
        guardian,
        parent_raised,
    )


async def _guard_owned_worker(
    worker: asyncio.Task[_OwnedWorkerOutcome],
    ready: asyncio.Future[None],
    start_committed: asyncio.Future[None],
    induced_worker_cancellation: list[bool],
) -> _OwnedWorkerOutcome:
    interruptions: list[BaseException] = []
    try:
        if not ready.done():
            ready.set_result(None)
        return await _guard_owned_worker_inner(
            worker,
            interruptions,
            start_committed,
            induced_worker_cancellation,
        )
    except BaseException as boundary_raised:
        interruptions.append(boundary_raised)
        boundary_raised = None
        if not start_committed.done():
            try:
                start_committed.cancel()
            except BaseException as cancel_raised:
                interruptions.append(cancel_raised)
                cancel_raised = None
                raise BaseExceptionGroup(
                    "guardian startup interruptions",
                    interruptions,
                ) from None
        outcome, joined_interruptions, overflowed = await _join_worker(worker)
        interruptions.extend(joined_interruptions)
        joined_interruptions = None
        if induced_worker_cancellation[0] and (
            (
                type(outcome) is _OwnedWorkerOutcome
                and outcome.control_exception == "cancelled"
            )
            or type(outcome) is asyncio.CancelledError
        ):
            outcome = object.__new__(_OwnedWorkerOutcome)
            object.__setattr__(outcome, "value", _UNSET)
            object.__setattr__(outcome, "failed", True)
            object.__setattr__(outcome, "control_exception", None)
            object.__setattr__(outcome, "system_exit_code", None)
        try:
            return _classified_guardian_outcome(
                outcome,
                tuple(interruptions),
                overflowed=overflowed,
            )
        except BaseException as emergency_raised:
            interruptions.append(emergency_raised)
            emergency_raised = None
            return _hard_guardian_outcome(
                outcome,
                tuple(interruptions),
                overflowed=overflowed,
            )


async def _guard_owned_worker_inner(
    worker: asyncio.Task[_OwnedWorkerOutcome],
    interruptions: list[BaseException],
    start_committed: asyncio.Future[None],
    induced_cancellation_state: list[bool],
) -> _OwnedWorkerOutcome:
    try:
        outcome = await asyncio.shield(worker)
        if (
            induced_cancellation_state[0]
            and type(outcome) is _OwnedWorkerOutcome
            and outcome.control_exception == "cancelled"
        ):
            return _new_owned_worker_outcome(failed=True)
        return outcome
    except BaseException as guardian_raised:
        interruptions.append(guardian_raised)
        try:
            current_is_induced_cancellation = (
                type(guardian_raised) is asyncio.CancelledError
                and not _current_task_is_cancelling()
                and not start_committed.done()
            )
        except BaseException:
            guardian_raised = None
            raise
        if current_is_induced_cancellation:
            interruptions.pop()
            induced_cancellation_state[0] = True
        guardian_raised = None
        if not start_committed.done():
            try:
                start_committed.cancel()
            except BaseException as cancel_raised:
                interruptions.append(cancel_raised)
                cancel_raised = None
                raise BaseExceptionGroup(
                    "guardian startup interruptions",
                    interruptions,
                ) from None
        outcome, joined_interruptions, overflowed = await _join_worker(worker)
        interruptions.extend(joined_interruptions)
        joined_interruptions = None
        return _merged_guardian_outcome(
            outcome,
            tuple(interruptions),
            overflowed=overflowed,
            ignore_worker_cancellation=induced_cancellation_state[0],
        )


def _merged_guardian_outcome(
    outcome: object,
    interruptions: tuple[BaseException, ...],
    *,
    overflowed: bool,
    ignore_worker_cancellation: bool = False,
) -> _OwnedWorkerOutcome:
    return _classified_guardian_outcome(
        outcome,
        interruptions,
        overflowed=overflowed,
        ignore_worker_cancellation=ignore_worker_cancellation,
    )


def _classified_guardian_outcome(
    outcome: object,
    interruptions: tuple[BaseException, ...],
    *,
    overflowed: bool,
    ignore_worker_cancellation: bool = False,
) -> _OwnedWorkerOutcome:
    for interrupted in interruptions:
        control_exception, system_exit_code = (
            _classified_non_cancel_control_exception(interrupted)
        )
        if control_exception is not None:
            return _new_owned_worker_outcome(
                control_exception=control_exception,
                system_exit_code=system_exit_code,
            )
    worker_control, worker_system_exit_code = (
        _non_cancel_control_from_worker_outcome(outcome)
    )
    if worker_control is not None:
        return _new_owned_worker_outcome(
            control_exception=worker_control,
            system_exit_code=worker_system_exit_code,
        )
    if overflowed or any(
        _failure_contains_cancellation(interrupted)
        for interrupted in interruptions
    ) or (
        not ignore_worker_cancellation
        and _worker_outcome_is_cancellation(outcome)
    ) or (
        isinstance(outcome, BaseException)
        and not ignore_worker_cancellation
        and _failure_contains_cancellation(outcome)
    ):
        return _new_owned_worker_outcome(control_exception="cancelled")
    if type(outcome) is _OwnedWorkerOutcome and not interruptions:
        if (
            ignore_worker_cancellation
            and outcome.control_exception == "cancelled"
        ):
            return _new_owned_worker_outcome(failed=True)
        return outcome
    return _new_owned_worker_outcome(failed=True)


def _hard_guardian_outcome(
    outcome: object,
    interruptions: tuple[BaseException, ...],
    *,
    overflowed: bool,
) -> _OwnedWorkerOutcome:
    try:
        return _hard_guardian_outcome_inner(
            outcome,
            interruptions,
            overflowed=overflowed,
        )
    except BaseException as hard_raised:
        return _last_resort_guardian_outcome(
            outcome,
            interruptions,
            hard_raised,
        )


def _hard_guardian_outcome_inner(
    outcome: object,
    interruptions: tuple[BaseException, ...],
    *,
    overflowed: bool,
) -> _OwnedWorkerOutcome:
    control_exception, system_exit_code = _bounded_control_exception(
        *interruptions
    )
    worker_control = None
    worker_system_exit_code = None
    worker_cancelled = False
    if type(outcome) is _OwnedWorkerOutcome:
        worker_control = outcome.control_exception
        worker_system_exit_code = outcome.system_exit_code
        worker_cancelled = worker_control == "cancelled"
    elif isinstance(outcome, BaseException):
        worker_control, worker_system_exit_code = (
            _bounded_control_exception(outcome)
        )
        worker_cancelled = worker_control == "cancelled"
    selected_control = None
    selected_system_exit_code = None
    if control_exception not in (None, "cancelled"):
        selected_control = control_exception
        selected_system_exit_code = system_exit_code
    elif worker_control not in (None, "cancelled"):
        selected_control = worker_control
        selected_system_exit_code = worker_system_exit_code
    elif (
        control_exception == "cancelled"
        or worker_cancelled
        or overflowed
    ):
        selected_control = "cancelled"
    result = object.__new__(_OwnedWorkerOutcome)
    object.__setattr__(result, "value", _UNSET)
    object.__setattr__(result, "failed", selected_control is None)
    object.__setattr__(result, "control_exception", selected_control)
    object.__setattr__(
        result,
        "system_exit_code",
        selected_system_exit_code,
    )
    return result


def _last_resort_guardian_outcome(
    outcome: object,
    interruptions: tuple[BaseException, ...],
    hard_raised: BaseException,
) -> _OwnedWorkerOutcome:
    selected_control = None
    selected_system_exit_code = None
    for raised in (*interruptions, hard_raised):
        if isinstance(raised, KeyboardInterrupt):
            selected_control = "keyboard_interrupt"
            break
        if isinstance(raised, SystemExit):
            selected_control = "system_exit"
            selected_system_exit_code = (
                raised.code
                if type(raised) is SystemExit
                and (type(raised.code) is int or raised.code is None)
                else 1
            )
            break
        if isinstance(raised, GeneratorExit):
            selected_control = "generator_exit"
            break
    if (
        selected_control is None
        and type(outcome) is _OwnedWorkerOutcome
        and outcome.control_exception not in (None, "cancelled")
    ):
        selected_control = outcome.control_exception
        selected_system_exit_code = outcome.system_exit_code
    if selected_control is None:
        selected_control = "cancelled"
    result = object.__new__(_OwnedWorkerOutcome)
    object.__setattr__(result, "value", _UNSET)
    object.__setattr__(result, "failed", False)
    object.__setattr__(result, "control_exception", selected_control)
    object.__setattr__(
        result,
        "system_exit_code",
        selected_system_exit_code,
    )
    return result


async def _run_worker(
    start_gate: asyncio.Event,
    ownership_committed: asyncio.Future[None],
    start_committed: asyncio.Future[None],
    operation: Callable[[], _Result],
) -> _OwnedWorkerOutcome:
    try:
        return await _run_worker_inner(
            start_gate,
            ownership_committed,
            start_committed,
            operation,
        )
    except BaseException as boundary_raised:
        start_gate = operation = None
        try:
            outcome = _emergency_worker_outcome(boundary_raised)
            boundary_raised = None
        except BaseException as emergency_raised:
            control_exception, system_exit_code = (
                _bounded_control_exception(
                    boundary_raised,
                    emergency_raised,
                )
            )
            boundary_raised = emergency_raised = None
            outcome = object.__new__(_OwnedWorkerOutcome)
            object.__setattr__(outcome, "value", _UNSET)
            object.__setattr__(
                outcome,
                "failed",
                control_exception is None,
            )
            object.__setattr__(
                outcome,
                "control_exception",
                control_exception,
            )
            object.__setattr__(
                outcome,
                "system_exit_code",
                system_exit_code,
            )
        return outcome


async def _run_worker_inner(
    start_gate: asyncio.Event,
    ownership_committed: asyncio.Future[None],
    start_committed: asyncio.Future[None],
    operation: Callable[[], _Result],
) -> _OwnedWorkerOutcome:
    try:
        # A task factory may schedule this coroutine and then raise before the
        # caller receives the Task. An unowned task expires after one turn;
        # an owned task waits for the separately committed start gate.
        await asyncio.sleep(0)
        if not ownership_committed.done():
            operation = None
            return _new_owned_worker_outcome(failed=True)
        try:
            await asyncio.shield(start_committed)
        except asyncio.CancelledError:
            if start_committed.cancelled():
                operation = None
                return _new_owned_worker_outcome(failed=True)
            raise
        if start_committed.cancelled():
            operation = None
            return _new_owned_worker_outcome(failed=True)
        await start_gate.wait()
        value = await anyio.to_thread.run_sync(
            operation,
            abandon_on_cancel=False,
        )
    except BaseException as raised:
        try:
            control_exception, system_exit_code, _failure_reason = (
                _safe_classified_service_failure(raised)
            )
            if control_exception is not None:
                outcome = _new_owned_worker_outcome(
                    control_exception=control_exception,
                    system_exit_code=system_exit_code,
                )
            else:
                outcome = _new_owned_worker_outcome(failed=True)
        except BaseException as outcome_raised:
            outcome = _combined_emergency_worker_outcome(
                raised,
                outcome_raised,
            )
            outcome_raised = None
        raised = operation = None
        return outcome
    finally:
        operation = None
    return _new_owned_worker_outcome(value=value)


def _emergency_worker_outcome(
    raised: BaseException,
) -> _OwnedWorkerOutcome:
    try:
        control_exception, system_exit_code, _failure_reason = (
            _classified_service_failure(raised)
        )
    except BaseException as classifier_raised:
        control_exception, system_exit_code = _bounded_control_exception(
            raised,
            classifier_raised,
        )
        classifier_raised = None
    outcome = object.__new__(_OwnedWorkerOutcome)
    object.__setattr__(outcome, "value", _UNSET)
    object.__setattr__(
        outcome,
        "failed",
        control_exception is None,
    )
    object.__setattr__(
        outcome,
        "control_exception",
        control_exception,
    )
    object.__setattr__(outcome, "system_exit_code", system_exit_code)
    return outcome


def _combined_emergency_worker_outcome(
    original: BaseException,
    secondary: BaseException,
) -> _OwnedWorkerOutcome:
    original_outcome = _emergency_worker_outcome(original)
    secondary_outcome = _emergency_worker_outcome(secondary)
    control_exception = None
    system_exit_code = None
    for candidate in (original_outcome, secondary_outcome):
        if candidate.control_exception not in (None, "cancelled"):
            control_exception = candidate.control_exception
            system_exit_code = candidate.system_exit_code
            break
    else:
        if (
            original_outcome.control_exception == "cancelled"
            or secondary_outcome.control_exception == "cancelled"
        ):
            control_exception = "cancelled"
    original = secondary = original_outcome = secondary_outcome = None
    outcome = object.__new__(_OwnedWorkerOutcome)
    object.__setattr__(outcome, "value", _UNSET)
    object.__setattr__(outcome, "failed", control_exception is None)
    object.__setattr__(outcome, "control_exception", control_exception)
    object.__setattr__(outcome, "system_exit_code", system_exit_code)
    return outcome


def _unwrap_worker_outcome(outcome: object) -> _Result:
    if type(outcome) is not _OwnedWorkerOutcome:
        raise ValueError("invalid joined worker outcome")
    if outcome.control_exception is not None:
        _raise_control_exception(
            outcome.control_exception,
            system_exit_code=outcome.system_exit_code,
        )
    if type(outcome.failed) is not bool or outcome.failed:
        raise RuntimeError("joined database operation failed") from None
    if outcome.value is _UNSET:
        raise ValueError("invalid joined worker outcome")
    return outcome.value


async def _finish_interrupted_join(
    worker: asyncio.Task[_OwnedWorkerOutcome],
    parent_raised: BaseException,
    *,
    cancel_worker: bool = False,
) -> NoReturn:
    interruptions: list[BaseException] = [parent_raised]
    parent_raised = None
    if cancel_worker:
        try:
            _TASK_CANCEL(worker)
        except BaseException as cancel_raised:
            interruptions.append(cancel_raised)
            cancel_raised = None
    outcome, joined_interruptions, overflowed = await _join_worker(worker)
    interruptions.extend(joined_interruptions)

    for interrupted in interruptions:
        control_exception, system_exit_code = (
            _classified_non_cancel_control_exception(interrupted)
        )
        if control_exception is not None:
            interruptions.clear()
            outcome = None
            _raise_control_exception(
                control_exception,
                system_exit_code=system_exit_code,
            )
    worker_control, worker_system_exit_code = (
        _non_cancel_control_from_worker_outcome(outcome)
    )
    if worker_control is not None:
        interruptions.clear()
        outcome = None
        _raise_control_exception(
            worker_control,
            system_exit_code=worker_system_exit_code,
        )

    for interrupted in interruptions:
        if type(interrupted) is asyncio.CancelledError:
            direct_cancellation = interrupted
            interruptions.clear()
            outcome = None
            _raise_boundary_cancellation(direct_cancellation)
    if overflowed or any(
        _failure_contains_cancellation(interrupted)
        for interrupted in interruptions
    ) or (
        not cancel_worker and _worker_outcome_is_cancellation(outcome)
    ):
        interruptions.clear()
        outcome = None
        _raise_control_exception("cancelled", system_exit_code=None)

    selected = interruptions[0]
    interruptions.clear()
    outcome = None
    raise selected


async def _join_worker(
    worker: asyncio.Task[_OwnedWorkerOutcome],
) -> tuple[object, tuple[BaseException, ...], bool]:
    outcome: object = _UNSET
    interruptions: list[BaseException] = []
    overflowed = False
    with anyio.CancelScope(shield=True):
        while not worker.done():
            try:
                outcome = await asyncio.shield(worker)
            except BaseException as raised:
                if worker.done():
                    observed = _observed_worker_result(worker)
                    if type(observed) is _OwnedWorkerOutcome:
                        if len(interruptions) < _MAX_CONTROL_EXCEPTION_NODES:
                            interruptions.append(raised)
                        else:
                            overflowed = True
                        outcome = observed
                    else:
                        outcome = observed
                    raised = observed = None
                    break
                if len(interruptions) < _MAX_CONTROL_EXCEPTION_NODES:
                    interruptions.append(raised)
                else:
                    overflowed = True
                raised = None
                continue
    if outcome is _UNSET and worker.done():
        outcome = _observed_worker_result(worker)
    return outcome, tuple(interruptions), overflowed


def _observed_worker_result(
    worker: asyncio.Task[_OwnedWorkerOutcome],
) -> object:
    try:
        return worker.result()
    except BaseException as raised:
        return raised


def _non_cancel_control_from_worker_outcome(
    outcome: object,
) -> tuple[str | None, int | None]:
    if type(outcome) is _OwnedWorkerOutcome:
        if outcome.control_exception not in (None, "cancelled"):
            return outcome.control_exception, outcome.system_exit_code
        return None, None
    if isinstance(outcome, BaseException):
        return _classified_non_cancel_control_exception(outcome)
    return None, None


def _worker_outcome_is_cancellation(outcome: object) -> bool:
    if type(outcome) is _OwnedWorkerOutcome:
        return outcome.control_exception == "cancelled"
    return isinstance(outcome, asyncio.CancelledError)


def _failure_contains_cancellation(raised: BaseException) -> bool:
    control_exception, _system_exit_code, _failure_reason = (
        _safe_classified_service_failure(raised)
    )
    return control_exception == "cancelled"


def _raise_parent_interruption(raised: BaseException) -> NoReturn:
    control_exception, system_exit_code = (
        _classified_non_cancel_control_exception(raised)
    )
    if control_exception is not None:
        raised = None
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    if type(raised) is asyncio.CancelledError:
        _raise_boundary_cancellation(raised)
    raise raised


async def _checkpoint_cancellation() -> None:
    if not _current_task_is_cancelling():
        return
    await asyncio.sleep(0)
    if _current_task_is_cancelling():
        _raise_control_exception("cancelled", system_exit_code=None)


def _current_task_is_cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and _TASK_CANCELLING(task) > 0


def _safe_classified_service_failure(
    raised: BaseException,
) -> tuple[str | None, int | None, CalendarBusyFailureReason]:
    try:
        return _classified_service_failure(raised)
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


def _classified_service_failure(
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
        if reason in _FAILURE_REASONS:
            return None, None, reason
    return None, None, "provider_unavailable"


def _classified_non_cancel_control_exception(
    raised: BaseException,
) -> tuple[str | None, int | None]:
    try:
        nodes, _overflowed = _bounded_exception_graph(raised)
    except BaseException as classifier_raised:
        control_exception, system_exit_code = _bounded_control_exception(
            raised,
            classifier_raised,
        )
        classifier_raised = None
        if control_exception in (None, "cancelled"):
            return None, None
        return control_exception, system_exit_code
    for current in nodes:
        outcome = _direct_control_exception(current)
        if outcome[0] not in (None, "cancelled"):
            return outcome
    return None, None


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
    """Classify controls across failures without trusting another classifier."""

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


def _raise_service_failure(reason: CalendarBusyFailureReason) -> NoReturn:
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


def _raise_boundary_cancellation(
    error: asyncio.CancelledError,
) -> NoReturn:
    if (
        type(error) is not asyncio.CancelledError
        or not _current_task_is_cancelling()
    ):
        error = None
        _raise_control_exception("cancelled", system_exit_code=None)
    _raise_direct_cancellation(error)


def _raise_direct_cancellation(
    error: asyncio.CancelledError,
) -> NoReturn:
    error.__cause__ = None
    error.__context__ = None
    error.__suppress_context__ = True
    error.__traceback__ = None
    raise error.with_traceback(None) from None


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
        _raise_service_failure("provider_unavailable")
    try:
        raise error from None
    except BaseException as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise
