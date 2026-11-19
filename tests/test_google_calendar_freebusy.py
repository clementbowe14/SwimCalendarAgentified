from __future__ import annotations

import asyncio
import datetime
import inspect
import json
import pickle
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import google_calendar_freebusy
from app.external_calendar import (
    CalendarBusyInterval,
    ExternalCalendarAuthorizationRequired,
    ExternalCalendarInvalidResponse,
    ExternalCalendarTimeout,
    ExternalCalendarUnavailable,
    MAX_BUSY_QUERY_WINDOW,
)
from app.google_calendar_credential_handoff import _new_claim
from app.google_calendar_freebusy import (
    GOOGLE_CALENDAR_FREEBUSY_ENDPOINT,
    GoogleCalendarFreeBusyQueryResult,
    GoogleCalendarFreeBusyTransportResponse,
    query_google_calendar_freebusy,
)
from app.google_calendar_token_exchange import (
    GoogleCalendarRefreshTokenExchangeResult,
    _new_refresh_exchange_result,
)


UTC = datetime.timezone.utc
ACCOUNT_ID = "acct_freebusy_private"
INSTRUCTOR_ID = "inst_freebusy_private"
CONNECTION_ID = "calconn_" + "a" * 32
REVISION = 7
REFRESH_TOKEN = "refresh-token-freebusy-private"
ACCESS_TOKEN = "access-token-freebusy-private"
PROVIDER_DETAIL = "provider-event-detail-private"
WINDOW_START = datetime.datetime(2026, 11, 19, 10, tzinfo=UTC)
WINDOW_END = datetime.datetime(2026, 11, 19, 16, tzinfo=UTC)
_UNSET = object()


def _claim(
    *,
    account_id=ACCOUNT_ID,
    instructor_id=INSTRUCTOR_ID,
    connection_id=CONNECTION_ID,
    revision=REVISION,
    refresh_token=REFRESH_TOKEN,
):
    return _new_claim(
        account_id=account_id,
        instructor_id=instructor_id,
        connection_id=connection_id,
        expected_calendar_connection_revision=revision,
        refresh_token=refresh_token,
    )


def _access_result(
    *,
    account_id=ACCOUNT_ID,
    instructor_id=INSTRUCTOR_ID,
    connection_id=CONNECTION_ID,
    revision=REVISION,
    access_token=ACCESS_TOKEN,
    expires_in_seconds=3600,
):
    return _new_refresh_exchange_result(
        account_id=account_id,
        instructor_id=instructor_id,
        connection_id=connection_id,
        expected_calendar_connection_revision=revision,
        access_token=access_token,
        expires_in_seconds=expires_in_seconds,
    )


def _payload(*, busy=_UNSET, **overrides):
    if busy is _UNSET:
        busy = [
            {
                "start": "2026-11-19T10:30:00Z",
                "end": "2026-11-19T11:00:00Z",
            }
        ]
    value = {
        "kind": "calendar#freeBusy",
        "timeMin": "2026-11-19T10:00:00Z",
        "timeMax": "2026-11-19T16:00:00Z",
        "calendars": {"canonical@example.com": {"busy": busy}},
    }
    value.update(overrides)
    return value


def _response(
    *,
    status_code=200,
    payload=None,
    body=None,
    content_type="application/json",
):
    if body is None:
        if payload is None:
            payload = _payload()
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return GoogleCalendarFreeBusyTransportResponse(
        status_code=status_code,
        content_type=content_type,
        body=body,
    )


class RecordingTransport:
    def __init__(self, response=None, *, handler=None, error=None):
        self.response = _response() if response is None else response
        self.handler = handler
        self.error = error
        self.calls = []

    def post_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.handler is not None:
            return self.handler(kwargs)
        return self.response


def _query(
    transport,
    *,
    claim=None,
    access_result=None,
    window_start=WINDOW_START,
    window_end=WINDOW_END,
):
    return query_google_calendar_freebusy(
        claim=_claim() if claim is None else claim,
        access_result=(
            _access_result() if access_result is None else access_result
        ),
        window_start=window_start,
        window_end=window_end,
        transport=transport,
    )


def _formatted(error: BaseException) -> str:
    return "".join(traceback.format_exception(error))


