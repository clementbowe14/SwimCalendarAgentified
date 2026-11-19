"""Strict, transport-injected Google Calendar FreeBusy protocol."""

from __future__ import annotations

import asyncio
import datetime
import json
import re
import secrets
from collections.abc import Iterator
from decimal import Decimal
from typing import Literal, NoReturn, Protocol, final

from app.external_calendar import (
    CalendarBusyFailureReason,
    CalendarBusyInterval,
    ExternalCalendarAuthorizationRequired,
    ExternalCalendarInvalidResponse,
    ExternalCalendarTimeout,
    ExternalCalendarUnavailable,
    MAX_BUSY_INTERVALS,
    MAX_BUSY_QUERY_WINDOW,
    normalize_busy_intervals,
)
from app.google_calendar_credential_handoff import (
    GoogleCalendarRefreshCredentialClaim,
    GoogleCalendarValidatedRefreshCredentialClaim,
    validate_google_calendar_refresh_credential_claim,
)
from app.google_calendar_token_exchange import (
    GoogleCalendarRefreshTokenExchangeResult,
    _refresh_exchange_result_integrity_matches,
)


__all__ = (
    "GOOGLE_CALENDAR_FREEBUSY_ENDPOINT",
    "GoogleCalendarFreeBusyQueryResult",
    "GoogleCalendarFreeBusyTransport",
    "GoogleCalendarFreeBusyTransportResponse",
    "query_google_calendar_freebusy",
)


GOOGLE_CALENDAR_FREEBUSY_ENDPOINT = (
    "https://www.googleapis.com/calendar/v3/freeBusy"
)
_FREEBUSY_TIMEOUT_SECONDS = 5.0
_MAX_RESPONSE_BODY_BYTES = 1024 * 1024
_MAX_ACCESS_TOKEN_BYTES = 16 * 1024
_MAX_IDENTIFIER_BYTES = 255
_MAX_CALENDAR_KEY_BYTES = 2 * 1024
_MAX_PROVIDER_ERROR_TEXT_BYTES = 512
_MAX_PROVIDER_ERRORS = 100
_MAX_EXPIRES_IN_SECONDS = 2_147_483_647
_POSTGRESQL_BIGINT_MAX = 9_223_372_036_854_775_807
_UTC = datetime.timezone.utc
_CONNECTION_ID_PATTERN = re.compile(r"calconn_[0-9a-f]{32}\Z")
_RFC3339_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)
_FAILURE_REASONS = frozenset(
    {
        "authorization_required",
        "timeout",
        "provider_unavailable",
        "invalid_response",
    }
)
_MAX_CONTROL_EXCEPTION_NODES = 4_096


class _SealedFreeBusyValue:
    """Immutable and non-serializable FreeBusy boundary value."""

    __slots__ = ()

    def __setattr__(self, _name: str, _value: object) -> None:
        del self, _name, _value
        _raise_immutable_error()

    def __delattr__(self, _name: str) -> None:
        del self, _name
        _raise_immutable_error()

    def __reduce__(self) -> NoReturn:
        del self
        _raise_serialization_error()

    def __reduce_ex__(self, _protocol: int) -> NoReturn:
        del self, _protocol
        _raise_serialization_error()

    def __getstate__(self) -> NoReturn:
        del self
        _raise_serialization_error()


@final
class GoogleCalendarFreeBusyTransportResponse(_SealedFreeBusyValue):
    """Bounded response candidate returned by a trusted transport."""

    __slots__ = ("status_code", "content_type", "body")

    status_code: int
    content_type: str | None
    body: bytes

    def __init_subclass__(cls, **_kwargs: object) -> NoReturn:
        del cls, _kwargs
        _raise_subclass_error()

    def __init__(
        self,
        *,
        status_code: int,
        content_type: str | None,
        body: bytes,
    ) -> None:
        object.__setattr__(self, "status_code", status_code)
        object.__setattr__(self, "content_type", content_type)
        object.__setattr__(self, "body", body)

    def __repr__(self) -> str:
        return "GoogleCalendarFreeBusyTransportResponse(<redacted>)"

    __str__ = __repr__


