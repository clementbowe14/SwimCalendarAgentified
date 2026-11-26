"""Lifecycle-fenced Google Calendar availability composition."""

from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import NoReturn, final

from sqlalchemy.orm import Session as _Session

from app.external_calendar import (
    MAX_BUSY_QUERY_WINDOW,
    CalendarBusySnapshot,
    CalendarBusySource,
    ExternalCalendarAuthorizationRequired,
    ExternalCalendarInvalidResponse,
    ExternalCalendarTimeout,
    ExternalCalendarUnavailable,
    query_calendar_busy_snapshot,
)
from app.google_calendar_availability_fence import (
    _classify_control_exception,
    _instructor_lookup_key,
    claim_google_calendar_availability_fence,
    revalidate_google_calendar_availability_fence,
)
from app.google_calendar_busy_source_factory import (
    _raise_control_exception,
    _raise_fresh,
)


__all__ = ("GoogleCalendarAvailabilitySource",)


_FAILURE_REASONS = (
    "authorization_required",
    "timeout",
    "provider_unavailable",
    "invalid_response",
)
_INVALID_QUERY_MESSAGE = "Google Calendar availability query is invalid."
_INVALID_CONFIGURATION_MESSAGE = (
    "Google Calendar availability source is not configured safely."
)


class _InvalidAvailabilityQuery(Exception):
    """Internal marker for caller-owned query validation failures."""


class _InvalidAvailabilitySourceConfiguration(Exception):
    """Internal marker for unsafe construction attempts."""


