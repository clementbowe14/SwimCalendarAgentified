import datetime
import itertools
import traceback
from dataclasses import fields
from zoneinfo import ZoneInfo

import pytest

from app.external_calendar import (
    CalendarBusyInterval,
    CalendarBusySnapshot,
    CalendarBusySource,
    ExternalCalendarAuthorizationRequired,
    ExternalCalendarInvalidResponse,
    ExternalCalendarTimeout,
    ExternalCalendarUnavailable,
    MAX_BUSY_INTERVALS,
    MAX_BUSY_QUERY_WINDOW,
    normalize_busy_intervals,
    query_calendar_busy_snapshot,
)


UTC = datetime.timezone.utc


def instant(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


WINDOW_START = instant("2026-11-12T14:00:00Z")
WINDOW_END = instant("2026-11-12T20:00:00Z")


def period(start: str, end: str) -> CalendarBusyInterval:
    return CalendarBusyInterval(start=instant(start), end=instant(end))


class StaticSource:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []

    def query_busy_periods(self, *, instructor_id, window_start, window_end):
        self.calls.append((instructor_id, window_start, window_end))
        return self.snapshot


def query(source, instructor_id="inst_private", start=WINDOW_START, end=WINDOW_END):
    return query_calendar_busy_snapshot(
        source,
        instructor_id=instructor_id,
        window_start=start,
        window_end=end,
    )


def formatted_exception(error: Exception) -> str:
    return "".join(traceback.format_exception(type(error), error, error.__traceback__))


def test_contract_is_opaque_and_fake_sources_are_structural():
    source = StaticSource(CalendarBusySnapshot(status="verified", periods=()))

    assert [field.name for field in fields(CalendarBusyInterval)] == ["start", "end"]
    assert isinstance(source, CalendarBusySource)


@pytest.mark.parametrize("status", ["disconnected", "verified"])
def test_disconnected_and_verified_empty_snapshots_remain_distinct(status):
    assert query(StaticSource(CalendarBusySnapshot(status=status, periods=()))) == (
        CalendarBusySnapshot(status=status, periods=())
    )


def test_busy_results_and_calls_remain_provider_scoped():
    periods = {
        "inst_first": (period("2026-11-12T14:15:00Z", "2026-11-12T14:45:00Z"),),
        "inst_second": (period("2026-11-12T14:30:00Z", "2026-11-12T15:00:00Z"),),
    }

    class ScopedSource:
        def __init__(self):
            self.calls = []

        def query_busy_periods(self, *, instructor_id, window_start, window_end):
            self.calls.append((instructor_id, window_start, window_end))
            return CalendarBusySnapshot(status="verified", periods=periods[instructor_id])

    source = ScopedSource()
    central = ZoneInfo("America/Chicago")
    start = datetime.datetime(2026, 11, 12, 8, tzinfo=central)
    end = datetime.datetime(2026, 11, 12, 9, tzinfo=central)

    results = [
        query(source, provider, start, end).periods
        for provider in ("inst_first", "inst_second", "inst_first")
    ]

    assert results == [periods["inst_first"], periods["inst_second"], periods["inst_first"]]
    assert [call[0] for call in source.calls] == ["inst_first", "inst_second", "inst_first"]
    assert all(call[1:] == (WINDOW_START, instant("2026-11-12T15:00:00Z")) for call in source.calls)


def test_intervals_are_clipped_sorted_and_coalesced():
    result = normalize_busy_intervals(
        [
            period("2026-11-12T18:00:00Z", "2026-11-12T19:00:00Z"),
            period("2026-11-12T13:30:00Z", "2026-11-12T15:00:00Z"),
            period("2026-11-12T14:45:00Z", "2026-11-12T16:00:00Z"),
            period("2026-11-12T16:00:00Z", "2026-11-12T17:00:00Z"),
            period("2026-11-12T19:30:00Z", "2026-11-12T21:00:00Z"),
        ],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert result == (
        period("2026-11-12T14:00:00Z", "2026-11-12T17:00:00Z"),
        period("2026-11-12T18:00:00Z", "2026-11-12T19:00:00Z"),
        period("2026-11-12T19:30:00Z", "2026-11-12T20:00:00Z"),
    )


def test_intervals_are_canonicalized_to_utc_and_outside_values_are_ignored():
    central = ZoneInfo("America/Chicago")
    result = normalize_busy_intervals(
        [
            CalendarBusyInterval(
                start=datetime.datetime(2026, 11, 12, 9, tzinfo=central),
                end=datetime.datetime(2026, 11, 12, 10, tzinfo=central),
            ),
            period("2026-11-13T01:00:00Z", "2026-11-13T02:00:00Z"),
        ],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert result == (period("2026-11-12T15:00:00Z", "2026-11-12T16:00:00Z"),)
    assert result[0].start.tzinfo is UTC


def test_half_open_boundaries_exclude_touching_intervals_and_keep_full_window():
    result = normalize_busy_intervals(
        [
            period("2026-11-12T13:00:00Z", "2026-11-12T14:00:00Z"),
            CalendarBusyInterval(start=WINDOW_START, end=WINDOW_END),
            period("2026-11-12T20:00:00Z", "2026-11-12T21:00:00Z"),
        ],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert result == (CalendarBusyInterval(start=WINDOW_START, end=WINDOW_END),)


@pytest.mark.parametrize(
    ("start", "end", "expected_start", "expected_end", "hours"),
    [
        (
            datetime.datetime(2026, 3, 8, tzinfo=ZoneInfo("America/New_York")),
            datetime.datetime(2026, 3, 9, tzinfo=ZoneInfo("America/New_York")),
            "2026-03-08T05:00:00Z",
            "2026-03-09T04:00:00Z",
            23,
        ),
        (
            datetime.datetime(2026, 11, 1, tzinfo=ZoneInfo("America/New_York")),
            datetime.datetime(2026, 11, 2, tzinfo=ZoneInfo("America/New_York")),
            "2026-11-01T04:00:00Z",
            "2026-11-02T05:00:00Z",
            25,
        ),
    ],
)
def test_dst_all_day_shapes_preserve_provider_instants(
    start,
    end,
    expected_start,
    expected_end,
    hours,
):
    result = normalize_busy_intervals(
        [CalendarBusyInterval(start=start, end=end)],
        window_start=instant(expected_start) - datetime.timedelta(hours=1),
        window_end=instant(expected_end) + datetime.timedelta(hours=1),
    )

    assert result == (period(expected_start, expected_end),)
    assert result[0].end - result[0].start == datetime.timedelta(hours=hours)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ExternalCalendarAuthorizationRequired(), "authorization_required"),
        (ExternalCalendarTimeout(), "timeout"),
        (ExternalCalendarUnavailable("provider_unavailable"), "provider_unavailable"),
        (ExternalCalendarInvalidResponse(), "invalid_response"),
    ],
)
def test_failures_have_safe_stable_reasons(error, reason):
    assert error.reason_code == reason
    assert str(error) == "External calendar availability could not be verified."


@pytest.mark.parametrize(
    ("source_error", "expected_type", "reason", "secret"),
    [
        (TimeoutError("private timeout details"), ExternalCalendarTimeout, "timeout", "private timeout details"),
        (
            RuntimeError("access_token=private-secret"),
            ExternalCalendarUnavailable,
            "provider_unavailable",
            "access_token=private-secret",
        ),
    ],
)
def test_raw_source_failures_are_sanitized(source_error, expected_type, reason, secret):
    class FailingSource:
        def query_busy_periods(self, **_kwargs):
            raise source_error

    with pytest.raises(expected_type) as raised:
        query(FailingSource())

    error = raised.value
    assert (error.reason_code, str(error)) == (
        reason,
        "External calendar availability could not be verified.",
    )
    assert error.__cause__ is None and error.__suppress_context__ is True
    assert secret not in formatted_exception(error)


def test_chained_domain_failure_is_canonicalized_without_provider_details():
    class FailingSource:
        def query_busy_periods(self, **_kwargs):
            try:
                raise RuntimeError("access_token=chained-private-secret")
            except RuntimeError as provider_error:
                raise ExternalCalendarAuthorizationRequired() from provider_error

    with pytest.raises(ExternalCalendarAuthorizationRequired) as raised:
        query(FailingSource())

    assert raised.value.reason_code == "authorization_required"
    assert raised.value.__cause__ is None and raised.value.__suppress_context__ is True
    assert "access_token=chained-private-secret" not in formatted_exception(raised.value)


@pytest.mark.parametrize(
    ("window_start", "window_end"),
    [
        (datetime.datetime(2026, 11, 12, 9), WINDOW_END),
        (WINDOW_START, datetime.datetime(2026, 11, 12, 17)),
        (WINDOW_START, WINDOW_START),
        (WINDOW_END, WINDOW_START),
    ],
)
def test_invalid_query_windows_are_rejected(window_start, window_end):
    with pytest.raises(ValueError):
        normalize_busy_intervals([], window_start=window_start, window_end=window_end)


def test_query_window_has_a_dst_safe_hard_limit_before_source_call():
    source = StaticSource(CalendarBusySnapshot(status="verified", periods=()))
    query(source, end=WINDOW_START + MAX_BUSY_QUERY_WINDOW)
    assert len(source.calls) == 1

    with pytest.raises(ValueError, match="supported horizon"):
        query(
            source,
            end=WINDOW_START + MAX_BUSY_QUERY_WINDOW + datetime.timedelta(microseconds=1),
        )
    assert len(source.calls) == 1


@pytest.mark.parametrize("instructor_id", ["", "   ", None])
def test_invalid_provider_scope_never_calls_source(instructor_id):
    source = StaticSource(CalendarBusySnapshot(status="verified", periods=()))
    with pytest.raises(ValueError, match="trusted instructor_id"):
        query(source, instructor_id=instructor_id)
    assert source.calls == []


@pytest.mark.parametrize(
    "invalid_period",
    [
        CalendarBusyInterval(start=datetime.datetime(2026, 11, 12, 9), end=WINDOW_END),
        CalendarBusyInterval(start=WINDOW_START, end=datetime.datetime(2026, 11, 12, 17)),
        CalendarBusyInterval(start=WINDOW_START, end=WINDOW_START),
        CalendarBusyInterval(start=WINDOW_END, end=WINDOW_START),
        {"start": "private event", "end": "private event"},
    ],
)
def test_invalid_periods_fail_closed_without_echoing_values(invalid_period):
    with pytest.raises(ExternalCalendarInvalidResponse) as raised:
        normalize_busy_intervals(
            [invalid_period],
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )

    assert raised.value.reason_code == "invalid_response"
    assert "private event" not in str(raised.value)


def test_malformed_or_excessive_period_iterables_fail_closed():
    valid = period("2026-11-12T15:00:00Z", "2026-11-12T16:00:00Z")
    invalid_iterables = [None, itertools.repeat(valid, MAX_BUSY_INTERVALS + 1)]

    for invalid in invalid_iterables:
        with pytest.raises(ExternalCalendarInvalidResponse):
            normalize_busy_intervals(
                invalid,
                window_start=WINDOW_START,
                window_end=WINDOW_END,
            )


def test_failing_period_iterator_is_sanitized_even_for_a_chained_domain_error():
    def fails_mid_read():
        yield period("2026-11-12T15:00:00Z", "2026-11-12T16:00:00Z")
        try:
            raise RuntimeError("access_token=iterator-private-secret")
        except RuntimeError as provider_error:
            raise ExternalCalendarInvalidResponse() from provider_error

    with pytest.raises(ExternalCalendarInvalidResponse) as raised:
        normalize_busy_intervals(
            fails_mid_read(),
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )

    assert raised.value.__cause__ is None and raised.value.__suppress_context__ is True
    assert "access_token=iterator-private-secret" not in formatted_exception(raised.value)


@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        CalendarBusySnapshot(status="unknown", periods=()),
        CalendarBusySnapshot(status="disconnected", periods=None),
        CalendarBusySnapshot(status="verified", periods=None),
        CalendarBusySnapshot(status="verified", periods=[]),
        CalendarBusySnapshot(
            status="disconnected",
            periods=(period("2026-11-12T15:00:00Z", "2026-11-12T16:00:00Z"),),
        ),
    ],
)
def test_invalid_source_snapshots_fail_closed(snapshot):
    with pytest.raises(ExternalCalendarInvalidResponse):
        query(StaticSource(snapshot))
