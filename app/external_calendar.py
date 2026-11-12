"""Provider-neutral external calendar availability contracts.

This module intentionally contains no OAuth, persistence, or provider transport.
It defines the privacy-safe boundary that those layers must satisfy: callers can
observe only occupied instants, never event titles, attendees, or descriptions.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Iterable, Literal, Protocol, runtime_checkable


UTC = datetime.timezone.utc
MAX_BUSY_QUERY_WINDOW = datetime.timedelta(days=15)
MAX_BUSY_INTERVALS = 10_000


class ExternalCalendarError(Exception):
    """Base error for external calendar availability failures."""


CalendarBusyFailureReason = Literal[
    "authorization_required",
    "timeout",
    "provider_unavailable",
    "invalid_response",
]
_BUSY_FAILURE_REASONS = {
    "authorization_required",
    "timeout",
    "provider_unavailable",
    "invalid_response",
}


class ExternalCalendarUnavailable(ExternalCalendarError):
    """The provider could not verify external calendar availability."""

    def __init__(self, reason_code: CalendarBusyFailureReason):
        if reason_code not in _BUSY_FAILURE_REASONS:
            raise ValueError("Unknown external calendar failure reason.")
        self.reason_code = reason_code
        super().__init__("External calendar availability could not be verified.")


class ExternalCalendarAuthorizationRequired(ExternalCalendarUnavailable):
    """The stored provider grant can no longer authorize availability reads."""

    def __init__(self):
        super().__init__("authorization_required")


class ExternalCalendarTimeout(ExternalCalendarUnavailable):
    """The external calendar provider did not answer within its deadline."""

    def __init__(self):
        super().__init__("timeout")


class ExternalCalendarInvalidResponse(ExternalCalendarUnavailable):
    """The provider returned an unsafe or malformed busy interval response."""

    def __init__(self):
        super().__init__("invalid_response")


@dataclass(frozen=True, slots=True)
class CalendarBusyInterval:
    """An opaque half-open busy interval with no external event metadata."""

    start: datetime.datetime
    end: datetime.datetime


@dataclass(frozen=True, slots=True)
class CalendarBusySnapshot:
    """Connection state plus verified opaque intervals for one query window."""

    status: Literal["disconnected", "verified"]
    periods: tuple[CalendarBusyInterval, ...]


@runtime_checkable
class CalendarBusySource(Protocol):
    """Provider-scoped boundary consumed by scheduling and agenda services."""

    def query_busy_periods(
        self,
        *,
        instructor_id: str,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> CalendarBusySnapshot:
        """Return connection state and occupied periods for a bounded window."""


def query_calendar_busy_snapshot(
    source: CalendarBusySource,
    *,
    instructor_id: str,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
) -> CalendarBusySnapshot:
    """Query a trusted provider scope and normalize the source response."""

    if not isinstance(instructor_id, str) or not instructor_id.strip():
        raise ValueError("A trusted instructor_id is required for calendar availability.")
    normalized_window_start, normalized_window_end = _normalize_window(
        window_start,
        window_end,
    )

    try:
        snapshot = source.query_busy_periods(
            instructor_id=instructor_id,
            window_start=normalized_window_start,
            window_end=normalized_window_end,
        )
    except ExternalCalendarUnavailable as exc:
        raise _canonical_unavailable(exc.reason_code) from None
    except TimeoutError:
        raise ExternalCalendarTimeout() from None
    except Exception:
        raise ExternalCalendarUnavailable("provider_unavailable") from None
    if not isinstance(snapshot, CalendarBusySnapshot):
        raise ExternalCalendarInvalidResponse()
    if not isinstance(snapshot.periods, tuple):
        raise ExternalCalendarInvalidResponse()
    if snapshot.status == "disconnected":
        if snapshot.periods:
            raise ExternalCalendarInvalidResponse()
        return CalendarBusySnapshot(status="disconnected", periods=())
    if snapshot.status != "verified":
        raise ExternalCalendarInvalidResponse()
    return CalendarBusySnapshot(
        status="verified",
        periods=normalize_busy_intervals(
            snapshot.periods,
            window_start=normalized_window_start,
            window_end=normalized_window_end,
        ),
    )


def normalize_busy_intervals(
    intervals: Iterable[CalendarBusyInterval],
    *,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
) -> tuple[CalendarBusyInterval, ...]:
    """Validate, clip, sort, and merge external busy intervals.

    Inputs and provider values must be timezone-aware. Provider values are
    canonicalized to UTC, clipped to the requested half-open window, and merged
    when they overlap or touch. Any malformed provider value fails the complete
    read instead of allowing an unverified opening to appear available.
    """

    normalized_window_start, normalized_window_end = _normalize_window(
        window_start,
        window_end,
    )

    clipped: list[CalendarBusyInterval] = []
    try:
        iterator = iter(intervals)
        for interval_count, interval in enumerate(iterator, start=1):
            if interval_count > MAX_BUSY_INTERVALS:
                raise ExternalCalendarInvalidResponse()
            if not isinstance(interval, CalendarBusyInterval):
                raise ExternalCalendarInvalidResponse()
            start = _aware_utc(interval.start, field="busy interval start")
            end = _aware_utc(interval.end, field="busy interval end")
            if end <= start:
                raise ExternalCalendarInvalidResponse()

            clipped_start = max(start, normalized_window_start)
            clipped_end = min(end, normalized_window_end)
            if clipped_end > clipped_start:
                clipped.append(CalendarBusyInterval(start=clipped_start, end=clipped_end))
    except ExternalCalendarInvalidResponse:
        raise ExternalCalendarInvalidResponse() from None
    except Exception:
        raise ExternalCalendarInvalidResponse() from None

    clipped.sort(key=lambda interval: (interval.start, interval.end))
    merged: list[CalendarBusyInterval] = []
    for interval in clipped:
        if not merged or interval.start > merged[-1].end:
            merged.append(interval)
            continue
        merged[-1] = CalendarBusyInterval(
            start=merged[-1].start,
            end=max(merged[-1].end, interval.end),
        )
    return tuple(merged)


def _aware_utc(value: datetime.datetime, *, field: str) -> datetime.datetime:
    if not isinstance(value, datetime.datetime):
        raise TypeError(f"{field} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset.")
    return value.astimezone(UTC)


def _normalize_window(
    window_start: datetime.datetime,
    window_end: datetime.datetime,
) -> tuple[datetime.datetime, datetime.datetime]:
    normalized_start = _aware_utc(window_start, field="window_start")
    normalized_end = _aware_utc(window_end, field="window_end")
    if normalized_end <= normalized_start:
        raise ValueError("External calendar window_end must be after window_start.")
    if normalized_end - normalized_start > MAX_BUSY_QUERY_WINDOW:
        raise ValueError("External calendar query window exceeds the supported horizon.")
    return normalized_start, normalized_end


def _canonical_unavailable(
    reason_code: CalendarBusyFailureReason,
) -> ExternalCalendarUnavailable:
    if reason_code == "authorization_required":
        return ExternalCalendarAuthorizationRequired()
    if reason_code == "timeout":
        return ExternalCalendarTimeout()
    if reason_code == "invalid_response":
        return ExternalCalendarInvalidResponse()
    return ExternalCalendarUnavailable("provider_unavailable")
