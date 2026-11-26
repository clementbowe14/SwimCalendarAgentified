import { expect, test, type Page } from "@playwright/test";

test.describe("client invite self-booking flow", () => {
  test("remains public-token scoped without coach authentication UI", async ({ page }) => {
    await page.goto("/");

    await expect(page.locator("html")).toHaveAttribute("data-data-mode", "mock");
    await expect(page.getByText("Mock data", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Let’s find a time that works" })).toBeVisible();
    await expect(page.getByText("Dayweave", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: /sign in/i })).toHaveCount(0);
    await expect(page.getByLabel(/password/i)).toHaveCount(0);
  });

  test("renders intake landing and moves to availability preferences", async ({ page }) => {
    await page.goto("/");

    await expect(page.getByRole("heading", { name: "Let’s find a time that works" })).toBeVisible();
    await expect(page.getByLabel("Client name")).toHaveValue("Sofia Martinez");
    await expectInviteProgress(page, "Step 1 of 5: Info", 20);

    await page.getByRole("button", { name: "Continue to availability" }).click();
    const availabilityHeading = page.getByRole("heading", { name: "Choose the times that suit you" });
    await expect(availabilityHeading).toBeVisible();
    await expect(availabilityHeading).toBeFocused();
    const selectedDuration = page.getByRole("button", { name: "45 min" });
    await expect(selectedDuration).toHaveClass(/selected/);
    await page.keyboard.press("Tab");
    await page.keyboard.press("Tab");
    await expect(selectedDuration).toBeFocused();
    await expect(selectedDuration).toHaveCSS("outline-color", "rgb(24, 71, 55)");
    await expectInviteProgress(page, "Step 2 of 5: Times", 40);
  });

  test("keeps all progress labels inside the 320px reflow viewport", async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 800 });
    await page.goto("/");

    const progressSteps = page.getByRole("navigation", { name: "Invite progress steps" });
    const calendarStep = progressSteps.getByText("Calendar", { exact: true });
    const [progressBox, calendarBox] = await Promise.all([
      progressSteps.boundingBox(),
      calendarStep.boundingBox(),
    ]);
    expect(progressBox).not.toBeNull();
    expect(calendarBox).not.toBeNull();
    if (!progressBox || !calendarBox) throw new Error("Progress labels must have measurable bounds.");
    expect(calendarBox.x + calendarBox.width).toBeLessThanOrEqual(progressBox.x + progressBox.width + 0.5);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });

  test("preserves the client address while comparing the provider location", async ({ page }) => {
    await page.goto("/");

    const homeAddress = page.getByLabel("Appointment address");
    await homeAddress.fill("123 Test Home Avenue");

    await page.getByRole("button", { name: "Provider location" }).click();
    await expect(page.getByLabel("Service location")).toHaveValue("Riverside Studio, Austin, TX");
    await expect(page.getByLabel("Service location")).toHaveAttribute("readonly", "");

    await page.getByRole("button", { name: "My location" }).click();
    await expect(page.getByLabel("Appointment address")).toHaveValue("123 Test Home Avenue");
    await expect(page.getByLabel("Appointment address")).not.toHaveAttribute("readonly", "");
  });

  test("finds backend-shaped slot results from availability preferences", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Continue to availability" }).click();
    await page.getByRole("button", { name: "Weekly" }).click();
    await page.getByRole("button", { name: "Show available times" }).click();
    const bookingNote = page.locator(".booking-note");
    const firstSlot = page.locator(".slot-card").first();
    const requestAction = page.getByRole("button", { name: "Request this time" });
    const [bookingNoteBox, firstSlotBox, requestActionBox] = await Promise.all([
      bookingNote.boundingBox(),
      firstSlot.boundingBox(),
      requestAction.boundingBox(),
    ]);
    expect(bookingNoteBox).not.toBeNull();
    expect(firstSlotBox).not.toBeNull();
    expect(requestActionBox).not.toBeNull();
    if (!bookingNoteBox || !firstSlotBox || !requestActionBox) {
      throw new Error("Result content and its action must have measurable bounds.");
    }
    expect(boxesOverlap(requestActionBox, bookingNoteBox)).toBe(false);
    expect(boxesOverlap(requestActionBox, firstSlotBox)).toBe(false);

    const resultsHeading = page.getByRole("heading", { name: "Choose your preferred time" });
    await expect(resultsHeading).toBeVisible();
    await expect(resultsHeading).toBeFocused();
    await expect(page.getByText("Available to request").first()).toBeVisible();
    await expect(page.getByText(/travel|buffer|distance/i)).toHaveCount(0);
    await expect(page.getByText("No matching options Tue after 5 PM")).toBeVisible();
    await expectInviteProgress(page, "Step 3 of 5: Slots", 60);

    await page.getByRole("button", { name: "Edit preferences" }).click();
    await expect(page.getByRole("heading", { name: "Choose the times that suit you" })).toBeVisible();
    await expectInviteProgress(page, "Step 2 of 5: Times", 40);
  });

  test("requests a lesson and shows confirmation status", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Continue to availability" }).click();
    await page.getByRole("button", { name: "Show available times" }).click();
    await page.getByRole("button", { name: "Request this time" }).click();

    const requestHeading = page.getByRole("heading", { name: "Your appointment request" });
    await expect(requestHeading).toBeVisible();
    await expect(requestHeading).toBeFocused();
    await expect(page.getByRole("status")).toContainText("Pending confirmation. Someone else may book this time.");
    await expect(page.getByText(/(slot|time) is held/i)).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Add to calendar" })).toHaveCount(0);
    await expect(page.getByText("Appointment request received")).toBeVisible();
    await expectInviteProgress(page, "Step 4 of 5: Status", 80);

    await page.getByRole("button", { name: "Check confirmation" }).click();
    const confirmedHero = page.getByRole("heading", { name: "Appointment confirmed", level: 1 });
    await expect(confirmedHero).toBeFocused();
    await expect(page.getByRole("status")).toContainText("Riley confirmed");
    await expect(page.getByRole("heading", { name: "Appointment confirmed", level: 2 })).toBeVisible();
    await expectInviteProgress(page, "Step 4 of 5: Status", 80);

    const calendarAction = page.getByRole("button", { name: "Add to calendar" });
    const refreshAction = page.getByRole("button", { name: "Refresh status" });
    await expect(calendarAction).toBeVisible();
    await expect(refreshAction).toBeVisible();
    const [calendarBox, refreshBox] = await Promise.all([
      calendarAction.boundingBox(),
      refreshAction.boundingBox(),
    ]);
    expect(calendarBox).not.toBeNull();
    expect(refreshBox).not.toBeNull();
    if (!calendarBox || !refreshBox) throw new Error("Confirmed actions must have measurable bounds.");
    const actionsOverlap =
      calendarBox.x < refreshBox.x + refreshBox.width &&
      calendarBox.x + calendarBox.width > refreshBox.x &&
      calendarBox.y < refreshBox.y + refreshBox.height &&
      calendarBox.y + calendarBox.height > refreshBox.y;
    expect(actionsOverlap).toBe(false);
  });

  test("shows calendar handoff after confirmed request", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Continue to availability" }).click();
    await page.getByRole("button", { name: "Show available times" }).click();
    await page.getByRole("button", { name: "Request this time" }).click();
    await page.getByRole("button", { name: "Check confirmation" }).click();
    await page.getByRole("button", { name: "Add to calendar" }).click();

    const calendarHeading = page.getByRole("heading", { name: "Appointment confirmed", level: 1 });
    await expect(calendarHeading).toBeVisible();
    await expect(calendarHeading).toBeFocused();
    await expect(page.getByRole("heading", { name: "Ready for your calendar" })).toBeVisible();
    await expectInviteProgress(page, "Step 5 of 5: Calendar", 100);
    await expect(page.getByRole("link", { name: "Google Calendar" })).toHaveAttribute("href", /https:\/\/calendar.google.com\/calendar\/render/);
    await expect(page.getByText("Added to Google Calendar.")).toHaveCount(0);
  });
});

async function expectInviteProgress(page: Page, stepText: string, percentComplete: number) {
  await expect(page.getByText(stepText)).toBeVisible();
  await expect(page.getByText(`${percentComplete}% complete`)).toBeVisible();

  const progress = page.getByRole("progressbar", { name: "Invite completion" });
  await expect(progress).toHaveAttribute("aria-valuenow", String(percentComplete));
  await expect(progress).toHaveAttribute("aria-valuetext", `${percentComplete}% complete`);
}

function boxesOverlap(
  first: { x: number; y: number; width: number; height: number },
  second: { x: number; y: number; width: number; height: number },
) {
  return (
    first.x < second.x + second.width &&
    first.x + first.width > second.x &&
    first.y < second.y + second.height &&
    first.y + first.height > second.y
  );
}
