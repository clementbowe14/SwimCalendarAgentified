from __future__ import annotations

import datetime
import math
from dataclasses import dataclass
from typing import Iterable, Optional

from app import db, timezones
from app.models import InstructorDB, ProviderWorkingHoursDB


WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
WEEKDAY_INDEX = {name: index for index, name in enumerate(WEEKDAY_NAMES)}


class ProviderNotFound(Exception):
    pass


class ProviderSetupIncomplete(Exception):
    def __init__(self, missing_fields: Iterable[str]):
        self.missing_fields = tuple(missing_fields)
        super().__init__("Provider setup is incomplete.")


@dataclass(frozen=True)
class ProviderLocation:
    address: str
    lat: float
    lng: float


@dataclass(frozen=True)
class ProviderService:
    name: str
    default_duration_mins: int
    location: ProviderLocation


@dataclass(frozen=True)
class ProviderWorkingHours:
    weekday: str
    enabled: bool
    start_time: Optional[str]
    end_time: Optional[str]


@dataclass(frozen=True)
class ProviderConfiguration:
    id: str
    display_name: str
    business_name: Optional[str]
    timezone: str
    base_location: Optional[ProviderLocation]
    service_location: Optional[ProviderLocation]
    service_name: Optional[str]
    default_duration_mins: Optional[int]
    working_hours: tuple[ProviderWorkingHours, ...]
    setup_complete: bool
    setup_missing_fields: tuple[str, ...]

    @property
    def presentation_business_name(self) -> str:
        return self.business_name or self.display_name

    @property
    def service(self) -> Optional[ProviderService]:
        if (
            self.service_location is None
            or self.service_name is None
            or self.default_duration_mins is None
        ):
            return None
        return ProviderService(
            name=self.service_name,
            default_duration_mins=self.default_duration_mins,
            location=self.service_location,
        )


def load_provider_configuration(instructor_id: str) -> ProviderConfiguration:
    session = db.SessionLocal()
    try:
        instructor = session.get(InstructorDB, instructor_id)
        if instructor is None:
            raise ProviderNotFound()
        hour_rows = (
            session.query(ProviderWorkingHoursDB)
            .filter_by(instructor_id=instructor_id)
            .order_by(ProviderWorkingHoursDB.weekday.asc())
            .all()
        )
        return _configuration_from_rows(instructor, hour_rows)
    finally:
        session.close()


def require_provider_setup(instructor_id: str) -> ProviderConfiguration:
    configuration = load_provider_configuration(instructor_id)
    if not configuration.setup_complete:
        raise ProviderSetupIncomplete(configuration.setup_missing_fields)
    return configuration


def working_interval_for_date(
    configuration: ProviderConfiguration,
    requested_date: datetime.date,
) -> Optional[tuple[str, str]]:
    weekday = WEEKDAY_NAMES[requested_date.weekday()]
    row = next(
        (candidate for candidate in configuration.working_hours if candidate.weekday == weekday),
        None,
    )
    if row is None or not row.enabled or row.start_time is None or row.end_time is None:
        return None
    return row.start_time, row.end_time


