import { expect, test, type Page, type Request, type Route } from "@playwright/test";
import { providerConfiguration, snapshot } from "../src/api/mockData";
import type { BlockerScheduleItem, CoachAgenda, LessonScheduleItem, ProviderBlocker, ScheduleItem } from "../src/api/types";
import { agendaResponse } from "./agenda-fixture";

const day = "2026-08-27";
const providerId = "coach/É +1";
const timeZone = "America/Chicago";
const cookie = "swim_session=agenda-session";
const address = "100 Main St, Austin, TX";
const lessonLocation = { address, lat: 30, lng: -97, is_instructor_facility: true };

test.use({ timezoneId: "Asia/Tokyo" });

test("renders a populated day using the session owner and explicit zero travel", async ({ page }) => {
  const lesson = appointment("primary", "Morning lesson", "14:00", "14:45");
  const api = await mockApi(page, (date) => ({ json: agenda(date, {
    items: [lesson],
    next_up: { appointment_id: lesson.id, leave_by: lesson.start_time, travel_time_mins: 0, buffer_mins: 0 },
  }) }));
  await page.clock.setFixedTime(new Date(`${day}T13:00:00Z`));
  await page.goto(`/?date=${day}`);

  await expect(page.locator("[data-agenda-row]")).toHaveCount(1);
  await expect(page.locator("[data-agenda-row]")).toContainText("9:00 AM-9:45 AM");
  const next = page.getByRole("region", { name: "Next appointment" });
  await expect(next.getByRole("heading")).toHaveText("No travel needed");
  await expect(next).toContainText("0 min travel");
  await expect(next).toContainText("0 min recovery");
  expect(await api.agendaRequests[0].headerValue("x-expected-instructor-id")).toBe(encodeURIComponent(providerId));
  expect(await api.agendaRequests[0].headerValue("cookie")).toContain(cookie);
  expect(new URL(api.agendaRequests[0].url()).searchParams.get("date")).toBe(day);
});

test("shows an empty day with a working add-event action on mobile", async ({ page }) => {
  let added = false;
  const blocker: BlockerScheduleItem = {
    ...interval("blocker-1", "Lunch break", "17:00", "17:30"),
    kind: "break", revision: 1, location: null,
  };
  const api = await mockApi(page, (date) => ({ json: agenda(date, { items: added ? [blocker] : [] }) }), {
    mutateBlocker: async (route) => {
      expect(route.request().method()).toBe("POST");
      expect(route.request().postDataJSON()).toMatchObject({ title: blocker.title, date: day });
      added = true;
      await route.fulfill({ status: 201, json: savedBlocker(blocker, day, "12:00", "12:30") });
    },
  });
  await page.setViewportSize({ width: 320, height: 680 });
  await page.goto(`/?date=${day}`);

  await expect(page.getByRole("heading", { name: "Nothing on the route yet" })).toBeVisible();
  await expect(page.locator("[data-agenda-row]")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320);
  await page.getByRole("button", { name: "Add event from empty schedule" }).click();
  const editor = page.getByRole("dialog", { name: "Add schedule event" });
  await expect(editor.getByLabel("Date")).toHaveValue(day);
  await editor.getByLabel("Title").fill(blocker.title);
  await editor.getByRole("button", { name: "Add to schedule" }).click();
  await expect(page.getByRole("button", { name: "Edit Lunch break" })).toBeVisible();
  expect(api.blockerRequests.map((request) => request.method())).toEqual(["POST"]);
});

