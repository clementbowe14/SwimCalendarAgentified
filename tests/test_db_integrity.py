import datetime
import os

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app import db, tools
from app.models import AppointmentDB, LessonRequestDB
from app.public_tokens import DEMO_INVITE_TOKEN, create_lesson_request
from app.routing import RouteEstimate
from app.routing_cache import DatabaseRouteEstimateCache, RouteCacheKey
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
            start_time=datetime.datetime.fromisoformat("2026-08-20T09:30:00-05:00"),
            end_time=datetime.datetime.fromisoformat("2026-08-20T10:30:00-05:00"),
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


def test_appointment_write_replays_only_with_matching_location(isolated_db):
    starts = datetime.datetime.fromisoformat("2026-08-21T10:00:00-05:00")
    booking = {
        "instructor_id": "inst_001", "client_id": "client_alice", "title": "Lesson with Alice",
        "start_time": starts, "end_time": starts + datetime.timedelta(minutes=45),
        "location_address": "450 North Blvd, Austin, TX", "lat": 30.3300, "lng": -97.7200,
        "idempotency_key": "booking-location-retry", "match_location": True,
    }
    first = db.save_appointment(**{**booking, "location_address": f"  {booking['location_address']}  "})
    assert db.save_appointment(**booking) == first
    for different in ({"location_address": "Different pool"}, {"lat": 30.5, "lng": -97.5}):
        with pytest.raises(db.IdempotencyKeyConflictError):
            db.save_appointment(**{**booking, **different})


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


def test_local_day_query_includes_cross_midnight_overlaps(isolated_db):
    db.save_appointment(
        instructor_id="inst_001",
        client_id="client_late",
        title="Late lesson",
        start_time=datetime.datetime.fromisoformat("2026-08-21T23:30:00-05:00"),
        end_time=datetime.datetime.fromisoformat("2026-08-22T00:30:00-05:00"),
        location_address="450 North Blvd, Austin, TX",
        lat=30.3300,
        lng=-97.7200,
    )
    db.save_appointment(
        instructor_id="inst_001",
        client_id="client_early",
        title="Early lesson",
        start_time=datetime.datetime.fromisoformat("2026-08-22T23:30:00-05:00"),
        end_time=datetime.datetime.fromisoformat("2026-08-23T00:30:00-05:00"),
        location_address="800 West Lake Dr, Austin, TX",
        lat=30.2700,
        lng=-97.8200,
    )

    appointments = db.get_local_appointments_for_date("inst_001", "2026-08-22")
    titles = {appointment["title"] for appointment in appointments}

    assert {"Late lesson", "Early lesson"}.issubset(titles)
    for appointment in appointments:
        assert appointment["start_time"].tzinfo is not None
        assert appointment["end_time"].tzinfo is not None


def test_route_cache_conflicting_expired_row_is_refreshed(isolated_db):
    cache = DatabaseRouteEstimateCache()
    key = RouteCacheKey(
        provider="mapbox",
        origin_key="30.26720,-97.74310",
        destination_key="30.33000,-97.72000",
        departure_bucket_start=datetime.datetime(2026, 8, 20, 14, 0),
        traffic_mode="live",
    )
    origin = (30.2672, -97.7431)
    destination = (30.33, -97.72)

    cache.set(
        key,
        RouteEstimate(distance_miles=1.0, travel_time_mins=5, duration_seconds=300, provider="mapbox"),
        origin,
        destination,
        expires_at=datetime.datetime(2026, 8, 20, 10, 0),
    )
    refreshed = cache.set(
        key,
        RouteEstimate(distance_miles=2.0, travel_time_mins=12, duration_seconds=720, provider="mapbox"),
        origin,
        destination,
        expires_at=datetime.datetime.utcnow() + datetime.timedelta(hours=1),
    )

    session = db.SessionLocal()
    try:
        rows = session.execute(
            text(
                """
                SELECT distance_miles, travel_time_mins
                FROM route_estimates
                WHERE provider = :provider
                  AND origin_key = :origin_key
                  AND destination_key = :destination_key
                  AND departure_bucket_start = :departure_bucket_start
                  AND traffic_mode = :traffic_mode
                """
            ),
            key.__dict__,
        ).all()
    finally:
        session.close()

    assert refreshed.travel_time_mins == 12
    assert len(rows) == 1
    assert rows[0].distance_miles == 2.0
    assert rows[0].travel_time_mins == 12


