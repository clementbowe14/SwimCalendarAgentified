from __future__ import annotations

import asyncio
import copy
import inspect
import os
import pickle
import subprocess
import sys
import textwrap
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, local

import pytest
from fastapi.testclient import TestClient

from app import config, db, tools
from app.api import create_app
from app.calendar_credentials import CalendarCredentialCipher
from app.external_calendar import CalendarBusySource, ExternalCalendarUnavailable
from app.google_calendar_busy_source import GoogleCalendarBusySource
from app.google_calendar_busy_source_factory import (
    GoogleCalendarBusySourceFactory,
)
from app.google_calendar_oauth import (
    GOOGLE_CALENDAR_CALLBACK_PATH,
    GoogleCalendarOAuthConfiguration,
)
from app.google_calendar_token_exchange import (
    GoogleCalendarTokenExchangeConfiguration,
)
import app.google_calendar_busy_source_factory as factory_module


TOKEN_CONFIGURATION = GoogleCalendarTokenExchangeConfiguration(
    authorization_configuration=GoogleCalendarOAuthConfiguration(
        client_id="123456789-dayweave.apps.googleusercontent.com",
        redirect_uri=(
            "https://api.dayweave.example" + GOOGLE_CALENDAR_CALLBACK_PATH
        ),
    ),
    client_secret="factory-client-secret-private",
)
CIPHER = CalendarCredentialCipher(
    active_key_id="factory-primary-private",
    keys={"factory-primary-private": b"f" * 32},
)


class CreatedSource:
    def __init__(self, configuration: object, credential_cipher: object):
        self.configuration = configuration
        self.credential_cipher = credential_cipher


def _session_factory():
    raise AssertionError("source construction must not open a database session")


