from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import datetime as dt
from threading import Barrier

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import db, timezones
from app.api import create_app
from app.api_errors import ApiException
from app.api_schemas import CoachLessonRequestConfirmRequest, ClientInviteAlternativeSelectionRequest
from app.api_services import confirm_coach_lesson_request
from app.client_invites import select_client_lesson_request_alternative
from app.auth import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    authenticate_coach,
    provision_provider_account,
)
from app.dependencies import get_current_coach
from app.models import (
    AppointmentDB,
    InstructorDB,
    InviteSlotOfferDB,
    LessonRequestDB,
    NotificationEventDB,
)


SEEDED_REQUEST_TOKEN = "req_sofia_pending_0820"


def test_coach_approves_pending_request_into_scheduled_appointment(
    authenticated_api_client,
    assert_public_payload_safe,
):
    api_client = authenticated_api_client
    locked_links = api_client.get(f"/api/public/lesson-requests/{SEEDED_REQUEST_TOKEN}/calendar-links")
    assert locked_links.status_code == 409
    assert locked_links.json()["error"]["code"] == "CALENDAR_LINKS_UNAVAILABLE"

    approval = api_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/approve")

    assert approval.status_code == 200
    approval_payload = approval.json()
    assert approval_payload["id"] == SEEDED_REQUEST_TOKEN
    assert approval_payload["status"] == "approved"
    assert approval_payload["appointment_id"].startswith("app_")

    schedule = api_client.get("/api/coach/schedule", params={"date": "2026-08-20"})
    assert approval_payload["appointment_id"] in {
        appointment["id"] for appointment in schedule.json()["appointments"]
    }

    public_status = api_client.get(f"/api/public/lesson-requests/{SEEDED_REQUEST_TOKEN}")
    assert public_status.status_code == 200
    assert public_status.json()["status"] == "confirmed"
    assert public_status.json()["confirmation"]["calendar_links"]["ics"].endswith(
        f"/calendar/{SEEDED_REQUEST_TOKEN}.ics"
    )
    assert_public_payload_safe(public_status.json())

    legacy_status = api_client.get(f"/api/public/requests/{SEEDED_REQUEST_TOKEN}")
    assert legacy_status.status_code == 200
    assert legacy_status.json()["status"] == "confirmed"
    assert_public_payload_safe(legacy_status.json())

    calendar_links = api_client.get(f"/api/public/lesson-requests/{SEEDED_REQUEST_TOKEN}/calendar-links")
    assert calendar_links.status_code == 200
    assert set(calendar_links.json()) == {"google", "apple", "ics", "google_events"}


def test_coach_request_approval_is_idempotent(authenticated_api_client):
    api_client = authenticated_api_client
    first = api_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/approve")
    second = api_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/approve")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["appointment_id"] == second.json()["appointment_id"]
    assert _appointment_count_for_request(SEEDED_REQUEST_TOKEN) == 1


def test_canonical_confirm_returns_confirmed_with_ordered_appointment_ids(authenticated_api_client):
    api_client = authenticated_api_client
    before = _request_slot_state(SEEDED_REQUEST_TOKEN)

    response = api_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/confirm", json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "confirmed"
    assert payload["appointment_id"] == payload["appointment_ids"][0]
    assert len(payload["appointment_ids"]) == 1
    assert payload["calendar_links"]["ics"] == (
        f"/api/coach/appointments/{payload['appointment_id']}/calendar.ics"
    )
    assert len(payload["calendar_links"]["google_events"]) == 1
    assert _request_status(SEEDED_REQUEST_TOKEN) == "confirmed"
    assert _confirmed_slot_offer_id(SEEDED_REQUEST_TOKEN) is None
    assert _appointment_times(SEEDED_REQUEST_TOKEN) == [
        (before["slot_start"], before["slot_end"])
    ]

    handoff = api_client.get(
        f"/api/coach/appointments/{payload['appointment_id']}/calendar-links"
    )
    assert handoff.status_code == 200
    assert handoff.json() == payload["calendar_links"]


def test_confirmation_persistence_survives_handoff_generation_failure(
    authenticated_api_client,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.api_services.get_coach_calendar_links",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("artifact failed")),
    )

    response = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/confirm",
        json={},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    assert response.json()["calendar_links"] is None
    assert _request_status(SEEDED_REQUEST_TOKEN) == "confirmed"
    assert _appointment_count_for_request(SEEDED_REQUEST_TOKEN) == 1


