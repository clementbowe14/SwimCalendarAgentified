from __future__ import annotations

import asyncio
import datetime
import inspect
import json
import sys
import threading
import time
import traceback

import anyio
import pytest
from sqlalchemy import text

from app import (
    db,
    google_calendar_credential_handoff,
    google_calendar_freebusy,
    google_calendar_freebusy_deadline,
    google_calendar_freebusy_service,
    google_calendar_token_deadline,
    google_calendar_token_exchange,
)
from app.calendar_connections import replace_google_calendar_connection
from app.calendar_credentials import CalendarCredentialCipher
from app.external_calendar import (
    CalendarBusyInterval,
    CalendarBusySnapshot,
    ExternalCalendarAuthorizationRequired,
    ExternalCalendarInvalidResponse,
    ExternalCalendarTimeout,
    ExternalCalendarUnavailable,
)
from app.google_calendar_freebusy_service import (
    query_google_calendar_busy_snapshot,
)
from app.google_calendar_freebusy import (
    GoogleCalendarFreeBusyTransportResponse,
)
from app.google_calendar_oauth import (
    GOOGLE_CALENDAR_CALLBACK_PATH,
    GoogleCalendarOAuthConfiguration,
    GoogleCalendarTokenExchangeError,
)
from app.google_calendar_token_exchange import (
    GoogleCalendarTokenTransportResponse,
    GoogleCalendarTokenExchangeConfiguration,
)
from app.models import (
    InstructorDB,
    ProviderAccountDB,
    ProviderCalendarConnectionDB,
)


UTC = datetime.timezone.utc
WINDOW_START = datetime.datetime(2026, 11, 20, 14, 0, tzinfo=UTC)
WINDOW_END = WINDOW_START + datetime.timedelta(hours=4)
ACCOUNT_ID = "acct_freebusy_service_private"
INSTRUCTOR_ID = "inst_freebusy_service_private"
CONNECTION_ID = "calconn_" + "a" * 32
REVISION = 7
REFRESH_TOKEN = "freebusy-service-refresh-token-private"
ACCESS_TOKEN = "freebusy-service-access-token-private"
CLIENT_SECRET = "freebusy-service-client-secret-private"
PRIVATE_DETAIL = "freebusy-service-dependency-detail-private"
CIPHER = CalendarCredentialCipher(
    active_key_id="freebusy-service-key",
    keys={"freebusy-service-key": b"c" * 32},
)


class CredentialCipherSubclass(CalendarCredentialCipher):
    pass


class HostileTimezone(datetime.tzinfo):
    def utcoffset(self, _value):
        raise RuntimeError(PRIVATE_DETAIL)

    def dst(self, _value):
        return None


SUBCLASS_CIPHER = CredentialCipherSubclass(
    active_key_id="freebusy-service-subclass-key",
    keys={"freebusy-service-subclass-key": b"s" * 32},
)


def _configuration() -> GoogleCalendarTokenExchangeConfiguration:
    return GoogleCalendarTokenExchangeConfiguration(
        authorization_configuration=GoogleCalendarOAuthConfiguration(
            client_id="123456789-dayweave.apps.googleusercontent.com",
            redirect_uri=(
                "https://api.dayweave.example"
                + GOOGLE_CALENDAR_CALLBACK_PATH
            ),
        ),
        client_secret=CLIENT_SECRET,
    )


def _claim():
    return google_calendar_credential_handoff._new_claim(
        account_id=ACCOUNT_ID,
        instructor_id=INSTRUCTOR_ID,
        connection_id=CONNECTION_ID,
        expected_calendar_connection_revision=REVISION,
        refresh_token=REFRESH_TOKEN,
    )


def _access_result():
    return google_calendar_token_exchange._new_refresh_exchange_result(
        account_id=ACCOUNT_ID,
        instructor_id=INSTRUCTOR_ID,
        connection_id=CONNECTION_ID,
        expected_calendar_connection_revision=REVISION,
        access_token=ACCESS_TOKEN,
        expires_in_seconds=3600,
    )


def _authorization_required():
    return google_calendar_token_exchange._new_refresh_authorization_required(
        account_id=ACCOUNT_ID,
        instructor_id=INSTRUCTOR_ID,
        connection_id=CONNECTION_ID,
        expected_calendar_connection_revision=REVISION,
    )


def _freebusy_result(
    *,
    periods=(
        CalendarBusyInterval(
            start=WINDOW_START + datetime.timedelta(hours=1),
            end=WINDOW_START + datetime.timedelta(hours=2),
        ),
    ),
):
    return google_calendar_freebusy._new_query_result(
        account_id=ACCOUNT_ID,
        instructor_id=INSTRUCTOR_ID,
        connection_id=CONNECTION_ID,
        expected_calendar_connection_revision=REVISION,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        periods=periods,
    )


def _query(session_factory=lambda: None, **overrides):
    values = {
        "instructor_id": INSTRUCTOR_ID,
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "configuration": _configuration(),
        "credential_cipher": CIPHER,
    }
    values.update(overrides)
    return query_google_calendar_busy_snapshot(
        session_factory,
        **values,
    )


def _run(awaitable):
    return asyncio.run(awaitable)


def _rfc3339(value: datetime.datetime) -> str:
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.astimezone(UTC).isoformat(timespec=timespec).replace(
        "+00:00",
        "Z",
    )


def _install_success_dependencies(
    monkeypatch,
    events,
    *,
    claim=None,
    access_result=None,
    query_result=None,
):
    claim = claim or _claim()
    access_result = access_result or _access_result()
    query_result = query_result or _freebusy_result()
    event_loop_thread = threading.get_ident()
    revalidation_count = 0

    def claim_callback(
        session_factory,
        *,
        instructor_id,
        credential_cipher,
    ):
        events.append(("claim", threading.get_ident()))
        assert threading.get_ident() != event_loop_thread
        assert callable(session_factory)
        assert instructor_id == INSTRUCTOR_ID
        assert credential_cipher is CIPHER
        return claim

    async def exchange_callback(*, configuration, claim: object):
        events.append(("refresh", threading.get_ident()))
        assert threading.get_ident() == event_loop_thread
        assert type(configuration) is GoogleCalendarTokenExchangeConfiguration
        assert claim is claim_value
        return access_result

    def revalidate_callback(session_factory, *, claim: object):
        nonlocal revalidation_count
        revalidation_count += 1
        events.append((f"revalidate-{revalidation_count}", threading.get_ident()))
        assert threading.get_ident() != event_loop_thread
        assert callable(session_factory)
        assert claim is claim_value

    async def freebusy_callback(
        *,
        claim: object,
        access_result: object,
        window_start,
        window_end,
    ):
        events.append(("freebusy", threading.get_ident()))
        assert threading.get_ident() == event_loop_thread
        assert claim is claim_value
        assert access_result is access_result_value
        assert window_start == WINDOW_START
        assert window_end == WINDOW_END
        return query_result

    def forbidden_recovery(*_args, **_kwargs):
        raise AssertionError("success path reached recovery")

    claim_value = claim
    access_result_value = access_result
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        claim_callback,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "exchange_google_calendar_refresh_token_with_deadline",
        exchange_callback,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "revalidate_google_calendar_refresh_credential_claim",
        revalidate_callback,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "query_google_calendar_freebusy_with_deadline",
        freebusy_callback,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "mark_google_calendar_refresh_credential_authorization_required",
        forbidden_recovery,
    )
    return claim, access_result, query_result


def _install_window_dependencies(
    monkeypatch,
    *,
    expected_start,
    expected_end,
    periods=(),
):
    claim = _claim()
    access_result = _access_result()
    observed_windows = []

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        lambda *_args, **_kwargs: claim,
    )

    async def exchange_callback(**_kwargs):
        return access_result

    async def freebusy_callback(
        *,
        window_start,
        window_end,
        **_kwargs,
    ):
        observed_windows.append((window_start, window_end))
        assert window_start == expected_start
        assert window_end == expected_end
        return google_calendar_freebusy._new_query_result(
            account_id=ACCOUNT_ID,
            instructor_id=INSTRUCTOR_ID,
            connection_id=CONNECTION_ID,
            expected_calendar_connection_revision=REVISION,
            window_start=window_start,
            window_end=window_end,
            periods=periods,
        )

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "exchange_google_calendar_refresh_token_with_deadline",
        exchange_callback,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "revalidate_google_calendar_refresh_credential_claim",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "query_google_calendar_freebusy_with_deadline",
        freebusy_callback,
    )
    assert expected_end > expected_start
    return observed_windows


def _assert_failure(awaitable, error_type, *, reason=None):
    with pytest.raises(error_type) as captured:
        _run(awaitable)
    error = captured.value
    if reason is not None:
        assert error.reason_code == reason
    assert error.__cause__ is None
    assert error.__context__ is None
    return error


def test_public_surface_and_successful_ordering(monkeypatch):
    assert google_calendar_freebusy_service.__all__ == (
        "query_google_calendar_busy_snapshot",
    )
    assert inspect.iscoroutinefunction(query_google_calendar_busy_snapshot)
    signature = inspect.signature(query_google_calendar_busy_snapshot)
    assert list(signature.parameters) == [
        "session_factory",
        "instructor_id",
        "window_start",
        "window_end",
        "configuration",
        "credential_cipher",
    ]
    assert signature.parameters["session_factory"].kind is (
        inspect.Parameter.POSITIONAL_OR_KEYWORD
    )
    assert all(
        signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        for name in list(signature.parameters)[1:]
    )
    for prohibited in (
        "transport",
        "endpoint",
        "timeout",
        "deadline",
        "claim",
        "access_token",
        "session",
    ):
        assert prohibited not in signature.parameters

    events = []
    _claim_value, _access_value, query_result = (
        _install_success_dependencies(monkeypatch, events)
    )
    snapshot = _run(_query())

    assert snapshot == CalendarBusySnapshot(
        status="verified",
        periods=query_result.periods,
    )
    assert snapshot.periods is not query_result.periods
    assert snapshot.periods[0] is not query_result.periods[0]
    assert [name for name, _thread in events] == [
        "claim",
        "refresh",
        "revalidate-1",
        "freebusy",
        "revalidate-2",
    ]


