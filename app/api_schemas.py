from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: Literal["ok"]


class CoachContext(BaseModel):
    instructor_id: str
    display_name: str


class LocationResponse(BaseModel):
    address: str
    lat: float
    lng: float
    is_instructor_facility: bool = False


class InstructorProfileResponse(BaseModel):
    id: str
    name: str
    main_address: str
    lat: float
    lng: float
    work_start_time: str
    work_end_time: str


class AppointmentResponse(BaseModel):
    id: str
    title: str
    start_time: datetime
    end_time: datetime
    location_address: str
    lat: float
    lng: float
    is_break: bool


class CoachScheduleResponse(BaseModel):
    instructor: InstructorProfileResponse
    date: date
    appointments: list[AppointmentResponse]


class AvailabilitySearchRequest(BaseModel):
    client_name: str = Field(min_length=1, max_length=120)
    client_address: str = Field(min_length=1, max_length=300)
    requested_date: date
    is_instructor_facility: bool = False
    duration_mins: int = Field(default=60, ge=15, le=240, multiple_of=15)


class TimeSlotResponse(BaseModel):
    start_iso: datetime
    end_iso: datetime
    display: str
    travel_time_mins: int
    buffer_mins: int
    distance_miles: float


class AvailabilitySearchResponse(BaseModel):
    status: Literal["PRESENTING"]
    client_name: str
    requested_date: date
    duration_mins: int
    location: LocationResponse
    available_slots: list[TimeSlotResponse]


class CoachBookLessonRequest(BaseModel):
    client_name: str = Field(min_length=1, max_length=120)
    client_address: str = Field(min_length=1, max_length=300)
    start_time: datetime
    duration_mins: int = Field(default=60, ge=15, le=240, multiple_of=15)
    is_instructor_facility: bool = False


class BookLessonResponse(BaseModel):
    success: bool
    appointment_id: str
    message: str


class PublicInviteResponse(BaseModel):
    invite_token: str
    instructor_name: str
    business_name: str
    location_label: str
    preferred_duration_mins: int
    allowed_durations_mins: list[int]
    expires_at: datetime


class PublicAvailabilitySearchRequest(BaseModel):
    client_name: str = Field(min_length=1, max_length=120)
    client_address: str = Field(min_length=1, max_length=300)
    requested_date: date
    location_type: Literal["home", "facility"] = "home"
    duration_mins: int = Field(default=60, ge=15, le=240, multiple_of=15)


class PublicAvailabilitySearchResponse(BaseModel):
    status: Literal["PRESENTING"]
    requested_date: date
    duration_mins: int
    location_label: str
    available_slots: list[TimeSlotResponse]


class PublicLessonRequestCreate(BaseModel):
    client_name: str = Field(min_length=1, max_length=120)
    client_address: str = Field(min_length=1, max_length=300)
    requested_date: date
    slot_start: datetime
    duration_mins: int = Field(default=60, ge=15, le=240, multiple_of=15)
    location_type: Literal["home", "facility"] = "home"


class PublicLessonRequestResponse(BaseModel):
    request_token: str
    status: Literal["pending"]
    selected_slot: TimeSlotResponse
    message: str


class PublicRequestStatusResponse(BaseModel):
    request_token: str
    status: Literal["pending"]
    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Optional[dict] = None


class ErrorResponse(BaseModel):
    error: ErrorBody