def test_cancelled_confirmation_retry_keeps_success_and_omits_handoff(authenticated_api_client):
    first = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/confirm",
        json={},
    )
    assert first.status_code == 200
    appointment_id = first.json()["appointment_id"]
    with db.SessionLocal() as session:
        session.get(AppointmentDB, appointment_id).status = "cancelled"
        session.commit()

    retry = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/confirm",
        json={},
    )

    assert retry.status_code == 200
    assert retry.json()["appointment_id"] == appointment_id
    assert retry.json()["calendar_links"] is None
    assert _request_status(SEEDED_REQUEST_TOKEN) == "confirmed"


def test_coach_request_approval_conflict_does_not_confirm_request(
    authenticated_api_client,
    assert_public_payload_safe,
):
    api_client = authenticated_api_client
    _add_conflicting_appointment(SEEDED_REQUEST_TOKEN)

    approval = api_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/approve")

    assert approval.status_code == 409
    assert approval.json()["error"]["code"] == "SCHEDULING_CONFLICT"
    assert _appointment_count_for_request(SEEDED_REQUEST_TOKEN) == 0
    assert _validation_failure_code(SEEDED_REQUEST_TOKEN) == "SLOT_CONFLICT"

    public_status = api_client.get(f"/api/public/lesson-requests/{SEEDED_REQUEST_TOKEN}")
    assert public_status.status_code == 200
    assert public_status.json()["status"] == "requested"
    assert public_status.json()["confirmation"] is None
    assert_public_payload_safe(public_status.json())


def test_expired_request_actions_are_rejected(authenticated_api_client):
    api_client = authenticated_api_client
    _expire_request(SEEDED_REQUEST_TOKEN)

    for path in ("approve", "confirm", "alternatives", "decline"):
        kwargs = {} if path == "approve" else {"json": {}}
        response = api_client.post(
            f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/{path}",
            **kwargs,
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "LESSON_REQUEST_EXPIRED"


def test_coach_can_request_alternatives_without_exposing_private_schedule(
    authenticated_api_client,
    assert_public_payload_safe,
):
    api_client = authenticated_api_client
    alternatives = api_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/alternatives")

    assert alternatives.status_code == 200
    assert alternatives.json() == {
        "id": SEEDED_REQUEST_TOKEN,
        "status": "needs_alternatives",
        "appointment_id": None,
        "message": "Client should choose new lesson times.",
    }

    public_status = api_client.get(f"/api/public/lesson-requests/{SEEDED_REQUEST_TOKEN}")
    assert public_status.status_code == 200
    assert public_status.json()["status"] == "needs_new_times"
    assert public_status.json()["confirmation"] is None
    assert_public_payload_safe(public_status.json())

    legacy_status = api_client.get(f"/api/public/requests/{SEEDED_REQUEST_TOKEN}")
    assert legacy_status.status_code == 200
    assert legacy_status.json()["status"] == "needs_new_times"
    assert_public_payload_safe(legacy_status.json())

    calendar_links = api_client.get(f"/api/public/lesson-requests/{SEEDED_REQUEST_TOKEN}/calendar-links")
    assert calendar_links.status_code == 409


def test_alternatives_are_request_bound_and_client_can_select_one(
    authenticated_api_client,
    assert_public_payload_safe,
):
    api_client = authenticated_api_client
    slot_ids = _create_search_offer_ids(api_client)
    request = api_client.post(
        "/api/public/client-invites/invite_sofia_0826/lesson-requests",
        json={"slot_id": slot_ids[0]},
    )
    assert request.status_code == 200
    request_token = request.json()["id"]

    alternatives = api_client.post(
        f"/api/coach/lesson-requests/{request_token}/alternatives",
        json={"slot_ids": [slot_ids[1]], "message": "Try this later opening."},
    )

    assert alternatives.status_code == 200
    public_status = api_client.get(f"/api/public/lesson-requests/{request_token}")
    assert public_status.status_code == 200
    payload = public_status.json()
    assert payload["status"] == "needs_new_times"
    assert [slot["id"] for slot in payload["alternative_slots"]]
    assert_public_payload_safe(payload)

    selection = api_client.post(
        f"/api/public/lesson-requests/{request_token}/select-alternative",
        json={"slot_id": payload["alternative_slots"][0]["id"]},
    )

    assert selection.status_code == 200
    assert selection.json()["status"] == "requested"
    assert selection.json()["slot"]["id"] == payload["alternative_slots"][0]["id"]


def test_selected_alternative_identity_survives_snapshot_confirmation_and_public_status(
    authenticated_api_client,
):
    request_token, _, alternatives = _create_request_with_alternatives(
        authenticated_api_client,
        alternative_count=1,
    )
    selected = alternatives[0]

    selection = authenticated_api_client.post(
        f"/api/public/lesson-requests/{request_token}/select-alternative",
        json={"slot_id": selected["id"]},
    )
    assert selection.status_code == 200, selection.text

    snapshot = authenticated_api_client.get(
        "/api/coach/command-center/snapshot",
        params={"date": "2026-08-20"},
    )
    request_card = next(item for item in snapshot.json()["requests"] if item["id"] == request_token)
    assert request_card["requested_slot"]["id"] == selected["id"]
    assert selected["id"] in {item["id"] for item in request_card["alternative_slots"]}

    confirmation = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{request_token}/confirm",
        json={"slot_id": selected["id"]},
    )
    assert confirmation.status_code == 200, confirmation.text
    payload = confirmation.json()
    expected = (_storage_time(selected["start_iso"]), _storage_time(selected["end_iso"]))
    assert payload["status"] == "confirmed"
    assert _appointment_times(request_token) == [expected]
    assert _confirmed_slot_offer_id(request_token) == selected["id"]

    public_status = authenticated_api_client.get(
        f"/api/public/lesson-requests/{request_token}"
    ).json()
    assert _storage_time(public_status["slot"]["start_iso"]) == expected[0]
    assert _storage_time(public_status["slot"]["end_iso"]) == expected[1]
    public_events = public_status["confirmation"]["calendar_links"]["google_events"]
    assert [
        (_storage_time(event["start_iso"]), _storage_time(event["end_iso"]))
        for event in public_events
    ] == [expected]