def _factory_frame_locals(error: BaseException) -> list[dict]:
    frames = []
    traceback = error.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_filename.endswith(
            "/app/google_calendar_busy_source_factory.py"
        ):
            frames.append(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next
    return frames


def test_app_factory_is_distinct_route_inert_and_loads_no_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def forbidden_loader():
        calls.append("loader")
        raise AssertionError("calendar secrets must not load at startup")

    monkeypatch.setattr(
        config,
        "google_calendar_token_exchange_configuration_from_env",
        forbidden_loader,
    )
    monkeypatch.setattr(
        config,
        "calendar_credential_cipher_from_env",
        forbidden_loader,
    )
    monkeypatch.setattr(db, "init_db", lambda **_kwargs: None)

    first = create_app()
    second = create_app()
    assert type(first.state.calendar_busy_source_factory) is (
        GoogleCalendarBusySourceFactory
    )
    assert type(second.state.calendar_busy_source_factory) is (
        GoogleCalendarBusySourceFactory
    )
    assert (
        first.state.calendar_busy_source_factory
        is not second.state.calendar_busy_source_factory
    )
    assert "calendar_busy_source_factory" not in first.state.routing_readiness
    assert not hasattr(first.state.settings, "calendar_busy_source_factory")

    first.openapi()
    with TestClient(first) as client:
        assert client.get("/health").status_code == 200
    assert calls == []


def test_clean_api_import_does_not_invoke_secret_loaders() -> None:
    project_root = Path(__file__).resolve().parents[1]
    child_environment = os.environ.copy()
    child_environment["SWIM_ENV"] = "test"
    child_environment["SWIM_ENABLE_DEMO_DATA"] = "true"
    child_environment["SWIM_ENABLE_DEMO_AUTH"] = "true"
    script = "\n".join(
        (
            "import app.config as config",
            "def forbidden():",
            "    raise AssertionError('calendar secret loader ran at import')",
            (
                "config.google_calendar_token_exchange_configuration_from_env "
                "= forbidden"
            ),
            "config.calendar_credential_cipher_from_env = forbidden",
            "from app.api import create_app",
            "application = create_app()",
            (
                "assert application.state.calendar_busy_source_factory "
                "is not None"
            ),
        )
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=child_environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_app_factory_resolves_the_current_session_factory_at_create_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def first_session_factory():
        return None

    def second_session_factory():
        return None

    monkeypatch.setattr(db, "SessionLocal", first_session_factory)
    factory = create_app().state.calendar_busy_source_factory
    monkeypatch.setattr(db, "SessionLocal", second_session_factory)
    captured: list[object] = []
    monkeypatch.setattr(
        config,
        "google_calendar_token_exchange_configuration_from_env",
        lambda: object(),
    )
    monkeypatch.setattr(
        config,
        "calendar_credential_cipher_from_env",
        lambda: object(),
    )
    monkeypatch.setattr(
        factory_module,
        "_new_google_calendar_busy_source",
        lambda session_factory, **_kwargs: captured.append(session_factory)
        or CreatedSource(object(), object()),
    )

    factory.create()

    assert captured == [second_session_factory]


def test_create_loads_exact_dependencies_in_order_without_caching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    configurations = [object(), object()]
    ciphers = [object(), object()]
    created: list[CreatedSource] = []

    def load_configuration():
        value = configurations[len(created)]
        calls.append(("configuration", value))
        return value

    def load_cipher():
        value = ciphers[len(created)]
        calls.append(("cipher", value))
        return value

    def build_source(session_factory, *, configuration, credential_cipher):
        assert session_factory is _session_factory
        calls.append(("source", configuration))
        source = CreatedSource(configuration, credential_cipher)
        created.append(source)
        return source

    monkeypatch.setattr(
        config,
        "google_calendar_token_exchange_configuration_from_env",
        load_configuration,
    )
    monkeypatch.setattr(
        config,
        "calendar_credential_cipher_from_env",
        load_cipher,
    )
    monkeypatch.setattr(
        factory_module,
        "_new_google_calendar_busy_source",
        build_source,
    )
    monkeypatch.setattr(db, "SessionLocal", _session_factory)
    factory = GoogleCalendarBusySourceFactory()

    first = factory.create()
    second = factory.create()

    assert first is created[0]
    assert second is created[1]
    assert first is not second
    assert calls == [
        ("configuration", configurations[0]),
        ("cipher", ciphers[0]),
        ("source", configurations[0]),
        ("configuration", configurations[1]),
        ("cipher", ciphers[1]),
        ("source", configurations[1]),
    ]
    assert not hasattr(factory, "_session_factory")
    assert not hasattr(factory, "configuration")
    assert not hasattr(factory, "credential_cipher")


def test_real_create_returns_protocol_source_without_opening_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config,
        "google_calendar_token_exchange_configuration_from_env",
        lambda: TOKEN_CONFIGURATION,
    )
    monkeypatch.setattr(
        config,
        "calendar_credential_cipher_from_env",
        lambda: CIPHER,
    )

    monkeypatch.setattr(db, "SessionLocal", _session_factory)
    source = GoogleCalendarBusySourceFactory().create()

    assert type(source) is GoogleCalendarBusySource
    assert isinstance(source, CalendarBusySource)
    assert source._session_factory is _session_factory
    assert source._configuration is TOKEN_CONFIGURATION
    assert source._credential_cipher is CIPHER


@pytest.mark.parametrize(
    "failure_stage",
    ["configuration", "cipher", "source"],
)
def test_stage_failure_is_sanitized_short_circuits_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_stage: str,
) -> None:
    caplog.set_level("DEBUG")
    marker = "factory-collaborator-secret-private"
    configuration = object()
    credential_cipher = object()
    recovered = CreatedSource(configuration, credential_cipher)
    failed = False
    calls: list[str] = []

    def load_configuration():
        nonlocal failed
        calls.append("configuration")
        if failure_stage == "configuration" and not failed:
            failed = True
            raise RuntimeError(marker)
        return configuration

    def load_cipher():
        nonlocal failed
        calls.append("cipher")
        if failure_stage == "cipher" and not failed:
            failed = True
            raise RuntimeError(marker)
        return credential_cipher

    def build_source(*_args, **_kwargs):
        nonlocal failed
        calls.append("source")
        if failure_stage == "source" and not failed:
            failed = True
            raise RuntimeError(marker)
        return recovered

    monkeypatch.setattr(
        config,
        "google_calendar_token_exchange_configuration_from_env",
        load_configuration,
    )
    monkeypatch.setattr(
        config,
        "calendar_credential_cipher_from_env",
        load_cipher,
    )
    monkeypatch.setattr(
        factory_module,
        "_new_google_calendar_busy_source",
        build_source,
    )
    monkeypatch.setattr(db, "SessionLocal", _session_factory)
    factory = GoogleCalendarBusySourceFactory()

    with pytest.raises(ExternalCalendarUnavailable) as captured:
        factory.create()

    assert type(captured.value) is ExternalCalendarUnavailable
    assert captured.value.reason_code == "provider_unavailable"
    assert str(captured.value) == (
        "External calendar availability could not be verified."
    )
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True
    assert marker not in repr(captured.value)
    assert marker not in caplog.text
    assert all(
        marker not in repr(frame_locals)
        for frame_locals in _factory_frame_locals(captured.value)
    )
    for frame_locals in _factory_frame_locals(captured.value):
        assert configuration not in frame_locals.values()
        assert credential_cipher not in frame_locals.values()
        assert recovered not in frame_locals.values()

    expected_first_calls = {
        "configuration": ["configuration"],
        "cipher": ["configuration", "cipher"],
        "source": ["configuration", "cipher", "source"],
    }[failure_stage]
    assert calls == expected_first_calls

    assert factory.create() is recovered
    assert calls == [
        *expected_first_calls,
        "configuration",
        "cipher",
        "source",
    ]


