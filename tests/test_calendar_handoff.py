from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from icalendar import Calendar
import pytest

from app import db
from app.api_errors import CalendarLinksUnavailable
from app.calendar_handoff import calendar_ics, calendar_links
from app.models import AppointmentDB, InstructorDB, InviteTokenDB, LessonRequestDB, NotificationEventDB


UTC = timezone.utc


def appointment(start=None, **overrides):
    start = start or datetime(2028, 2, 29, 23, 45)
    fields = dict(
        id=f"app_{uuid4()}", calendar_uid=str(uuid4()), instructor_id="inst_private",
        client_id="client_private", title="PRIVATE NAME AND NOTES",
        start_time=start, end_time=start + timedelta(minutes=45),
        location_address="Public pool", lat=0, lng=0, is_break=False,
        event_type="lesson", status="scheduled", created_at=datetime(2026, 1, 2, 3, 4, 5),
    )
    fields.update(overrides)
    return AppointmentDB(**fields)


def events(content):
    return Calendar.from_ical(content).walk("VEVENT")


@pytest.mark.parametrize("address", [
    "Pool, north; entrance\\gate",
    "Pool\nBEGIN:VEVENT\nSUMMARY:Injected\nEND:VEVENT",
    "Pool\r\nATTENDEE:mailto:private@example.com\r\nX-HEADER: injected",
    "Pool\rATTENDEE:mailto:private@example.com\nRoom\r\nEntry",
    "Pool&text=Injected&dates=bad#fragment?x=1",
    "Piscine " + "\u00e9\U0001f30a\u6cf3" * 100,
])
def test_calendar_text_roundtrips_without_injection(address):
    item = appointment(location_address=address)
    content = calendar_ics([item])
    parsed = events(content)
    assert len(parsed) == 1
    event = parsed[0]
    normalized = address.replace("\r\n", "\n").replace("\r", "\n")
    assert str(event["LOCATION"]) == normalized
    assert str(event["SUMMARY"]) == "Swim lesson"
    assert str(event["DESCRIPTION"]) == "Confirmed swim lesson."
    assert str(event["UID"]) == item.calendar_uid
    UUID(str(event["UID"]))
    assert event.decoded("DTSTAMP") == item.created_at.replace(tzinfo=UTC)
    assert set(event) == {"UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY", "LOCATION", "DESCRIPTION"}
    assert "PRIVATE" not in content and "app_" not in content and "client_" not in content
    raw = content.encode("utf-8")
    assert raw.endswith(b"\r\n") and b"\n" not in raw.replace(b"\r\n", b"")
    assert b"\r" not in raw.replace(b"\r\n", b"")
    for line in raw.split(b"\r\n"):
        assert len(line) <= 75
        line.decode("utf-8")
    links = calendar_links("req_secret", [item])
    url = urlsplit(links.google)
    assert (url.scheme, url.netloc, url.fragment) == ("https", "calendar.google.com", "")
    query = parse_qs(url.query)
    assert query == {
        "action": ["TEMPLATE"], "text": ["Swim lesson"],
        "dates": ["20280229T234500Z/20280301T003000Z"],
        "location": [normalized], "details": ["Confirmed swim lesson."],
    }
    assert "req_secret" not in links.google and "req_secret" not in content


@pytest.mark.parametrize("local", [
    datetime(2026, 3, 8, 1, 45, tzinfo=ZoneInfo("America/New_York")),
    datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=0),
    datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=1),
    datetime(2028, 2, 29, 23, 45, tzinfo=ZoneInfo("Asia/Kathmandu")),
    datetime(2028, 2, 29, 23, 45, tzinfo=ZoneInfo("Asia/Kolkata")),
    datetime(2028, 2, 29, 23, 45, tzinfo=ZoneInfo("Pacific/Chatham")),
])
def test_both_formats_preserve_stored_instants_across_time_boundaries(local):
    start = local.astimezone(UTC)
    end = start + timedelta(minutes=45)
    item = appointment(start=start.replace(tzinfo=None), end_time=end.replace(tzinfo=None))
    event = events(calendar_ics([item]))[0]
    assert event.decoded("DTSTART") == start
    assert event.decoded("DTEND") == end
    assert event.decoded("DTEND") - event.decoded("DTSTART") == timedelta(minutes=45)
    link = calendar_links("req_secret", [item]).google_events[0]
    assert link.start_iso == start and link.end_iso == end
    assert parse_qs(urlsplit(link.url).query)["dates"] == [
        f"{start:%Y%m%dT%H%M%SZ}/{end:%Y%m%dT%H%M%SZ}"
    ]


