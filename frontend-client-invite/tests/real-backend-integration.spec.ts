import { expect, test } from "@playwright/test";

declare const process: { env: Record<string, string | undefined> };

const describeWhenBackendConfigured = process.env.VITE_CLIENT_INVITE_API_URL ? test.describe : test.describe.skip;

describeWhenBackendConfigured("client invite real-backend integration skeleton", () => {
  test("loads a seed invite from the public API", async ({ page }) => {
    await page.goto("/");

    await expect(page.getByRole("heading", { name: "Tell us when you can swim" })).toBeVisible();
    await expect(page.getByLabel("Client name")).toHaveValue(/.+/);
  });

  test("submits intake, searches availability, and creates an idempotent lesson request", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Continue to availability" }).click();
    await page.getByRole("button", { name: "Show available times" }).click();

    await expect(page.getByRole("heading", { name: "Pick a lesson time" })).toBeVisible();

    await page.getByRole("button", { name: "Request this lesson" }).click();
    await expect(page.getByRole("heading", { name: "Lesson request sent" })).toBeVisible();
  });

  test.fixme("surfaces stable public API errors for invalid invite tokens", async ({ page }) => {
    await page.goto("/?invite_token=invalid-token");
    await expect(page.getByRole("alert")).toContainText(/INVITE_|TOKEN_|REQUEST_FAILED/);
  });
});
