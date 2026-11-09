import { expect, test, type Page } from "@playwright/test";

test.describe("client invite self-booking flow", () => {
  test("remains public-token scoped without coach authentication UI", async ({ page }) => {
    await page.goto("/");

    await expect(page.locator("html")).toHaveAttribute("data-data-mode", "mock");
    await expect(page.getByText("Mock data", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Tell us when you can swim" })).toBeVisible();
    await expect(page.getByRole("button", { name: /sign in/i })).toHaveCount(0);
    await expect(page.getByLabel(/password/i)).toHaveCount(0);
  });

  test("renders intake landing and moves to availability preferences", async ({ page }) => {
    await page.goto("/");

    await expect(page.getByRole("heading", { name: "Tell us when you can swim" })).toBeVisible();
    await expect(page.getByLabel("Client name")).toHaveValue("Sofia Martinez");
    await expectInviteProgress(page, "Step 1 of 5: Info", 20);

    await page.getByRole("button", { name: "Continue to availability" }).click();
    await expect(page.getByRole("heading", { name: "Choose your open windows" })).toBeVisible();
    await expect(page.getByRole("button", { name: "45 min" })).toHaveClass(/selected/);
    await expectInviteProgress(page, "Step 2 of 5: Times", 40);
  });

  test("finds backend-shaped slot results from availability preferences", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Continue to availability" }).click();
    await page.getByRole("button", { name: "Weekly" }).click();
    await page.getByRole("button", { name: "Show available times" }).click();

    await expect(page.getByRole("heading", { name: "Pick a lesson time" })).toBeVisible();
    await expect(page.getByText("Travel 12 min - Buffer 10 min - 2.1 mi")).toBeVisible();
    await expect(page.getByText("No safe options Tue after 5 PM")).toBeVisible();
    await expectInviteProgress(page, "Step 3 of 5: Slots", 60);

    await page.getByRole("button", { name: "Edit preferences" }).click();
    await expect(page.getByRole("heading", { name: "Choose your open windows" })).toBeVisible();
    await expectInviteProgress(page, "Step 2 of 5: Times", 40);
  });

  test("requests a lesson and shows confirmation status", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Continue to availability" }).click();
    await page.getByRole("button", { name: "Show available times" }).click();
    await page.getByRole("button", { name: "Request this lesson" }).click();

    await expect(page.getByRole("heading", { name: "Lesson request sent" })).toBeVisible();
    await expect(page.getByText("Lesson request received")).toBeVisible();
    await expectInviteProgress(page, "Step 4 of 5: Status", 80);

    await page.getByRole("button", { name: "Check confirmation" }).click();
    await expect(page.getByRole("heading", { name: "Lesson confirmed", level: 2 })).toBeVisible();
    await expectInviteProgress(page, "Step 4 of 5: Status", 80);
  });

  test("shows calendar handoff after confirmed request", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Continue to availability" }).click();
    await page.getByRole("button", { name: "Show available times" }).click();
    await page.getByRole("button", { name: "Request this lesson" }).click();
    await page.getByRole("button", { name: "Check confirmation" }).click();
    await page.getByRole("button", { name: "Add to calendar" }).click();

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
