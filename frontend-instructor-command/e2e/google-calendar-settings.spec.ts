import { expect, type Page, type Request, type Route, test } from "@playwright/test";
import { candidateSlots, providerConfiguration, snapshot } from "../src/api/mockData";
import type { GoogleCalendarConnection } from "../src/api/types";

const calendarPath = "/api/coach/calendar-connections/google";
const googleAuthorization = "https://accounts.google.com/o/oauth2/v2/auth?state=local-state";
const sessionCookie = "swim_session=calendar-settings-session";
const switchedSessionCookie = "swim_session=calendar-settings-switched-session";
const switchedInstructorId = "inst_calendar_switched";
const apiError = { error: { code: "TEST_ERROR", message: "Backend debug detail" } };
const ownerChangedError = {
  error: { code: "CALENDAR_CONNECTION_OWNER_CHANGED", message: "Backend owner change detail" },
};

type CalendarState = GoogleCalendarConnection["state"];
type CalendarMethod = "GET" | "POST" | "DELETE";
type RouteResponse = NonNullable<Parameters<Route["fulfill"]>[0]>;

test("connects from mobile Settings and clears cached Find results after disconnect", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 320, height: 844 });
  const api = await installFakeCalendarProvider(page, baseURL);
  const initialStatus = deferredResponse();
  api.replyNext("GET", initialStatus.response);

  await page.goto("/");
  await expect(page.getByRole("region", { name: "Today schedule", exact: true })).toBeVisible();
  expect(api.calendarRequests).toHaveLength(0);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const card = page.getByRole("region", { name: "Google Calendar" });
  await expect(card.getByRole("status")).toHaveText("Checking Google Calendar connection…");
  await expect(card.getByRole("button")).toHaveCount(0);
  initialStatus.release({ json: connectionStatus("disconnected") });
  await expect(card.getByRole("status")).toHaveText("Disconnected");

  const connect = card.getByRole("button", { name: "Connect Google Calendar", exact: true });
  expect((await connect.boundingBox())?.height).toBeGreaterThanOrEqual(44);
  await connect.click();
  await expect(card.getByRole("status")).toHaveText("Connected");
  await expect(page).toHaveURL(new URL("/?view=calendar#settings", baseURL).href);
  expect(api.googleVisits).toEqual([googleAuthorization]);
  expect(api.calendarRequests.map((request) => request.method())).toEqual(["GET", "POST", "GET"]);

  await page.getByRole("button", { name: "Find", exact: true }).click();
  const book = page.getByRole("button", { name: "Book lesson" });
  await expect(book).toHaveAttribute("data-slot-id", "slot_202608271615000400_0");
  await page.getByRole("button", { name: "Settings", exact: true }).click();

  const disconnect = card.getByRole("button", { name: "Disconnect Google Calendar" });
  api.replyNext("DELETE", { status: 503, json: apiError });
  await disconnect.click();
  await expect(card.getByRole("alert")).toContainText("Try disconnecting again");
  await expect(card.getByRole("status")).toHaveText("Connected");

  const previousAvailabilityReads = api.availabilityReads();
  await disconnect.click();
  await expect(card.getByRole("status")).toHaveText("Disconnected");
  await page.getByRole("button", { name: "Find", exact: true }).click();
  await expect(book).toHaveCount(0);
  expect(api.availabilityReads()).toBe(previousAvailabilityReads);
  await page.getByRole("button", { name: "Find safe starts", exact: true }).click();
  await expect(book).toHaveAttribute("data-slot-id", "slot_202608271515000400_0");
  expect(api.availabilityReads()).toBe(previousAvailabilityReads + 1);
});

for (const connection of ["disconnected", "connected"] as const) {
  test(`browser Back from unfinished consent allows another attempt when ${connection}`, async ({ page, baseURL }) => {
    const api = await installFakeCalendarProvider(page, baseURL, { connection });
    api.pauseConsent();
    const isConnected = connection === "connected";

    await page.goto(isConnected ? "/?calendar=failed" : "/");
    if (!isConnected) await page.getByRole("button", { name: "Settings", exact: true }).click();
    const card = page.getByRole("region", { name: "Google Calendar" });
    const authorize = card.getByRole("button", {
      name: isConnected ? "Reconnect Google Calendar" : "Connect Google Calendar",
      exact: true,
    });
    await authorize.click();
    await expect(page.getByRole("heading", { name: "Fake Google consent" })).toBeVisible();
    await expect(page).toHaveURL(googleAuthorization);

    await page.goBack();
    await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible();
    await expect(card.getByRole("status")).toHaveText(isConnected ? "Connected" : "Disconnected");
    expect(api.sessionReads()).toBe(2);
    expect(api.calendarRequests.map((request) => request.method())).toEqual(["GET", "POST", "GET"]);

    api.returnFromGoogle("connected", "connected");
    await authorize.click();
    await expect(page).toHaveURL(new URL("/?view=calendar#settings", baseURL).href);
    await expect(card.getByRole("status")).toHaveText("Connected");
    expect(api.googleVisits).toEqual([googleAuthorization, googleAuthorization]);
  });
}

