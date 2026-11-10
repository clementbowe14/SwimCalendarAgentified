from datetime import datetime, time, timedelta, timezone
from uuid import uuid4
from typing import List, Optional
from pydantic import BaseModel, Field
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    Time,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

class InstructorDB(Base):
    __tablename__ = 'instructors'
    __table_args__ = (
        CheckConstraint(
            "default_duration_mins IS NULL OR "
            "(default_duration_mins BETWEEN 15 AND 240 AND default_duration_mins % 15 = 0)",
            name="ck_instructors_default_duration",
        ),
        CheckConstraint(
            "(base_address IS NULL AND base_lat IS NULL AND base_lng IS NULL) OR "
            "(base_address IS NOT NULL AND btrim(base_address) <> '' "
            "AND base_lat IS NOT NULL AND base_lng IS NOT NULL)",
            name="ck_instructors_base_location_complete",
        ),
        CheckConstraint(
            "(base_lat IS NULL OR base_lat BETWEEN -90 AND 90) AND "
            "(base_lng IS NULL OR base_lng BETWEEN -180 AND 180)",
            name="ck_instructors_base_coordinates",
        ),
        CheckConstraint(
            "(service_address IS NULL AND service_lat IS NULL AND service_lng IS NULL) OR "
            "(service_address IS NOT NULL AND btrim(service_address) <> '' "
            "AND service_lat IS NOT NULL AND service_lng IS NOT NULL)",
            name="ck_instructors_service_location_complete",
        ),
        CheckConstraint(
            "(service_lat IS NULL OR service_lat BETWEEN -90 AND 90) AND "
            "(service_lng IS NULL OR service_lng BETWEEN -180 AND 180)",
            name="ck_instructors_service_coordinates",
        ),
    )

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    main_address = Column(String, nullable=False)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    work_start_time = Column(String, default="08:00") # HH:MM format
    work_end_time = Column(String, default="18:00")   # HH:MM format
    timezone = Column(String, nullable=False, default="America/Chicago")
    business_name = Column(String(160), nullable=True)
    base_address = Column(String(300), nullable=True)
    base_lat = Column(Float, nullable=True)
    base_lng = Column(Float, nullable=True)
    service_address = Column(String(300), nullable=True)
    service_lat = Column(Float, nullable=True)
    service_lng = Column(Float, nullable=True)
    service_name = Column(String(160), nullable=True)
    default_duration_mins = Column(Integer, nullable=True)

    appointments = relationship("AppointmentDB", back_populates="instructor")
    clients = relationship("ClientDB", back_populates="instructor")
    invite_tokens = relationship("InviteTokenDB", back_populates="instructor")
    lesson_requests = relationship("LessonRequestDB", back_populates="instructor")
    provider_account = relationship(
        "ProviderAccountDB",
        back_populates="instructor",
        uselist=False,
    )
    working_hours = relationship(
        "ProviderWorkingHoursDB",
        back_populates="instructor",
        cascade="all, delete-orphan",
        order_by="ProviderWorkingHoursDB.weekday",
    )


