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

  await page.getByRole("button", { name: "Requests" }).click();
  await expect(page.getByRole("heading", { name: "Request management" })).toBeVisible();

  await page.getByRole("button", { name: "Settings" }).click();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
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

  test.fixme("books a held slot and refreshes the backend schedule", async ({ page }) => {
    await signIn(page);
    await page.getByRole("button", { name: "Find" }).click();
    await page.getByRole("button", { name: "Hold" }).first().click();

    await expect(page.getByRole("status")).toBeVisible();
  });
});

async function signIn(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /locked|sign in/i })).toBeVisible();
  await page.getByLabel("Email").fill(process.env.VITE_INSTRUCTOR_E2E_EMAIL ?? "coach@example.com");
  await page.getByLabel("Password").fill(process.env.VITE_INSTRUCTOR_E2E_PASSWORD ?? "swim-dev-pass");
  await page.getByRole("button", { name: "Sign in" }).click();
}
