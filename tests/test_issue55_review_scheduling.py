from __future__ import annotations

import datetime as dt

import pytest

from app import provider_settings, tools
from app.scheduler_settings import default_scheduling_policy, to_buffer_policy
from app.models import InstructorDB, ProviderWorkingHoursDB, TimeSlotDTO
from app.provider_settings import (
    ProviderConfiguration,
    ProviderLocation,
    ProviderWorkingHours,
)
from app.routing import BaseRoutingProvider, RouteEstimate


REQUESTED_DATE = dt.date(2026, 8, 20)
UTC = dt.timezone.utc


class ZeroRoutingProvider(BaseRoutingProvider):
    provider_name = "issue-55-zero-routing"

    def estimate_route(
        self,
        origin_coords,
        dest_coords,
        departure_time=None,
        traffic_mode="none",
    ) -> RouteEstimate:
        return RouteEstimate(
            distance_miles=0,
            travel_time_mins=0,
            duration_seconds=0,
            provider=self.provider_name,
        )

    def geocode(self, address: str) -> tuple[float, float]:
        return (30.0, -97.0)


def _provider_with_thursday_hours() -> ProviderConfiguration:
    location = ProviderLocation(address="Provider location", lat=30.0, lng=-97.0)
    hours = tuple(
        ProviderWorkingHours(
            weekday=weekday,
            enabled=weekday == "thursday",
            start_time="10:00" if weekday == "thursday" else None,
            end_time="17:00" if weekday == "thursday" else None,
        )
        for weekday in provider_settings.WEEKDAY_NAMES
    )
    return ProviderConfiguration(
        id="inst_review",
        display_name="Review Provider",
        business_name=None,
        timezone="UTC",
        base_location=location,
        service_location=location,
        service_name="Review service",
        default_duration_mins=60,
        working_hours=hours,
        setup_complete=True,
        setup_missing_fields=(),
    )


def _event(start_hour: int, start_minute: int, end_hour: int, end_minute: int) -> dict:
    return {
        "id": f"event-{start_hour}-{end_hour}",
        "start_time": dt.datetime(2026, 8, 20, start_hour, start_minute, tzinfo=UTC),
        "end_time": dt.datetime(2026, 8, 20, end_hour, end_minute, tzinfo=UTC),
        "lat": 30.0,
        "lng": -97.0,
        "is_break": False,
    }


def _find_slots(monkeypatch: pytest.MonkeyPatch, events: list[dict]) -> list[TimeSlotDTO]:
    monkeypatch.setattr(tools, "require_provider_setup", lambda _instructor_id: _provider_with_thursday_hours())
    monkeypatch.setattr(tools.db, "get_utc_appointment_events_for_date", lambda *_args, **_kwargs: events)
    monkeypatch.setattr(tools, "routing_provider", ZeroRoutingProvider())
    return tools.find_available_swim_slots(
        instructor_id="inst_review",
        date_str=REQUESTED_DATE.isoformat(),
        client_address="Provider location",
        is_instructor_facility=True,
        duration_mins=60,
        buffer_policy=to_buffer_policy(default_scheduling_policy()),
    )


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(_event(8, 0, 9, 0), id="before-opening"),
        pytest.param(_event(18, 0, 19, 0), id="after-closing"),
    ],
)
def test_events_outside_working_hours_cannot_expand_availability(
    monkeypatch: pytest.MonkeyPatch,
    event: dict,
) -> None:
    slots = _find_slots(monkeypatch, [event])

    assert slots
    assert slots[0].start_time == dt.datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    assert slots[-1].end_time == dt.datetime(2026, 8, 20, 17, 0, tzinfo=UTC)
    assert all(
        dt.datetime(2026, 8, 20, 10, 0, tzinfo=UTC) <= slot.start_time
        and slot.end_time <= dt.datetime(2026, 8, 20, 17, 0, tzinfo=UTC)
        for slot in slots
    )


def test_scheduler_keeps_outbound_routing_context_and_clips_available_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_events: list[dict] = []

    class LeakyEngine:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def find_available_slots_for_day(self, *, existing_events, **_kwargs):
            captured_events.extend(existing_events)
            return [
                TimeSlotDTO(
                    start_time=dt.datetime(2026, 8, 20, 9, 45, tzinfo=UTC),
                    end_time=dt.datetime(2026, 8, 20, 10, 45, tzinfo=UTC),
                    travel_time_mins=0,
                    buffer_mins=0,
                    distance_miles=0,
                ),
                TimeSlotDTO(
                    start_time=dt.datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
                    end_time=dt.datetime(2026, 8, 20, 13, 0, tzinfo=UTC),
                    travel_time_mins=0,
                    buffer_mins=0,
                    distance_miles=0,
                ),
                TimeSlotDTO(
                    start_time=dt.datetime(2026, 8, 20, 16, 30, tzinfo=UTC),
                    end_time=dt.datetime(2026, 8, 20, 17, 30, tzinfo=UTC),
                    travel_time_mins=0,
                    buffer_mins=0,
                    distance_miles=0,
                ),
            ]

    monkeypatch.setattr(tools, "SwimSchedulerConstraintEngine", LeakyEngine)
    slots = _find_slots(
        monkeypatch,
        [
            _event(8, 0, 9, 0),
            _event(9, 30, 10, 30),
            _event(16, 30, 18, 0),
            _event(18, 0, 19, 0),
        ],
    )

    assert [
        (event["start_time"], event["end_time"])
        for event in captured_events
    ] == [
        (
            dt.datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
            dt.datetime(2026, 8, 20, 10, 30, tzinfo=UTC),
        ),
        (
            dt.datetime(2026, 8, 20, 16, 30, tzinfo=UTC),
            dt.datetime(2026, 8, 20, 18, 0, tzinfo=UTC),
        ),
        (
            dt.datetime(2026, 8, 20, 18, 0, tzinfo=UTC),
            dt.datetime(2026, 8, 20, 19, 0, tzinfo=UTC),
        ),
    ]
    assert [(slot.start_time, slot.end_time) for slot in slots] == [
        (
            dt.datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
            dt.datetime(2026, 8, 20, 13, 0, tzinfo=UTC),
        )
    ]


def test_all_seven_disabled_days_are_complete_and_return_no_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instructor = InstructorDB(
        id="inst_disabled_week",
        name="Disabled Week Provider",
        main_address="Public service",
        lat=30.0,
        lng=-97.0,
        timezone="UTC",
        base_address="Private base",
        base_lat=30.0,
        base_lng=-97.0,
        service_address="Public service",
        service_lat=30.0,
        service_lng=-97.0,
        service_name="Disabled-week service",
        default_duration_mins=60,
    )
    rows = [
        ProviderWorkingHoursDB(
            instructor_id=instructor.id,
            weekday=weekday,
            enabled=False,
            start_time=None,
            end_time=None,
        )
        for weekday in range(7)
    ]
    configuration = provider_settings._configuration_from_rows(instructor, rows)

    assert configuration.setup_complete is True
    assert configuration.setup_missing_fields == ()

    monkeypatch.setattr(tools, "require_provider_setup", lambda _instructor_id: configuration)
    for offset in range(7):
        requested_date = dt.date(2026, 8, 17) + dt.timedelta(days=offset)
        assert tools.find_available_swim_slots(
            instructor_id=instructor.id,
            date_str=requested_date.isoformat(),
            client_address="Public service",
            is_instructor_facility=True,
        ) == []