def save_provider_configuration(
    instructor_id: str,
    *,
    display_name: str,
    business_name: Optional[str],
    timezone_name: str,
    base_address: str,
    base_coords: tuple[float, float],
    service_address: str,
    service_coords: tuple[float, float],
    service_name: str,
    default_duration_mins: int,
    working_hours: Iterable[object],
) -> ProviderConfiguration:
    base_lat, base_lng = _validated_coords(base_coords)
    service_lat, service_lng = _validated_coords(service_coords)
    normalized_hours = tuple(working_hours)
    session = db.SessionLocal()
    try:
        with session.begin():
            instructor = (
                session.query(InstructorDB)
                .filter_by(id=instructor_id)
                .with_for_update()
                .one_or_none()
            )
            if instructor is None:
                raise ProviderNotFound()

            instructor.name = display_name
            instructor.business_name = business_name
            instructor.timezone = timezone_name
            instructor.base_address = base_address
            instructor.base_lat = base_lat
            instructor.base_lng = base_lng
            instructor.service_address = service_address
            instructor.service_lat = service_lat
            instructor.service_lng = service_lng
            instructor.service_name = service_name
            instructor.default_duration_mins = default_duration_mins

            # Compatibility mirrors for readers that predate issue #55.
            instructor.main_address = service_address
            instructor.lat = service_lat
            instructor.lng = service_lng

            (
                session.query(ProviderWorkingHoursDB)
                .filter_by(instructor_id=instructor_id)
                .delete(synchronize_session=False)
            )
            now = datetime.datetime.utcnow()
            first_enabled: Optional[object] = None
            for hours in normalized_hours:
                enabled = bool(hours.enabled)
                start_time = _parse_time(hours.start_time) if enabled else None
                end_time = _parse_time(hours.end_time) if enabled else None
                row = ProviderWorkingHoursDB(
                    instructor_id=instructor_id,
                    weekday=WEEKDAY_INDEX[hours.weekday],
                    enabled=enabled,
                    start_time=start_time,
                    end_time=end_time,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                if first_enabled is None and enabled:
                    first_enabled = hours

            instructor.work_start_time = first_enabled.start_time if first_enabled else None
            instructor.work_end_time = first_enabled.end_time if first_enabled else None

        return load_provider_configuration(instructor_id)
    finally:
        session.close()


def service_location_matches(
    configuration: ProviderConfiguration,
    *,
    address: str,
    lat: float,
    lng: float,
) -> bool:
    if configuration.service_location is None:
        return False
    location = configuration.service_location
    return (
        address.strip() == location.address
        and math.isclose(lat, location.lat, abs_tol=0.000001)
        and math.isclose(lng, location.lng, abs_tol=0.000001)
    )


def _configuration_from_rows(
    instructor: InstructorDB,
    hour_rows: list[ProviderWorkingHoursDB],
) -> ProviderConfiguration:
    base_location = _location(
        instructor.base_address,
        instructor.base_lat,
        instructor.base_lng,
    )
    service_location = _location(
        instructor.service_address,
        instructor.service_lat,
        instructor.service_lng,
    )
    hours = tuple(
        ProviderWorkingHours(
            weekday=WEEKDAY_NAMES[row.weekday],
            enabled=bool(row.enabled),
            start_time=_format_time(row.start_time),
            end_time=_format_time(row.end_time),
        )
        for row in hour_rows
        if 0 <= row.weekday < len(WEEKDAY_NAMES)
    )
    missing: list[str] = []
    if not _nonblank(instructor.name):
        missing.append("display_name")
    try:
        if not _nonblank(instructor.timezone):
            raise timezones.InvalidTimezoneError("Timezone is blank.")
        timezones.get_zone(instructor.timezone)
    except timezones.InvalidTimezoneError:
        missing.append("timezone")
    if base_location is None:
        missing.append("base_address")
    if service_location is None:
        missing.append("service_address")
    if not _nonblank(instructor.service_name):
        missing.append("service_name")
    if not _valid_duration(instructor.default_duration_mins):
        missing.append("default_duration_mins")
    if not _complete_week(hours):
        missing.append("working_hours")

    return ProviderConfiguration(
        id=instructor.id,
        display_name=instructor.name.strip() if instructor.name else "",
        business_name=(instructor.business_name.strip() if _nonblank(instructor.business_name) else None),
        timezone=instructor.timezone,
        base_location=base_location,
        service_location=service_location,
        service_name=(instructor.service_name.strip() if _nonblank(instructor.service_name) else None),
        default_duration_mins=(
            int(instructor.default_duration_mins)
            if _valid_duration(instructor.default_duration_mins)
            else None
        ),
        working_hours=hours,
        setup_complete=not missing,
        setup_missing_fields=tuple(missing),
    )


def _complete_week(hours: tuple[ProviderWorkingHours, ...]) -> bool:
    if tuple(row.weekday for row in hours) != WEEKDAY_NAMES:
        return False
    for row in hours:
        if row.enabled:
            if row.start_time is None or row.end_time is None or row.end_time <= row.start_time:
                return False
        elif row.start_time is not None or row.end_time is not None:
            return False
    return True


def _location(
    address: Optional[str],
    lat: Optional[float],
    lng: Optional[float],
) -> Optional[ProviderLocation]:
    if not _nonblank(address) or lat is None or lng is None:
        return None
    try:
        latitude, longitude = _validated_coords((lat, lng))
    except ValueError:
        return None
    return ProviderLocation(address=address.strip(), lat=latitude, lng=longitude)


def _validated_coords(coords: tuple[float, float]) -> tuple[float, float]:
    lat, lng = float(coords[0]), float(coords[1])
    if not math.isfinite(lat) or not math.isfinite(lng):
        raise ValueError("Location coordinates must be finite.")
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        raise ValueError("Location coordinates are outside geographic bounds.")
    return lat, lng


def _valid_duration(value: Optional[int]) -> bool:
    return value is not None and 15 <= value <= 240 and value % 15 == 0


def _nonblank(value: Optional[str]) -> bool:
    return bool(value and value.strip())


def _parse_time(value: Optional[str]) -> datetime.time:
    if value is None:
        raise ValueError("Enabled working hours require a time.")
    return datetime.datetime.strptime(value, "%H:%M").time()


def _format_time(value: Optional[datetime.time]) -> Optional[str]:
    return value.strftime("%H:%M") if value is not None else None