def test_real_deadline_boundaries_compose_with_fake_http(monkeypatch):
    claim = _claim()
    events = []

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        lambda *_args, **_kwargs: claim,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "revalidate_google_calendar_refresh_credential_claim",
        lambda *_args, **_kwargs: None,
    )

    async def token_http(
        *,
        url,
        fields,
        timeout_seconds,
        deadline,
    ):
        del fields
        events.append("token-http")
        assert url == "https://oauth2.googleapis.com/token"
        assert timeout_seconds == 5.0
        assert deadline > asyncio.get_running_loop().time()
        return GoogleCalendarTokenTransportResponse(
            status_code=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "access_token": ACCESS_TOKEN,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
                separators=(",", ":"),
            ).encode("utf-8"),
        )

    async def freebusy_http(
        *,
        url,
        access_token,
        body,
        timeout_seconds,
        deadline,
    ):
        del body
        events.append("freebusy-http")
        assert url == "https://www.googleapis.com/calendar/v3/freeBusy"
        assert access_token == ACCESS_TOKEN
        assert timeout_seconds == 5.0
        assert deadline > asyncio.get_running_loop().time()
        return GoogleCalendarFreeBusyTransportResponse(
            status_code=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "kind": "calendar#freeBusy",
                    "timeMin": _rfc3339(WINDOW_START),
                    "timeMax": _rfc3339(WINDOW_END),
                    "calendars": {
                        "primary": {
                            "busy": [
                                {
                                    "start": _rfc3339(
                                        WINDOW_START
                                        + datetime.timedelta(hours=1)
                                    ),
                                    "end": _rfc3339(
                                        WINDOW_START
                                        + datetime.timedelta(hours=2)
                                    ),
                                }
                            ]
                        }
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8"),
        )

    monkeypatch.setattr(
        google_calendar_token_deadline,
        "_post_form_async",
        token_http,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_deadline,
        "_post_json_async",
        freebusy_http,
    )

    snapshot = _run(_query())

    assert snapshot == CalendarBusySnapshot(
        status="verified",
        periods=(
            CalendarBusyInterval(
                start=WINDOW_START + datetime.timedelta(hours=1),
                end=WINDOW_START + datetime.timedelta(hours=2),
            ),
        ),
    )
    assert events == ["token-http", "freebusy-http"]


@pytest.mark.parametrize(
    ("session_factory", "overrides", "reason"),
    [
        (None, {}, "provider_unavailable"),
        (lambda: None, {"configuration": object()}, "provider_unavailable"),
        (lambda: None, {"credential_cipher": object()}, "provider_unavailable"),
        (
            lambda: None,
            {"credential_cipher": SUBCLASS_CIPHER},
            "provider_unavailable",
        ),
        (lambda: None, {"instructor_id": ""}, "invalid_response"),
        (lambda: None, {"instructor_id": " bad"}, "invalid_response"),
        (
            lambda: None,
            {"window_start": WINDOW_START.replace(tzinfo=None)},
            "invalid_response",
        ),
        (
            lambda: None,
            {
                "window_start": datetime.datetime(
                    2026,
                    11,
                    20,
                    14,
                    0,
                    tzinfo=HostileTimezone(),
                )
            },
            "invalid_response",
        ),
        (
            lambda: None,
            {"window_end": WINDOW_START},
            "invalid_response",
        ),
        (
            lambda: None,
            {"window_end": WINDOW_START + datetime.timedelta(days=16)},
            "invalid_response",
        ),
    ],
)
def test_static_validation_precedes_side_effects(
    monkeypatch,
    session_factory,
    overrides,
    reason,
):
    calls = []

    def forbidden_claim(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("invalid input reached credential claim")

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        forbidden_claim,
    )
    _assert_failure(
        _query(session_factory, **overrides),
        ExternalCalendarUnavailable,
        reason=reason,
    )
    assert calls == []


def test_exact_fifteen_day_window_is_accepted(monkeypatch):
    window_end = WINDOW_START + datetime.timedelta(days=15)
    observed = _install_window_dependencies(
        monkeypatch,
        expected_start=WINDOW_START,
        expected_end=window_end,
    )
    snapshot = _run(_query(window_end=window_end))
    assert snapshot == CalendarBusySnapshot(status="verified", periods=())
    assert observed == [(WINDOW_START, window_end)]


def test_fifteen_days_plus_one_microsecond_is_rejected(monkeypatch):
    claim_calls = []

    def forbidden_claim(*_args, **_kwargs):
        claim_calls.append(True)
        raise AssertionError("oversized window reached persistence")

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        forbidden_claim,
    )
    _assert_failure(
        _query(
            window_end=(
                WINDOW_START
                + datetime.timedelta(days=15, microseconds=1)
            )
        ),
        ExternalCalendarInvalidResponse,
        reason="invalid_response",
    )
    assert claim_calls == []


def test_non_utc_window_is_normalized_once(monkeypatch):
    eastern_standard = datetime.timezone(datetime.timedelta(hours=-5))
    local_start = WINDOW_START.astimezone(eastern_standard)
    local_end = WINDOW_END.astimezone(eastern_standard)
    observed = _install_window_dependencies(
        monkeypatch,
        expected_start=WINDOW_START,
        expected_end=WINDOW_END,
    )
    snapshot = _run(
        _query(window_start=local_start, window_end=local_end)
    )
    assert snapshot.status == "verified"
    assert observed == [(WINDOW_START, WINDOW_END)]


@pytest.mark.parametrize(
    ("period_start", "period_end", "expected_hours"),
    [
        (
            datetime.datetime(2026, 3, 8, 5, 0, tzinfo=UTC),
            datetime.datetime(2026, 3, 9, 4, 0, tzinfo=UTC),
            23,
        ),
        (
            datetime.datetime(2026, 11, 1, 4, 0, tzinfo=UTC),
            datetime.datetime(2026, 11, 2, 5, 0, tzinfo=UTC),
            25,
        ),
    ],
)
def test_dst_all_day_period_duration_is_preserved(
    monkeypatch,
    period_start,
    period_end,
    expected_hours,
):
    period = CalendarBusyInterval(start=period_start, end=period_end)
    observed = _install_window_dependencies(
        monkeypatch,
        expected_start=period_start,
        expected_end=period_end,
        periods=(period,),
    )
    snapshot = _run(
        _query(window_start=period_start, window_end=period_end)
    )
    assert snapshot.periods == (period,)
    assert (
        snapshot.periods[0].end - snapshot.periods[0].start
    ) == datetime.timedelta(hours=expected_hours)
    assert observed == [(period_start, period_end)]


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (
            "disconnected",
            CalendarBusySnapshot(status="disconnected", periods=()),
        ),
        ("authorization_required", None),
    ],
)
def test_connection_state_short_circuits_network(
    monkeypatch,
    state,
    expected,
):
    events = []

    def claim_callback(*_args, **_kwargs):
        events.append("claim")
        return state

    async def forbidden_network(**_kwargs):
        events.append("network")
        raise AssertionError("state short circuit reached network")

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        claim_callback,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "exchange_google_calendar_refresh_token_with_deadline",
        forbidden_network,
    )
    if expected is not None:
        assert _run(_query()) == expected
    else:
        _assert_failure(
            _query(),
            ExternalCalendarAuthorizationRequired,
            reason="authorization_required",
        )
    assert events == ["claim"]


def test_invalid_grant_recovers_before_authorization_error(monkeypatch):
    claim = _claim()
    outcome = _authorization_required()
    events = []
    event_loop_thread = threading.get_ident()

    def claim_callback(*_args, **_kwargs):
        events.append("claim")
        return claim

    async def exchange_callback(**_kwargs):
        events.append("refresh")
        return outcome

    def recovery_callback(session_factory, *, claim: object, outcome: object):
        events.append("recovery")
        assert threading.get_ident() != event_loop_thread
        assert callable(session_factory)
        assert claim is claim_value
        assert outcome is outcome_value

    async def forbidden_freebusy(**_kwargs):
        raise AssertionError("invalid grant reached FreeBusy")

    claim_value = claim
    outcome_value = outcome
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        claim_callback,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "exchange_google_calendar_refresh_token_with_deadline",
        exchange_callback,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "mark_google_calendar_refresh_credential_authorization_required",
        recovery_callback,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "query_google_calendar_freebusy_with_deadline",
        forbidden_freebusy,
    )

    _assert_failure(
        _query(),
        ExternalCalendarAuthorizationRequired,
        reason="authorization_required",
    )
    assert events == ["claim", "refresh", "recovery"]


def test_failed_recovery_never_claims_authorization_required(monkeypatch):
    claim = _claim()

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        lambda *_args, **_kwargs: claim,
    )

    async def exchange_callback(**_kwargs):
        return _authorization_required()

    def recovery_callback(*_args, **_kwargs):
        raise RuntimeError(PRIVATE_DETAIL)

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "exchange_google_calendar_refresh_token_with_deadline",
        exchange_callback,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "mark_google_calendar_refresh_credential_authorization_required",
        recovery_callback,
    )
    error = _assert_failure(
        _query(),
        ExternalCalendarUnavailable,
        reason="provider_unavailable",
    )
    assert PRIVATE_DETAIL not in str(error)


def test_cross_fenced_invalid_grant_is_rejected_before_recovery(monkeypatch):
    claim = _claim()
    crossed = google_calendar_token_exchange._new_refresh_authorization_required(
        account_id=ACCOUNT_ID,
        instructor_id=INSTRUCTOR_ID,
        connection_id="calconn_" + "b" * 32,
        expected_calendar_connection_revision=REVISION + 1,
    )
    recovery_calls = []

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        lambda *_args, **_kwargs: claim,
    )

    async def exchange_callback(**_kwargs):
        return crossed

    def forbidden_recovery(*_args, **_kwargs):
        recovery_calls.append(True)
        raise AssertionError("crossed recovery outcome reached persistence")

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "exchange_google_calendar_refresh_token_with_deadline",
        exchange_callback,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "mark_google_calendar_refresh_credential_authorization_required",
        forbidden_recovery,
    )

    _assert_failure(
        _query(),
        ExternalCalendarInvalidResponse,
        reason="invalid_response",
    )
    assert recovery_calls == []


