"""Transactional notification ledger and explicitly invoked provider processing."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
from typing import Literal, Protocol, TYPE_CHECKING
from uuid import uuid4
from urllib.parse import urlencode, urlsplit

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import and_, or_

from app import db
from app.api_errors import ApiException, IdempotencyConflict, InviteExpired, InviteNotFound
from app.models import (
    AppointmentDB,
    ClientDB,
    InstructorDB,
    InviteIntakeDB,
    InviteTokenDB,
    LessonRequestDB,
    NotificationEventDB,
)

if TYPE_CHECKING:
    from app.api_schemas import CoachEmailDeliveryResponse, NotificationStatus


def utcnow():
    return datetime.now(timezone.utc)


KNOWN_RESEND_FAILURES = {"provider_retryable", "provider_permanent", "attempts_exhausted"}
EDITABLE_RECIPIENT_FAILURES = {"invalid_recipient", "missing_recipient"}
FAILURE_ISSUES = {
    "invalid_recipient": "invalid_address",
    "missing_recipient": "missing_address",
    "provider_retryable": "temporary_failure",
    "provider_permanent": "permanent_failure",
    "outcome_unknown": "delivery_uncertain",
    "attempts_exhausted": "retries_exhausted",
}


def _recipient(channel, raw):
    raw = raw.strip() if raw else None
    if not raw:
        return None
    if channel == "sms":
        return raw if re.fullmatch(r"\+[1-9][0-9]{1,14}", raw) else None
    try:
        return validate_email(raw, check_deliverability=False).normalized
    except EmailNotValidError:
        return None


def _enqueue(session, invite_token, instructor_id, kind, logical_key, context, request_id=None):
    intake = session.query(InviteIntakeDB).filter_by(invite_token=invite_token).first()
    events = []
    for channel, field in (("sms", "guardian_phone"), ("email", "guardian_email")):
        raw = getattr(intake, field, None)
        recipient = _recipient(channel, raw)
        event = NotificationEventDB(
            instructor_id=instructor_id, invite_token=invite_token, request_id=request_id,
            kind=kind, channel=channel, logical_key=logical_key,
            status="queued" if recipient else "skipped", recipient=recipient,
            template_context=context, next_attempt_at=utcnow() if recipient else None,
            failure_code=None if recipient else ("invalid_recipient" if raw and raw.strip() else "missing_recipient"),
        )
        session.add(event)
        events.append(event)
    session.flush()
    return events


def enqueue_confirmation(session, lesson_request, appointments):
    occurrences = [{"start_iso": a.start_time.replace(tzinfo=timezone.utc).isoformat(),
                    "end_iso": a.end_time.replace(tzinfo=timezone.utc).isoformat()}
                   for a in sorted(appointments, key=lambda a: a.start_time)]
    return _enqueue(session, lesson_request.invite_token, lesson_request.instructor_id,
                    "confirmation", f"confirmation:{lesson_request.id}",
                    {"title": "Swim lesson", "address": lesson_request.client_address, "occurrences": occurrences}, lesson_request.id)


def _statuses(events):
    from app.api_schemas import NotificationStatus
    return [NotificationStatus(kind=e.kind, channel=e.channel, status=e.status, updated_at=e.updated_at)
            for e in sorted(events, key=lambda e: (e.channel != "sms", e.created_at))]


def public_notifications(session, request_id):
    return _statuses(session.query(NotificationEventDB).filter_by(request_id=request_id, kind="confirmation").all())


def _invite_context(invite_token):
    origin = os.getenv("SWIM_CLIENT_INVITE_ORIGIN", "").strip()
    if not origin and os.getenv("SWIM_ENV", "development").lower() not in {"production", "staging"}:
        origin = "http://127.0.0.1:3001"
    try:
        url = urlsplit(origin)
        valid_scheme = url.scheme == "https" or (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"})
        valid = valid_scheme and url.hostname and not (url.username or url.password or url.query or url.fragment) and url.path in {"", "/"}
        _ = url.port
    except ValueError:
        valid = False
    if not valid:
        raise ApiException("NOTIFICATION_CONFIGURATION_ERROR", "The client invitation origin is not configured.", 503)
    return {"title": "Swim lesson invitation", "invite_url": origin.rstrip("/") + "/?" + urlencode({"invite_token": invite_token})}


@dataclass(frozen=True)
class CoachInviteNotificationResult:
    notifications: list["NotificationStatus"]
    email_delivery: "CoachEmailDeliveryResponse | None"


def enqueue_invite_for_coach(instructor_id, invite_token, idempotency_key):
    logical_key = "invite:" + hashlib.sha256(json.dumps([instructor_id, "invite", idempotency_key]).encode()).hexdigest()
    with db.SessionLocal() as session:
        session.query(InstructorDB).filter_by(id=instructor_id).with_for_update().first()
        invite = session.query(InviteTokenDB).filter_by(token=invite_token, instructor_id=instructor_id).first()
        if invite is None or invite.revoked_at is not None:
            raise InviteNotFound()
        if invite.expires_at <= datetime.utcnow():
            raise InviteExpired()
        events = session.query(NotificationEventDB).filter_by(instructor_id=instructor_id, logical_key=logical_key).all()
        if events and any(e.invite_token != invite_token for e in events):
            raise IdempotencyConflict()
        if not events:
            events = _enqueue(session, invite_token, instructor_id, "invite", logical_key, _invite_context(invite_token))
        result = CoachInviteNotificationResult(
            notifications=_statuses(events),
            email_delivery=_project_email_event(
                session,
                next((event for event in events if event.channel == "email"), None),
            ),
        )
        session.commit()
        return result


def enqueue_invite(instructor_id, invite_token, idempotency_key):
    return enqueue_invite_for_coach(instructor_id, invite_token, idempotency_key).notifications


@dataclass(frozen=True)
class _DeliverySourceGraph:
    invite: InviteTokenDB | None
    intake: InviteIntakeDB | None
    lesson_request: LessonRequestDB | None
    appointments: list[AppointmentDB]
    client: ClientDB | None
    client_link_valid: bool


def list_coach_email_deliveries(instructor_id: str) -> list["CoachEmailDeliveryResponse"]:
    with db.SessionLocal() as session:
        events = (
            session.query(NotificationEventDB)
            .filter_by(instructor_id=instructor_id, channel="email")
            .order_by(NotificationEventDB.updated_at.desc(), NotificationEventDB.id.desc())
            .all()
        )
        return [_project_email_event(session, event) for event in events]


def coach_email_delivery_for_request(
    session,
    instructor_id: str,
    request_id: str,
) -> "CoachEmailDeliveryResponse | None":
    event = (
        session.query(NotificationEventDB)
        .filter_by(
            instructor_id=instructor_id,
            request_id=request_id,
            kind="confirmation",
            channel="email",
        )
        .order_by(NotificationEventDB.created_at.desc(), NotificationEventDB.id.desc())
        .first()
    )
    return _project_email_event(session, event)


def resend_coach_email_delivery(
    instructor_id: str,
    delivery_id: str,
) -> "CoachEmailDeliveryResponse":
    with db.SessionLocal() as session:
        event = _lock_email_event(session, instructor_id, delivery_id)
        graph = _load_source_graph(session, event, for_update=True)
        source_state = _source_state(event, graph)
        if source_state != "active" or not graph.client_link_valid:
            raise _delivery_conflict()

        if event.status in {"queued", "processing", "accepted", "delivered"}:
            return _coach_delivery_response(event, graph, source_state)
        if (
            event.status != "failed"
            or event.failure_code not in KNOWN_RESEND_FAILURES
            or not event.recipient
        ):
            raise _delivery_conflict()

        now = utcnow()
        event.status = "queued"
        event.attempt_count = 0
        event.next_attempt_at = now
        event.lease_expires_at = None
        event.attempt_token = None
        event.failure_code = None
        event.provider_message_id = None
        event.updated_at = now
        session.flush()
        response = _coach_delivery_response(event, graph, source_state)
        session.commit()
        return response


def correct_coach_email_delivery_contact(
    instructor_id: str,
    delivery_id: str,
    email: str,
) -> "CoachEmailDeliveryResponse":
    normalized = _recipient("email", email)
    if normalized is None:
        raise ApiException("INVALID_EMAIL_ADDRESS", "Enter a valid email address.", 422)

    with db.SessionLocal() as session:
        event = _lock_email_event(session, instructor_id, delivery_id)
        graph = _load_source_graph(session, event, for_update=True)
        source_state = _source_state(event, graph)
        if (
            source_state != "active"
            or not graph.client_link_valid
            or graph.intake is None
        ):
            raise _delivery_conflict()

        if event.status == "queued" and event.recipient == normalized:
            return _coach_delivery_response(event, graph, source_state)
        if (
            event.status != "skipped"
            or event.attempt_count != 0
            or event.failure_code not in EDITABLE_RECIPIENT_FAILURES
        ):
            raise _delivery_conflict()

        now = utcnow()
        event.recipient = normalized
        event.status = "queued"
        event.attempt_count = 0
        event.next_attempt_at = now
        event.lease_expires_at = None
        event.attempt_token = None
        event.failure_code = None
        event.provider_message_id = None
        event.updated_at = now

        graph.intake.guardian_email = normalized
        graph.intake.updated_at = datetime.utcnow()
        if graph.lesson_request is not None:
            graph.lesson_request.client_email = normalized
            graph.lesson_request.updated_at = datetime.utcnow()
        if graph.client is not None:
            graph.client.email = normalized
            graph.client.updated_at = datetime.utcnow()

        session.flush()
        response = _coach_delivery_response(event, graph, source_state)
        session.commit()
        return response


def _lock_email_event(session, instructor_id: str, delivery_id: str) -> NotificationEventDB:
    event = (
        session.query(NotificationEventDB)
        .filter_by(id=delivery_id, instructor_id=instructor_id, channel="email")
        .with_for_update()
        .first()
    )
    if event is None:
        raise ApiException("EMAIL_DELIVERY_NOT_FOUND", "The email delivery could not be found.", 404)
    return event


def _load_source_graph(
    session,
    event: NotificationEventDB,
    *,
    for_update: bool,
) -> _DeliverySourceGraph:
    invite_query = session.query(InviteTokenDB).filter_by(
        token=event.invite_token,
        instructor_id=event.instructor_id,
    )
    invite = _first(invite_query, for_update)

    intake = None
    if invite is not None:
        intake = _first(
            session.query(InviteIntakeDB).filter_by(invite_token=invite.token),
            for_update,
        )

    lesson_request = None
    if event.request_id is not None:
        lesson_request = _first(
            session.query(LessonRequestDB).filter_by(
                id=event.request_id,
                invite_token=event.invite_token,
                instructor_id=event.instructor_id,
            ),
            for_update,
        )

    appointments: list[AppointmentDB] = []
    if lesson_request is not None:
        appointment_query = (
            session.query(AppointmentDB)
            .filter_by(
                request_id=lesson_request.id,
                instructor_id=event.instructor_id,
            )
            .order_by(AppointmentDB.id.asc())
        )
        if for_update:
            appointment_query = appointment_query.with_for_update()
        appointments = appointment_query.all()

    invite_client_id = invite.client_id if invite is not None else None
    request_client_id = lesson_request.client_id if lesson_request is not None else None
    client_link_valid = not (
        invite_client_id is not None
        and request_client_id is not None
        and invite_client_id != request_client_id
    )
    client_id = request_client_id or invite_client_id
    client = None
    if client_link_valid and client_id is not None:
        client = _first(
            session.query(ClientDB).filter_by(
                id=client_id,
                instructor_id=event.instructor_id,
            ),
            for_update,
        )
        client_link_valid = client is not None

    return _DeliverySourceGraph(
        invite=invite,
        intake=intake,
        lesson_request=lesson_request,
        appointments=appointments,
        client=client,
        client_link_valid=client_link_valid,
    )


def _first(query, for_update: bool):
    return (query.with_for_update() if for_update else query).first()


def _project_email_event(session, event) -> "CoachEmailDeliveryResponse | None":
    if event is None:
        return None
    graph = _load_source_graph(session, event, for_update=False)
    return _coach_delivery_response(event, graph, _source_state(event, graph))


def _coach_delivery_response(
    event: NotificationEventDB,
    graph: _DeliverySourceGraph,
    source_state: str,
) -> "CoachEmailDeliveryResponse":
    from app.api_schemas import CoachEmailDeliveryResponse

    active = source_state == "active" and graph.client_link_valid
    can_resend = (
        active
        and event.status == "failed"
        and event.failure_code in KNOWN_RESEND_FAILURES
        and bool(event.recipient)
    )
    can_edit_contact = (
        active
        and graph.intake is not None
        and event.status == "skipped"
        and event.attempt_count == 0
        and event.failure_code in EDITABLE_RECIPIENT_FAILURES
    )
    client_name = (
        graph.lesson_request.client_name
        if graph.lesson_request is not None
        else graph.intake.client_name
        if graph.intake is not None
        else "Client"
    )
    updated_at = event.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return CoachEmailDeliveryResponse(
        id=event.id,
        kind=event.kind,
        status=event.status,
        updated_at=updated_at,
        client_id=graph.client.id if graph.client is not None else None,
        client_name=client_name,
        request_token=(
            graph.lesson_request.request_token
            if graph.lesson_request is not None
            else None
        ),
        issue=FAILURE_ISSUES.get(event.failure_code),
        source_state=source_state,
        can_resend=can_resend,
        can_edit_contact=can_edit_contact,
    )


def _source_state(event: NotificationEventDB, graph: _DeliverySourceGraph) -> str:
    if event.kind == "invite":
        invite = graph.invite
        if (
            invite is None
            or invite.revoked_at is not None
            or invite.expires_at <= datetime.utcnow()
        ):
            return "invite_inactive"
        return "active"
    return "active" if _confirmation_source_is_active(event, graph) else "appointment_inactive"


def _confirmation_source_is_active(
    event: NotificationEventDB,
    graph: _DeliverySourceGraph,
) -> bool:
    lesson_request = graph.lesson_request
    if lesson_request is None or lesson_request.status != "confirmed":
        return False
    context = event.template_context if isinstance(event.template_context, dict) else {}
    occurrences = context.get("occurrences")
    address = context.get("address")
    if (
        not isinstance(occurrences, list)
        or not occurrences
        or not isinstance(address, str)
        or not graph.appointments
    ):
        return False
    try:
        expected = sorted(
            (
                _snapshot_instant(item["start_iso"]),
                _snapshot_instant(item["end_iso"]),
            )
            for item in occurrences
            if isinstance(item, dict)
        )
    except (KeyError, TypeError, ValueError):
        return False
    if len(expected) != len(occurrences) or len(expected) != len(graph.appointments):
        return False
    if any(
        appointment.status != "scheduled"
        or appointment.cancelled_at is not None
        or appointment.event_type != "lesson"
        or appointment.is_break
        or appointment.location_address != address
        for appointment in graph.appointments
    ):
        return False
    actual = sorted(
        (
            _storage_instant(appointment.start_time),
            _storage_instant(appointment.end_time),
        )
        for appointment in graph.appointments
    )
    return actual == expected


def _snapshot_instant(value) -> datetime:
    if not isinstance(value, str):
        raise TypeError("Notification occurrence instants must be strings.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _storage_instant(parsed)


def _storage_instant(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _delivery_conflict() -> ApiException:
    return ApiException(
        "EMAIL_DELIVERY_NOT_ACTIONABLE",
        "The email delivery is no longer actionable.",
        409,
    )


@dataclass(frozen=True)
class NotificationMessage:
    kind: str
    channel: str
    recipient: str
    template_version: str
    context: dict


@dataclass(frozen=True)
class ProviderResult:
    outcome: Literal["accepted", "delivered", "retryable_failure", "permanent_failure", "unknown"]
    provider_message_id: str | None = None


class NotificationProvider(Protocol):
    """Adapters must enforce timeout_seconds on their transport, including retries."""
    supports_idempotent_send: bool
    timeout_seconds: float

    def send(self, message: NotificationMessage, idempotency_key: str) -> ProviderResult: ...


@dataclass(frozen=True)
class Claim:
    event_id: str
    attempt_token: str
    message: NotificationMessage


def claim_due(session_factory, provider, now=None):
    if provider is None:
        return None
    now = now or utcnow()
    with session_factory() as session:
        event = session.query(NotificationEventDB).filter(or_(
            and_(NotificationEventDB.status == "queued", or_(
                NotificationEventDB.next_attempt_at.is_(None), NotificationEventDB.next_attempt_at <= now)),
            and_(NotificationEventDB.status == "processing", NotificationEventDB.lease_expires_at <= now),
        )).order_by(NotificationEventDB.created_at, NotificationEventDB.id).with_for_update(skip_locked=True).first()
        if event is None:
            return None
        if event.attempt_count >= 5 or (event.status == "processing" and not provider.supports_idempotent_send):
            event.failure_code = "outcome_unknown" if event.status == "processing" else "attempts_exhausted"
            event.status = "failed"
            event.lease_expires_at = None
            event.attempt_token = None
            event.updated_at = now
            session.commit()
            return None
        if event.status == "processing":
            event.status = "queued"
            event.failure_code = "outcome_unknown"
            event.next_attempt_at = now + timedelta(seconds=60 * 2 ** (event.attempt_count - 1))
            event.lease_expires_at = None
            event.attempt_token = None
            event.updated_at = now
            session.commit()
            return None
        event.status = "processing"
        event.attempt_count += 1
        event.attempt_token = str(uuid4())
        event.lease_expires_at = now + timedelta(seconds=120)
        event.next_attempt_at = None
        event.updated_at = now
        claim = Claim(event.id, event.attempt_token, NotificationMessage(event.kind, event.channel, event.recipient, event.template_version, event.template_context))
        session.commit()
        return claim


def complete_claim(session_factory, provider, claim, result, now=None):
    now = now or utcnow()
    with session_factory() as session:
        event = session.query(NotificationEventDB).filter_by(id=claim.event_id).with_for_update().one()
        if event.status != "processing" or event.attempt_token != claim.attempt_token:
            return False
        outcome = result.outcome
        if outcome in {"accepted", "delivered"}:
            event.status = outcome
            event.provider_message_id = result.provider_message_id
            event.failure_code = None
        elif outcome == "retryable_failure" or (outcome == "unknown" and provider.supports_idempotent_send):
            event.status = "queued" if event.attempt_count < 5 else "failed"
            event.failure_code = "outcome_unknown" if outcome == "unknown" else "provider_retryable"
            if event.status == "queued":
                event.next_attempt_at = now + timedelta(seconds=60 * 2 ** (event.attempt_count - 1))
        else:
            event.status = "failed"
            event.failure_code = "provider_permanent" if outcome == "permanent_failure" else "outcome_unknown"
        event.lease_expires_at = None
        event.attempt_token = None
        event.updated_at = now
        session.commit()
        return True


def process_one(provider=None, *, session_factory=None, now=None):
    """Process at most one claim; unconfigured deployments perform no attempts."""
    if provider is None:
        return False
    if not 0 < provider.timeout_seconds <= 30:
        raise ValueError("Provider transport timeout must be at most 30 seconds.")
    factory = session_factory or db.SessionLocal
    claim = claim_due(factory, provider, now)
    if claim is None:
        return False
    try:
        result = provider.send(claim.message, claim.event_id)
        if not isinstance(result, ProviderResult):
            result = ProviderResult("unknown")
    except Exception:
        result = ProviderResult("unknown")
    complete_claim(factory, provider, claim, result, now)
    return True