def test_public_surface_is_exact_and_keyword_only():
    assert google_calendar_freebusy.__all__ == (
        "GOOGLE_CALENDAR_FREEBUSY_ENDPOINT",
        "GoogleCalendarFreeBusyQueryResult",
        "GoogleCalendarFreeBusyTransport",
        "GoogleCalendarFreeBusyTransportResponse",
        "query_google_calendar_freebusy",
    )
    assert GOOGLE_CALENDAR_FREEBUSY_ENDPOINT == (
        "https://www.googleapis.com/calendar/v3/freeBusy"
    )
    signature = inspect.signature(query_google_calendar_freebusy)
    assert list(signature.parameters) == [
        "claim",
        "access_result",
        "window_start",
        "window_end",
        "transport",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )


def test_exact_request_and_normalized_claim_bound_result():
    transport = RecordingTransport(
        _response(
            payload=_payload(
                busy=[
                    {
                        "start": "2026-11-19T12:00:00+00:00",
                        "end": "2026-11-19T13:00:00Z",
                    },
                    {
                        "start": "2026-11-19T10:30:00Z",
                        "end": "2026-11-19T11:30:00Z",
                    },
                    {
                        "start": "2026-11-19T11:30:00Z",
                        "end": "2026-11-19T12:15:00Z",
                    },
                ]
            )
        )
    )

    result = _query(transport)

    assert type(result) is GoogleCalendarFreeBusyQueryResult
    assert result.account_id == ACCOUNT_ID
    assert result.instructor_id == INSTRUCTOR_ID
    assert result.connection_id == CONNECTION_ID
    assert result.expected_calendar_connection_revision == REVISION
    assert result.window_start == WINDOW_START
    assert result.window_end == WINDOW_END
    assert result.periods == (
        CalendarBusyInterval(
            start=datetime.datetime(2026, 11, 19, 10, 30, tzinfo=UTC),
            end=datetime.datetime(2026, 11, 19, 13, tzinfo=UTC),
        ),
    )
    assert len(transport.calls) == 1
    assert transport.calls[0] == {
        "url": GOOGLE_CALENDAR_FREEBUSY_ENDPOINT,
        "access_token": ACCESS_TOKEN,
        "body": (
            b'{"timeMin":"2026-11-19T10:00:00Z",'
            b'"timeMax":"2026-11-19T16:00:00Z",'
            b'"timeZone":"UTC","calendarExpansionMax":1,'
            b'"items":[{"id":"primary"}]}'
        ),
        "timeout_seconds": 5.0,
    }
    assert ACCESS_TOKEN.encode() not in transport.calls[0]["body"]
    assert REFRESH_TOKEN.encode() not in transport.calls[0]["body"]
    assert ACCOUNT_ID.encode() not in transport.calls[0]["body"]
    assert INSTRUCTOR_ID.encode() not in transport.calls[0]["body"]
    assert CONNECTION_ID.encode() not in transport.calls[0]["body"]


def test_empty_busy_list_is_the_only_verified_empty_protocol_result():
    result = _query(RecordingTransport(_response(payload=_payload(busy=[]))))

    assert result.periods == ()
    assert result.window_start == WINDOW_START
    assert result.window_end == WINDOW_END


def test_representative_google_success_envelope_is_accepted():
    start = WINDOW_START.replace(microsecond=123456)
    end = WINDOW_END.replace(microsecond=654321)
    payload = {
        "kind": "calendar#freeBusy",
        "timeMin": "2026-11-19T10:00:00.123456Z",
        "timeMax": "2026-11-19T16:00:00.654321Z",
        "groups": {},
        "calendars": {
            "resolved-calendar@example.com": {
                "errors": [],
                "busy": [
                    {
                        "start": "2026-11-19T10:30:00.123456Z",
                        "end": "2026-11-19T11:00:00.654321Z",
                    }
                ],
            }
        },
    }

    result = _query(
        RecordingTransport(
            _response(
                payload=payload,
                content_type="application/json; charset=UTF-8",
            )
        ),
        window_start=start,
        window_end=end,
    )

    assert result.periods == (
        CalendarBusyInterval(
            start=datetime.datetime(
                2026, 11, 19, 10, 30, 0, 123456, tzinfo=UTC
            ),
            end=datetime.datetime(
                2026, 11, 19, 11, 0, 0, 654321, tzinfo=UTC
            ),
        ),
    )


def test_response_calendar_key_is_discarded_not_assumed_to_be_primary():
    result = _query(
        RecordingTransport(
            _response(
                payload=_payload(
                    calendars={
                        "resolved-calendar-private@example.com": {
                            "busy": []
                        }
                    }
                )
            )
        )
    )

    assert result.periods == ()
    assert "resolved-calendar-private" not in repr(result)
    assert not hasattr(result, "calendar_id")