@pytest.mark.parametrize(
    "stage",
    ["claim", "refresh", "preflight", "recovery", "final"],
)
def test_non_freebusy_domain_errors_cannot_masquerade(monkeypatch, stage):
    events = []
    _install_success_dependencies(monkeypatch, events)
    raised = ExternalCalendarAuthorizationRequired()

    if stage == "claim":

        def failing_claim(*_args, **_kwargs):
            raise raised

        monkeypatch.setattr(
            google_calendar_freebusy_service,
            "claim_google_calendar_refresh_credential",
            failing_claim,
        )
    elif stage == "refresh":

        async def failing_refresh(**_kwargs):
            raise raised

        monkeypatch.setattr(
            google_calendar_freebusy_service,
            "exchange_google_calendar_refresh_token_with_deadline",
            failing_refresh,
        )
    elif stage in ("preflight", "final"):
        call_count = 0

        def failing_revalidation(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if stage == "preflight" or call_count == 2:
                raise raised

        monkeypatch.setattr(
            google_calendar_freebusy_service,
            "revalidate_google_calendar_refresh_credential_claim",
            failing_revalidation,
        )
    else:
        outcome = _authorization_required()

        async def invalid_grant(**_kwargs):
            return outcome

        def failing_recovery(*_args, **_kwargs):
            raise raised

        monkeypatch.setattr(
            google_calendar_freebusy_service,
            "exchange_google_calendar_refresh_token_with_deadline",
            invalid_grant,
        )
        monkeypatch.setattr(
            google_calendar_freebusy_service,
            "mark_google_calendar_refresh_credential_authorization_required",
            failing_recovery,
        )

    _assert_failure(
        _query(),
        ExternalCalendarUnavailable,
        reason="provider_unavailable",
    )


@pytest.mark.parametrize(
    ("raised", "error_type", "reason"),
    [
        (
            ExternalCalendarAuthorizationRequired(),
            ExternalCalendarAuthorizationRequired,
            "authorization_required",
        ),
        (ExternalCalendarTimeout(), ExternalCalendarTimeout, "timeout"),
        (
            ExternalCalendarInvalidResponse(),
            ExternalCalendarInvalidResponse,
            "invalid_response",
        ),
        (
            ExternalCalendarUnavailable("provider_unavailable"),
            ExternalCalendarUnavailable,
            "provider_unavailable",
        ),
    ],
)
def test_freebusy_failure_reason_is_preserved_without_recovery(
    monkeypatch,
    raised,
    error_type,
    reason,
):
    events = []
    _install_success_dependencies(monkeypatch, events)

    async def failing_freebusy(**_kwargs):
        events.append(("failing-freebusy", threading.get_ident()))
        raise raised

    def forbidden_recovery(*_args, **_kwargs):
        raise AssertionError("FreeBusy failure persisted recovery")

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "query_google_calendar_freebusy_with_deadline",
        failing_freebusy,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "mark_google_calendar_refresh_credential_authorization_required",
        forbidden_recovery,
    )
    _assert_failure(_query(), error_type, reason=reason)
    assert [name for name, _thread in events] == [
        "claim",
        "refresh",
        "revalidate-1",
        "failing-freebusy",
    ]


def test_refresh_failure_maps_to_provider_unavailable(monkeypatch):
    events = []
    _install_success_dependencies(monkeypatch, events)

    async def failing_refresh(**_kwargs):
        raise GoogleCalendarTokenExchangeError()

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "exchange_google_calendar_refresh_token_with_deadline",
        failing_refresh,
    )
    _assert_failure(
        _query(),
        ExternalCalendarUnavailable,
        reason="provider_unavailable",
    )
    assert [name for name, _thread in events] == ["claim"]


@pytest.mark.parametrize(
    ("stage", "expected_events"),
    [
        ("claim", ["claim"]),
        ("refresh", ["claim", "refresh"]),
        ("preflight", ["claim", "refresh", "revalidate-1"]),
        (
            "freebusy",
            ["claim", "refresh", "revalidate-1", "freebusy"],
        ),
        (
            "final",
            [
                "claim",
                "refresh",
                "revalidate-1",
                "freebusy",
                "revalidate-2",
            ],
        ),
    ],
)
def test_malformed_collaborator_result_fails_closed(
    monkeypatch,
    stage,
    expected_events,
):
    events = []
    _install_success_dependencies(monkeypatch, events)
    if stage == "claim":

        def malformed_claim(*_args, **_kwargs):
            events.append(("claim", threading.get_ident()))
            return object()

        monkeypatch.setattr(
            google_calendar_freebusy_service,
            "claim_google_calendar_refresh_credential",
            malformed_claim,
        )
    elif stage == "refresh":

        async def malformed_refresh(**_kwargs):
            events.append(("refresh", threading.get_ident()))
            return object()

        monkeypatch.setattr(
            google_calendar_freebusy_service,
            "exchange_google_calendar_refresh_token_with_deadline",
            malformed_refresh,
        )
    elif stage in ("preflight", "final"):
        call_count = 0

        def malformed_revalidation(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            events.append(
                (f"revalidate-{call_count}", threading.get_ident())
            )
            if stage == "preflight" or call_count == 2:
                return object()
            return None

        monkeypatch.setattr(
            google_calendar_freebusy_service,
            "revalidate_google_calendar_refresh_credential_claim",
            malformed_revalidation,
        )
    else:

        async def malformed_freebusy(**_kwargs):
            events.append(("freebusy", threading.get_ident()))
            return object()

        monkeypatch.setattr(
            google_calendar_freebusy_service,
            "query_google_calendar_freebusy_with_deadline",
            malformed_freebusy,
        )

    _assert_failure(
        _query(),
        ExternalCalendarInvalidResponse,
        reason="invalid_response",
    )
    assert [name for name, _thread in events] == expected_events


@pytest.mark.parametrize("stage", ["claim", "access-result"])
def test_tampered_exact_secret_value_is_invalid_response(monkeypatch, stage):
    claim = _claim()
    access_result = _access_result()
    if stage == "claim":
        object.__setattr__(claim, "instructor_id", "inst_tampered")
    else:
        object.__setattr__(access_result, "account_id", "acct_tampered")
    events = []
    _install_success_dependencies(
        monkeypatch,
        events,
        claim=claim,
        access_result=access_result,
    )
    _assert_failure(
        _query(),
        ExternalCalendarInvalidResponse,
        reason="invalid_response",
    )
    assert [name for name, _thread in events] == (
        ["claim"] if stage == "claim" else ["claim", "refresh"]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_id", "acct_crossed"),
        ("instructor_id", "inst_crossed"),
        ("connection_id", "calconn_" + "b" * 32),
        ("expected_calendar_connection_revision", REVISION + 1),
        ("window_start", WINDOW_START + datetime.timedelta(minutes=1)),
        ("window_end", WINDOW_END - datetime.timedelta(minutes=1)),
        ("periods", []),
    ],
)
def test_crossed_or_malformed_freebusy_result_is_rejected(
    monkeypatch,
    field,
    value,
):
    result = _freebusy_result()
    object.__setattr__(result, field, value)
    events = []
    _install_success_dependencies(
        monkeypatch,
        events,
        query_result=result,
    )
    _assert_failure(
        _query(),
        ExternalCalendarInvalidResponse,
        reason="invalid_response",
    )
    assert [name for name, _thread in events] == [
        "claim",
        "refresh",
        "revalidate-1",
        "freebusy",
    ]


def test_noncanonical_periods_are_rejected(monkeypatch):
    result = _freebusy_result(
        periods=(
            CalendarBusyInterval(
                start=WINDOW_START + datetime.timedelta(hours=1),
                end=WINDOW_START + datetime.timedelta(hours=3),
            ),
            CalendarBusyInterval(
                start=WINDOW_START + datetime.timedelta(hours=2),
                end=WINDOW_START + datetime.timedelta(hours=4),
            ),
        )
    )
    events = []
    _install_success_dependencies(monkeypatch, events, query_result=result)
    _assert_failure(
        _query(),
        ExternalCalendarInvalidResponse,
        reason="invalid_response",
    )
    assert [name for name, _thread in events][-1] == "freebusy"