def test_selected_alternative_replays_explicit_and_omitted_without_duplicates(
    authenticated_api_client,
):
    request_token, _, alternatives = _create_request_with_alternatives(
        authenticated_api_client,
        alternative_count=1,
    )
    selected_id = alternatives[0]["id"]

    first = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{request_token}/confirm",
        json={"slot_id": selected_id},
    )
    same_explicit = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{request_token}/confirm",
        json={"slot_id": selected_id},
    )
    omitted = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{request_token}/confirm",
        json={},
    )

    assert first.status_code == same_explicit.status_code == omitted.status_code == 200
    assert first.json()["appointment_ids"] == same_explicit.json()["appointment_ids"]
    assert first.json()["appointment_ids"] == omitted.json()["appointment_ids"]
    assert _appointment_count_for_request(request_token) == 1
    assert _notification_count(request_token) == 2


def test_selected_alternative_can_confirm_omitted_then_replay_explicit(
    authenticated_api_client,
):
    request_token, _, alternatives = _create_request_with_alternatives(
        authenticated_api_client,
        alternative_count=1,
    )
    selected = alternatives[0]
    selection = authenticated_api_client.post(
        f"/api/public/lesson-requests/{request_token}/select-alternative",
        json={"slot_id": selected["id"]},
    )
    assert selection.status_code == 200, selection.text

    first = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{request_token}/confirm",
        json={},
    )
    second = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{request_token}/confirm",
        json={"slot_id": selected["id"]},
    )

    assert first.status_code == second.status_code == 200
    assert first.json()["appointment_ids"] == second.json()["appointment_ids"]
    assert _confirmed_slot_offer_id(request_token) == selected["id"]
    assert _appointment_times(request_token) == [
        (_storage_time(selected["start_iso"]), _storage_time(selected["end_iso"]))
    ]


def test_different_explicit_alternative_replay_is_rejected_without_mutation(
    authenticated_api_client,
):
    request_token, _, alternatives = _create_request_with_alternatives(
        authenticated_api_client,
        alternative_count=2,
    )
    first = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{request_token}/confirm",
        json={"slot_id": alternatives[0]["id"]},
    )
    before = _request_slot_state(request_token)
    appointment_ids = first.json()["appointment_ids"]

    conflicting = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{request_token}/confirm",
        json={"slot_id": alternatives[1]["id"]},
    )

    assert first.status_code == 200
    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["code"] == "LESSON_REQUEST_CONFIRM_CONFLICT"
    assert _request_slot_state(request_token) == before
    assert _appointment_ids(request_token) == appointment_ids
    assert _notification_count(request_token) == 2


def test_selected_expired_alternative_remains_confirmable_until_request_expiry(
    authenticated_api_client,
):
    request_token, _, alternatives = _create_request_with_alternatives(
        authenticated_api_client,
        alternative_count=1,
    )
    selected = alternatives[0]
    selection = authenticated_api_client.post(
        f"/api/public/lesson-requests/{request_token}/select-alternative",
        json={"slot_id": selected["id"]},
    )
    assert selection.status_code == 200, selection.text
    _expire_offer(selected["id"])

    confirmation = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{request_token}/confirm",
        json={"slot_id": selected["id"]},
    )

    assert confirmation.status_code == 200, confirmation.text
    assert _appointment_times(request_token) == [
        (_storage_time(selected["start_iso"]), _storage_time(selected["end_iso"]))
    ]


