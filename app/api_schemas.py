from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: Literal["ok"]


class CoachContext(BaseModel):
    instructor_id: str
    display_name: str


class CoachLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class CoachAuthResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime
    coach: CoachContext


class CoachSessionResponse(BaseModel):
    authenticated: Literal[True] = True
    coach: CoachContext


class CoachLogoutResponse(BaseModel):
    success: bool
    message: str


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


class CoachInstructorProfileResponse(InstructorProfileResponse):
    business_name: str


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


class CoachAvailabilityWindow(BaseModel):
    id: str
    days: list[str]
    start_time: str
    end_time: str


class CoachClientResponse(BaseModel):
    id: str
    name: str
    phone: str = ""
    email: str = ""
    guardian_name: Optional[str] = None
    default_location: LocationResponse
    location_mode: Literal["client_home", "instructor_facility"]
    availability: list[CoachAvailabilityWindow] = Field(default_factory=list)
    lesson_duration_mins: int = Field(default=45, ge=15, le=240, multiple_of=15)
    notes: str = ""
    history_summary: str = ""
    progress_focus: str = ""


class CoachTimeSlotResponse(BaseModel):
    id: str
    start_time: datetime
    end_time: datetime
    travel_time_mins: int
    buffer_mins: int
    distance_miles: float
    target_location: LocationResponse
    previous_event_id: Optional[str] = None
    next_event_id: Optional[str] = None


class CoachScheduleItemResponse(BaseModel):
    id: str
    kind: Literal["lesson", "travel", "buffer", "break", "open_gap"]
    client_id: Optional[str] = None
    title: str
    start_time: datetime
    end_time: datetime
    location: LocationResponse
    travel_time_mins: Optional[int] = None
    buffer_mins: Optional[int] = None
    status: Optional[Literal["scheduled", "rescheduled", "canceled"]] = None
    note: Optional[str] = None


class CoachLessonRequestResponse(BaseModel):
    id: str
    client_id: str
    client_name: str
    requested_slot: CoachTimeSlotResponse
    source: Literal["client_invite", "manual_intake", "reschedule"]
    status: Literal["pending", "approved", "needs_alternatives", "declined"]
    risk: Literal["clear", "at_risk"]
    submitted_at: datetime
    note: str
    alternative_slots: list[CoachTimeSlotResponse] = Field(default_factory=list)


class CoachDistanceBufferTier(BaseModel):
    id: str
    label: str
    min_miles: float = Field(ge=0)
    max_miles: Optional[float] = Field(default=None, ge=0)
    buffer_mins: int = Field(ge=0, le=240)


class CoachSchedulerPayments(BaseModel):
    enabled_methods: list[Literal["card", "ach", "cash", "wallet"]] = Field(default_factory=list)
    cancellation_window_hours: int = Field(default=24, ge=0, le=168)
    require_payment_method_for_new_clients: bool = False


class CoachSchedulerRules(BaseModel):
    rounding_increment_mins: Literal[15] = 15
    post_break_buffer_mins: int = Field(default=15, ge=0, le=240)
    distance_buffer_tiers: list[CoachDistanceBufferTier]
    payments: CoachSchedulerPayments = Field(default_factory=CoachSchedulerPayments)


class CoachCommandCenterSnapshot(BaseModel):
    instructor: CoachInstructorProfileResponse
    date: date
    clients: list[CoachClientResponse]
    schedule: list[CoachScheduleItemResponse]
    requests: list[CoachLessonRequestResponse]
    rules: CoachSchedulerRules


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
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=160)


class BookLessonResponse(BaseModel):
    success: bool
    appointment_id: str
    message: str


class CoachLessonRequestActionResponse(BaseModel):
    id: str
    status: Literal["approved", "needs_alternatives", "declined"]
    appointment_id: Optional[str] = None
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
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=160)


class PublicLessonRequestResponse(BaseModel):
    request_token: str
    status: Literal["pending"]
    selected_slot: TimeSlotResponse
    message: str


class PublicRequestStatusResponse(BaseModel):
    request_token: str
    status: Literal["pending", "confirmed", "needs_new_times"]
    message: str


class ClientInviteLocation(BaseModel):
    address: str = Field(min_length=1, max_length=300)
    lat: Optional[float] = None
    lng: Optional[float] = None
    is_instructor_facility: bool = False


class ClientInviteIntake(BaseModel):
    client_name: str = Field(min_length=1, max_length=120)
    guardian_phone: str = ""
    guardian_email: str = ""
    notes: str = ""
    location_type: Literal["home", "facility"] = "home"
    location: ClientInviteLocation


class ClientInviteEnvelopeResponse(BaseModel):
    invite_token: str
    instructor_name: str
    business_name: str
    expires_at_iso: datetime
    intake: ClientInviteIntake
    preferred_duration_mins: int
    allowed_durations_mins: list[int]


class ClientInviteAvailabilityWindow(BaseModel):
    id: str
    days: list[str]
    start_time: str
    end_time: str


class ClientInviteAvailabilityRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    invite_token: Optional[str] = None
    instructor_id: Optional[str] = None
    client_id: Optional[str] = None
    client_name: str = Field(min_length=1, max_length=120)
    location: ClientInviteLocation
    requested_date: date
    duration_mins: int = Field(default=60, ge=15, le=240, multiple_of=15)
    recurrence: Literal["single", "weekly"] = "single"
    windows: list[ClientInviteAvailabilityWindow] = Field(default_factory=list)


class ClientInviteUnavailableWindow(BaseModel):
    id: str
    label: str
    reason: str


class ClientInviteSchedulerSlot(BaseModel):
    id: str
    start_iso: datetime
    end_iso: datetime
    display: str
    location_address: str
    travel_time_mins: int
    buffer_mins: int
    distance_miles: float
    previous_event: str
    next_event: Optional[str] = None
    safety_reason: str
    recurrence_available: bool


class ClientInviteAvailabilityResponse(BaseModel):
    status: Literal["PRESENTING", "FAILED"]
    dialogue_response: str
    slots: list[ClientInviteSchedulerSlot]
    unavailable_windows: list[ClientInviteUnavailableWindow]


class ClientInviteLessonRequestCreate(BaseModel):
    slot_id: str = Field(min_length=1, max_length=120)
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=160)


class ClientInviteCalendarLinks(BaseModel):
    google: str
    apple: str
    ics: str


class ClientInviteLessonConfirmation(BaseModel):
    appointment_id: str
    message: str
    confirmed_at_iso: datetime
    calendar_links: ClientInviteCalendarLinks


class ClientInviteLessonRequestResponse(BaseModel):
    id: str
    invite_token: str
    slot: ClientInviteSchedulerSlot
    status: Literal["requested", "confirmed", "needs_new_times"]
    requested_at_iso: datetime
    confirmation: Optional[ClientInviteLessonConfirmation] = None


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Optional[dict] = None


class ErrorResponse(BaseModel):
    error: ErrorBody