def test_concurrent_tenants_keep_claims_windows_and_outcomes_isolated(
    monkeypatch,
):
    values = {}
    for suffix, offset in (("alpha", 0), ("beta", 6)):
        instructor_id = f"inst_concurrent_{suffix}"
        account_id = f"acct_concurrent_{suffix}"
        connection_id = "calconn_" + ("a" if suffix == "alpha" else "b") * 32
        window_start = WINDOW_START + datetime.timedelta(hours=offset)
        window_end = window_start + datetime.timedelta(hours=2)
        claim = google_calendar_credential_handoff._new_claim(
            account_id=account_id,
            instructor_id=instructor_id,
            connection_id=connection_id,
            expected_calendar_connection_revision=REVISION,
            refresh_token=f"concurrent-{suffix}-refresh-private",
        )
        access_result = (
            google_calendar_token_exchange._new_refresh_exchange_result(
                account_id=account_id,
                instructor_id=instructor_id,
                connection_id=connection_id,
                expected_calendar_connection_revision=REVISION,
                access_token=f"concurrent-{suffix}-access-private",
                expires_in_seconds=3600,
            )
        )
        values[instructor_id] = {
            "claim": claim,
            "access": access_result,
            "window_start": window_start,
            "window_end": window_end,
        }

    def claim_callback(_session_factory, *, instructor_id, **_kwargs):
        return values[instructor_id]["claim"]

    def revalidate_callback(_session_factory, *, claim):
        trusted = (
            google_calendar_credential_handoff
            .validate_google_calendar_refresh_credential_claim(claim)
        )
        assert values[trusted.instructor_id]["claim"] is claim

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        claim_callback,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "revalidate_google_calendar_refresh_credential_claim",
        revalidate_callback,
    )

    async def scenario():
        refresh_seen = {
            instructor_id: asyncio.Event() for instructor_id in values
        }

        async def exchange_callback(*, claim, **_kwargs):
            trusted = (
                google_calendar_credential_handoff
                .validate_google_calendar_refresh_credential_claim(claim)
            )
            own = refresh_seen[trusted.instructor_id]
            other = next(
                event
                for instructor_id, event in refresh_seen.items()
                if instructor_id != trusted.instructor_id
            )
            own.set()
            await other.wait()
            return values[trusted.instructor_id]["access"]

        async def freebusy_callback(
            *,
            claim,
            access_result,
            window_start,
            window_end,
        ):
            trusted = (
                google_calendar_credential_handoff
                .validate_google_calendar_refresh_credential_claim(claim)
            )
            own = values[trusted.instructor_id]
            assert access_result is own["access"]
            assert window_start == own["window_start"]
            assert window_end == own["window_end"]
            if trusted.instructor_id.endswith("beta"):
                raise ExternalCalendarTimeout()
            return google_calendar_freebusy._new_query_result(
                account_id=trusted.account_id,
                instructor_id=trusted.instructor_id,
                connection_id=trusted.connection_id,
                expected_calendar_connection_revision=(
                    trusted.expected_calendar_connection_revision
                ),
                window_start=window_start,
                window_end=window_end,
                periods=(
                    CalendarBusyInterval(
                        start=window_start,
                        end=window_start + datetime.timedelta(minutes=30),
                    ),
                ),
            )

        monkeypatch.setattr(
            google_calendar_freebusy_service,
            "exchange_google_calendar_refresh_token_with_deadline",
            exchange_callback,
        )
        monkeypatch.setattr(
            google_calendar_freebusy_service,
            "query_google_calendar_freebusy_with_deadline",
            freebusy_callback,
        )

        tasks = []
        for instructor_id, own in values.items():
            tasks.append(
                asyncio.create_task(
                    _query(
                        instructor_id=instructor_id,
                        window_start=own["window_start"],
                        window_end=own["window_end"],
                    )
                )
            )
        return await asyncio.gather(*tasks, return_exceptions=True)

    alpha_result, beta_result = _run(scenario())
    assert alpha_result == CalendarBusySnapshot(
        status="verified",
        periods=(
            CalendarBusyInterval(
                start=values["inst_concurrent_alpha"]["window_start"],
                end=(
                    values["inst_concurrent_alpha"]["window_start"]
                    + datetime.timedelta(minutes=30)
                ),
            ),
        ),
    )
    assert type(beta_result) is ExternalCalendarTimeout
    assert beta_result.reason_code == "timeout"


def test_cancellation_waits_for_claim_worker_and_skips_network(monkeypatch):
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()
    network_calls = []

    def held_claim(*_args, **_kwargs):
        worker_started.set()
        assert release_worker.wait(timeout=5)
        worker_finished.set()
        raise RuntimeError(PRIVATE_DETAIL)

    async def forbidden_refresh(**_kwargs):
        network_calls.append(True)
        raise AssertionError("cancelled claim reached network")

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        held_claim,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "exchange_google_calendar_refresh_token_with_deadline",
        forbidden_refresh,
    )

    async def scenario():
        task = asyncio.create_task(_query())
        assert await asyncio.to_thread(worker_started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release_worker.set()
        with pytest.raises(asyncio.CancelledError) as captured:
            await task
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None

    _run(scenario())
    assert worker_finished.is_set()
    assert network_calls == []


def test_task_group_abort_never_invokes_joined_worker_cancel(monkeypatch):
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()
    cancel_calls = []

    def held_claim(*_args, **_kwargs):
        worker_started.set()
        assert release_worker.wait(timeout=5)
        worker_finished.set()
        return "disconnected"

    def forbidden_worker_cancel(_worker, msg=None):
        cancel_calls.append(msg)
        raise AssertionError("task group cancelled joined worker")

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        held_claim,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service._JoinedWorkerTask,
        "cancel",
        forbidden_worker_cancel,
    )

    async def scenario():
        task = asyncio.create_task(_query())
        assert await asyncio.to_thread(worker_started.wait, 5)
        task.cancel("parent-cancel")
        await asyncio.sleep(0)
        assert not task.done()
        release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(scenario())
    assert worker_finished.is_set()
    assert cancel_calls == []


@pytest.mark.parametrize(
    ("raised", "error_type", "expected_code"),
    [
        (KeyboardInterrupt(), KeyboardInterrupt, None),
        (SystemExit(23), SystemExit, 23),
        (GeneratorExit(), GeneratorExit, None),
        (asyncio.CancelledError(), asyncio.CancelledError, None),
    ],
)
def test_worker_controls_are_reemitted_only_by_parent(
    monkeypatch,
    raised,
    error_type,
    expected_code,
):
    def controlled_claim(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        controlled_claim,
    )

    with pytest.raises(error_type) as captured:
        _run(_query())
    if error_type is SystemExit:
        assert captured.value.code == expected_code
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    "interruption_site",
    ["classifier", "success-envelope", "failure-envelope"],
)
def test_worker_boundary_contains_internal_control_interruptions(
    monkeypatch,
    interruption_site,
):
    if interruption_site in ("classifier", "failure-envelope"):

        def failing_claim(*_args, **_kwargs):
            raise RuntimeError(PRIVATE_DETAIL)

        monkeypatch.setattr(
            google_calendar_freebusy_service,
            "claim_google_calendar_refresh_credential",
            failing_claim,
        )
    else:
        monkeypatch.setattr(
            google_calendar_freebusy_service,
            "claim_google_calendar_refresh_credential",
            lambda *_args, **_kwargs: _claim(),
        )

    if interruption_site == "classifier":
        real_classifier = (
            google_calendar_freebusy_service
            ._safe_classified_service_failure
        )
        classifier_calls = 0

        def interrupted_classifier(raised):
            nonlocal classifier_calls
            classifier_calls += 1
            if classifier_calls == 1:
                raise KeyboardInterrupt(PRIVATE_DETAIL)
            return real_classifier(raised)

        monkeypatch.setattr(
            google_calendar_freebusy_service,
            "_safe_classified_service_failure",
            interrupted_classifier,
        )
    else:

        def interrupted_allocator(**_kwargs):
            raise KeyboardInterrupt(PRIVATE_DETAIL)

        monkeypatch.setattr(
            google_calendar_freebusy_service,
            "_new_owned_worker_outcome",
            interrupted_allocator,
        )

    with pytest.raises(KeyboardInterrupt) as captured:
        _run(_query())
    assert str(captured.value) == ""
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_worker_control_survives_ordinary_envelope_failure(monkeypatch):
    def controlled_claim(*_args, **_kwargs):
        raise KeyboardInterrupt(PRIVATE_DETAIL)

    def failing_allocator(**_kwargs):
        raise RuntimeError("secondary-envelope-detail-private")

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        controlled_claim,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "_new_owned_worker_outcome",
        failing_allocator,
    )

    with pytest.raises(KeyboardInterrupt) as captured:
        _run(_query())
    assert str(captured.value) == ""
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    ("original", "emergency", "error_type", "expected_code"),
    [
        (
            BaseExceptionGroup(
                "original-group-private",
                [
                    asyncio.CancelledError("cancel-private"),
                    KeyboardInterrupt(PRIVATE_DETAIL),
                ],
            ),
            RuntimeError("emergency-outcome-detail-private"),
            KeyboardInterrupt,
            None,
        ),
        (
            RuntimeError(PRIVATE_DETAIL),
            BaseExceptionGroup(
                "emergency-group-private",
                [
                    asyncio.CancelledError("cancel-private"),
                    SystemExit(23),
                ],
            ),
            SystemExit,
            23,
        ),
    ],
)
def test_outer_worker_fallback_preserves_grouped_control(
    monkeypatch,
    original,
    emergency,
    error_type,
    expected_code,
):
    async def interrupted_worker_inner(*_args, **_kwargs):
        raise original

    def failing_emergency_outcome(_raised):
        raise emergency

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "_run_worker_inner",
        interrupted_worker_inner,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "_emergency_worker_outcome",
        failing_emergency_outcome,
    )

    with pytest.raises(error_type) as captured:
        _run(_query())
    if error_type is SystemExit:
        assert captured.value.code == expected_code
    else:
        assert str(captured.value) == ""
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_outer_worker_fallback_group_overflow_fails_closed(monkeypatch):
    async def interrupted_worker_inner(*_args, **_kwargs):
        raise BaseExceptionGroup(
            "overflow-group-private",
            [RuntimeError(PRIVATE_DETAIL)],
        )

    def failing_emergency_outcome(_raised):
        raise RuntimeError("emergency-outcome-detail-private")

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "_MAX_CONTROL_EXCEPTION_NODES",
        1,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "_run_worker_inner",
        interrupted_worker_inner,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "_emergency_worker_outcome",
        failing_emergency_outcome,
    )

    with pytest.raises(asyncio.CancelledError) as captured:
        _run(_query())
    assert str(captured.value) == ""
    assert captured.value.args == ()
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_bounded_control_checks_all_roots_before_nested_overflow(monkeypatch):
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "_MAX_CONTROL_EXCEPTION_NODES",
        1,
    )
    outcome = google_calendar_freebusy_service._bounded_control_exception(
        BaseExceptionGroup(
            "overflow-group-private",
            [RuntimeError(PRIVATE_DETAIL)],
        ),
        KeyboardInterrupt(PRIVATE_DETAIL),
    )
    assert outcome == ("keyboard_interrupt", None)