test("shows ordered multi-location rows and exact bookable starts with the API departure", async ({ page }) => {
  const first = appointment("first", "East pool lesson", "13:30", "14:05");
  const next = appointment("next", "West pool lesson", "15:10", "16:00", "500 West Ave, Austin, TX");
  const items: ScheduleItem[] = [
    first,
    {
      ...interval("starts", "Open gap", "14:05", "14:35"),
      kind: "open_gap", location: null, start_times: [`${day}T14:05:00Z`, `${day}T14:20:00Z`],
      note: "45 min appointments at the service location",
    },
    { ...interval("travel", "Drive to West pool", "14:35", "14:55"), kind: "travel", location: null, travel_time_mins: 20 },
    { ...interval("buffer", "Recovery", "14:55", "15:10"), kind: "buffer", location: null, buffer_mins: 15 },
    next,
  ];
  await mockApi(page, (date) => ({ json: agenda(date, {
    items,
    next_up: { appointment_id: next.id, leave_by: `${day}T14:30:00Z`, travel_time_mins: 25, buffer_mins: 15 },
  }) }));
  await page.clock.setFixedTime(new Date(`${day}T14:25:00Z`));
  await page.goto(`/?date=${day}`);

  await expect(page.locator(".route-content-copy strong")).toHaveText([
    "East pool lesson", "Bookable starts", "Drive to West pool", "Recovery", "West pool lesson",
  ]);
  await expect(page.locator(".agenda-start-times")).toHaveText("9:05 AM · 9:20 AM");
  await expect(page.locator('[data-agenda-row="starts"]')).toContainText("45 min appointments at the service location");
  await expect(page.locator('[data-agenda-row="starts"]')).not.toContainText("9:35 AM");
  const card = page.getByRole("region", { name: "Next appointment" });
  await expect(card.getByRole("heading")).toHaveText("Leave by 9:30 AM");
  await expect(card).toContainText("West pool lesson");
  await expect(card).toContainText("25 min travel");
  await expect(card).toContainText("15 min recovery");
  await expect(card.getByRole("link", { name: "Navigate" })).toHaveAttribute("href", /500%20West%20Ave/);

  await page.clock.setFixedTime(new Date(`${day}T14:45:00Z`));
  await page.getByRole("button", { name: "Schedule", exact: true }).click();
  await expect(card.getByRole("heading")).toHaveText("Leave-by time passed (9:30 AM)");
  await page.clock.setFixedTime(new Date(`${day}T15:20:00Z`));
  await page.getByRole("button", { name: "Schedule", exact: true }).click();
  await expect(page.getByRole("region", { name: "Current lesson" })).toContainText("In progress");
});

test("edits an agenda-only blocker in provider time and refreshes after save", async ({ page }) => {
  await simulateUnavailableTimezone(page, "America/Denver");
  let blocker = {
    id: "blocker-1",
    kind: "personal" as const,
    title: "School meeting",
    start_time: "2026-08-28T02:15:00Z",
    end_time: "2026-08-28T02:45:00Z",
    revision: 2,
    location: lessonLocation,
  };
  const api = await mockApi(page, (date) => ({ json: agenda(date, { items: [blocker] }) }), {
    blockers: () => [savedBlocker(blocker, day, "20:15", "20:45", "America/Denver")],
    failSnapshot: () => blocker.revision === 3,
    mutateBlocker: async (route) => {
      expect(route.request().method()).toBe("PUT");
      expect(route.request().postDataJSON()).toMatchObject({
        title: "School pickup", date: day, start_time: "20:15", end_time: "20:45",
        expected_revision: 2, expected_timezone: "America/Denver",
      });
      blocker = { ...blocker, title: "School pickup", revision: 3 };
      await route.fulfill({ json: savedBlocker(blocker, day, "20:15", "20:45", "America/Denver") });
    },
  });
  await page.goto(`/?date=${day}`);

  await page.getByRole("button", { name: "Edit School meeting" }).click();
  const editor = page.getByRole("dialog", { name: "Edit School meeting" });
  await expect(editor.getByLabel("Date")).toHaveValue(day);
  await expect(editor.getByLabel("Starts")).toHaveValue("20:15");
  await expect(editor.getByLabel("Ends")).toHaveValue("20:45");
  await editor.getByLabel("Title").fill("School pickup");
  await page.getByRole("button", { name: "Save changes" }).click();

  await expect(page.getByRole("button", { name: "Edit School pickup" })).toBeFocused();
  await expect(page.getByRole("button", { name: "Edit School meeting" })).toHaveCount(0);
  expect(api.agendaRequests).toHaveLength(2);
  expect(api.blockerRequests.map((request) => request.method())).toEqual(["GET", "PUT"]);
  expect(new URL(api.blockerRequests[0].url()).searchParams.get("date")).toBe(day);
});

test("prevents a title-only edit of a blocker clipped at local midnight", async ({ page }) => {
  const blocker: BlockerScheduleItem = {
    ...interval("blocker-1", "Overnight pool maintenance", "04:30", "05:30"),
    kind: "unavailable", location: null, revision: 2,
  };
  const clipped = { ...blocker, start_time: `${day}T05:00:00Z` };
  const api = await mockApi(page, (date) => ({ json: agenda(date, { items: [clipped] }) }), {
    blockers: () => [savedBlocker(blocker, "2026-08-26", "23:30", "00:30")],
  });
  await page.goto(`/?date=${day}`);

  await expect(page.locator('[data-agenda-row="blocker-1"]')).toContainText("12:00 AM-12:30 AM");
  await page.getByRole("button", { name: "Edit Overnight pool maintenance" }).click();
  const editor = page.getByRole("dialog", { name: "Edit Overnight pool maintenance" });
  await expect(editor).toContainText("This event spans two dates");
  await expect(editor.getByLabel("Date")).toHaveValue("2026-08-26");
  await expect(editor.getByLabel("Starts")).toHaveValue("23:30");
  await expect(editor.getByLabel("Ends")).toHaveValue("00:30");
  await expect(editor.getByLabel("Title")).toBeDisabled();
  await expect(editor.getByRole("button", { name: "Save changes" })).toBeDisabled();
  await expect(editor.getByRole("button", { name: "Remove event" })).toBeEnabled();
  expect(api.blockerRequests.map((request) => request.method())).toEqual(["GET"]);
  await editor.getByRole("button", { name: "Close schedule event editor" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("does not open an obsolete blocker read after navigating away and back", async ({ page }) => {
  const blocker: BlockerScheduleItem = {
    ...interval("blocker-1", "Pool maintenance", "17:00", "17:30"),
    kind: "unavailable", location: null, revision: 2,
  };
  let respond!: () => void;
  const pending = new Promise<void>((resolve) => { respond = resolve; });
  const api = await mockApi(page, (date) => ({ json: agenda(date, {
    items: date === day ? [blocker] : [],
  }) }), {
    blockers: async () => {
      await pending;
      return [savedBlocker(blocker, day, "12:00", "12:30")];
    },
  });
  await page.goto(`/?date=${day}`);
  const edit = page.getByRole("button", { name: "Edit Pool maintenance" });
  await edit.click();
  await expect.poll(() => api.blockerRequests.length).toBe(1);
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await page.getByRole("button", { name: "Previous", exact: true }).click();
  await expect(edit).toBeVisible();

  const response = page.waitForResponse((value) => new URL(value.url()).pathname === "/api/coach/blockers");
  respond();
  await (await response).finished();
  await page.evaluate(() => new Promise(requestAnimationFrame));
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await edit.click();
  await expect(page.getByRole("dialog", { name: "Edit Pool maintenance" })).toBeVisible();
});

test("keeps a deleted blocker out of the fallback when both refreshes fail", async ({ page }) => {
  let removed = false;
  const blocker: BlockerScheduleItem = {
    ...interval("blocker-1", "Pool maintenance", "17:00", "17:30"),
    kind: "unavailable", location: null, revision: 2,
  };
  const known = appointment("known", "Lesson on the same day", "18:00", "18:45");
  const schedule = [blocker, known];
  const api = await mockApi(page, (date) => removed
    ? { status: 503, json: error("Agenda unavailable") }
    : { json: agenda(date, { items: schedule }) }, {
    snapshotItems: () => schedule,
    blockers: () => [savedBlocker(blocker, day, "12:00", "12:30")],
    failSnapshot: () => removed,
    mutateBlocker: async (route) => {
      expect(route.request().method()).toBe("DELETE");
      expect(new URL(route.request().url()).searchParams.get("expected_revision")).toBe("2");
      removed = true;
      await route.fulfill({ status: 204 });
    },
  });
  await page.goto(`/?date=${day}`);
  const row = page.locator('[data-agenda-row="blocker-1"]');
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "Edit Pool maintenance" }).click();
  await page.getByRole("dialog", { name: "Edit Pool maintenance" })
    .getByRole("button", { name: "Remove event" }).click();

  const refreshes = ["/api/coach/agenda", "/api/coach/command-center/snapshot"].map((path) => (
    page.waitForResponse((response) => new URL(response.url()).pathname === path && response.status() === 503)
  ));
  await page.getByRole("dialog", { name: "Remove Pool maintenance?" })
    .getByRole("button", { name: "Remove event" }).click();
  await Promise.all(refreshes);

  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("alert")).toContainText("Showing known appointments");
  await expect(row).toHaveCount(0);
  await expect(page.locator('[data-agenda-row="known"]')).toContainText(known.title);
  expect(api.blockerRequests.map((request) => request.method())).toEqual(["GET", "DELETE"]);
});

test("renders an opaque Google busy interval across a daylight-saving change", async ({ page }) => {
  const dstDay = "2026-11-01";
  await mockApi(page, (date) => ({ json: agenda(date, {
    items: [{
      id: "busy-1", kind: "calendar_busy", title: "Private event title", note: "Private event notes",
      start_time: `${dstDay}T05:30:00Z`, end_time: `${dstDay}T08:30:00Z`, location: null,
    }],
  }) }));
  await page.goto(`/?date=${dstDay}`);

  const busy = page.locator('[data-agenda-row="busy-1"]');
  await expect(busy).toContainText("Calendar busy");
  await expect(busy).toContainText("Busy on Google Calendar");
  await expect(busy).toContainText("12:30 AM-2:30 AM");
  await expect(busy).not.toContainText("Private event");
  await expect(busy.getByRole("button")).toHaveCount(0);
});

test("distinguishes repeated-hour starts and preserves keyboard focus while the agenda loads", async ({ page }) => {
  const dstDay = "2026-11-01";
  let respond!: () => void;
  const pending = new Promise<void>((resolve) => { respond = resolve; });
  await mockApi(page, async (date) => {
    await pending;
    return { json: agenda(date, { items: [{
      ...interval("opening", "Bookable at your service location", "06:30", "08:15", dstDay),
      kind: "open_gap", location: null,
      start_times: [`${dstDay}T06:30:00Z`, `${dstDay}T07:30:00Z`],
    }] }) };
  });
  await page.goto(`/?date=${dstDay}`);
  const addEvent = page.getByRole("button", { name: "Add schedule event" });
  await addEvent.focus();
  respond();

  const opening = page.locator('[data-agenda-row="opening"] .agenda-start-times');
  await expect(opening).toHaveText("1:30 AM CDT · 1:30 AM CST");
  await expect(addEvent).toBeFocused();
});

test("loads only on Schedule and keeps appointments visible when planning is unavailable", async ({ page }) => {
  let status: CoachAgenda["status"] = "setup_required";
  const lesson = appointment("primary", "Booked lesson", "14:00", "14:45");
  const api = await mockApi(page, (date) => ({ json: agenda(date, {
    status, items: [lesson],
    next_up: { appointment_id: lesson.id, leave_by: null, travel_time_mins: null, buffer_mins: null },
  }) }), { setupComplete: false });
  await page.clock.setFixedTime(new Date(`${day}T13:00:00Z`));
  await page.goto(`/?date=${day}`);

  await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible();
  expect(api.agendaRequests).toHaveLength(0);
  const schedule = page.getByRole("button", { name: "Schedule", exact: true });
  await schedule.click();
  await expect(page.locator(".agenda-notice")).toContainText("Complete provider setup");
  await expect(page.locator("[data-agenda-row]")).toContainText("Booked lesson");
  await expect(page.getByRole("heading", { name: "Departure time unavailable" })).toBeVisible();
  await expect(page.getByText(/0 min travel|No travel needed|Bookable starts/)).toHaveCount(0);
  await page.getByRole("button", { name: "Open settings" }).click();
  await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible();
  expect(api.agendaRequests).toHaveLength(1);

  status = "calendar_unavailable";
  await schedule.click();
  await expect(page.locator(".agenda-notice")).toContainText("Google Calendar couldn’t be checked");
  await expect(page.locator("[data-agenda-row]")).toContainText("Booked lesson");
  status = "routing_unavailable";
  await schedule.click();
  await expect(page.locator(".agenda-notice")).toContainText("Travel estimates and bookable starts are unavailable");
  await expect(page.locator("[data-agenda-row]")).toContainText("Booked lesson");
});

test("handles Previous, Next, and provider-local Today while discarding a late day's response", async ({ page }) => {
  const providerToday = "2026-08-26";
  const selected = "2026-08-28";
  let releasePrevious!: () => void;
  const previous = new Promise<void>((resolve) => { releasePrevious = resolve; });
  const api = await mockApi(page, async (date) => {
    if (date === day) await previous;
    return { json: agenda(date, {
      today: providerToday,
      items: [{
        ...interval("daily", `Row for ${date}`, "20:00", "20:30", date),
        kind: "break", location: null, revision: 1,
      }],
    }) };
  }, { today: providerToday });
  await page.clock.setFixedTime(new Date("2026-08-27T02:00:00Z"));
  await page.goto(`/?date=${selected}`);
  await expect(page.locator("[data-agenda-row]")).toContainText(`Row for ${selected}`);

  await page.getByRole("button", { name: "Previous", exact: true }).click();
  await expect.poll(() => api.agendaRequests.length).toBe(2);
  await expect(page.getByRole("heading", { name: "Thursday, August 27" })).toBeVisible();
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.locator("[data-agenda-row]")).toContainText(`Row for ${selected}`);
  const lateResponse = page.waitForResponse((response) => response.url().endsWith(`/agenda?date=${day}`));
  releasePrevious();
  await (await lateResponse).finished();
  await expect(page.locator("[data-agenda-row]")).toContainText(`Row for ${selected}`);
  await expect(page.getByRole("button", { name: "Next", exact: true })).toBeFocused();

  await page.getByRole("button", { name: "Today", exact: true }).click();
  await expect(page.locator("[data-agenda-row]")).toContainText(`Row for ${providerToday}`);
  await expect(page.getByRole("heading", { name: "Wednesday, August 26" })).toBeVisible();
  expect(new URL(api.agendaRequests.at(-1)!.url()).searchParams.has("date")).toBe(false);
});