@pytest.mark.parametrize(
    ("claim", "access_result", "start", "end"),
    [
        (object(), _access_result(), WINDOW_START, WINDOW_END),
        (_claim(), object(), WINDOW_START, WINDOW_END),
        (
            _claim(),
            _access_result(instructor_id="inst_mismatch"),
            WINDOW_START,
            WINDOW_END,
        ),
        (
            _claim(),
            _access_result(),
            datetime.datetime(2026, 11, 19, 10),
            WINDOW_END,
        ),
        (_claim(), _access_result(), WINDOW_START, WINDOW_START),
        (
            _claim(),
            _access_result(),
            WINDOW_START,
            WINDOW_START
            + MAX_BUSY_QUERY_WINDOW
            + datetime.timedelta(microseconds=1),
        ),
    ],
)
def test_invalid_inputs_fail_before_transport(claim, access_result, start, end):
    transport = RecordingTransport()

    with pytest.raises(ExternalCalendarInvalidResponse):
        _query(
            transport,
            claim=claim,
            access_result=access_result,
            window_start=start,
            window_end=end,
        )

    assert transport.calls == []


def test_forged_access_result_cannot_cross_bind_a_victim_fence():
    legitimate = _access_result()
    forged = object.__new__(GoogleCalendarRefreshTokenExchangeResult)
    for name in (
        "account_id",
        "instructor_id",
        "connection_id",
        "expected_calendar_connection_revision",
        "expires_in_seconds",
        "_integrity_tag",
    ):
        object.__setattr__(forged, name, getattr(legitimate, name))
    object.__setattr__(
        forged,
        "access_token",
        "attacker-calendar-access-token-private",
    )
    transport = RecordingTransport()

    with pytest.raises(ExternalCalendarInvalidResponse):
        _query(transport, access_result=forged)

    assert transport.calls == []


@pytest.mark.parametrize("target", ["claim", "access_result"])
def test_mutation_across_transport_gap_fails_closed(target):
    claim = _claim()
    access_result = _access_result()

    def mutate(_request):
        if target == "claim":
            replacement = _claim(
                account_id="acct_replacement",
                instructor_id="inst_replacement",
                connection_id="calconn_" + "b" * 32,
                revision=11,
                refresh_token="replacement-refresh-private",
            )
            for name in (
                "account_id",
                "instructor_id",
                "connection_id",
                "expected_calendar_connection_revision",
                "refresh_token",
                "_integrity_tag",
            ):
                object.__setattr__(claim, name, getattr(replacement, name))
        else:
            replacement = _access_result(
                access_token="replacement-access-private",
                expires_in_seconds=1800,
            )
            for name in (
                "account_id",
                "instructor_id",
                "connection_id",
                "expected_calendar_connection_revision",
                "access_token",
                "expires_in_seconds",
            ):
                object.__setattr__(
                    access_result,
                    name,
                    getattr(replacement, name),
                )
        return _response()

    transport = RecordingTransport(handler=mutate)

    with pytest.raises(ExternalCalendarInvalidResponse):
        _query(transport, claim=claim, access_result=access_result)

    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("status_code", "expected_type", "reason"),
    [
        (401, ExternalCalendarAuthorizationRequired, "authorization_required"),
        (403, ExternalCalendarUnavailable, "provider_unavailable"),
        (408, ExternalCalendarUnavailable, "provider_unavailable"),
        (429, ExternalCalendarUnavailable, "provider_unavailable"),
        (500, ExternalCalendarUnavailable, "provider_unavailable"),
        (503, ExternalCalendarUnavailable, "provider_unavailable"),
        (302, ExternalCalendarInvalidResponse, "invalid_response"),
        (400, ExternalCalendarInvalidResponse, "invalid_response"),
        (404, ExternalCalendarInvalidResponse, "invalid_response"),
    ],
)
def test_statuses_fail_closed_without_parsing_diagnostic_bodies(
    status_code,
    expected_type,
    reason,
    monkeypatch,
):
    monkeypatch.setattr(
        google_calendar_freebusy.json,
        "loads",
        lambda *_args, **_kwargs: pytest.fail(
            "non-success response body was parsed"
        ),
    )
    secret_body = b"\xff" + PROVIDER_DETAIL.encode()
    transport = RecordingTransport(
        _response(status_code=status_code, body=secret_body)
    )

    with pytest.raises(expected_type) as raised:
        _query(transport)

    assert raised.value.reason_code == reason
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("error", "expected_type", "reason"),
    [
        (
            TimeoutError(PROVIDER_DETAIL),
            ExternalCalendarTimeout,
            "timeout",
        ),
        (
            RuntimeError(PROVIDER_DETAIL),
            ExternalCalendarUnavailable,
            "provider_unavailable",
        ),
    ],
)
def test_transport_failures_are_sanitized(error, expected_type, reason):
    with pytest.raises(expected_type) as raised:
        _query(RecordingTransport(error=error))

    assert raised.value.reason_code == reason
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    output = " ".join((repr(raised.value), _formatted(raised.value)))
    for private in (
        PROVIDER_DETAIL,
        ACCESS_TOKEN,
        REFRESH_TOKEN,
        ACCOUNT_ID,
        INSTRUCTOR_ID,
        CONNECTION_ID,
    ):
        assert private not in output


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            GoogleCalendarFreeBusyTransportResponse(
                status_code="200",
                content_type="application/json",
                body=b"{}",
            ),
            "invalid_response",
        ),
        (_response(content_type="text/plain"), "invalid_response"),
        (_response(content_type="application/json; charset=latin-1"), "invalid_response"),
        (_response(body=b""), "invalid_response"),
        (_response(body=b"\xff"), "invalid_response"),
        (
            _response(
                body=b"x"
                * (google_calendar_freebusy._MAX_RESPONSE_BODY_BYTES + 1)
            ),
            "invalid_response",
        ),
    ],
)
def test_transport_response_contract_is_strict(response, reason):
    with pytest.raises(ExternalCalendarInvalidResponse) as raised:
        _query(RecordingTransport(response))

    assert raised.value.reason_code == reason


