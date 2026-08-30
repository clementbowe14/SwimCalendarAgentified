import { expect, test } from "../../swim-command-center/node_modules/@playwright/test/index.mjs";

test("navigates the instructor command screens with the local adapter", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Today schedule" })).toBeVisible();
  await page.getByRole("button", { name: "Find" }).click();
  await expect(page.getByRole("heading", { name: "Availability finder" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Find safe starts" })).toBeVisible();

  await page.getByRole("button", { name: "Client", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Client detail" })).toBeVisible();

  await page.getByRole("button", { name: "Requests" }).click();
  await expect(page.getByRole("heading", { name: "Request management" })).toBeVisible();

  await page.getByRole("button", { name: "Rules" }).click();
  await expect(page.getByRole("heading", { name: "Settings and rules" })).toBeVisible();
});

const describeWhenBackendConfigured = process.env.VITE_INSTRUCTOR_API_URL ? test.describe : test.describe.skip;

describeWhenBackendConfigured("instructor command real-backend integration skeleton", () => {
  test("loads coach schedule data and searches availability through /api/coach", async ({ page }) => {
    await page.goto("/");

    await expect(page.getByRole("heading", { name: "Today schedule" })).toBeVisible();
    await page.getByRole("button", { name: "Find" }).click();
    await page.getByRole("button", { name: "Find safe starts" }).click();

    await expect(page.getByRole("heading", { name: "Availability finder" })).toBeVisible();
    await expect(page.getByText(/safe options/)).toBeVisible();
  });

  test.fixme("books a held slot and refreshes the backend schedule", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Find" }).click();
    await page.getByRole("button", { name: "Hold" }).first().click();

    await expect(page.getByRole("status")).toBeVisible();
  });
});
