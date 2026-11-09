import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie

import pytest
from argon2 import PasswordHasher, Type
from fastapi.testclient import TestClient

from app import api as api_module
from app import auth, db
from app.api import create_app
from app.api_errors import InvalidAuthToken, InvalidCredentials
from app.auth import (
    CSRF_HEADER_NAME,
    CSRF_HEADER_VALUE,
    DEMO_PROVIDER_EMAIL,
    DEMO_PROVIDER_PASSWORD,
    ProviderAuthAdminError,
    SESSION_COOKIE_NAME,
    authenticate_coach,
    disable_provider_account,
    enable_provider_account,
    provision_provider_account,
    reset_provider_password,
    revoke_all_provider_sessions,
    validate_coach_session,
)
from app.config import application_settings_from_env
from app.dependencies import get_current_coach
from app.models import (
    AppointmentDB,
    InstructorDB,
    InviteTokenDB,
    ProviderAccountDB,
    ProviderSessionDB,
)
from app.routes import coach as coach_routes_module
from tests.conftest import login_coach
from tests.db_test_utils import reset_postgres_database


CSRF_HEADERS = {CSRF_HEADER_NAME: CSRF_HEADER_VALUE}
INVALID_CREDENTIALS_BODY = {
    "error": {
        "code": "INVALID_CREDENTIALS",
        "message": "The coach credentials are invalid.",
    }
}
INVALID_SESSION_BODY = {
    "error": {
        "code": "INVALID_AUTH_TOKEN",
        "message": "The coach session is invalid or expired.",
    }
}


def test_login_issues_digest_only_cookie_and_metadata(api_client):
    response = _sign_in(api_client)
    payload = response.json()
    raw_token = response.cookies.get(SESSION_COOKIE_NAME)

    assert response.status_code == 200
    assert payload == {
        "authenticated": True,
        "expires_at": payload["expires_at"],
        "coach": {
            "instructor_id": "inst_001",
            "display_name": "Coach Sarah",
        },
    }
    assert "access_token" not in payload
    assert "token_type" not in payload
    assert raw_token is not None and len(raw_token) == 43
    assert response.headers["cache-control"] == "private, no-store"

    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    morsel = cookie[SESSION_COOKIE_NAME]
    assert morsel["path"] == "/api"
    assert morsel["httponly"] is True
    assert morsel["samesite"].lower() == "strict"
    assert morsel["domain"] == ""
    assert morsel["secure"] == ""
    assert int(morsel["max-age"]) == 8 * 60 * 60
    assert morsel["expires"]

    with db.SessionLocal() as session:
        stored = session.query(ProviderSessionDB).one()
        assert stored.token_digest == hashlib.sha256(raw_token.encode("ascii")).digest()
        assert raw_token.encode("ascii") != stored.token_digest
        assert raw_token not in repr(stored.__dict__)


def test_production_cookie_is_secure_and_cannot_be_disabled(
    migrated_postgres_db,
    monkeypatch,
):
    reset_postgres_database(seed=False)
    _add_instructor("inst_production", "Production Provider")
    provision_provider_account(
        "inst_production",
        "production-provider@example.com",
        "production-provider-password",
    )
    monkeypatch.setenv("SWIM_ENV", "production")
    monkeypatch.setenv("SWIM_ENABLE_DEMO_DATA", "false")
    monkeypatch.setenv("SWIM_ENABLE_DEMO_AUTH", "false")
    monkeypatch.setenv("SWIM_API_CORS_ORIGINS", "https://command.example.com")
    monkeypatch.delenv("SWIM_COACH_COOKIE_SECURE", raising=False)
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = _sign_in(
            client,
            email="production-provider@example.com",
            password="production-provider-password",
        )
    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    assert cookie[SESSION_COOKIE_NAME]["secure"] is True

    monkeypatch.setenv("SWIM_COACH_COOKIE_SECURE", "false")
    with pytest.raises(RuntimeError, match="cannot be disabled"):
        application_settings_from_env()


