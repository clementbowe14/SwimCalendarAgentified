from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from icalendar import Calendar
import pytest

from app import db, timezones, tools
from app.models import AppointmentDB, ClientDB, InstructorDB, InviteTokenDB, LessonRequestDB


UTC = timezone.utc


def _calendar_events(content: bytes | str):
    return Calendar.from_ical(content).walk("VEVENT")


def test_finder_books_the_selected_client_in_the_expected_account(authenticated_api_client, monkeypatch):
    api = authenticated_api_client
    expected = {"X-Expected-Instructor-Id": "inst_001"}
    stale = {"X-Expected-Instructor-Id": "a-different-provider"}
    with db.SessionLocal.begin() as session:
        session.get(ClientDB, "client_alice").default_location_address = "  450 North Blvd, Austin, TX  "
    availability = api.post("/api/coach/availability", json={
        "client_name": "Selected client",
        "client_address": "450 North Blvd, Austin, TX",
        "requested_date": "2026-08-21",
        "duration_mins": 45,
    })
    assert availability.status_code == 200, availability.text
    payload = {
        "client_id": "client_alice",
        "client_name": "Name from an old client list",
        "client_address": "450 North Blvd, Austin, TX",
        "start_time": availability.json()["available_slots"][0]["start_iso"],
        "duration_mins": 45,
        "idempotency_key": "book-selected-client-by-id",
    }

    assert api.get("/api/coach/clients", headers=stale).status_code == 409
    assert api.post("/api/coach/bookings", json=payload, headers=stale).status_code == 409
    missing_client = api.post("/api/coach/bookings", json={**payload, "client_id": "unknown"}, headers=expected)
    assert missing_client.status_code == 404
    different_address = {**payload, "client_address": "800 West Lake Dr, Austin, TX"}
    stale_location = api.post("/api/coach/bookings", json=different_address, headers=expected)
    assert stale_location.status_code == 409
    assert stale_location.json()["error"]["code"] == "SCHEDULING_CONFLICT"

    geocode = Mock(return_value=(30.33, -97.72))
    monkeypatch.setattr(tools.routing_provider, "geocode", geocode)
    booked = api.post("/api/coach/bookings", json=payload, headers=expected)
    assert booked.status_code == 200, booked.text
    replayed = api.post(
        "/api/coach/bookings", json={**payload, "client_address": f"  {payload['client_address']}  "}, headers=expected,
    )
    assert replayed.json() == booked.json()
    different_location = api.post("/api/coach/bookings", json=different_address, headers=expected)
    assert different_location.status_code == 409
    assert different_location.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    geocode.assert_called_once_with(payload["client_address"])
    with db.SessionLocal() as session:
        appointment = session.get(AppointmentDB, booked.json()["appointment_id"])
        client = db.get_client_profile_for_instructor("inst_001", "client_alice")
        assert appointment.client_id == client["id"]
        assert appointment.title == f"Lesson with {client['name']}"
        assert (appointment.lat, appointment.lng) == (30.33, -97.72)


@pytest.mark.parametrize("change_during_recheck", [False, True])
def test_finder_booking_rejects_a_facility_change_for_the_selected_slot(
    authenticated_api_client, monkeypatch, change_during_recheck,
):
    api = authenticated_api_client
    with db.SessionLocal.begin() as session:
        session.get(ClientDB, "client_alice").location_mode = "service_location"
    availability = api.post("/api/coach/availability", json={
        "client_name": "Selected client",
        "client_address": "facility",
        "requested_date": "2026-08-21",
        "duration_mins": 45,
        "is_instructor_facility": True,
    })
    assert availability.status_code == 200, availability.text
    result = availability.json()

    def change_facility():
        with db.SessionLocal.begin() as session:
            session.get(InstructorDB, "inst_001").service_address = "New service location"

    if change_during_recheck:
        require_setup = tools.require_provider_setup

        def load_changed_provider(instructor_id):
            change_facility()
            return require_setup(instructor_id)

        monkeypatch.setattr(tools, "require_provider_setup", load_changed_provider)
    else:
        change_facility()
    key = "book-stale-finder-facility"
    response = api.post("/api/coach/bookings", json={
        "client_id": "client_alice", "client_name": "Selected client",
        "client_address": result["location"]["address"],
        "start_time": result["available_slots"][0]["start_iso"], "duration_mins": 45,
        "is_instructor_facility": True, "idempotency_key": key,
    }, headers={"X-Expected-Instructor-Id": "inst_001"})

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SCHEDULING_CONFLICT"
    assert db.get_appointment_by_idempotency_key("inst_001", key) is None


def _create_confirmed_series(*, count: int = 3) -> tuple[str, list[str]]:
    with db.SessionLocal() as session:
        invite = session.query(InviteTokenDB).filter_by(instructor_id="inst_001").first()
        request_id = f"req_db_{uuid4().hex}"
        request_token = f"req_{uuid4().hex}"
        start = datetime(2028, 2, 29, 23, 45)
        lesson_request = LessonRequestDB(
            id=request_id,
            request_token=request_token,
            invite_token=invite.token,
            instructor_id="inst_001",
            client_id=invite.client_id,
            client_name="Calendar Client",
            client_address="Public pool",
            lat=30.27,
            lng=-97.74,
            requested_date=start.date(),
            slot_start=start,
            slot_end=start + timedelta(minutes=45),
            duration_mins=45,
            location_type="home",
            status="confirmed",
            recurrence="weekly" if count > 1 else "single",
            recurrence_count=count,
            expires_at=datetime.utcnow() + timedelta(days=1),
        )
        session.add(lesson_request)
        session.flush()
        appointments = []
        for index in range(count):
            occurrence_start = start + timedelta(weeks=index)
            appointments.append(
                AppointmentDB(
                    id=f"app_{uuid4().hex[:12]}",
                    instructor_id="inst_001",
                    client_id="client_calendar",
                    request_id=request_id,
                    title="Private appointment title",
                    start_time=occurrence_start,
                    end_time=occurrence_start + timedelta(minutes=45),
                    location_address="Public pool",
                    lat=30.27,
                    lng=-97.74,
                    is_break=False,
                    event_type="lesson",
                    status="scheduled",
                )
            )
        session.add_all(appointments)
        session.commit()
        return request_id, [appointment.id for appointment in appointments]


def _persisted_instants(appointment_ids: list[str]) -> list[tuple[datetime, datetime, str]]:
    with db.SessionLocal() as session:
        appointments = (
            session.query(AppointmentDB)
            .filter(AppointmentDB.id.in_(appointment_ids))
            .order_by(AppointmentDB.start_time.asc(), AppointmentDB.calendar_uid.asc())
            .all()
        )
        return [
            (
                timezones.storage_to_aware_utc(appointment.start_time),
                timezones.storage_to_aware_utc(appointment.end_time),
                appointment.calendar_uid,
            )
            for appointment in appointments
        ]


def test_authenticated_series_handoff_uses_exact_persisted_instants(authenticated_api_client):
    _, appointment_ids = _create_confirmed_series(count=3)
    expected = _persisted_instants(appointment_ids)
    links_path = f"/api/coach/appointments/{appointment_ids[0]}/calendar-links"

    links_response = authenticated_api_client.get(links_path)

    assert links_response.status_code == 200, links_response.text
    assert links_response.headers["cache-control"] == "private, no-store"
    assert links_response.headers["referrer-policy"] == "no-referrer"
    links = links_response.json()
    assert links["apple"] == links["ics"] == (
        f"/api/coach/appointments/{appointment_ids[0]}/calendar.ics"
    )
    assert links["google"] == links["google_events"][0]["url"]
    assert len(links["google_events"]) == 3

    for google_event, (expected_start, expected_end, _) in zip(links["google_events"], expected):
        assert datetime.fromisoformat(google_event["start_iso"].replace("Z", "+00:00")) == expected_start
        assert datetime.fromisoformat(google_event["end_iso"].replace("Z", "+00:00")) == expected_end
        query = parse_qs(urlsplit(google_event["url"]).query)
        assert query["text"] == ["Appointment"]
        assert query["details"] == ["Confirmed appointment."]
        assert query["dates"] == [
            f"{expected_start:%Y%m%dT%H%M%SZ}/{expected_end:%Y%m%dT%H%M%SZ}"
        ]

    ics_response = authenticated_api_client.get(links["ics"])

    assert ics_response.status_code == 200, ics_response.text
    assert ics_response.headers["content-type"] == "text/calendar; charset=utf-8"
    assert ics_response.headers["content-disposition"] == 'attachment; filename="appointment.ics"'
    assert ics_response.headers["cache-control"] == "private, no-store"
    assert ics_response.headers["referrer-policy"] == "no-referrer"
    ics_events = _calendar_events(ics_response.content)
    assert len(ics_events) == 3
    for event, (expected_start, expected_end, expected_uid) in zip(ics_events, expected):
        assert event.decoded("DTSTART") == expected_start
        assert event.decoded("DTEND") == expected_end
        assert event.decoded("DTEND") - event.decoded("DTSTART") == timedelta(minutes=45)
        assert str(event["UID"]) == expected_uid
        assert str(event["SUMMARY"]) == "Appointment"
        assert str(event["DESCRIPTION"]) == "Confirmed appointment."
        assert "Private appointment title" not in ics_response.text


def test_cancelled_anchor_exports_only_active_confirmed_request_siblings(authenticated_api_client):
    request_id, appointment_ids = _create_confirmed_series(count=3)
    with db.SessionLocal() as session:
        session.get(AppointmentDB, appointment_ids[0]).status = "cancelled"
        session.commit()

    partial = authenticated_api_client.get(
        f"/api/coach/appointments/{appointment_ids[0]}/calendar-links"
    )

    assert partial.status_code == 200, partial.text
    assert len(partial.json()["google_events"]) == 2
    partial_events = _calendar_events(authenticated_api_client.get(partial.json()["ics"]).content)
    remaining_uids = {uid for _, _, uid in _persisted_instants(appointment_ids[1:])}
    assert {str(event["UID"]) for event in partial_events} == remaining_uids

    with db.SessionLocal() as session:
        session.get(AppointmentDB, appointment_ids[1]).status = "completed"
        session.get(AppointmentDB, appointment_ids[2]).cancelled_at = datetime.utcnow()
        session.commit()

    unavailable = authenticated_api_client.get(
        f"/api/coach/appointments/{appointment_ids[0]}/calendar-links"
    )

    assert unavailable.status_code == 409
    assert unavailable.json()["error"]["code"] == "CALENDAR_LINKS_UNAVAILABLE"
    assert unavailable.headers["cache-control"] == "private, no-store"
    assert unavailable.headers["referrer-policy"] == "no-referrer"
    with db.SessionLocal() as session:
        assert session.get(LessonRequestDB, request_id).status == "confirmed"


def test_request_parent_must_remain_confirmed(authenticated_api_client):
    request_id, appointment_ids = _create_confirmed_series(count=2)
    with db.SessionLocal() as session:
        session.get(LessonRequestDB, request_id).status = "cancelled"
        session.commit()

    response = authenticated_api_client.get(
        f"/api/coach/appointments/{appointment_ids[0]}/calendar-links"
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CALENDAR_LINKS_UNAVAILABLE"


def test_completed_anchor_locates_only_active_request_siblings(authenticated_api_client):
    _, appointment_ids = _create_confirmed_series(count=2)
    with db.SessionLocal() as session:
        session.get(AppointmentDB, appointment_ids[0]).status = "completed"
        session.commit()

    response = authenticated_api_client.get(
        f"/api/coach/appointments/{appointment_ids[0]}/calendar-links"
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["google_events"]) == 1
    expected = _persisted_instants([appointment_ids[1]])[0]
    event = payload["google_events"][0]
    assert datetime.fromisoformat(event["start_iso"].replace("Z", "+00:00")) == expected[0]
    assert datetime.fromisoformat(event["end_iso"].replace("Z", "+00:00")) == expected[1]


def test_coach_calendar_routes_require_auth_and_hide_cross_tenant_anchors(
    api_client,
    coach_auth_headers,
):
    _, appointment_ids = _create_confirmed_series(count=1)
    with db.SessionLocal() as session:
        session.add(
            InstructorDB(
                id="inst_calendar_other",
                name="Other Provider",
                main_address="1 Other St",
                lat=30.0,
                lng=-97.0,
                timezone="America/Chicago",
            )
        )
        other = AppointmentDB(
            id=f"app_{uuid4().hex[:12]}",
            instructor_id="inst_calendar_other",
            client_id="client_other",
            title="Other appointment",
            start_time=datetime(2028, 6, 1, 15, 0),
            end_time=datetime(2028, 6, 1, 16, 0),
            location_address="1 Other St",
            lat=30.0,
            lng=-97.0,
            is_break=False,
            event_type="lesson",
            status="scheduled",
        )
        session.add(other)
        session.commit()
        other_id = other.id

    for suffix in ("calendar-links", "calendar.ics"):
        unauthenticated = api_client.get(
            f"/api/coach/appointments/{appointment_ids[0]}/{suffix}"
        )
        assert unauthenticated.status_code == 401
        assert unauthenticated.headers["cache-control"] == "private, no-store"
        assert unauthenticated.headers["referrer-policy"] == "no-referrer"

        cross_tenant = api_client.get(
            f"/api/coach/appointments/{other_id}/{suffix}",
            headers=coach_auth_headers,
        )
        assert cross_tenant.status_code == 404
        assert cross_tenant.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert cross_tenant.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize(
    ("event_type", "status", "is_break", "cancelled_at"),
    [
        ("lesson", "cancelled", False, None),
        ("lesson", "scheduled", False, datetime.utcnow()),
        ("lesson", "completed", False, None),
        ("lesson", "hold", False, None),
        ("block", "scheduled", True, None),
    ],
)
def test_ineligible_direct_anchors_never_export(
    authenticated_api_client,
    event_type,
    status,
    is_break,
    cancelled_at,
):
    appointment_id = f"app_{uuid4().hex[:12]}"
    with db.SessionLocal() as session:
        session.add(
            AppointmentDB(
                id=appointment_id,
                instructor_id="inst_001",
                client_id="client_calendar",
                title="Private appointment title",
                start_time=datetime(2028, 7, 1, 15, 0),
                end_time=datetime(2028, 7, 1, 16, 0),
                location_address="Public pool",
                lat=30.27,
                lng=-97.74,
                is_break=is_break,
                event_type=event_type,
                status=status,
                cancelled_at=cancelled_at,
            )
        )
        session.commit()

    for suffix in ("calendar-links", "calendar.ics"):
        response = authenticated_api_client.get(
            f"/api/coach/appointments/{appointment_id}/{suffix}"
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CALENDAR_LINKS_UNAVAILABLE"


def test_direct_booking_returns_persisted_handoff_on_idempotent_retry(authenticated_api_client):
    availability = authenticated_api_client.post(
        "/api/coach/availability",
        json={
            "client_name": "Calendar Booking",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-21",
            "duration_mins": 45,
            "is_instructor_facility": False,
        },
    )
    assert availability.status_code == 200, availability.text
    slot_start = availability.json()["available_slots"][0]["start_iso"]
    payload = {
        "client_name": "Calendar Booking",
        "client_address": "450 North Blvd, Austin, TX",
        "start_time": slot_start,
        "duration_mins": 45,
        "is_instructor_facility": False,
        "idempotency_key": "calendar-booking-retry-001",
    }

    first = authenticated_api_client.post("/api/coach/bookings", json=payload)
    second = authenticated_api_client.post("/api/coach/bookings", json=payload)

    assert first.status_code == second.status_code == 200
    assert first.json()["appointment_id"] == second.json()["appointment_id"]
    assert first.json()["calendar_links"] == second.json()["calendar_links"]
    assert first.json()["calendar_links"]["ics"].endswith(
        f"/{first.json()['appointment_id']}/calendar.ics"
    )
    expected = _persisted_instants([first.json()["appointment_id"]])[0]
    google_event = first.json()["calendar_links"]["google_events"][0]
    assert datetime.fromisoformat(google_event["start_iso"].replace("Z", "+00:00")) == expected[0]
    assert datetime.fromisoformat(google_event["end_iso"].replace("Z", "+00:00")) == expected[1]

    with db.SessionLocal() as session:
        session.get(AppointmentDB, first.json()["appointment_id"]).status = "cancelled"
        session.commit()

    cancelled_retry = authenticated_api_client.post("/api/coach/bookings", json=payload)

    assert cancelled_retry.status_code == 200
    assert cancelled_retry.json()["appointment_id"] == first.json()["appointment_id"]
    assert cancelled_retry.json()["calendar_links"] is None


def test_post_commit_handoff_failure_does_not_misreport_direct_booking(
    authenticated_api_client,
    monkeypatch,
):
    availability = authenticated_api_client.post(
        "/api/coach/availability",
        json={
            "client_name": "Persisted Without Handoff",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-22",
            "duration_mins": 45,
            "is_instructor_facility": False,
        },
    )
    assert availability.status_code == 200, availability.text
    monkeypatch.setattr(
        "app.api_services.get_coach_calendar_links",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("artifact failed")),
    )

    response = authenticated_api_client.post(
        "/api/coach/bookings",
        json={
            "client_name": "Persisted Without Handoff",
            "client_address": "450 North Blvd, Austin, TX",
            "start_time": availability.json()["available_slots"][0]["start_iso"],
            "duration_mins": 45,
            "is_instructor_facility": False,
            "idempotency_key": "calendar-booking-failure-001",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["calendar_links"] is None
    with db.SessionLocal() as session:
        assert session.get(AppointmentDB, response.json()["appointment_id"]) is not None
