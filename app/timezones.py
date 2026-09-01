import datetime
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_INSTRUCTOR_TIMEZONE = "America/Chicago"
UTC = datetime.timezone.utc


class InvalidTimezoneError(ValueError):
    pass


class AmbiguousLocalTimeError(ValueError):
    pass


class NonexistentLocalTimeError(ValueError):
    pass


def get_zone(timezone_name: Optional[str]) -> ZoneInfo:
    name = timezone_name or DEFAULT_INSTRUCTOR_TIMEZONE
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise InvalidTimezoneError(f"Invalid IANA timezone: {name}") from exc


def ensure_aware_utc(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_utc_storage(value: datetime.datetime) -> datetime.datetime:
    return ensure_aware_utc(value).replace(tzinfo=None)


def storage_to_aware_utc(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC)
    return value.replace(tzinfo=UTC)


def storage_to_local(value: datetime.datetime, timezone_name: Optional[str]) -> datetime.datetime:
    return storage_to_aware_utc(value).astimezone(get_zone(timezone_name))


def aware_to_local(value: datetime.datetime, timezone_name: Optional[str]) -> datetime.datetime:
    return ensure_aware_utc(value).astimezone(get_zone(timezone_name))


def parse_local_date_time(
    local_date: datetime.date,
    time_value: str,
    timezone_name: Optional[str],
) -> datetime.datetime:
    hour, minute = map(int, time_value.split(":"))
    return validate_local_datetime(
        datetime.datetime.combine(local_date, datetime.time(hour, minute)),
        timezone_name,
    )


def validate_local_datetime(
    value: datetime.datetime,
    timezone_name: Optional[str],
) -> datetime.datetime:
    zone = get_zone(timezone_name)
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(zone)

    fold0 = value.replace(tzinfo=zone, fold=0)
    fold1 = value.replace(tzinfo=zone, fold=1)
    roundtrip0 = fold0.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    roundtrip1 = fold1.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    valid0 = roundtrip0 == value
    valid1 = roundtrip1 == value

    if not valid0 and not valid1:
        raise NonexistentLocalTimeError(f"Nonexistent local time: {value.isoformat()} in {zone.key}")
    if valid0 and valid1 and fold0.utcoffset() != fold1.utcoffset():
        raise AmbiguousLocalTimeError(f"Ambiguous local time: {value.isoformat()} in {zone.key}")
    return fold0 if valid0 else fold1


def local_day_window_utc_storage(
    local_date: datetime.date,
    timezone_name: Optional[str],
) -> tuple[datetime.datetime, datetime.datetime]:
    zone = get_zone(timezone_name)
    local_start = datetime.datetime.combine(local_date, datetime.time.min, tzinfo=zone)
    local_end = local_start + datetime.timedelta(days=1)
    return to_utc_storage(local_start), to_utc_storage(local_end)


def requested_local_date(
    value: datetime.datetime,
    timezone_name: Optional[str],
) -> datetime.date:
    if value.tzinfo is None or value.utcoffset() is None:
        return validate_local_datetime(value, timezone_name).date()
    return value.astimezone(get_zone(timezone_name)).date()


def canonical_instant(value: datetime.datetime) -> datetime.datetime:
    return ensure_aware_utc(value).replace(microsecond=0)


def canonical_instant_text(value: datetime.datetime) -> str:
    return canonical_instant(value).isoformat().replace("+00:00", "Z")


def calendar_utc_timestamp(value: datetime.datetime) -> str:
    return canonical_instant(value).strftime("%Y%m%dT%H%M%SZ")