def test_response_body_accepts_the_exact_one_mebibyte_limit():
    exact_body_limit = 1_048_576
    assert google_calendar_freebusy._MAX_RESPONSE_BODY_BYTES == exact_body_limit
    encoded = json.dumps(
        _payload(busy=[]), separators=(",", ":")
    ).encode("utf-8")
    body = encoded + b" " * (exact_body_limit - len(encoded))

    result = _query(RecordingTransport(_response(body=body)))

    assert len(body) == exact_body_limit
    assert result.periods == ()


@pytest.mark.parametrize(
    "body",
    [
        (
            b'{"kind":"calendar#freeBusy","kind":"duplicate",'
            b'"timeMin":"2026-11-19T10:00:00Z",'
            b'"timeMax":"2026-11-19T16:00:00Z",'
            b'"calendars":{"x":{"busy":[]}}}'
        ),
        b"[]",
        b'{"kind":NaN}',
        b'{"kind":Infinity}',
    ],
)
def test_json_parser_rejects_duplicate_or_nonstandard_shapes(body):
    with pytest.raises(ExternalCalendarInvalidResponse):
        _query(RecordingTransport(_response(body=body)))


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "calendar#events"},
        {"timeMin": "2026-11-19T10:00:00.000001Z"},
        {"timeMax": "2026-11-19T16:00:01Z"},
        {"groups": {"private-group": {}}},
        {"unexpected": PROVIDER_DETAIL},
        {"calendars": {}},
        {
            "calendars": {
                "one": {"busy": []},
                "two": {"busy": []},
            }
        },
        {"calendars": {"": {"busy": []}}},
    ],
)
def test_envelope_mismatches_and_extra_details_fail_closed(overrides):
    with pytest.raises(ExternalCalendarInvalidResponse) as raised:
        _query(RecordingTransport(_response(payload=_payload(**overrides))))

    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_valid_provider_errors_are_unavailable_and_details_are_private():
    payload = _payload(
        calendars={
            "canonical@example.com": {
                "errors": [
                    {
                        "domain": "calendar",
                        "reason": PROVIDER_DETAIL,
                    }
                ],
            }
        }
    )

    with pytest.raises(ExternalCalendarUnavailable) as raised:
        _query(RecordingTransport(_response(payload=payload)))

    assert type(raised.value) is ExternalCalendarUnavailable
    assert raised.value.reason_code == "provider_unavailable"
    assert PROVIDER_DETAIL not in _formatted(raised.value)