test("a callback survives sign-in and only reports a connection confirmed by status", async ({ page, baseURL }) => {
  const api = await installFakeCalendarProvider(page, baseURL, { signedIn: false });
  const returnPath = "/?calendar=view&date=2026-08-27&calendar=connected&keep=here#settings";
  await page.goto(returnPath);
  await expect(page.getByRole("heading", { name: "Dayweave locked" })).toBeVisible();
  await expect(page).toHaveURL(new URL(returnPath, baseURL).href);
  expect(api.calendarRequests).toHaveLength(0);

  await signIn(page);
  await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible();
  const card = page.getByRole("region", { name: "Google Calendar" });
  await expect(card.getByRole("status")).toHaveText("Disconnected");
  await expect(card.getByRole("alert")).toContainText("couldn’t confirm the Google Calendar connection");
  await expect(card.getByRole("button", { name: "Connect Google Calendar", exact: true })).toBeEnabled();
  await expect(page).toHaveURL(new URL("/?calendar=view&date=2026-08-27&keep=here#settings", baseURL).href);
  expect(api.calendarRequests.map((request) => request.method())).toEqual(["GET"]);
});

test("retries status, rejects an unsafe authorization URL, and recovers from a failed Google return", async ({ page, baseURL }) => {
  const api = await installFakeCalendarProvider(page, baseURL, { connection: "authorization_required" });
  api.replyNext("GET", { status: 503, json: apiError });

  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const card = page.getByRole("region", { name: "Google Calendar" });
  await expect(card.getByRole("alert")).toContainText("Retry status");
  await card.getByRole("button", { name: "Retry status" }).click();
  await expect(card.getByRole("status")).toHaveText("Reconnect required");
  await expect(card.getByRole("button", { name: "Disconnect Google Calendar" })).toBeEnabled();

  const reconnect = card.getByRole("button", { name: "Reconnect Google Calendar" });
  api.replyNext("POST", {
    json: { authorization_url: "https://accounts.google.com.example.test/o/oauth2/v2/auth?state=unsafe" },
  });
  await reconnect.click();
  await expect(card.getByRole("alert")).toContainText("couldn’t open Google Calendar authorization");
  await expect(reconnect).toBeFocused();
  expect(api.googleVisits).toHaveLength(0);
  expect(api.unexpectedExternalRequests).toHaveLength(0);

  api.returnFromGoogle("failed", "authorization_required");
  await reconnect.click();
  await expect(card.getByRole("status")).toHaveText("Reconnect required");
  await expect(card.getByRole("alert")).toContainText("authorization didn’t finish");
  await expect(page).toHaveURL(new URL("/?view=calendar#settings", baseURL).href);
  await expect(card.getByText(apiError.error.message)).toHaveCount(0);

  api.returnFromGoogle("connected", "connected");
  await reconnect.click();
  await expect(card.getByRole("status")).toHaveText("Connected");
  await expect(card.getByRole("alert")).toHaveCount(0);
});

test("a stale Settings tab clears Calendar state after another provider takes over the session", async ({ page, baseURL }) => {
  const api = await installFakeCalendarProvider(page, baseURL, { connection: "connected" });
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const card = page.getByRole("region", { name: "Google Calendar" });
  await expect(card.getByRole("status")).toHaveText("Connected");

  await api.switchProvider();
  const rejected = page.waitForResponse((response) => (
    new URL(response.url()).pathname === calendarPath && response.request().method() === "DELETE"
  ));
  await card.getByRole("button", { name: "Disconnect Google Calendar" }).click();
  const response = await rejected;
  expect(response.status()).toBe(409);
  expect(await response.request().headerValue("cookie")).toContain(switchedSessionCookie);
  expect(await response.request().headerValue("x-expected-instructor-id"))
    .toBe(encodeURIComponent(snapshot.instructor.id));

  await expect(card.getByRole("alert")).toContainText("Reload Dayweave and sign in if prompted");
  await expect(card.getByRole("status")).toHaveCount(0);
  await expect(card.getByRole("button")).toHaveCount(1);
  await expect(card.getByText(ownerChangedError.error.message)).toHaveCount(0);

  await page.getByRole("button", { name: "Schedule", exact: true }).click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(card.getByRole("button", { name: "Reload Dayweave" })).toBeEnabled();
  expect(api.calendarRequests).toHaveLength(2);

  await Promise.all([
    page.waitForEvent("domcontentloaded"),
    card.getByRole("button", { name: "Reload Dayweave" }).click(),
  ]);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(card.getByRole("status")).toHaveText("Disconnected");
  expect(api.calendarRequests.map((request) => request.method())).toEqual(["GET", "DELETE", "GET"]);
  expect(await api.calendarRequests[2].headerValue("x-expected-instructor-id")).toBe(switchedInstructorId);
});