def test_passwords_are_argon2id_salted_and_account_mapping_is_unique(migrated_postgres_db):
    _add_instructor("inst_auth_2", "Provider Two")
    _add_instructor("inst_auth_3", "Provider Three")
    first = provision_provider_account("inst_auth_2", "First@Example.com ", "same-secure-password")
    second = provision_provider_account("inst_auth_3", "second@example.com", "same-secure-password")

    with db.SessionLocal() as session:
        first_row = session.get(ProviderAccountDB, first)
        second_row = session.get(ProviderAccountDB, second)
        assert first_row.email_normalized == "first@example.com"
        assert first_row.password_hash.startswith("$argon2id$")
        assert second_row.password_hash.startswith("$argon2id$")
        assert first_row.password_hash != second_row.password_hash
        assert "same-secure-password" not in first_row.password_hash

    with pytest.raises(ProviderAuthAdminError, match="provider already"):
        provision_provider_account("inst_auth_2", "new@example.com", "another-secure-password")
    with pytest.raises(ProviderAuthAdminError, match="email or provider"):
        provision_provider_account("inst_auth_3", "first@example.com", "another-secure-password")
    with pytest.raises(ProviderAuthAdminError, match="does not exist"):
        provision_provider_account("missing", "missing@example.com", "another-secure-password")


def test_concurrent_duplicate_provisioning_returns_one_safe_admin_error(migrated_postgres_db):
    _add_instructor("inst_concurrent_a", "Concurrent A")
    _add_instructor("inst_concurrent_b", "Concurrent B")

    def provision(instructor_id: str) -> str:
        try:
            return provision_provider_account(
                instructor_id,
                "concurrent@example.com",
                "concurrent-secure-password",
            )
        except ProviderAuthAdminError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(provision, ["inst_concurrent_a", "inst_concurrent_b"]))

    assert sum(result.startswith("acct_") for result in results) == 1
    assert sum("email or provider" in result for result in results) == 1


