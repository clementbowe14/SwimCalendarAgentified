"""Hard overall deadline for the concrete Google Calendar token exchange."""

from __future__ import annotations

import asyncio
from typing import NoReturn

import anyio

from app.google_calendar_credential_handoff import (
    GoogleCalendarRefreshCredentialClaim,
)
from app.google_calendar_oauth import GoogleCalendarTokenExchangeError
from app.google_calendar_token_exchange import (
    GOOGLE_CALENDAR_TOKEN_ENDPOINT,
    GoogleCalendarRefreshTokenExchangeOutcome,
    GoogleCalendarTokenExchangeConfiguration,
    GoogleCalendarTokenExchangeResult,
    _complete_google_calendar_authorization_code_exchange,
    _complete_google_calendar_refresh_token_exchange,
    _prepare_google_calendar_authorization_code_exchange,
    _prepare_google_calendar_refresh_token_exchange,
)
from app.google_calendar_token_httpx import _post_form_async


__all__ = (
    "exchange_google_calendar_authorization_code_with_deadline",
    "exchange_google_calendar_refresh_token_with_deadline",
)


_TOKEN_EXCHANGE_DEADLINE_SECONDS = 5.0
_TOKEN_EXCHANGE_TIMEOUT_SECONDS = 5.0


async def exchange_google_calendar_authorization_code_with_deadline(
    *,
    configuration: GoogleCalendarTokenExchangeConfiguration,
    authorization_code: str,
    code_verifier: str,
) -> GoogleCalendarTokenExchangeResult:
    """Run one concrete exchange within a cancellation-capable deadline."""

    # The first traced instruction must already be inside the handler.
    # Keyword values evaluate left-to-right, so the helper captures each
    # argument before the final tuple clears the public frame's references.
    try: return await _guarded_google_calendar_authorization_code_with_deadline(configuration=configuration, authorization_code=authorization_code, code_verifier=code_verifier, _cleared=(configuration := None, authorization_code := None, code_verifier := None))
    except BaseException as boundary_raised:
        try: configuration = authorization_code = code_verifier = None; control_exception, system_exit_code = _classified_control_exception(boundary_raised); boundary_raised = None
        except BaseException as cleanup_raised: configuration = authorization_code = code_verifier = boundary_raised = None; control_exception, system_exit_code = _classified_control_exception(cleanup_raised)
    if control_exception == "cancelled":
        _raise_cancelled()
    if control_exception is None and _current_task_is_cancelling():
        _raise_cancelled()
    if control_exception is not None:
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    _raise_token_exchange_error()


async def exchange_google_calendar_refresh_token_with_deadline(
    *,
    configuration: GoogleCalendarTokenExchangeConfiguration,
    claim: GoogleCalendarRefreshCredentialClaim,
) -> GoogleCalendarRefreshTokenExchangeOutcome:
    """Run one claim-bound refresh exchange within a hard deadline."""

    # The first traced instruction must already be inside the handler.
    # Clear the public frame only after the helper arguments are captured.
    try: return await _guarded_google_calendar_refresh_token_with_deadline(configuration=configuration, claim=claim, _cleared=(configuration := None, claim := None))
    except BaseException as boundary_raised:
        try: configuration = claim = None; control_exception, system_exit_code = _classified_control_exception(boundary_raised); boundary_raised = None
        except BaseException as cleanup_raised: configuration = claim = boundary_raised = None; control_exception, system_exit_code = _classified_control_exception(cleanup_raised)
    if control_exception == "cancelled":
        _raise_cancelled()
    if control_exception is None and _current_task_is_cancelling():
        _raise_cancelled()
    if control_exception is not None:
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    _raise_token_exchange_error()


async def _guarded_google_calendar_authorization_code_with_deadline(
    *,
    configuration: object,
    authorization_code: object,
    code_verifier: object,
    _cleared: object,
) -> GoogleCalendarTokenExchangeResult:
    del _cleared
    try:
        try: return await _exchange_google_calendar_authorization_code_with_deadline(configuration=configuration, authorization_code=authorization_code, code_verifier=code_verifier)
        except BaseException as failure:
            try: configuration = authorization_code = code_verifier = None; control_exception, system_exit_code = _classified_control_exception(failure); failure = None
            except BaseException as cleanup_raised: configuration = authorization_code = code_verifier = failure = None; control_exception, system_exit_code = _classified_control_exception(cleanup_raised)
    except BaseException as boundary_raised:
        try: configuration = authorization_code = code_verifier = None; control_exception, system_exit_code = _classified_control_exception(boundary_raised); boundary_raised = None
        except BaseException as cleanup_raised: configuration = authorization_code = code_verifier = boundary_raised = None; control_exception, system_exit_code = _classified_control_exception(cleanup_raised)
    if control_exception == "cancelled":
        _raise_cancelled()
    if control_exception is None and _current_task_is_cancelling():
        _raise_cancelled()
    if control_exception is not None:
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    _raise_token_exchange_error()