def test_interruption_after_start_gate_commit_joins_worker(monkeypatch):
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()

    def controlled_claim(*_args, **_kwargs):
        worker_started.set()
        assert release_worker.wait(timeout=5)
        worker_finished.set()
        return _claim()

    source_lines, first_line = inspect.getsourcelines(
        google_calendar_freebusy_service._run_joined
    )
    shield_line = first_line + next(
        index
        for index, line in enumerate(source_lines)
        if "outcome = await asyncio.shield(guardian)" in line
    )
    interrupted = False

    def interrupt_on_shield_line(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code
            is google_calendar_freebusy_service._run_joined.__code__
            and frame.f_lineno == shield_line
        ):
            interrupted = True
            sys.settrace(None)
            raise KeyboardInterrupt(PRIVATE_DETAIL)
        return interrupt_on_shield_line

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        controlled_claim,
    )

    async def scenario():
        async def release_after_start():
            assert await asyncio.to_thread(worker_started.wait, 5)
            release_worker.set()

        controller = asyncio.create_task(release_after_start())
        sys.settrace(interrupt_on_shield_line)
        try:
            with pytest.raises(KeyboardInterrupt) as captured:
                await _query()
            assert str(captured.value) == ""
            assert captured.value.__cause__ is None
            assert captured.value.__context__ is None
        finally:
            sys.settrace(None)
            release_worker.set()
            await controller

    _run(scenario())
    assert interrupted
    assert worker_started.is_set()
    assert worker_finished.is_set()


def test_committed_gate_interruption_cannot_bypass_worker_cleanup(
    monkeypatch,
):
    real_event = asyncio.Event
    parent_task = None
    operation_calls = []
    parent_gate_inspections = []

    class InterruptingGate(real_event):
        def set(self):
            super().set()
            raise KeyboardInterrupt(PRIVATE_DETAIL)

        def is_set(self):
            if asyncio.current_task() is parent_task:
                parent_gate_inspections.append(True)
                raise SystemExit(23)
            return super().is_set()

    def event_factory():
        return InterruptingGate()

    def forbidden_claim(*_args, **_kwargs):
        operation_calls.append(True)
        raise AssertionError("cancelled startup worker reached database")

    monkeypatch.setattr(
        google_calendar_freebusy_service.asyncio,
        "Event",
        event_factory,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        forbidden_claim,
    )

    async def scenario():
        nonlocal parent_task
        parent_task = asyncio.current_task()
        with pytest.raises(KeyboardInterrupt) as captured:
            await _query()
        assert str(captured.value) == ""
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    _run(scenario())
    assert parent_gate_inspections == []
    assert operation_calls == []


def test_repeated_control_before_custom_join_cannot_abandon_worker(
    monkeypatch,
):
    real_event = asyncio.Event
    operation_started = threading.Event()
    release_operation = threading.Event()
    operation_finished = threading.Event()

    class CommitThenInterruptGate(real_event):
        def set(self):
            super().set()
            raise KeyboardInterrupt(PRIVATE_DETAIL)

    def controlled_claim(*_args, **_kwargs):
        operation_started.set()
        assert release_operation.wait(timeout=5)
        operation_finished.set()
        return _claim()

    source_lines, first_line = inspect.getsourcelines(
        google_calendar_freebusy_service._run_joined
    )
    cleanup_line = first_line + next(
        index
        for index, line in enumerate(source_lines)
        if "return await _finish_precommit_owned_worker(" in line
    )
    interrupted = False

    def interrupt_before_custom_join(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code
            is google_calendar_freebusy_service._run_joined.__code__
            and frame.f_lineno == cleanup_line
        ):
            interrupted = True
            sys.settrace(None)
            raise SystemExit(23)
        return interrupt_before_custom_join

    monkeypatch.setattr(
        google_calendar_freebusy_service.asyncio,
        "Event",
        CommitThenInterruptGate,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        controlled_claim,
    )

    async def scenario():
        release_timer = threading.Timer(0.05, release_operation.set)
        release_timer.start()
        sys.settrace(interrupt_before_custom_join)
        try:
            with pytest.raises(SystemExit) as captured:
                await _query()
            assert captured.value.code == 23
            assert captured.value.__cause__ is None
            assert captured.value.__context__ is None
            assert not operation_started.is_set() or operation_finished.is_set()
        finally:
            sys.settrace(None)
            release_operation.set()
            release_timer.join(timeout=5)

    _run(scenario())
    assert interrupted
    assert not operation_started.is_set() or operation_finished.is_set()


def test_structured_fallback_preserves_settled_worker_control(monkeypatch):
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()

    def controlled_claim(*_args, **_kwargs):
        worker_started.set()
        assert release_worker.wait(timeout=5)
        worker_finished.set()
        raise KeyboardInterrupt(PRIVATE_DETAIL)

    source_lines, first_line = inspect.getsourcelines(
        google_calendar_freebusy_service._run_joined
    )
    cleanup_lines = [
        index
        for index, line in enumerate(source_lines)
        if "return await _finish_interrupted_join(" in line
    ]
    cleanup_line = first_line + cleanup_lines[1]
    interrupted = False

    def interrupt_custom_join(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code
            is google_calendar_freebusy_service._run_joined.__code__
            and frame.f_lineno == cleanup_line
        ):
            interrupted = True
            sys.settrace(None)
            raise RuntimeError("custom-join-detail-private")
        return interrupt_custom_join

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        controlled_claim,
    )

    async def scenario():
        current = asyncio.current_task()

        async def cancel_and_release():
            assert await asyncio.to_thread(worker_started.wait, 5)
            current.cancel()
            release_worker.set()

        controller = asyncio.create_task(cancel_and_release())
        sys.settrace(interrupt_custom_join)
        try:
            with pytest.raises(KeyboardInterrupt) as captured:
                await _query()
            assert str(captured.value) == ""
            assert captured.value.__cause__ is None
            assert captured.value.__context__ is None
        finally:
            sys.settrace(None)
            release_worker.set()
            await controller

    _run(scenario())
    assert interrupted
    assert worker_finished.is_set()


def test_guardian_control_is_not_swallowed(monkeypatch):
    network_calls = []

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        lambda *_args, **_kwargs: _claim(),
    )

    async def forbidden_refresh(**_kwargs):
        network_calls.append(True)
        raise AssertionError("guardian control reached network")

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "exchange_google_calendar_refresh_token_with_deadline",
        forbidden_refresh,
    )

    source_lines, first_line = inspect.getsourcelines(
        google_calendar_freebusy_service._guard_owned_worker_inner
    )
    shield_line = first_line + next(
        index
        for index, line in enumerate(source_lines)
        if "outcome = await asyncio.shield(worker)" in line
    )
    interrupted = False

    def interrupt_guardian(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code
            is google_calendar_freebusy_service
            ._guard_owned_worker_inner.__code__
            and frame.f_lineno == shield_line
        ):
            interrupted = True
            sys.settrace(None)
            raise KeyboardInterrupt(PRIVATE_DETAIL)
        return interrupt_guardian

    async def scenario():
        sys.settrace(interrupt_guardian)
        try:
            with pytest.raises(KeyboardInterrupt) as captured:
                await _query()
            assert str(captured.value) == ""
            assert captured.value.__cause__ is None
            assert captured.value.__context__ is None
        finally:
            sys.settrace(None)

    _run(scenario())
    assert interrupted
    assert network_calls == []


def test_guardian_precommit_failure_does_not_become_cancellation(monkeypatch):
    operation_calls = []

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        lambda *_args, **_kwargs: operation_calls.append(True),
    )

    source_lines, first_line = inspect.getsourcelines(
        google_calendar_freebusy_service._guard_owned_worker_inner
    )
    shield_line = first_line + next(
        index
        for index, line in enumerate(source_lines)
        if "outcome = await asyncio.shield(worker)" in line
    )
    interrupted = False

    def interrupt_guardian(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code
            is google_calendar_freebusy_service
            ._guard_owned_worker_inner.__code__
            and frame.f_lineno == shield_line
        ):
            interrupted = True
            sys.settrace(None)
            raise RuntimeError(PRIVATE_DETAIL)
        return interrupt_guardian

    async def scenario():
        sys.settrace(interrupt_guardian)
        try:
            with pytest.raises(ExternalCalendarUnavailable) as captured:
                await _query()
            assert captured.value.reason_code == "provider_unavailable"
        finally:
            sys.settrace(None)

    _run(scenario())
    assert interrupted
    assert operation_calls == []


def test_guardian_secondary_merge_control_is_redacted(monkeypatch):
    network_calls = []

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        lambda *_args, **_kwargs: _claim(),
    )

    async def forbidden_refresh(**_kwargs):
        network_calls.append(True)
        raise AssertionError("guardian merge control reached network")

    def interrupted_merge(*_args, **_kwargs):
        raise KeyboardInterrupt("guardian-merge-private")

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "exchange_google_calendar_refresh_token_with_deadline",
        forbidden_refresh,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "_merged_guardian_outcome",
        interrupted_merge,
    )

    source_lines, first_line = inspect.getsourcelines(
        google_calendar_freebusy_service._guard_owned_worker_inner
    )
    shield_line = first_line + next(
        index
        for index, line in enumerate(source_lines)
        if "outcome = await asyncio.shield(worker)" in line
    )
    interrupted = False

    def interrupt_guardian(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code
            is google_calendar_freebusy_service
            ._guard_owned_worker_inner.__code__
            and frame.f_lineno == shield_line
        ):
            interrupted = True
            sys.settrace(None)
            raise RuntimeError("guardian-initial-private")
        return interrupt_guardian

    async def scenario():
        sys.settrace(interrupt_guardian)
        try:
            with pytest.raises(KeyboardInterrupt) as captured:
                await _query()
            assert str(captured.value) == ""
            assert captured.value.__cause__ is None
            assert captured.value.__context__ is None
        finally:
            sys.settrace(None)

    _run(scenario())
    assert interrupted
    assert network_calls == []