def test_invalid_explicit_offer_classes_share_one_non_disclosing_error(
    authenticated_api_client,
):
    request_token, search_offer_id, alternatives = _create_request_with_alternatives(
        authenticated_api_client,
        alternative_count=2,
    )
    foreign_token, _, foreign_alternatives = _create_request_with_alternatives(
        authenticated_api_client,
        requested_date="2026-08-21",
        alternative_count=1,
    )
    assert foreign_token != request_token
    expired_id = alternatives[0]["id"]
    unbound_id = alternatives[1]["id"]
    _expire_offer(expired_id)
    _unbind_offer(unbound_id)
    before = _request_slot_state(request_token)
    expected_error = {
        "error": {
            "code": "INVALID_SCHEDULING_REQUEST",
            "message": "The selected confirmation slot is not valid for this request.",
        }
    }

    for slot_id in (
        expired_id,
        search_offer_id,
        foreign_alternatives[0]["id"],
        unbound_id,
        "alt_unknown",
    ):
        response = authenticated_api_client.post(
            f"/api/coach/lesson-requests/{request_token}/confirm",
            json={"slot_id": slot_id},
        )
        assert response.status_code == 400
        assert response.json() == expected_error

    assert _request_slot_state(request_token) == before
    assert _appointment_count_for_request(request_token) == 0
    assert _notification_count(request_token) == 0


def test_omitted_confirmation_uses_persisted_occurrence_not_mutated_search_offer(
    authenticated_api_client,
):
    offer_ids = _create_offer_ids(authenticated_api_client, minimum=1)
    created = authenticated_api_client.post(
        "/api/public/client-invites/invite_sofia_0826/lesson-requests",
        json={"slot_id": offer_ids[0]},
    )
    assert created.status_code == 200, created.text
    request_token = created.json()["id"]
    expected = _request_slot_state(request_token)
    _shift_offer(offer_ids[0], hours=2)

    confirmation = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{request_token}/confirm",
        json={},
    )

    assert confirmation.status_code == 200, confirmation.text
    assert _appointment_times(request_token) == [
        (expected["slot_start"], expected["slot_end"])
    ]
    assert _confirmed_slot_offer_id(request_token) is None


def test_approve_alias_requires_truly_empty_body_and_maps_canonical_response(
    authenticated_api_client,
):
    rejected = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/approve",
        json={},
    )
    assert rejected.status_code == 422
    assert rejected.json() == {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "This endpoint does not accept a request body.",
        }
    }
    assert _request_status(SEEDED_REQUEST_TOKEN) == "pending"

    accepted = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/approve"
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "approved"
    assert set(accepted.json()) == {"id", "status", "appointment_id", "message"}


def test_explicit_alternative_schedule_conflict_rolls_back_all_slot_mutations(
    authenticated_api_client,
):
    request_token, _, alternatives = _create_request_with_alternatives(
        authenticated_api_client,
        alternative_count=1,
    )
    selected = alternatives[0]
    before = _request_slot_state(request_token)
    _add_conflicting_appointment_for_offer(selected["id"])

    response = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{request_token}/confirm",
        json={"slot_id": selected["id"]},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCHEDULING_CONFLICT"
    assert _request_slot_state(request_token) == before
    assert _request_status(request_token) == "alternatives_sent"
    assert _validation_failure_code(request_token) == "SLOT_CONFLICT"
    assert _appointment_count_for_request(request_token) == 0
    assert _notification_count(request_token) == 0


def test_competing_alternative_confirmations_have_one_winner_on_postgres(
    authenticated_api_client,
):
    request_token, _, alternatives = _create_request_with_alternatives(
        authenticated_api_client,
        alternative_count=2,
    )
    barrier = Barrier(2)

    def confirm(slot_id: str):
        barrier.wait()
        try:
            result = confirm_coach_lesson_request(
                "inst_001",
                request_token,
                CoachLessonRequestConfirmRequest(slot_id=slot_id),
            )
            return 200, result.appointment_ids
        except ApiException as exc:
            return exc.status_code, exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, [item["id"] for item in alternatives]))

    assert sorted(result[0] for result in results) == [200, 409]
    assert next(result[1] for result in results if result[0] == 409) == (
        "LESSON_REQUEST_CONFIRM_CONFLICT"
    )
    assert _appointment_count_for_request(request_token) == 1
    assert _notification_count(request_token) == 2