@pytest.mark.parametrize(
    ("raised", "expected_type", "expected_code"),
    [
        (asyncio.CancelledError("cancel-private"), asyncio.CancelledError, None),
        (KeyboardInterrupt("keyboard-private"), KeyboardInterrupt, None),
        (SystemExit("system-private"), SystemExit, 1),
        (GeneratorExit("generator-private"), GeneratorExit, None),
        (
            BaseExceptionGroup(
                "group-private",
                [asyncio.CancelledError("nested-cancel-private")],
            ),
            asyncio.CancelledError,
            None,
        ),
        (
            BaseExceptionGroup(
                "group-private",
                [KeyboardInterrupt("nested-keyboard-private")],
            ),
            KeyboardInterrupt,
            None,
        ),
        (
            BaseExceptionGroup(
                "group-private",
                [SystemExit("nested-system-private")],
            ),
            SystemExit,
            1,
        ),
        (
            BaseExceptionGroup(
                "group-private",
                [GeneratorExit("nested-generator-private")],
            ),
            GeneratorExit,
            None,
        ),
        (
            BaseExceptionGroup(
                "group-private",
                [
                    asyncio.CancelledError("nested-cancel-private"),
                    BaseExceptionGroup(
                        "nested-group-private",
                        [KeyboardInterrupt("nested-keyboard-private")],
                    ),
                ],
            ),
            KeyboardInterrupt,
            None,
        ),
    ],
)
def test_process_controls_are_fresh_and_cause_free(
    monkeypatch: pytest.MonkeyPatch,
    raised: BaseException,
    expected_type: type[BaseException],
    expected_code: int | None,
) -> None:
    def interrupt():
        raise raised

    monkeypatch.setattr(
        config,
        "google_calendar_token_exchange_configuration_from_env",
        interrupt,
    )
    monkeypatch.setattr(db, "SessionLocal", _session_factory)
    factory = GoogleCalendarBusySourceFactory()

    with pytest.raises(expected_type) as captured:
        factory.create()

    assert captured.value is not raised
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True
    assert "private" not in str(captured.value)
    if expected_type is SystemExit:
        assert captured.value.code == expected_code