def test_parent_cancellation_cannot_mask_guardian_control(monkeypatch):
    worker_started = threading.Event()
    release_worker = threading.Event()
    network_calls = []

    def controlled_claim(*_args, **_kwargs):
        worker_started.set()
        assert release_worker.wait(timeout=5)
        return _claim()

    async def forbidden_refresh(**_kwargs):
        network_calls.append(True)
        raise AssertionError("masked guardian control reached network")

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        controlled_claim,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "exchange_google_calendar_refresh_token_with_deadline",
        forbidden_refresh,
    )

    real_shield = asyncio.shield
    interrupted = False

    def interrupt_guardian_after_worker(awaitable):
        nonlocal interrupted
        current = asyncio.current_task()
        if (
            current is not None
            and current.get_coro().cr_code
            is google_calendar_freebusy_service._guard_owned_worker.__code__
        ):

            async def wait_then_interrupt():
                nonlocal interrupted
                await real_shield(awaitable)
                interrupted = True
                raise KeyboardInterrupt(PRIVATE_DETAIL)

            return wait_then_interrupt()
        return real_shield(awaitable)

    monkeypatch.setattr(
        google_calendar_freebusy_service.asyncio,
        "shield",
        interrupt_guardian_after_worker,
    )

    async def scenario():
        current = asyncio.current_task()

        async def cancel_and_release():
            assert await asyncio.to_thread(worker_started.wait, 5)
            current.cancel("parent-cancel")
            release_worker.set()

        controller = asyncio.create_task(cancel_and_release())
        try:
            with pytest.raises(KeyboardInterrupt) as captured:
                await _query()
            assert str(captured.value) == ""
            assert captured.value.__cause__ is None
            assert captured.value.__context__ is None
        finally:
            release_worker.set()
            await controller

    _run(scenario())
    assert interrupted
    assert network_calls == []


def test_guardian_control_before_readiness_cannot_deadlock(monkeypatch):
    operation_calls = []
    hostile_cancel_calls = []

    class HostileCancelTask(asyncio.Task):
        def cancel(self, *_args, **_kwargs):
            hostile_cancel_calls.append(True)
            raise RuntimeError("hostile-cancel-private")

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        lambda *_args, **_kwargs: operation_calls.append(True),
    )

    source_lines, first_line = inspect.getsourcelines(
        google_calendar_freebusy_service._guard_owned_worker
    )
    ready_line = first_line + next(
        index
        for index, line in enumerate(source_lines)
        if "ready.set_result(None)" in line
    )
    interrupted = False

    def interrupt_before_ready(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code
            is google_calendar_freebusy_service._guard_owned_worker.__code__
            and frame.f_lineno == ready_line
        ):
            interrupted = True
            sys.settrace(None)
            raise KeyboardInterrupt(PRIVATE_DETAIL)
        return interrupt_before_ready

    async def scenario():
        loop = asyncio.get_running_loop()
        previous_factory = loop.get_task_factory()
        factory_calls = 0

        def hostile_first_task(_loop, coroutine, *, context=None):
            nonlocal factory_calls
            factory_calls += 1
            task_type = HostileCancelTask if factory_calls == 1 else asyncio.Task
            return task_type(coroutine, loop=loop, context=context)

        loop.set_task_factory(hostile_first_task)
        sys.settrace(interrupt_before_ready)
        try:
            with pytest.raises(KeyboardInterrupt) as captured:
                await _query()
            assert str(captured.value) == ""
            assert captured.value.__cause__ is None
            assert captured.value.__context__ is None
        finally:
            sys.settrace(None)
            loop.set_task_factory(previous_factory)

    _run(scenario())
    assert interrupted
    assert hostile_cancel_calls == []
    assert operation_calls == []


def test_parent_cancellation_cannot_mask_pre_ready_guardian_control(
    monkeypatch,
):
    operation_calls = []
    parent_task = None

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        lambda *_args, **_kwargs: operation_calls.append(True),
    )

    source_lines, first_line = inspect.getsourcelines(
        google_calendar_freebusy_service._guard_owned_worker
    )
    ready_line = first_line + next(
        index
        for index, line in enumerate(source_lines)
        if "ready.set_result(None)" in line
    )
    interrupted = False

    def interrupt_before_ready(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code
            is google_calendar_freebusy_service._guard_owned_worker.__code__
            and frame.f_lineno == ready_line
        ):
            interrupted = True
            sys.settrace(None)
            parent_task.cancel("parent-cancel")
            raise KeyboardInterrupt(PRIVATE_DETAIL)
        return interrupt_before_ready

    async def scenario():
        nonlocal parent_task
        parent_task = asyncio.current_task()
        sys.settrace(interrupt_before_ready)
        try:
            with pytest.raises(KeyboardInterrupt) as captured:
                await _query()
            assert str(captured.value) == ""
            assert captured.value.__cause__ is None
            assert captured.value.__context__ is None
        finally:
            sys.settrace(None)

    _run(scenario())
    assert interrupted
    assert operation_calls == []


def test_precommit_cleanup_cancellation_cannot_mask_ordinary_failure(
    monkeypatch,
):
    real_event = asyncio.Event
    operation_calls = []

    class CommitThenFailGate(real_event):
        def set(self):
            super().set()
            raise RuntimeError(PRIVATE_DETAIL)

    monkeypatch.setattr(
        google_calendar_freebusy_service.asyncio,
        "Event",
        CommitThenFailGate,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        lambda *_args, **_kwargs: operation_calls.append(True),
    )

    error = _assert_failure(
        _query(),
        ExternalCalendarUnavailable,
        reason="provider_unavailable",
    )
    assert error.__cause__ is None
    assert error.__context__ is None
    assert operation_calls == []


def test_interruption_before_start_decision_cannot_deadlock(monkeypatch):
    operation_calls = []

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        lambda *_args, **_kwargs: operation_calls.append(True),
    )

    source_lines, first_line = inspect.getsourcelines(
        google_calendar_freebusy_service._run_joined
    )
    decision_line = first_line + next(
        index
        for index, line in enumerate(source_lines)
        if "start_committed_signal.set_result(None)" in line
    )
    interrupted = False

    def interrupt_start_decision(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code
            is google_calendar_freebusy_service._run_joined.__code__
            and frame.f_lineno == decision_line
        ):
            interrupted = True
            sys.settrace(None)
            raise RuntimeError(PRIVATE_DETAIL)
        return interrupt_start_decision

    async def scenario():
        sys.settrace(interrupt_start_decision)
        try:
            with pytest.raises(ExternalCalendarUnavailable) as captured:
                await asyncio.wait_for(_query(), timeout=1)
            assert captured.value.reason_code == "provider_unavailable"
        finally:
            sys.settrace(None)

    _run(scenario())
    assert interrupted
    assert operation_calls == []


def test_precommit_cleanup_bypasses_hostile_task_cancel_override(
    monkeypatch,
):
    real_event = asyncio.Event
    operation_calls = []
    hostile_cancel_calls = []

    class CommitThenFailGate(real_event):
        def set(self):
            super().set()
            raise RuntimeError(PRIVATE_DETAIL)

    class HostileCancelTask(asyncio.Task):
        def cancel(self, *_args, **_kwargs):
            hostile_cancel_calls.append(True)
            raise RuntimeError("hostile-cancel-private")

    monkeypatch.setattr(
        google_calendar_freebusy_service.asyncio,
        "Event",
        CommitThenFailGate,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        lambda *_args, **_kwargs: operation_calls.append(True),
    )

    async def scenario():
        loop = asyncio.get_running_loop()
        previous_factory = loop.get_task_factory()
        factory_calls = 0

        def hostile_first_task(_loop, coroutine, *, context=None):
            nonlocal factory_calls
            factory_calls += 1
            task_type = HostileCancelTask if factory_calls == 1 else asyncio.Task
            return task_type(coroutine, loop=loop, context=context)

        loop.set_task_factory(hostile_first_task)
        try:
            with pytest.raises(ExternalCalendarUnavailable) as captured:
                await _query()
        finally:
            loop.set_task_factory(previous_factory)
        assert captured.value.reason_code == "provider_unavailable"

    _run(scenario())
    assert hostile_cancel_calls == []
    assert operation_calls == []


def test_guardian_aborts_raw_worker_if_precommit_cleanup_is_interrupted(
    monkeypatch,
):
    real_event = asyncio.Event
    operation_calls = []

    class FailBeforeCommitGate(real_event):
        def set(self):
            raise RuntimeError(PRIVATE_DETAIL)

    monkeypatch.setattr(
        google_calendar_freebusy_service.asyncio,
        "Event",
        FailBeforeCommitGate,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        lambda *_args, **_kwargs: operation_calls.append(True),
    )

    source_lines, first_line = inspect.getsourcelines(
        google_calendar_freebusy_service._finish_precommit_owned_worker
    )
    abort_line = first_line + next(
        index
        for index, line in enumerate(source_lines)
        if "start_committed.cancel()" in line
    )
    interrupted = False

    def interrupt_parent_cleanup(frame, event, _argument):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code
            is google_calendar_freebusy_service
            ._finish_precommit_owned_worker.__code__
            and frame.f_lineno == abort_line
        ):
            interrupted = True
            sys.settrace(None)
            raise KeyboardInterrupt(PRIVATE_DETAIL)
        return interrupt_parent_cleanup

    async def scenario():
        sys.settrace(interrupt_parent_cleanup)
        try:
            with pytest.raises(KeyboardInterrupt) as captured:
                await _query()
            assert str(captured.value) == ""
            assert captured.value.__cause__ is None
            assert captured.value.__context__ is None
            await asyncio.sleep(0)
        finally:
            sys.settrace(None)

    _run(scenario())
    assert interrupted
    assert operation_calls == []


def test_direct_guardian_cancellation_is_not_treated_as_induced(
    monkeypatch,
):
    real_event = asyncio.Event
    guardian_task = None
    operation_calls = []
    hostile_cancelling_calls = []

    class HostileCancellingTask(asyncio.Task):
        def cancelling(self):
            hostile_cancelling_calls.append(True)
            raise RuntimeError("hostile-cancelling-private")

    class CancelGuardianThenFailGate(real_event):
        def set(self):
            guardian_task.cancel("guardian-cancel-private")
            super().set()
            raise RuntimeError(PRIVATE_DETAIL)

    monkeypatch.setattr(
        google_calendar_freebusy_service.asyncio,
        "Event",
        CancelGuardianThenFailGate,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        lambda *_args, **_kwargs: operation_calls.append(True),
    )

    real_guardian_factory = (
        google_calendar_freebusy_service._create_owned_base_task
    )

    def capture_guardian(owner, coroutine):
        nonlocal guardian_task
        guardian_task = real_guardian_factory(owner, coroutine)
        return guardian_task

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "_create_owned_base_task",
        capture_guardian,
    )

    async def scenario():
        nonlocal guardian_task
        loop = asyncio.get_running_loop()
        previous_factory = loop.get_task_factory()
        def hostile_task_factory(_loop, coroutine, *, context=None):
            return HostileCancellingTask(
                coroutine,
                loop=loop,
                context=context,
            )

        loop.set_task_factory(hostile_task_factory)
        try:
            with pytest.raises(asyncio.CancelledError) as captured:
                await google_calendar_freebusy_service._run_joined(
                    lambda: operation_calls.append(True)
                )
        finally:
            loop.set_task_factory(previous_factory)
        assert str(captured.value) == ""
        assert captured.value.args == ()
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None

    _run(scenario())
    assert hostile_cancelling_calls == []
    assert operation_calls == []