@pytest.mark.parametrize(
    "errors",
    [
        None,
        {},
        [PROVIDER_DETAIL],
        [{"domain": "calendar"}],
        [{"domain": "calendar", "reason": PROVIDER_DETAIL, "extra": "x"}],
    ],
)
def test_malformed_provider_errors_are_invalid_response(errors):
    payload = _payload(
        calendars={
            "canonical@example.com": {
                "busy": [],
                "errors": errors,
            }
        }
    )

    with pytest.raises(ExternalCalendarInvalidResponse):
        _query(RecordingTransport(_response(payload=payload)))


@pytest.mark.parametrize(
    "busy",
    [
        None,
        {},
        [PROVIDER_DETAIL],
        [{"start": "2026-11-19T10:30:00Z"}],
        [
            {
                "start": "2026-11-19T10:30:00Z",
                "end": "2026-11-19T11:00:00Z",
                "summary": PROVIDER_DETAIL,
            }
        ],
        [
            {
                "start": "2026-11-19",
                "end": "2026-11-19T11:00:00Z",
            }
        ],
        [
            {
                "start": "2026-11-19T10:30:00",
                "end": "2026-11-19T11:00:00Z",
            }
        ],
        [
            {
                "start": "2026-11-19T10:30:00.1234567Z",
                "end": "2026-11-19T11:00:00Z",
            }
        ],
        [
            {
                "start": "2026-11-19T10:30:60Z",
                "end": "2026-11-19T11:00:00Z",
            }
        ],
    ],
)
def test_busy_entries_reject_event_details_and_bad_timestamps(busy):
    with pytest.raises(ExternalCalendarInvalidResponse) as raised:
        _query(RecordingTransport(_response(payload=_payload(busy=busy))))

    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_busy_interval_cardinality_accepts_exact_limit_and_rejects_one_more():
    entry = {
        "start": "2026-11-19T10:30:00Z",
        "end": "2026-11-19T11:00:00Z",
    }
    exact_interval_limit = 10_000
    assert google_calendar_freebusy.MAX_BUSY_INTERVALS == exact_interval_limit
    exact_limit = [entry] * exact_interval_limit
    result = _query(
        RecordingTransport(
            _response(payload=_payload(busy=exact_limit))
        )
    )
    assert result.periods == (
        CalendarBusyInterval(
            start=datetime.datetime(2026, 11, 19, 10, 30, tzinfo=UTC),
            end=datetime.datetime(2026, 11, 19, 11, tzinfo=UTC),
        ),
    )

    with pytest.raises(ExternalCalendarInvalidResponse):
        _query(
            RecordingTransport(
                _response(payload=_payload(busy=exact_limit + [entry]))
            )
        )


def test_query_window_accepts_the_exact_fifteen_day_limit():
    exact_window_limit = datetime.timedelta(days=15)
    assert MAX_BUSY_QUERY_WINDOW == exact_window_limit
    end = WINDOW_START + exact_window_limit
    payload = _payload(
        timeMax=google_calendar_freebusy._canonical_rfc3339(end),
        busy=[],
    )
    transport = RecordingTransport(_response(payload=payload))

    result = _query(transport, window_end=end)

    assert result.window_start == WINDOW_START
    assert result.window_end == end
    assert result.periods == ()
    request = json.loads(transport.calls[0]["body"])
    assert request["timeMax"] == "2026-12-04T10:00:00Z"


def test_offset_and_fractional_timestamps_compare_as_instants():
    payload = _payload(
        timeMin="2026-11-19T05:00:00-05:00",
        timeMax="2026-11-19T11:00:00-05:00",
        busy=[
            {
                "start": "2026-11-19T05:30:00.123456-05:00",
                "end": "2026-11-19T06:00:00.123456-05:00",
            }
        ],
    )

    result = _query(RecordingTransport(_response(payload=payload)))

    assert result.periods == (
        CalendarBusyInterval(
            start=datetime.datetime(
                2026,
                11,
                19,
                10,
                30,
                0,
                123456,
                tzinfo=UTC,
            ),
            end=datetime.datetime(
                2026,
                11,
                19,
                11,
                0,
                0,
                123456,
                tzinfo=UTC,
            ),
        ),
    )


