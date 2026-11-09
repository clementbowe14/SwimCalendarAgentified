import datetime
import json
import os
import uuid
from typing import Optional, List
from sqlalchemy import create_engine, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from app import timezones
from app.config import (
    ApplicationSettings,
    application_settings_from_env,
    demo_data_access_enabled_from_env,
)
from app.models import (
    AppointmentDB,
    Base,
    ClientDB,
    InstructorDB,
    InviteIntakeDB,
    InviteTokenDB,
    LessonRequestDB,
    LocationInfo,
    SchedulerSettingsDB,
    InviteSlotOfferDB,
    ProviderAccountDB,
    ProviderWorkingHoursDB,
)

DATABASE_URL = os.getenv(
    "SWIM_DATABASE_URL",
    "postgresql+psycopg://swim:swim@127.0.0.1:5432/swim_scheduler",
)
ACTIVE_APPOINTMENT_STATUSES = ("scheduled", "hold")
DEMO_INVITE_TOKEN = "demo-invite-token"
SOFIA_INVITE_TOKEN = "invite_sofia_0826"

engine = create_engine(DATABASE_URL, echo=False, hide_parameters=True)
SessionLocal = sessionmaker(bind=engine)

def configure_database(database_url: str = DATABASE_URL):
    """Rebinds the database session factory; useful for isolated API tests."""
    global DATABASE_URL, engine, SessionLocal
    DATABASE_URL = database_url
    engine = create_engine(DATABASE_URL, echo=False, hide_parameters=True)
    SessionLocal = sessionmaker(bind=engine)

def init_db(*, settings: Optional[ApplicationSettings] = None):
    active_settings = settings or application_settings_from_env()
    if active_settings.environment in {"staging", "production"}:
        _reject_deployed_demo_artifacts()
    if not active_settings.demo_data_enabled:
        return
    seed_data()
    if active_settings.demo_auth_enabled:
        from app.auth import seed_demo_provider_account

        seed_demo_provider_account()