test("uses provider-local today on unpinned boot and keeps the Playwright pin when set", async ({ page }) => {
  const providerToday = "2026-08-26";
  await page.clock.setFixedTime(new Date("2026-08-27T02:00:00Z"));
  const api = await mockApi(page, (date) => ({ json: agenda(date, { today: providerToday }) }), { today: providerToday });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Nothing on the route yet" })).toBeVisible();

  const pin = (process.env.VITE_INSTRUCTOR_COMMAND_DATE ?? day).trim();
  expect(new URL(api.agendaRequests[0].url()).searchParams.get("date")).toBe(pin || null);
  expect(api.snapshotDates).toEqual([pin || providerToday]);
  expect(new URL(page.url()).searchParams.has("date")).toBe(false);
});

test("opens provider Settings when timezone setup is missing", async ({ page }) => {
  const providerToday = "2026-08-26";
  const api = await mockApi(page, (date) => ({ json: agenda(date, { today: providerToday }) }), {
    today: providerToday, missingTimezone: true,
  });
  await page.clock.setFixedTime(new Date("2026-08-27T02:00:00Z"));
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Complete your scheduling setup" })).toBeVisible();
  const timezone = page.getByRole("combobox", { name: "Timezone", exact: true });
  await expect(timezone).toBeEnabled();
  await expect(timezone).toHaveValue("");
  const pin = (process.env.VITE_INSTRUCTOR_COMMAND_DATE ?? day).trim();
  expect(api.snapshotDates).toEqual([pin || providerToday]);
  expect(api.agendaRequests).toHaveLength(0);
});

test("labels the same UTC fallback in the timeline and Next Up when the browser lacks the provider timezone", async ({ page }) => {
  const lesson = appointment("utc-fallback", "Booked lesson", "14:00", "14:45");
  await simulateUnavailableTimezone(page, "America/Denver");
  await mockApi(page, (date) => ({ json: agenda(date, {
    timezone: "America/Denver",
    items: [lesson],
    next_up: {
      appointment_id: lesson.id, leave_by: `${day}T13:30:00Z`, travel_time_mins: 15, buffer_mins: 15,
    },
  }) }));
  await page.clock.setFixedTime(new Date(`${day}T12:00:00Z`));
  await page.goto(`/?date=${day}`);

  await expect(page.getByText(/Times shown in UTC/)).toBeVisible();
  await expect(page.getByRole("heading", { name: "Leave by 1:30 PM" })).toBeVisible();
  await expect(page.locator('[data-agenda-row="utc-fallback"]')).toContainText("2:00 PM-2:45 PM");
});

test("does not fall back to rows selected in a previous provider timezone", async ({ page }) => {
  const previousZone = appointment("old-zone", "Moved to tomorrow", "04:30", "04:50", address, "2026-08-28");
  const currentZone = appointment("current-zone", "Still on the selected day", "13:00", "13:30");
  let saved = false;
  let fallbackAvailable = false;
  await mockApi(page, (date) => saved
    ? { status: 503, json: error("Agenda unavailable") }
    : { json: agenda(date, { items: [previousZone] }) }, {
    snapshotItems: () => saved ? [currentZone] : [previousZone],
    snapshotTimezone: () => saved ? "America/New_York" : timeZone,
    failSnapshot: () => saved && !fallbackAvailable,
    saveProvider: async (route) => {
      expect(route.request().postDataJSON()).toMatchObject({ timezone: "America/New_York" });
      saved = true;
      await route.fulfill({ json: {
        ...providerConfiguration, id: providerId, timezone: "America/New_York", setup_complete: true,
      } });
    },
  });
  await page.goto(`/?date=${day}`);
  await expect(page.locator('[data-agenda-row="old-zone"]')).toBeVisible();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("combobox", { name: "Timezone", exact: true }).selectOption("America/New_York");
  await page.getByRole("button", { name: "Save provider settings", exact: true }).click();
  await expect(page.locator("[data-provider-save-status]")).toContainText("Provider settings saved");

  await page.getByRole("button", { name: "Schedule", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("The agenda couldn’t load");
  await expect(page.locator('[data-agenda-row="old-zone"]')).toHaveCount(0);
  fallbackAvailable = true;
  await page.getByRole("button", { name: "Retry agenda" }).click();
  await expect(page.locator('[data-agenda-row="current-zone"]')).toBeVisible();
});

test("discards stale fallback after Schedule observes a timezone change in another tab", async ({ page }) => {
  const moved = appointment("moved", "Moved to tomorrow", "04:30", "04:50", address, "2026-08-28");
  const known = appointment("known", "Still on the selected day", "13:00", "13:30");
  let changedTimezone = false;
  let agendaUnavailable = false;
  let snapshotAvailable = false;
  const api = await mockApi(page, (date) => agendaUnavailable
    ? { status: 503, json: error("Agenda unavailable") }
    : { json: agenda(date, {
      timezone: changedTimezone ? "America/New_York" : timeZone,
      items: changedTimezone ? [known] : [moved],
    }) }, {
    snapshotItems: () => changedTimezone ? [known] : [moved],
    snapshotTimezone: () => changedTimezone ? "America/New_York" : timeZone,
    failSnapshot: () => changedTimezone && !snapshotAvailable,
  });

  await page.goto(`/?date=${day}`);
  await expect(page.locator('[data-agenda-row="moved"]')).toBeVisible();
  changedTimezone = true;
  await page.getByRole("button", { name: "Find", exact: true }).click();
  await page.getByRole("button", { name: "Schedule", exact: true }).click();
  await expect(page.locator('[data-agenda-row="known"]')).toBeVisible();

  agendaUnavailable = true;
  await page.getByRole("button", { name: "Find", exact: true }).click();
  await page.getByRole("button", { name: "Schedule", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("The agenda couldn’t load");
  await expect(page.locator('[data-agenda-row="moved"]')).toHaveCount(0);
  expect(api.snapshotDates).toEqual([day, day]);

  snapshotAvailable = true;
  await page.getByRole("button", { name: "Retry agenda" }).click();
  await expect(page.locator('[data-agenda-row="known"]')).toContainText("9:00 AM-9:30 AM");
  await expect(page.locator('[data-agenda-row="moved"]')).toHaveCount(0);
});

test("uses the new local day's known rows after midnight when an unpinned agenda fails", async ({ page }) => {
  const nextDay = "2026-08-28";
  const oldLesson = appointment("old", "Yesterday’s lesson", "14:00", "14:45");
  const newLesson = appointment("new", "Today’s lesson", "14:00", "14:45", address, nextDay);
  let unavailable = false;
  await mockApi(page, (date) => unavailable
    ? { status: 503, json: error("Agenda unavailable") }
    : { json: agenda(date, { today: day, items: [oldLesson] }) },
  { snapshotItems: (date) => date === day ? [oldLesson] : [newLesson] });
  await page.clock.setFixedTime(new Date("2026-08-28T04:59:00Z"));
  await page.goto(`/?date=${day}`);
  await page.getByRole("button", { name: "Today", exact: true }).click();
  await expect(page.locator('[data-agenda-row="old"]')).toBeVisible();

  unavailable = true;
  await page.clock.setFixedTime(new Date("2026-08-28T05:01:00Z"));
  await page.getByRole("button", { name: "Find", exact: true }).click();
  await page.getByRole("button", { name: "Schedule", exact: true }).click();

  await expect(page.getByRole("heading", { name: "Friday, August 28" })).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("Showing known appointments");
  await expect(page.locator('[data-agenda-row="new"]')).toBeVisible();
  await expect(page.locator('[data-agenda-row="old"]')).toHaveCount(0);
});

for (const finalizedAfterMidnight of [false, true]) {
  test(`rechecks Today when a response ${finalizedAfterMidnight ? "finishes" : "arrives"} after local midnight`, async ({ page }) => {
    const pinnedDay = "2026-08-26";
    const nextDay = "2026-08-28";
    const oldLesson = appointment("old", "Yesterday’s lesson", "14:00", "14:45");
    const newLesson = appointment("new", "Today’s lesson", "14:00", "14:45", address, nextDay);
    let respond!: () => void;
    const pending = new Promise<void>((resolve) => { respond = resolve; });
    let requestsForToday = 0;
    const api = await mockApi(page, async (date) => {
      if (date === pinnedDay) return { json: agenda(date, { today: day }) };
      if (++requestsForToday === 1) {
        await pending;
        return { json: agenda(day, { today: finalizedAfterMidnight ? nextDay : day, items: [oldLesson] }) };
      }
      return { json: agenda(nextDay, { today: nextDay, items: [newLesson] }) };
    }, { today: day });
    await page.clock.setFixedTime(new Date("2026-08-28T04:59:00Z"));
    await page.goto(`/?date=${pinnedDay}`);
    await expect(page.getByRole("heading", { name: "Nothing on the route yet" })).toBeVisible();
    await page.getByRole("button", { name: "Today", exact: true }).click();
    await expect.poll(() => requestsForToday).toBe(1);

    await page.clock.setFixedTime(new Date("2026-08-28T05:01:00Z"));
    respond();

    await expect(page.getByRole("heading", { name: "Friday, August 28" })).toBeVisible();
    await expect(page.locator('[data-agenda-row="new"]')).toBeVisible();
    await expect(page.locator('[data-agenda-row="old"]')).toHaveCount(0);
    expect(api.agendaRequests).toHaveLength(3);
  });
}

test("uses same-day known appointments after an agenda error and retries concurrent changes", async ({ page }) => {
  const past = "2026-08-26";
  let pass = 0;
  const known = appointment("known", "Known in snapshot", "14:00", "14:45", address, past);
  const fakeTravel: ScheduleItem = {
    ...interval("old-travel", "Unverified drive", "13:30", "14:00", past),
    kind: "travel", location: null, travel_time_mins: 30,
  };
  await mockApi(page, (date) => {
    if (++pass === 1) return { status: 503, json: error("Unavailable agenda details") };
    if (pass === 2) return { json: agenda(date, { status: "schedule_changed", items: [known] }) };
    return { json: agenda(date, {
      items: [known],
      next_up: { appointment_id: known.id, leave_by: known.start_time, travel_time_mins: 0, buffer_mins: 0 },
    }) };
  }, { snapshotItems: (date) => date === past ? [known, fakeTravel] : [] });
  await page.clock.setFixedTime(new Date(`${day}T13:00:00Z`));
  await page.goto(`/?date=${past}`);

  const notice = page.getByRole("alert");
  await expect(notice).toContainText("Showing known appointments");
  await expect(notice).not.toContainText("Unavailable agenda details");
  await expect(page.locator("[data-agenda-row]")).toHaveCount(1);
  await expect(page.locator("[data-agenda-row]")).toContainText("Known in snapshot");
  await page.getByRole("button", { name: "Retry agenda" }).click();
  await expect(notice).toContainText("The schedule changed");
  await expect(page.locator("[data-agenda-row]")).toContainText("Known in snapshot");
  await page.getByRole("button", { name: "Retry agenda" }).click();
  await expect(notice).toHaveCount(0);
  await expect(page.getByText("Past schedule", { exact: true })).toBeVisible();
  await expect(page.locator(".departure-card")).toHaveCount(0);
});

test("clears the protected workspace when an agenda request detects a changed provider", async ({ page }) => {
  const known = appointment("known", "Previous provider lesson", "14:00", "14:45");
  let respond!: () => void;
  const waiting = new Promise<void>((resolve) => { respond = resolve; });
  const api = await mockApi(page, async () => {
    await waiting;
    return { status: 409, json: error("Provider changed", "CALENDAR_CONNECTION_OWNER_CHANGED") };
  }, {
    snapshotItems: () => [known],
  });
  await page.goto(`/?date=${day}`);
  await expect.poll(() => api.agendaRequests.length).toBe(1);
  await page.getByRole("button", { name: "Find", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Availability finder" })).toBeVisible();
  respond();

  await expect(page.getByRole("alert")).toContainText("Your signed-in provider changed");
  await expect(page.locator("[data-agenda-row]")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Availability finder" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Schedule", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Retry session check" })).toBeVisible();
  expect(api.agendaRequests).toHaveLength(1);
});

test("skips the agenda when the signed-in snapshot belongs to another provider", async ({ page }) => {
  const api = await mockApi(page, (date) => ({ json: agenda(date) }), { snapshotOwner: "other-provider" });
  await page.goto(`/?date=${day}`);

  await expect(page.getByRole("button", { name: "Retry session check" })).toBeVisible();
  await expect(page.locator("[data-agenda-row]")).toHaveCount(0);
  expect(api.agendaRequests).toHaveLength(0);
});

function agenda(date: string, overrides: Partial<CoachAgenda> = {}): CoachAgenda {
  return agendaResponse({ date, instructor_id: providerId, timezone: timeZone, ...overrides });
}

async function simulateUnavailableTimezone(page: Page, timeZoneName: string): Promise<void> {
  await page.addInitScript((unavailable) => {
    const DateTimeFormat = Intl.DateTimeFormat;
    Intl.DateTimeFormat = new Proxy(DateTimeFormat, {
      construct(target, args) {
        if (args[1]?.timeZone === unavailable) throw new RangeError("Timezone unavailable in this browser");
        return Reflect.construct(target, args);
      },
    });
  }, timeZoneName);
}

function interval(id: string, title: string, start: string, end: string, date = day) {
  return { id, title, start_time: `${date}T${start}:00Z`, end_time: `${date}T${end}:00Z` };
}

function savedBlocker(
  blocker: BlockerScheduleItem, date: string, start: string, end: string, zone = timeZone,
): ProviderBlocker {
  const offset = zone === "America/Denver" ? "-06:00" : "-05:00";
  const boundary = new Date(`${date}T00:00:00Z`);
  if (end < start) boundary.setUTCDate(boundary.getUTCDate() + 1);
  return {
    ...blocker,
    start_time: `${date}T${start}:00${offset}`,
    end_time: `${boundary.toISOString().slice(0, 10)}T${end}:00${offset}`,
    local_date: date,
    local_start_time: start,
    local_end_time: end,
    timezone: zone,
  };
}

function appointment(id: string, title: string, start: string, end: string, at = address, date = day): LessonScheduleItem {
  return {
    ...interval(id, title, start, end, date),
    kind: "lesson", client_id: `client-${id}`, status: "scheduled", location: { ...lessonLocation, address: at },
  };
}

function error(message: string, code = "AGENDA_TEST_ERROR") {
  return { error: { code, message } };
}

type AgendaReply = { status?: number; json: CoachAgenda | ReturnType<typeof error> };

async function mockApi(
  page: Page,
  reply: (date: string) => AgendaReply | Promise<AgendaReply>,
  options: {
    today?: string;
    setupComplete?: boolean;
    missingTimezone?: boolean;
    snapshotOwner?: string;
    snapshotItems?: (date: string) => ScheduleItem[];
    snapshotTimezone?: () => string;
    failSnapshot?: () => boolean;
    saveProvider?: (route: Route) => Promise<void>;
    blockers?: () => ProviderBlocker[] | Promise<ProviderBlocker[]>;
    mutateBlocker?: (route: Route) => Promise<void>;
  } = {},
) {
  const agendaRequests: Request[] = [];
  const snapshotDates: string[] = [];
  const blockerRequests: Request[] = [];
  const provider = {
    ...providerConfiguration,
    id: providerId,
    timezone: options.missingTimezone ? "" : timeZone,
    setup_complete: options.setupComplete ?? !options.missingTimezone,
    setup_missing_fields: options.missingTimezone ? ["timezone"] : providerConfiguration.setup_missing_fields,
  };
  await page.context().addCookies([{
    name: "swim_session", value: "agenda-session", domain: "127.0.0.1", path: "/api", httpOnly: true, sameSite: "Strict",
  }]);
  await page.route(/\/api\/(?:auth|coach)\//, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const date = url.searchParams.get("date") ?? options.today ?? day;
    switch (url.pathname) {
      case "/api/auth/coach/session":
        return route.fulfill({ json: {
          authenticated: true, coach: { instructor_id: providerId, display_name: provider.display_name },
          expires_at: "2099-01-01T00:00:00Z",
        } });
      case "/api/coach/provider":
        if (request.method() === "PUT" && options.saveProvider) return options.saveProvider(route);
        return route.fulfill({ json: provider });
      case "/api/coach/command-center/snapshot":
        snapshotDates.push(date);
        if (options.failSnapshot?.()) return route.fulfill({ status: 503, json: error("Snapshot unavailable") });
        return route.fulfill({ json: {
          ...snapshot, date, instructor: {
            ...snapshot.instructor,
            id: options.snapshotOwner ?? providerId,
            timezone: options.snapshotTimezone?.() ?? provider.timezone,
          },
          schedule: options.snapshotItems?.(date) ?? [], requests: [], email_deliveries: [],
        } });
      case "/api/coach/agenda":
        agendaRequests.push(request);
        return route.fulfill(await reply(date));
      case "/api/coach/availability":
        return route.fulfill({ json: { status: "PRESENTING", location: lessonLocation, available_slots: [] } });
      case "/api/coach/calendar-connections/google":
        return route.fulfill({ json: { provider: "google", state: "disconnected" } });
      case "/api/coach/blockers":
      case "/api/coach/blockers/blocker-1":
        blockerRequests.push(request);
        expect(await request.headerValue("x-expected-instructor-id")).toBe(encodeURIComponent(providerId));
        expect(await request.headerValue("cookie")).toContain(cookie);
        if (request.method() === "GET") return route.fulfill({ json: await options.blockers?.() ?? [] });
        if (options.mutateBlocker) return options.mutateBlocker(route);
        break;
    }
    return route.fulfill({ status: 404, json: error("Route not mocked") });
  });
  return { agendaRequests, snapshotDates, blockerRequests };
}