class ProviderWorkingHoursDB(Base):
    __tablename__ = "provider_working_hours"
    __table_args__ = (
        CheckConstraint(
            "weekday BETWEEN 0 AND 6",
            name="ck_provider_working_hours_weekday",
        ),
        CheckConstraint(
            "(enabled AND start_time IS NOT NULL AND end_time IS NOT NULL AND end_time > start_time) "
            "OR (NOT enabled AND start_time IS NULL AND end_time IS NULL)",
            name="ck_provider_working_hours_state",
        ),
        ForeignKeyConstraint(
            ["instructor_id"],
            ["instructors.id"],
            name="fk_provider_working_hours_instructor_id",
            ondelete="CASCADE",
        ),
        PrimaryKeyConstraint(
            "instructor_id",
            "weekday",
            name="pk_provider_working_hours",
        ),
    )

    instructor_id = Column(String, nullable=False)
    weekday = Column(Integer, nullable=False)
    enabled = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    start_time = Column(Time, nullable=True)
    end_time = Column(Time, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    instructor = relationship("InstructorDB", back_populates="working_hours")


class ProviderAccountDB(Base):
    __tablename__ = "provider_accounts"
    __table_args__ = (
        CheckConstraint("session_version >= 1", name="ck_provider_accounts_session_version"),
        UniqueConstraint("instructor_id", name="uq_provider_accounts_instructor_id"),
        UniqueConstraint("email_normalized", name="uq_provider_accounts_email_normalized"),
    )

    id = Column(String, primary_key=True)
    instructor_id = Column(String, ForeignKey("instructors.id", ondelete="RESTRICT"), nullable=False)
    email_normalized = Column(String(254), nullable=False)
    password_hash = Column(Text, nullable=False)
    is_demo = Column(Boolean, nullable=False, default=False, server_default="false")
    session_version = Column(Integer, nullable=False, default=1, server_default="1")
    disabled_at = Column(DateTime(timezone=True), nullable=True)
    password_changed_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    instructor = relationship("InstructorDB", back_populates="provider_account")
    sessions = relationship(
        "ProviderSessionDB",
        back_populates="account",
        cascade="all, delete-orphan",
    )


class ProviderSessionDB(Base):
    __tablename__ = "provider_sessions"
    __table_args__ = (
        CheckConstraint("session_version >= 1", name="ck_provider_sessions_session_version"),
        CheckConstraint("octet_length(token_digest) = 32", name="ck_provider_sessions_digest_length"),
        Index("uq_provider_sessions_token_digest", "token_digest", unique=True),
        Index("ix_provider_sessions_account_revoked", "account_id", "revoked_at"),
        Index("ix_provider_sessions_expires_at", "expires_at"),
    )

    id = Column(String, primary_key=True)
    account_id = Column(
        String,
        ForeignKey("provider_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_digest = Column(LargeBinary(32), nullable=False)
    session_version = Column(Integer, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revocation_reason = Column(String(80), nullable=True)

    account = relationship("ProviderAccountDB", back_populates="sessions")

class AppointmentDB(Base):
    __tablename__ = 'appointments'
    __table_args__ = (
        CheckConstraint("end_time > start_time", name="ck_appointments_end_after_start"),
        CheckConstraint(
            "event_type in ('lesson', 'block', 'hold')",
            name="ck_appointments_event_type",
        ),
        CheckConstraint(
            "status in ('scheduled', 'hold', 'cancelled', 'completed')",
            name="ck_appointments_status",
        ),
        CheckConstraint(
            "blocker_kind IS NULL OR blocker_kind IN ('break', 'personal', 'unavailable')",
            name="ck_appointments_blocker_kind",
        ),
        CheckConstraint("revision >= 1", name="ck_appointments_revision_positive"),
        CheckConstraint(
            "(location_address IS NULL AND lat IS NULL AND lng IS NULL) OR "
            "(location_address IS NOT NULL AND btrim(location_address) <> '' "
            "AND lat IS NOT NULL AND lng IS NOT NULL)",
            name="ck_appointments_location_complete",
        ),
        CheckConstraint(
            "(lat IS NULL OR lat BETWEEN -90 AND 90) AND "
            "(lng IS NULL OR lng BETWEEN -180 AND 180)",
            name="ck_appointments_location_coordinates",
        ),
        CheckConstraint(
            "(event_type = 'block' AND blocker_kind IS NOT NULL "
            "AND client_id IS NULL AND request_id IS NULL) OR "
            "(event_type <> 'block' AND blocker_kind IS NULL "
            "AND (client_id IS NOT NULL OR status = 'cancelled') "
            "AND location_address IS NOT NULL)",
            name="ck_appointments_blocker_shape",
        ),
        CheckConstraint(
            "is_break = (event_type = 'block' AND blocker_kind = 'break')",
            name="ck_appointments_break_consistency",
        ),
        CheckConstraint(
            "blocker_kind IS DISTINCT FROM 'unavailable' OR location_address IS NULL",
            name="ck_appointments_unavailable_locationless",
        ),
        CheckConstraint(
            "blocker_create_payload IS NULL OR event_type = 'block'",
            name="ck_appointments_blocker_create_payload",
        ),
        Index("ix_appointments_instructor_window", "instructor_id", "start_time", "end_time"),
        Index(
            "uq_appointments_instructor_idempotency_key",
            "instructor_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id = Column(String, primary_key=True)
    calendar_uid = Column(String, nullable=False, unique=True, default=lambda: str(uuid4()), server_default=text("gen_random_uuid()::text"))
    instructor_id = Column(String, ForeignKey('instructors.id'), nullable=False)
    client_id = Column(String, nullable=True)
    request_id = Column(String, ForeignKey("lesson_requests.id"), nullable=True)
    title = Column(String, nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    location_address = Column(String, nullable=True)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    is_break = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    event_type = Column(String, nullable=False, default="lesson")
    blocker_kind = Column(String(20), nullable=True)
    revision = Column(BigInteger, nullable=False, default=1, server_default=text("1"))
    status = Column(String, nullable=False, default="scheduled")
    idempotency_key = Column(String, nullable=True)
    blocker_create_payload = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    cancelled_at = Column(DateTime, nullable=True)

    instructor = relationship("InstructorDB", back_populates="appointments")
    lesson_request = relationship("LessonRequestDB", back_populates="appointments")


@event.listens_for(AppointmentDB, "before_insert")
@event.listens_for(AppointmentDB, "before_update")
def _normalize_legacy_block_shape(_mapper, _connection, appointment: AppointmentDB) -> None:
    """Keep pre-issue callers compatible while the database enforces the new shape."""

    if appointment.event_type == "block" or appointment.is_break is True:
        appointment.event_type = "block"
        appointment.blocker_kind = appointment.blocker_kind or "break"
        appointment.client_id = None
        appointment.request_id = None
        appointment.is_break = appointment.blocker_kind == "break"
    else:
        appointment.blocker_kind = None
        appointment.is_break = False

class ClientDB(Base):
    __tablename__ = "clients"
    __table_args__ = (
        Index("ix_clients_instructor_name", "instructor_id", "name"),
    )

    id = Column(String, primary_key=True)
    instructor_id = Column(String, ForeignKey("instructors.id"), nullable=False)
    name = Column(String, nullable=False)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    default_location_address = Column(String, nullable=True)
    default_lat = Column(Float, nullable=True)
    default_lng = Column(Float, nullable=True)
    status = Column(String, nullable=False, default="active")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    instructor = relationship("InstructorDB", back_populates="clients")

class InviteTokenDB(Base):
    __tablename__ = "invite_tokens"
    __table_args__ = (
        UniqueConstraint("token", "instructor_id", name="uq_invite_tokens_tenant"),
        Index("ix_invite_tokens_instructor", "instructor_id"),
    )

    token = Column(String, primary_key=True)
    instructor_id = Column(String, ForeignKey("instructors.id"), nullable=False)
    client_id = Column(String, ForeignKey("clients.id"), nullable=True)
    business_name = Column(String, nullable=False)
    location_label = Column(String, nullable=False)
    preferred_duration_mins = Column(Integer, nullable=False, default=60)
    allowed_durations_mins = Column(Text, nullable=False, default="30,45,60")
    is_demo = Column(Boolean, nullable=False, default=False, server_default="false")
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    instructor = relationship("InstructorDB", back_populates="invite_tokens")
    client = relationship("ClientDB")
    lesson_requests = relationship("LessonRequestDB", back_populates="invite")

class LessonRequestDB(Base):
    __tablename__ = "lesson_requests"
    __table_args__ = (
        UniqueConstraint("id", "invite_token", "instructor_id", name="uq_lesson_requests_tenant_invite"),
        CheckConstraint("slot_end > slot_start", name="ck_lesson_requests_end_after_start"),
        CheckConstraint(
            "status in ('pending', 'confirmed', 'alternatives_sent', 'declined', 'cancelled')",
            name="ck_lesson_requests_status",
        ),
        CheckConstraint(
            "recurrence in ('single', 'weekly')",
            name="ck_lesson_requests_recurrence",
        ),
        CheckConstraint(
            "((recurrence = 'single' AND recurrence_count = 1) OR (recurrence = 'weekly' AND recurrence_count BETWEEN 1 AND 12))",
            name="ck_lesson_requests_recurrence_count",
        ),
        CheckConstraint(
            "location_type in ('home', 'facility')",
            name="ck_lesson_requests_location_type",
        ),
        Index("ix_lesson_requests_instructor_status", "instructor_id", "status"),
        Index("uq_lesson_requests_request_token", "request_token", unique=True),
        Index("uq_lesson_requests_idempotency_key", "idempotency_key", unique=True),
    )

    id = Column(String, primary_key=True)
    request_token = Column(String, nullable=False)
    invite_token = Column(String, ForeignKey("invite_tokens.token"), nullable=False)
    instructor_id = Column(String, ForeignKey("instructors.id"), nullable=False)
    client_id = Column(String, ForeignKey("clients.id"), nullable=True)
    client_name = Column(String, nullable=False)
    client_email = Column(String, nullable=True)
    client_phone = Column(String, nullable=True)
    client_address = Column(String, nullable=False)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    requested_date = Column(Date, nullable=False)
    slot_start = Column(DateTime, nullable=False)
    slot_end = Column(DateTime, nullable=False)
    duration_mins = Column(Integer, nullable=False)
    travel_time_mins = Column(Integer, nullable=False, default=0)
    buffer_mins = Column(Integer, nullable=False, default=0)
    distance_miles = Column(Float, nullable=False, default=0.0)
    location_type = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending")
    recurrence = Column(String, nullable=False, default="single")
    recurrence_count = Column(Integer, nullable=False, default=1)
    original_slot_offer_id = Column(String, ForeignKey("invite_slot_offers.id"), nullable=True)
    confirmed_slot_offer_id = Column(String, ForeignKey("invite_slot_offers.id"), nullable=True)
    expires_at = Column(DateTime, nullable=False, default=lambda: datetime.utcnow() + timedelta(hours=24))
    confirmed_at = Column(DateTime, nullable=True)
    declined_at = Column(DateTime, nullable=True)
    alternatives_sent_at = Column(DateTime, nullable=True)
    validation_failure_code = Column(String, nullable=True)
    validation_failure_details = Column(JSON, nullable=True)
    validation_failed_at = Column(DateTime, nullable=True)
    client_notification_status = Column(String, nullable=True)
    client_notification_message = Column(Text, nullable=True)
    client_notified_at = Column(DateTime, nullable=True)
    idempotency_key = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    instructor = relationship("InstructorDB", back_populates="lesson_requests")
    client = relationship("ClientDB")
    invite = relationship("InviteTokenDB", back_populates="lesson_requests")
    appointments = relationship("AppointmentDB", back_populates="lesson_request")
    original_slot_offer = relationship("InviteSlotOfferDB", foreign_keys=[original_slot_offer_id])
    confirmed_slot_offer = relationship("InviteSlotOfferDB", foreign_keys=[confirmed_slot_offer_id])


class NotificationEventDB(Base):
    __tablename__ = "notification_events"
    __table_args__ = (
        ForeignKeyConstraint(["invite_token", "instructor_id"], ["invite_tokens.token", "invite_tokens.instructor_id"], name="fk_notifications_invite_tenant"),
        ForeignKeyConstraint(["request_id", "invite_token", "instructor_id"], ["lesson_requests.id", "lesson_requests.invite_token", "lesson_requests.instructor_id"], name="fk_notifications_request_tenant"),
        UniqueConstraint("instructor_id", "logical_key", "channel", name="uq_notifications_channel"),
        CheckConstraint("kind in ('invite', 'confirmation')", name="ck_notifications_kind"),
        CheckConstraint("channel in ('sms', 'email')", name="ck_notifications_channel"),
        CheckConstraint("status in ('queued', 'processing', 'accepted', 'delivered', 'failed', 'skipped')", name="ck_notifications_status"),
        CheckConstraint("attempt_count between 0 and 5", name="ck_notifications_attempts"),
        CheckConstraint("(kind = 'confirmation' AND request_id IS NOT NULL) OR (kind = 'invite' AND request_id IS NULL)", name="ck_notifications_request_kind"),
        Index("ix_notifications_due", "status", "next_attempt_at", "lease_expires_at"),
        Index("ix_notifications_request", "request_id"),
    )

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    instructor_id = Column(String, ForeignKey("instructors.id"), nullable=False)
    invite_token = Column(String, nullable=False)
    request_id = Column(String, nullable=True)
    kind = Column(String, nullable=False)
    channel = Column(String, nullable=False)
    logical_key = Column(String, nullable=False)
    status = Column(String, nullable=False, default="queued")
    recipient = Column(String, nullable=True)
    template_version = Column(String, nullable=False, default="1")
    template_context = Column(JSON, nullable=False, default=dict)
    attempt_count = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    attempt_token = Column(String, nullable=True)
    failure_code = Column(String, nullable=True)
    provider_message_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class RouteEstimateDB(Base):
    __tablename__ = "route_estimates"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "origin_key",
            "destination_key",
            "departure_bucket_start",
            "traffic_mode",
            name="uq_route_estimates_cache_key",
        ),
        Index("ix_route_estimates_expires_at", "expires_at"),
    )

    id = Column(String, primary_key=True)
    provider = Column(String, nullable=False)
    origin_lat = Column(Float, nullable=False)
    origin_lng = Column(Float, nullable=False)
    destination_lat = Column(Float, nullable=False)
    destination_lng = Column(Float, nullable=False)
    origin_key = Column(String, nullable=False)
    destination_key = Column(String, nullable=False)
    departure_bucket_start = Column(DateTime, nullable=False)
    traffic_mode = Column(String, nullable=False, default="none")
    distance_miles = Column(Float, nullable=False)
    travel_time_mins = Column(Integer, nullable=False)
    duration_seconds = Column(Integer, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class InviteIntakeDB(Base):
    __tablename__ = "invite_intakes"
    __table_args__ = (
        UniqueConstraint("invite_token", name="uq_invite_intakes_invite_token"),
    )

    id = Column(String, primary_key=True)
    invite_token = Column(String, ForeignKey("invite_tokens.token"), nullable=False)
    client_name = Column(String, nullable=False)
    guardian_phone = Column(String, nullable=True)
    guardian_email = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    location_type = Column(String, nullable=False, default="home")
    location_address = Column(String, nullable=False)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    is_instructor_facility = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    invite = relationship("InviteTokenDB")


class InviteSlotOfferDB(Base):
    __tablename__ = "invite_slot_offers"
    __table_args__ = (
        CheckConstraint(
            "offer_kind in ('search', 'alternative')",
            name="ck_invite_slot_offers_offer_kind",
        ),
        CheckConstraint(
            "((recurrence = 'single' AND recurrence_count = 1) OR (recurrence = 'weekly' AND recurrence_count BETWEEN 1 AND 12))",
            name="ck_invite_slot_offers_recurrence_count",
        ),
        Index("ix_invite_slot_offers_invite_expires", "invite_token", "expires_at"),
        Index("ix_invite_slot_offers_request_kind", "lesson_request_id", "offer_kind"),
    )

    id = Column(String, primary_key=True)
    invite_token = Column(String, ForeignKey("invite_tokens.token"), nullable=False)
    instructor_id = Column(String, ForeignKey("instructors.id"), nullable=False)
    client_name = Column(String, nullable=False)
    client_address = Column(String, nullable=False)
    requested_date = Column(Date, nullable=False)
    slot_start = Column(DateTime, nullable=False)
    slot_end = Column(DateTime, nullable=False)
    duration_mins = Column(Integer, nullable=False)
    location_type = Column(String, nullable=False)
    location_address = Column(String, nullable=False)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    travel_time_mins = Column(Integer, nullable=False)
    buffer_mins = Column(Integer, nullable=False)
    distance_miles = Column(Float, nullable=False)
    recurrence = Column(String, nullable=False, default="single")
    recurrence_count = Column(Integer, nullable=False, default=1)
    recurrence_available = Column(Boolean, nullable=False, default=True)
    lesson_request_id = Column(String, ForeignKey("lesson_requests.id"), nullable=True)
    offer_kind = Column(String, nullable=False, default="search")
    message = Column(Text, nullable=True)
    notified_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    invite = relationship("InviteTokenDB")
    lesson_request = relationship("LessonRequestDB", foreign_keys=[lesson_request_id])


class SchedulerSettingsDB(Base):
    __tablename__ = "scheduler_settings"
    __table_args__ = (
        CheckConstraint(
            "policy_revision >= 0",
            name="ck_scheduler_settings_policy_revision_nonnegative",
        ),
    )

    instructor_id = Column(String, ForeignKey("instructors.id"), primary_key=True)
    buffers_enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    apply_distance_buffer_at_shift_start = Column(Boolean, nullable=False, default=False, server_default="false")
    rounding_increment_mins = Column(Integer, nullable=False, default=15)
    post_break_buffer_mins = Column(Integer, nullable=False, default=15)
    distance_buffer_tiers = Column(Text, nullable=False)
    payments_enabled_methods = Column(Text, nullable=False, default="")
    cancellation_window_hours = Column(Integer, nullable=False, default=24)
    require_payment_method_for_new_clients = Column(Boolean, nullable=False, default=False)
    policy_revision = Column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    instructor = relationship("InstructorDB")

# Pydantic Schemas
class LocationInfo(BaseModel):
    address: str
    lat: float
    lng: float
    is_instructor_facility: bool = False

class TimeSlotDTO(BaseModel):
    start_time: datetime
    end_time: datetime
    travel_time_mins: int
    buffer_mins: int
    distance_miles: float

    def format_display(self) -> str:
        return f"{self.start_time.strftime('%I:%M %p')} - {self.end_time.strftime('%I:%M %p')} (Travel: {self.travel_time_mins}m, Buffer: {self.buffer_mins}m, Dist: {self.distance_miles:.1f}mi)"

class BookingRequestDTO(BaseModel):
    instructor_id: str
    client_id: str
    client_name: str
    location: LocationInfo
    requested_date: str # YYYY-MM-DD
    duration_mins: int = 60