def seed_data():
    session = SessionLocal()

    # Check if instructor already exists
    instructor = session.query(InstructorDB).filter_by(id="inst_001").first()
    if not instructor:
        instructor = InstructorDB(
            id="inst_001",
            name="Coach Sarah",
            main_address="100 Main St, Austin, TX",
            lat=30.2672,
            lng=-97.7431,
            work_start_time="08:00",
            work_end_time="17:00",
            timezone=timezones.DEFAULT_INSTRUCTOR_TIMEZONE,
            business_name="Swim Command",
            base_address="100 Main St, Austin, TX",
            base_lat=30.2672,
            base_lng=-97.7431,
            service_address="100 Main St, Austin, TX",
            service_lat=30.2672,
            service_lng=-97.7431,
            service_name="Swim lesson",
            default_duration_mins=45,
        )
        session.add(instructor)
        session.commit()

        # Seed existing appointments & breaks for a sample date (e.g. today / 2026-08-20)
        sample_date = datetime.date(2026, 8, 20)

        # 1. Morning Lesson at North Pool (30.3300, -97.7200) -> ~5 miles
        app1_start = timezones.to_utc_storage(
            timezones.validate_local_datetime(
                datetime.datetime.combine(sample_date, datetime.time(9, 0)),
                instructor.timezone,
            )
        )
        app1 = AppointmentDB(
            id="app_101",
            instructor_id="inst_001",
            client_id="client_alice",
            title="Lesson with Alice",
            start_time=app1_start,
            end_time=app1_start + datetime.timedelta(hours=1),
            location_address="450 North Blvd, Austin, TX",
            lat=30.3300,
            lng=-97.7200,
            is_break=False
        )

        # 2. Scheduled Lunch Break (12:00 PM - 1:00 PM) at Main Base
        app2_start = timezones.to_utc_storage(
            timezones.validate_local_datetime(
                datetime.datetime.combine(sample_date, datetime.time(12, 0)),
                instructor.timezone,
            )
        )
        app2 = AppointmentDB(
            id="app_break_1",
            instructor_id="inst_001",
            client_id="instructor_break",
            title="Lunch Break",
            start_time=app2_start,
            end_time=app2_start + datetime.timedelta(hours=1),
            location_address="100 Main St, Austin, TX",
            lat=30.2672,
            lng=-97.7431,
            is_break=True
        )

        # 3. Afternoon Lesson at West Pool (30.2700, -97.8200) -> ~8 miles
        app3_start = timezones.to_utc_storage(
            timezones.validate_local_datetime(
                datetime.datetime.combine(sample_date, datetime.time(14, 30)),
                instructor.timezone,
            )
        )
        app3 = AppointmentDB(
            id="app_102",
            instructor_id="inst_001",
            client_id="client_bob",
            title="Lesson with Bob",
            start_time=app3_start,
            end_time=app3_start + datetime.timedelta(hours=1),
            location_address="800 West Lake Dr, Austin, TX",
            lat=30.2700,
            lng=-97.8200,
            is_break=False
        )

        session.add_all([app1, app2, app3])
        session.commit()

    if not session.query(ProviderWorkingHoursDB).filter_by(instructor_id="inst_001").first():
        session.add_all(
            [
                ProviderWorkingHoursDB(
                    instructor_id="inst_001",
                    weekday=weekday,
                    enabled=True,
                    start_time=datetime.time(8, 0),
                    end_time=datetime.time(17, 0),
                )
                for weekday in range(7)
            ]
        )

    if not session.query(ClientDB).filter_by(id="client_alice").first():
        session.add(
            ClientDB(
                id="client_alice",
                instructor_id="inst_001",
                name="Alice",
                default_location_address="450 North Blvd, Austin, TX",
                default_lat=30.3300,
                default_lng=-97.7200,
            )
        )

    if not session.query(ClientDB).filter_by(id="client_bob").first():
        session.add(
            ClientDB(
                id="client_bob",
                instructor_id="inst_001",
                name="Bob",
                default_location_address="800 West Lake Dr, Austin, TX",
                default_lat=30.2700,
                default_lng=-97.8200,
            )
        )

    if not session.query(ClientDB).filter_by(id="client_sofia").first():
        session.add(
            ClientDB(
                id="client_sofia",
                instructor_id="inst_001",
                name="Sofia Martinez",
                email="ana.martinez@example.com",
                phone="(512) 555-0194",
                default_location_address="820 Lakeview Dr, Austin, TX",
                default_lat=30.289,
                default_lng=-97.739,
            )
        )

    demo_invite = session.query(InviteTokenDB).filter_by(token=DEMO_INVITE_TOKEN).first()
    if not demo_invite:
        session.add(
            InviteTokenDB(
                token=DEMO_INVITE_TOKEN,
                instructor_id="inst_001",
                business_name="Riley Swim",
                location_label="Instructor facility or client pool",
                preferred_duration_mins=60,
                allowed_durations_mins="30,45,60",
                is_demo=True,
                expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=30),
            )
        )
    else:
        demo_invite.is_demo = True

    sofia_invite = session.query(InviteTokenDB).filter_by(token=SOFIA_INVITE_TOKEN).first()
    if not sofia_invite:
        session.add(
            InviteTokenDB(
                token=SOFIA_INVITE_TOKEN,
                instructor_id="inst_001",
                client_id="client_sofia",
                business_name="Swim Command",
                location_label="Riverside facility or home pool",
                preferred_duration_mins=45,
                allowed_durations_mins="30,45,60",
                is_demo=True,
                expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=30),
            )
        )
    else:
        sofia_invite.is_demo = True

    if not session.query(InviteIntakeDB).filter_by(invite_token=SOFIA_INVITE_TOKEN).first():
        session.add(
            InviteIntakeDB(
                id="intake_sofia_0826",
                invite_token=SOFIA_INVITE_TOKEN,
                client_name="Sofia Martinez",
                guardian_phone="(512) 555-0194",
                guardian_email="ana.martinez@example.com",
                notes="Beginner swimmer. Prefers shallow end warmup.",
                location_type="home",
                location_address="820 Lakeview Dr, Austin, TX",
                lat=30.289,
                lng=-97.739,
                is_instructor_facility=False,
            )
        )
    if not session.query(LessonRequestDB).filter_by(id="req_sofia_pending_0820").first():
        session.add(
            LessonRequestDB(
                id="req_sofia_pending_0820",
                request_token="req_sofia_pending_0820",
                invite_token=SOFIA_INVITE_TOKEN,
                instructor_id="inst_001",
                client_id="client_sofia",
                client_name="Sofia Martinez",
                client_email="ana.martinez@example.com",
                client_phone="(512) 555-0194",
                client_address="820 Lakeview Dr, Austin, TX",
                lat=30.289,
                lng=-97.739,
                requested_date=datetime.date(2026, 8, 20),
                slot_start=timezones.to_utc_storage(
                    timezones.validate_local_datetime(
                        datetime.datetime(2026, 8, 20, 16, 15),
                        instructor.timezone,
                    )
                ),
                slot_end=timezones.to_utc_storage(
                    timezones.validate_local_datetime(
                        datetime.datetime(2026, 8, 20, 17, 0),
                        instructor.timezone,
                    )
                ),
                duration_mins=45,
                travel_time_mins=24,
                buffer_mins=20,
                distance_miles=2.1,
                location_type="home",
                status="pending",
                recurrence="single",
                recurrence_count=1,
                expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=1),
                created_at=datetime.datetime(2026, 8, 19, 16, 30),
                updated_at=datetime.datetime(2026, 8, 19, 16, 30),
            )
        )
    session.commit()
    session.close()


