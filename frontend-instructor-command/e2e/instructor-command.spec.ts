import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

const testWhenMockAdapter = process.env.VITE_INSTRUCTOR_API_URL ? test.skip : test;
const describeWhenBackendConfigured = process.env.VITE_INSTRUCTOR_API_URL ? test.describe : test.describe.skip;

testWhenMockAdapter("navigates the instructor command screens with the local adapter", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Today schedule" })).toBeVisible();
  await page.getByRole("button", { name: "Find" }).click();
  await expect(page.getByRole("heading", { name: "Availability finder" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Find safe starts" })).toBeVisible();

  await page.getByRole("button", { name: "Client", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Client detail" })).toBeVisible();

  await page.getByRole("button", { name: "Requests", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Request management" })).toBeVisible();

  await page.getByRole("button", { name: "Settings" }).click();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
});

testWhenMockAdapter("hands a directly booked persisted appointment to the instructor calendar", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Find" }).click();

  const bookLesson = page.getByRole("button", { name: "Book lesson" }).first();
  await bookLesson.click();

  const dialog = page.getByRole("dialog", { name: "Ready for your calendar" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toBeFocused();
  await expect(dialog.getByRole("link", { name: /Open Google Calendar/ })).toHaveAttribute(
    "href",
    /^https:\/\/calendar\.google\.com\/calendar\/render/,
  );
  await expect(dialog).toContainText("does not add the appointment automatically");
  await expect(dialog).not.toContainText(/added to (?:your|the) calendar/i);

  await page.keyboard.press("Shift+Tab");
  await expect(dialog.getByRole("button", { name: "Done" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "Close calendar options" })).toBeFocused();

  const downloadPromise = page.waitForEvent("download");
  await dialog.getByRole("button", { name: "Download .ics" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("appointment.ics");
  await expect(dialog.getByRole("status")).toContainText(/downloaded/i);

  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "Close calendar options" })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(dialog.getByRole("button", { name: "Done" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(bookLesson).toBeFocused();
});

testWhenMockAdapter("uses confirmation IDs for a recurring request handoff", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Requests", exact: true }).click();
  await page.getByRole("button", { name: "Confirm" }).first().click();

  const dialog = page.getByRole("dialog", { name: "Ready for your calendar" });
  const occurrences = dialog.getByRole("list", { name: "Google Calendar occurrences" }).getByRole("link");
  await expect(dialog).toBeVisible();
  await expect(occurrences).toHaveCount(2);
  await expect(occurrences.nth(0)).toHaveAttribute("href", /dates=/);
  await expect(occurrences.nth(1)).toHaveAttribute("href", /dates=/);
  await dialog.getByRole("button", { name: "Done" }).click();

  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Requests", exact: true })).toBeFocused();
});

testWhenMockAdapter("keeps calendar handoff controls usable at 320px", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 720 });
  await page.goto("/");
  await page.getByRole("button", { name: "Find" }).click();
  await page.getByRole("button", { name: "Book lesson" }).first().click();

  const dialog = page.getByRole("dialog", { name: "Ready for your calendar" });
  const bounds = await dialog.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(320);
  for (const control of await dialog.locator("a.calendar-action, button.calendar-action, button.calendar-done-action").all()) {
    const controlBounds = await control.boundingBox();
    expect(controlBounds?.height ?? 0).toBeGreaterThanOrEqual(44);
  }
  const hasHorizontalOverflow = await dialog.evaluate((element) => element.scrollWidth > element.clientWidth + 1);
  expect(hasHorizontalOverflow).toBe(false);
});

describeWhenBackendConfigured("instructor command real-backend integration", () => {
  test("signs in, loads protected coach data, and signs out", async ({ page }) => {
    await signIn(page);

    await expect(page.getByRole("heading", { name: "Today schedule" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Sign out" })).toHaveCount(0);

    await page.getByRole("button", { name: "Settings" }).click();
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    const signOut = page.getByRole("button", { name: "Sign out" });
    await expect(signOut).toBeVisible();
    await expect(signOut).toHaveCSS("color", "rgb(255, 255, 255)");
    await expect(signOut).toHaveCSS("background-image", /rgb\(217, 71, 50\)/);
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page.getByRole("heading", { name: /locked|sign in/i })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Today schedule" })).toHaveCount(0);
  });

  test("loads coach schedule data and searches availability through /api/coach", async ({ page }) => {
    await signIn(page);

    await page.getByRole("button", { name: "Find" }).click();
    await expect(page.getByRole("heading", { name: "Availability finder" })).toBeVisible();

    await page.getByRole("button", { name: "Find safe starts" }).click();
    await expect(page.getByRole("heading", { name: "Availability finder" })).toBeVisible();
    await expect(page.locator("main")).toContainText(/safe options|No safe starts/i);
  });

  test("books a safe slot and opens its calendar handoff", async ({ page }) => {
    await signIn(page);
    await page.getByRole("button", { name: "Find" }).click();
    await page.getByRole("button", { name: "Book lesson" }).first().click();

    const dialog = page.getByRole("dialog", { name: "Ready for your calendar" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("link", { name: /Google Calendar/ })).toHaveAttribute(
      "href",
      /^https:\/\/calendar\.google\.com\/calendar\/render/,
    );
    const downloadPromise = page.waitForEvent("download");
    await dialog.getByRole("button", { name: "Download .ics" }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toBe("appointment.ics");
    await expect(dialog.getByRole("status")).toContainText(/downloaded/i);
  });

  test("confirms a pending request and opens its persisted calendar handoff", async ({ page }) => {
    await signIn(page);
    await page.getByRole("button", { name: "Requests", exact: true }).click();
    await page.getByRole("button", { name: "Confirm" }).first().click();

    const dialog = page.getByRole("dialog", { name: "Ready for your calendar" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("link", { name: /Google Calendar/ })).toHaveAttribute(
      "href",
      /^https:\/\/calendar\.google\.com\/calendar\/render/,
    );
    await expect(dialog).toContainText("does not add the appointment automatically");
  });
});

async function signIn(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /locked|sign in/i })).toBeVisible();
  await page.getByLabel("Email").fill(process.env.VITE_INSTRUCTOR_E2E_EMAIL ?? "coach@example.com");
  await page.getByLabel("Password").fill(process.env.VITE_INSTRUCTOR_E2E_PASSWORD ?? "swim-dev-pass");
  await page.getByRole("button", { name: "Sign in" }).click();
}