def test_weekly_exports_are_chronological_snapshots_with_stable_uids():
    items = [appointment(start=datetime(2026, 10, 1) + timedelta(weeks=i)) for i in range(4)]
    links = calendar_links("req_secret", list(reversed(items)))
    assert len(links.google_events) == 4
    assert links.google == links.google_events[0].url
    assert links.apple == links.ics == "/api/public/calendar/req_secret.ics"
    before = events(calendar_ics(list(reversed(items))))
    after = events(calendar_ics(items[1:]))
    assert [str(e["UID"]) for e in before] == [i.calendar_uid for i in items]
    assert [str(e["UID"]) for e in after] == [i.calendar_uid for i in items[1:]]
    assert all("RRULE" not in e for e in before)
    assert calendar_ics(items) == calendar_ics(items)


@pytest.mark.parametrize("minutes", [0, -1])
def test_invalid_durations_have_sanitized_errors(minutes):
    item = appointment()
    item.end_time = item.start_time + timedelta(minutes=minutes)
    for render in [lambda: calendar_ics([item]), lambda: calendar_links("secret", [item])]:
        with pytest.raises(CalendarLinksUnavailable) as error:
            render()
        assert error.value.code == "CALENDAR_LINKS_UNAVAILABLE"
        assert "secret" not in error.value.message


def test_empty_exports_are_unavailable():
    for render in [lambda: calendar_ics([]), lambda: calendar_links("secret", [])]:
        with pytest.raises(CalendarLinksUnavailable):
            render()


@pytest.fixture()
def booked(api_client):
    with db.SessionLocal() as session:
        invite = session.query(InviteTokenDB).first()
        start = datetime(2028, 2, 29, 23, 45)
        request = LessonRequestDB(
            id=str(uuid4()), request_token=f"req_{uuid4().hex}", invite_token=invite.token,
            instructor_id=invite.instructor_id, client_id=invite.client_id,
            client_name="PRIVATE NAME", client_address="Public pool", lat=0, lng=0,
            requested_date=start.date(), slot_start=start, slot_end=start + timedelta(minutes=45),
            duration_mins=45, location_type="home", status="confirmed", recurrence="weekly",
            recurrence_count=4, expires_at=datetime(2020, 1, 1),
        )
        session.add(request)
        session.flush()
        items = [appointment(start=start + timedelta(weeks=i), request_id=request.id,
                             instructor_id=invite.instructor_id) for i in range(4)]
        session.add_all(items)
        session.commit()
        return request.request_token, request.id, invite.token, [i.id for i in items]


@pytest.mark.parametrize("cancellation_field", ["status", "cancelled_at"])
def test_postgres_filtering_cancellation_and_timezone_changes(api_client, booked, cancellation_field):
    token, request_id, _, ids = booked
    path = f"/api/public/lesson-requests/{token}/calendar-links"
    original = api_client.get(path)
    assert original.status_code == 200, original.text
    download = original.json()["ics"]
    baseline = api_client.get(download)
    assert baseline.headers["content-disposition"] == 'attachment; filename="swim-lessons.ics"'
    assert baseline.headers["content-type"] == "text/calendar; charset=utf-8"
    assert baseline.headers["cache-control"] == "private, no-store"
    assert baseline.headers["referrer-policy"] == "no-referrer"
    before = events(baseline.content)
    with db.SessionLocal() as session:
        request = session.get(LessonRequestDB, request_id)
        session.get(InstructorDB, request.instructor_id).timezone = "Asia/Kathmandu"
        for index, (event_type, status, is_break) in enumerate([
            ("hold", "hold", False), ("block", "scheduled", True),
            ("lesson", "completed", False), ("lesson", "cancelled", False),
        ]):
            session.add(appointment(start=datetime(2028, 4, 1) + timedelta(days=index),
                                    request_id=request_id, instructor_id=request.instructor_id,
                                    event_type=event_type, status=status, is_break=is_break))
        session.add(appointment(start=datetime(2028, 5, 1), instructor_id=request.instructor_id))
        session.commit()
    assert api_client.get(download).content == baseline.content
    assert api_client.get(path).json() == original.json()
    with db.SessionLocal() as session:
        setattr(session.get(AppointmentDB, ids[0]), cancellation_field,
                "cancelled" if cancellation_field == "status" else datetime(2026, 1, 1))
        session.commit()
    remaining = events(api_client.get(download).content)
    assert [str(e["UID"]) for e in remaining] == [str(e["UID"]) for e in before[1:]]
    assert len(api_client.get(path).json()["google_events"]) == 3
    with db.SessionLocal() as session:
        for identifier in ids[1:]:
            setattr(session.get(AppointmentDB, identifier), cancellation_field,
                    "cancelled" if cancellation_field == "status" else datetime(2026, 1, 1))
        session.commit()
    for url in [path, download]:
        response = api_client.get(url)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CALENDAR_LINKS_UNAVAILABLE"
    assert api_client.get(f"/api/public/lesson-requests/{token}").json()["confirmation"] is None


