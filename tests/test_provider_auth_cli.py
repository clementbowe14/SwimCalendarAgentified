import pytest
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.auth import authenticate_coach, validate_coach_session
from app.api_errors import InvalidAuthToken
from app.models import InstructorDB, ProviderAccountDB
from scripts import manage_provider_auth


def test_cli_provisions_and_resets_without_password_arguments_or_secret_output(
    migrated_postgres_db,
    monkeypatch,
    capsys,
):
    with db.SessionLocal() as session:
        session.add(
            InstructorDB(
                id="inst_cli",
                name="CLI Provider",
                main_address="3 CLI Way",
                lat=30.2,
                lng=-97.2,
                work_start_time="08:00",
                work_end_time="17:00",
                timezone="America/Chicago",
            )
        )
        session.commit()

    prompts = iter(["first-secure-password", "first-secure-password"])
    monkeypatch.setattr(manage_provider_auth.getpass, "getpass", lambda _: next(prompts))
    assert manage_provider_auth.main(
        ["provision", "--instructor-id", "inst_cli", "--email", "CLI@Example.com"]
    ) == 0
    output = capsys.readouterr()
    assert "Provisioned provider account" in output.out
    assert "first-secure-password" not in output.out + output.err

    issued = authenticate_coach("cli@example.com", "first-secure-password")
    prompts = iter(["second-secure-password", "second-secure-password"])
    monkeypatch.setattr(manage_provider_auth.getpass, "getpass", lambda _: next(prompts))
    assert manage_provider_auth.main(
        ["reset-password", "--email", "cli@example.com"]
    ) == 0
    assert "second-secure-password" not in "".join(capsys.readouterr())
    with pytest.raises(InvalidAuthToken):
        validate_coach_session(issued.raw_token)
    assert authenticate_coach(
        "cli@example.com",
        "second-secure-password",
    ).principal.instructor_id == "inst_cli"


def test_cli_revoke_disable_and_enable_are_safe_and_session_versioned(
    migrated_postgres_db,
    monkeypatch,
):
    issued = authenticate_coach("coach@example.com", "swim-dev-pass")
    assert manage_provider_auth.main(
        ["revoke-sessions", "--email", "coach@example.com"]
    ) == 0
    with pytest.raises(InvalidAuthToken):
        validate_coach_session(issued.raw_token)

    assert manage_provider_auth.main(["disable", "--email", "coach@example.com"]) == 0
    with db.SessionLocal() as session:
        account = session.query(ProviderAccountDB).filter_by(
            email_normalized="coach@example.com"
        ).one()
        assert account.disabled_at is not None

    assert manage_provider_auth.main(["enable", "--email", "coach@example.com"]) == 0
    assert authenticate_coach(
        "coach@example.com",
        "swim-dev-pass",
    ).principal.instructor_id == "inst_001"


def test_cli_has_no_password_option():
    with pytest.raises(SystemExit):
        manage_provider_auth.build_parser().parse_args(
            [
                "provision",
                "--instructor-id",
                "inst_001",
                "--email",
                "coach@example.com",
                "--password",
                "must-not-be-accepted",
            ]
        )


def test_cli_redacts_database_failure_details(monkeypatch, capsys):
    secret = "$argon2id$credential-material"

    def fail_safely(_: str) -> None:
        raise SQLAlchemyError(secret)

    monkeypatch.setattr(
        manage_provider_auth,
        "revoke_all_provider_sessions",
        fail_safely,
    )
    assert manage_provider_auth.main(
        ["revoke-sessions", "--email", "coach@example.com"]
    ) == 2
    output = capsys.readouterr()
    assert "Database operation failed" in output.err
    assert secret not in output.out + output.err