@pytest.mark.parametrize(
    ("start", "end", "expected_hours"),
    [
        (
            "2026-03-08T00:00:00-05:00",
            "2026-03-09T00:00:00-04:00",
            23,
        ),
        (
            "2026-11-01T00:00:00-04:00",
            "2026-11-02T00:00:00-05:00",
            25,
        ),
    ],
)
def test_all_day_freebusy_ranges_preserve_dst_elapsed_time(
    start,
    end,
    expected_hours,
):
    parsed_start = google_calendar_freebusy._parse_rfc3339(start)
    parsed_end = google_calendar_freebusy._parse_rfc3339(end)
    window_start = parsed_start - datetime.timedelta(hours=1)
    window_end = parsed_end + datetime.timedelta(hours=1)
    payload = {
        "kind": "calendar#freeBusy",
        "timeMin": google_calendar_freebusy._canonical_rfc3339(window_start),
        "timeMax": google_calendar_freebusy._canonical_rfc3339(window_end),
        "calendars": {
            "canonical@example.com": {
                "busy": [{"start": start, "end": end}]
            }
        },
    }

    result = _query(
        RecordingTransport(_response(payload=payload)),
        window_start=window_start,
        window_end=window_end,
    )

    assert len(result.periods) == 1
    assert result.periods[0].end - result.periods[0].start == (
        datetime.timedelta(hours=expected_hours)
    )


def test_microsecond_query_window_is_canonical_and_echo_bound():
    start = WINDOW_START.replace(microsecond=123456)
    end = WINDOW_END.replace(microsecond=654321)
    payload = _payload(
        timeMin="2026-11-19T10:00:00.123456Z",
        timeMax="2026-11-19T16:00:00.654321Z",
        busy=[],
    )
    transport = RecordingTransport(_response(payload=payload))

    result = _query(transport, window_start=start, window_end=end)

    assert result.periods == ()
    request = json.loads(transport.calls[0]["body"])
    assert request["timeMin"] == "2026-11-19T10:00:00.123456Z"
    assert request["timeMax"] == "2026-11-19T16:00:00.654321Z"


def test_query_result_and_transport_response_are_redacted_and_sealed():
    response = _response()
    result = _query(RecordingTransport(response))

    assert repr(response) == (
        "GoogleCalendarFreeBusyTransportResponse(<redacted>)"
    )
    assert repr(result) == "GoogleCalendarFreeBusyQueryResult(<redacted>)"
    assert ACCESS_TOKEN not in repr(result)
    assert REFRESH_TOKEN not in repr(result)
    assert not hasattr(result, "access_token")
    assert not hasattr(result, "refresh_token")
    for name, value in (
        ("status_code", 204),
        ("content_type", "text/plain"),
        ("body", b"changed"),
    ):
        with pytest.raises(AttributeError):
            setattr(response, name, value)
    with pytest.raises(AttributeError):
        result.periods = ()
    for value in (response, result):
        with pytest.raises(TypeError):
            pickle.dumps(value)


@pytest.mark.parametrize(
    ("error_factory", "expected_type", "expected_exit_code"),
    [
        (lambda: asyncio.CancelledError(PROVIDER_DETAIL), asyncio.CancelledError, None),
        (lambda: KeyboardInterrupt(PROVIDER_DETAIL), KeyboardInterrupt, None),
        (lambda: SystemExit(7), SystemExit, 7),
        (lambda: SystemExit(PROVIDER_DETAIL), SystemExit, 1),
        (lambda: GeneratorExit(PROVIDER_DETAIL), GeneratorExit, None),
    ],
)
def test_control_exceptions_are_fresh_and_secret_free(
    error_factory,
    expected_type,
    expected_exit_code,
):
    with pytest.raises(expected_type) as raised:
        _query(RecordingTransport(error=error_factory()))

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    if expected_type is SystemExit:
        assert raised.value.code == expected_exit_code
    output = " ".join((repr(raised.value), _formatted(raised.value)))
    for private in (
        PROVIDER_DETAIL,
        ACCESS_TOKEN,
        REFRESH_TOKEN,
        ACCOUNT_ID,
        INSTRUCTOR_ID,
        CONNECTION_ID,
    ):
        assert private not in output


