import { expect, test, type Page } from "@playwright/test";

const liveApiBase = readLiveApiBase();

test.describe("instructor command real-backend integration", () => {
  test("signs in, loads protected coach data, and signs out", async ({ page }) => {
    await signIn(page);

    await expect(page.locator("html")).toHaveAttribute("data-data-mode", "http");
    await expect(page.getByText("Mock data", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "Today schedule" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Sign out" })).toHaveCount(0);

    await page.getByRole("button", { name: "Settings" }).click();
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    const signOut = page.getByRole("button", { name: "Sign out" });
    await expect(signOut).toBeVisible();
    await expect(signOut).toHaveCSS("color", "rgb(255, 255, 255)");
    await expect(signOut).toHaveCSS("background-image", /rgb\(217, 71, 50\)/);
    await signOut.click();
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

  test("books a safe slot and independently reads back its exact appointment ID", async ({ page, request }) => {
    const accessToken = await signIn(page);
    await page.getByRole("button", { name: "Find" }).click();

    const bookingResponsePromise = page.waitForResponse((response) =>
      response.request().method() === "POST" && response.url().endsWith("/coach/bookings"),
    );
    await page.getByRole("button", { name: "Book lesson" }).first().click();
    const bookingResponse = await bookingResponsePromise;
    const bookingBody = await bookingResponse.text();
    expect(bookingResponse.ok(), bookingBody).toBeTruthy();
    const booking = JSON.parse(bookingBody) as { appointment_id: string };
    const bookingRequest = bookingResponse.request().postDataJSON() as { start_time: string };

    const dialog = page.getByRole("dialog", { name: "Ready for your calendar" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("link", { name: /Google Calendar/ })).toHaveAttribute(
      "href",
      /^https:\/\/calendar\.google\.com\/calendar\/render/,
    );

    const scheduleResponse = await request.get(
      `${liveApiBase}/coach/schedule?date=${encodeURIComponent(bookingRequest.start_time.slice(0, 10))}`,
      { headers: { authorization: `Bearer ${accessToken}` } },
    );
    const scheduleBody = await scheduleResponse.text();
    expect(scheduleResponse.ok(), scheduleBody).toBeTruthy();
    const schedule = JSON.parse(scheduleBody) as { appointments: Array<{ id: string }> };
    expect(schedule.appointments.map((appointment) => appointment.id)).toContain(booking.appointment_id);

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

async function signIn(page: Page): Promise<string> {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /locked|sign in/i })).toBeVisible();
  const signInResponsePromise = page.waitForResponse((response) =>
    response.request().method() === "POST" && response.url().endsWith("/auth/coach/sign-in"),
  );
  await page.getByLabel("Email").fill(process.env.VITE_INSTRUCTOR_E2E_EMAIL ?? "coach@example.com");
  await page.getByLabel("Password").fill(process.env.VITE_INSTRUCTOR_E2E_PASSWORD ?? "swim-dev-pass");
  await page.getByRole("button", { name: "Sign in" }).click();
  const signInResponse = await signInResponsePromise;
  const signInBody = await signInResponse.text();
  expect(signInResponse.ok(), signInBody).toBeTruthy();
  const session = JSON.parse(signInBody) as { access_token: string };
  return session.access_token;
}

function readLiveApiBase(): string {
  const value = process.env.VITE_INSTRUCTOR_API_URL?.trim();
  if (!value) {
    throw new Error("Instructor live Playwright requires VITE_INSTRUCTOR_API_URL.");
  }
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("Instructor live Playwright requires an absolute HTTP(S) VITE_INSTRUCTOR_API_URL.");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("Instructor live Playwright requires an absolute HTTP(S) VITE_INSTRUCTOR_API_URL.");
  }
  return value.replace(/\/+$/, "");
}
