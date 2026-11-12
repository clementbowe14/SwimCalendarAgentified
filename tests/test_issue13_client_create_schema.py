from __future__ import annotations

from copy import deepcopy

from pydantic import ValidationError
import pytest

from app.api_schemas import (
    CoachClientAvailabilityWindowMutation,
    CoachClientLocationMutation,
    CoachClientMutation,
)


def _payload(**overrides) -> dict:
    payload = {
        "name": "Maya Chen",
        "phone": "+1 (512) 555-0100",
        "email": None,
        "notes": "Gate code 2468",
        "location_mode": "client_location",
        "client_location": {"address": "901 Home Rd, Austin, TX"},
        "default_duration_mins": 45,
        "availability": [
            {
                "days": ["wednesday", "monday"],
                "start_time": "08:00",
                "end_time": "10:00",
            },
            {
                "days": ["saturday"],
                "start_time": "12:00",
                "end_time": "14:00",
            },
        ],
    }
    payload.update(overrides)
    return payload


def _assert_invalid(payload: dict) -> None:
    with pytest.raises(ValidationError):
        CoachClientMutation.model_validate(payload)


def test_client_create_json_schema_is_strict_and_server_fields_are_absent():
    schema = CoachClientMutation.model_json_schema()
    fields = {
        "name",
        "phone",
        "email",
        "notes",
        "location_mode",
        "client_location",
        "default_duration_mins",
        "availability",
    }
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == fields
    assert set(schema["required"]) == fields - {"client_location"}
    assert schema["properties"]["location_mode"]["enum"] == [
        "client_location",
        "service_location",
    ]
    assert schema["properties"]["name"]["minLength"] == 1
    assert schema["properties"]["name"]["maxLength"] == 120
    assert schema["properties"]["phone"]["anyOf"][0]["maxLength"] == 64
    assert schema["properties"]["email"]["anyOf"][0]["maxLength"] == 254
    assert schema["properties"]["notes"]["maxLength"] == 4000
    assert schema["properties"]["default_duration_mins"] == {
        "maximum": 240,
        "minimum": 15,
        "multipleOf": 15,
        "title": "Default Duration Mins",
        "type": "integer",
    }
    assert schema["properties"]["availability"]["maxItems"] == 64

    location_schema = CoachClientLocationMutation.model_json_schema()
    assert location_schema["additionalProperties"] is False
    assert set(location_schema["properties"]) == {"address"}
    assert location_schema["required"] == ["address"]
    assert location_schema["properties"]["address"]["minLength"] == 1
    assert location_schema["properties"]["address"]["maxLength"] == 300

    window_schema = CoachClientAvailabilityWindowMutation.model_json_schema()
    assert window_schema["additionalProperties"] is False
    assert set(window_schema["required"]) == {"days", "start_time", "end_time"}
    assert window_schema["properties"]["days"]["minItems"] == 1
    assert window_schema["properties"]["days"]["maxItems"] == 7
    for field in ("start_time", "end_time"):
        assert window_schema["properties"][field]["pattern"] == (
            r"^ *(?:[01]\d|2[0-3]):[0-5]\d *$"
        )


def test_valid_profile_normalizes_text_contacts_days_and_times():
    profile = CoachClientMutation.model_validate(
        _payload(
            name="  Maya Chen  ",
            phone="  +1 (512) 555-0100 ext. 42  ",
            email="  maya@example.com  ",
            notes="  Gate code 2468  ",
            client_location={"address": "  901 Home Rd, Austin, TX  "},
            availability=[
                {
                    "days": ["sunday", "monday", "friday"],
                    "start_time": " 08:00 ",
                    "end_time": " 10:00 ",
                }
            ],
        )
    )

    assert profile.name == "Maya Chen"
    assert profile.phone == "+1 (512) 555-0100 ext. 42"
    assert str(profile.email) == "maya@example.com"
    assert profile.notes == "Gate code 2468"
    assert profile.client_location.address == "901 Home Rd, Austin, TX"
    assert profile.availability[0].days == ["monday", "friday", "sunday"]
    assert profile.availability[0].start_time == "08:00"
    assert profile.availability[0].end_time == "10:00"


def test_service_location_may_omit_client_location_and_empty_contacts_normalize():
    profile = CoachClientMutation.model_validate(
        _payload(
            phone="   ",
            email="provider@example.com",
            location_mode="service_location",
            availability=[],
        )
        | {"client_location": None}
    )
    assert profile.phone is None
    assert profile.client_location is None
    assert profile.availability == []

    omitted = _payload(
        phone="+15125550100 # 7",
        email="   ",
        location_mode="service_location",
        availability=[],
    )
    omitted.pop("client_location")
    omitted_profile = CoachClientMutation.model_validate(omitted)
    assert omitted_profile.email is None
    assert omitted_profile.phone == "+15125550100 # 7"
    assert omitted_profile.client_location is None

    retained = CoachClientMutation.model_validate(
        _payload(
            location_mode="service_location",
            client_location={"address": " 500 Saved Pool Rd "},
            availability=[],
        )
    )
    assert retained.client_location.address == "500 Saved Pool Rd"


