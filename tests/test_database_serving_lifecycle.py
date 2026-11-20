from __future__ import annotations

import asyncio
import inspect
import sys

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import create_app


def test_database_binding_cannot_change_during_overlapping_app_lifespans(
    migrated_postgres_db,
) -> None:
    original_factory = db.SessionLocal
    with TestClient(create_app(), raise_server_exceptions=False):
        with TestClient(create_app(), raise_server_exceptions=False):
            with pytest.raises(RuntimeError, match="cannot be reconfigured"):
                db.configure_database()
            assert db.SessionLocal is original_factory
        with pytest.raises(RuntimeError, match="cannot be reconfigured"):
            db.configure_database()
        assert db.SessionLocal is original_factory

    db.configure_database()
    assert db.SessionLocal is not original_factory


def test_database_binding_lease_releases_after_startup_failure(
    migrated_postgres_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_factory = db.SessionLocal

    def fail_startup(*, settings):
        del settings
        raise RuntimeError("startup-private")

    monkeypatch.setattr(db, "init_db", fail_startup)
    with pytest.raises(RuntimeError, match="startup-private"):
        with TestClient(create_app(), raise_server_exceptions=False):
            pass

    db.configure_database()
    assert db.SessionLocal is not original_factory


def test_database_binding_lease_releases_after_registration_interruption(
    migrated_postgres_db,
) -> None:
    lifecycle = db.database_serving_lifecycle.__wrapped__
    source_lines, source_start = inspect.getsourcelines(lifecycle)
    target_line = source_start + next(
        index
        for index, source_line in enumerate(source_lines)
        if source_line.strip() == "yield"
    )
    interrupted = False

    def interrupt_after_registration(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is lifecycle.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt("lifecycle-private")
        return interrupt_after_registration

    try:
        sys.settrace(interrupt_after_registration)
        with pytest.raises(KeyboardInterrupt, match="lifecycle-private"):
            with db.database_serving_lifecycle():
                pass
    finally:
        sys.settrace(None)

    assert interrupted is True
    db.configure_database()


@pytest.mark.parametrize(
    "target_fragment",
    ["with _database_binding_lock:", "discard(lease)"],
)
def test_database_binding_cleanup_interruption_removes_only_target_lease(
    migrated_postgres_db,
    target_fragment: str,
) -> None:
    first = db.database_serving_lifecycle()
    second = db.database_serving_lifecycle()
    first.__enter__()
    second.__enter__()
    release = db._discard_database_serving_lease
    source_lines, source_start = inspect.getsourcelines(release)
    target_line = source_start + next(
        index
        for index, source_line in enumerate(source_lines)
        if target_fragment in source_line
    )
    interrupted = KeyboardInterrupt("lease-cleanup-private")
    injected = False

    def interrupt_cleanup(frame, event, _argument):
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code is release.__code__
            and frame.f_lineno == target_line
        ):
            injected = True
            raise interrupted
        return interrupt_cleanup

    try:
        sys.settrace(interrupt_cleanup)
        with pytest.raises(KeyboardInterrupt) as captured:
            first.__exit__(None, None, None)
        assert injected is True
        assert captured.value is not interrupted
        assert captured.value.args == ()
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        with pytest.raises(RuntimeError, match="cannot be reconfigured"):
            db.configure_database()
    finally:
        sys.settrace(None)
        second.__exit__(None, None, None)

    db.configure_database()


@pytest.mark.parametrize(
    ("control", "relation", "grouped", "expected_type", "expected_args"),
    [
        (asyncio.CancelledError("masked-private"), "context", False, asyncio.CancelledError, ()),
        (KeyboardInterrupt("masked-private"), "cause", False, KeyboardInterrupt, ()),
        (SystemExit(29), "cause", True, SystemExit, (29,)),
        (GeneratorExit("masked-private"), "context", True, GeneratorExit, ()),
    ],
)
def test_database_binding_cleanup_reemits_masked_process_controls(
    migrated_postgres_db,
    monkeypatch: pytest.MonkeyPatch,
    control: BaseException,
    relation: str,
    grouped: bool,
    expected_type: type[BaseException],
    expected_args: tuple,
) -> None:
    original_discard = db._discard_database_serving_lease
    cleanup_failure = RuntimeError("cleanup-private")
    masked: BaseException = control
    if grouped:
        masked = BaseExceptionGroup(
            "cleanup-private-group",
            [RuntimeError("ordinary-private"), control],
        )
    if relation == "cause":
        cleanup_failure.__cause__ = masked
    else:
        cleanup_failure.__context__ = masked
    calls = 0

    def fail_once(lease: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise cleanup_failure
        original_discard(lease)

    monkeypatch.setattr(db, "_discard_database_serving_lease", fail_once)

    with pytest.raises(expected_type) as captured:
        with db.database_serving_lifecycle():
            pass

    assert calls >= 2
    assert captured.value is not control
    assert captured.value.args == expected_args
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    db.configure_database()