def test_guardian_creation_preserves_custom_loop_task_factory():
    factory_calls = []

    async def guardian_operation():
        return google_calendar_freebusy_service._new_owned_worker_outcome(
            value=None,
        )

    async def scenario():
        loop = asyncio.get_running_loop()
        previous_factory = loop.get_task_factory()

        def custom_task_factory(_loop, coroutine, *, context=None):
            factory_calls.append(True)
            return asyncio.Task(coroutine, loop=loop, context=context)

        loop.set_task_factory(custom_task_factory)
        try:
            async with google_calendar_freebusy_service._OwnedTaskGroup() as owner:
                guardian = (
                    google_calendar_freebusy_service._create_owned_base_task(
                        owner,
                        guardian_operation(),
                    )
                )
                assert type(guardian) is asyncio.Task
                assert loop.get_task_factory() is custom_task_factory
                outcome = await guardian
        finally:
            loop.set_task_factory(previous_factory)

        assert type(outcome) is google_calendar_freebusy_service._OwnedWorkerOutcome

    _run(scenario())
    assert factory_calls == []


def test_joined_worker_cancel_accepts_optional_message_without_abandoning():
    async def worker_operation():
        await asyncio.sleep(0)
        return google_calendar_freebusy_service._new_owned_worker_outcome(
            value="settled",
        )

    async def scenario():
        worker = google_calendar_freebusy_service._JoinedWorkerTask(
            worker_operation(),
            loop=asyncio.get_running_loop(),
        )
        assert worker.cancel() is False
        assert worker.cancel(msg="reason") is False
        outcome = await worker
        assert outcome.value == "settled"

    _run(scenario())


def test_duplicate_worker_is_rejected_before_its_coroutine_can_run():
    rejected_calls = []

    async def worker_operation(value):
        if value == "rejected":
            rejected_calls.append(True)
        return google_calendar_freebusy_service._new_owned_worker_outcome(
            value=value,
        )

    async def scenario():
        async with google_calendar_freebusy_service._OwnedTaskGroup() as owner:
            worker = (
                google_calendar_freebusy_service._create_owned_worker_task(
                    owner,
                    worker_operation("owned"),
                )
            )
            with pytest.raises(RuntimeError):
                google_calendar_freebusy_service._create_owned_worker_task(
                    owner,
                    worker_operation("rejected"),
                )
            outcome = await worker
        assert outcome.value == "owned"

    _run(scenario())
    assert rejected_calls == []


def test_induced_cancellation_provenance_survives_envelope_failure(
    monkeypatch,
):
    real_event = asyncio.Event
    operation_calls = []
    real_outcome_factory = (
        google_calendar_freebusy_service._new_owned_worker_outcome
    )

    class CommitThenFailGate(real_event):
        def set(self):
            super().set()
            raise RuntimeError(PRIVATE_DETAIL)

    def interrupted_failed_envelope(**kwargs):
        if kwargs.get("failed") is True:
            raise RuntimeError("failed-envelope-private")
        return real_outcome_factory(**kwargs)

    monkeypatch.setattr(
        google_calendar_freebusy_service.asyncio,
        "Event",
        CommitThenFailGate,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        lambda *_args, **_kwargs: operation_calls.append(True),
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "_new_owned_worker_outcome",
        interrupted_failed_envelope,
    )

    error = _assert_failure(
        _query(),
        ExternalCalendarUnavailable,
        reason="provider_unavailable",
    )
    assert error.__cause__ is None
    assert error.__context__ is None
    assert operation_calls == []


@pytest.mark.parametrize("stage", ["refresh", "freebusy"])
def test_collaborator_cancellation_cannot_spoof_caller_cancellation(
    monkeypatch,
    stage,
):
    events = []
    _install_success_dependencies(monkeypatch, events)

    async def forged_cancellation(**_kwargs):
        assert asyncio.current_task().cancelling() == 0
        raise asyncio.CancelledError("collaborator-secret-private")

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        (
            "exchange_google_calendar_refresh_token_with_deadline"
            if stage == "refresh"
            else "query_google_calendar_freebusy_with_deadline"
        ),
        forged_cancellation,
    )

    with pytest.raises(asyncio.CancelledError) as captured:
        _run(_query())
    assert str(captured.value) == ""
    assert captured.value.args == ()
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_anyio_timeout_preserves_direct_cancellation_provenance(monkeypatch):
    network_calls = []

    def slow_claim(*_args, **_kwargs):
        time.sleep(0.05)
        return _claim()

    async def forbidden_refresh(**_kwargs):
        network_calls.append(True)
        raise AssertionError("timed-out claim reached network")

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        slow_claim,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "exchange_google_calendar_refresh_token_with_deadline",
        forbidden_refresh,
    )

    async def scenario():
        with pytest.raises(TimeoutError):
            with anyio.fail_after(0.01):
                await _query()

    _run(scenario())
    assert network_calls == []


def test_cancelled_parent_waits_for_grouped_worker_control(monkeypatch):
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()

    def controlled_claim(*_args, **_kwargs):
        worker_started.set()
        assert release_worker.wait(timeout=5)
        worker_finished.set()
        raise BaseExceptionGroup(
            "mixed worker controls",
            [asyncio.CancelledError(), KeyboardInterrupt(PRIVATE_DETAIL)],
        )

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        controlled_claim,
    )

    async def scenario():
        current = asyncio.current_task()

        async def cancel_and_release():
            assert await asyncio.to_thread(worker_started.wait, 5)
            current.cancel()
            release_worker.set()

        controller = asyncio.create_task(cancel_and_release())
        try:
            with pytest.raises(KeyboardInterrupt) as captured:
                await _query()
            assert str(captured.value) == ""
            assert captured.value.__cause__ is None
            assert captured.value.__context__ is None
        finally:
            await controller

    _run(scenario())
    assert worker_finished.is_set()


def test_custom_task_factory_cannot_create_unowned_database_work():
    operation_calls = []
    factory_calls = []

    async def scenario():
        loop = asyncio.get_running_loop()
        previous_factory = loop.get_task_factory()

        def forbidden_factory(_loop, _coroutine, *, context=None):
            del context
            factory_calls.append(True)
            raise AssertionError("custom task factory was invoked")

        loop.set_task_factory(forbidden_factory)
        try:
            result = await google_calendar_freebusy_service._run_joined(
                lambda: operation_calls.append(True) or "settled",
            )
        finally:
            loop.set_task_factory(previous_factory)
        assert result == "settled"

    _run(scenario())
    assert operation_calls == [True]
    assert factory_calls == []


@pytest.mark.parametrize("branch", ["success", "recovery"])
def test_cancellation_after_final_database_phase_wins(monkeypatch, branch):
    events = []
    _install_success_dependencies(monkeypatch, events)
    if branch == "recovery":
        outcome = _authorization_required()

        async def invalid_grant(**_kwargs):
            events.append(("refresh", threading.get_ident()))
            return outcome

        def recovery_callback(*_args, **_kwargs):
            events.append(("recovery", threading.get_ident()))

        monkeypatch.setattr(
            google_calendar_freebusy_service,
            "exchange_google_calendar_refresh_token_with_deadline",
            invalid_grant,
        )
        monkeypatch.setattr(
            google_calendar_freebusy_service,
            "mark_google_calendar_refresh_credential_authorization_required",
            recovery_callback,
        )

    real_run_joined = google_calendar_freebusy_service._run_joined
    call_count = 0
    cancellation_call = 2 if branch == "recovery" else 3

    async def cancelling_run_joined(operation):
        nonlocal call_count
        call_count += 1
        result = await real_run_joined(operation)
        if call_count == cancellation_call:
            asyncio.current_task().cancel()
        return result

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "_run_joined",
        cancelling_run_joined,
    )

    async def scenario():
        task = asyncio.create_task(_query())
        with pytest.raises(asyncio.CancelledError) as captured:
            await task
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None

    _run(scenario())
    assert call_count == cancellation_call


def test_nested_non_cancel_control_precedes_cancellation():
    raised = BaseExceptionGroup(
        "mixed",
        [asyncio.CancelledError(), KeyboardInterrupt()],
    )
    outcome = google_calendar_freebusy_service._safe_classified_service_failure(
        raised
    )
    assert outcome == ("keyboard_interrupt", None, "provider_unavailable")


def test_dependency_details_and_secrets_do_not_escape(monkeypatch):
    claim = _claim()

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "claim_google_calendar_refresh_credential",
        lambda *_args, **_kwargs: claim,
    )

    async def failing_refresh(**_kwargs):
        raise RuntimeError(PRIVATE_DETAIL)

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "exchange_google_calendar_refresh_token_with_deadline",
        failing_refresh,
    )
    error = _assert_failure(
        _query(),
        ExternalCalendarUnavailable,
        reason="provider_unavailable",
    )
    rendered = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    local_values = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_filename.endswith(
            "/app/google_calendar_freebusy_service.py"
        ):
            local_values.extend(current.tb_frame.f_locals.values())
        current = current.tb_next
    local_repr = repr(local_values)
    for private in (
        PRIVATE_DETAIL,
        REFRESH_TOKEN,
        ACCESS_TOKEN,
        CLIENT_SECRET,
        ACCOUNT_ID,
        CONNECTION_ID,
    ):
        assert private not in rendered
        assert private not in local_repr


