from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import sys

import pytest

from app import api_services
from app.api_errors import CalendarAvailabilityUnavailable
from app.api_schemas import (
    AvailabilitySearchRequest,
    LocationResponse,
    PublicAvailabilitySearchRequest,
)
from app.external_calendar import (
    ExternalCalendarAuthorizationRequired,
    ExternalCalendarInvalidResponse,
    ExternalCalendarTimeout,
    ExternalCalendarUnavailable,
)
from app.public_tokens import InviteContext


THURSDAY = dt.date(2026, 8, 20)
LOCATION = LocationResponse(
    address="Provider location",
    lat=30.0,
    lng=-97.0,
    is_instructor_facility=True,
)


def _contains_identity(value: object, target: object) -> bool:
    if value is target:
        return True
    if isinstance(value, dict):
        return any(
            _contains_identity(item, target)
            for pair in value.items()
            for item in pair
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_identity(item, target) for item in value)
    return False


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    pending = [error]
    seen: set[int] = set()
    chain: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return tuple(chain)


def _coach_request() -> AvailabilitySearchRequest:
    return AvailabilitySearchRequest(
        client_name="Client",
        client_address="Provider location",
        requested_date=THURSDAY,
        is_instructor_facility=True,
        duration_mins=60,
    )


def _public_request() -> PublicAvailabilitySearchRequest:
    return PublicAvailabilitySearchRequest(
        client_name="Client",
        client_address="Provider location",
        requested_date=THURSDAY,
        location_type="facility",
        duration_mins=60,
    )


def _invite() -> InviteContext:
    return InviteContext(
        token="invite-calendar-source",
        instructor_id="inst_calendar_source",
        instructor_name="Provider",
        business_name="Provider business",
        location_label="Provider location",
        service_name="Service",
        service_location=None,
        preferred_duration_mins=60,
        allowed_durations_mins=(60,),
        expires_at=dt.datetime(2026, 8, 21),
    )