def test_selection_and_confirmation_race_leaves_one_consistent_terminal_state(
    authenticated_api_client,
):
    request_token, _, alternatives = _create_request_with_alternatives(
        authenticated_api_client,
        alternative_count=1,
    )
    selected = alternatives[0]
    original = _request_slot_state(request_token)
    barrier = Barrier(2)

    def select():
        barrier.wait()
        try:
            select_client_lesson_request_alternative(
                request_token,
                ClientInviteAlternativeSelectionRequest(slot_id=selected["id"]),
            )
            return 200, None
        except ApiException as exc:
            return exc.status_code, exc.code

    def confirm():
        barrier.wait()
        result = confirm_coach_lesson_request(
            "inst_001",
            request_token,
            CoachLessonRequestConfirmRequest(),
        )
        return 200, result.appointment_ids

    with ThreadPoolExecutor(max_workers=2) as pool:
        selection_future = pool.submit(select)
        confirmation_future = pool.submit(confirm)
        selection_result = selection_future.result()
        confirmation_result = confirmation_future.result()

    assert confirmation_result[0] == 200
    assert selection_result in {
        (200, None),
        (400, "INVALID_SCHEDULING_REQUEST"),
    }
    state = _request_slot_state(request_token)
    assert _request_status(request_token) == "confirmed"
    assert _appointment_count_for_request(request_token) == 1
    if selection_result[0] == 200:
        expected = (_storage_time(selected["start_iso"]), _storage_time(selected["end_iso"]))
        assert state["original_slot_offer_id"] == selected["id"]
        assert _confirmed_slot_offer_id(request_token) == selected["id"]
    else:
        expected = (original["slot_start"], original["slot_end"])
        assert state["original_slot_offer_id"] == original["original_slot_offer_id"]
        assert _confirmed_slot_offer_id(request_token) is None
    assert _appointment_times(request_token) == [expected]


def test_selection_after_confirmation_cannot_reopen_request(authenticated_api_client):
    request_token, _, alternatives = _create_request_with_alternatives(
        authenticated_api_client,
        alternative_count=1,
    )
    confirmation = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{request_token}/confirm",
        json={},
    )
    before = _request_slot_state(request_token)

    selection = authenticated_api_client.post(
        f"/api/public/lesson-requests/{request_token}/select-alternative",
        json={"slot_id": alternatives[0]["id"]},
    )

    assert confirmation.status_code == 200
    assert selection.status_code == 400
    assert selection.json()["error"]["code"] == "INVALID_SCHEDULING_REQUEST"
    assert _request_slot_state(request_token) == before
    assert _appointment_count_for_request(request_token) == 1


def test_weekly_selected_alternative_has_ordered_coach_and_public_occurrences(
    authenticated_api_client,
):
    request_token, _, alternatives = _create_request_with_alternatives(
        authenticated_api_client,
        requested_date="2026-08-21",
        recurrence="weekly",
        alternative_count=1,
    )
    selected = alternatives[0]
    selection = authenticated_api_client.post(
        f"/api/public/lesson-requests/{request_token}/select-alternative",
        json={"slot_id": selected["id"]},
    )
    assert selection.status_code == 200, selection.text

    confirmation = authenticated_api_client.post(
        f"/api/coach/lesson-requests/{request_token}/confirm",
        json={"slot_id": selected["id"]},
    )
    assert confirmation.status_code == 200, confirmation.text
    coach_events = confirmation.json()["calendar_links"]["google_events"]
    coach_occurrences = [
        (_storage_time(event["start_iso"]), _storage_time(event["end_iso"]))
        for event in coach_events
    ]
    assert len(coach_occurrences) == 4
    assert coach_occurrences == sorted(coach_occurrences)
    assert _appointment_times(request_token) == coach_occurrences

    public_status = authenticated_api_client.get(
        f"/api/public/lesson-requests/{request_token}"
    ).json()
    public_occurrences = [
        (_storage_time(event["start_iso"]), _storage_time(event["end_iso"]))
        for event in public_status["confirmation"]["calendar_links"]["google_events"]
    ]
    assert public_occurrences == coach_occurrences
    assert _storage_time(public_status["slot"]["start_iso"]) == coach_occurrences[0][0]
    assert _storage_time(public_status["slot"]["end_iso"]) == coach_occurrences[0][1]


def test_alternative_promotion_rejects_mismatched_context(authenticated_api_client):
    api_client = authenticated_api_client
    slot_ids = _create_search_offer_ids(api_client)
    request = api_client.post(
        "/api/public/client-invites/invite_sofia_0826/lesson-requests",
        json={"slot_id": slot_ids[0]},
    )
    request_token = request.json()["id"]
    _change_offer_duration(slot_ids[1], duration_mins=60)

    alternatives = api_client.post(
        f"/api/coach/lesson-requests/{request_token}/alternatives",
        json={"slot_ids": [slot_ids[1]]},
    )

    assert alternatives.status_code == 409
    assert alternatives.json()["error"]["code"] == "ALTERNATIVE_SLOT_CONTEXT_MISMATCH"
    assert _request_status(request_token) == "pending"
    assert _alternative_count(request_token) == 0


