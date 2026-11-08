"""Calendar snapshots built exclusively from persisted appointment instants."""

from __future__ import annotations

from urllib.parse import quote, urlencode

from icalendar import Calendar, Event

from app import timezones
from app.api_errors import CalendarLinksUnavailable
from app.api_schemas import ClientInviteCalendarLinks, GoogleCalendarEvent
from app.models import AppointmentDB


TITLE = "Swim lesson"
DESCRIPTION = "Confirmed swim lesson."


def _calendar_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in normalized):
        raise CalendarLinksUnavailable()
    return normalized


def _instants(appointment: AppointmentDB):
    start = timezones.storage_to_aware_utc(appointment.start_time)
    end = timezones.storage_to_aware_utc(appointment.end_time)
    if end <= start:
        raise CalendarLinksUnavailable()
    return start, end


def calendar_links(
    calendar_key: str,
    appointments: list[AppointmentDB],
    *,
    download_url: str | None = None,
    title: str = TITLE,
    description: str = DESCRIPTION,
) -> ClientInviteCalendarLinks:
    if not appointments:
        raise CalendarLinksUnavailable()
    safe_title = _calendar_text(title)
    safe_description = _calendar_text(description)
    events = []
    for appointment in sorted(appointments, key=lambda item: _instants(item)[0]):
        start, end = _instants(appointment)
        query = urlencode({
            "action": "TEMPLATE",
            "text": safe_title,
            "dates": f"{timezones.calendar_utc_timestamp(start)}/{timezones.calendar_utc_timestamp(end)}",
            "location": _calendar_text(appointment.location_address),
            "details": safe_description,
        })
        events.append(GoogleCalendarEvent(
            start_iso=start, end_iso=end,
            url=f"https://calendar.google.com/calendar/render?{query}",
        ))
    download = download_url or f"/api/public/calendar/{quote(calendar_key, safe='')}.ics"
    return ClientInviteCalendarLinks(
        google=events[0].url, google_events=events, apple=download, ics=download,
    )


def calendar_ics(
    appointments: list[AppointmentDB],
    *,
    title: str = TITLE,
    description: str = DESCRIPTION,
) -> str:
    if not appointments:
        raise CalendarLinksUnavailable()
    safe_title = _calendar_text(title)
    safe_description = _calendar_text(description)
    calendar = Calendar()
    calendar.add("prodid", "-//Swim Command//Client Invite//EN")
    calendar.add("version", "2.0")
    for appointment in sorted(appointments, key=lambda item: _instants(item)[0]):
        start, end = _instants(appointment)
        event = Event()
        event.add("uid", appointment.calendar_uid)
        event.add("dtstamp", timezones.storage_to_aware_utc(appointment.created_at))
        event.add("dtstart", start)
        event.add("dtend", end)
        event.add("summary", safe_title)
        event.add("location", _calendar_text(appointment.location_address))
        event.add("description", safe_description)
        calendar.add_component(event)
    return calendar.to_ical().decode("utf-8")
