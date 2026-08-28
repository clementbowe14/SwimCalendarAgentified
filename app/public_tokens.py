from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from app.api_errors import InviteExpired, InviteNotFound


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
    request_token: str
    invite_token: str
    client_name: str
    selected_slot_start: datetime
    status: str
    created_at: datetime


DEMO_INVITE_TOKEN = "demo-invite-token"

_INVITES: dict[str, InviteContext] = {
    DEMO_INVITE_TOKEN: InviteContext(
        token=DEMO_INVITE_TOKEN,
        instructor_id="inst_001",
        instructor_name="Coach Sarah",
        business_name="Riley Swim",
        location_label="Instructor facility or client pool",
        preferred_duration_mins=60,
        allowed_durations_mins=(30, 45, 60),
        expires_at=datetime.utcnow() + timedelta(days=30),
    )
}

_REQUESTS: dict[str, LessonRequestRecord] = {}


def resolve_invite(token: str) -> InviteContext:
    invite = _INVITES.get(token)
    if not invite:
        raise InviteNotFound()
    if invite.expires_at <= datetime.utcnow():
        raise InviteExpired()
    return invite


def create_lesson_request(
    invite_token: str,
    client_name: str,
    selected_slot_start: datetime,
) -> LessonRequestRecord:
    record = LessonRequestRecord(
        request_token=f"req_{uuid4().hex[:12]}",
        invite_token=invite_token,
        client_name=client_name,
        selected_slot_start=selected_slot_start,
        status="pending",
        created_at=datetime.utcnow(),
    )
    _REQUESTS[record.request_token] = record
    return record


def resolve_lesson_request(request_token: str) -> LessonRequestRecord:
    record = _REQUESTS.get(request_token)
    if not record:
        raise InviteNotFound()
    return record


def clear_lesson_requests() -> None:
    _REQUESTS.clear()