def test_weekly_confirm_creates_multiple_appointments_and_ics_events(authenticated_api_client):
    api_client = authenticated_api_client
    slot_id = _create_weekly_offer_id(api_client)
    request = api_client.post(
        "/api/public/client-invites/invite_sofia_0826/lesson-requests",
        json={"slot_id": slot_id},
    )
    request_token = request.json()["id"]

    confirmation = api_client.post(f"/api/coach/lesson-requests/{request_token}/confirm", json={})

    assert confirmation.status_code == 200
    assert len(confirmation.json()["appointment_ids"]) == 4
    assert len(confirmation.json()["calendar_links"]["google_events"]) == 4
    coach_ics = api_client.get(confirmation.json()["calendar_links"]["ics"])
    assert coach_ics.status_code == 200
    assert coach_ics.text.count("BEGIN:VEVENT") == 4
    status = api_client.get(f"/api/public/lesson-requests/{request_token}")
    ics = api_client.get(status.json()["confirmation"]["calendar_links"]["ics"])
    assert ics.status_code == 200
    assert ics.text.count("BEGIN:VEVENT") == 4


def test_coach_can_decline_request_without_exposing_private_schedule(
    authenticated_api_client,
    assert_public_payload_safe,
):
    api_client = authenticated_api_client
    decline = api_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/decline")

    assert decline.status_code == 200
    assert decline.json() == {
        "id": SEEDED_REQUEST_TOKEN,
        "status": "declined",
        "appointment_id": None,
        "message": "Lesson request was declined.",
    }

    public_status = api_client.get(f"/api/public/lesson-requests/{SEEDED_REQUEST_TOKEN}")
    assert public_status.status_code == 200
    assert public_status.json()["status"] == "needs_new_times"
    assert public_status.json()["confirmation"] is None
    assert_public_payload_safe(public_status.json())


