import {
  expect,
  request as playwrightRequest,
  test,
  type APIRequestContext,
  type APIResponse,
  type Page,
  type TestInfo,
} from "@playwright/test";
import type {
  CalendarLinks,
  CoachLessonRequestConfirmResponse,
  ProviderConfiguration,
} from "../src/api/types";

const liveApiBase = readLiveApiBase();
const publicApiBase = `${liveApiBase}/public`;

type LiveSlot = {
  id: string;
  start_iso: string;
  end_iso: string;
};

type LiveLessonRequest = {
  id: string;
  status: "requested" | "confirmed" | "needs_new_times" | "expired";
  slot: LiveSlot;
  alternative_slots: LiveSlot[];
  confirmation: {
    calendar_links: CalendarLinks;
  } | null;
};

type SelectedAlternativeSetup = {
  clientName: string;
  requestToken: string;
  selectedSlot: LiveSlot;
};

type SessionCookie = {
  name: string;
  value: string;
  domain: string;
  path: string;
  expires: number;
  httpOnly: boolean;
  secure: boolean;
  sameSite: "Strict" | "Lax" | "None";
};

test.describe("instructor command real-backend integration", () => {
  test("signs in, loads protected coach data, and signs out", async ({ page }) => {
    const { cookie } = await signIn(page);

    await expect(page.locator("html")).toHaveAttribute("data-data-mode", "http");
    await expect(page.getByText("Mock data", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "Today schedule" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Sign out" })).toHaveCount(0);

    const apiOrder: string[] = [];
    let sessionValidated = false;
    let coachRequestStartedBeforeValidation = false;
    const recordResponse = (response: { url(): string }) => {
      if (new URL(response.url()).pathname === "/api/auth/coach/session") sessionValidated = true;
    };
    const recordRequest = (request: { url(): string }) => {
      const pathname = new URL(request.url()).pathname;
      if (!pathname.startsWith("/api/")) return;
      apiOrder.push(pathname);
      if (pathname.startsWith("/api/coach/") && !sessionValidated) {
        coachRequestStartedBeforeValidation = true;
      }
    };
    page.on("response", recordResponse);
    page.on("request", recordRequest);
    await page.reload();
    await expect(page.getByRole("heading", { name: "Today schedule" })).toBeVisible();
    page.off("response", recordResponse);
    page.off("request", recordRequest);

    expect(apiOrder[0]).toBe("/api/auth/coach/session");
    expect(coachRequestStartedBeforeValidation).toBeFalsy();

    await page.getByRole("button", { name: "Settings" }).click();
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    const signOut = page.getByRole("button", { name: "Sign out" });
    await expect(signOut).toBeVisible();
    await expect(signOut).toHaveCSS("color", "rgb(255, 255, 255)");
    await expect(signOut).toHaveCSS("background-image", /rgb\(217, 71, 50\)/);
    const signOutResponsePromise = page.waitForResponse((response) =>
      response.request().method() === "POST" && response.url().endsWith("/auth/coach/sign-out"),
    );
    await signOut.click();
    const signOutResponse = await signOutResponsePromise;
    expect(signOutResponse.ok()).toBeTruthy();
    await expect(page.getByRole("heading", { name: /locked|sign in/i })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Today schedule" })).toHaveCount(0);
    expect((await page.context().cookies()).filter(({ name }) => name === "swim_session")).toHaveLength(0);

    const replay = await playwrightRequest.newContext({
      extraHTTPHeaders: { Cookie: `${cookie.name}=${cookie.value}` },
    });
    try {
      for (const path of ["/auth/coach/session", "/coach/me"]) {
        const response = await replay.get(`${liveApiBase}${path}`);
        expect(response.status()).toBe(401);
        expect(await response.json()).toMatchObject({ error: { code: "INVALID_AUTH_TOKEN" } });
      }
    } finally {
      await replay.dispose();
    }
  });

  test("loads coach schedule data and searches availability through /api/coach", async ({ page }) => {
    await signIn(page);

    await page.getByRole("button", { name: "Find" }).click();
    await expect(page.getByRole("heading", { name: "Availability finder" })).toBeVisible();

    await page.getByRole("button", { name: "Find safe starts" }).click();
    await expect(page.getByRole("heading", { name: "Availability finder" })).toBeVisible();
    await expect(page.locator("main")).toContainText(/safe options|No safe starts/i);
  });

  test("saves and reloads provider configuration without mobile overflow", async ({ page }) => {
    await signIn(page);
    await page.getByRole("button", { name: "Settings" }).click();
    const displayName = await page.getByLabel("Display name").inputValue();
    const putResponsePromise = page.waitForResponse((response) =>
      response.request().method() === "PUT" && response.url().endsWith("/coach/provider"),
    );
    await page.getByRole("button", { name: "Save provider settings" }).click();
    const putResponse = await putResponsePromise;
    const saved = await jsonResponse<ProviderConfiguration>(putResponse, "provider settings save");
    expect(saved.working_hours).toHaveLength(7);
    expect(saved.display_name).toBe(displayName);
    await expect(page.locator("[data-provider-save-status]")).toContainText("Provider settings saved");

    await page.reload();
    await page.getByRole("button", { name: "Settings" }).click();
    await expect(page.getByLabel("Display name")).toHaveValue(displayName);
    await expect(page.getByRole("switch")).toHaveCount(7);

    for (const width of [375, 320]) {
      await page.setViewportSize({ width, height: 720 });
      const overflow = await page.evaluate(() => Math.max(
        document.documentElement.scrollWidth - document.documentElement.clientWidth,
        document.body.scrollWidth - document.body.clientWidth,
      ));
      expect(overflow).toBeLessThanOrEqual(1);
      for (const control of await page.locator("[data-provider-form] input, [data-provider-form] select, [data-provider-form] button").all()) {
        expect((await control.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);
      }
    }
  });

  test("books a safe slot and independently reads back its exact appointment ID", async ({ page }) => {
    await signIn(page);
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

    const scheduleResponse = await page.context().request.get(
      `${liveApiBase}/coach/schedule?date=${encodeURIComponent(bookingRequest.start_time.slice(0, 10))}`,
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

  test("confirms the exact client-selected alternative and independently verifies its occurrence", async ({
    page,
    request,
  }, testInfo) => {
    await signIn(page);
    const coachRequest = page.context().request;
    const setup = await createAndSelectAlternative(request, coachRequest, testInfo);

    // Reload so the authenticated command-center snapshot includes the request created above.
    await page.reload();
    await expect(page.getByRole("heading", { name: "Today schedule" })).toBeVisible();
    await page.getByRole("button", { name: "Requests", exact: true }).click();
    const requestCard = page.locator("article.request-card").filter({ hasText: setup.clientName });
    await expect(requestCard).toHaveCount(1);

    const confirmResponsePromise = page.waitForResponse((response) =>
      response.request().method() === "POST"
      && new URL(response.url()).pathname.endsWith(
        `/api/coach/lesson-requests/${encodeURIComponent(setup.requestToken)}/confirm`,
      ),
    );
    await requestCard.getByRole("button", { name: "Confirm", exact: true }).click();
    const confirmResponse = await confirmResponsePromise;
    const confirmation = await jsonResponse<CoachLessonRequestConfirmResponse>(
      confirmResponse,
      "UI confirmation",
    );

    expect(confirmResponse.request().postDataJSON()).toEqual({ slot_id: setup.selectedSlot.id });
    expect(confirmation.status).toBe("confirmed");
    expect(confirmation.appointment_id).toBeTruthy();
    expect(confirmation.appointment_ids).toContain(confirmation.appointment_id);
    expect(confirmation.calendar_links).not.toBeNull();

    const dialog = page.getByRole("dialog", { name: "Ready for your calendar" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("link", { name: /Google Calendar/ })).toHaveAttribute(
      "href",
      /^https:\/\/calendar\.google\.com\/calendar\/render/,
    );
    await expect(dialog).toContainText("does not add the appointment automatically");

    const selectedStart = canonicalInstant(setup.selectedSlot.start_iso);
    const selectedEnd = canonicalInstant(setup.selectedSlot.end_iso);
    const occurrenceDate = setup.selectedSlot.start_iso.slice(0, 10);
    const schedule = await getJson<{
      appointments: Array<{ id: string; start_time: string; end_time: string }>;
    }>(
      coachRequest,
      `${liveApiBase}/coach/schedule?date=${encodeURIComponent(occurrenceDate)}`,
      "coach schedule read-back",
    );
    const appointment = schedule.appointments.find(({ id }) => id === confirmation.appointment_id);
    expect(appointment, "confirmation appointment must be independently readable from coach schedule").toBeTruthy();
    expect(canonicalInstant(appointment!.start_time)).toBe(selectedStart);
    expect(canonicalInstant(appointment!.end_time)).toBe(selectedEnd);

    const coachCalendar = await getJson<CalendarLinks>(
      coachRequest,
      `${liveApiBase}/coach/appointments/${encodeURIComponent(confirmation.appointment_id!)}/calendar-links`,
      "coach calendar read-back",
    );
    expect(coachCalendar.google_events.map(eventInstants)).toEqual([
      { start: selectedStart, end: selectedEnd },
    ]);
    expect(confirmation.calendar_links!.google_events.map(eventInstants)).toEqual(
      coachCalendar.google_events.map(eventInstants),
    );

    const publicStatus = await getPublicLessonRequest(request, setup.requestToken);
    expect(publicStatus.status).toBe("confirmed");
    expect(canonicalInstant(publicStatus.slot.start_iso)).toBe(selectedStart);
    expect(canonicalInstant(publicStatus.slot.end_iso)).toBe(selectedEnd);
    expect(publicStatus.confirmation).not.toBeNull();
    expect(publicStatus.confirmation!.calendar_links.google_events.map(eventInstants)).toEqual([
      { start: selectedStart, end: selectedEnd },
    ]);
  });
});

async function createAndSelectAlternative(
  publicRequest: APIRequestContext,
  coachRequest: APIRequestContext,
  testInfo: TestInfo,
): Promise<SelectedAlternativeSetup> {
  const inviteToken = process.env.VITE_INSTRUCTOR_E2E_INVITE_TOKEN ?? "invite_sofia_0826";
  const { requestedDate, day } = isolatedDateForProject(testInfo.project.name);
  const runKey = `${testInfo.project.name}-${Date.now().toString(36)}`.replace(/[^a-z0-9-]/gi, "-");
  const clientName = `Live alternative ${runKey}`;
  const search = await postPublicJson<{ slots: LiveSlot[] }>(
    publicRequest,
    `${publicApiBase}/client-invites/${encodeURIComponent(inviteToken)}/slots`,
    {
      client_name: clientName,
      location: {
        address: "820 Lakeview Dr, Austin, TX",
        lat: 30.289,
        lng: -97.739,
        is_instructor_facility: false,
      },
      requested_date: requestedDate,
      duration_mins: 45,
      recurrence: "single",
      windows: [{
        id: `live-${runKey}`,
        days: [day],
        start_time: "08:00",
        end_time: "17:00",
      }],
    },
    "availability search",
  );
  expect(search.slots.length, "live setup needs an original slot and an alternative candidate").toBeGreaterThanOrEqual(2);

  const created = await postPublicJson<LiveLessonRequest>(
    publicRequest,
    `${publicApiBase}/client-invites/${encodeURIComponent(inviteToken)}/lesson-requests`,
    {
      slot_id: search.slots[0].id,
      idempotency_key: `live-request-${runKey}`,
    },
    "lesson request creation",
  );
  expect(created.status).toBe("requested");

  await postCoachJson(
    coachRequest,
    `${liveApiBase}/coach/lesson-requests/${encodeURIComponent(created.id)}/alternatives`,
    {
      slot_ids: [search.slots[1].id],
      message: "Choose this verified live-test alternative.",
    },
    "alternative promotion",
  );

  const offered = await getPublicLessonRequest(publicRequest, created.id);
  expect(offered.status).toBe("needs_new_times");
  expect(offered.alternative_slots).toHaveLength(1);
  const selectedSlot = offered.alternative_slots[0];
  expect(selectedSlot.id).not.toBe(search.slots[1].id);

  const selected = await postPublicJson<LiveLessonRequest>(
    publicRequest,
    `${publicApiBase}/lesson-requests/${encodeURIComponent(created.id)}/select-alternative`,
    {
      slot_id: selectedSlot.id,
      idempotency_key: `live-select-${runKey}`,
    },
    "alternative selection",
  );
  expect(selected.status).toBe("requested");
  expect(selected.slot.id).toBe(selectedSlot.id);
  expect(canonicalInstant(selected.slot.start_iso)).toBe(canonicalInstant(selectedSlot.start_iso));
  expect(canonicalInstant(selected.slot.end_iso)).toBe(canonicalInstant(selectedSlot.end_iso));

  return {
    clientName,
    requestToken: created.id,
    selectedSlot,
  };
}

async function getPublicLessonRequest(
  request: APIRequestContext,
  requestToken: string,
): Promise<LiveLessonRequest> {
  return getJson<LiveLessonRequest>(
    request,
    `${publicApiBase}/lesson-requests/${encodeURIComponent(requestToken)}`,
    "public lesson-request read-back",
  );
}

async function getJson<T>(
  request: APIRequestContext,
  url: string,
  label: string,
): Promise<T> {
  const response = await request.get(url);
  return jsonResponse<T>(response, label);
}

async function postPublicJson<T = unknown>(
  request: APIRequestContext,
  url: string,
  data: unknown,
  label: string,
): Promise<T> {
  const response = await request.post(url, { data });
  return jsonResponse<T>(response, label);
}

async function postCoachJson<T = unknown>(
  request: APIRequestContext,
  url: string,
  data: unknown,
  label: string,
): Promise<T> {
  const response = await request.post(url, {
    data,
    headers: { "X-CSRF-Protection": "1" },
  });
  return jsonResponse<T>(response, label);
}

async function jsonResponse<T>(response: APIResponse, label: string): Promise<T> {
  const body = await response.text();
  expect(response.ok(), `${label} failed (${response.status()}): ${body}`).toBeTruthy();
  return JSON.parse(body) as T;
}

function eventInstants(event: { start_iso: string; end_iso: string }): { start: string; end: string } {
  return {
    start: canonicalInstant(event.start_iso),
    end: canonicalInstant(event.end_iso),
  };
}

function canonicalInstant(value: string): string {
  const parsed = new Date(value);
  expect(Number.isNaN(parsed.getTime()), `invalid ISO timestamp: ${value}`).toBeFalsy();
  return parsed.toISOString();
}

function isolatedDateForProject(projectName: string): { requestedDate: string; day: string } {
  const configured = process.env.VITE_INSTRUCTOR_COMMAND_DATE?.trim();
  const base = configured
    ? new Date(`${configured}T12:00:00Z`)
    : new Date(Date.now() + 7 * 86_400_000);
  if (Number.isNaN(base.getTime())) {
    throw new Error("VITE_INSTRUCTOR_COMMAND_DATE must be an ISO calendar date.");
  }
  const projectOffset = projectName.includes("mobile") ? 35 : 0;
  base.setUTCDate(base.getUTCDate() + projectOffset);
  const day = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][base.getUTCDay()];
  return { requestedDate: base.toISOString().slice(0, 10), day };
}

async function signIn(page: Page): Promise<{ cookie: SessionCookie }> {
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
  const session = JSON.parse(signInBody) as Record<string, unknown>;
  expect(session).toMatchObject({ authenticated: true, coach: { instructor_id: "inst_001" } });
  expect(session).not.toHaveProperty("access_token");
  expect(session).not.toHaveProperty("token_type");

  const setCookie = (await signInResponse.allHeaders())["set-cookie"] ?? "";
  expect(setCookie).toContain("swim_session=");
  expect(setCookie).toMatch(/HttpOnly/i);
  expect(setCookie).toMatch(/SameSite=Strict/i);
  expect(setCookie).toMatch(/Path=\/api/i);
  expect(setCookie).toMatch(/Max-Age=/i);
  expect(setCookie).toMatch(/Expires=/i);

  const cookies = await page.context().cookies(`${new URL(liveApiBase).origin}/api`);
  const cookie = cookies.find(({ name }) => name === "swim_session") as SessionCookie | undefined;
  expect(cookie).toBeDefined();
  expect(cookie!.httpOnly).toBeTruthy();
  expect(cookie!.sameSite).toBe("Strict");
  expect(cookie!.path).toBe("/api");
  expect(cookie!.secure).toBe(new URL(liveApiBase).protocol === "https:");
  expect(cookie!.domain.startsWith(".")).toBeFalsy();
  expect(cookie!.expires).toBeGreaterThan(Date.now() / 1000);

  const browserSecrets = await page.evaluate(() => ({
    cookie: document.cookie,
    legacySession: window.localStorage.getItem("swim-command:coach-session:v1"),
    localValues: Object.values(window.localStorage),
    sessionValues: Object.values(window.sessionStorage),
  }));
  expect(browserSecrets.cookie).not.toContain("swim_session");
  expect(browserSecrets.legacySession).toBeNull();
  expect([...browserSecrets.localValues, ...browserSecrets.sessionValues]).not.toContain(cookie!.value);

  return { cookie: cookie! };
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
