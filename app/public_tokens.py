from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
from uuid import uuid4

from app import db, timezones
from app.api_errors import InviteExpired, InviteNotFound, LessonRequestNotFound
from app.models import InviteTokenDB, LessonRequestDB
from sqlalchemy.exc import IntegrityError, OperationalError


@dataclass(frozen=True)
class InviteContext:
    token: str
    instructor_id: str
    instructor_name: str
    business_name: str
    location_label: str
    preferred_duration_mins: int
    allowed_durations_mins: tuple[int, ...]
    expires_at: datetime


@dataclass(frozen=True)
class LessonRequestRecord:
    id: str
    request_token: str
    invite_token: str
    instructor_id: str
    client_name: str
    client_address: str
    requested_date: date
    selected_slot_start: datetime
    selected_slot_end: datetime
    duration_mins: int
    travel_time_mins: int
    buffer_mins: int
    distance_miles: float
    location_type: str
    status: str
    created_at: datetime


DEMO_INVITE_TOKEN = "demo-invite-token"


def resolve_invite(token: str) -> InviteContext:
    session = db.SessionLocal()
    try:
        invite = session.query(InviteTokenDB).filter_by(token=token).first()
        if not invite:
            if token == DEMO_INVITE_TOKEN:
                db.seed_data()
                invite = session.query(InviteTokenDB).filter_by(token=token).first()
            if not invite:
                raise InviteNotFound()
        if invite.revoked_at is not None:
            raise InviteNotFound()
        if invite.expires_at <= datetime.utcnow():
            raise InviteExpired()
        instructor_name = invite.instructor.name if invite.instructor else "Coach"
        return InviteContext(
            token=invite.token,
            instructor_id=invite.instructor_id,
            instructor_name=instructor_name,
            business_name=invite.business_name,
            location_label=invite.location_label,
            preferred_duration_mins=invite.preferred_duration_mins,
            allowed_durations_mins=_parse_durations(invite.allowed_durations_mins),
            expires_at=invite.expires_at,
        )
    finally:
        session.close()


def create_lesson_request(
    invite_token: str,
    instructor_id: str,
    client_name: str,
    client_address: str,
    requested_date: date,
    selected_slot_start: datetime,
    selected_slot_end: datetime,
    duration_mins: int,
    travel_time_mins: int,
    buffer_mins: int,
    distance_miles: float,
    location_type: str,
    lat: float,
    lng: float,
    idempotency_key: Optional[str] = None,
) -> LessonRequestRecord:
    selected_slot_start = timezones.to_utc_storage(selected_slot_start)
    selected_slot_end = timezones.to_utc_storage(selected_slot_end)
    session = db.SessionLocal()
    try:
        if idempotency_key:
            existing = session.query(LessonRequestDB).filter_by(idempotency_key=idempotency_key).first()
            if existing:
                if (
                    existing.invite_token != invite_token
                    or existing.instructor_id != instructor_id
                    or existing.client_name != client_name
                    or existing.client_address != client_address
                    or existing.slot_start != selected_slot_start
                    or existing.slot_end != selected_slot_end
                    or existing.duration_mins != duration_mins
                    or existing.travel_time_mins != travel_time_mins
                    or existing.buffer_mins != buffer_mins
                    or existing.distance_miles != distance_miles
                    or existing.location_type != location_type
                ):
                    raise db.IdempotencyKeyConflictError(
                        "Idempotency key was already used for a different request."
                    )
                return _lesson_request_record(existing)

        request = LessonRequestDB(
            id=f"lr_{uuid4().hex[:12]}",
            request_token=f"req_{uuid4().hex[:12]}",
            invite_token=invite_token,
            instructor_id=instructor_id,
            client_name=client_name,
            client_address=client_address,
            requested_date=requested_date,
            slot_start=selected_slot_start,
            slot_end=selected_slot_end,
            duration_mins=duration_mins,
            travel_time_mins=travel_time_mins,
            buffer_mins=buffer_mins,
            distance_miles=distance_miles,
            location_type=location_type,
            lat=lat,
            lng=lng,
            status="pending",
            idempotency_key=idempotency_key,
        )
        session.add(request)
        session.commit()
        return _lesson_request_record(request)
    except IntegrityError as exc:
        session.rollback()
        if idempotency_key:
            existing = session.query(LessonRequestDB).filter_by(idempotency_key=idempotency_key).first()
            if existing:
                return _lesson_request_record(existing)
        raise exc
    finally:
        session.close()


def resolve_lesson_request(request_token: str) -> LessonRequestRecord:
    session = db.SessionLocal()
    try:
        request = session.query(LessonRequestDB).filter_by(request_token=request_token).first()
        if not request:
            raise LessonRequestNotFound()
        return _lesson_request_record(request)
    finally:
        session.close()


def resolve_lesson_request_by_idempotency_key(idempotency_key: Optional[str]) -> Optional[LessonRequestRecord]:
    if not idempotency_key:
        return None
    session = db.SessionLocal()
    try:
        request = session.query(LessonRequestDB).filter_by(idempotency_key=idempotency_key).first()
        return _lesson_request_record(request) if request else None
    finally:
        session.close()


def clear_lesson_requests() -> None:
    session = db.SessionLocal()
    try:
        session.query(LessonRequestDB).delete()
        session.commit()
    except OperationalError:
        session.rollback()
    finally:
        session.close()


def _lesson_request_record(request: LessonRequestDB) -> LessonRequestRecord:
    return LessonRequestRecord(
        id=request.id,
        request_token=request.request_token,
        invite_token=request.invite_token,
        instructor_id=request.instructor_id,
        client_name=request.client_name,
        client_address=request.client_address,
        requested_date=request.requested_date,
        selected_slot_start=request.slot_start,
        selected_slot_end=request.slot_end,
        duration_mins=request.duration_mins,
        travel_time_mins=request.travel_time_mins,
        buffer_mins=request.buffer_mins,
        distance_miles=request.distance_miles,
        location_type=request.location_type,
        status=request.status,
        created_at=request.created_at,
    )


def _parse_durations(raw: str) -> tuple[int, ...]:
    durations = tuple(int(value.strip()) for value in raw.split(",") if value.strip())
    return durations or (60,)
