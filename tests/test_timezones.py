import datetime

import pytest

from app import timezones


def test_local_day_window_uses_instructor_timezone_and_dst_boundary():
    start, end = timezones.local_day_window_utc_storage(
        datetime.date(2026, 3, 8),
        "America/Chicago",
    )

    assert start == datetime.datetime(2026, 3, 8, 6, 0)
    assert end == datetime.datetime(2026, 3, 9, 5, 0)


def test_nonexistent_spring_forward_local_time_is_rejected():
    with pytest.raises(timezones.NonexistentLocalTimeError):
        timezones.validate_local_datetime(
            datetime.datetime(2026, 3, 8, 2, 30),
            "America/Chicago",
        )


def test_ambiguous_fall_back_naive_local_time_is_rejected():
    with pytest.raises(timezones.AmbiguousLocalTimeError):
        timezones.validate_local_datetime(
            datetime.datetime(2026, 11, 1, 1, 30),
            "America/Chicago",
        )


def test_aware_fall_back_offsets_are_preserved_as_distinct_instants():
    early = datetime.datetime.fromisoformat("2026-11-01T01:30:00-05:00")
    late = datetime.datetime.fromisoformat("2026-11-01T01:30:00-06:00")

    assert timezones.to_utc_storage(early) == datetime.datetime(2026, 11, 1, 6, 30)
    assert timezones.to_utc_storage(late) == datetime.datetime(2026, 11, 1, 7, 30)