@pytest.mark.parametrize("surface", ["coach", "public"])
def test_availability_services_forward_one_calendar_source(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    source = object()
    captured_sources: list[object] = []

    def find_slots(**kwargs):
        captured_sources.append(kwargs["calendar_busy_source"])
        return []

    monkeypatch.setattr(api_services, "_find_slots", find_slots)
    monkeypatch.setattr(
        api_services,
        "_resolve_location",
        lambda *_args, **_kwargs: LOCATION,
    )

    if surface == "coach":
        response = api_services.search_coach_availability(
            "inst_calendar_source",
            _coach_request(),
            calendar_busy_source=source,
        )
    else:
        response = api_services.search_public_availability(
            _invite(),
            _public_request(),
            calendar_busy_source=source,
        )

    assert response.available_slots == []
    assert captured_sources == [source]


@pytest.mark.parametrize(
    "provider_error",
    [
        ExternalCalendarAuthorizationRequired(),
        ExternalCalendarTimeout(),
        ExternalCalendarUnavailable("provider_unavailable"),
        ExternalCalendarInvalidResponse(),
    ],
)
def test_calendar_failures_map_to_one_stable_api_error(
    monkeypatch: pytest.MonkeyPatch,
    provider_error: ExternalCalendarUnavailable,
) -> None:
    source = object()

    def find_slots(**kwargs):
        assert kwargs["calendar_busy_source"] is source
        raise provider_error

    monkeypatch.setattr(
        api_services.tools,
        "find_available_swim_slots",
        find_slots,
    )

    with pytest.raises(CalendarAvailabilityUnavailable) as captured:
        api_services._find_slots(
            instructor_id="inst_calendar_source",
            requested_date=THURSDAY,
            client_address="Provider location",
            is_instructor_facility=True,
            duration_mins=60,
            calendar_busy_source=source,
        )

    assert captured.value.code == "CALENDAR_AVAILABILITY_UNAVAILABLE"
    assert captured.value.status_code == 503
    assert captured.value.message == (
        "Calendar availability could not be verified."
    )
    assert captured.value.details is None
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True
    api_frames = []
    current = captured.value.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith("/app/api_services.py"):
            api_frames.append(current.tb_frame.f_locals)
        current = current.tb_next
    assert api_frames
    assert all(source not in frame.values() for frame in api_frames)


@pytest.mark.parametrize("surface", ["coach", "public"])
def test_service_failure_tracebacks_do_not_retain_calendar_source(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    source = object()

    def fail(**_kwargs):
        raise CalendarAvailabilityUnavailable()

    monkeypatch.setattr(api_services, "_find_slots", fail)

    with pytest.raises(CalendarAvailabilityUnavailable) as captured:
        if surface == "coach":
            api_services.search_coach_availability(
                "inst_calendar_source",
                _coach_request(),
                calendar_busy_source=source,
            )
        else:
            api_services.search_public_availability(
                _invite(),
                _public_request(),
                calendar_busy_source=source,
            )

    service_frames = []
    current = captured.value.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith("/app/api_services.py"):
            service_frames.append(current.tb_frame.f_locals)
        current = current.tb_next
    assert service_frames
    assert all(source not in frame.values() for frame in service_frames)


@pytest.mark.parametrize(
    ("target_name", "line_fragment", "surface"),
    [
        (
            "search_coach_availability",
            "try: slots = _find_slots(",
            "coach",
        ),
        (
            "search_coach_availability",
            "try: calendar_busy_source = None",
            "coach",
        ),
        (
            "search_public_availability",
            "try: slots = _find_slots(",
            "public",
        ),
        (
            "search_public_availability",
            "try: calendar_busy_source = None",
            "public",
        ),
        (
            "_find_slots",
            "try: control_kind = None; system_exit_code = None; cleanup_failure = False; return _find_slots_evaluated(",
            "shared",
        ),
        (
            "_find_slots",
            "try: calendar_busy_source = None",
            "shared",
        ),
    ],
)
def test_control_interruption_does_not_retain_calendar_source(
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    line_fragment: str,
    surface: str,
) -> None:
    source = object()
    target = getattr(api_services, target_name)
    source_lines, source_start = inspect.getsourcelines(target)
    target_line = source_start + next(
        index
        for index, source_line in enumerate(source_lines)
        if line_fragment in source_line
    )
    interrupted = False

    monkeypatch.setattr(
        api_services,
        "_resolve_location",
        lambda *_args, **_kwargs: LOCATION,
    )
    if surface != "shared":
        monkeypatch.setattr(api_services, "_find_slots", lambda **_kwargs: [])
    else:
        monkeypatch.setattr(
            api_services.tools,
            "find_available_swim_slots",
            lambda **_kwargs: [],
        )

    def interrupt(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is target.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt("calendar-source-interruption-private")
        return interrupt

    try:
        sys.settrace(interrupt)
        with pytest.raises(KeyboardInterrupt) as captured:
            if surface == "coach":
                target(
                    "inst_calendar_source",
                    _coach_request(),
                    calendar_busy_source=source,
                )
            elif surface == "public":
                target(
                    _invite(),
                    _public_request(),
                    calendar_busy_source=source,
                )
            else:
                target(
                    instructor_id="inst_calendar_source",
                    requested_date=THURSDAY,
                    client_address="Provider location",
                    is_instructor_facility=True,
                    duration_mins=60,
                    calendar_busy_source=source,
                )
    finally:
        sys.settrace(None)

    assert interrupted is True
    assert captured.value.__context__ is None
    source_frames = []
    current = captured.value.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith("/app/api_services.py"):
            source_frames.append(current.tb_frame.f_locals)
        current = current.tb_next
    assert source_frames
    assert all(
        not any(
            _contains_identity(value, source)
            for value in frame_locals.values()
        )
        for frame_locals in source_frames
    )


@pytest.mark.parametrize(
    "line_fragment",
    [
        "error = CalendarAvailabilityUnavailable()",
        "raise error.with_traceback(None) from None",
        "raised.__cause__ = None",
    ],
    ids=["entry", "raise", "cleanup"],
)
def test_error_translation_interruption_detaches_provider_chain(
    monkeypatch: pytest.MonkeyPatch,
    line_fragment: str,
) -> None:
    source = object()
    private_failure = RuntimeError("provider-transport-secret-private")
    provider_error = ExternalCalendarTimeout()
    provider_error.__cause__ = private_failure
    target = api_services._raise_calendar_availability_unavailable
    source_lines, source_start = inspect.getsourcelines(target)
    target_line = source_start + next(
        index
        for index, source_line in enumerate(source_lines)
        if line_fragment in source_line
    )
    interrupted = False

    def fail(**_kwargs):
        raise provider_error

    monkeypatch.setattr(
        api_services.tools,
        "find_available_swim_slots",
        fail,
    )

    def interrupt(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is target.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt("translation-interruption-private")
        return interrupt

    try:
        sys.settrace(interrupt)
        with pytest.raises(KeyboardInterrupt) as captured:
            api_services._find_slots(
                instructor_id="inst_calendar_source",
                requested_date=THURSDAY,
                client_address="Provider location",
                is_instructor_facility=True,
                duration_mins=60,
                calendar_busy_source=source,
            )
    finally:
        sys.settrace(None)

    assert interrupted is True
    chain = _exception_chain(captured.value)
    assert provider_error not in chain
    assert private_failure not in chain
    source_frames = []
    current = captured.value.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith("/app/api_services.py"):
            source_frames.append(current.tb_frame.f_locals)
        current = current.tb_next
    assert source_frames
    assert all(
        not any(
            _contains_identity(value, source)
            for value in frame_locals.values()
        )
        for frame_locals in source_frames
    )


@pytest.mark.parametrize(
    "line_fragment",
    [
        "except ExternalCalendarUnavailable:",
        "try: calendar_failure = True",
    ],
    ids=["dispatch", "transition"],
)
def test_provider_handler_interruption_detaches_provider_chain(
    monkeypatch: pytest.MonkeyPatch,
    line_fragment: str,
) -> None:
    source = object()
    private_failure = RuntimeError("provider-handler-secret-private")
    provider_error = ExternalCalendarTimeout()
    provider_error.__cause__ = private_failure
    target = api_services._find_slots_evaluated
    source_lines, source_start = inspect.getsourcelines(target)
    target_line = source_start + next(
        index
        for index, source_line in enumerate(source_lines)
        if line_fragment in source_line
    )
    interrupted = False

    def fail(**_kwargs):
        raise provider_error

    monkeypatch.setattr(
        api_services.tools,
        "find_available_swim_slots",
        fail,
    )

    def interrupt(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is target.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt("handler-interruption-private")
        return interrupt

    try:
        sys.settrace(interrupt)
        with pytest.raises(KeyboardInterrupt) as captured:
            api_services._find_slots(
                instructor_id="inst_calendar_source",
                requested_date=THURSDAY,
                client_address="Provider location",
                is_instructor_facility=True,
                duration_mins=60,
                calendar_busy_source=source,
            )
    finally:
        sys.settrace(None)

    assert interrupted is True
    chain = _exception_chain(captured.value)
    assert provider_error not in chain
    assert private_failure not in chain
    source_frames = []
    current = captured.value.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith("/app/api_services.py"):
            source_frames.append(current.tb_frame.f_locals)
        current = current.tb_next
    assert source_frames
    assert all(
        not any(
            _contains_identity(value, source)
            for value in frame_locals.values()
        )
        for frame_locals in source_frames
    )


@pytest.mark.parametrize(
    ("raised", "expected_type", "expected_code"),
    [
        (asyncio.CancelledError("cancel-private"), asyncio.CancelledError, None),
        (KeyboardInterrupt("keyboard-private"), KeyboardInterrupt, None),
        (SystemExit("system-private"), SystemExit, 1),
        (GeneratorExit("generator-private"), GeneratorExit, None),
    ],
)
def test_outer_boundary_reemits_fresh_cause_free_controls(
    monkeypatch: pytest.MonkeyPatch,
    raised: BaseException,
    expected_type: type[BaseException],
    expected_code: int | None,
) -> None:
    source = object()
    private_values = [raised]
    if type(raised) is SystemExit:
        private_values.append(raised.code)

    def interrupt(**_kwargs):
        raise raised

    monkeypatch.setattr(
        api_services,
        "_find_slots_evaluated",
        interrupt,
    )

    with pytest.raises(expected_type) as captured:
        api_services._find_slots(
            instructor_id="inst_calendar_source",
            requested_date=THURSDAY,
            client_address="Provider location",
            is_instructor_facility=True,
            duration_mins=60,
            calendar_busy_source=source,
        )

    assert captured.value is not raised
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True
    assert "private" not in str(captured.value)
    if expected_type is SystemExit:
        assert captured.value.code == expected_code
    source_frames = []
    current = captured.value.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith("/app/api_services.py"):
            source_frames.append(current.tb_frame.f_locals)
        current = current.tb_next
    assert source_frames
    assert all("private" not in repr(frame) for frame in source_frames)
    assert all(
        not any(
            _contains_identity(value, private_value)
            for value in frame.values()
        )
        for private_value in private_values
        for frame in source_frames
    )
    assert all(
        not any(
            _contains_identity(value, source)
            for value in frame_locals.values()
        )
        for frame_locals in source_frames
    )


@pytest.mark.parametrize(
    ("line_fragment", "cleanup_interruption", "expected_type"),
    [
        (
            "type(interrupted.code) is int",
            KeyboardInterrupt("cleanup-interruption-private"),
            KeyboardInterrupt,
        ),
        (
            "interrupted = None",
            KeyboardInterrupt("cleanup-interruption-private"),
            KeyboardInterrupt,
        ),
        (
            "type(interrupted.code) is int",
            UnboundLocalError("ordinary-cleanup-private"),
            RuntimeError,
        ),
    ],
    ids=["payload", "exception", "ordinary-failure"],
)
def test_system_exit_cleanup_interruption_retains_no_private_state(
    monkeypatch: pytest.MonkeyPatch,
    line_fragment: str,
    cleanup_interruption: BaseException,
    expected_type: type[BaseException],
) -> None:
    source = object()
    private_payload = object()
    original = SystemExit(private_payload)
    target = api_services._find_slots
    source_lines, source_start = inspect.getsourcelines(target)
    target_line = source_start + next(
        index
        for index, source_line in enumerate(source_lines)
        if line_fragment in source_line
    )
    interrupted = False

    def fail(**_kwargs):
        raise original

    monkeypatch.setattr(api_services, "_find_slots_evaluated", fail)

    def interrupt(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is target.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise cleanup_interruption
        return interrupt

    try:
        sys.settrace(interrupt)
        with pytest.raises(expected_type) as captured:
            target(
                instructor_id="inst_calendar_source",
                requested_date=THURSDAY,
                client_address="Provider location",
                is_instructor_facility=True,
                duration_mins=60,
                calendar_busy_source=source,
            )
    finally:
        sys.settrace(None)

    assert interrupted is True
    assert captured.value is not cleanup_interruption
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True
    chain = _exception_chain(captured.value)
    assert original not in chain
    assert cleanup_interruption not in chain
    source_frames = []
    current = captured.value.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith("/app/api_services.py"):
            source_frames.append(current.tb_frame.f_locals)
        current = current.tb_next
    assert source_frames
    for private_value in (
        source,
        private_payload,
        original,
        cleanup_interruption,
    ):
        assert all(
            not any(
                _contains_identity(value, private_value)
                for value in frame.values()
            )
            for frame in source_frames
        )


def test_first_instruction_system_exit_is_fresh_and_retains_no_private_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = object()
    private_payload = object()
    original = SystemExit(private_payload)
    target = api_services._find_slots
    source_lines, source_start = inspect.getsourcelines(target)
    target_line = source_start + next(
        index
        for index, source_line in enumerate(source_lines)
        if "try: control_kind = None; system_exit_code = None;" in source_line
    )
    interrupted = False

    def find_slots(**_kwargs):
        raise AssertionError("first-instruction interruption must skip evaluation")

    monkeypatch.setattr(api_services, "_find_slots_evaluated", find_slots)

    def interrupt(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is target.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise original
        return interrupt

    try:
        sys.settrace(interrupt)
        with pytest.raises(SystemExit) as captured:
            target(
                instructor_id="inst_calendar_source",
                requested_date=THURSDAY,
                client_address="Provider location",
                is_instructor_facility=True,
                duration_mins=60,
                calendar_busy_source=source,
            )
    finally:
        sys.settrace(None)

    assert interrupted is True
    assert captured.value is not original
    assert captured.value.code == 1
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True
    assert original not in _exception_chain(captured.value)
    source_frames = []
    current = captured.value.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith("/app/api_services.py"):
            source_frames.append(current.tb_frame.f_locals)
        current = current.tb_next
    assert source_frames
    for private_value in (source, private_payload, original):
        assert all(
            not any(
                _contains_identity(value, private_value)
                for value in frame.values()
            )
            for frame in source_frames
        )


def test_nested_source_cleanup_failure_detaches_active_system_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = object()
    private_payload = object()
    original = SystemExit(private_payload)
    cleanup_interruption = UnboundLocalError("nested-cleanup-private")
    target = api_services._find_slots_evaluated
    source_lines, source_start = inspect.getsourcelines(target)
    target_line = source_start + next(
        index
        for index, source_line in enumerate(source_lines)
        if "try: calendar_busy_source = None" in source_line
    )
    interrupted = False

    def fail(**_kwargs):
        raise original

    monkeypatch.setattr(
        api_services.tools,
        "find_available_swim_slots",
        fail,
    )

    def interrupt(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is target.__code__
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise cleanup_interruption
        return interrupt

    try:
        sys.settrace(interrupt)
        with pytest.raises(RuntimeError) as captured:
            api_services._find_slots(
                instructor_id="inst_calendar_source",
                requested_date=THURSDAY,
                client_address="Provider location",
                is_instructor_facility=True,
                duration_mins=60,
                calendar_busy_source=source,
            )
    finally:
        sys.settrace(None)

    assert interrupted is True
    assert captured.value is not cleanup_interruption
    assert str(captured.value) == (
        "Calendar availability control cleanup failed."
    )
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__ is True
    chain = _exception_chain(captured.value)
    assert original not in chain
    assert cleanup_interruption not in chain
    source_frames = []
    current = captured.value.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith("/app/api_services.py"):
            source_frames.append(current.tb_frame.f_locals)
        current = current.tb_next
    assert source_frames
    for private_value in (
        source,
        private_payload,
        original,
        cleanup_interruption,
    ):
        assert all(
            not any(
                _contains_identity(value, private_value)
                for value in frame.values()
            )
            for frame in source_frames
        )