def _reject_deployed_demo_artifacts() -> None:
    session = SessionLocal()
    try:
        demo_account_exists = (
            session.query(ProviderAccountDB.id)
            .filter(ProviderAccountDB.is_demo.is_(True))
            .first()
            is not None
        )
        demo_invite_exists = (
            session.query(InviteTokenDB.token)
            .filter(InviteTokenDB.is_demo.is_(True))
            .first()
            is not None
        )
    finally:
        session.close()
    if demo_account_exists or demo_invite_exists:
        raise RuntimeError(
            "Demo authentication or invite artifacts are forbidden in staging and production."
        )

def get_instructor_info(instructor_id: str = "inst_001") -> Optional[InstructorDB]:
    session = SessionLocal()
    inst = session.query(InstructorDB).filter_by(id=instructor_id).first()
    session.close()
    return inst

def get_appointments_for_date(instructor_id: str, date_str: str) -> List[dict]:
    return get_local_appointments_for_date(instructor_id, date_str)

def get_local_appointments_for_date(instructor_id: str, date_str: str) -> List[dict]:
    instructor = get_instructor_info(instructor_id)
    if not instructor:
        return []
    timezone_name = _read_timezone(instructor.timezone)
    rows = _get_appointment_rows_for_local_date(instructor_id, date_str, timezone_name)
    return [_appointment_to_dict(row, timezone_name=timezone_name) for row in rows]

def get_utc_appointment_events_for_date(instructor_id: str, date_str: str) -> List[dict]:
    instructor = get_instructor_info(instructor_id)
    if not instructor:
        return []
    rows = _get_appointment_rows_for_local_date(
        instructor_id,
        date_str,
        _read_timezone(instructor.timezone),
    )
    return [_appointment_to_dict(row, aware_utc=True) for row in rows]


def _read_timezone(timezone_name: Optional[str]) -> str:
    try:
        return timezones.get_zone(timezone_name).key
    except timezones.InvalidTimezoneError:
        return timezones.DEFAULT_INSTRUCTOR_TIMEZONE

