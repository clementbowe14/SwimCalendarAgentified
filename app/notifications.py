"""Transactional notification ledger and explicitly invoked provider processing."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
from typing import Literal, Protocol
from uuid import uuid4
from urllib.parse import urlencode, urlsplit

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import and_, or_

from app import db
from app.api_errors import ApiException, IdempotencyConflict, InviteExpired, InviteNotFound
from app.models import InstructorDB, InviteIntakeDB, InviteTokenDB, NotificationEventDB


def utcnow():
    return datetime.now(timezone.utc)


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


def enqueue_invite(instructor_id, invite_token, idempotency_key):
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
        result = _statuses(events)
        session.commit()
        return result


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
