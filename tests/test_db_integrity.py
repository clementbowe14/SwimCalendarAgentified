import datetime

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app import db, tools
from app.models import AppointmentDB, LessonRequestDB
from app.public_tokens import DEMO_INVITE_TOKEN, create_lesson_request
from tests.db_test_utils import TEST_DATABASE_URL, reset_postgres_database


@pytest.fixture()
def isolated_db():
    reset_postgres_database()
    yield
    db.configure_database()


def test_service_rejects_overlapping_active_appointments(isolated_db):
    with pytest.raises(db.ScheduleConflictError):
        db.save_appointment(
            instructor_id="inst_001",
            client_id="client_overlap",
            title="Lesson with Overlap",
            start_time=datetime.datetime(2026, 8, 20, 9, 30),
            end_time=datetime.datetime(2026, 8, 20, 10, 30),
            location_address="450 North Blvd, Austin, TX",
            lat=30.3300,
            lng=-97.7200,
        )


def test_booking_idempotency_key_retry_returns_existing_appointment(isolated_db):
    kwargs = {
        "instructor_id": "inst_001",
        "client_id": "client_retry",
        "client_name": "Retry Client",
        "start_time_iso": "2026-08-21T10:00:00",
        "duration_mins": 60,
        "location_address": "450 North Blvd, Austin, TX",
        "idempotency_key": "booking-key-123",
    }

    first = tools.book_swim_lesson(**kwargs)
    second = tools.book_swim_lesson(**kwargs)

    session = db.SessionLocal()
    try:
        stored = session.query(AppointmentDB).filter_by(idempotency_key="booking-key-123").all()
    finally:
        session.close()

    assert first["appointment_id"] == second["appointment_id"]
    assert len(stored) == 1


def test_public_request_idempotency_key_retry_returns_existing_request(isolated_db):
    slot_start = datetime.datetime(2026, 8, 21, 10, 0)
    slot_end = slot_start + datetime.timedelta(minutes=45)

    first = create_lesson_request(
        invite_token=DEMO_INVITE_TOKEN,
        instructor_id="inst_001",
        client_name="Retry Request",
        client_address="450 North Blvd, Austin, TX",
        requested_date=datetime.date(2026, 8, 21),
        selected_slot_start=slot_start,
        selected_slot_end=slot_end,
        duration_mins=45,
        travel_time_mins=0,
        buffer_mins=10,
        distance_miles=0.0,
        location_type="home",
        lat=30.3300,
        lng=-97.7200,
        idempotency_key="request-key-123",
    )
    second = create_lesson_request(
        invite_token=DEMO_INVITE_TOKEN,
        instructor_id="inst_001",
        client_name="Retry Request",
        client_address="450 North Blvd, Austin, TX",
        requested_date=datetime.date(2026, 8, 21),
        selected_slot_start=slot_start,
        selected_slot_end=slot_end,
        duration_mins=45,
        travel_time_mins=0,
        buffer_mins=10,
        distance_miles=0.0,
        location_type="home",
        lat=30.3300,
        lng=-97.7200,
        idempotency_key="request-key-123",
    )

    session = db.SessionLocal()
    try:
        stored = session.query(LessonRequestDB).filter_by(idempotency_key="request-key-123").all()
    finally:
        session.close()

    assert first.request_token == second.request_token
    assert len(stored) == 1


def test_alembic_migration_creates_entities_and_postgres_overlap_constraint():
    reset_postgres_database(seed=False)
    database_url = TEST_DATABASE_URL
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)

    command.current(config, check_heads=True)

    engine = create_engine(database_url)
    inspector = inspect(engine)
    assert {"instructors", "appointments", "clients", "invite_tokens", "lesson_requests"}.issubset(
        set(inspector.get_table_names())
    )

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO instructors (id, name, main_address, lat, lng, work_start_time, work_end_time)
                VALUES ('inst_001', 'Coach Sarah', '100 Main St, Austin, TX', 30.2672, -97.7431, '08:00', '17:00')
                """
            )
        )

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO appointments
                    (id, instructor_id, client_id, title, start_time, end_time, location_address, lat, lng, is_break, event_type, status)
                VALUES
                    ('app_1', 'inst_001', 'client_a', 'Lesson A', '2026-08-21 10:00:00', '2026-08-21 11:00:00',
                     '450 North Blvd, Austin, TX', 30.33, -97.72, false, 'lesson', 'scheduled')
                """
            )
        )

    with pytest.raises(IntegrityError, match="exclude_appointments_active_overlap"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO appointments
                        (id, instructor_id, client_id, title, start_time, end_time, location_address, lat, lng, is_break, event_type, status)
                    VALUES
                        ('app_2', 'inst_001', 'client_b', 'Lesson B', '2026-08-21 10:30:00', '2026-08-21 11:30:00',
                         '800 West Lake Dr, Austin, TX', 30.27, -97.82, false, 'lesson', 'scheduled')
                    """
                )
            )
    engine.dispose()
