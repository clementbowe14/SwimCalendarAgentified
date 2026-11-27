from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from uuid import uuid4

from app import db, timezones
from app.api_errors import InviteExpired, InviteNotFound, LessonRequestNotFound
from app.config import demo_data_access_enabled_from_env
from app.models import InviteTokenDB, LessonRequestDB
from app.provider_settings import ProviderLocation, ProviderNotFound, load_provider_configuration
from sqlalchemy.exc import IntegrityError, OperationalError


@dataclass(frozen=True)
class InviteContext:
    token: str
    instructor_id: str
    instructor_name: str
    business_name: str
    location_label: str
    service_name: Optional[str]
    service_location: Optional[ProviderLocation]
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
    recurrence: str
    recurrence_count: int
    original_slot_offer_id: Optional[str]
    expires_at: datetime
    created_at: datetime


DEMO_INVITE_TOKEN = "demo-invite-token"


def resolve_invite(token: str) -> InviteContext:
    session = db.SessionLocal()
    try:
        invite = require_active_invite(session, token)
        try:
            provider = load_provider_configuration(invite.instructor_id)
        except ProviderNotFound as exc:
            raise InviteNotFound() from exc
        instructor_name = provider.display_name or "Provider"
        allowed_durations = _parse_durations(invite.allowed_durations_mins)
        preferred_duration = provider.default_duration_mins or allowed_durations[0]
        if preferred_duration not in allowed_durations:
            allowed_durations = (*allowed_durations, preferred_duration)
        return InviteContext(
            token=invite.token,
            instructor_id=invite.instructor_id,
            instructor_name=instructor_name,
            business_name=provider.business_name or instructor_name,
            location_label="Client pool",
            service_name=provider.service_name,
            service_location=provider.service_location,
            preferred_duration_mins=preferred_duration,
            allowed_durations_mins=allowed_durations,
            expires_at=invite.expires_at,
        )
    finally:
        session.close()


def require_active_invite(
    session,
    token: str,
    *,
    instructor_id: str | None = None,
    lock: bool = False,
) -> InviteTokenDB:
    query = session.query(InviteTokenDB).filter_by(token=token)
    if instructor_id is not None:
        query = query.filter_by(instructor_id=instructor_id)
    invite = (query.with_for_update() if lock else query).first()
    if invite is None or invite.revoked_at is not None or not demo_invite_is_enabled(invite):
        raise InviteNotFound()
    if invite.expires_at <= datetime.utcnow():
        raise InviteExpired()
    return invite


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
    recurrence: str = "single",
    recurrence_count: int = 1,
    original_slot_offer_id: Optional[str] = None,
    expires_at: Optional[datetime] = None,
) -> LessonRequestRecord:
    selected_slot_start = timezones.to_utc_storage(selected_slot_start)
    selected_slot_end = timezones.to_utc_storage(selected_slot_end)
    if recurrence == "single":
        recurrence_count = 1
    elif recurrence_count < 1:
        recurrence_count = 4
    session = db.SessionLocal()
    try:
        invite = session.query(InviteTokenDB).filter_by(token=invite_token).first()
        capped_expires_at = _request_expires_at(invite, expires_at)
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
                    or existing.recurrence != recurrence
                    or existing.recurrence_count != recurrence_count
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
            recurrence=recurrence,
            recurrence_count=recurrence_count,
            original_slot_offer_id=original_slot_offer_id,
            expires_at=capped_expires_at,
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
        if (
            not request
            or request.invite is None
            or not demo_invite_is_enabled(request.invite)
            or request.invite.revoked_at is not None
        ):
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


def demo_invite_is_enabled(invite: InviteTokenDB) -> bool:
    return not invite.is_demo or demo_data_access_enabled_from_env()


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
        recurrence=request.recurrence,
        recurrence_count=request.recurrence_count,
        original_slot_offer_id=request.original_slot_offer_id,
        expires_at=request.expires_at,
        created_at=request.created_at,
    )


def _parse_durations(raw: str) -> tuple[int, ...]:
    durations = tuple(int(value.strip()) for value in raw.split(",") if value.strip())
    return durations or (60,)


def _request_expires_at(invite: Optional[InviteTokenDB], requested: Optional[datetime]) -> datetime:
    expires_at = requested or (datetime.utcnow() + timedelta(hours=24))
    if invite is not None and invite.expires_at < expires_at:
        return invite.expires_at
    return expires_at
