import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.api_schemas import CoachSchedulerRules
from app.engine import BufferPolicy, DistanceBufferTier
from app.scheduler_settings import default_scheduler_rules, to_buffer_policy


def _policy(**overrides) -> BufferPolicy:
    values = {
        "buffers_enabled": True,
        "apply_distance_buffer_at_shift_start": False,
        "rounding_increment_mins": 15,
        "post_break_buffer_mins": 15,
        "distance_buffer_tiers": (
            DistanceBufferTier(0, 4.999, 10),
            DistanceBufferTier(5, 10, 20),
            DistanceBufferTier(10.001, None, 30),
        ),
    }
    values.update(overrides)
    return BufferPolicy(**values)


@pytest.mark.parametrize(
    ("distance", "expected"),
    [(4.99, 10), (4.999, 20), (5.0, 20), (10.0, 20), (10.01, 30), (500_000, 30)],
)
def test_distance_lookup_normalizes_to_the_hundredth(distance, expected):
    assert _policy().distance_buffer_minutes(distance) == expected


@pytest.mark.parametrize("distance", [-1, float("nan"), float("inf")])
def test_distance_lookup_rejects_invalid_routing_values(distance):
    with pytest.raises(ValueError):
        _policy().distance_buffer_minutes(distance)


def test_disabled_buffers_and_shift_start_precedence():
    assert _policy(buffers_enabled=False).inbound_buffer_minutes(
        7, is_after_break=True, is_shift_start=False
    ) == 0
    assert _policy().inbound_buffer_minutes(7, is_after_break=False, is_shift_start=True) == 0
    assert _policy(apply_distance_buffer_at_shift_start=True).inbound_buffer_minutes(
        7, is_after_break=False, is_shift_start=True
    ) == 20
    assert _policy(post_break_buffer_mins=37).inbound_buffer_minutes(
        7, is_after_break=True, is_shift_start=False
    ) == 37


def test_default_policy_matches_canonical_settings():
    assert to_buffer_policy(default_scheduler_rules()) == BufferPolicy.default()


def test_rounding_skips_nonexistent_spring_forward_label():
    zone = ZoneInfo("America/New_York")
    source = datetime.datetime(2026, 3, 8, 1, 58, tzinfo=zone)
    rounded = _policy().ceil_datetime(source, "America/New_York")
    assert rounded == datetime.datetime(2026, 3, 8, 3, 0, tzinfo=zone)


def test_rounding_preserves_fold_and_does_not_revisit_lower_fall_back_label():
    zone = ZoneInfo("America/New_York")
    exact_second_fold = datetime.datetime(2026, 11, 1, 1, 0, tzinfo=zone, fold=1)
    assert _policy().ceil_datetime(exact_second_fold, "America/New_York").fold == 1

    first_fold = datetime.datetime(2026, 11, 1, 1, 58, tzinfo=zone, fold=0)
    rounded = _policy().ceil_datetime(first_fold, "America/New_York")
    assert rounded.hour == 2
    assert rounded.minute == 0
    assert rounded.utcoffset() == datetime.timedelta(hours=-5)


def test_non_hour_offset_timezone_rounding():
    zone = ZoneInfo("Asia/Kathmandu")
    source = datetime.datetime(2026, 1, 10, 9, 6, tzinfo=zone)
    assert _policy(rounding_increment_mins=10).ceil_datetime(source, "Asia/Kathmandu") == datetime.datetime(
        2026, 1, 10, 9, 10, tzinfo=zone
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(rounding_increment_mins=7),
        lambda value: value.update(distance_buffer_tiers=[]),
        lambda value: value["distance_buffer_tiers"][0].update(min_miles=1),
        lambda value: value["distance_buffer_tiers"][0].update(max_miles=4.98),
        lambda value: value["distance_buffer_tiers"][1].update(id="near"),
        lambda value: value["distance_buffer_tiers"][-1].update(max_miles=100),
    ],
)
def test_scheduler_rules_reject_unsupported_or_incomplete_policies(mutation):
    payload = default_scheduler_rules().model_dump(mode="json")
    mutation(payload)
    with pytest.raises(ValidationError):
        CoachSchedulerRules.model_validate(payload)
