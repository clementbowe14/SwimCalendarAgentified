"""Route-inert orchestration for one Google Calendar OAuth callback."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from typing import NoReturn, TypeVar

import anyio
from sqlalchemy.orm import Session as _Session

from app.calendar_connections import CalendarConnectionStatus
from app.calendar_credentials import CalendarCredentialCipher
from app.calendar_oauth_pkce import CalendarOAuthPKCECipher
from app.google_calendar_oauth import GoogleCalendarOAuthError
from app.google_calendar_oauth_callback_claim import (
    GoogleCalendarOAuthCallbackClaim,
    claim_google_calendar_oauth_callback,
)
from app.google_calendar_oauth_callback_finalizer import (
    finalize_google_calendar_oauth_callback,
)
from app.google_calendar_token_deadline import (
    exchange_google_calendar_authorization_code_with_deadline,
)
from app.google_calendar_token_exchange import (
    GoogleCalendarTokenExchangeConfiguration,
    GoogleCalendarTokenExchangeResult,
)


__all__ = (
    "GoogleCalendarOAuthCallbackCompletionError",
    "complete_google_calendar_oauth_callback",
)


_Result = TypeVar("_Result")


class GoogleCalendarOAuthCallbackCompletionError(GoogleCalendarOAuthError):
    """A Google Calendar callback could not complete safely."""

    def __init__(self) -> None:
        super().__init__("Google Calendar authorization could not be completed.")


async def complete_google_calendar_oauth_callback(
    session_factory: Callable[[], _Session],
    *,
    raw_state: str,
    raw_browser_binding: str,
    authorization_code: str,
    configuration: GoogleCalendarTokenExchangeConfiguration,
    pkce_cipher: CalendarOAuthPKCECipher,
    credential_cipher: CalendarCredentialCipher,
) -> CalendarConnectionStatus:
    """Claim, exchange, and finalize one browser-bound OAuth callback."""

    cancelled = False
    try:
        return await _complete_google_calendar_oauth_callback(
            session_factory,
            raw_state=raw_state,
            raw_browser_binding=raw_browser_binding,
            authorization_code=authorization_code,
            configuration=configuration,
            pkce_cipher=pkce_cipher,
            credential_cipher=credential_cipher,
        )
    except asyncio.CancelledError:
        cancelled = True
    except Exception:
        pass
    del (
        session_factory,
        raw_state,
        raw_browser_binding,
        authorization_code,
        configuration,
        pkce_cipher,
        credential_cipher,
    )
    if cancelled:
        _raise_cancelled()
    _raise_completion_error()


async def _complete_google_calendar_oauth_callback(
    session_factory: object,
    *,
    raw_state: object,
    raw_browser_binding: object,
    authorization_code: object,
    configuration: object,
    pkce_cipher: object,
    credential_cipher: object,
) -> CalendarConnectionStatus:
    _validate_static_dependencies(
        session_factory=session_factory,
        configuration=configuration,
        pkce_cipher=pkce_cipher,
        credential_cipher=credential_cipher,
    )

    claim = await _run_joined(
        partial(
            claim_google_calendar_oauth_callback,
            session_factory,
            raw_state=raw_state,
            raw_browser_binding=raw_browser_binding,
            pkce_cipher=pkce_cipher,
        )
    )
    if type(claim) is not GoogleCalendarOAuthCallbackClaim:
        raise ValueError("invalid callback claim")

    token_result = (
        await exchange_google_calendar_authorization_code_with_deadline(
            configuration=configuration,
            authorization_code=authorization_code,
            code_verifier=claim.code_verifier,
        )
    )
    if type(token_result) is not GoogleCalendarTokenExchangeResult:
        raise ValueError("invalid token result")

    status = await _run_joined(
        partial(
            finalize_google_calendar_oauth_callback,
            session_factory,
            claim=claim,
            token_result=token_result,
            credential_cipher=credential_cipher,
        )
    )
    if type(status) is not CalendarConnectionStatus:
        raise ValueError("invalid connection status")
    return status


def _validate_static_dependencies(
    *,
    session_factory: object,
    configuration: object,
    pkce_cipher: object,
    credential_cipher: object,
) -> None:
    if (
        not callable(session_factory)
        or type(configuration) is not GoogleCalendarTokenExchangeConfiguration
        or type(pkce_cipher) is not CalendarOAuthPKCECipher
        or type(credential_cipher) is not CalendarCredentialCipher
    ):
        raise ValueError("invalid callback dependency")


async def _run_joined(operation: Callable[[], _Result]) -> _Result:
    worker = asyncio.create_task(_run_worker(operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        pass

    with anyio.CancelScope(shield=True):
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break

    try:
        worker.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    raise asyncio.CancelledError() from None


async def _run_worker(operation: Callable[[], _Result]) -> _Result:
    return await anyio.to_thread.run_sync(
        operation,
        abandon_on_cancel=False,
    )


def _raise_cancelled() -> NoReturn:
    error = asyncio.CancelledError()
    try:
        raise error from None
    except asyncio.CancelledError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_completion_error() -> NoReturn:
    error = GoogleCalendarOAuthCallbackCompletionError()
    try:
        raise error from None
    except GoogleCalendarOAuthCallbackCompletionError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise
