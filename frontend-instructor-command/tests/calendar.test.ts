import assert from "node:assert/strict";
import test from "node:test";
import { trustedGoogleCalendarUrl, trustedInstructorCalendarUrl } from "../src/api/calendar.js";
import type { CalendarLinks } from "../src/api/types.js";
import {
  renderCalendarHandoffModal,
  type CalendarHandoffModalModel,
} from "../src/components/CalendarHandoffModal.js";

const apiBase = "https://api.example/api";
const documentUrl = "https://command.example/find";

test("calendar URL validation accepts only Google and the configured coach ICS origin", () => {
  assert.equal(
    trustedGoogleCalendarUrl("https://calendar.google.com/calendar/render?action=TEMPLATE"),
    "https://calendar.google.com/calendar/render?action=TEMPLATE",
  );
  assert.equal(
    trustedInstructorCalendarUrl("/api/coach/appointments/app_123/calendar.ics", apiBase, documentUrl),
    "https://api.example/api/coach/appointments/app_123/calendar.ics",
  );

  for (const value of [
    "http://calendar.google.com/calendar/render",
    "https://calendar.google.com.evil/calendar/render",
    "https://calendar.google.com:444/calendar/render",
    "https://calendar.google.com/other",
    "javascript:alert(1)",
  ]) {
    assert.throws(() => trustedGoogleCalendarUrl(value));
  }
  for (const value of [
    "https://evil.example/api/coach/appointments/app_123/calendar.ics",
    "https://api.example/api/public/calendar/token.ics",
    "/api/coach/appointments/app_123/calendar.ics?secret=1",
  ]) {
    assert.throws(() => trustedInstructorCalendarUrl(value, apiBase, documentUrl));
  }
});

test("ready modal renders recurring persisted occurrences with truthful accessible handoffs", () => {
  const html = renderCalendarHandoffModal(readyModel(calendarLinks()), apiBase, documentUrl);

  assert.match(html, /role="dialog"/);
  assert.match(html, /aria-modal="true"/);
  assert.match(html, /Google Calendar occurrences/);
  assert.equal((html.match(/calendar-action-google/g) ?? []).length, 2);
  assert.match(html, /data-calendar-ics="https:\/\/api\.example\/api\/coach\/appointments\/app_123\/calendar\.ics"/);
  assert.match(html, /does not add the appointment automatically/);
  assert.doesNotMatch(html, /added to (?:your|the) calendar/i);
});

test("unsafe and unavailable handoffs never render active calendar actions", () => {
  const unsafe = calendarLinks();
  unsafe.google = "https://evil.example/calendar";
  unsafe.google_events = [{ ...unsafe.google_events[0], url: unsafe.google }];
  unsafe.ics = "https://evil.example/calendar.ics";
  const unsafeHtml = renderCalendarHandoffModal(readyModel(unsafe), apiBase, documentUrl);
  const unavailableHtml = renderCalendarHandoffModal(
    { ...readyModel(null), status: "unavailable" },
    apiBase,
    documentUrl,
  );

  assert.doesNotMatch(unsafeHtml, /href=/);
  assert.doesNotMatch(unsafeHtml, /data-calendar-ics=/);
  assert.match(unsafeHtml, /role="alert"/);
  assert.doesNotMatch(unavailableHtml, /calendar-action-google|data-calendar-ics/);
  assert.match(unavailableHtml, /appointment is saved/i);
});

function readyModel(links: CalendarLinks | null): CalendarHandoffModalModel {
  return {
    status: "ready",
    appointmentId: "app_123",
    links,
    returnFocusKey: "slot:one",
    returnFocusFallback: "finder",
    downloadStatus: "idle",
    inlineError: "",
  };
}

function calendarLinks(): CalendarLinks {
  const google = "https://calendar.google.com/calendar/render?action=TEMPLATE";
  return {
    google,
    google_events: [
      { start_iso: "2026-08-29T17:30:00Z", end_iso: "2026-08-29T18:15:00Z", url: `${google}&dates=one` },
      { start_iso: "2026-09-05T17:30:00Z", end_iso: "2026-09-05T18:15:00Z", url: `${google}&dates=two` },
    ],
    apple: "/api/coach/appointments/app_123/calendar.ics",
    ics: "/api/coach/appointments/app_123/calendar.ics",
  };
}