@pytest.mark.parametrize(
    "phone",
    [
        "5550100",
        "5125550100 x7",
        "5125550100 ext 7",
        "5125550100 ext. 7",
        "5125550100 # 7",
        "12345678901234567890",
    ],
)
def test_valid_phone_forms_and_digit_boundaries_are_accepted(phone: str):
    profile = CoachClientMutation.model_validate(_payload(phone=phone, email=None))
    assert profile.phone == phone


@pytest.mark.parametrize(
    "phone",
    [
        "call me",
        "x1234567",
        "+++++++5125550100",
        "#######5125550100",
        "(512 555-0100",
        "512) 555-0100",
        "123()4567",
        "512--555--0100",
        "5125550100\next 7",
        "5125550100\text 7",
        "5125550100\u2028ext 7",
        "123456",
        "123456789012345678901",
        "+1 (512) 555-0100 ext. 12345678901",
    ],
)
def test_invalid_phone_forms_are_rejected(phone: str):
    _assert_invalid(_payload(phone=phone, email=None))


@pytest.mark.parametrize(
    "payload",
    [
        _payload(phone=None, email=None),
        _payload(phone=None, email="invalid-email"),
        _payload(name="   "),
        _payload(name="Maya\nChen"),
        _payload(name="Maya\u202eChen"),
        _payload(name="Maya\u200bChen"),
        _payload(name="Maya\ud800Chen"),
        _payload(notes="Private\u2029notes"),
        _payload(client_location=None),
        _payload(client_location={"address": "   "}),
        _payload(client_location={"address": "901 Home Rd", "lat": 30.1}),
        _payload(client_location={"address": "901\u2060 Home Rd"}),
        _payload(default_duration_mins=14),
        _payload(default_duration_mins=50),
        _payload(default_duration_mins=255),
        _payload(default_duration_mins=45.0),
        _payload(unknown_private_field="not allowed"),
    ],
)
def test_invalid_profile_fields_are_rejected(payload: dict):
    _assert_invalid(payload)


@pytest.mark.parametrize(
    "availability",
    [
        [{"days": [], "start_time": "08:00", "end_time": "10:00"}],
        [
            {
                "days": ["monday", "monday"],
                "start_time": "08:00",
                "end_time": "10:00",
            }
        ],
        [{"days": ["funday"], "start_time": "08:00", "end_time": "10:00"}],
        [{"days": ["monday"], "start_time": "8:00", "end_time": "10:00"}],
        [{"days": ["monday"], "start_time": "\t08:00", "end_time": "10:00"}],
        [{"days": ["monday"], "start_time": "08:00", "end_time": "10:00\n"}],
        [{"days": ["monday"], "start_time": "24:00", "end_time": "25:00"}],
        [{"days": ["monday"], "start_time": "10:00", "end_time": "10:00"}],
        [{"days": ["monday"], "start_time": "12:00", "end_time": "10:00"}],
        [
            {"days": ["monday"], "start_time": "08:00", "end_time": "10:00"},
            {"days": ["monday"], "start_time": "09:00", "end_time": "11:00"},
        ],
        [
            {
                "days": ["monday", "wednesday"],
                "start_time": "08:00",
                "end_time": "10:00",
            },
            {
                "days": ["wednesday", "friday"],
                "start_time": "09:00",
                "end_time": "11:00",
            },
        ],
        [
            {
                "days": ["monday"],
                "start_time": "08:00",
                "end_time": "10:00",
                "timezone": "America/New_York",
            }
        ],
    ],
)
def test_invalid_availability_is_rejected(availability: list[dict]):
    _assert_invalid(_payload(availability=availability))


def test_adjacent_or_different_day_windows_do_not_overlap():
    availability = [
        {"days": ["monday"], "start_time": "08:00", "end_time": "10:00"},
        {"days": ["monday"], "start_time": "10:00", "end_time": "12:00"},
        {"days": ["tuesday"], "start_time": "08:00", "end_time": "10:00"},
    ]
    profile = CoachClientMutation.model_validate(_payload(availability=availability))
    assert len(profile.availability) == 3


def test_availability_collection_is_limited_to_64_windows():
    def one_minute_window(index: int) -> dict:
        start_hour, start_minute = divmod(index, 60)
        end_hour, end_minute = divmod(index + 1, 60)
        return {
            "days": ["monday"],
            "start_time": f"{start_hour:02d}:{start_minute:02d}",
            "end_time": f"{end_hour:02d}:{end_minute:02d}",
        }

    windows = [one_minute_window(index) for index in range(65)]
    accepted = CoachClientMutation.model_validate(
        _payload(availability=deepcopy(windows[:64]))
    )
    assert len(accepted.availability) == 64
    reverse_ordered = CoachClientMutation.model_validate(
        _payload(availability=list(reversed(deepcopy(windows[:64]))))
    )
    assert len(reverse_ordered.availability) == 64
    _assert_invalid(_payload(availability=windows))