@pytest.mark.parametrize(
    ("error_factory", "expected_type", "expected_exit_code"),
    [
        (
            lambda: BaseExceptionGroup(
                PROVIDER_DETAIL,
                [asyncio.CancelledError(PROVIDER_DETAIL)],
            ),
            asyncio.CancelledError,
            None,
        ),
        (
            lambda: BaseExceptionGroup(
                PROVIDER_DETAIL,
                [
                    RuntimeError(PROVIDER_DETAIL),
                    KeyboardInterrupt(PROVIDER_DETAIL),
                ],
            ),
            KeyboardInterrupt,
            None,
        ),
        (
            lambda: BaseExceptionGroup(
                PROVIDER_DETAIL,
                [
                    RuntimeError(PROVIDER_DETAIL),
                    BaseExceptionGroup(
                        PROVIDER_DETAIL,
                        [SystemExit(PROVIDER_DETAIL)],
                    ),
                ],
            ),
            SystemExit,
            1,
        ),
    ],
)
def test_grouped_control_exceptions_are_recursively_sanitized(
    error_factory,
    expected_type,
    expected_exit_code,
):
    with pytest.raises(expected_type) as raised:
        _query(RecordingTransport(error=error_factory()))

    assert type(raised.value) is expected_type
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    if expected_type is SystemExit:
        assert raised.value.code == expected_exit_code
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_grouped_control_traversal_is_iterative_beyond_recursion_limit():
    grouped: BaseException = asyncio.CancelledError(PROVIDER_DETAIL)
    for _ in range(1_500):
        grouped = BaseExceptionGroup(PROVIDER_DETAIL, [grouped])

    with pytest.raises(asyncio.CancelledError) as raised:
        _query(RecordingTransport(error=grouped))

    assert type(raised.value) is asyncio.CancelledError
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_group_subclass_cannot_hide_a_stored_control_exception():
    class HidingGroup(BaseExceptionGroup):
        @property
        def exceptions(self):
            return (RuntimeError(PROVIDER_DETAIL),)

    grouped = HidingGroup(
        PROVIDER_DETAIL,
        [asyncio.CancelledError(PROVIDER_DETAIL)],
    )
    assert type(grouped.exceptions[0]) is RuntimeError
    assert type(
        BaseExceptionGroup.exceptions.__get__(grouped, BaseExceptionGroup)[0]
    ) is asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError) as raised:
        _query(RecordingTransport(error=grouped))

    assert type(raised.value) is asyncio.CancelledError
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert PROVIDER_DETAIL not in _formatted(raised.value)


def test_concurrent_queries_remain_provider_isolated():
    rendezvous = threading.Barrier(2)
    second_query_completed = threading.Event()
    completion_order = []
    completion_lock = threading.Lock()
    cases = []
    for index, suffix in enumerate(("b", "c"), start=1):
        account_id = f"acct_concurrent_{suffix}"
        instructor_id = f"inst_concurrent_{suffix}"
        connection_id = "calconn_" + suffix * 32
        access_token = f"access-concurrent-{suffix}"
        busy_start = WINDOW_START + datetime.timedelta(hours=index)
        busy_end = busy_start + datetime.timedelta(minutes=30)
        payload = _payload(
            busy=[
                {
                    "start": busy_start.isoformat().replace("+00:00", "Z"),
                    "end": busy_end.isoformat().replace("+00:00", "Z"),
                }
            ]
        )
        response = _response(payload=payload)

        def handle(_request, *, case_index=index, value=response):
            rendezvous.wait(timeout=5)
            if case_index == 1:
                assert second_query_completed.wait(timeout=5)
            return value

        transport = RecordingTransport(handler=handle)
        cases.append(
            (
                _claim(
                    account_id=account_id,
                    instructor_id=instructor_id,
                    connection_id=connection_id,
                    revision=index,
                    refresh_token=f"refresh-concurrent-{suffix}",
                ),
                _access_result(
                    account_id=account_id,
                    instructor_id=instructor_id,
                    connection_id=connection_id,
                    revision=index,
                    access_token=access_token,
                ),
                transport,
                busy_start,
                busy_end,
            )
        )

    def run(index_and_case):
        index, case = index_and_case
        claim, access_result, transport, _start, _end = case
        result = _query(
            transport,
            claim=claim,
            access_result=access_result,
        )
        with completion_lock:
            completion_order.append(claim.account_id)
        if index == 2:
            second_query_completed.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(run, enumerate(cases, start=1)))

    assert completion_order == [cases[1][0].account_id, cases[0][0].account_id]

    for result, case in zip(results, cases, strict=True):
        claim, access_result, transport, start, end = case
        assert result.account_id == claim.account_id
        assert result.connection_id == claim.connection_id
        assert result.periods == (CalendarBusyInterval(start=start, end=end),)
        assert transport.calls[0]["access_token"] == access_result.access_token
        assert len(transport.calls) == 1
