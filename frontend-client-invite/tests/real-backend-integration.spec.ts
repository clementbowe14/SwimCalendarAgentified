import { expect, test } from "@playwright/test";
import ICAL from "ical.js";
import { readFile } from "node:fs/promises";
import type { LessonRequest } from "../src/api/types";

const apiBase = process.env.VITE_CLIENT_INVITE_API_URL;
if (!apiBase) {
  throw new Error("VITE_CLIENT_INVITE_API_URL is required for the client invite live Playwright suite.");
}

test.describe("live backend calendar and notification handoff", () => {
  test("weekly booking, coach approval, notification refresh and parsed calendar download", async ({ page, request, context }) => {
    const backend = new URL(apiBase).origin;
    const token = process.env.VITE_CLIENT_INVITE_TOKEN || "invite_sofia_0826";
    const inviteResponse = page.waitForResponse((response) => response.url().endsWith(`/client-invites/${token}`));
    await page.goto(`/?invite_token=${encodeURIComponent(token)}`);
    expect((await inviteResponse).ok()).toBeTruthy();
    await expect(page.locator("html")).toHaveAttribute("data-data-mode", "http");
    await expect(page.getByText("Mock data", { exact: true })).toHaveCount(0);
    await expect(page.getByLabel("Client name")).toHaveValue("Sofia Martinez");
    await page.getByRole("button", { name: "Continue to availability" }).click();
    await page.getByRole("button", { name: "Weekly", exact: true }).click();
    await page.getByRole("button", { name: "Show available times" }).click();
    await expect(page.getByRole("button", { name: "Request this lesson" })).toBeEnabled();
    const createdPromise = page.waitForResponse((r) => r.request().method() === "POST" && r.url().endsWith("/lesson-requests"));
    await page.getByRole("button", { name: "Request this lesson" }).click();
    const createdResponse = await createdPromise;
    expect(createdResponse.ok()).toBeTruthy();
    const created = await createdResponse.json() as LessonRequest;
    expect(created.notifications).toEqual([]);
    const persistedResponse = await request.get(
      `${apiBase}/lesson-requests/${encodeURIComponent(created.id)}`,
    );
    expect(persistedResponse.ok(), await persistedResponse.text()).toBeTruthy();
    const persisted = await persistedResponse.json() as LessonRequest;
    expect(persisted.id).toBe(created.id);
    expect(persisted.status).toBe("requested");
    const login = await request.post(`${backend}/api/auth/coach/login`, { data: { email: "coach@example.com", password: "swim-dev-pass" } });
    expect(login.ok()).toBeTruthy();
    const auth = await login.json();
    const approval = await request.post(`${backend}/api/coach/lesson-requests/${created.id}/approve`, { headers: { Authorization: `Bearer ${auth.access_token}` } });
    expect(approval.ok(), await approval.text()).toBeTruthy();
    const refreshedPromise = page.waitForResponse((r) => r.request().method() === "GET" && r.url().endsWith(`/lesson-requests/${created.id}`));
    await page.getByRole("button", { name: "Check confirmation" }).click();
    const confirmed = await (await refreshedPromise).json() as LessonRequest;
    expect(confirmed.status).toBe("confirmed");
    await expect(page.getByText("Text: skipped")).toBeVisible();
    await expect(page.getByText("Email: queued")).toBeVisible();
    await page.getByRole("button", { name: "Refresh status" }).click();
    await expect(page.getByRole("button", { name: "Refresh status" })).toBeEnabled();
    await page.getByRole("button", { name: "Add to calendar" }).click();
    await page.getByRole("button", { name: "Refresh status" }).click();
    await expect(page.getByText("Email: queued")).toBeVisible();
    const events = confirmed.confirmation!.calendar_links.google_events;
    expect(events.length).toBeGreaterThan(1);
    await page.getByRole("button", { name: "Google Calendar", exact: true }).click();
    const occurrenceLinks = page.getByRole("list", { name: "Google Calendar occurrences" }).getByRole("link");
    await expect(occurrenceLinks).toHaveCount(events.length);
    for (let i = 0; i < events.length; i++) {
      await expect(occurrenceLinks.nth(i)).toHaveAttribute("href", events[i].url);
      const params = new URL(events[i].url).searchParams;
      const compact = (value: string) => new Date(value).toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
      expect(params.get("dates")).toBe(`${compact(events[i].start_iso)}/${compact(events[i].end_iso)}`);
    }
    await context.route("https://calendar.google.com/**", async (route) => {
      expect(route.request().headers().referer).toBeUndefined();
      await route.fulfill({ contentType: "text/html", body: "Google Calendar handoff" });
    });
    const popupPromise = page.waitForEvent("popup");
    await occurrenceLinks.last().click();
    const popup = await popupPromise;
    await popup.waitForLoadState();
    expect(await popup.evaluate(() => window.opener)).toBeNull();
    await popup.close();
    for (const name of ["Download .ics", "Apple Calendar"]) {
      const downloadPromise = page.waitForEvent("download");
      const calendarRequestPromise = page.waitForRequest((r) => r.url().endsWith(".ics"));
      await page.getByRole("button", { name, exact: true }).click();
      const download = await downloadPromise;
      expect(download.suggestedFilename()).toBe("swim-lessons.ics");
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

  test("invalid invite reports stable API error", async ({ page }) => {
    await page.goto("/?invite_token=invalid-token");
    await expect(page.getByRole("alert")).toContainText(/INVITE_|TOKEN_|REQUEST_FAILED/);
  });
});
