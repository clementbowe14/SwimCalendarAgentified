import { expect, test, type Page } from "@playwright/test";
import { createConfirmedLesson, inviteEnvelope, schedulerSlots, slotResponse } from "../src/data/mockInvite";
import type { NotificationStatus } from "../src/api/types";

test.use({ baseURL: "http://127.0.0.1:3003" });

async function setup(page: Page, weekly = false) {
  const request = createConfirmedLesson(schedulerSlots[0]);
  const links = request.confirmation!.calendar_links;
  links.apple = links.ics = "/api/public/lesson-requests/request-token/calendar.ics";
  if (weekly) links.google_events.push({ start_iso: "2026-09-05T17:30:00Z", end_iso: "2026-09-05T18:15:00Z", url: "https://calendar.google.com/calendar/render?action=TEMPLATE&dates=20260905T173000Z%2F20260905T181500Z" });
  let failRefresh = false;
  await page.route("http://127.0.0.1:3109/api/public/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("calendar.ics")) return route.fulfill({ status: 200, contentType: "text/calendar;charset=utf-8", body: "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n" });
    if (path.endsWith("/slots")) return route.fulfill({ json: slotResponse });
    if (path.endsWith("/lesson-requests")) return route.fulfill({ json: request });
    if (path.includes("/lesson-requests/")) return route.fulfill(failRefresh ? { status: 503, json: { error: { message: "Status refresh unavailable", code: "UNAVAILABLE" } } } : { json: request });
    return route.fulfill({ json: inviteEnvelope });
  });
  await page.goto("/?invite_token=bearer-secret");
  await expect(page.locator("html")).toHaveAttribute("data-data-mode", "http");
  await expect(page.getByText("Mock data", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Continue to availability" }).click();
  await page.getByRole("button", { name: "Show available times" }).click();
  await page.getByRole("button", { name: "Request this lesson" }).click();
  return { request, fail: () => { failRefresh = true; } };
}

test("all delivery states refresh after confirmation and handoff; failures retain known state", async ({ page }) => {
  const { request, fail } = await setup(page);
  await expect(page.locator(".delivery-chip")).toHaveCount(0);
  for (const status of ["queued", "processing", "accepted", "delivered", "failed", "skipped"] as NotificationStatus["status"][]) {
    request.notifications = ["sms", "email"].map((channel) => ({ kind: "confirmation", channel: channel as "sms" | "email", status, updated_at: "2026-08-29T12:00:00Z" }));
    await page.getByRole("button", { name: "Refresh status" }).click();
    await expect(page.locator(".delivery-chip").first()).toContainText(`Text: ${status}`);
    if (status === "accepted") await expect(page.getByText("Text: accepted by provider (delivery unconfirmed)")).toBeVisible();
  }
  await page.getByRole("button", { name: "Add to calendar" }).click();
  request.notifications[0].status = "delivered";
  await page.getByRole("button", { name: "Refresh status" }).click();
  await expect(page.getByText("Text: delivered")).toBeVisible();
  fail();
  await page.getByRole("button", { name: "Refresh status" }).click();
  await expect(page.getByRole("alert")).toContainText("Status refresh unavailable");
  await expect(page.getByText("Text: delivered")).toBeVisible();
});

test("Google navigation suppresses bearer referrer and has no false imported claim", async ({ page, context }) => {
  await setup(page);
  await page.getByRole("button", { name: "Add to calendar" }).click();
  let referer: string | undefined;
  await context.route("https://calendar.google.com/**", async (route) => {
    referer = route.request().headers().referer;
    await route.fulfill({ contentType: "text/html", body: "Calendar" });
  });
  const popupPromise = page.waitForEvent("popup");
  await page.getByRole("link", { name: "Google Calendar" }).click();
  const popup = await popupPromise;
  await popup.waitForLoadState();
  expect(referer).toBeUndefined();
  expect(await popup.evaluate(() => window.opener)).toBeNull();
  await expect(page.locator('meta[name="referrer"]')).toHaveAttribute("content", "no-referrer");
  await expect(page.getByText(/Added to Google/)).toHaveCount(0);
});

test("weekly Google action exposes each dated occurrence", async ({ page }, testInfo) => {
  await setup(page, true);
  await page.getByRole("button", { name: "Add to calendar" }).click();
  await page.getByRole("button", { name: "Google Calendar" }).click();
  const links = page.getByRole("list", { name: "Google Calendar occurrences" }).getByRole("link");
  await expect(links).toHaveCount(2);
  await expect(links.nth(0)).toContainText("Aug 29, 2026");
  await expect(links.nth(1)).toContainText("Sep 5, 2026");
  await expect(links.nth(1)).toHaveAttribute("href", /20260905/);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("weekly-calendar.png"), fullPage: true });
});