class GoogleCalendarFreeBusyTransport(Protocol):
    """Port for one fixed Google FreeBusy JSON request."""

    def post_json(
        self,
        *,
        url: str,
        access_token: str,
        body: bytes,
        timeout_seconds: float,
    ) -> GoogleCalendarFreeBusyTransportResponse: ...


@final
class GoogleCalendarFreeBusyQueryResult(_SealedFreeBusyValue):
    """Opaque periods bound to one credential generation and query window."""

    __slots__ = (
        "account_id",
        "instructor_id",
        "connection_id",
        "expected_calendar_connection_revision",
        "window_start",
        "window_end",
        "periods",
    )

    account_id: str
    instructor_id: str
    connection_id: str
    expected_calendar_connection_revision: int
    window_start: datetime.datetime
    window_end: datetime.datetime
    periods: tuple[CalendarBusyInterval, ...]
    __init__ = None

    def __init_subclass__(cls, **_kwargs: object) -> NoReturn:
        del cls, _kwargs
        _raise_subclass_error()

    def __repr__(self) -> str:
        return "GoogleCalendarFreeBusyQueryResult(<redacted>)"

    __str__ = __repr__


@final
class _ValidatedAccessResult(_SealedFreeBusyValue):
    __slots__ = (
        "account_id",
        "instructor_id",
        "connection_id",
        "expected_calendar_connection_revision",
        "access_token",
        "expires_in_seconds",
    )

    account_id: str
    instructor_id: str
    connection_id: str
    expected_calendar_connection_revision: int
    access_token: str
    expires_in_seconds: int
    __init__ = None

    def __repr__(self) -> str:
        return "_ValidatedAccessResult(<redacted>)"

    __str__ = __repr__


@final
class _PreparedFreeBusyQuery(_SealedFreeBusyValue):
    __slots__ = (
        "claim",
        "expected_claim",
        "access_result",
        "expected_access_result",
        "window_start",
        "window_end",
        "request_body",
    )

    claim: GoogleCalendarRefreshCredentialClaim
    expected_claim: GoogleCalendarValidatedRefreshCredentialClaim
    access_result: GoogleCalendarRefreshTokenExchangeResult
    expected_access_result: _ValidatedAccessResult
    window_start: datetime.datetime
    window_end: datetime.datetime
    request_body: bytes
    __init__ = None

    def __repr__(self) -> str:
        return "_PreparedFreeBusyQuery(<redacted>)"

    __str__ = __repr__


def query_google_calendar_freebusy(
    *,
    claim: GoogleCalendarRefreshCredentialClaim,
    access_result: GoogleCalendarRefreshTokenExchangeResult,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
    transport: GoogleCalendarFreeBusyTransport,
) -> GoogleCalendarFreeBusyQueryResult:
    """Query one fixed primary calendar without accepting provider details."""

    # Keyword values evaluate left-to-right. The final tuple clears public
    # references only after the guarded helper captures every argument.
    try: return _guarded_query_google_calendar_freebusy(claim=claim, access_result=access_result, window_start=window_start, window_end=window_end, transport=transport, _cleared=(claim := None, access_result := None, window_start := None, window_end := None, transport := None))
    except BaseException as boundary_raised:
        try: claim = access_result = window_start = window_end = transport = None; control_exception, system_exit_code, failure_reason = _classified_boundary_failure(boundary_raised); boundary_raised = None
        except BaseException as cleanup_raised: claim = access_result = window_start = window_end = transport = boundary_raised = None; control_exception, system_exit_code, failure_reason = _classified_boundary_failure(cleanup_raised)
    if control_exception is not None:
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    _raise_freebusy_failure(failure_reason)