def _get_appointment_rows_for_local_date(
    instructor_id: str,
    date_str: str,
    timezone_name: str,
) -> List[AppointmentDB]:
    session = SessionLocal()
    try:
        target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        start_of_day, end_of_day = timezones.local_day_window_utc_storage(target_date, timezone_name)
        query = session.query(AppointmentDB)
        if not demo_data_access_enabled_from_env():
            query = (
                query.outerjoin(
                    LessonRequestDB,
                    AppointmentDB.request_id == LessonRequestDB.id,
                )
                .outerjoin(
                    InviteTokenDB,
                    LessonRequestDB.invite_token == InviteTokenDB.token,
                )
                .filter(
                    or_(
                        AppointmentDB.request_id.is_(None),
                        InviteTokenDB.is_demo.is_(False),
                    )
                )
            )
        apps = query.filter(
            AppointmentDB.instructor_id == instructor_id,
            AppointmentDB.start_time < end_of_day,
            AppointmentDB.end_time > start_of_day,
            AppointmentDB.status.in_(ACTIVE_APPOINTMENT_STATUSES),
            AppointmentDB.cancelled_at.is_(None),
        ).order_by(AppointmentDB.start_time.asc()).all()
        for appointment in apps:
            session.expunge(appointment)
        return apps
    finally:
        session.close()

def get_clients_for_instructor(instructor_id: str) -> List[dict]:
    session = SessionLocal()
    clients = (
        session.query(ClientDB)
        .filter(ClientDB.instructor_id == instructor_id, ClientDB.status == "active")
        .order_by(ClientDB.name)
        .all()
    )
    result = [
        {
            "id": client.id,
            "name": client.name,
            "email": client.email,
            "phone": client.phone,
            "default_location_address": client.default_location_address,
            "default_lat": client.default_lat,
            "default_lng": client.default_lng,
        }
        for client in clients
    ]
    session.close()
    return result

def get_pending_lesson_requests_for_instructor(instructor_id: str) -> List[dict]:
    session = SessionLocal()
    now = datetime.datetime.utcnow()
    query = session.query(LessonRequestDB).join(
        InviteTokenDB,
        LessonRequestDB.invite_token == InviteTokenDB.token,
    )
    if not demo_data_access_enabled_from_env():
        query = query.filter(InviteTokenDB.is_demo.is_(False))
    requests = (
        query
        .filter(
            LessonRequestDB.instructor_id == instructor_id,
            LessonRequestDB.status.in_(("pending", "confirmed", "alternatives_sent", "declined")),
        )
        .order_by(LessonRequestDB.created_at.asc())
        .all()
    )
    result = [
        {
            "id": request.id,
            "request_token": request.request_token,
            "invite_token": request.invite_token,
            "instructor_id": request.instructor_id,
            "client_id": request.client_id,
            "client_name": request.client_name,
            "client_email": request.client_email,
            "client_phone": request.client_phone,
            "client_address": request.client_address,
            "lat": request.lat,
            "lng": request.lng,
            "requested_date": request.requested_date,
            "slot_start": request.slot_start,
            "slot_end": request.slot_end,
            "duration_mins": request.duration_mins,
            "travel_time_mins": request.travel_time_mins,
            "buffer_mins": request.buffer_mins,
            "distance_miles": request.distance_miles,
            "location_type": request.location_type,
            "status": request.status,
            "recurrence": request.recurrence,
            "recurrence_count": request.recurrence_count,
            "original_slot_offer_id": request.original_slot_offer_id,
            "expires_at": request.expires_at,
            "computed_expired": request.status not in {"confirmed", "declined", "cancelled"} and request.expires_at <= now,
            "validation_failure_code": request.validation_failure_code,
            "created_at": request.created_at,
            "alternative_slots": [
                {
                    "id": offer.id,
                    "slot_start": offer.slot_start,
                    "slot_end": offer.slot_end,
                    "travel_time_mins": offer.travel_time_mins,
                    "buffer_mins": offer.buffer_mins,
                    "distance_miles": offer.distance_miles,
                    "client_address": offer.client_address,
                    "lat": offer.lat,
                    "lng": offer.lng,
                    "location_type": offer.location_type,
                }
                for offer in session.query(InviteSlotOfferDB)
                .filter_by(lesson_request_id=request.id, offer_kind="alternative")
                .order_by(InviteSlotOfferDB.slot_start.asc())
                .all()
            ],
        }
        for request in requests
    ]
    session.close()
    return result

