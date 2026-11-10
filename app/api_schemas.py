from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math
import re
from typing import Literal, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app import timezones


class HealthResponse(BaseModel):
    status: Literal["ok"]


class RoutingReadinessResponse(BaseModel):
    provider: Literal["haversine", "mapbox"]
    mode: Literal["synthetic", "external"]
    configured: Literal[True]


class ReadinessResponse(BaseModel):
    status: Literal["ready"]
    routing: RoutingReadinessResponse


class CoachContext(BaseModel):
    instructor_id: str
    display_name: str


class CoachLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class CoachAuthResponse(BaseModel):
    authenticated: Literal[True] = True
    expires_at: datetime
    coach: CoachContext


class CoachSessionResponse(BaseModel):
    authenticated: Literal[True] = True
    expires_at: datetime
    coach: CoachContext


class CoachLogoutResponse(BaseModel):
    success: bool
    message: str


class LocationResponse(BaseModel):
    address: str
    lat: float
    lng: float
    is_instructor_facility: bool = False


ProviderWeekday = Literal[
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]
PROVIDER_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


class CoachProviderLocationResponse(BaseModel):
    address: str
    lat: float
    lng: float


class CoachProviderWorkingHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weekday: ProviderWeekday
    enabled: bool
    start_time: Optional[str] = None
    end_time: Optional[str] = None

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def normalize_time(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value.strip()):
            raise ValueError("Time must use 24-hour HH:MM format.")
        return value.strip()

    @model_validator(mode="after")
    def validate_interval(self) -> "CoachProviderWorkingHours":
        if not self.enabled:
            if self.start_time is not None or self.end_time is not None:
                raise ValueError("Disabled weekdays cannot include opening or closing times.")
            return self
        if self.start_time is None or self.end_time is None:
            raise ValueError("Enabled weekdays require opening and closing times.")
        if self.end_time <= self.start_time:
            raise ValueError("Closing time must be later than opening time.")
        return self


class CoachProviderServiceResponse(BaseModel):
    name: Optional[str] = None
    default_duration_mins: Optional[int] = None
    location: Optional[CoachProviderLocationResponse] = None


class CoachProviderSettingsResponse(BaseModel):
    id: str
    display_name: str
    business_name: Optional[str] = None
    timezone: str
    base_location: Optional[CoachProviderLocationResponse] = None
    service: CoachProviderServiceResponse
    working_hours: list[CoachProviderWorkingHours]
    setup_complete: bool
    setup_missing_fields: list[str]


class CoachProviderSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=160)
    business_name: Optional[str] = Field(default=None, max_length=160)
    timezone: str = Field(min_length=1, max_length=120)
    base_address: str = Field(min_length=1, max_length=300)
    service_address: str = Field(min_length=1, max_length=300)
    service_name: str = Field(min_length=1, max_length=160)
    default_duration_mins: int = Field(ge=15, le=240, multiple_of=15)
    working_hours: list[CoachProviderWorkingHours] = Field(min_length=7, max_length=7)

    @field_validator(
        "display_name",
        "timezone",
        "base_address",
        "service_address",
        "service_name",
        mode="before",
    )
    @classmethod
    def trim_required_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("business_name", mode="before")
    @classmethod
    def normalize_optional_business_name(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        return normalized or None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            timezones.get_zone(value)
        except timezones.InvalidTimezoneError as exc:
            raise ValueError("Timezone must be a valid IANA identifier.") from exc
        return value

    @field_validator("working_hours")
    @classmethod
    def validate_complete_week(
        cls,
        value: list[CoachProviderWorkingHours],
    ) -> list[CoachProviderWorkingHours]:
        weekdays = [row.weekday for row in value]
        if len(set(weekdays)) != len(weekdays):
            raise ValueError("Each weekday may appear only once.")
        if set(weekdays) != set(PROVIDER_WEEKDAYS):
            raise ValueError("Working hours must include Monday through Sunday.")
        return sorted(value, key=lambda row: PROVIDER_WEEKDAYS.index(row.weekday))


class InstructorProfileResponse(BaseModel):
    id: str
    name: str
    main_address: str
    lat: float
    lng: float
    work_start_time: str
    work_end_time: str
    timezone: str


class CoachInstructorProfileResponse(InstructorProfileResponse):
    business_name: str
    base_location: Optional[CoachProviderLocationResponse] = None
    service: CoachProviderServiceResponse
    working_hours: list[CoachProviderWorkingHours]
    setup_complete: bool
    setup_missing_fields: list[str]


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
    status: Literal["pending", "approved", "needs_alternatives", "declined", "expired"]
    risk: Literal["clear", "at_risk"]
    submitted_at: datetime
    note: str
    alternative_slots: list[CoachTimeSlotResponse] = Field(default_factory=list)


class CoachEmailDeliveryResponse(BaseModel):
    id: str
    kind: Literal["invite", "confirmation"]
    status: Literal["queued", "processing", "accepted", "delivered", "failed", "skipped"]
    updated_at: AwareDatetime
    client_id: Optional[str] = None
    client_name: str
    request_token: Optional[str] = None
    issue: Optional[
        Literal[
            "invalid_address",
            "missing_address",
            "temporary_failure",
            "permanent_failure",
            "delivery_uncertain",
            "retries_exhausted",
        ]
    ] = None
    source_state: Literal["active", "invite_inactive", "appointment_inactive"]
    can_resend: bool
    can_edit_contact: bool

    @field_validator("updated_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)


class CoachEmailContactUpdateRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class CoachDistanceBufferTier(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    min_miles: float = Field(ge=0, le=10_000, allow_inf_nan=False, strict=True)
    max_miles: Optional[float] = Field(
        default=None,
        ge=0,
        le=10_000,
        allow_inf_nan=False,
        strict=True,
    )
    buffer_mins: int = Field(ge=0, le=240, strict=True)

    @field_validator("id", "label", mode="before")
    @classmethod
    def trim_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
            raise ValueError("Tier id must start with a lowercase letter and contain only lowercase letters, numbers, '_' or '-'.")
        return value

    @model_validator(mode="after")
    def validate_bounds(self) -> "CoachDistanceBufferTier":
        if not math.isfinite(self.min_miles) or (
            self.max_miles is not None and not math.isfinite(self.max_miles)
        ):
            raise ValueError("Tier bounds must be finite numbers.")
        if self.max_miles is not None and self.max_miles < self.min_miles:
            raise ValueError("Tier max_miles must be greater than or equal to min_miles.")
        return self


def _validate_complete_distance_coverage(
    tiers: list[CoachDistanceBufferTier],
) -> None:
    ids = [tier.id for tier in tiers]
    if len(ids) != len(set(ids)):
        raise ValueError("Distance buffer tier ids must be unique.")
    if Decimal(str(tiers[0].min_miles)) != Decimal("0"):
        raise ValueError("The first distance buffer tier must start at 0 miles.")

    for index, tier in enumerate(tiers):
        is_last = index == len(tiers) - 1
        if is_last and tier.max_miles is not None:
            raise ValueError("The last distance buffer tier must have max_miles set to null.")
        if not is_last and tier.max_miles is None:
            raise ValueError("Only the last distance buffer tier may have max_miles set to null.")
        if not is_last:
            current_max = Decimal(str(tier.max_miles))
            next_min = Decimal(str(tiers[index + 1].min_miles))
            if next_min <= current_max:
                raise ValueError("Distance buffer tiers must not overlap.")
            last_hundredth = int(
                (current_max * 100).to_integral_value(rounding=ROUND_FLOOR)
            )
            next_hundredth = int(
                (next_min * 100).to_integral_value(rounding=ROUND_CEILING)
            )
            if last_hundredth + 1 != next_hundredth:
                raise ValueError(
                    "Distance buffer tiers must cover every hundredth of a mile exactly once."
                )


class CoachSchedulerPayments(BaseModel):
    enabled_methods: list[Literal["card", "ach", "cash", "wallet"]] = Field(default_factory=list)
    cancellation_window_hours: int = Field(default=24, ge=0, le=168)
    require_payment_method_for_new_clients: bool = False


class CoachSchedulerRules(BaseModel):
    buffers_enabled: bool = True
    apply_distance_buffer_at_shift_start: bool = False
    rounding_increment_mins: Literal[5, 10, 15, 30] = 15
    post_break_buffer_mins: int = Field(default=15, ge=0, le=240)
    distance_buffer_tiers: list[CoachDistanceBufferTier] = Field(min_length=1, max_length=10)
    payments: CoachSchedulerPayments = Field(default_factory=CoachSchedulerPayments)

    @model_validator(mode="after")
    def validate_complete_distance_coverage(self) -> "CoachSchedulerRules":
        _validate_complete_distance_coverage(self.distance_buffer_tiers)
        return self


class CoachSchedulingPolicyTierUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    buffer_mins: int = Field(ge=0, le=240, strict=True)

    @field_validator("id", mode="before")
    @classmethod
    def trim_id(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
            raise ValueError(
                "Tier id must start with a lowercase letter and contain only lowercase letters, numbers, '_' or '-'."
            )
        return value


class CoachSchedulingPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0, strict=True)
    distance_buffers_enabled: bool = Field(strict=True)
    post_break_recovery_mins: int = Field(ge=0, le=240, strict=True)
    distance_buffer_tiers: list[CoachSchedulingPolicyTierUpdate] = Field(
        min_length=1,
        max_length=10,
    )

    @model_validator(mode="after")
    def validate_unique_tier_ids(self) -> "CoachSchedulingPolicyUpdate":
        ids = [tier.id for tier in self.distance_buffer_tiers]
        if len(ids) != len(set(ids)):
            raise ValueError("Distance buffer tier ids must be unique.")
        return self


class CoachSchedulingPolicyResponse(BaseModel):
    revision: int = Field(ge=0, strict=True)
    distance_buffers_enabled: bool = Field(strict=True)
    post_break_recovery_mins: int = Field(ge=0, le=240, strict=True)
    distance_buffer_tiers: list[CoachDistanceBufferTier] = Field(
        min_length=1,
        max_length=10,
    )

    @model_validator(mode="after")
    def validate_complete_distance_coverage(self) -> "CoachSchedulingPolicyResponse":
        _validate_complete_distance_coverage(self.distance_buffer_tiers)
        return self


class CoachCommandCenterSnapshot(BaseModel):
    instructor: CoachInstructorProfileResponse
    date: date
    clients: list[CoachClientResponse]
    schedule: list[CoachScheduleItemResponse]
    requests: list[CoachLessonRequestResponse]
    email_deliveries: list[CoachEmailDeliveryResponse] = Field(default_factory=list)
    rules: CoachSchedulerRules
    scheduling_policy: CoachSchedulingPolicyResponse


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


class GoogleCalendarEvent(BaseModel):
    start_iso: datetime
    end_iso: datetime
    url: str


class ClientInviteCalendarLinks(BaseModel):
    google_events: list[GoogleCalendarEvent] = Field(default_factory=list)
    google: str
    apple: str
    ics: str


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
    calendar_links: Optional[ClientInviteCalendarLinks] = None


class CoachLessonRequestActionResponse(BaseModel):
    id: str
    status: Literal["approved", "needs_alternatives", "declined"]
    appointment_id: Optional[str] = None
    message: str


class CoachLessonRequestConfirmResponse(BaseModel):
    id: str
    status: Literal["confirmed"]
    appointment_id: Optional[str] = None
    appointment_ids: list[str] = Field(default_factory=list)
    message: str
    calendar_links: Optional[ClientInviteCalendarLinks] = None
    email_delivery: Optional[CoachEmailDeliveryResponse] = None


class CoachLessonRequestConfirmRequest(BaseModel):
    slot_id: Optional[str] = Field(default=None, min_length=1, max_length=160)
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=160)


class CoachLessonRequestAlternativesRequest(BaseModel):
    slot_ids: list[str] = Field(default_factory=list)
    message: Optional[str] = Field(default=None, max_length=500)


class PublicInviteResponse(BaseModel):
    invite_token: str
    instructor_name: str
    business_name: str
    location_label: str
    service_name: Optional[str] = None
    service_location: Optional[CoachProviderLocationResponse] = None
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


class NotificationStatus(BaseModel):
    kind: Literal["invite", "confirmation"]
    channel: Literal["sms", "email"]
    status: Literal["queued", "processing", "accepted", "delivered", "failed", "skipped"]
    updated_at: AwareDatetime

    @field_validator("updated_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)


class InviteNotificationResponse(BaseModel):
    notifications: list[NotificationStatus]
    email_delivery: Optional[CoachEmailDeliveryResponse] = None


class PublicLessonRequestResponse(BaseModel):
    notifications: list[NotificationStatus] = Field(default_factory=list)
    request_token: str
    status: Literal["pending"]
    selected_slot: TimeSlotResponse
    message: str


class PublicRequestStatusResponse(BaseModel):
    notifications: list[NotificationStatus] = Field(default_factory=list)
    request_token: str
    status: Literal["pending", "confirmed", "needs_new_times", "expired"]
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
    service_name: Optional[str] = None
    service_location: Optional[CoachProviderLocationResponse] = None
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
    recurrence: Literal["single", "weekly"] = "single"
    recurrence_count: int = 1


class ClientInviteAvailabilityResponse(BaseModel):
    status: Literal["PRESENTING", "FAILED"]
    dialogue_response: str
    slots: list[ClientInviteSchedulerSlot]
    unavailable_windows: list[ClientInviteUnavailableWindow]


class ClientInviteLessonRequestCreate(BaseModel):
    slot_id: str = Field(min_length=1, max_length=120)
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=160)


class ClientInviteLessonConfirmation(BaseModel):
    appointment_id: str
    appointment_ids: list[str] = Field(default_factory=list)
    message: str
    confirmed_at_iso: datetime
    calendar_links: ClientInviteCalendarLinks


class ClientInviteLessonRequestResponse(BaseModel):
    notifications: list[NotificationStatus] = Field(default_factory=list)
    id: str
    invite_token: str
    slot: ClientInviteSchedulerSlot
    status: Literal["requested", "confirmed", "needs_new_times", "expired"]
    requested_at_iso: datetime
    alternative_slots: list[ClientInviteSchedulerSlot] = Field(default_factory=list)
    confirmation: Optional[ClientInviteLessonConfirmation] = None


class ClientInviteAlternativeSelectionRequest(BaseModel):
    slot_id: str = Field(min_length=1, max_length=160)
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=160)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Optional[dict] = None


class ErrorResponse(BaseModel):
    error: ErrorBody
