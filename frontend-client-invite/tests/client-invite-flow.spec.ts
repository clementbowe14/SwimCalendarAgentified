import { expect, test } from "@playwright/test";

test.describe("client invite self-booking flow", () => {
  test("renders intake landing and moves to availability preferences", async ({ page }) => {
    await page.goto("/");

    await expect(page.getByRole("heading", { name: "Tell us when you can swim" })).toBeVisible();
    await expect(page.getByLabel("Client name")).toHaveValue("Sofia Martinez");

    await page.getByRole("button", { name: "Continue to availability" }).click();
    await expect(page.getByRole("heading", { name: "Choose your open windows" })).toBeVisible();
    await expect(page.getByRole("button", { name: "45 min" })).toHaveClass(/selected/);
  });

  test("finds backend-shaped slot results from availability preferences", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Continue to availability" }).click();
    await page.getByRole("button", { name: "Weekly" }).click();
    await page.getByRole("button", { name: "Show available times" }).click();

    await expect(page.getByRole("heading", { name: "Pick a lesson time" })).toBeVisible();
    await expect(page.getByText("Travel 12 min - Buffer 10 min - 2.1 mi")).toBeVisible();
    await expect(page.getByText("No safe options Tue after 5 PM")).toBeVisible();
  });

  test("requests a lesson and shows confirmation status", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Continue to availability" }).click();
    await page.getByRole("button", { name: "Show available times" }).click();
    await page.getByRole("button", { name: "Request this lesson" }).click();

    await expect(page.getByRole("heading", { name: "Lesson request sent" })).toBeVisible();
    await expect(page.getByText("Text and email request sent")).toBeVisible();

    await page.getByRole("button", { name: "Check confirmation" }).click();
    await expect(page.getByRole("heading", { name: "Lesson confirmed" })).toBeVisible();
  });

  test("shows calendar handoff after confirmed request", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Continue to availability" }).click();
    await page.getByRole("button", { name: "Show available times" }).click();
    await page.getByRole("button", { name: "Request this lesson" }).click();
    await page.getByRole("button", { name: "Check confirmation" }).click();
    await page.getByRole("button", { name: "Add to calendar" }).click();

    await expect(page.getByRole("heading", { name: "Ready for your calendar" })).toBeVisible();
    await page.getByRole("button", { name: "Google Calendar" }).click();
    await expect(page.getByText("Added to Google Calendar.")).toBeVisible();
  });
});

