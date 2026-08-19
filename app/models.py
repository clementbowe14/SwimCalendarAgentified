from datetime import datetime, time
from typing import List, Optional
from pydantic import BaseModel, Field
from sqlalchemy import Column, String, Float, DateTime, Boolean, ForeignKey, Integer, create_engine
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

class InstructorDB(Base):
    __tablename__ = 'instructors'

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    main_address = Column(String, nullable=False)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    work_start_time = Column(String, default="08:00") # HH:MM format
    work_end_time = Column(String, default="18:00")   # HH:MM format

    appointments = relationship("AppointmentDB", back_populates="instructor")

class AppointmentDB(Base):
    __tablename__ = 'appointments'

    id = Column(String, primary_key=True)
    instructor_id = Column(String, ForeignKey('instructors.id'), nullable=False)
    client_id = Column(String, nullable=False) # 'client' or 'instructor_break'
    title = Column(String, nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    location_address = Column(String, nullable=False)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    is_break = Column(Boolean, default=False)

    instructor = relationship("InstructorDB", back_populates="appointments")

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