def test_coach_lesson_request_action_routes_require_coach_dependency(migrated_postgres_db):
    app = create_app()

    def reject_coach():
        raise HTTPException(status_code=401, detail="coach required")

    app.dependency_overrides[get_current_coach] = reject_coach
    test_client = TestClient(app, raise_server_exceptions=False)
    try:
        responses = [
            test_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/approve"),
            test_client.post(
                f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/confirm",
                json={},
            ),
            test_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/alternatives"),
            test_client.post(f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/decline"),
        ]
    finally:
        test_client.close()

    assert {response.status_code for response in responses} == {401}

    app.dependency_overrides.clear()
    with db.SessionLocal() as session:
        session.add(
            InstructorDB(
                id="inst_not_owner",
                name="Another coach",
                main_address="9 Other Provider Way",
                lat=30.2,
                lng=-97.7,
                work_start_time="08:00",
                work_end_time="17:00",
                timezone="America/Chicago",
            )
        )
        session.commit()
    provision_provider_account(
        "inst_not_owner",
        "not-owner@example.com",
        "not-owner-password",
    )
    other_session = authenticate_coach(
        "not-owner@example.com",
        "not-owner-password",
    )
    tenant_headers = {
        "Cookie": f"{SESSION_COOKIE_NAME}={other_session.raw_token}",
        CSRF_HEADER_NAME: "1",
    }
    with TestClient(app, raise_server_exceptions=False) as tenant_client:
        tenant_response = tenant_client.post(
            f"/api/coach/lesson-requests/{SEEDED_REQUEST_TOKEN}/confirm",
            json={},
            headers=tenant_headers,
        )
        missing_response = tenant_client.post(
            "/api/coach/lesson-requests/req_does_not_exist/confirm",
            json={},
            headers=tenant_headers,
        )
    assert tenant_response.status_code == 404
    assert tenant_response.json()["error"]["code"] == "LESSON_REQUEST_NOT_FOUND"
    assert tenant_response.json() == missing_response.json()


def _appointment_count_for_request(request_token: str) -> int:
    session = db.SessionLocal()
    try:
        lesson_request = session.query(LessonRequestDB).filter_by(request_token=request_token).one()
        return session.query(AppointmentDB).filter_by(request_id=lesson_request.id).count()
    finally:
        session.close()


def _request_status(request_token: str) -> str:
    session = db.SessionLocal()
    try:
        return session.query(LessonRequestDB).filter_by(request_token=request_token).one().status
    finally:
        session.close()


def _validation_failure_code(request_token: str) -> str | None:
    session = db.SessionLocal()
    try:
        return session.query(LessonRequestDB).filter_by(request_token=request_token).one().validation_failure_code
    finally:
        session.close()


def _expire_request(request_token: str) -> None:
    session = db.SessionLocal()
    try:
        lesson_request = session.query(LessonRequestDB).filter_by(request_token=request_token).one()
        lesson_request.expires_at = db.datetime.datetime.utcnow() - db.datetime.timedelta(minutes=1)
        session.commit()
    finally:
        session.close()


def _create_search_offer_ids(api_client) -> list[str]:
    response = api_client.post(
        "/api/public/client-invites/invite_sofia_0826/slots",
        json={
            "client_name": "Sofia Martinez",
            "location": {
                "address": "450 North Blvd, Austin, TX",
                "lat": 30.33,
                "lng": -97.72,
                "is_instructor_facility": False,
            },
            "requested_date": "2026-08-20",
            "duration_mins": 45,
            "recurrence": "single",
            "windows": [{"id": "all_day", "days": ["Thu"], "start_time": "08:00", "end_time": "18:00"}],
        },
    )
    assert response.status_code == 200
    slot_ids = [slot["id"] for slot in response.json()["slots"]]
    assert len(slot_ids) >= 2
    return slot_ids


def _create_weekly_offer_id(api_client) -> str:
    response = api_client.post(
        "/api/public/client-invites/invite_sofia_0826/slots",
        json={
            "client_name": "Sofia Martinez",
            "location": {
                "address": "450 North Blvd, Austin, TX",
                "lat": 30.33,
                "lng": -97.72,
                "is_instructor_facility": False,
            },
            "requested_date": "2026-08-21",
            "duration_mins": 45,
            "recurrence": "weekly",
            "windows": [{"id": "all_day", "days": ["Fri"], "start_time": "08:00", "end_time": "18:00"}],
        },
    )
    assert response.status_code == 200
    return response.json()["slots"][0]["id"]


def _change_offer_duration(slot_id: str, *, duration_mins: int) -> None:
    session = db.SessionLocal()
    try:
        offer = session.query(InviteSlotOfferDB).filter_by(id=slot_id).one()
        offer.duration_mins = duration_mins
        session.commit()
    finally:
        session.close()


def _alternative_count(request_token: str) -> int:
    session = db.SessionLocal()
    try:
        lesson_request = session.query(LessonRequestDB).filter_by(request_token=request_token).one()
        return session.query(InviteSlotOfferDB).filter_by(
            lesson_request_id=lesson_request.id,
            offer_kind="alternative",
        ).count()
    finally:
        session.close()


def _add_conflicting_appointment(request_token: str) -> None:
    session = db.SessionLocal()
    try:
        lesson_request = session.query(LessonRequestDB).filter_by(request_token=request_token).one()
        session.add(
            AppointmentDB(
                id="app_request_conflict",
                instructor_id=lesson_request.instructor_id,
                client_id="client_conflict",
                title="Existing lesson",
                start_time=lesson_request.slot_start,
                end_time=lesson_request.slot_end,
                location_address=lesson_request.client_address,
                lat=lesson_request.lat,
                lng=lesson_request.lng,
                is_break=False,
                event_type="lesson",
                status="scheduled",
            )
        )
        session.commit()
    finally:
        session.close()


def _create_offer_ids(
    api_client,
    *,
    requested_date: str = "2026-08-20",
    recurrence: str = "single",
    minimum: int = 3,
) -> list[str]:
    day = dt.date.fromisoformat(requested_date).strftime("%a")
    response = api_client.post(
        "/api/public/client-invites/invite_sofia_0826/slots",
        json={
            "client_name": "Sofia Martinez",
            "location": {
                "address": "450 North Blvd, Austin, TX",
                "lat": 30.33,
                "lng": -97.72,
                "is_instructor_facility": False,
            },
            "requested_date": requested_date,
            "duration_mins": 45,
            "recurrence": recurrence,
            "windows": [
                {
                    "id": "all_day",
                    "days": [day],
                    "start_time": "08:00",
                    "end_time": "18:00",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    slot_ids = [slot["id"] for slot in response.json()["slots"]]
    assert len(slot_ids) >= minimum
    return slot_ids


def _create_request_with_alternatives(
    api_client,
    *,
    requested_date: str = "2026-08-20",
    recurrence: str = "single",
    alternative_count: int,
) -> tuple[str, str, list[dict]]:
    slot_ids = _create_offer_ids(
        api_client,
        requested_date=requested_date,
        recurrence=recurrence,
        minimum=alternative_count + 1,
    )
    created = api_client.post(
        "/api/public/client-invites/invite_sofia_0826/lesson-requests",
        json={"slot_id": slot_ids[0]},
    )
    assert created.status_code == 200, created.text
    request_token = created.json()["id"]
    promoted = api_client.post(
        f"/api/coach/lesson-requests/{request_token}/alternatives",
        json={"slot_ids": slot_ids[1 : alternative_count + 1]},
    )
    assert promoted.status_code == 200, promoted.text
    status = api_client.get(f"/api/public/lesson-requests/{request_token}")
    assert status.status_code == 200, status.text
    alternatives = status.json()["alternative_slots"]
    assert len(alternatives) == alternative_count
    return request_token, slot_ids[0], alternatives


def _request_slot_state(request_token: str) -> dict:
    with db.SessionLocal() as session:
        lesson_request = session.query(LessonRequestDB).filter_by(request_token=request_token).one()
        return {
            "slot_start": lesson_request.slot_start,
            "slot_end": lesson_request.slot_end,
            "requested_date": lesson_request.requested_date,
            "travel_time_mins": lesson_request.travel_time_mins,
            "buffer_mins": lesson_request.buffer_mins,
            "distance_miles": lesson_request.distance_miles,
            "original_slot_offer_id": lesson_request.original_slot_offer_id,
        }


def _confirmed_slot_offer_id(request_token: str) -> str | None:
    with db.SessionLocal() as session:
        return (
            session.query(LessonRequestDB)
            .filter_by(request_token=request_token)
            .one()
            .confirmed_slot_offer_id
        )


def _appointment_times(request_token: str) -> list[tuple[dt.datetime, dt.datetime]]:
    with db.SessionLocal() as session:
        lesson_request = session.query(LessonRequestDB).filter_by(request_token=request_token).one()
        appointments = (
            session.query(AppointmentDB)
            .filter_by(request_id=lesson_request.id)
            .order_by(AppointmentDB.start_time.asc())
            .all()
        )
        return [(item.start_time, item.end_time) for item in appointments]


def _appointment_ids(request_token: str) -> list[str]:
    with db.SessionLocal() as session:
        lesson_request = session.query(LessonRequestDB).filter_by(request_token=request_token).one()
        return [
            item.id
            for item in session.query(AppointmentDB)
            .filter_by(request_id=lesson_request.id)
            .order_by(AppointmentDB.start_time.asc())
            .all()
        ]


def _notification_count(request_token: str) -> int:
    with db.SessionLocal() as session:
        lesson_request = session.query(LessonRequestDB).filter_by(request_token=request_token).one()
        return session.query(NotificationEventDB).filter_by(request_id=lesson_request.id).count()


def _storage_time(value: str) -> dt.datetime:
    return timezones.to_utc_storage(dt.datetime.fromisoformat(value.replace("Z", "+00:00")))


def _expire_offer(slot_id: str) -> None:
    with db.SessionLocal() as session:
        offer = session.query(InviteSlotOfferDB).filter_by(id=slot_id).one()
        offer.expires_at = dt.datetime.utcnow() - dt.timedelta(minutes=1)
        session.commit()


def _unbind_offer(slot_id: str) -> None:
    with db.SessionLocal() as session:
        offer = session.query(InviteSlotOfferDB).filter_by(id=slot_id).one()
        offer.lesson_request_id = None
        session.commit()


def _shift_offer(slot_id: str, *, hours: int) -> None:
    with db.SessionLocal() as session:
        offer = session.query(InviteSlotOfferDB).filter_by(id=slot_id).one()
        offer.slot_start += dt.timedelta(hours=hours)
        offer.slot_end += dt.timedelta(hours=hours)
        offer.requested_date = offer.slot_start.date()
        offer.travel_time_mins += 30
        offer.buffer_mins += 30
        offer.distance_miles += 10
        session.commit()


def _add_conflicting_appointment_for_offer(slot_id: str) -> None:
    with db.SessionLocal() as session:
        offer = session.query(InviteSlotOfferDB).filter_by(id=slot_id).one()
        session.add(
            AppointmentDB(
                id=f"app_conflict_{slot_id[-8:]}",
                instructor_id=offer.instructor_id,
                client_id="client_conflict",
                title="Existing lesson",
                start_time=offer.slot_start,
                end_time=offer.slot_end,
                location_address=offer.location_address,
                lat=offer.lat,
                lng=offer.lng,
                is_break=False,
                event_type="lesson",
                status="scheduled",
            )
        )
        session.commit()