@pytest.mark.parametrize(
    ("graph_kind", "expected_type"),
    [
        ("exact-bound", KeyboardInterrupt),
        ("overflow", asyncio.CancelledError),
        ("shared-dag", KeyboardInterrupt),
        ("wide-shared-overflow", asyncio.CancelledError),
    ],
)
def test_grouped_process_control_classification_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    graph_kind: str,
    expected_type: type[BaseException],
) -> None:
    if graph_kind == "exact-bound":
        children: list[BaseException] = [
            RuntimeError() for _index in range(4_094)
        ]
        children.append(KeyboardInterrupt("keyboard-private"))
        failure = BaseExceptionGroup("exact-bound-private", children)
    elif graph_kind == "overflow":
        failure = BaseExceptionGroup(
            "overflow-private",
            [RuntimeError() for _index in range(4_096)],
        )
    elif graph_kind == "shared-dag":
        shared = ExceptionGroup("shared-private", [RuntimeError()])
        failure = BaseExceptionGroup(
            "shared-dag-private",
            [*([shared] * 4_096), KeyboardInterrupt("keyboard-private")],
        )
    else:
        shared = ExceptionGroup("wide-shared-private", [RuntimeError()])
        failure = BaseExceptionGroup(
            "wide-shared-overflow-private",
            [*([shared] * 8_192), KeyboardInterrupt("keyboard-private")],
        )

    def interrupt():
        raise failure

    monkeypatch.setattr(
        config,
        "google_calendar_token_exchange_configuration_from_env",
        interrupt,
    )
    monkeypatch.setattr(db, "SessionLocal", _session_factory)

    with pytest.raises(expected_type) as captured:
        GoogleCalendarBusySourceFactory().create()

    assert captured.value is not failure
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "line_fragment",
    [
        "configuration = (",
        "credential_cipher = config.calendar_credential_cipher_from_env()",
        "source = _new_google_calendar_busy_source(",
        "try: configuration = credential_cipher = source = None",
    ],
    ids=["configuration", "cipher", "source", "cleanup"],
)
def test_interrupted_construction_retains_no_secret_objects(
    monkeypatch: pytest.MonkeyPatch,
    line_fragment: str,
) -> None:
    configuration = object()
    credential_cipher = object()
    created_source = CreatedSource(configuration, credential_cipher)
    monkeypatch.setattr(
        config,
        "google_calendar_token_exchange_configuration_from_env",
        lambda: configuration,
    )
    monkeypatch.setattr(
        config,
        "calendar_credential_cipher_from_env",
        lambda: credential_cipher,
    )
    monkeypatch.setattr(
        factory_module,
        "_new_google_calendar_busy_source",
        lambda *_args, **_kwargs: created_source,
    )
    target = factory_module._create_google_calendar_busy_source
    source_lines, source_start = inspect.getsourcelines(target)
    target_line = source_start + next(
        index
        for index, source_line in enumerate(source_lines)
        if line_fragment in source_line
    )
    interrupted = False

    def interrupt_construction(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is target.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt("factory-interruption-private")
        return interrupt_construction

    try:
        sys.settrace(interrupt_construction)
        with pytest.raises(KeyboardInterrupt) as captured:
            GoogleCalendarBusySourceFactory().create()
    finally:
        sys.settrace(None)

    assert interrupted is True
    assert captured.value.__context__ is None
    for frame_locals in _factory_frame_locals(captured.value):
        assert configuration not in frame_locals.values()
        assert credential_cipher not in frame_locals.values()
        assert created_source not in frame_locals.values()


def test_shared_factory_is_safe_for_concurrent_fresh_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_state = local()
    configuration_loaded = Barrier(8)
    cipher_entered = Barrier(8)
    cipher_loaded = Barrier(8)

    def load_configuration():
        value = object()
        thread_state.configuration = value
        configuration_loaded.wait(timeout=5)
        return value

    def load_cipher():
        cipher_entered.wait(timeout=5)
        value = object()
        thread_state.credential_cipher = value
        cipher_loaded.wait(timeout=5)
        return value

    def build_source(_session_factory, *, configuration, credential_cipher):
        assert configuration is thread_state.configuration
        assert credential_cipher is thread_state.credential_cipher
        return CreatedSource(configuration, credential_cipher)

    monkeypatch.setattr(
        config,
        "google_calendar_token_exchange_configuration_from_env",
        load_configuration,
    )
    monkeypatch.setattr(
        config,
        "calendar_credential_cipher_from_env",
        load_cipher,
    )
    monkeypatch.setattr(
        factory_module,
        "_new_google_calendar_busy_source",
        build_source,
    )
    monkeypatch.setattr(db, "SessionLocal", _session_factory)
    factory = GoogleCalendarBusySourceFactory()

    with ThreadPoolExecutor(max_workers=8) as executor:
        sources = list(executor.map(lambda _index: factory.create(), range(32)))

    assert len({id(source) for source in sources}) == 32
    assert len({id(source.configuration) for source in sources}) == 32
    assert len({id(source.credential_cipher) for source in sources}) == 32


def test_shared_factory_supports_reentrant_fresh_creation() -> None:
    project_root = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(
        """
        from app import config, db
        import app.google_calendar_busy_source_factory as factory_module
        from app.google_calendar_busy_source_factory import (
            GoogleCalendarBusySourceFactory,
        )

        class CreatedSource:
            def __init__(self, configuration, credential_cipher):
                self.configuration = configuration
                self.credential_cipher = credential_cipher

        factory = GoogleCalendarBusySourceFactory()
        outer_configuration = object()
        inner_configuration = object()
        follow_up_configuration = object()
        outer_cipher = object()
        inner_cipher = object()
        follow_up_cipher = object()
        configuration_calls = 0
        cipher_calls = 0
        inner_source = None
        call_order = []

        def load_configuration():
            global configuration_calls, inner_source
            configuration_calls += 1
            if configuration_calls == 1:
                call_order.append("outer-configuration-start")
                inner_source = factory.create()
                call_order.append("outer-configuration-return")
                return outer_configuration
            if configuration_calls == 2:
                call_order.append("inner-configuration")
                return inner_configuration
            call_order.append("follow-up-configuration")
            return follow_up_configuration

        def load_cipher():
            global cipher_calls
            cipher_calls += 1
            if cipher_calls == 1:
                call_order.append("inner-cipher")
                return inner_cipher
            if cipher_calls == 2:
                call_order.append("outer-cipher")
                return outer_cipher
            call_order.append("follow-up-cipher")
            return follow_up_cipher

        def build_source(
            _session_factory,
            *,
            configuration,
            credential_cipher,
        ):
            call_order.append("source")
            expected_pairs = {
                id(inner_configuration): inner_cipher,
                id(outer_configuration): outer_cipher,
                id(follow_up_configuration): follow_up_cipher,
            }
            assert credential_cipher is expected_pairs[id(configuration)]
            return CreatedSource(configuration, credential_cipher)

        config.google_calendar_token_exchange_configuration_from_env = (
            load_configuration
        )
        config.calendar_credential_cipher_from_env = load_cipher
        factory_module._new_google_calendar_busy_source = build_source
        db.SessionLocal = lambda: None

        outer_source = factory.create()
        follow_up_source = factory.create()

        assert inner_source.configuration is inner_configuration
        assert inner_source.credential_cipher is inner_cipher
        assert outer_source.configuration is outer_configuration
        assert outer_source.credential_cipher is outer_cipher
        assert follow_up_source.configuration is follow_up_configuration
        assert follow_up_source.credential_cipher is follow_up_cipher
        assert len(
            {id(inner_source), id(outer_source), id(follow_up_source)}
        ) == 3
        assert call_order == [
            "outer-configuration-start",
            "inner-configuration",
            "inner-cipher",
            "source",
            "outer-configuration-return",
            "outer-cipher",
            "source",
            "follow-up-configuration",
            "follow-up-cipher",
            "source",
        ]
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_factory_is_final_immutable_redacted_and_nonserializable() -> None:
    factory = GoogleCalendarBusySourceFactory()
    assert repr(factory) == "GoogleCalendarBusySourceFactory(<redacted>)"
    assert str(factory) == repr(factory)

    with pytest.raises(TypeError):
        type("FactorySubclass", (GoogleCalendarBusySourceFactory,), {})
    with pytest.raises(AttributeError):
        factory.session_factory = lambda: None
    with pytest.raises(AttributeError):
        del factory.session_factory
    with pytest.raises(TypeError):
        pickle.dumps(factory)
    with pytest.raises(TypeError):
        copy.copy(factory)


@pytest.mark.parametrize(
    ("operation", "expected_type"),
    [
        ("subclass", TypeError),
        ("setattr", AttributeError),
        ("delattr", AttributeError),
        ("reduce", TypeError),
        ("reduce-ex", TypeError),
        ("getstate", TypeError),
        ("pickle", TypeError),
        ("copy", TypeError),
    ],
)
def test_factory_rejections_clear_ambient_exception_context(
    operation: str,
    expected_type: type[BaseException],
) -> None:
    factory = GoogleCalendarBusySourceFactory()
    marker = "ambient-factory-secret-private"

    def reject() -> None:
        if operation == "subclass":
            type("FactorySubclass", (GoogleCalendarBusySourceFactory,), {})
        elif operation == "setattr":
            factory.value = object()
        elif operation == "delattr":
            del factory.value
        elif operation == "reduce":
            factory.__reduce__()
        elif operation == "reduce-ex":
            factory.__reduce_ex__(pickle.HIGHEST_PROTOCOL)
        elif operation == "getstate":
            factory.__getstate__()
        elif operation == "pickle":
            pickle.dumps(factory)
        else:
            copy.copy(factory)

    try:
        raise RuntimeError(marker)
    except RuntimeError:
        with pytest.raises(expected_type) as captured:
            reject()

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True
    formatted = "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )
    assert marker not in formatted


def test_wrong_dependency_types_fail_without_source_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config,
        "google_calendar_token_exchange_configuration_from_env",
        lambda: object(),
    )
    monkeypatch.setattr(
        config,
        "calendar_credential_cipher_from_env",
        lambda: object(),
    )

    with pytest.raises(ExternalCalendarUnavailable) as captured:
        GoogleCalendarBusySourceFactory().create()

    assert captured.value.reason_code == "provider_unavailable"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