test("Apple and ICS download from backend origin; HTTP errors never download", async ({ page }) => {
  await setup(page);
  await page.getByRole("button", { name: "Add to calendar" }).click();
  for (const name of ["Apple Calendar", "Download .ics"]) {
    const responsePromise = page.waitForRequest((r) => r.url().endsWith("calendar.ics"));
    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("button", { name, exact: true }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toBe("swim-lessons.ics");
    const request = await responsePromise;
    expect(request.url()).toBe("http://127.0.0.1:3109/api/public/lesson-requests/request-token/calendar.ics");
    expect(request.headers().referer).toBeUndefined();
  }
  await page.route("**/calendar.ics", (route) => route.fulfill({ status: 409, json: { error: "unavailable" } }));
  let downloads = 0;
  page.on("download", () => { downloads++; });
  await page.getByRole("button", { name: "Download .ics", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Calendar download failed");
  expect(downloads).toBe(0);
});

test("untrusted calendar payloads cannot navigate or download", async ({ page }) => {
  const { request } = await setup(page);
  const links = request.confirmation!.calendar_links;
  links.google_events = [];
  links.google = "javascript:alert('unsafe')";
  links.ics = "https://evil.example/calendar.ics";
  await page.getByRole("button", { name: "Refresh status" }).click();
  await expect(page.getByRole("button", { name: "Refresh status" })).toBeEnabled();
  await page.getByRole("button", { name: "Add to calendar" }).click();
  await expect(page.locator("a.calendar-save-button")).not.toHaveAttribute("href");
  await page.getByRole("button", { name: "Download .ics", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Calendar download is unavailable");
});

for (const screen of ["request", "handoff"]) {
  test(`${screen} refresh removes cancelled dates and retains confirmation and notifications`, async ({ page }) => {
    const { request } = await setup(page, true);
    request.notifications = [{ kind: "confirmation", channel: "email", status: "queued", updated_at: "2026-08-29T12:00:00Z" }];
    if (screen === "handoff") await page.getByRole("button", { name: "Add to calendar" }).click();
    request.confirmation!.calendar_links.google_events.shift();
    request.confirmation!.message = "Confirmed swim lesson on Sep 5, 2026.";
    await page.getByRole("button", { name: "Refresh status" }).click();
    await expect(page.getByText("Email: queued")).toBeVisible();
    await expect(page.getByLabel("Active lesson dates")).toContainText("Sep 5, 2026");
    await expect(page.getByText(/Aug 29/)).toHaveCount(0);
    request.confirmation = null;
    await page.getByRole("button", { name: "Refresh status" }).click();
    await expect(page.getByText("No active lesson dates available.")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Lesson confirmed", level: 1 })).toBeVisible();
    await expect(page.getByText("Email: queued")).toBeVisible();
    await expect(page.getByRole("button", { name: "Refresh status" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "Add to calendar" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Download .ics" })).toHaveCount(0);
    await expect(page.getByText(/Sep 5|Aug 29|slot is held|time is held|Request sent/)).toHaveCount(0);
  });
}