async def _guarded_google_calendar_refresh_token_with_deadline(
    *,
    configuration: object,
    claim: object,
    _cleared: object,
) -> GoogleCalendarRefreshTokenExchangeOutcome:
    del _cleared
    try:
        try: return await _exchange_google_calendar_refresh_token_with_deadline(configuration=configuration, claim=claim)
        except BaseException as failure:
            try: configuration = claim = None; control_exception, system_exit_code = _classified_control_exception(failure); failure = None
            except BaseException as cleanup_raised: configuration = claim = failure = None; control_exception, system_exit_code = _classified_control_exception(cleanup_raised)
    except BaseException as boundary_raised:
        try: configuration = claim = None; control_exception, system_exit_code = _classified_control_exception(boundary_raised); boundary_raised = None
        except BaseException as cleanup_raised: configuration = claim = boundary_raised = None; control_exception, system_exit_code = _classified_control_exception(cleanup_raised)
    if control_exception == "cancelled":
        _raise_cancelled()
    if control_exception is None and _current_task_is_cancelling():
        _raise_cancelled()
    if control_exception is not None:
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    _raise_token_exchange_error()


async def _exchange_google_calendar_authorization_code_with_deadline(
    *,
    configuration: object,
    authorization_code: object,
    code_verifier: object,
) -> GoogleCalendarTokenExchangeResult:
    fields, pkce_code_challenge = (
        _prepare_google_calendar_authorization_code_exchange(
            configuration=configuration,
            authorization_code=authorization_code,
            code_verifier=code_verifier,
        )
    )
    deadline = _monotonic_time() + _TOKEN_EXCHANGE_DEADLINE_SECONDS
    timeout_scope = _new_deadline_scope(deadline=deadline)
    with timeout_scope:
        response = await _post_form_async(
            url=GOOGLE_CALENDAR_TOKEN_ENDPOINT,
            fields=fields,
            timeout_seconds=_TOKEN_EXCHANGE_TIMEOUT_SECONDS,
            deadline=deadline,
        )
        if (
            not timeout_scope.cancel_called
            and _current_task_is_cancelling()
        ):
            raise asyncio.CancelledError
        result = _complete_google_calendar_authorization_code_exchange(
            response,
            pkce_code_challenge=pkce_code_challenge,
        )

    if _current_task_is_cancelling():
        raise asyncio.CancelledError
    if timeout_scope.cancel_called or _monotonic_time() >= deadline:
        raise TimeoutError("token exchange exceeded its deadline")
    return result


async def _exchange_google_calendar_refresh_token_with_deadline(
    *,
    configuration: object,
    claim: object,
) -> GoogleCalendarRefreshTokenExchangeOutcome:
    fields, original_claim, expected_claim = (
        _prepare_google_calendar_refresh_token_exchange(
            configuration=configuration,
            claim=claim,
        )
    )
    deadline = _monotonic_time() + _TOKEN_EXCHANGE_DEADLINE_SECONDS
    timeout_scope = _new_deadline_scope(deadline=deadline)
    with timeout_scope:
        response = await _post_form_async(
            url=GOOGLE_CALENDAR_TOKEN_ENDPOINT,
            fields=fields,
            timeout_seconds=_TOKEN_EXCHANGE_TIMEOUT_SECONDS,
            deadline=deadline,
        )
        if (
            not timeout_scope.cancel_called
            and _current_task_is_cancelling()
        ):
            raise asyncio.CancelledError
        result = _complete_google_calendar_refresh_token_exchange(
            response,
            claim=original_claim,
            expected_claim=expected_claim,
        )

    if _current_task_is_cancelling():
        raise asyncio.CancelledError
    if timeout_scope.cancel_called or _monotonic_time() >= deadline:
        raise TimeoutError("token exchange exceeded its deadline")
    return result


def _new_deadline_scope(*, deadline: float) -> anyio.CancelScope:
    return anyio.CancelScope(deadline=deadline)


def _monotonic_time() -> float:
    return asyncio.get_running_loop().time()


def _current_task_is_cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _sanitized_system_exit_code(raised: SystemExit) -> int | None:
    if type(raised) is not SystemExit:
        return 1
    code = raised.code
    return code if type(code) is int or code is None else 1


def _classified_control_exception(
    raised: BaseException,
) -> tuple[str | None, int | None]:
    if isinstance(raised, asyncio.CancelledError):
        return "cancelled", None
    if isinstance(raised, KeyboardInterrupt):
        return "keyboard_interrupt", None
    if isinstance(raised, SystemExit):
        return "system_exit", _sanitized_system_exit_code(raised)
    if isinstance(raised, GeneratorExit):
        return "generator_exit", None
    return None, None


def _raise_cancelled() -> NoReturn:
    error = asyncio.CancelledError()
    try:
        raise error from None
    except asyncio.CancelledError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_control_exception(
    kind: str,
    *,
    system_exit_code: int | None,
) -> NoReturn:
    if kind == "keyboard_interrupt":
        error: BaseException = KeyboardInterrupt()
    elif kind == "system_exit":
        error = SystemExit(system_exit_code)
    elif kind == "generator_exit":
        error = GeneratorExit()
    else:
        _raise_token_exchange_error()
    try:
        raise error from None
    except BaseException as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_token_exchange_error() -> NoReturn:
    error = GoogleCalendarTokenExchangeError()
    try:
        raise error from None
    except GoogleCalendarTokenExchangeError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise
