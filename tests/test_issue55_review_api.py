from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient

from app import db, timezones
import app.client_invites as client_invites
from app.models import (
    AppointmentDB,
    InstructorDB,
    InviteIntakeDB,
    InviteSlotOfferDB,
    LessonRequestDB,
    ProviderWorkingHoursDB,
    TimeSlotDTO,
)


INSTRUCTOR_ID = "inst_001"
INVITE_TOKEN = "invite_sofia_0826"


def _storage(local_value: dt.datetime, timezone_name: str) -> dt.datetime:
    return timezones.to_utc_storage(
        timezones.validate_local_datetime(local_value, timezone_name)
    )


def test_stale_facility_alternative_is_rejected_before_any_mutation(
    authenticated_api_client: TestClient,
):
    timezone_name = "America/Chicago"
    requested_date = dt.date(2027, 1, 5)
    start = _storage(dt.datetime(2027, 1, 5, 10, 0), timezone_name)
    end = start + dt.timedelta(minutes=45)
    with db.SessionLocal() as session:
        instructor = session.get(InstructorDB, INSTRUCTOR_ID)
        assert instructor is not None
        stale_address = instructor.service_address
        stale_lat = instructor.service_lat
        stale_lng = instructor.service_lng
        assert stale_address is not None and stale_lat is not None and stale_lng is not None

        lesson_request = LessonRequestDB(
            id="lr_review_stale_alternative",
            request_token="req_review_stale_alternative",
            invite_token=INVITE_TOKEN,
            instructor_id=INSTRUCTOR_ID,
            client_name="Facility client",
            client_address=stale_address,
            requested_date=requested_date,
            slot_start=start,
            slot_end=end,
            duration_mins=45,
            travel_time_mins=0,
            buffer_mins=10,
            distance_miles=0.0,
            location_type="facility",
            lat=stale_lat,
            lng=stale_lng,
            status="pending",
            recurrence="single",
            recurrence_count=1,
            expires_at=dt.datetime.utcnow() + dt.timedelta(days=1),
        )
        offer = InviteSlotOfferDB(
            id="slot_review_stale_alternative",
            invite_token=INVITE_TOKEN,
            instructor_id=INSTRUCTOR_ID,
            client_name=lesson_request.client_name,
            client_address=stale_address,
            requested_date=requested_date,
            slot_start=start,
            slot_end=end,
            duration_mins=45,
            location_type="facility",
            location_address=stale_address,
            lat=stale_lat,
            lng=stale_lng,
            travel_time_mins=0,
            buffer_mins=10,
            distance_miles=0.0,
            recurrence="single",
            recurrence_count=1,
            recurrence_available=True,
            offer_kind="search",
            expires_at=dt.datetime.utcnow() + dt.timedelta(hours=2),
        )
        session.add_all([lesson_request, offer])
        instructor.service_address = "900 Current Service Way"
        instructor.service_lat = 30.5005
        instructor.service_lng = -97.5005
        session.commit()

    response = authenticated_api_client.post(
        "/api/coach/lesson-requests/req_review_stale_alternative/alternatives",
        json={"slot_ids": ["slot_review_stale_alternative"]},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SCHEDULING_CONFLICT"
    with db.SessionLocal() as session:
        request = session.get(LessonRequestDB, "lr_review_stale_alternative")
        assert request is not None
        assert request.status == "pending"
        assert request.alternatives_sent_at is None
        assert request.client_notification_status is None
        assert (
            session.query(InviteSlotOfferDB)
            .filter_by(
                lesson_request_id=request.id,
                offer_kind="alternative",
            )
            .count()
            == 0
        )


def test_alternative_workflow_requires_provider_setup_before_status_mutation(
    authenticated_api_client: TestClient,
):
    with db.SessionLocal() as session:
        instructor = session.get(InstructorDB, INSTRUCTOR_ID)
        assert instructor is not None
        instructor.base_address = None
        instructor.base_lat = None
        instructor.base_lng = None
        session.commit()

    response = authenticated_api_client.post(
        "/api/coach/lesson-requests/req_sofia_pending_0820/alternatives",
        json={},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PROVIDER_SETUP_REQUIRED"
    assert response.json()["error"]["details"] == {"missing_fields": ["base_address"]}
    with db.SessionLocal() as session:
        request = session.get(LessonRequestDB, "req_sofia_pending_0820")
        assert request is not None
        assert request.status == "pending"
        assert request.alternatives_sent_at is None
        assert request.client_notification_status is None


def test_public_envelope_reprojects_legacy_facility_intake_from_current_service(
    api_client: TestClient,
):
    with db.SessionLocal() as session:
        instructor = session.get(InstructorDB, INSTRUCTOR_ID)
        intake = session.query(InviteIntakeDB).filter_by(invite_token=INVITE_TOKEN).one()
        assert instructor is not None
        instructor.service_address = "725 Authoritative Service Ave"
        instructor.service_lat = 30.725
        instructor.service_lng = -97.725
        intake.location_type = "facility"
        intake.location_address = "Client supplied stale facility"
        intake.lat = 1.25
        intake.lng = 2.5
        intake.is_instructor_facility = True
        session.commit()

    response = api_client.get(f"/api/public/client-invites/{INVITE_TOKEN}")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["intake"]["location_type"] == "facility"
    assert payload["intake"]["location"] == {
        "address": "725 Authoritative Service Ave",
        "lat": 30.725,
        "lng": -97.725,
        "is_instructor_facility": True,
    }
    assert "Client supplied stale facility" not in response.text
    assert "1.25" not in response.text
    with db.SessionLocal() as session:
        persisted = session.query(InviteIntakeDB).filter_by(invite_token=INVITE_TOKEN).one()
        assert persisted.location_address == "Client supplied stale facility"
        assert persisted.lat == 1.25
        assert persisted.lng == 2.5


def test_public_weekly_request_rejects_nonexistent_dst_occurrence_without_insert(
    api_client: TestClient,
    monkeypatch,
):
    timezone_name = "America/New_York"
    base_local = dt.datetime(2027, 2, 21, 2, 30)
    start = _storage(base_local, timezone_name)
    with db.SessionLocal() as session:
        instructor = session.get(InstructorDB, INSTRUCTOR_ID)
        assert instructor is not None
        instructor.timezone = timezone_name
        session.add(
            InviteSlotOfferDB(
                id="slot_review_dst_public",
                invite_token=INVITE_TOKEN,
                instructor_id=INSTRUCTOR_ID,
                client_name="DST client",
                client_address="55 Home Pool Rd",
                requested_date=base_local.date(),
                slot_start=start,
                slot_end=start + dt.timedelta(minutes=45),
                duration_mins=45,
                location_type="home",
                location_address="55 Home Pool Rd",
                lat=30.55,
                lng=-97.55,
                travel_time_mins=10,
                buffer_mins=10,
                distance_miles=3.0,
                recurrence="weekly",
                recurrence_count=4,
                recurrence_available=True,
                offer_kind="search",
                expires_at=dt.datetime.utcnow() + dt.timedelta(hours=2),
            )
        )
        before_count = session.query(LessonRequestDB).count()
        session.commit()

    def available_slot_for_date(
        *, requested_date: dt.date, duration_mins: int, **_kwargs
    ) -> list[TimeSlotDTO]:
        local_start = timezones.validate_local_datetime(
            dt.datetime.combine(requested_date, dt.time(2, 30)),
            timezone_name,
        )
        return [
            TimeSlotDTO(
                start_time=local_start,
                end_time=local_start + dt.timedelta(minutes=duration_mins),
                travel_time_mins=10,
                buffer_mins=10,
                distance_miles=3.0,
            )
        ]

    monkeypatch.setattr(client_invites, "_find_slots", available_slot_for_date)

    response = api_client.post(
        f"/api/public/client-invites/{INVITE_TOKEN}/lesson-requests",
        json={"slot_id": "slot_review_dst_public"},
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"] == {
        "code": "INVALID_SCHEDULING_REQUEST",
        "message": "The selected lesson time is no longer available.",
    }
    with db.SessionLocal() as session:
        assert session.query(LessonRequestDB).count() == before_count


def test_coach_confirmation_rejects_ambiguous_dst_recurrence_without_mutation(
    authenticated_api_client: TestClient,
):
    timezone_name = "America/New_York"
    base_local = dt.datetime(2027, 10, 17, 1, 30)
    start = _storage(base_local, timezone_name)
    end = start + dt.timedelta(minutes=45)
    with db.SessionLocal() as session:
        instructor = session.get(InstructorDB, INSTRUCTOR_ID)
        assert instructor is not None
        instructor.timezone = timezone_name
        session.add(
            LessonRequestDB(
                id="lr_review_dst_coach",
                request_token="req_review_dst_coach",
                invite_token=INVITE_TOKEN,
                instructor_id=INSTRUCTOR_ID,
                client_name="DST client",
                client_address="55 Home Pool Rd",
                requested_date=base_local.date(),
                slot_start=start,
                slot_end=end,
                duration_mins=45,
                travel_time_mins=10,
                buffer_mins=10,
                distance_miles=3.0,
                location_type="home",
                lat=30.55,
                lng=-97.55,
                status="pending",
                recurrence="weekly",
                recurrence_count=4,
                expires_at=dt.datetime.utcnow() + dt.timedelta(days=365),
            )
        )
        session.commit()

    response = authenticated_api_client.post(
        "/api/coach/lesson-requests/req_review_dst_coach/confirm",
        json={},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SCHEDULING_CONFLICT"
    assert "Send the client new times" in response.json()["error"]["message"]
    with db.SessionLocal() as session:
        request = session.get(LessonRequestDB, "lr_review_dst_coach")
        assert request is not None
        assert request.status == "pending"
        assert request.slot_start == start
        assert request.slot_end == end
        assert request.validation_failure_code is None
        assert session.query(AppointmentDB).filter_by(request_id=request.id).count() == 0


def test_command_center_compatibility_fields_use_only_explicit_provider_config(
    authenticated_api_client: TestClient,
):
    with db.SessionLocal() as session:
        instructor = session.get(InstructorDB, INSTRUCTOR_ID)
        assert instructor is not None
        explicit_service = (
            instructor.service_address,
            instructor.service_lat,
            instructor.service_lng,
        )
        instructor.main_address = "Legacy mirror must not appear"
        instructor.lat = 1.0
        instructor.lng = 2.0
        instructor.work_start_time = "01:00"
        instructor.work_end_time = "02:00"
        instructor.timezone = "Not/A_Real_Zone"
        for row in session.query(ProviderWorkingHoursDB).filter_by(
            instructor_id=INSTRUCTOR_ID
        ):
            row.enabled = True
            row.start_time = dt.time(9, 0)
            row.end_time = dt.time(16, 0)
        session.commit()

    response = authenticated_api_client.get(
        "/api/coach/command-center/snapshot",
        params={"date": "2026-08-20"},
    )

    assert response.status_code == 200, response.text
    instructor_payload = response.json()["instructor"]
    assert instructor_payload["main_address"] == explicit_service[0]
    assert instructor_payload["lat"] == explicit_service[1]
    assert instructor_payload["lng"] == explicit_service[2]
    assert instructor_payload["work_start_time"] == "09:00"
    assert instructor_payload["work_end_time"] == "16:00"
    assert instructor_payload["timezone"] == "Not/A_Real_Zone"
    assert instructor_payload["setup_complete"] is False
    assert "timezone" in instructor_payload["setup_missing_fields"]
    assert "Legacy mirror must not appear" not in response.text