test("a successful disconnect never searches Find using a newly switched session", async ({ page, baseURL }) => {
  const api = await installFakeCalendarProvider(page, baseURL, { connection: "connected" });
  const pending = deferredResponse();
  api.replyNext("DELETE", pending.response);

  await page.goto("/");
  await page.getByRole("button", { name: "Find", exact: true }).click();
  await expect(page.getByRole("button", { name: "Book lesson" })).toBeVisible();
  const previousAvailabilityReads = api.availabilityReads();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const card = page.getByRole("region", { name: "Google Calendar" });
  await card.getByRole("button", { name: "Disconnect Google Calendar" }).click();
  await expect.poll(() => api.calendarRequests.map((request) => request.method())).toEqual(["GET", "DELETE"]);
  await api.switchProvider();
  pending.release({ json: connectionStatus("disconnected") });

  await expect(card.getByRole("status")).toHaveText("Disconnected");
  await page.getByRole("button", { name: "Find", exact: true }).click();
  await expect(page.getByRole("button", { name: "Book lesson" })).toHaveCount(0);
  expect(api.availabilityReads()).toBe(previousAvailabilityReads);
});

for (const [method, lateReply] of [
  ["GET", { status: 401, json: apiError }],
  ["POST", { json: { authorization_url: googleAuthorization } }],
  ["DELETE", { json: connectionStatus("disconnected") }],
] as const) {
  test(`ignores a late ${method} response after signing out and back in`, async ({ page, baseURL }) => {
    const connection = method === "DELETE" ? "connected" : "disconnected";
    const api = await installFakeCalendarProvider(page, baseURL, { connection });
    const pending = deferredResponse();
    api.replyNext(method, pending.response);

    await page.goto("/");
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    const card = page.getByRole("region", { name: "Google Calendar" });
    if (method === "POST") {
      await card.getByRole("button", { name: "Connect Google Calendar", exact: true }).click();
      await expect(card.getByRole("button", { name: "Connect Google Calendar", exact: true })).toBeDisabled();
    }
    if (method === "DELETE") {
      await card.getByRole("button", { name: "Disconnect Google Calendar" }).click();
      await expect(card.getByRole("button", { name: "Disconnect Google Calendar" })).toBeDisabled();
    }
    const requestsForMethod = () => api.calendarRequests.filter((request) => request.method() === method);
    await expect.poll(() => requestsForMethod().length).toBe(1);
    const [staleRequest] = requestsForMethod();

    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Dayweave locked" })).toBeVisible();
    await signIn(page);
    const currentStatus = connection === "connected" ? "Connected" : "Disconnected";
    await expect(card.getByRole("status")).toHaveText(currentStatus);

    const staleRequestFinished = page.waitForEvent("requestfinished", { predicate: (request) => request === staleRequest });
    pending.release(lateReply);
    await staleRequestFinished;
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(card.getByRole("status")).toHaveText(currentStatus);
    await expect(page).toHaveURL(new URL("/", baseURL).href);
    expect(api.googleVisits).toHaveLength(0);
  });
}

