"""Hard overall deadline for the concrete Google Calendar token exchange."""

from __future__ import annotations

import asyncio
from typing import NoReturn

import anyio

from app.google_calendar_oauth import GoogleCalendarTokenExchangeError
from app.google_calendar_token_exchange import (
    GOOGLE_CALENDAR_TOKEN_ENDPOINT,
    GoogleCalendarTokenExchangeConfiguration,
    GoogleCalendarTokenExchangeResult,
    _complete_google_calendar_authorization_code_exchange,
    _prepare_google_calendar_authorization_code_exchange,
)
from app.google_calendar_token_httpx import _post_form_async


__all__ = ("exchange_google_calendar_authorization_code_with_deadline",)


_TOKEN_EXCHANGE_DEADLINE_SECONDS = 5.0
_TOKEN_EXCHANGE_TIMEOUT_SECONDS = 5.0


async def exchange_google_calendar_authorization_code_with_deadline(
    *,
    configuration: GoogleCalendarTokenExchangeConfiguration,
    authorization_code: str,
    code_verifier: str,
) -> GoogleCalendarTokenExchangeResult:
    """Run one concrete exchange within a cancellation-capable deadline."""

    cancelled = False
    try:
        return await _exchange_google_calendar_authorization_code_with_deadline(
            configuration=configuration,
            authorization_code=authorization_code,
            code_verifier=code_verifier,
        )
    except asyncio.CancelledError:
        cancelled = True
    except Exception:
        pass
    del configuration, authorization_code, code_verifier
    if cancelled:
        _raise_cancelled()
    _raise_token_exchange_error()


async def _exchange_google_calendar_authorization_code_with_deadline(
    *,
    configuration: object,
    authorization_code: object,
    code_verifier: object,
) -> GoogleCalendarTokenExchangeResult:
    fields = _prepare_google_calendar_authorization_code_exchange(
        configuration=configuration,
        authorization_code=authorization_code,
        code_verifier=code_verifier,
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
        result = _complete_google_calendar_authorization_code_exchange(response)

    if timeout_scope.cancel_called or _monotonic_time() >= deadline:
        raise TimeoutError("token exchange exceeded its deadline")
    return result


def _new_deadline_scope(*, deadline: float) -> anyio.CancelScope:
    return anyio.CancelScope(deadline=deadline)


def _monotonic_time() -> float:
    return asyncio.get_running_loop().time()


def _raise_cancelled() -> NoReturn:
    error = asyncio.CancelledError()
    try:
        raise error from None
    except asyncio.CancelledError as raised:
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