@final
class GoogleCalendarAvailabilitySource:
    """Query one instructor's Google calendar behind a lifecycle fence."""

    __slots__ = (
        "_session_factory",
        "_instructor_id",
        "_busy_source_factory",
    )

    def __init_subclass__(cls, **_kwargs: object) -> NoReturn:
        del cls, _kwargs
        _raise_fresh(
            TypeError("GoogleCalendarAvailabilitySource cannot be subclassed.")
        )

    def __init__(
        self,
        session_factory: Callable[[], _Session],
        *,
        instructor_id: str,
        busy_source_factory: Callable[
            [Callable[[], _Session]],
            CalendarBusySource,
        ],
    ) -> None:
        # The first traced instruction must already be inside the handler.
        try: return _install_source(self, session_factory, instructor_id, busy_source_factory)
        except BaseException as boundary_raised:
            try: self = session_factory = instructor_id = busy_source_factory = None; control_kind, system_exit_code = _classify_control_exception(boundary_raised); boundary_raised = None
            except BaseException as cleanup_raised: self = session_factory = instructor_id = busy_source_factory = boundary_raised = None; control_kind, system_exit_code = _classify_control_exception(cleanup_raised); cleanup_raised = None
        if control_kind is not None:
            _raise_control_exception(
                control_kind,
                system_exit_code=system_exit_code,
            )
        _raise_fresh(ValueError(_INVALID_CONFIGURATION_MESSAGE))

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        del self, _name, _value
        _raise_fresh(AttributeError("GoogleCalendarAvailabilitySource is immutable."))

    def __delattr__(self, _name: str) -> NoReturn:
        del self, _name
        _raise_fresh(AttributeError("GoogleCalendarAvailabilitySource is immutable."))

    def __reduce__(self) -> NoReturn:
        del self
        _raise_fresh(
            TypeError("GoogleCalendarAvailabilitySource is not serializable.")
        )

    def __reduce_ex__(self, _protocol: int) -> NoReturn:
        del self, _protocol
        _raise_fresh(
            TypeError("GoogleCalendarAvailabilitySource is not serializable.")
        )

    def __getstate__(self) -> NoReturn:
        del self
        _raise_fresh(
            TypeError("GoogleCalendarAvailabilitySource is not serializable.")
        )

    def __repr__(self) -> str:
        return "GoogleCalendarAvailabilitySource(<redacted>)"

    __str__ = __repr__

    def query_busy_periods(
        self,
        *,
        instructor_id: str,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> CalendarBusySnapshot:
        """Return one verified snapshot for the bound instructor."""

        # Capture every argument before clearing the public boundary frame.
        try: return _guarded_query_busy_periods(self, instructor_id=instructor_id, window_start=window_start, window_end=window_end, _cleared=(self := None, instructor_id := None, window_start := None, window_end := None))
        except BaseException as boundary_raised:
            try: self = instructor_id = window_start = window_end = None; control_kind, system_exit_code, failure_kind = _classified_failure(boundary_raised); boundary_raised = None
            except BaseException as cleanup_raised: self = instructor_id = window_start = window_end = boundary_raised = None; control_kind, system_exit_code = _classify_control_exception(cleanup_raised); cleanup_raised = None; failure_kind = "provider_unavailable"
        if control_kind is not None:
            _raise_control_exception(
                control_kind,
                system_exit_code=system_exit_code,
            )
        _raise_availability_failure(failure_kind)


def _install_source(
    source: object,
    session_factory: object,
    instructor_id: object,
    busy_source_factory: object,
) -> None:
    try:
        trusted_instructor_id = _instructor_lookup_key(instructor_id)
    except Exception:
        raise _InvalidAvailabilitySourceConfiguration() from None
    if (
        type(source) is not GoogleCalendarAvailabilitySource
        or not callable(session_factory)
        or not callable(busy_source_factory)
    ):
        raise _InvalidAvailabilitySourceConfiguration()
    for slot_name in (
        "_session_factory",
        "_instructor_id",
        "_busy_source_factory",
    ):
        try:
            object.__getattribute__(source, slot_name)
        except AttributeError:
            continue
        raise _InvalidAvailabilitySourceConfiguration()
    object.__setattr__(source, "_session_factory", session_factory)
    object.__setattr__(source, "_instructor_id", trusted_instructor_id)
    object.__setattr__(source, "_busy_source_factory", busy_source_factory)


def _guarded_query_busy_periods(
    source: object,
    *,
    instructor_id: object,
    window_start: object,
    window_end: object,
    _cleared: object,
) -> CalendarBusySnapshot:
    del _cleared
    try: return _query_busy_periods(source, instructor_id=instructor_id, window_start=window_start, window_end=window_end)
    except BaseException as boundary_raised:
        try: source = instructor_id = window_start = window_end = None; control_kind, system_exit_code, failure_kind = _classified_failure(boundary_raised); boundary_raised = None
        except BaseException as cleanup_raised: source = instructor_id = window_start = window_end = boundary_raised = None; control_kind, system_exit_code = _classify_control_exception(cleanup_raised); cleanup_raised = None; failure_kind = "provider_unavailable"
    if control_kind is not None:
        _raise_control_exception(
            control_kind,
            system_exit_code=system_exit_code,
        )
    _raise_guarded_failure(failure_kind)


def _query_busy_periods(
    source: object,
    *,
    instructor_id: object,
    window_start: object,
    window_end: object,
) -> CalendarBusySnapshot:
    if type(source) is not GoogleCalendarAvailabilitySource:
        raise ExternalCalendarUnavailable("provider_unavailable")
    session_factory = source._session_factory
    expected_instructor_id = source._instructor_id
    busy_source_factory = source._busy_source_factory
    source = None

    try:
        try:
            trusted_instructor_id = _instructor_lookup_key(instructor_id)
        except Exception:
            raise ExternalCalendarUnavailable("provider_unavailable") from None
        if trusted_instructor_id != expected_instructor_id:
            raise ExternalCalendarUnavailable("provider_unavailable")
        normalized_start, normalized_end = _validated_window(
            window_start,
            window_end,
        )
        return _query_fenced_source(
            session_factory,
            instructor_id=trusted_instructor_id,
            window_start=normalized_start,
            window_end=normalized_end,
            busy_source_factory=busy_source_factory,
        )
    finally:
        session_factory = expected_instructor_id = busy_source_factory = None
        instructor_id = window_start = window_end = None


def _validated_window(
    window_start: object,
    window_end: object,
) -> tuple[datetime.datetime, datetime.datetime]:
    if (
        type(window_start) is not datetime.datetime
        or type(window_end) is not datetime.datetime
    ):
        raise _InvalidAvailabilityQuery()
    try:
        if (
            window_start.tzinfo is None
            or window_end.tzinfo is None
            or window_start.utcoffset() is None
            or window_end.utcoffset() is None
        ):
            raise _InvalidAvailabilityQuery()
        normalized_start = window_start.astimezone(datetime.timezone.utc)
        normalized_end = window_end.astimezone(datetime.timezone.utc)
    except _InvalidAvailabilityQuery:
        raise
    except Exception:
        raise _InvalidAvailabilityQuery() from None
    if (
        normalized_end <= normalized_start
        or normalized_end - normalized_start > MAX_BUSY_QUERY_WINDOW
    ):
        raise _InvalidAvailabilityQuery()
    return normalized_start, normalized_end


def _query_fenced_source(
    session_factory: object,
    *,
    instructor_id: object,
    window_start: object,
    window_end: object,
    busy_source_factory: object,
) -> CalendarBusySnapshot:
    fence = concrete_source = snapshot = None
    try:
        fence = claim_google_calendar_availability_fence(
            session_factory,
            instructor_id=instructor_id,
        )
        if fence.state == "disconnected":
            revalidate_google_calendar_availability_fence(
                session_factory,
                fence=fence,
            )
            fence = None
            return CalendarBusySnapshot(status="disconnected", periods=())
        if fence.state == "authorization_required":
            revalidate_google_calendar_availability_fence(
                session_factory,
                fence=fence,
            )
            fence = None
            raise ExternalCalendarAuthorizationRequired()
        if fence.state != "connected" or not callable(busy_source_factory):
            raise ExternalCalendarUnavailable("provider_unavailable")

        concrete_source = busy_source_factory(session_factory)
        snapshot = query_calendar_busy_snapshot(
            concrete_source,
            instructor_id=instructor_id,
            window_start=window_start,
            window_end=window_end,
        )
        if snapshot.status != "verified":
            raise ExternalCalendarUnavailable("provider_unavailable")
        concrete_source = None
        revalidate_google_calendar_availability_fence(
            session_factory,
            fence=fence,
        )
        fence = None
        result = snapshot
        snapshot = None
        return result
    finally:
        concrete_source = fence = snapshot = None
        session_factory = busy_source_factory = None
        instructor_id = window_start = window_end = None


def _classified_failure(
    raised: BaseException,
) -> tuple[str | None, int | None, str]:
    control_kind, system_exit_code = _classify_control_exception(raised)
    if control_kind is not None:
        return control_kind, system_exit_code, "provider_unavailable"
    if type(raised) is _InvalidAvailabilityQuery:
        return None, None, "invalid_query"
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


def _raise_availability_failure(failure_kind: str) -> NoReturn:
    if failure_kind == "invalid_query":
        error: BaseException = ValueError(_INVALID_QUERY_MESSAGE)
    elif failure_kind == "authorization_required":
        error = ExternalCalendarAuthorizationRequired()
    elif failure_kind == "timeout":
        error = ExternalCalendarTimeout()
    elif failure_kind == "invalid_response":
        error = ExternalCalendarInvalidResponse()
    else:
        error = ExternalCalendarUnavailable("provider_unavailable")
    _raise_fresh(error)


def _raise_guarded_failure(failure_kind: str) -> NoReturn:
    if failure_kind == "invalid_query":
        _raise_fresh(_InvalidAvailabilityQuery())
    _raise_availability_failure(failure_kind)
