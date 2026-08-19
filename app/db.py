import datetime
from typing import Optional, List
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import Base, InstructorDB, AppointmentDB, LocationInfo

DATABASE_URL = "sqlite:///swim_scheduler.db"

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)

def init_db():
    Base.metadata.create_all(bind=engine)
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
        AppointmentDB.end_time <= end_of_day
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

def save_appointment(
    instructor_id: str,
    client_id: str,
    title: str,
    start_time: datetime.datetime,
    end_time: datetime.datetime,
    location_address: str,
    lat: float,
    lng: float
) -> str:
    session = SessionLocal()
    app_id = f"app_{int(datetime.datetime.now().timestamp())}"
    new_app = AppointmentDB(
        id=app_id,
        instructor_id=instructor_id,
        client_id=client_id,
        title=title,
        start_time=start_time,
        end_time=end_time,
        location_address=location_address,
        lat=lat,
        lng=lng,
        is_break=False
    )
    session.add(new_app)
    session.commit()
    session.close()
    return app_id
