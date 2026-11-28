import { expect, request as playwrightRequest, test } from "@playwright/test";
import ICAL from "ical.js";
import { readFile } from "node:fs/promises";
import type { LessonConfirmation, LessonRequest } from "../src/api/types";

const apiBase = process.env.VITE_CLIENT_INVITE_API_URL;
if (!apiBase) {
  throw new Error("VITE_CLIENT_INVITE_API_URL is required for the client invite live Playwright suite.");
}

test.describe("live backend calendar and notification handoff", () => {
  test("one-time booking, canonical coach confirmation, notification refresh and parsed calendar download", async ({ page, request, context }, testInfo) => {
    const backend = new URL(apiBase).origin;
    const backendUrl = new URL(backend);
    const token = process.env.VITE_CLIENT_INVITE_TOKEN || "invite_sofia_0826";
    await context.addCookies([{
      name: "swim_session",
      value: "public-client-must-not-send-coach-cookie",
      domain: backendUrl.hostname,
      path: "/api",
      httpOnly: true,
      secure: backendUrl.protocol === "https:",
      sameSite: "Strict",
    }]);
    const inviteResponse = page.waitForResponse((response) => response.url().endsWith(`/client-invites/${token}`));
    await page.goto(`/?invite_token=${encodeURIComponent(token)}`);
    const inviteNetworkResponse = await inviteResponse;
    expect(inviteNetworkResponse.ok()).toBeTruthy();
    expect((await inviteNetworkResponse.request().allHeaders()).cookie ?? "").not.toContain("swim_session");
    await expect(page.locator("html")).toHaveAttribute("data-data-mode", "http");
    await expect(page.getByText("Mock data", { exact: true })).toHaveCount(0);
    await expect(page.getByLabel("Client name")).toHaveValue("Sofia Martinez");
    await page.getByRole("button", { name: "Continue to availability" }).click();
    const excludedDays = testInfo.project.name === "mobile-chrome" ? ["Tue", "Thu"] : ["Sat"];
    for (const day of excludedDays) {
      await page.getByRole("button", { name: day, exact: true }).click();
    }
    await page.getByLabel("Weekdays time availability")
      .getByRole("button", { name: "10 AM-12 PM", exact: true }).click();
    await page.getByRole("button", { name: "Show available times" }).click();
    await expect(page.getByRole("button", { name: "Request this time" })).toBeEnabled();
    const createdPromise = page.waitForResponse((r) => r.request().method() === "POST" && r.url().endsWith("/lesson-requests"));
    await page.getByRole("button", { name: "Request this time" }).click();
    const createdResponse = await createdPromise;
    expect(createdResponse.ok()).toBeTruthy();
    const created = await createdResponse.json() as LessonRequest;
    expect(created.notifications).toEqual([]);
    expect(created.slot.recurrence).toBe("single");
    const selectedOccurrence = eventInstants(created.slot);
    const persistedResponse = await request.get(
      `${apiBase}/lesson-requests/${encodeURIComponent(created.id)}`,
    );
    expect(persistedResponse.ok(), await persistedResponse.text()).toBeTruthy();
    const persisted = await persistedResponse.json() as LessonRequest;
    expect(persisted.id).toBe(created.id);
    expect(persisted.status).toBe("requested");
    expect(eventInstants(persisted.slot)).toEqual(selectedOccurrence);
    const coachConfirmation = await confirmWithIsolatedCoachSession(backend, created.id);
    expect(coachConfirmation.status).toBe("confirmed");
    expect(coachConfirmation.appointment_ids).toHaveLength(1);
    expect(coachConfirmation.calendar_links).not.toBeNull();
    const refreshedPromise = page.waitForResponse((r) => r.request().method() === "GET" && r.url().endsWith(`/lesson-requests/${created.id}`));
    await page.getByRole("button", { name: "Check confirmation" }).click();
    const confirmed = await (await refreshedPromise).json() as LessonRequest;
    expect(confirmed.status).toBe("confirmed");
    expect(eventInstants(confirmed.slot)).toEqual(selectedOccurrence);
    expect(confirmed.confirmation).not.toBeNull();
    expect(confirmed.confirmation!.calendar_links.google_events.map(eventInstants)).toEqual(
      coachConfirmation.calendar_links!.google_events.map(eventInstants),
    );
    expect(eventInstants(confirmed.confirmation!.calendar_links.google_events[0])).toEqual(
      selectedOccurrence,
    );
    await expect(page.getByText("Text: skipped")).toBeVisible();
    await expect(page.getByText("Email: queued")).toBeVisible();
    await page.getByRole("button", { name: "Refresh status" }).click();
    await expect(page.getByRole("button", { name: "Refresh status" })).toBeEnabled();
    await page.getByRole("button", { name: "Add to calendar" }).click();
    await page.getByRole("button", { name: "Refresh status" }).click();
    await expect(page.getByText("Email: queued")).toBeVisible();
    const events = confirmed.confirmation!.calendar_links.google_events;
    expect(events).toHaveLength(1);
    const googleLink = page.getByRole("link", { name: "Google Calendar", exact: true });
    await expect(googleLink).toHaveAttribute("href", events[0].url);
    const params = new URL(events[0].url).searchParams;
    const compact = (value: string) => new Date(value).toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
    expect(params.get("dates")).toBe(`${compact(events[0].start_iso)}/${compact(events[0].end_iso)}`);
    await context.route("https://calendar.google.com/**", async (route) => {
      expect(route.request().headers().referer).toBeUndefined();
      await route.fulfill({ contentType: "text/html", body: "Google Calendar handoff" });
    });
    const popupPromise = page.waitForEvent("popup");
    await googleLink.click();
    const popup = await popupPromise;
    await popup.waitForLoadState();
    expect(await popup.evaluate(() => window.opener)).toBeNull();
    await popup.close();
    for (const name of ["Download .ics", "Apple Calendar"]) {
      const downloadPromise = page.waitForEvent("download");
      const calendarRequestPromise = page.waitForRequest((r) => r.url().endsWith(".ics"));
      await page.getByRole("button", { name, exact: true }).click();
      const download = await downloadPromise;
      expect(download.suggestedFilename()).toBe("dayweave-appointments.ics");
      const calendarRequest = await calendarRequestPromise;
      expect(new URL(calendarRequest.url()).origin).toBe(backend);
      expect(calendarRequest.headers().referer).toBeUndefined();
      const text = await readFile((await download.path())!, "utf8");
      const calendar = new ICAL.Component(ICAL.parse(text));
      const parsedEvents = calendar.getAllSubcomponents("vevent").map((event) => new ICAL.Event(event));
      expect(parsedEvents).toHaveLength(events.length);
      expect(parsedEvents.map((event) => event.startDate.toJSDate().toISOString())).toEqual(events.map((event) => new Date(event.start_iso).toISOString()));
      expect(parsedEvents.map((event) => event.endDate.toJSDate().toISOString())).toEqual(events.map((event) => new Date(event.end_iso).toISOString()));
      for (const event of parsedEvents) {
        expect(event.summary).toBe("Swim lesson");
        expect(event.description).toBe("Confirmed swim lesson.");
      }
      expect(text).not.toContain(token);
      expect(text).not.toContain("ana.martinez");
    }
    await expect(page.getByText(/Added to Google|Text sent|Email sent/)).toHaveCount(0);
  });

  test("shows general suggestions when the provider cannot offer any times", async ({ page }) => {
    const token = process.env.VITE_CLIENT_INVITE_TOKEN || "invite_sofia_0826";
    await page.goto(`/?invite_token=${encodeURIComponent(token)}`);
    await page.getByRole("button", { name: "Continue to availability" }).click();
    for (const group of ["Weekdays", "Weekend"]) {
      await page.getByLabel(`${group} time availability`)
        .getByRole("button", { name: "8 PM-10 PM", exact: true }).click();
    }
    const search = page.waitForResponse((response) => (
      response.request().method() === "POST" && response.url().endsWith("/availability/search")
    ));
    await page.getByRole("button", { name: "Show available times" }).click();
    const response = await search;
    expect(response.ok(), await response.text()).toBeTruthy();
    expect(await response.json()).toEqual({
      start_date: expect.any(String),
      end_date: expect.any(String),
      duration_mins: 45,
      slots: [],
    });
    await expect(page.getByRole("status")).toContainText("Try changing your days, time windows, or duration.");
    await expect(page.getByRole("button", { name: "Request this time" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Edit preferences" })).toBeVisible();
  });

  test("invalid invite reports stable API error", async ({ page }) => {
    await page.goto("/?invite_token=invalid-token");
    await expect(page.getByRole("alert")).toContainText(/INVITE_|TOKEN_|REQUEST_FAILED/);
  });
});

function eventInstants(value: { start_iso: string; end_iso: string }): { start: string; end: string } {
  return {
    start: new Date(value.start_iso).toISOString(),
    end: new Date(value.end_iso).toISOString(),
  };
}

async function confirmWithIsolatedCoachSession(
  backend: string,
  requestId: string,
): Promise<{
  status: "confirmed";
  appointment_ids: string[];
  calendar_links: LessonConfirmation["calendar_links"] | null;
}> {
  const coach = await playwrightRequest.newContext();
  try {
    const login = await coach.post(`${backend}/api/auth/coach/sign-in`, {
      data: {
        email: process.env.VITE_INSTRUCTOR_E2E_EMAIL ?? "coach@example.com",
        password: process.env.VITE_INSTRUCTOR_E2E_PASSWORD ?? "swim-dev-pass",
      },
      headers: { "X-CSRF-Protection": "1" },
    });
    const loginBody = await login.text();
    expect(login.ok(), loginBody).toBeTruthy();
    const session = JSON.parse(loginBody) as Record<string, unknown>;
    expect(session).toMatchObject({ authenticated: true });
    expect(session).not.toHaveProperty("access_token");
    expect(session).not.toHaveProperty("token_type");

    const confirmationResponse = await coach.post(
      `${backend}/api/coach/lesson-requests/${requestId}/confirm`,
      {
        data: {},
        headers: { "X-CSRF-Protection": "1" },
      },
    );
    const confirmationBody = await confirmationResponse.text();
    expect(confirmationResponse.ok(), confirmationBody).toBeTruthy();
    return JSON.parse(confirmationBody) as {
      status: "confirmed";
      appointment_ids: string[];
      calendar_links: LessonConfirmation["calendar_links"] | null;
    };
  } finally {
    await coach.post(`${backend}/api/auth/coach/sign-out`, {
      headers: { "X-CSRF-Protection": "1" },
    }).catch(() => undefined);
    await coach.dispose();
  }
}
