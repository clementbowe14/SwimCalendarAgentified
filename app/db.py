import datetime
import os
import uuid
from typing import Optional, List
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from app.models import (
    AppointmentDB,
    Base,
    ClientDB,
    InstructorDB,
    InviteTokenDB,
    LessonRequestDB,
    LocationInfo,
)

DATABASE_URL = os.getenv(
    "SWIM_DATABASE_URL",
    "postgresql+psycopg://swim:swim@127.0.0.1:5432/swim_scheduler",
)
ACTIVE_APPOINTMENT_STATUSES = ("scheduled", "hold")
DEMO_INVITE_TOKEN = "demo-invite-token"

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)

def configure_database(database_url: str = DATABASE_URL):
    """Rebinds the database session factory; useful for isolated API tests."""
    global DATABASE_URL, engine, SessionLocal
    DATABASE_URL = database_url
    engine = create_engine(DATABASE_URL, echo=False)
    SessionLocal = sessionmaker(bind=engine)

def init_db():
    seed_data()

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
            work_end_time="17:00"
        )
        session.add(instructor)
        session.commit()

        # Seed existing appointments & breaks for a sample date (e.g. today / 2026-08-20)
        sample_date = datetime.date(2026, 8, 20)

        # 1. Morning Lesson at North Pool (30.3300, -97.7200) -> ~5 miles
        app1 = AppointmentDB(
            id="app_101",
            instructor_id="inst_001",
            client_id="client_alice",
            title="Lesson with Alice",
            start_time=datetime.datetime.combine(sample_date, datetime.time(9, 0)),
            end_time=datetime.datetime.combine(sample_date, datetime.time(10, 0)),
            location_address="450 North Blvd, Austin, TX",
            lat=30.3300,
            lng=-97.7200,
            is_break=False
        )

        # 2. Scheduled Lunch Break (12:00 PM - 1:00 PM) at Main Base
        app2 = AppointmentDB(
            id="app_break_1",
            instructor_id="inst_001",
            client_id="instructor_break",
            title="Lunch Break",
            start_time=datetime.datetime.combine(sample_date, datetime.time(12, 0)),
            end_time=datetime.datetime.combine(sample_date, datetime.time(13, 0)),
            location_address="100 Main St, Austin, TX",
            lat=30.2672,
            lng=-97.7431,
            is_break=True
        )

        # 3. Afternoon Lesson at West Pool (30.2700, -97.8200) -> ~8 miles
        app3 = AppointmentDB(
            id="app_102",
            instructor_id="inst_001",
            client_id="client_bob",
            title="Lesson with Bob",
            start_time=datetime.datetime.combine(sample_date, datetime.time(14, 30)),
            end_time=datetime.datetime.combine(sample_date, datetime.time(15, 30)),
            location_address="800 West Lake Dr, Austin, TX",
            lat=30.2700,
            lng=-97.8200,
            is_break=False
        )

        session.add_all([app1, app2, app3])
        session.commit()

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

    if not session.query(InviteTokenDB).filter_by(token=DEMO_INVITE_TOKEN).first():
        session.add(
            InviteTokenDB(
                token=DEMO_INVITE_TOKEN,
                instructor_id="inst_001",
                business_name="Riley Swim",
                location_label="Instructor facility or client pool",
                preferred_duration_mins=60,
                allowed_durations_mins="30,45,60",
                expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=30),
            )
        )
    session.commit()
    session.close()

def get_instructor_info(instructor_id: str = "inst_001") -> Optional[InstructorDB]:
    session = SessionLocal()
    inst = session.query(InstructorDB).filter_by(id=instructor_id).first()
    session.close()
    return inst

def get_appointments_for_date(instructor_id: str, date_str: str) -> List[dict]:
    session = SessionLocal()
    target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    
    start_of_day = datetime.datetime.combine(target_date, datetime.time.min)
    end_of_day = datetime.datetime.combine(target_date, datetime.time.max)

    apps = session.query(AppointmentDB).filter(
        AppointmentDB.instructor_id == instructor_id,
        AppointmentDB.start_time >= start_of_day,
        AppointmentDB.end_time <= end_of_day,
        AppointmentDB.status.in_(ACTIVE_APPOINTMENT_STATUSES),
        AppointmentDB.cancelled_at.is_(None),
    ).all()

    result = [
        {
            "id": a.id,
            "title": a.title,
            "start_time": a.start_time,
            "end_time": a.end_time,
            "location_address": a.location_address,
            "lat": a.lat,
            "lng": a.lng,
            "is_break": a.is_break
        }
        for a in apps
    ]
    session.close()
    return result

class ScheduleConflictError(Exception):
    pass

class IdempotencyKeyConflictError(Exception):
    pass

def get_appointment_by_idempotency_key(idempotency_key: Optional[str]) -> Optional[dict]:
    if not idempotency_key:
        return None
    session = SessionLocal()
    appointment = session.query(AppointmentDB).filter_by(idempotency_key=idempotency_key).first()
    result = _appointment_to_dict(appointment) if appointment else None
    session.close()
    return result

def _appointment_to_dict(appointment: Optional[AppointmentDB]) -> Optional[dict]:
    if not appointment:
        return None
    return {
        "id": appointment.id,
        "instructor_id": appointment.instructor_id,
        "client_id": appointment.client_id,
        "title": appointment.title,
        "start_time": appointment.start_time,
        "end_time": appointment.end_time,
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
    if end_time <= start_time:
        raise ValueError("Appointment end time must be after start time.")

    session = SessionLocal()
    try:
        if idempotency_key:
            existing = session.query(AppointmentDB).filter_by(idempotency_key=idempotency_key).first()
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
            existing = session.query(AppointmentDB).filter_by(idempotency_key=idempotency_key).first()
            if existing:
                return existing.id
        raise
    finally:
        session.close()
