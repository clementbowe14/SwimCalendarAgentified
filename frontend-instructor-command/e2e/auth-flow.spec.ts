import { expect, type Page, type Route, test } from "@playwright/test";

const describeWhenLiveApiConfigured = process.env.VITE_INSTRUCTOR_API_URL ? test.describe : test.describe.skip;

const AUTH_TOKEN = "playwright-coach-token";
const COACH_EMAIL = "coach@example.com";
const COACH_PASSWORD = "correct-horse-battery-staple";
const LOCKED_OR_EXPIRED_HEADING = /sign in|locked|session expired/i;

describeWhenLiveApiConfigured("MVP coach auth flow", () => {
  test.describe.configure({ timeout: 15_000 });

  test("shows compact locked state, signs in, keeps command-center styling, and signs out", async ({ page }) => {
    const api = await mockAuthenticatedCoachApi(page);

    await page.goto("/");

    await expect(page.getByRole("heading", { name: LOCKED_OR_EXPIRED_HEADING })).toBeVisible();
    await expect(page.getByLabel(/email/i)).toBeVisible();
    await expect(page.getByLabel(/password/i)).toBeVisible();
    await expect(page.getByRole("heading", { name: "Today schedule" })).toHaveCount(0);
    expect(api.protectedCallsBeforeAuth()).toHaveLength(0);

    await page.getByLabel(/email/i).fill(COACH_EMAIL);
    await page.getByLabel(/password/i).fill(COACH_PASSWORD);
    await page.getByRole("button", { name: /sign in/i }).click();

    await expect(page.getByRole("heading", { name: "Today schedule" })).toBeVisible();
    await expect(page.locator(".command-shell")).toBeVisible();
    await expect(page.locator(".sidebar")).toBeVisible();
    await expect(page.locator(".topbar")).toBeVisible();
    await expect(page.getByRole("button", { name: /sign out/i })).toBeVisible();

    const shellColumns = await page.locator(".command-shell").evaluate((node) => {
      return window.getComputedStyle(node).gridTemplateColumns;
    });
    expect(shellColumns).toContain("280px");

    const brandRadius = await page.locator(".brand-mark").evaluate((node) => {
      return window.getComputedStyle(node).borderRadius;
    });
    expect(brandRadius).toBe("8px");

    expect(
      api.protectedCalls().every((call) => call.authorization?.toLowerCase() === `bearer ${AUTH_TOKEN}`),
    ).toBe(true);

    await page.getByRole("button", { name: /sign out/i }).click();

    await expect(page.getByRole("heading", { name: LOCKED_OR_EXPIRED_HEADING })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Today schedule" })).toHaveCount(0);
    expect(api.logoutCalls()).toBe(1);
  });

  test("keeps invalid credentials in the locked state with an accessible error", async ({ page }) => {
    await mockAuthenticatedCoachApi(page);

    await page.goto("/");
    await expect(page.getByRole("heading", { name: LOCKED_OR_EXPIRED_HEADING })).toBeVisible();
    await page.getByLabel(/email/i).fill(COACH_EMAIL);
    await page.getByLabel(/password/i).fill("wrong-password");
    await page.getByRole("button", { name: /sign in/i }).click();

    await expect(page.getByRole("alert")).toContainText(/invalid|sign in/i);
    await expect(page.getByRole("heading", { name: /sign in|locked/i })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Today schedule" })).toHaveCount(0);
  });

  for (const status of [401, 403] as const) {
    test(`clears auth and shows session-expired sign-in UI after a protected ${status}`, async ({ page }) => {
      const api = await mockAuthenticatedCoachApi(page);

      await page.goto("/");
      await expect(page.getByRole("heading", { name: LOCKED_OR_EXPIRED_HEADING })).toBeVisible();
      await page.getByLabel(/email/i).fill(COACH_EMAIL);
      await page.getByLabel(/password/i).fill(COACH_PASSWORD);
      await page.getByRole("button", { name: /sign in/i }).click();
      await expect(page.getByRole("heading", { name: "Today schedule" })).toBeVisible();

      api.expireProtectedRequestsWith(status);
      await page.getByRole("button", { name: "Find" }).click();
      await page.getByRole("button", { name: "Find safe starts" }).click();

      await expect(page.getByRole("alert")).toContainText(/session|sign in|expired/i);
      await expect(page.getByRole("heading", { name: LOCKED_OR_EXPIRED_HEADING })).toBeVisible();
      await expect(page.getByRole("heading", { name: "Availability finder" })).toHaveCount(0);
    });
  }
});

type ProtectedCall = {
  path: string;
  authorization: string | null;
  beforeAuth: boolean;
};

async function mockAuthenticatedCoachApi(page: Page): Promise<{
  expireProtectedRequestsWith(status: 401 | 403): void;
  logoutCalls(): number;
  protectedCalls(): ProtectedCall[];
  protectedCallsBeforeAuth(): ProtectedCall[];
}> {
  let token: string | null = null;
  let expiredStatus: 401 | 403 | null = null;
  let logoutCallCount = 0;
  const protectedCalls: ProtectedCall[] = [];

  await page.route(/\/api\/(?:auth|coach)\//, async (route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname.endsWith("/auth/coach/session")) {
      if (token) {
        await route.fulfill({ json: { authenticated: true, coach: authSessionResponse() } });
        return;
      }
      await route.fulfill({ status: 401, json: apiError("AUTH_REQUIRED", "Sign in is required.") });
      return;
    }

    if (url.pathname.endsWith("/auth/coach/sign-in")) {
      const body = postBody(request.postData());
      if (body.email === COACH_EMAIL && body.password === COACH_PASSWORD) {
        token = AUTH_TOKEN;
        expiredStatus = null;
        await route.fulfill({ json: authLoginResponse() });
        return;
      }
      await route.fulfill({ status: 401, json: apiError("INVALID_CREDENTIALS", "Invalid sign in credentials.") });
      return;
    }

    if (url.pathname.endsWith("/auth/coach/sign-out")) {
      logoutCallCount += 1;
      token = null;
      await route.fulfill({ json: { success: true } });
      return;
    }

    if (url.pathname.startsWith("/api/coach/")) {
      const authorization = request.headers().authorization ?? null;
      protectedCalls.push({ path: url.pathname, authorization, beforeAuth: !token });

      if (!token || authorization?.toLowerCase() !== `bearer ${AUTH_TOKEN}`) {
        await route.fulfill({ status: 401, json: apiError("AUTH_REQUIRED", "Sign in is required.") });
        return;
      }

      if (expiredStatus) {
        await route.fulfill({
          status: expiredStatus,
          json: apiError(expiredStatus === 401 ? "AUTH_REQUIRED" : "FORBIDDEN", "Your session expired. Sign in again."),
        });
        return;
      }

      await fulfillProtectedCoachRoute(route, url.pathname);
      return;
    }

    await route.fulfill({ status: 404, json: apiError("NOT_FOUND", "Route not mocked.") });
  });

  return {
    expireProtectedRequestsWith(status: 401 | 403): void {
      expiredStatus = status;
    },
    logoutCalls(): number {
      return logoutCallCount;
    },
    protectedCalls(): ProtectedCall[] {
      return [...protectedCalls];
    },
    protectedCallsBeforeAuth(): ProtectedCall[] {
      return protectedCalls.filter((call) => call.beforeAuth);
    },
  };
}