def get_scheduler_settings(instructor_id: str) -> Optional[dict]:
    session = SessionLocal()
    try:
        settings = session.query(SchedulerSettingsDB).filter_by(instructor_id=instructor_id).first()
        if settings is None:
            return None
        return {
            "policy_revision": settings.policy_revision,
            "buffers_enabled": settings.buffers_enabled,
            "apply_distance_buffer_at_shift_start": settings.apply_distance_buffer_at_shift_start,
            "rounding_increment_mins": settings.rounding_increment_mins,
            "post_break_buffer_mins": settings.post_break_buffer_mins,
            "distance_buffer_tiers": json.loads(settings.distance_buffer_tiers),
            "payments": {
                "enabled_methods": _split_csv(settings.payments_enabled_methods),
                "cancellation_window_hours": settings.cancellation_window_hours,
                "require_payment_method_for_new_clients": settings.require_payment_method_for_new_clients,
            },
        }
    finally:
        session.close()


def get_scheduling_policy(instructor_id: str) -> Optional[dict]:
    """Read only the physical columns owned by the canonical policy."""

    session = SessionLocal()
    try:
        row = (
            session.execute(
                text(
                    """
                    SELECT
                        policy_revision,
                        buffers_enabled,
                        post_break_buffer_mins,
                        distance_buffer_tiers
                    FROM scheduler_settings
                    WHERE instructor_id = CAST(:instructor_id AS VARCHAR)
                    """
                ),
                {"instructor_id": instructor_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return {
            "policy_revision": row["policy_revision"],
            "buffers_enabled": row["buffers_enabled"],
            "post_break_buffer_mins": row["post_break_buffer_mins"],
            "distance_buffer_tiers": json.loads(row["distance_buffer_tiers"]),
        }
    finally:
        session.close()


def save_scheduler_settings(
    instructor_id: str,
    rules: dict,
    *,
    explicit_fields: Optional[set[str]] = None,
) -> dict:
    session = SessionLocal()
    try:
        settings = session.query(SchedulerSettingsDB).filter_by(instructor_id=instructor_id).first()
        is_new = settings is None
        if is_new:
            settings = SchedulerSettingsDB(instructor_id=instructor_id)
            session.add(settings)
        if is_new or explicit_fields is None or "buffers_enabled" in explicit_fields:
            settings.buffers_enabled = rules["buffers_enabled"]
        if is_new or explicit_fields is None or "apply_distance_buffer_at_shift_start" in explicit_fields:
            settings.apply_distance_buffer_at_shift_start = rules["apply_distance_buffer_at_shift_start"]
        settings.rounding_increment_mins = rules["rounding_increment_mins"]
        settings.post_break_buffer_mins = rules["post_break_buffer_mins"]
        settings.distance_buffer_tiers = json.dumps(rules["distance_buffer_tiers"], sort_keys=True)
        settings.payments_enabled_methods = ",".join(rules["payments"]["enabled_methods"])
        settings.cancellation_window_hours = rules["payments"]["cancellation_window_hours"]
        settings.require_payment_method_for_new_clients = rules["payments"]["require_payment_method_for_new_clients"]
        settings.updated_at = datetime.datetime.utcnow()
        session.commit()
    finally:
        session.close()
    saved = get_scheduler_settings(instructor_id)
    if saved is None:
        raise RuntimeError("Scheduler settings were not saved.")
    return saved


def save_scheduling_policy_cas(
    instructor_id: str,
    *,
    expected_revision: int,
    distance_buffers_enabled: bool,
    post_break_recovery_mins: int,
    distance_buffer_tiers: list[dict],
) -> Optional[dict]:
    """Atomically insert or compare-and-swap the canonical scheduling policy."""

    serialized_tiers = json.dumps(distance_buffer_tiers, sort_keys=True)
    now = datetime.datetime.utcnow()
    session = SessionLocal()
    try:
        row = (
            session.execute(
                text(
                    """
                    INSERT INTO scheduler_settings (
                        instructor_id,
                        buffers_enabled,
                        post_break_buffer_mins,
                        distance_buffer_tiers,
                        policy_revision,
                        updated_at
                    )
                    SELECT
                        CAST(:instructor_id AS VARCHAR),
                        CAST(:distance_buffers_enabled AS BOOLEAN),
                        CAST(:post_break_recovery_mins AS INTEGER),
                        CAST(:distance_buffer_tiers AS TEXT),
                        1,
                        CAST(:updated_at AS TIMESTAMP WITHOUT TIME ZONE)
                    WHERE
                        CAST(:expected_revision AS BIGINT) = 0
                        OR EXISTS (
                            SELECT 1
                            FROM scheduler_settings
                            WHERE instructor_id = CAST(:instructor_id AS VARCHAR)
                        )
                    ON CONFLICT (instructor_id) DO UPDATE
                    SET
                        buffers_enabled = EXCLUDED.buffers_enabled,
                        post_break_buffer_mins = EXCLUDED.post_break_buffer_mins,
                        distance_buffer_tiers = EXCLUDED.distance_buffer_tiers,
                        policy_revision = scheduler_settings.policy_revision + 1,
                        updated_at = EXCLUDED.updated_at
                    WHERE scheduler_settings.policy_revision = CAST(:expected_revision AS BIGINT)
                    RETURNING
                        policy_revision,
                        buffers_enabled,
                        post_break_buffer_mins,
                        distance_buffer_tiers
                    """
                ),
                {
                    "instructor_id": instructor_id,
                    "expected_revision": expected_revision,
                    "distance_buffers_enabled": distance_buffers_enabled,
                    "post_break_recovery_mins": post_break_recovery_mins,
                    "distance_buffer_tiers": serialized_tiers,
                    "updated_at": now,
                },
            )
            .mappings()
            .one_or_none()
        )
        session.commit()
        if row is None:
            return None
        return {
            "policy_revision": row["policy_revision"],
            "buffers_enabled": row["buffers_enabled"],
            "post_break_buffer_mins": row["post_break_buffer_mins"],
            "distance_buffer_tiers": json.loads(row["distance_buffer_tiers"]),
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

def _split_csv(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]

class ScheduleConflictError(Exception):
    pass

class IdempotencyKeyConflictError(Exception):
    pass

def get_appointment_by_idempotency_key(
    instructor_id: str,
    idempotency_key: Optional[str],
) -> Optional[dict]:
    if not idempotency_key:
        return None
    session = SessionLocal()
    try:
        appointment = _idempotent_appointment_or_conflict(
            session,
            instructor_id,
            idempotency_key,
        )
        return _appointment_to_dict(appointment) if appointment else None
    finally:
        session.close()


def _idempotent_appointment_or_conflict(
    session,
    instructor_id: str,
    idempotency_key: str,
) -> Optional[AppointmentDB]:
    appointment = (
        session.query(AppointmentDB)
        .filter_by(instructor_id=instructor_id, idempotency_key=idempotency_key)
        .first()
    )
    if appointment is None or demo_data_access_enabled_from_env():
        return appointment
    if appointment.request_id is None:
        return appointment

    visible_request = (
        session.query(LessonRequestDB.id)
        .join(
            InviteTokenDB,
            LessonRequestDB.invite_token == InviteTokenDB.token,
        )
        .filter(
            LessonRequestDB.id == appointment.request_id,
            InviteTokenDB.is_demo.is_(False),
        )
        .first()
    )
    if visible_request is None:
        # Treat a key owned by hidden demo provenance as unavailable without
        # returning any appointment fields to the caller.
        raise IdempotencyKeyConflictError(
            "The idempotency key is not available for this booking."
        )
    return appointment

def _appointment_to_dict(
    appointment: Optional[AppointmentDB],
    *,
    timezone_name: Optional[str] = None,
    aware_utc: bool = False,
) -> Optional[dict]:
    if not appointment:
        return None
    if timezone_name:
        start_time = timezones.storage_to_local(appointment.start_time, timezone_name)
        end_time = timezones.storage_to_local(appointment.end_time, timezone_name)
    elif aware_utc:
        start_time = timezones.storage_to_aware_utc(appointment.start_time)
        end_time = timezones.storage_to_aware_utc(appointment.end_time)
    else:
        start_time = appointment.start_time
        end_time = appointment.end_time
    return {
        "id": appointment.id,
        "instructor_id": appointment.instructor_id,
        "client_id": appointment.client_id,
        "title": appointment.title,
        "start_time": start_time,
        "end_time": end_time,
        "location_address": appointment.location_address,
        "lat": appointment.lat,
        "lng": appointment.lng,
        "is_break": appointment.is_break,
        "event_type": appointment.event_type,
        "status": appointment.status,
        "idempotency_key": appointment.idempotency_key,
        "request_id": appointment.request_id,
    }

def has_active_schedule_conflict(
    instructor_id: str,
    start_time: datetime.datetime,
    end_time: datetime.datetime,
    exclude_appointment_id: Optional[str] = None,
) -> bool:
    start_time = timezones.to_utc_storage(start_time)
    end_time = timezones.to_utc_storage(end_time)
    session = SessionLocal()
    query = session.query(AppointmentDB).filter(
        AppointmentDB.instructor_id == instructor_id,
        AppointmentDB.status.in_(ACTIVE_APPOINTMENT_STATUSES),
        AppointmentDB.cancelled_at.is_(None),
        AppointmentDB.start_time < end_time,
        AppointmentDB.end_time > start_time,
    )
    if exclude_appointment_id:
        query = query.filter(AppointmentDB.id != exclude_appointment_id)
    exists = session.query(query.exists()).scalar()
    session.close()
    return bool(exists)

def save_appointment(
    instructor_id: str,
    client_id: str,
    title: str,
    start_time: datetime.datetime,
    end_time: datetime.datetime,
    location_address: str,
    lat: float,
    lng: float,
    *,
    idempotency_key: Optional[str] = None,
    event_type: str = "lesson",
    status: str = "scheduled",
    request_id: Optional[str] = None,
) -> str:
    start_time = timezones.to_utc_storage(start_time)
    end_time = timezones.to_utc_storage(end_time)
    if end_time <= start_time:
        raise ValueError("Appointment end time must be after start time.")

    session = SessionLocal()
    try:
        if idempotency_key:
            existing = _idempotent_appointment_or_conflict(
                session,
                instructor_id,
                idempotency_key,
            )
            if existing:
                if (
                    existing.instructor_id != instructor_id
                    or existing.start_time != start_time
                    or existing.end_time != end_time
                    or existing.client_id != client_id
                ):
                    raise IdempotencyKeyConflictError("Idempotency key was already used for a different booking.")
                return existing.id

        conflict = session.query(AppointmentDB).filter(
            AppointmentDB.instructor_id == instructor_id,
            AppointmentDB.status.in_(ACTIVE_APPOINTMENT_STATUSES),
            AppointmentDB.cancelled_at.is_(None),
            AppointmentDB.start_time < end_time,
            AppointmentDB.end_time > start_time,
        ).first()
        if conflict:
            raise ScheduleConflictError("An active schedule item already occupies that time.")

        app_id = f"app_{uuid.uuid4().hex[:8]}"
        new_app = AppointmentDB(
            id=app_id,
            instructor_id=instructor_id,
            client_id=client_id,
            request_id=request_id,
            title=title,
            start_time=start_time,
            end_time=end_time,
            location_address=location_address,
            lat=lat,
            lng=lng,
            is_break=event_type == "block",
            event_type=event_type,
            status=status,
            idempotency_key=idempotency_key,
        )
        session.add(new_app)
        session.commit()
        return app_id
    except IntegrityError as exc:
        session.rollback()
        if "exclude_appointments_active_overlap" in str(exc):
            raise ScheduleConflictError("An active schedule item already occupies that time.") from exc
        if idempotency_key:
            existing = _idempotent_appointment_or_conflict(
                session,
                instructor_id,
                idempotency_key,
            )
            if existing:
                if (
                    existing.start_time != start_time
                    or existing.end_time != end_time
                    or existing.client_id != client_id
                ):
                    raise IdempotencyKeyConflictError(
                        "Idempotency key was already used for a different booking."
                    ) from exc
                return existing.id
        raise
    finally:
        session.close()