def _guarded_query_google_calendar_freebusy(
    *,
    claim: object,
    access_result: object,
    window_start: object,
    window_end: object,
    transport: object,
    _cleared: object,
) -> GoogleCalendarFreeBusyQueryResult:
    del _cleared
    try:
        try: return _query_google_calendar_freebusy(claim=claim, access_result=access_result, window_start=window_start, window_end=window_end, transport=transport)
        except BaseException as failure:
            try: claim = access_result = window_start = window_end = transport = None; control_exception, system_exit_code, failure_reason = _classified_boundary_failure(failure); failure = None
            except BaseException as cleanup_raised: claim = access_result = window_start = window_end = transport = failure = None; control_exception, system_exit_code, failure_reason = _classified_boundary_failure(cleanup_raised)
    except BaseException as boundary_raised:
        try: claim = access_result = window_start = window_end = transport = None; control_exception, system_exit_code, failure_reason = _classified_boundary_failure(boundary_raised); boundary_raised = None
        except BaseException as cleanup_raised: claim = access_result = window_start = window_end = transport = boundary_raised = None; control_exception, system_exit_code, failure_reason = _classified_boundary_failure(cleanup_raised)
    if control_exception is not None:
        _raise_control_exception(
            control_exception,
            system_exit_code=system_exit_code,
        )
    _raise_freebusy_failure(failure_reason)