async function fulfillProtectedCoachRoute(route: Route, pathname: string): Promise<void> {
  if (pathname.endsWith("/coach/command-center/snapshot")) {
    await route.fulfill({ json: commandCenterSnapshot() });
    return;
  }

  if (pathname.endsWith("/coach/availability")) {
    await route.fulfill({ json: availabilityResponse() });
    return;
  }

  await route.fulfill({ json: {} });
}

function postBody(rawBody: string | null): Record<string, unknown> {
  if (!rawBody) return {};
  try {
    return JSON.parse(rawBody) as Record<string, unknown>;
  } catch {
    return {};
  }
}

function authLoginResponse(): Record<string, unknown> {
  return {
    access_token: AUTH_TOKEN,
    token_type: "bearer",
    expires_at: "2026-09-01T00:00:00Z",
    coach: authSessionResponse(),
  };
}

function authSessionResponse(): Record<string, unknown> {
  return {
    instructor_id: "inst_001",
    display_name: "Coach Sarah",
  };
}

function apiError(code: string, message: string): Record<string, unknown> {
  return {
    error: { code, message },
  };
}

function availabilityResponse(): Record<string, unknown> {
  return {
    status: "PRESENTING",
    client_name: "Sofia Martinez",
    requested_date: "2026-08-27",
    duration_mins: 45,
    location: {
      address: "820 Lakeview Dr, Austin, TX",
      lat: 30.289,
      lng: -97.739,
      is_instructor_facility: false,
    },
    available_slots: [
      {
        start_iso: "2026-08-27T16:00:00",
        end_iso: "2026-08-27T16:45:00",
        display: "Thu, Aug 27 at 4:00 PM",
        travel_time_mins: 12,
        buffer_mins: 10,
        distance_miles: 2.1,
      },
    ],
  };
}

function commandCenterSnapshot(): Record<string, unknown> {
  return {
    instructor: {
      id: "inst_001",
      name: "Coach Sarah",
      business_name: "Swim Command",
      main_address: "100 Main St, Austin, TX",
      lat: 30.2672,
      lng: -97.7431,
      work_start_time: "08:00",
      work_end_time: "17:00",
    },
    date: "2026-08-27",
    clients: [
      {
        id: "client_sofia",
        name: "Sofia Martinez",
        phone: "",
        email: "",
        default_location: {
          address: "820 Lakeview Dr, Austin, TX",
          lat: 30.289,
          lng: -97.739,
          is_instructor_facility: false,
        },
        location_mode: "client_home",
        availability: [],
        lesson_duration_mins: 45,
        notes: "",
        history_summary: "Ready for backstroke work",
        progress_focus: "Backstroke starts",
      },
    ],
    schedule: [
      {
        id: "app_sofia_0827",
        kind: "lesson",
        client_id: "client_sofia",
        title: "Sofia Martinez",
        start_time: "2026-08-27T09:00:00",
        end_time: "2026-08-27T09:45:00",
        location: {
          address: "820 Lakeview Dr, Austin, TX",
          lat: 30.289,
          lng: -97.739,
          is_instructor_facility: false,
        },
        status: "scheduled",
        note: "Schedule loaded from backend",
      },
    ],
    requests: [],
    rules: {
      rounding_increment_mins: 15,
      post_break_buffer_mins: 15,
      distance_buffer_tiers: [
        { id: "near", label: "Under 5 mi", min_miles: 0, max_miles: 4.999, buffer_mins: 10 },
        { id: "medium", label: "5-10 mi", min_miles: 5, max_miles: 10, buffer_mins: 20 },
      ],
      payments: {
        enabled_methods: ["card", "ach", "cash"],
        cancellation_window_hours: 12,
        require_payment_method_for_new_clients: true,
      },
    },
  };
}