def test_postgres_success_has_no_database_lock_during_network(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, instructor_id = _add_persisted_connection("success")
    observed_claims = []

    async def exchange_callback(*, configuration, claim):
        assert type(configuration) is GoogleCalendarTokenExchangeConfiguration
        _assert_no_provider_account_lock()
        observed_claims.append(claim)
        trusted = (
            google_calendar_credential_handoff
            .validate_google_calendar_refresh_credential_claim(claim)
        )
        return google_calendar_token_exchange._new_refresh_exchange_result(
            account_id=trusted.account_id,
            instructor_id=trusted.instructor_id,
            connection_id=trusted.connection_id,
            expected_calendar_connection_revision=(
                trusted.expected_calendar_connection_revision
            ),
            access_token=ACCESS_TOKEN,
            expires_in_seconds=3600,
        )

    async def freebusy_callback(
        *,
        claim,
        access_result,
        window_start,
        window_end,
    ):
        del access_result
        _assert_no_provider_account_lock()
        assert claim is observed_claims[0]
        trusted = (
            google_calendar_credential_handoff
            .validate_google_calendar_refresh_credential_claim(claim)
        )
        return google_calendar_freebusy._new_query_result(
            account_id=trusted.account_id,
            instructor_id=trusted.instructor_id,
            connection_id=trusted.connection_id,
            expected_calendar_connection_revision=(
                trusted.expected_calendar_connection_revision
            ),
            window_start=window_start,
            window_end=window_end,
            periods=(
                CalendarBusyInterval(
                    start=window_start + datetime.timedelta(hours=1),
                    end=window_start + datetime.timedelta(hours=2),
                ),
            ),
        )

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "exchange_google_calendar_refresh_token_with_deadline",
        exchange_callback,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "query_google_calendar_freebusy_with_deadline",
        freebusy_callback,
    )

    snapshot = _run(
        _query(
            db.SessionLocal,
            instructor_id=instructor_id,
        )
    )

    assert snapshot.status == "verified"
    assert snapshot.periods == (
        CalendarBusyInterval(
            start=WINDOW_START + datetime.timedelta(hours=1),
            end=WINDOW_START + datetime.timedelta(hours=2),
        ),
    )
    stored = _stored_connection(account_id)
    assert stored["authorization_required_at"] is None
    assert stored["revision"] == 1


def test_postgres_replacement_during_refresh_stops_before_freebusy(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, instructor_id = _add_persisted_connection(
        "refresh-replacement"
    )
    original_connection_id = _stored_connection(account_id)["id"]
    freebusy_calls = []

    async def exchange_callback(*, claim, **_kwargs):
        trusted = (
            google_calendar_credential_handoff
            .validate_google_calendar_refresh_credential_claim(claim)
        )
        await asyncio.to_thread(
            _replace_persisted_connection,
            account_id,
            token="refresh-gap-replacement-private",
        )
        return google_calendar_token_exchange._new_refresh_exchange_result(
            account_id=trusted.account_id,
            instructor_id=trusted.instructor_id,
            connection_id=trusted.connection_id,
            expected_calendar_connection_revision=(
                trusted.expected_calendar_connection_revision
            ),
            access_token=ACCESS_TOKEN,
            expires_in_seconds=3600,
        )

    async def forbidden_freebusy(**_kwargs):
        freebusy_calls.append(True)
        raise AssertionError("stale refresh result reached FreeBusy")

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "exchange_google_calendar_refresh_token_with_deadline",
        exchange_callback,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "query_google_calendar_freebusy_with_deadline",
        forbidden_freebusy,
    )

    _assert_failure(
        _query(db.SessionLocal, instructor_id=instructor_id),
        ExternalCalendarUnavailable,
        reason="provider_unavailable",
    )
    assert freebusy_calls == []
    stored = _stored_connection(account_id)
    assert stored["id"] != original_connection_id
    assert stored["authorization_required_at"] is None
    assert stored["revision"] == 2


def test_postgres_replacement_during_freebusy_discards_periods(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, instructor_id = _add_persisted_connection("replacement")
    original_connection_id = _stored_connection(account_id)["id"]

    async def exchange_callback(*, claim, **_kwargs):
        trusted = (
            google_calendar_credential_handoff
            .validate_google_calendar_refresh_credential_claim(claim)
        )
        return google_calendar_token_exchange._new_refresh_exchange_result(
            account_id=trusted.account_id,
            instructor_id=trusted.instructor_id,
            connection_id=trusted.connection_id,
            expected_calendar_connection_revision=(
                trusted.expected_calendar_connection_revision
            ),
            access_token=ACCESS_TOKEN,
            expires_in_seconds=3600,
        )

    async def freebusy_callback(
        *,
        claim,
        window_start,
        window_end,
        **_kwargs,
    ):
        await asyncio.to_thread(
            _replace_persisted_connection,
            account_id,
            token="replacement-refresh-token-private",
        )
        trusted = (
            google_calendar_credential_handoff
            .validate_google_calendar_refresh_credential_claim(claim)
        )
        return google_calendar_freebusy._new_query_result(
            account_id=trusted.account_id,
            instructor_id=trusted.instructor_id,
            connection_id=trusted.connection_id,
            expected_calendar_connection_revision=(
                trusted.expected_calendar_connection_revision
            ),
            window_start=window_start,
            window_end=window_end,
            periods=(
                CalendarBusyInterval(
                    start=window_start,
                    end=window_start + datetime.timedelta(hours=1),
                ),
            ),
        )

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "exchange_google_calendar_refresh_token_with_deadline",
        exchange_callback,
    )
    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "query_google_calendar_freebusy_with_deadline",
        freebusy_callback,
    )

    _assert_failure(
        _query(db.SessionLocal, instructor_id=instructor_id),
        ExternalCalendarUnavailable,
        reason="provider_unavailable",
    )
    stored = _stored_connection(account_id)
    assert stored["id"] != original_connection_id
    assert stored["authorization_required_at"] is None
    assert stored["revision"] == 2


def test_postgres_invalid_grant_marks_exact_generation(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, instructor_id = _add_persisted_connection("invalid-grant")

    async def exchange_callback(*, claim, **_kwargs):
        trusted = (
            google_calendar_credential_handoff
            .validate_google_calendar_refresh_credential_claim(claim)
        )
        return google_calendar_token_exchange._new_refresh_authorization_required(
            account_id=trusted.account_id,
            instructor_id=trusted.instructor_id,
            connection_id=trusted.connection_id,
            expected_calendar_connection_revision=(
                trusted.expected_calendar_connection_revision
            ),
        )

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "exchange_google_calendar_refresh_token_with_deadline",
        exchange_callback,
    )

    _assert_failure(
        _query(db.SessionLocal, instructor_id=instructor_id),
        ExternalCalendarAuthorizationRequired,
        reason="authorization_required",
    )
    stored = _stored_connection(account_id)
    assert stored["authorization_required_at"] is not None
    assert stored["revision"] == 1


def test_postgres_stale_invalid_grant_does_not_poison_replacement(
    migrated_postgres_db,
    monkeypatch,
):
    account_id, instructor_id = _add_persisted_connection("stale-grant")
    original_connection_id = _stored_connection(account_id)["id"]

    async def exchange_callback(*, claim, **_kwargs):
        trusted = (
            google_calendar_credential_handoff
            .validate_google_calendar_refresh_credential_claim(claim)
        )
        await asyncio.to_thread(
            _replace_persisted_connection,
            account_id,
            token="stale-grant-replacement-private",
        )
        return google_calendar_token_exchange._new_refresh_authorization_required(
            account_id=trusted.account_id,
            instructor_id=trusted.instructor_id,
            connection_id=trusted.connection_id,
            expected_calendar_connection_revision=(
                trusted.expected_calendar_connection_revision
            ),
        )

    monkeypatch.setattr(
        google_calendar_freebusy_service,
        "exchange_google_calendar_refresh_token_with_deadline",
        exchange_callback,
    )

    _assert_failure(
        _query(db.SessionLocal, instructor_id=instructor_id),
        ExternalCalendarUnavailable,
        reason="provider_unavailable",
    )
    stored = _stored_connection(account_id)
    assert stored["id"] != original_connection_id
    assert stored["authorization_required_at"] is None
    assert stored["revision"] == 2


def _add_persisted_connection(suffix: str) -> tuple[str, str]:
    account_id = f"acct_freebusy_service_{suffix}"
    instructor_id = f"inst_freebusy_service_{suffix}"
    with db.SessionLocal.begin() as session:
        session.add(
            InstructorDB(
                id=instructor_id,
                name=f"FreeBusy Service {suffix}",
                main_address="1 Verified Way",
                lat=30.2,
                lng=-97.2,
                work_start_time="08:00",
                work_end_time="17:00",
                timezone="America/Chicago",
            )
        )
        session.add(
            ProviderAccountDB(
                id=account_id,
                instructor_id=instructor_id,
                email_normalized=f"freebusy-service-{suffix}@example.com",
                password_hash="not-used-by-freebusy-service-test",
                is_demo=False,
                session_version=1,
            )
        )
    _replace_persisted_connection(
        account_id,
        token=f"freebusy-service-{suffix}-refresh-private",
    )
    return account_id, instructor_id


def _replace_persisted_connection(account_id: str, *, token: str) -> None:
    with db.SessionLocal.begin() as session:
        replace_google_calendar_connection(
            session,
            account_id=account_id,
            refresh_token=token,
            cipher=CIPHER,
            now=datetime.datetime.now(UTC) + datetime.timedelta(minutes=1),
        )


def _stored_connection(account_id: str):
    with db.SessionLocal() as session:
        row = session.query(ProviderCalendarConnectionDB).filter_by(
            account_id=account_id
        ).one()
        account = session.get(ProviderAccountDB, account_id)
        return {
            "id": row.id,
            "authorization_required_at": row.authorization_required_at,
            "revision": account.calendar_connection_revision,
        }


def _assert_no_provider_account_lock() -> None:
    with db.SessionLocal.begin() as session:
        session.execute(
            text("LOCK TABLE provider_accounts IN ACCESS EXCLUSIVE MODE NOWAIT")
        )