def _query_google_calendar_freebusy(
    *,
    claim: object,
    access_result: object,
    window_start: object,
    window_end: object,
    transport: object,
) -> GoogleCalendarFreeBusyQueryResult:
    try:
        prepared = _prepare_google_calendar_freebusy_query(
            claim=claim,
            access_result=access_result,
            window_start=window_start,
            window_end=window_end,
        )
        post_json = getattr(transport, "post_json", None)
        if not callable(post_json):
            raise ValueError("invalid FreeBusy transport")
    except Exception:
        raise ExternalCalendarInvalidResponse() from None

    try:
        response = post_json(
            url=GOOGLE_CALENDAR_FREEBUSY_ENDPOINT,
            access_token=prepared.expected_access_result.access_token,
            body=prepared.request_body,
            timeout_seconds=_FREEBUSY_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        raise ExternalCalendarTimeout() from None
    except Exception:
        raise ExternalCalendarUnavailable("provider_unavailable") from None

    try:
        return _complete_google_calendar_freebusy_query(
            response,
            prepared=prepared,
        )
    except ExternalCalendarUnavailable:
        raise
    except Exception:
        raise ExternalCalendarInvalidResponse() from None


def _prepare_google_calendar_freebusy_query(
    *,
    claim: object,
    access_result: object,
    window_start: object,
    window_end: object,
) -> _PreparedFreeBusyQuery:
    if type(claim) is not GoogleCalendarRefreshCredentialClaim:
        raise ValueError("invalid credential claim")
    expected_claim = validate_google_calendar_refresh_credential_claim(claim)
    expected_access_result = _validated_access_result(access_result)
    _require_matching_fence(expected_claim, expected_access_result)
    normalized_start, normalized_end = _validated_window(
        window_start,
        window_end,
    )
    request_body = json.dumps(
        {
            "timeMin": _canonical_rfc3339(normalized_start),
            "timeMax": _canonical_rfc3339(normalized_end),
            "timeZone": "UTC",
            "calendarExpansionMax": 1,
            "items": [{"id": "primary"}],
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return _new_prepared_query(
        claim=claim,
        expected_claim=expected_claim,
        access_result=access_result,
        expected_access_result=expected_access_result,
        window_start=normalized_start,
        window_end=normalized_end,
        request_body=request_body,
    )


def _complete_google_calendar_freebusy_query(
    response: object,
    *,
    prepared: object,
) -> GoogleCalendarFreeBusyQueryResult:
    if type(prepared) is not _PreparedFreeBusyQuery:
        raise ValueError("invalid prepared FreeBusy query")
    actual_claim = validate_google_calendar_refresh_credential_claim(
        prepared.claim
    )
    _require_same_claim(prepared.expected_claim, actual_claim)
    actual_access_result = _validated_access_result(prepared.access_result)
    _require_same_access_result(
        prepared.expected_access_result,
        actual_access_result,
    )
    _require_matching_fence(actual_claim, actual_access_result)
    payload = _validated_transport_payload(response)
    periods = _validated_busy_periods(
        payload,
        window_start=prepared.window_start,
        window_end=prepared.window_end,
    )
    return _new_query_result(
        account_id=actual_claim.account_id,
        instructor_id=actual_claim.instructor_id,
        connection_id=actual_claim.connection_id,
        expected_calendar_connection_revision=(
            actual_claim.expected_calendar_connection_revision
        ),
        window_start=prepared.window_start,
        window_end=prepared.window_end,
        periods=periods,
    )


def _validated_access_result(value: object) -> _ValidatedAccessResult:
    if type(value) is not GoogleCalendarRefreshTokenExchangeResult:
        raise ValueError("invalid access-token result")
    account_id = _validated_identifier(value.account_id)
    instructor_id = _validated_identifier(value.instructor_id)
    connection_id = _validated_connection_id(value.connection_id)
    revision = _validated_revision(
        value.expected_calendar_connection_revision
    )
    access_token = _visible_ascii(
        value.access_token,
        maximum_bytes=_MAX_ACCESS_TOKEN_BYTES,
    )
    expires_in_seconds = value.expires_in_seconds
    if (
        type(expires_in_seconds) is not int
        or not 1 <= expires_in_seconds <= _MAX_EXPIRES_IN_SECONDS
    ):
        raise ValueError("invalid access-token expiry")
    if not _refresh_exchange_result_integrity_matches(
        value,
        account_id=account_id,
        instructor_id=instructor_id,
        connection_id=connection_id,
        expected_revision=revision,
        access_token=access_token,
        expires_in_seconds=expires_in_seconds,
    ):
        raise ValueError("invalid access-token result integrity")
    result = object.__new__(_ValidatedAccessResult)
    object.__setattr__(result, "account_id", account_id)
    object.__setattr__(result, "instructor_id", instructor_id)
    object.__setattr__(result, "connection_id", connection_id)
    object.__setattr__(
        result,
        "expected_calendar_connection_revision",
        revision,
    )
    object.__setattr__(result, "access_token", access_token)
    object.__setattr__(result, "expires_in_seconds", expires_in_seconds)
    return result


def _validated_transport_payload(response: object) -> dict[str, object]:
    if type(response) is not GoogleCalendarFreeBusyTransportResponse:
        raise ValueError("invalid FreeBusy response")
    status_code = response.status_code
    if type(status_code) is not int:
        raise ValueError("invalid FreeBusy status")
    if status_code == 401:
        raise ExternalCalendarAuthorizationRequired()
    if status_code in (403, 408, 429) or 500 <= status_code <= 599:
        raise ExternalCalendarUnavailable("provider_unavailable")
    if status_code != 200:
        raise ValueError("unexpected FreeBusy status")
    if not _json_content_type(response.content_type):
        raise ValueError("invalid FreeBusy content type")
    body = response.body
    if (
        type(body) is not bytes
        or not 1 <= len(body) <= _MAX_RESPONSE_BODY_BYTES
    ):
        raise ValueError("invalid FreeBusy body")
    payload = json.loads(
        body.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_json_constant,
        parse_float=_finite_json_decimal,
    )
    if type(payload) is not dict:
        raise ValueError("invalid FreeBusy payload")
    return payload


def _validated_busy_periods(
    payload: dict[str, object],
    *,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
) -> tuple[CalendarBusyInterval, ...]:
    allowed_keys = {"kind", "timeMin", "timeMax", "calendars", "groups"}
    required_keys = {"kind", "timeMin", "timeMax", "calendars"}
    if (
        not required_keys.issubset(payload)
        or not set(payload).issubset(allowed_keys)
        or payload["kind"] != "calendar#freeBusy"
        or _parse_rfc3339(payload["timeMin"]) != window_start
        or _parse_rfc3339(payload["timeMax"]) != window_end
    ):
        raise ValueError("invalid FreeBusy envelope")
    groups = payload.get("groups", {})
    if type(groups) is not dict or groups:
        raise ValueError("unexpected FreeBusy groups")
    calendars = payload["calendars"]
    if type(calendars) is not dict or len(calendars) != 1:
        raise ValueError("invalid FreeBusy calendars")
    calendar_key, calendar_result = next(iter(calendars.items()))
    _bounded_text(calendar_key, maximum_bytes=_MAX_CALENDAR_KEY_BYTES)
    if type(calendar_result) is not dict:
        raise ValueError("invalid FreeBusy calendar result")
    if not set(calendar_result).issubset({"busy", "errors"}):
        raise ValueError("invalid FreeBusy calendar members")
    if "errors" in calendar_result and _validated_provider_errors(
        calendar_result["errors"]
    ):
        raise ExternalCalendarUnavailable("provider_unavailable")
    if "busy" not in calendar_result:
        raise ValueError("missing FreeBusy periods")
    busy = calendar_result["busy"]
    if type(busy) is not list or len(busy) > MAX_BUSY_INTERVALS:
        raise ValueError("invalid FreeBusy periods")
    periods: list[CalendarBusyInterval] = []
    for value in busy:
        if type(value) is not dict or set(value) != {"start", "end"}:
            raise ValueError("invalid FreeBusy period")
        periods.append(
            CalendarBusyInterval(
                start=_parse_rfc3339(value["start"]),
                end=_parse_rfc3339(value["end"]),
            )
        )
    return normalize_busy_intervals(
        periods,
        window_start=window_start,
        window_end=window_end,
    )


def _validated_provider_errors(value: object) -> bool:
    if type(value) is not list or len(value) > _MAX_PROVIDER_ERRORS:
        raise ValueError("invalid provider errors")
    for error in value:
        if type(error) is not dict or set(error) != {"domain", "reason"}:
            raise ValueError("invalid provider error")
        _bounded_text(
            error["domain"],
            maximum_bytes=_MAX_PROVIDER_ERROR_TEXT_BYTES,
        )
        _bounded_text(
            error["reason"],
            maximum_bytes=_MAX_PROVIDER_ERROR_TEXT_BYTES,
        )
    return bool(value)


def _validated_window(
    window_start: object,
    window_end: object,
) -> tuple[datetime.datetime, datetime.datetime]:
    start = _aware_utc(window_start)
    end = _aware_utc(window_end)
    if end <= start or end - start > MAX_BUSY_QUERY_WINDOW:
        raise ValueError("invalid FreeBusy window")
    return start, end


def _aware_utc(value: object) -> datetime.datetime:
    if (
        not isinstance(value, datetime.datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("invalid aware datetime")
    return value.astimezone(_UTC)


def _canonical_rfc3339(value: datetime.datetime) -> str:
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=timespec).replace("+00:00", "Z")


def _parse_rfc3339(value: object) -> datetime.datetime:
    if (
        type(value) is not str
        or len(value) > 64
        or _RFC3339_PATTERN.fullmatch(value) is None
    ):
        raise ValueError("invalid RFC3339 timestamp")
    try:
        parsed = datetime.datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        raise ValueError("invalid RFC3339 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid RFC3339 timestamp")
    return parsed.astimezone(_UTC)


def _require_matching_fence(
    claim: object,
    access_result: object,
) -> None:
    if (
        type(claim) is not GoogleCalendarValidatedRefreshCredentialClaim
        or type(access_result) is not _ValidatedAccessResult
        or claim.account_id != access_result.account_id
        or claim.instructor_id != access_result.instructor_id
        or claim.connection_id != access_result.connection_id
        or claim.expected_calendar_connection_revision
        != access_result.expected_calendar_connection_revision
    ):
        raise ValueError("credential fence mismatch")


def _require_same_claim(expected: object, actual: object) -> None:
    if (
        type(expected) is not GoogleCalendarValidatedRefreshCredentialClaim
        or type(actual) is not GoogleCalendarValidatedRefreshCredentialClaim
        or expected.account_id != actual.account_id
        or expected.instructor_id != actual.instructor_id
        or expected.connection_id != actual.connection_id
        or expected.expected_calendar_connection_revision
        != actual.expected_calendar_connection_revision
        or not secrets.compare_digest(
            expected.refresh_token,
            actual.refresh_token,
        )
    ):
        raise ValueError("credential claim changed")


def _require_same_access_result(expected: object, actual: object) -> None:
    if (
        type(expected) is not _ValidatedAccessResult
        or type(actual) is not _ValidatedAccessResult
        or expected.account_id != actual.account_id
        or expected.instructor_id != actual.instructor_id
        or expected.connection_id != actual.connection_id
        or expected.expected_calendar_connection_revision
        != actual.expected_calendar_connection_revision
        or expected.expires_in_seconds != actual.expires_in_seconds
        or not secrets.compare_digest(
            expected.access_token,
            actual.access_token,
        )
    ):
        raise ValueError("access-token result changed")


def _new_prepared_query(
    *,
    claim: GoogleCalendarRefreshCredentialClaim,
    expected_claim: GoogleCalendarValidatedRefreshCredentialClaim,
    access_result: GoogleCalendarRefreshTokenExchangeResult,
    expected_access_result: _ValidatedAccessResult,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
    request_body: bytes,
) -> _PreparedFreeBusyQuery:
    result = object.__new__(_PreparedFreeBusyQuery)
    object.__setattr__(result, "claim", claim)
    object.__setattr__(result, "expected_claim", expected_claim)
    object.__setattr__(result, "access_result", access_result)
    object.__setattr__(
        result,
        "expected_access_result",
        expected_access_result,
    )
    object.__setattr__(result, "window_start", window_start)
    object.__setattr__(result, "window_end", window_end)
    object.__setattr__(result, "request_body", request_body)
    return result


def _new_query_result(
    *,
    account_id: str,
    instructor_id: str,
    connection_id: str,
    expected_calendar_connection_revision: int,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
    periods: tuple[CalendarBusyInterval, ...],
) -> GoogleCalendarFreeBusyQueryResult:
    result = object.__new__(GoogleCalendarFreeBusyQueryResult)
    object.__setattr__(result, "account_id", account_id)
    object.__setattr__(result, "instructor_id", instructor_id)
    object.__setattr__(result, "connection_id", connection_id)
    object.__setattr__(
        result,
        "expected_calendar_connection_revision",
        expected_calendar_connection_revision,
    )
    object.__setattr__(result, "window_start", window_start)
    object.__setattr__(result, "window_end", window_end)
    object.__setattr__(result, "periods", periods)
    return result


def _validated_identifier(value: object) -> str:
    return _visible_ascii(value, maximum_bytes=_MAX_IDENTIFIER_BYTES)


def _validated_connection_id(value: object) -> str:
    if (
        type(value) is not str
        or _CONNECTION_ID_PATTERN.fullmatch(value) is None
    ):
        raise ValueError("invalid connection ID")
    return value


def _validated_revision(value: object) -> int:
    if (
        type(value) is not int
        or not 1 <= value <= _POSTGRESQL_BIGINT_MAX
    ):
        raise ValueError("invalid connection revision")
    return value


def _visible_ascii(value: object, *, maximum_bytes: int) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("invalid visible text")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("invalid visible text") from None
    if len(encoded) > maximum_bytes or any(
        byte < 0x21 or byte > 0x7E for byte in encoded
    ):
        raise ValueError("invalid visible text")
    return value


def _bounded_text(value: object, *, maximum_bytes: int) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or not value.isprintable()
    ):
        raise ValueError("invalid bounded text")
    encoded = value.encode("utf-8")
    if len(encoded) > maximum_bytes:
        raise ValueError("invalid bounded text")
    return value


def _json_content_type(value: object) -> bool:
    if type(value) is not str or not value or len(value) > 256:
        return False
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    if any((byte < 0x20 and byte != 0x09) or byte == 0x7F for byte in encoded):
        return False
    parts = tuple(part.strip() for part in value.split(";"))
    if not parts or parts[0].lower() != "application/json":
        return False
    parameters = tuple(part.lower() for part in parts[1:])
    return parameters in ((), ("charset=utf-8",), ('charset="utf-8"',))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("invalid JSON constant")


def _finite_json_decimal(value: str) -> Decimal:
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise ValueError("invalid JSON number")
    return parsed


def _classified_boundary_failure(
    raised: BaseException,
) -> tuple[str | None, int | None, CalendarBusyFailureReason]:
    control_kind, system_exit_code = _classified_control_exception(raised)
    if control_kind is not None:
        return control_kind, system_exit_code, "provider_unavailable"
    if type(raised) is ExternalCalendarAuthorizationRequired:
        return None, None, "authorization_required"
    if type(raised) is ExternalCalendarTimeout:
        return None, None, "timeout"
    if type(raised) is ExternalCalendarInvalidResponse:
        return None, None, "invalid_response"
    if type(raised) is ExternalCalendarUnavailable:
        reason = raised.reason_code
        if reason in _FAILURE_REASONS:
            return None, None, reason
    return None, None, "provider_unavailable"


def _classified_control_exception(
    raised: BaseException,
) -> tuple[str | None, int | None]:
    nodes, overflowed = _bounded_unique_exception_graph(raised)
    for current in nodes:
        if isinstance(current, asyncio.CancelledError):
            return "cancelled", None
        if isinstance(current, KeyboardInterrupt):
            return "keyboard_interrupt", None
        if isinstance(current, SystemExit):
            if type(current) is not SystemExit:
                return "system_exit", 1
            code = current.code
            return (
                "system_exit",
                code if type(code) is int or code is None else 1,
            )
        if isinstance(current, GeneratorExit):
            return "generator_exit", None
    if overflowed:
        return "cancelled", None
    return None, None


def _bounded_unique_exception_graph(
    raised: BaseException,
) -> tuple[tuple[BaseException, ...], bool]:
    pending = [raised]
    seen: set[int] = set()
    nodes: list[BaseException] = []
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        if len(seen) >= _MAX_CONTROL_EXCEPTION_NODES:
            return tuple(nodes), True
        seen.add(identity)
        nodes.append(current)
        if isinstance(current, BaseExceptionGroup):
            nested = BaseExceptionGroup.exceptions.__get__(
                current,
                BaseExceptionGroup,
            )
            pending.extend(reversed(nested))
    return tuple(nodes), False


def _iter_unique_exception_graph(
    raised: BaseException,
) -> Iterator[BaseException]:
    nodes, _overflowed = _bounded_unique_exception_graph(raised)
    yield from nodes


def _raise_freebusy_failure(reason: CalendarBusyFailureReason) -> NoReturn:
    if reason == "authorization_required":
        error: ExternalCalendarUnavailable = (
            ExternalCalendarAuthorizationRequired()
        )
    elif reason == "timeout":
        error = ExternalCalendarTimeout()
    elif reason == "invalid_response":
        error = ExternalCalendarInvalidResponse()
    else:
        error = ExternalCalendarUnavailable("provider_unavailable")
    try:
        raise error from None
    except ExternalCalendarUnavailable as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_control_exception(
    kind: str,
    *,
    system_exit_code: int | None,
) -> NoReturn:
    if kind == "cancelled":
        error: BaseException = asyncio.CancelledError()
    elif kind == "keyboard_interrupt":
        error = KeyboardInterrupt()
    elif kind == "system_exit":
        error = SystemExit(system_exit_code)
    elif kind == "generator_exit":
        error = GeneratorExit()
    else:
        _raise_freebusy_failure("provider_unavailable")
    try:
        raise error from None
    except BaseException as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_immutable_error() -> NoReturn:
    error = AttributeError("Google Calendar FreeBusy values are immutable.")
    try:
        raise error from None
    except AttributeError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_serialization_error() -> NoReturn:
    error = TypeError("Google Calendar FreeBusy values cannot be serialized.")
    try:
        raise error from None
    except TypeError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise


def _raise_subclass_error() -> NoReturn:
    error = TypeError("Google Calendar FreeBusy values cannot be subclassed.")
    try:
        raise error from None
    except TypeError as raised:
        raised.__cause__ = None
        raised.__context__ = None
        raised.__suppress_context__ = True
        raise