async function signIn(page: Page): Promise<void> {
  await page.getByLabel("Email").fill("coach@example.test");
  await page.getByLabel("Password").fill("local-test-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
}

async function installFakeCalendarProvider(
  page: Page,
  baseURL: string | undefined,
  options: { signedIn?: boolean; connection?: CalendarState } = {},
) {
  const appUrl = new URL("/", baseURL);
  const calendarRequests: Request[] = [];
  const googleVisits: string[] = [];
  const unexpectedExternalRequests: string[] = [];
  const nextReplies = new Map<CalendarMethod, RouteResponse | Promise<RouteResponse>>();
  let activeSessionCookie = sessionCookie;
  let connection = options.connection ?? "disconnected";
  let sessionReads = 0;
  let availabilityReads = 0;
  let googleReturn: { result: "connected" | "failed"; status: CalendarState } | null = {
    result: "connected",
    status: "connected",
  };
  const session = {
    authenticated: true,
    coach: { instructor_id: snapshot.instructor.id, display_name: providerConfiguration.display_name },
    expires_at: "2099-08-27T23:00:00Z",
  };

  async function setActiveSessionCookie(): Promise<void> {
    const [name, value] = activeSessionCookie.split("=");
    await page.context().addCookies([{
      name,
      value,
      domain: appUrl.hostname,
      path: "/api",
      httpOnly: true,
      sameSite: "Strict",
    }]);
  }
  if (options.signedIn !== false) await setActiveSessionCookie();

  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (url.href === googleAuthorization && request.isNavigationRequest()) {
      googleVisits.push(url.href);
      if (!googleReturn) {
        return route.fulfill({ contentType: "text/html", body: "<h1>Fake Google consent</h1>" });
      }
      connection = googleReturn.status;
      const callback = new URL("/?view=calendar#settings", appUrl);
      callback.searchParams.set("calendar", googleReturn.result);
      return route.fulfill({ status: 302, headers: { location: callback.href } });
    }
    if (url.origin !== appUrl.origin) {
      unexpectedExternalRequests.push(url.href);
      return route.abort();
    }
    if (!path.startsWith("/api/")) return route.continue();

    if (path === "/api/auth/coach/sign-in") {
      return route.fulfill({
        json: session,
        headers: { "set-cookie": `${activeSessionCookie}; Path=/api; HttpOnly; SameSite=Strict` },
      });
    }
    if (path === "/api/auth/coach/sign-out") {
      return route.fulfill({
        status: 204,
        headers: { "set-cookie": "swim_session=; Path=/api; HttpOnly; SameSite=Strict; Max-Age=0" },
      });
    }
    if (path === "/api/auth/coach/session") sessionReads += 1;
    const cookie = await request.headerValue("cookie");
    if (!cookie?.includes(activeSessionCookie)) return route.fulfill({ status: 401, json: apiError });

    if (path === "/api/auth/coach/session") return route.fulfill({ json: session });
    if (path === "/api/coach/provider") return route.fulfill({ json: providerConfiguration });
    if (path === "/api/coach/command-center/snapshot") {
      return route.fulfill({ json: { ...snapshot, date: url.searchParams.get("date") ?? snapshot.date } });
    }
    if (path === "/api/coach/availability") {
      availabilityReads += 1;
      return route.fulfill({ json: availability(connection) });
    }
    if (
      (path === calendarPath && (method === "GET" || method === "DELETE"))
      || (path === `${calendarPath}/authorize` && method === "POST")
    ) {
      calendarRequests.push(request);
      const expectedInstructorId = await request.headerValue("x-expected-instructor-id");
      if (expectedInstructorId !== encodeURIComponent(session.coach.instructor_id)) {
        return route.fulfill({ status: 409, json: ownerChangedError });
      }
      const reply = nextReplies.get(method);
      nextReplies.delete(method);
      if (reply) return route.fulfill(await reply);
      if (method === "POST") return route.fulfill({ json: { authorization_url: googleAuthorization } });
      if (method === "DELETE") connection = "disconnected";
      return route.fulfill({ json: connectionStatus(connection) });
    }
    return route.fulfill({ status: 404, json: apiError });
  });

  return {
    calendarRequests,
    googleVisits,
    unexpectedExternalRequests,
    switchProvider: async () => {
      activeSessionCookie = switchedSessionCookie;
      session.coach.instructor_id = switchedInstructorId;
      connection = "disconnected";
      await setActiveSessionCookie();
    },
    replyNext: (method: CalendarMethod, reply: RouteResponse | Promise<RouteResponse>) => nextReplies.set(method, reply),
    pauseConsent: () => { googleReturn = null; },
    returnFromGoogle: (result: "connected" | "failed", status: CalendarState) => { googleReturn = { result, status }; },
    sessionReads: () => sessionReads,
    availabilityReads: () => availabilityReads,
  };
}

function connectionStatus(state: CalendarState): GoogleCalendarConnection {
  const timestamp = "2026-08-27T12:00:00Z";
  return {
    provider: "google",
    state,
    connected_at: state === "disconnected" ? null : timestamp,
    updated_at: state === "disconnected" ? null : timestamp,
    authorization_required_at: state === "authorization_required" ? timestamp : null,
  };
}

function availability(connection: CalendarState) {
  const hour = connection === "disconnected" ? 15 : 16;
  return {
    status: "PRESENTING",
    location: candidateSlots[0].target_location,
    available_slots: [{
      start_iso: `2026-08-27T${hour}:15:00-04:00`,
      end_iso: `2026-08-27T${hour}:45:00-04:00`,
      travel_time_mins: 15,
      buffer_mins: 10,
      distance_miles: 5,
    }],
  };
}

function deferredResponse(): { response: Promise<RouteResponse>; release: (response: RouteResponse) => void } {
  let release!: (response: RouteResponse) => void;
  const response = new Promise<RouteResponse>((resolve) => { release = resolve; });
  return { response, release };
}