def test_alembic_migration_creates_entities_and_postgres_overlap_constraint():
    reset_postgres_database(seed=False)
    database_url = TEST_DATABASE_URL
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)

    engine = create_engine(database_url)
    script = ScriptDirectory.from_config(config)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        assert set(context.get_current_heads()) == set(script.get_heads())

    inspector = inspect(engine)
    assert {
        "instructors",
        "appointments",
        "clients",
        "invite_tokens",
        "lesson_requests",
        "route_estimates",
        "invite_intakes",
        "invite_slot_offers",
    }.issubset(set(inspector.get_table_names()))
    route_constraints = {
        constraint["name"] for constraint in inspector.get_unique_constraints("route_estimates")
    }
    assert "uq_route_estimates_cache_key" in route_constraints

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


def test_timezone_migration_converts_legacy_local_rows_and_restores_overlap_constraint():
    reset_postgres_database(seed=False)
    database_url = TEST_DATABASE_URL
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    previous_database_url = os.environ.get("SWIM_DATABASE_URL")
    os.environ["SWIM_DATABASE_URL"] = database_url
    try:
        command.downgrade(config, "20260830_0004")
    finally:
        if previous_database_url is None:
            os.environ.pop("SWIM_DATABASE_URL", None)
        else:
            os.environ["SWIM_DATABASE_URL"] = previous_database_url

    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO instructors (id, name, main_address, lat, lng, work_start_time, work_end_time)
                    VALUES ('inst_migration', 'Coach Legacy', '100 Main St, Austin, TX', 30.2672, -97.7431, '08:00', '17:00')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO clients (id, instructor_id, name)
                    VALUES ('client_migration', 'inst_migration', 'Legacy Client')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO invite_tokens
                        (token, instructor_id, client_id, business_name, location_label, preferred_duration_mins, allowed_durations_mins, expires_at)
                    VALUES
                        ('invite_migration', 'inst_migration', 'client_migration', 'Legacy Swim', 'Pool', 60, '60', '2026-12-01 00:00:00')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO appointments
                        (id, instructor_id, client_id, title, start_time, end_time, location_address, lat, lng, is_break, event_type, status)
                    VALUES
                        ('app_std', 'inst_migration', 'client_migration', 'Standard lesson', '2026-01-15 09:00:00', '2026-01-15 10:00:00',
                         'Pool', 30.33, -97.72, false, 'lesson', 'scheduled'),
                        ('app_dst', 'inst_migration', 'client_migration', 'DST lesson', '2026-08-21 09:00:00', '2026-08-21 10:00:00',
                         'Pool', 30.33, -97.72, false, 'lesson', 'scheduled')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO lesson_requests
                        (id, request_token, invite_token, instructor_id, client_id, client_name, client_address, lat, lng,
                         requested_date, slot_start, slot_end, duration_mins, travel_time_mins, buffer_mins, distance_miles,
                         location_type, status, created_at, updated_at)
                    VALUES
                        ('lr_migration', 'req_migration', 'invite_migration', 'inst_migration', 'client_migration',
                         'Legacy Client', 'Pool', 30.33, -97.72, '2026-08-21', '2026-08-21 11:00:00', '2026-08-21 12:00:00',
                         60, 0, 10, 0, 'home', 'accepted', '2026-08-19 11:00:00', '2026-08-19 11:00:00')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO invite_slot_offers
                        (id, invite_token, instructor_id, client_name, client_address, requested_date, slot_start, slot_end,
                         duration_mins, location_type, location_address, lat, lng, travel_time_mins, buffer_mins,
                         distance_miles, recurrence, recurrence_available, expires_at)
                    VALUES
                        ('slot_migration', 'invite_migration', 'inst_migration', 'Legacy Client', 'Pool', '2026-08-21',
                         '2026-08-21 13:00:00', '2026-08-21 14:00:00', 60, 'home', 'Pool', 30.33, -97.72, 0, 10,
                         0, 'single', true, '2026-08-21 13:30:00')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO route_estimates
                        (id, provider, origin_lat, origin_lng, destination_lat, destination_lng, origin_key,
                         destination_key, departure_bucket_start, traffic_mode, distance_miles, travel_time_mins,
                         duration_seconds, expires_at)
                    VALUES
                        ('route_legacy', 'mapbox', 30.0, -97.0, 30.1, -97.1, '30.00000,-97.00000',
                         '30.10000,-97.10000', '2026-08-21 09:00:00', 'live', 1.0, 10, 600, '2026-08-22 09:00:00')
                    """
                )
            )

        previous_database_url = os.environ.get("SWIM_DATABASE_URL")
        os.environ["SWIM_DATABASE_URL"] = database_url
        try:
            command.upgrade(config, "head")
        finally:
            if previous_database_url is None:
                os.environ.pop("SWIM_DATABASE_URL", None)
            else:
                os.environ["SWIM_DATABASE_URL"] = previous_database_url

        with engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT timezone FROM instructors WHERE id = 'inst_migration'
                    """
                )
            ).one()
            assert row.timezone == "America/Chicago"

            converted = connection.execute(
                text(
                    """
                    SELECT id, start_time, end_time
                    FROM appointments
                    WHERE id IN ('app_std', 'app_dst')
                    ORDER BY id
                    """
                )
            ).mappings().all()
            assert converted[0]["start_time"] == datetime.datetime(2026, 8, 21, 14, 0)
            assert converted[0]["end_time"] == datetime.datetime(2026, 8, 21, 15, 0)
            assert converted[1]["start_time"] == datetime.datetime(2026, 1, 15, 15, 0)
            assert converted[1]["end_time"] == datetime.datetime(2026, 1, 15, 16, 0)

            slot_rows = connection.execute(
                text(
                    """
                    SELECT
                        (SELECT slot_start FROM lesson_requests WHERE id = 'lr_migration') AS request_start,
                        (SELECT status FROM lesson_requests WHERE id = 'lr_migration') AS request_status,
                        (SELECT recurrence FROM lesson_requests WHERE id = 'lr_migration') AS request_recurrence,
                        (SELECT recurrence_count FROM lesson_requests WHERE id = 'lr_migration') AS request_recurrence_count,
                        (SELECT expires_at FROM lesson_requests WHERE id = 'lr_migration') AS request_expires_at,
                        (SELECT slot_start FROM invite_slot_offers WHERE id = 'slot_migration') AS offer_start,
                        (SELECT recurrence_count FROM invite_slot_offers WHERE id = 'slot_migration') AS offer_recurrence_count,
                        (SELECT COUNT(*) FROM route_estimates) AS route_count
                    """
                )
            ).one()
            assert slot_rows.request_start == datetime.datetime(2026, 8, 21, 16, 0)
            assert slot_rows.request_status == "confirmed"
            assert slot_rows.request_recurrence == "single"
            assert slot_rows.request_recurrence_count == 1
            assert slot_rows.request_expires_at == datetime.datetime(2026, 8, 20, 11, 0)
            assert slot_rows.offer_start == datetime.datetime(2026, 8, 21, 18, 0)
            assert slot_rows.offer_recurrence_count == 1
            assert slot_rows.route_count == 0

            overlap_constraint = connection.execute(
                text(
                    """
                    SELECT conname
                    FROM pg_constraint
                    WHERE conname = 'exclude_appointments_active_overlap'
                    """
                )
            ).scalar_one()
            assert overlap_constraint == "exclude_appointments_active_overlap"

        with pytest.raises(IntegrityError, match="exclude_appointments_active_overlap"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO appointments
                            (id, instructor_id, client_id, title, start_time, end_time, location_address, lat, lng, is_break, event_type, status)
                        VALUES
                            ('app_overlap', 'inst_migration', 'client_migration', 'Overlap', '2026-08-21 14:30:00', '2026-08-21 15:30:00',
                             'Pool', 30.33, -97.72, false, 'lesson', 'scheduled')
                        """
                    )
                )
    finally:
        engine.dispose()