@pytest.mark.parametrize("status", ["pending", "alternatives_sent", "declined", "cancelled"])
def test_postgres_nonconfirmed_requests_never_export(api_client, booked, status):
    token, request_id, _, _ = booked
    with db.SessionLocal() as session:
        session.get(LessonRequestDB, request_id).status = status
        session.commit()
    assert api_client.get(f"/api/public/calendar/{token}.ics").status_code == 409
    assert api_client.get(f"/api/public/lesson-requests/{token}/calendar-links").status_code == 409
    assert api_client.get(f"/api/public/lesson-requests/{token}").json()["confirmation"] is None


def test_postgres_expired_confirmed_invites_work_but_revoked_and_unknown_fail(api_client, booked):
    token, _, invite_token, _ = booked
    paths = ["/api/public/requests/{}", "/api/public/lesson-requests/{}",
             "/api/public/lesson-requests/{}/calendar-links", "/api/public/calendar/{}.ics"]
    with db.SessionLocal() as session:
        session.get(InviteTokenDB, invite_token).expires_at = datetime(2020, 1, 1)
        session.commit()
    for path in paths:
        assert api_client.get(path.format(token)).status_code == 200
    with db.SessionLocal() as session:
        session.get(InviteTokenDB, invite_token).revoked_at = datetime.utcnow()
        session.commit()
    for path in paths:
        for value in [token, "req_unknown"]:
            response = api_client.get(path.format(value))
            assert response.status_code == 404, response.text
            assert response.json()["error"]["code"] == "LESSON_REQUEST_NOT_FOUND"
            assert response.headers["cache-control"] == "private, no-store"
            assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize("status", ["queued", "processing", "accepted", "delivered", "failed", "skipped"])
def test_postgres_public_notification_order_states_and_privacy(api_client, booked, status):
    token, request_id, invite_token, _ = booked
    paths = [f"/api/public/requests/{token}", f"/api/public/lesson-requests/{token}"]
    for path in paths:
        assert api_client.get(path).json()["notifications"] == []
    with db.SessionLocal() as session:
        instructor_id = session.get(LessonRequestDB, request_id).instructor_id
        for channel in ["email", "sms"]:
            session.add(NotificationEventDB(
                instructor_id=instructor_id, invite_token=invite_token, request_id=request_id,
                kind="confirmation", channel=channel, logical_key="confirmation", status=status,
                recipient="PRIVATE RECIPIENT", template_context={"secret": "PRIVATE CONTEXT"},
                failure_code="PRIVATE ERROR", provider_message_id="PRIVATE PROVIDER",
            ))
        session.add(NotificationEventDB(
            instructor_id=instructor_id, invite_token=invite_token, kind="invite", channel="sms",
            logical_key="invite", status="delivered",
        ))
        session.commit()
    for path in paths:
        response = api_client.get(path)
        assert response.status_code == 200, response.text
        notifications = response.json()["notifications"]
        assert [n["channel"] for n in notifications] == ["sms", "email"]
        for item in notifications:
            assert set(item) == {"kind", "channel", "status", "updated_at"}
            assert item["kind"] == "confirmation" and item["status"] == status
            assert "PRIVATE" not in str(item)
    with db.SessionLocal() as session:
        assert session.query(NotificationEventDB).count() == 3