@pytest.mark.parametrize(
    ("email", "password", "disable"),
    [
        (DEMO_PROVIDER_EMAIL, "wrong-password", False),
        ("unknown@example.com", "wrong-password", False),
        (DEMO_PROVIDER_EMAIL, DEMO_PROVIDER_PASSWORD, True),
    ],
)
def test_wrong_unknown_and_disabled_credentials_share_exact_response(
    api_client,
    email,
    password,
    disable,
):
    if disable:
        disable_provider_account(DEMO_PROVIDER_EMAIL)
    response = api_client.post(
        "/api/auth/coach/sign-in",
        json={"email": email, "password": password},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 401
    assert response.json() == INVALID_CREDENTIALS_BODY


def test_unknown_email_executes_dummy_argon_verification(api_client, monkeypatch):
    calls: list[str] = []
    original = auth._verify_password

    def recording_verify(password_hash: str, password: str) -> bool:
        calls.append(password_hash)
        return original(password_hash, password)

    monkeypatch.setattr(auth, "_verify_password", recording_verify)
    response = api_client.post(
        "/api/auth/coach/sign-in",
        json={"email": "unknown@example.com", "password": "not-the-password"},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 401
    assert calls == [auth._DUMMY_PASSWORD_HASH]


def test_successful_login_rehashes_an_obsolete_argon_profile(api_client):
    obsolete = PasswordHasher(
        time_cost=1,
        memory_cost=8 * 1024,
        parallelism=1,
        hash_len=16,
        salt_len=8,
        type=Type.ID,
    ).hash(DEMO_PROVIDER_PASSWORD)
    with db.SessionLocal() as session:
        account = session.query(ProviderAccountDB).filter_by(email_normalized=DEMO_PROVIDER_EMAIL).one()
        account.password_hash = obsolete
        session.commit()

    response = _sign_in(api_client)
    assert response.status_code == 200
    with db.SessionLocal() as session:
        refreshed = session.query(ProviderAccountDB).filter_by(email_normalized=DEMO_PROVIDER_EMAIL).one()
        assert refreshed.password_hash != obsolete
        assert "$m=19456,t=2,p=1$" in refreshed.password_hash


def test_session_validation_missing_malformed_unknown_and_expired(api_client):
    missing = api_client.get("/api/coach/me")
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"

    for raw_token in ("bad", "A" * 43):
        response = api_client.get(
            "/api/coach/me",
            headers={"Cookie": f"{SESSION_COOKIE_NAME}={raw_token}"},
        )
        assert response.status_code == 401
        assert response.json() == INVALID_SESSION_BODY
        assert "Max-Age=0" in response.headers["set-cookie"]

    signed_in = _sign_in(api_client)
    token = signed_in.cookies.get(SESSION_COOKIE_NAME)
    with db.SessionLocal() as session:
        row = session.query(ProviderSessionDB).filter_by(
            token_digest=hashlib.sha256(token.encode("ascii")).digest()
        ).one()
        row.expires_at = datetime.now(timezone.utc)
        session.commit()
    api_client.cookies.clear()
    expired = api_client.get(
        "/api/auth/coach/session",
        headers={"Cookie": f"{SESSION_COOKIE_NAME}={token}"},
    )
    assert expired.status_code == 401
    assert expired.json() == INVALID_SESSION_BODY


def test_exact_expiry_boundary_and_stale_version_are_rejected(migrated_postgres_db):
    fixed_now = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
    issued = authenticate_coach(DEMO_PROVIDER_EMAIL, DEMO_PROVIDER_PASSWORD, now=fixed_now)
    assert validate_coach_session(
        issued.raw_token,
        now=issued.principal.expires_at - timedelta(microseconds=1),
    ).instructor_id == "inst_001"
    with pytest.raises(InvalidAuthToken):
        validate_coach_session(issued.raw_token, now=issued.principal.expires_at)

    fresh = authenticate_coach(DEMO_PROVIDER_EMAIL, DEMO_PROVIDER_PASSWORD)
    with db.SessionLocal() as session:
        account = session.get(ProviderAccountDB, fresh.principal.account_id)
        account.session_version += 1
        session.commit()
    with pytest.raises(InvalidAuthToken):
        validate_coach_session(fresh.raw_token)


def test_session_digest_collision_retries_with_a_fresh_random_token(
    migrated_postgres_db,
    monkeypatch,
):
    first_token = "A" * 43
    second_token = "B" * 43
    monkeypatch.setattr(auth, "_generate_session_token", lambda: first_token)
    first = authenticate_coach(DEMO_PROVIDER_EMAIL, DEMO_PROVIDER_PASSWORD)
    assert first.raw_token == first_token

    candidates = iter([first_token, second_token])
    monkeypatch.setattr(auth, "_generate_session_token", lambda: next(candidates))
    second = authenticate_coach(DEMO_PROVIDER_EMAIL, DEMO_PROVIDER_PASSWORD)
    assert second.raw_token == second_token
    with db.SessionLocal() as session:
        assert session.query(ProviderSessionDB).count() == 2


def test_sign_out_revokes_only_current_session_and_replay_fails(migrated_postgres_db):
    app = create_app()
    with (
        TestClient(app, raise_server_exceptions=False) as first,
        TestClient(app, raise_server_exceptions=False) as second,
    ):
        first_token = _sign_in(first).cookies.get(SESSION_COOKIE_NAME)
        second_token = _sign_in(second).cookies.get(SESSION_COOKIE_NAME)

        signed_out = first.post("/api/auth/coach/sign-out", headers=CSRF_HEADERS)
        assert signed_out.status_code == 200
        assert "Max-Age=0" in signed_out.headers["set-cookie"]
        assert first.post("/api/auth/coach/sign-out", headers=CSRF_HEADERS).status_code == 200

        replay = first.get(
            "/api/auth/coach/session",
            headers={"Cookie": f"{SESSION_COOKIE_NAME}={first_token}"},
        )
        still_active = second.get("/api/auth/coach/session")
        assert replay.status_code == 401
        assert replay.json() == INVALID_SESSION_BODY
        assert still_active.status_code == 200

    with db.SessionLocal() as session:
        first_row = session.query(ProviderSessionDB).filter_by(
            token_digest=hashlib.sha256(first_token.encode("ascii")).digest()
        ).one()
        second_row = session.query(ProviderSessionDB).filter_by(
            token_digest=hashlib.sha256(second_token.encode("ascii")).digest()
        ).one()
        assert first_row.revoked_at is not None
        assert first_row.revocation_reason == "sign_out"
        assert second_row.revoked_at is None


def test_fresh_sign_in_rotates_only_cookie_presented_by_that_browser(api_client):
    first = _sign_in(api_client)
    old_token = first.cookies.get(SESSION_COOKIE_NAME)
    second = _sign_in(api_client)
    new_token = second.cookies.get(SESSION_COOKIE_NAME)
    assert old_token != new_token
    with pytest.raises(InvalidAuthToken):
        validate_coach_session(old_token)
    assert validate_coach_session(new_token).instructor_id == "inst_001"


def test_password_reset_revoke_all_disable_and_enable_invalidate_all_sessions(migrated_postgres_db):
    first = authenticate_coach(DEMO_PROVIDER_EMAIL, DEMO_PROVIDER_PASSWORD)
    second = authenticate_coach(DEMO_PROVIDER_EMAIL, DEMO_PROVIDER_PASSWORD)
    reset_provider_password(DEMO_PROVIDER_EMAIL, "new-secure-provider-password")
    for token in (first.raw_token, second.raw_token):
        with pytest.raises(InvalidAuthToken):
            validate_coach_session(token)
    with pytest.raises(InvalidCredentials):
        authenticate_coach(DEMO_PROVIDER_EMAIL, DEMO_PROVIDER_PASSWORD)

    third = authenticate_coach(DEMO_PROVIDER_EMAIL, "new-secure-provider-password")
    revoke_all_provider_sessions(DEMO_PROVIDER_EMAIL)
    with pytest.raises(InvalidAuthToken):
        validate_coach_session(third.raw_token)

    fourth = authenticate_coach(DEMO_PROVIDER_EMAIL, "new-secure-provider-password")
    disable_provider_account(DEMO_PROVIDER_EMAIL)
    with pytest.raises(InvalidAuthToken):
        validate_coach_session(fourth.raw_token)
    with pytest.raises(InvalidCredentials):
        authenticate_coach(DEMO_PROVIDER_EMAIL, "new-secure-provider-password")

    enable_provider_account(DEMO_PROVIDER_EMAIL)
    assert authenticate_coach(
        DEMO_PROVIDER_EMAIL,
        "new-secure-provider-password",
    ).principal.instructor_id == "inst_001"


def test_csrf_and_origin_policy_precedes_commands_but_not_coach_auth(api_client):
    auth_headers = login_coach(api_client)
    cookie_header = {"Cookie": auth_headers["Cookie"]}

    for headers in (
        cookie_header,
        {**cookie_header, CSRF_HEADER_NAME: "wrong"},
        {**auth_headers, "Origin": "null"},
        {**auth_headers, "Origin": "https://attacker.example"},
    ):
        response = api_client.put("/api/coach/settings", json={}, headers=headers)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_VALIDATION_FAILED"

    invalid_auth = api_client.put(
        "/api/coach/settings",
        json={},
        headers={"Cookie": f"{SESSION_COOKIE_NAME}={'A' * 43}"},
    )
    assert invalid_auth.status_code == 401
    assert invalid_auth.json()["error"]["code"] == "INVALID_AUTH_TOKEN"

    sign_in_without_header = api_client.post(
        "/api/auth/coach/sign-in",
        json={"email": DEMO_PROVIDER_EMAIL, "password": DEMO_PROVIDER_PASSWORD},
    )
    assert sign_in_without_header.status_code == 403
    assert sign_in_without_header.json()["error"]["code"] == "CSRF_VALIDATION_FAILED"


def test_non_browser_and_trusted_or_self_origin_mutations_succeed(api_client):
    auth_headers = login_coach(api_client)
    for origin in (None, "http://localhost:5173", "http://testserver"):
        headers = dict(auth_headers)
        if origin is not None:
            headers["Origin"] = origin
        response = api_client.post(
            "/api/coach/availability",
            headers=headers,
            json={
                "client_name": "CSRF Client",
                "client_address": "450 North Blvd, Austin, TX",
                "requested_date": "2026-08-21",
                "duration_mins": 45,
            },
        )
        assert response.status_code == 200


def test_public_routes_are_unauthenticated_and_csrf_exempt(api_client):
    invite = api_client.get("/api/public/invites/demo-invite-token")
    availability = api_client.post(
        "/api/public/invites/demo-invite-token/availability",
        headers={"Origin": "https://untrusted.example"},
        json={
            "client_name": "Public Client",
            "client_address": "450 North Blvd, Austin, TX",
            "requested_date": "2026-08-21",
            "location_type": "home",
            "duration_mins": 45,
        },
    )
    assert invite.status_code == 200
    assert availability.status_code == 200


def test_every_coach_route_inherits_cookie_auth_dependency(api_client):
    coach_routes = [
        route
        for route in coach_routes_module.router.routes
        if getattr(route, "path", "").startswith("/api/coach/")
    ]
    assert coach_routes
    for route in coach_routes:
        calls = {dependency.call for dependency in route.dependant.dependencies}
        assert get_current_coach in calls, (route.path, route.methods)

    schema = api_client.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    assert "CoachSessionCookie" in schemes
    assert schemes["CoachSessionCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": SESSION_COOKIE_NAME,
        "description": "Opaque revocable provider session cookie.",
    }
    assert "HTTPBearer" not in schemes
    for path, operations in schema["paths"].items():
        if path.startswith("/api/coach/"):
            for operation in operations.values():
                assert {"CoachSessionCookie": []} in operation.get("security", [])
        if path.startswith("/api/public/"):
            for operation in operations.values():
                assert not operation.get("security")


def test_legacy_bearer_and_environment_credentials_are_not_authentication_paths(
    api_client,
    monkeypatch,
):
    monkeypatch.setenv(
        "SWIM_COACH_CREDENTIALS",
        '[{"email":"legacy@example.com","password":"legacy-password","instructor_id":"inst_001"}]',
    )
    monkeypatch.setenv("SWIM_COACH_AUTH_SECRET", "legacy-secret")
    bearer_only = api_client.get(
        "/api/coach/me",
        headers={"Authorization": "Bearer old-token"},
    )
    legacy_login = api_client.post(
        "/api/auth/coach/sign-in",
        headers=CSRF_HEADERS,
        json={"email": "legacy@example.com", "password": "legacy-password"},
    )
    assert bearer_only.status_code == 401
    assert bearer_only.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert legacy_login.status_code == 401
    assert legacy_login.json() == INVALID_CREDENTIALS_BODY


def test_login_validation_never_echoes_password_material(api_client):
    password = "secret-value-" * 30
    response = api_client.post(
        "/api/auth/coach/sign-in",
        headers=CSRF_HEADERS,
        json={"email": DEMO_PROVIDER_EMAIL, "password": password},
    )
    assert response.status_code == 422
    assert password not in response.text
    assert all("input" not in field for field in response.json()["error"]["details"]["fields"])

    missing_email = api_client.post(
        "/api/auth/coach/login",
        headers=CSRF_HEADERS,
        json={"password": "missing-email-secret"},
    )
    assert missing_email.status_code == 422
    assert "missing-email-secret" not in missing_email.text
    assert all(
        "input" not in field
        for field in missing_email.json()["error"]["details"]["fields"]
    )


def test_auth_and_csrf_precede_malformed_json_body_decoding(api_client):
    malformed = b'{"broken"'
    content_headers = {"content-type": "application/json"}

    missing_session = api_client.post(
        "/api/coach/bookings",
        content=malformed,
        headers={**content_headers, "Origin": "http://localhost:5173"},
    )
    assert missing_session.status_code == 401
    assert missing_session.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert missing_session.headers["access-control-allow-origin"] == "http://localhost:5173"

    invalid_session = api_client.post(
        "/api/coach/bookings",
        content=malformed,
        headers={
            **content_headers,
            "Cookie": f"{SESSION_COOKIE_NAME}={'A' * 43}",
        },
    )
    assert invalid_session.status_code == 401
    assert invalid_session.json()["error"]["code"] == "INVALID_AUTH_TOKEN"
    assert SESSION_COOKIE_NAME in invalid_session.headers["set-cookie"]

    sign_in_without_csrf = api_client.post(
        "/api/auth/coach/sign-in",
        content=malformed,
        headers=content_headers,
    )
    assert sign_in_without_csrf.status_code == 403
    assert sign_in_without_csrf.json()["error"]["code"] == "CSRF_VALIDATION_FAILED"

    auth_headers = login_coach(api_client)
    authenticated_without_csrf = api_client.post(
        "/api/coach/bookings",
        content=malformed,
        headers={
            **content_headers,
            "Cookie": auth_headers["Cookie"],
        },
    )
    assert authenticated_without_csrf.status_code == 403
    assert authenticated_without_csrf.json()["error"]["code"] == "CSRF_VALIDATION_FAILED"

    authenticated_with_csrf = api_client.post(
        "/api/coach/bookings",
        content=malformed,
        headers={**content_headers, **auth_headers},
    )
    assert authenticated_with_csrf.status_code == 422
    assert authenticated_with_csrf.json()["error"]["code"] == "VALIDATION_ERROR"


def test_provider_middleware_reuses_the_validated_principal(
    api_client,
    monkeypatch,
):
    calls: list[str] = []
    validate = api_module.validate_coach_session

    def counting_validate(raw_token: str):
        calls.append(raw_token)
        return validate(raw_token)

    monkeypatch.setattr(api_module, "validate_coach_session", counting_validate)
    headers = login_coach(api_client)
    response = api_client.get("/api/coach/me", headers=headers)
    assert response.status_code == 200
    assert len(calls) == 1


def test_demo_invite_lookup_never_seeds_data_when_demo_is_disabled(monkeypatch):
    monkeypatch.setenv("SWIM_ENV", "test")
    monkeypatch.setenv("SWIM_ENABLE_DEMO_DATA", "false")
    monkeypatch.setenv("SWIM_ENABLE_DEMO_AUTH", "false")
    reset_postgres_database(seed=False)
    try:
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            response = client.get("/api/public/invites/demo-invite-token")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "INVITE_NOT_FOUND"
        with db.SessionLocal() as session:
            assert session.query(InstructorDB).count() == 0
            assert session.query(ProviderAccountDB).count() == 0
    finally:
        db.configure_database()


def test_existing_demo_credentials_and_invites_are_disabled_with_flags(
    api_client,
    monkeypatch,
):
    issued = _sign_in(api_client)
    session_token = issued.cookies.get(SESSION_COOKIE_NAME)
    assert session_token
    demo_headers = {
        "Cookie": f"{SESSION_COOKIE_NAME}={session_token}",
        CSRF_HEADER_NAME: CSRF_HEADER_VALUE,
    }
    notification = api_client.post(
        "/api/coach/invites/invite_sofia_0826/notifications",
        headers={**demo_headers, "Idempotency-Key": "demo-provenance-notification"},
    )
    confirmation = api_client.post(
        "/api/coach/lesson-requests/req_sofia_pending_0820/confirm",
        headers=demo_headers,
        json={},
    )
    assert notification.status_code == 202
    assert confirmation.status_code == 200
    delivery_id = notification.json()["email_delivery"]["id"]
    appointment_ids = confirmation.json()["appointment_ids"]
    with db.SessionLocal() as session:
        assert session.query(ProviderAccountDB).filter_by(is_demo=True).count() == 1
        assert session.query(InviteTokenDB).filter_by(is_demo=True).count() >= 2

    monkeypatch.setenv("SWIM_ENABLE_DEMO_DATA", "false")
    monkeypatch.setenv("SWIM_ENABLE_DEMO_AUTH", "false")

    replay = api_client.get(
        "/api/auth/coach/session",
        headers={"Cookie": f"{SESSION_COOKIE_NAME}={session_token}"},
    )
    sign_in = _sign_in(api_client)
    with db.SessionLocal() as session:
        demo_account = session.query(ProviderAccountDB).filter_by(is_demo=True).one()
        session.delete(demo_account)
        session.commit()
    provision_provider_account(
        "inst_001",
        "real-provider@example.com",
        "real-provider-password",
    )
    real_sign_in = _sign_in(
        api_client,
        email="real-provider@example.com",
        password="real-provider-password",
    )
    real_token = real_sign_in.cookies.get(SESSION_COOKIE_NAME)
    assert real_token
    real_headers = {
        "Cookie": f"{SESSION_COOKIE_NAME}={real_token}",
        CSRF_HEADER_NAME: CSRF_HEADER_VALUE,
    }
    invite = api_client.get("/api/public/invites/demo-invite-token")
    request_status = api_client.get(
        "/api/public/lesson-requests/req_sofia_pending_0820"
    )
    snapshot = api_client.get(
        "/api/coach/command-center/snapshot?date=2026-08-20",
        headers=real_headers,
    )
    schedule = api_client.get(
        "/api/coach/schedule?date=2026-08-20",
        headers=real_headers,
    )
    hidden_confirmation = api_client.post(
        "/api/coach/lesson-requests/req_sofia_pending_0820/confirm",
        headers=real_headers,
        json={},
    )
    hidden_notification = api_client.post(
        "/api/coach/invites/invite_sofia_0826/notifications",
        headers={**real_headers, "Idempotency-Key": "hidden-demo-notification"},
    )
    deliveries = api_client.get("/api/coach/email-deliveries", headers=real_headers)
    hidden_delivery = api_client.post(
        f"/api/coach/email-deliveries/{delivery_id}/resend",
        headers=real_headers,
    )
    hidden_calendar = api_client.get(
        f"/api/coach/appointments/{appointment_ids[0]}/calendar-links",
        headers=real_headers,
    )
    hidden_booking_replay = api_client.post(
        "/api/coach/bookings",
        headers=real_headers,
        json={
            "client_name": "Sofia",
            "client_address": "820 Lakeview Dr, Austin, TX",
            "start_time": "2026-08-20T16:15:00-05:00",
            "duration_mins": 45,
            "is_instructor_facility": False,
            "idempotency_key": "lesson-request-confirm:req_sofia_pending_0820:0",
        },
    )
    hidden_idempotency_key = "lesson-request-confirm:req_sofia_pending_0820:0"
    with pytest.raises(db.IdempotencyKeyConflictError):
        db.get_appointment_by_idempotency_key(
            "inst_001",
            hidden_idempotency_key,
        )

    with db.SessionLocal() as session:
        hidden_appointment = session.get(AppointmentDB, appointment_ids[0])
        assert hidden_appointment is not None
        recovery_fields = {
            "client_id": hidden_appointment.client_id,
            "title": hidden_appointment.title,
            "start_time": hidden_appointment.start_time,
            "end_time": hidden_appointment.end_time,
            "location_address": hidden_appointment.location_address,
            "lat": hidden_appointment.lat,
            "lng": hidden_appointment.lng,
            "request_id": hidden_appointment.request_id,
        }
        # A cancelled row no longer creates an overlap conflict, allowing this
        # test to force the unique-key integrity recovery branch.
        hidden_appointment.status = "cancelled"
        session.commit()

    real_idempotency_lookup = db._idempotent_appointment_or_conflict
    lookup_calls = 0

    def miss_then_check_provenance(session, instructor_id, idempotency_key):
        nonlocal lookup_calls
        lookup_calls += 1
        if lookup_calls == 1:
            return None
        return real_idempotency_lookup(session, instructor_id, idempotency_key)

    with monkeypatch.context() as race_patch:
        race_patch.setattr(
            db,
            "_idempotent_appointment_or_conflict",
            miss_then_check_provenance,
        )
        with pytest.raises(db.IdempotencyKeyConflictError):
            db.save_appointment(
                "inst_001",
                idempotency_key=hidden_idempotency_key,
                **recovery_fields,
            )
    assert lookup_calls == 2
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "INVALID_AUTH_TOKEN"
    assert sign_in.status_code == 401
    assert sign_in.json() == INVALID_CREDENTIALS_BODY
    assert real_sign_in.status_code == 200
    assert invite.status_code == 404
    assert invite.json()["error"]["code"] == "INVITE_NOT_FOUND"
    assert request_status.status_code == 404
    assert request_status.json()["error"]["code"] == "LESSON_REQUEST_NOT_FOUND"
    assert snapshot.status_code == 200
    assert all(
        request["id"] != "req_sofia_pending_0820"
        for request in snapshot.json()["requests"]
    )
    assert all(
        appointment["id"] not in appointment_ids
        for appointment in snapshot.json()["schedule"]
    )
    assert schedule.status_code == 200
    assert all(
        appointment["id"] not in appointment_ids
        for appointment in schedule.json()["appointments"]
    )
    assert hidden_confirmation.status_code == 404
    assert hidden_confirmation.json()["error"]["code"] == "LESSON_REQUEST_NOT_FOUND"
    assert hidden_notification.status_code == 404
    assert hidden_notification.json()["error"]["code"] == "INVITE_NOT_FOUND"
    assert deliveries.status_code == 200
    assert all(delivery["id"] != delivery_id for delivery in deliveries.json())
    assert hidden_delivery.status_code == 404
    assert hidden_delivery.json()["error"]["code"] == "EMAIL_DELIVERY_NOT_FOUND"
    assert hidden_calendar.status_code == 404
    assert hidden_booking_replay.status_code == 409
    assert hidden_booking_replay.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployed_startup_rejects_persisted_demo_artifacts(
    migrated_postgres_db,
    monkeypatch,
    environment,
):
    monkeypatch.setenv("SWIM_ENV", environment)
    monkeypatch.setenv("SWIM_API_CORS_ORIGINS", "https://command.example.com")
    monkeypatch.setenv("SWIM_ENABLE_DEMO_DATA", "false")
    monkeypatch.setenv("SWIM_ENABLE_DEMO_AUTH", "false")
    with pytest.raises(RuntimeError, match="Demo authentication or invite artifacts"):
        with TestClient(create_app(), raise_server_exceptions=False):
            pass


def test_database_engines_hide_bound_parameters(migrated_postgres_db):
    assert db.engine.hide_parameters is True


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployed_environments_reject_demo_flags(monkeypatch, environment):
    monkeypatch.setenv("SWIM_ENV", environment)
    monkeypatch.setenv("SWIM_API_CORS_ORIGINS", "https://command.example.com")
    monkeypatch.setenv("SWIM_ENABLE_DEMO_DATA", "true")
    monkeypatch.setenv("SWIM_ENABLE_DEMO_AUTH", "true")
    with pytest.raises(RuntimeError, match="forbidden"):
        application_settings_from_env()


def test_environment_and_cors_configuration_fail_closed(monkeypatch):
    monkeypatch.delenv("SWIM_ENV", raising=False)
    with pytest.raises(RuntimeError, match="SWIM_ENV is required"):
        application_settings_from_env()
    monkeypatch.setenv("SWIM_ENV", "preview")
    with pytest.raises(RuntimeError, match="SWIM_ENV is required"):
        application_settings_from_env()
    monkeypatch.setenv("SWIM_ENV", "production")
    monkeypatch.setenv("SWIM_ENABLE_DEMO_DATA", "false")
    monkeypatch.setenv("SWIM_ENABLE_DEMO_AUTH", "false")
    monkeypatch.delenv("SWIM_API_CORS_ORIGINS", raising=False)
    with pytest.raises(RuntimeError, match="must be configured"):
        application_settings_from_env()
    monkeypatch.setenv("SWIM_API_CORS_ORIGINS", "*")
    with pytest.raises(RuntimeError, match="Wildcard"):
        application_settings_from_env()


def test_sign_out_rejects_bodies_after_csrf_validation(api_client):
    headers = login_coach(api_client)
    response = api_client.post(
        "/api/auth/coach/sign-out",
        json={},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def _sign_in(client: TestClient, *, email: str = DEMO_PROVIDER_EMAIL, password: str = DEMO_PROVIDER_PASSWORD):
    return client.post(
        "/api/auth/coach/sign-in",
        json={"email": email, "password": password},
        headers=CSRF_HEADERS,
    )


def _add_instructor(instructor_id: str, name: str) -> None:
    with db.SessionLocal() as session:
        session.add(
            InstructorDB(
                id=instructor_id,
                name=name,
                main_address="1 Provider Way, Austin, TX",
                lat=30.2,
                lng=-97.7,
                work_start_time="08:00",
                work_end_time="17:00",
                timezone="America/Chicago",
            )
        )
        session.commit()
