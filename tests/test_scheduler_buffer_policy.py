import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.api_schemas import CoachSchedulerRules
from app.engine import BufferPolicy, DistanceBufferTier
from app.scheduler_settings import (
    default_scheduler_rules,
    default_scheduling_policy,
    to_buffer_policy,
)


def _policy(**overrides) -> BufferPolicy:
    values = {
        "distance_buffers_enabled": True,
        "post_break_recovery_mins": 15,
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


def test_disabled_distance_buffers_preserve_recovery_and_shift_start_precedence():
    assert _policy(distance_buffers_enabled=False).inbound_buffer_minutes(
        7, is_after_break=True, is_shift_start=False
    ) == 15
    assert _policy().inbound_buffer_minutes(7, is_after_break=False, is_shift_start=True) == 0
    assert _policy(post_break_recovery_mins=37).inbound_buffer_minutes(
        7, is_after_break=True, is_shift_start=False
    ) == 37
    assert _policy(distance_buffers_enabled=False).inbound_buffer_minutes(
        7, is_after_break=False, is_shift_start=False
    ) == 0


def test_default_policy_matches_canonical_settings():
    assert to_buffer_policy(default_scheduling_policy()) == _policy()


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
    assert _policy().ceil_datetime(source, "Asia/Kathmandu") == datetime.datetime(
        2026, 1, 10, 9, 15, tzinfo=zone
    )


def _eleven_contiguous_tiers() -> list[dict]:
    return [
        {
            "id": f"tier-{index}",
            "label": f"Tier {index}",
            "min_miles": float(index),
            "max_miles": index + 0.99 if index < 10 else None,
            "buffer_mins": 10,
        }
        for index in range(11)
    ]


@pytest.mark.parametrize(
    ("case", "mutation"),
    [
        pytest.param(
            "unsupported rounding increment",
            lambda value: value.update(rounding_increment_mins=7),
            id="unsupported-rounding-increment",
        ),
        pytest.param(
            "no tiers",
            lambda value: value.update(distance_buffer_tiers=[]),
            id="no-tiers",
        ),
        pytest.param(
            "over ten tiers",
            lambda value: value.update(distance_buffer_tiers=_eleven_contiguous_tiers()),
            id="over-ten-tiers",
        ),
        pytest.param(
            "blank tier id",
            lambda value: value["distance_buffer_tiers"][0].update(id="   "),
            id="blank-tier-id",
        ),
        pytest.param(
            "invalid tier id",
            lambda value: value["distance_buffer_tiers"][0].update(id="1-Near!"),
            id="invalid-tier-id",
        ),
        pytest.param(
            "blank tier label",
            lambda value: value["distance_buffer_tiers"][0].update(label="   "),
            id="blank-tier-label",
        ),
        pytest.param(
            "invalid tier label",
            lambda value: value["distance_buffer_tiers"][0].update(label="x" * 121),
            id="invalid-tier-label",
        ),
        pytest.param(
            "non-finite minimum",
            lambda value: value["distance_buffer_tiers"][0].update(min_miles=float("nan")),
            id="non-finite-minimum",
        ),
        pytest.param(
            "non-finite maximum",
            lambda value: value["distance_buffer_tiers"][0].update(max_miles=float("inf")),
            id="non-finite-maximum",
        ),
        pytest.param(
            "minimum below range",
            lambda value: value["distance_buffer_tiers"][0].update(min_miles=-0.01),
            id="minimum-below-range",
        ),
        pytest.param(
            "minimum above range",
            lambda value: value["distance_buffer_tiers"][0].update(min_miles=10_000.01),
            id="minimum-above-range",
        ),
        pytest.param(
            "maximum below range",
            lambda value: value["distance_buffer_tiers"][0].update(max_miles=-0.01),
            id="maximum-below-range",
        ),
        pytest.param(
            "maximum above range",
            lambda value: value["distance_buffer_tiers"][0].update(max_miles=10_000.01),
            id="maximum-above-range",
        ),
        pytest.param(
            "maximum below minimum",
            lambda value: value["distance_buffer_tiers"][1].update(max_miles=4.99),
            id="maximum-below-minimum",
        ),
        pytest.param(
            "first tier does not start at zero",
            lambda value: value["distance_buffer_tiers"][0].update(min_miles=1),
            id="first-tier-not-zero",
        ),
        pytest.param(
            "coverage gap",
            lambda value: value["distance_buffer_tiers"][0].update(max_miles=4.98),
            id="coverage-gap",
        ),
        pytest.param(
            "overlapping coverage",
            lambda value: value["distance_buffer_tiers"][0].update(max_miles=5.001),
            id="overlapping-coverage",
        ),
        pytest.param(
            "duplicate tier id",
            lambda value: value["distance_buffer_tiers"][1].update(id="near"),
            id="duplicate-tier-id",
        ),
        pytest.param(
            "interior tier is unbounded",
            lambda value: value["distance_buffer_tiers"][1].update(max_miles=None),
            id="interior-unbounded-tier",
        ),
        pytest.param(
            "last tier is bounded",
            lambda value: value["distance_buffer_tiers"][-1].update(max_miles=100),
            id="bounded-last-tier",
        ),
        pytest.param(
            "buffer below minute boundary",
            lambda value: value["distance_buffer_tiers"][0].update(buffer_mins=-1),
            id="buffer-minutes-below-boundary",
        ),
        pytest.param(
            "buffer above minute boundary",
            lambda value: value["distance_buffer_tiers"][0].update(buffer_mins=241),
            id="buffer-minutes-above-boundary",
        ),
    ],
)
def test_scheduler_rules_reject_invalid_policy_matrix(case, mutation):
    payload = default_scheduler_rules().model_dump(mode="json")
    mutation(payload)
    with pytest.raises(ValidationError) as exc_info:
        CoachSchedulerRules.model_validate(payload)
    assert exc_info.value.errors(), case


@pytest.mark.parametrize("buffer_mins", [0, 240], ids=["minimum", "maximum"])
def test_scheduler_rules_accept_tier_buffer_minute_boundaries(buffer_mins):
    payload = default_scheduler_rules().model_dump(mode="json")
    payload["distance_buffer_tiers"][0]["buffer_mins"] = buffer_mins

    rules = CoachSchedulerRules.model_validate(payload)

    assert rules.distance_buffer_tiers[0].buffer_mins == buffer_mins
