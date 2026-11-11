import { expect, type Page, type Request, type Route, test } from "@playwright/test";
import type {
  ProviderConfiguration,
  ProviderConfigurationUpdate,
  ProviderWorkingHour,
  SchedulingPolicy,
} from "../src/api/types";

const SESSION_COOKIE_NAME = "swim_session";
const SESSION_COOKIE_VALUE = "provider-settings-session";

test.describe("provider setup workflow", () => {
  test("opens incomplete setup without eager availability and gates scheduling actions", async ({ page }) => {
    const api = await installProviderApi(page, { setupComplete: false });
    await page.goto("/");

    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Complete your scheduling setup" })).toBeVisible();
    expect(api.availabilityCalls()).toBe(0);

    await page.getByRole("button", { name: "Schedule" }).click();
    await expect(page.getByRole("heading", { name: "Today schedule" })).toBeVisible();
    await page.getByRole("button", { name: "Find", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    await expect(page.getByText(/before you find availability/i)).toBeVisible();

    await page.getByRole("button", { name: "Requests", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Request management" })).toBeVisible();
    await page.getByRole("button", { name: "Confirm", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    expect(api.confirmCalls()).toBe(0);
  });

  test("serializes seven weekdays, saves atomically, and unlocks availability", async ({ page }) => {
    const api = await installProviderApi(page, { setupComplete: false });
    await page.goto("/");

    await page.getByLabel("Display name").fill("Coach Morgan");
    await page.getByLabel("Business name Optional").fill("Morgan Mobile Services");
    await page.getByLabel("Base / start-of-shift address").fill("100 Main St, Austin, TX");
    await page.getByLabel("Client-facing service address").fill("500 River Rd, Austin, TX");
    await page.getByLabel("Service name").fill("Private appointment");
    const monday = page.getByRole("group", { name: "Monday working hours" });
    await monday.getByRole("switch").check();
    await monday.getByLabel("Opens").fill("09:00");
    await monday.getByLabel("Closes").fill("17:00");

    await page.getByRole("button", { name: "Save provider settings" }).click();
    await expect(page.locator("[data-provider-save-status]")).toContainText("Provider settings saved");
    await expect(page.getByRole("heading", { name: "Scheduling is ready" })).toBeVisible();
    expect(api.providerPutCalls()).toBe(1);
    expect(api.availabilityCalls()).toBe(1);

    const payload = api.lastProviderPayload();
    expect(payload?.working_hours).toHaveLength(7);
    expect(payload?.working_hours.map(({ weekday }) => weekday)).toEqual([
      "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    ]);
    expect(payload).not.toHaveProperty("id");
    expect(payload).not.toHaveProperty("lat");
    expect(api.lastProviderRequest()?.headers()["x-csrf-protection"]).toBe("1");

    await page.getByRole("button", { name: "Find", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Availability finder" })).toBeVisible();
  });

  test("focuses inline weekday errors without issuing a request", async ({ page }) => {
    const api = await installProviderApi(page);
    await page.goto("/");
    await page.getByRole("button", { name: "Settings" }).click();

    const monday = page.getByRole("group", { name: "Monday working hours" });
    await monday.getByLabel("Opens").fill("17:00");
    await monday.getByLabel("Closes").fill("08:00");
    await page.getByRole("button", { name: "Save provider settings" }).click();

    await expect(monday.getByText(/closing time must be later/i)).toBeVisible();
    await expect(monday.getByLabel("Closes")).toBeFocused();
    expect(api.providerPutCalls()).toBe(0);
  });

  test("keeps the draft after geocoding failure and supports retry", async ({ page }) => {
    const api = await installProviderApi(page);
    await page.goto("/");
    await page.getByRole("button", { name: "Settings" }).click();
    api.failNextProviderSave(422, "GEOCODING_FAILED", "We could not locate that address.", {
      field: "base_address",
    });

    const baseAddress = page.getByLabel("Base / start-of-shift address");
    await baseAddress.fill("Unknown Trail, Austin, TX");
    await page.getByRole("button", { name: "Save provider settings" }).click();

    await expect(page.getByText("We could not locate that address.")).toBeVisible();
    await expect(baseAddress).toHaveValue("Unknown Trail, Austin, TX");
    await expect(baseAddress).toBeFocused();
    await baseAddress.fill("101 Main St, Austin, TX");
    await page.getByRole("button", { name: "Save provider settings" }).click();
    await expect(page.locator("[data-provider-save-status]")).toContainText("Provider settings saved");
    expect(api.providerPutCalls()).toBe(2);
  });

  test("late save cannot restore protected state after sign-out", async ({ page }) => {
    const api = await installProviderApi(page);
    await page.goto("/");
    await page.getByRole("button", { name: "Settings" }).click();
    api.delayNextProviderSave();
    await page.getByLabel("Display name").fill("Late Response Name");
    await page.getByRole("button", { name: "Save provider settings" }).click();
    await expect.poll(api.providerPutCalls).toBe(1);
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page.getByRole("heading", { name: /locked|sign in/i })).toBeVisible();

    api.releaseProviderSave();
    await page.waitForTimeout(50);
    await expect(page.locator("[data-command-surface]")).toHaveCount(0);
    await expect(page.getByText("Late Response Name")).toHaveCount(0);
  });

  test("a 401 during provider save clears the protected form", async ({ page }) => {
    const api = await installProviderApi(page);
    await page.goto("/");
    await page.getByRole("button", { name: "Settings" }).click();
    api.failNextProviderSave(401, "INVALID_AUTH_TOKEN", "Your session expired.");
    await page.getByRole("button", { name: "Save provider settings" }).click();

    await expect(page.getByRole("heading", { name: "Session expired" })).toBeVisible();
    await expect(page.locator("[data-provider-form]")).toHaveCount(0);
  });

  for (const width of [320, 375]) {
    test(`has no setup overflow and keeps Sunday above the save action at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 720 });
      await installProviderApi(page, { setupComplete: false });
      await page.goto("/");

      const overflow = await page.evaluate(() => Math.max(
        document.documentElement.scrollWidth - document.documentElement.clientWidth,
        document.body.scrollWidth - document.body.clientWidth,
      ));
      expect(overflow).toBeLessThanOrEqual(1);
      for (const control of await page.locator("[data-provider-form] input, [data-provider-form] select, [data-provider-form] button").all()) {
        expect((await control.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);
      }

      await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
      const sunday = await page.locator('[data-weekday="sunday"]').boundingBox();
      const save = await page.locator("[data-provider-form] .provider-save-bar").boundingBox();
      expect((sunday?.y ?? 0) + (sunday?.height ?? 0)).toBeLessThanOrEqual((save?.y ?? 0) + 1);
    });
  }
});

type ProviderApiOptions = {
  setupComplete?: boolean;
};

async function installProviderApi(page: Page, options: ProviderApiOptions = {}): Promise<{
  availabilityCalls(): number;
  confirmCalls(): number;
  providerPutCalls(): number;
  lastProviderPayload(): ProviderConfigurationUpdate | null;
  lastProviderRequest(): Request | null;
  failNextProviderSave(status: number, code: string, message: string, details?: Record<string, unknown>): void;
  delayNextProviderSave(): void;
  releaseProviderSave(): void;
}> {
  let sessionActive = true;
  let provider = providerResponse(options.setupComplete ?? true);
  let policy = schedulingPolicyResponse();
  let availabilityCallCount = 0;
  let confirmCallCount = 0;
  let providerPutCallCount = 0;
  let lastPayload: ProviderConfigurationUpdate | null = null;
  let lastRequest: Request | null = null;
  let nextFailure: { status: number; code: string; message: string; details?: Record<string, unknown> } | null = null;
  let providerSaveGate: Promise<void> | null = null;
  let releaseProviderSaveGate: (() => void) | null = null;

  await page.context().addCookies([{
    name: SESSION_COOKIE_NAME,
    value: SESSION_COOKIE_VALUE,
    url: "http://127.0.0.1:5174/api",
    httpOnly: true,
    sameSite: "Strict",
  }]);

  await page.route(/\/api\/(?:auth|coach)\//, async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;

    if (pathname.endsWith("/auth/coach/session")) {
      await route.fulfill(sessionActive
        ? { json: authSession() }
        : { status: 401, json: apiError("INVALID_AUTH_TOKEN", "Sign in is required.") });
      return;
    }
    if (pathname.endsWith("/auth/coach/sign-out")) {
      sessionActive = false;
      await route.fulfill({
        headers: { "set-cookie": `${SESSION_COOKIE_NAME}=; Path=/api; HttpOnly; SameSite=Strict; Max-Age=0` },
        json: { success: true, message: "Signed out." },
      });
      return;
    }
    if (!sessionActive) {
      await route.fulfill({ status: 401, json: apiError("INVALID_AUTH_TOKEN", "Sign in is required.") });
      return;
    }
    if (pathname.endsWith("/coach/provider") && request.method() === "GET") {
      await route.fulfill({ json: provider });
      return;
    }
    if (pathname.endsWith("/coach/provider") && request.method() === "PUT") {
      providerPutCallCount += 1;
      lastRequest = request;
      lastPayload = request.postDataJSON() as ProviderConfigurationUpdate;
      if (providerSaveGate) await providerSaveGate;
      if (nextFailure) {
        const failure = nextFailure;
        nextFailure = null;
        await route.fulfill({
          status: failure.status,
          json: apiError(failure.code, failure.message, failure.details),
        });
        return;
      }
      provider = providerFromUpdate(lastPayload);
      await route.fulfill({ json: provider });
      return;
    }
    if (pathname.endsWith("/coach/scheduling-policy") && request.method() === "GET") {
      await route.fulfill({ json: policy });
      return;
    }
    if (pathname.endsWith("/coach/scheduling-policy") && request.method() === "PUT") {
      const payload = request.postDataJSON() as {
        expected_revision: number;
        distance_buffers_enabled: boolean;
        post_break_recovery_mins: number;
        distance_buffer_tiers: Array<{ id: string; buffer_mins: number }>;
      };
      if (payload.expected_revision !== policy.revision) {
        await route.fulfill({ status: 409, json: apiError("SCHEDULING_POLICY_STALE", "Reload the saved policy.") });
        return;
      }
      policy = {
        revision: policy.revision + 1,
        distance_buffers_enabled: payload.distance_buffers_enabled,
        post_break_recovery_mins: payload.post_break_recovery_mins,
        distance_buffer_tiers: policy.distance_buffer_tiers.map((tier, index) => ({
          ...tier,
          buffer_mins: payload.distance_buffer_tiers[index].buffer_mins,
        })),
      };
      await route.fulfill({ json: policy });
      return;
    }
    if (pathname.endsWith("/coach/command-center/snapshot")) {
      await route.fulfill({ json: snapshotResponse(provider, policy) });
      return;
    }
    if (pathname.endsWith("/coach/availability")) {
      availabilityCallCount += 1;
      await route.fulfill({ json: availabilityResponse() });
      return;
    }
    if (pathname.includes("/lesson-requests/") && pathname.endsWith("/confirm")) {
      confirmCallCount += 1;
      await route.fulfill({ json: {} });
      return;
    }
    if (pathname.endsWith("/coach/email-deliveries")) {
      await route.fulfill({ json: [] });
      return;
    }
    await route.fulfill({ json: {} });
  });

  return {
    availabilityCalls: () => availabilityCallCount,
    confirmCalls: () => confirmCallCount,
    providerPutCalls: () => providerPutCallCount,
    lastProviderPayload: () => lastPayload,
    lastProviderRequest: () => lastRequest,
    failNextProviderSave(status, code, message, details): void {
      nextFailure = { status, code, message, details };
    },
    delayNextProviderSave(): void {
      providerSaveGate = new Promise<void>((resolve) => { releaseProviderSaveGate = resolve; });
    },
    releaseProviderSave(): void {
      releaseProviderSaveGate?.();
      releaseProviderSaveGate = null;
      providerSaveGate = null;
    },
  };
}

function providerResponse(setupComplete: boolean): ProviderConfiguration {
  const hours = workingHours(setupComplete);
  return {
    id: "inst_001",
    display_name: setupComplete ? "Coach Sarah" : "",
    business_name: setupComplete ? "Swim Command" : null,
    timezone: "America/Chicago",
    base_location: setupComplete
      ? { address: "100 Main St, Austin, TX", lat: 30.2672, lng: -97.7431 }
      : null,
    service: setupComplete
      ? {
          name: "Private lesson",
          default_duration_mins: 45,
          location: { address: "Riverside Pool, Austin, TX", lat: 30.27, lng: -97.74 },
        }
      : { name: null, default_duration_mins: null, location: null },
    working_hours: hours,
    setup_complete: setupComplete,
    setup_missing_fields: setupComplete
      ? []
      : ["display_name", "base_address", "service_address", "service_name", "working_hours"],
  };
}

function providerFromUpdate(update: ProviderConfigurationUpdate): ProviderConfiguration {
  return {
    id: "inst_001",
    display_name: update.display_name,
    business_name: update.business_name,
    timezone: update.timezone,
    base_location: { address: update.base_address, lat: 30.2672, lng: -97.7431 },
    service: {
      name: update.service_name,
      default_duration_mins: update.default_duration_mins,
      location: { address: update.service_address, lat: 30.27, lng: -97.74 },
    },
    working_hours: update.working_hours,
    setup_complete: true,
    setup_missing_fields: [],
  };
}

function workingHours(enabled: boolean): ProviderWorkingHour[] {
  const weekdays: ProviderWorkingHour["weekday"][] = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
  ];
  return weekdays.map((weekday) => ({
    weekday,
    enabled,
    start_time: enabled ? "08:00" : null,
    end_time: enabled ? "17:00" : null,
  }));
}

function authSession(): Record<string, unknown> {
  return {
    authenticated: true,
    expires_at: "2026-09-01T00:00:00Z",
    coach: { instructor_id: "inst_001", display_name: "Coach Sarah" },
  };
}

function snapshotResponse(provider: ProviderConfiguration, policy: SchedulingPolicy): Record<string, unknown> {
  const firstEnabled = provider.working_hours.find(({ enabled }) => enabled);
  return {
    instructor: {
      id: provider.id,
      name: provider.display_name || "Provider",
      business_name: provider.business_name || provider.display_name || "Provider",
      timezone: provider.timezone,
      base_location: provider.base_location,
      service: provider.service,
      working_hours: provider.working_hours,
      setup_complete: provider.setup_complete,
      setup_missing_fields: provider.setup_missing_fields,
      main_address: provider.service.location?.address ?? "",
      lat: provider.service.location?.lat ?? 0,
      lng: provider.service.location?.lng ?? 0,
      work_start_time: firstEnabled?.start_time ?? "",
      work_end_time: firstEnabled?.end_time ?? "",
    },
    date: "2026-08-27",
    clients: [{
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
      history_summary: "New client",
      progress_focus: "Water comfort",
    }],
    schedule: [],
    requests: [{
      id: "req_1",
      client_id: "client_sofia",
      client_name: "Sofia Martinez",
      requested_slot: slotResponse(),
      source: "client_invite",
      status: "pending",
      risk: "clear",
      submitted_at: "2026-08-26T18:00:00Z",
      note: "Client selected a time.",
      alternative_slots: [],
    }],
    email_deliveries: [],
    scheduling_policy: policy,
    rules: {
      buffers_enabled: true,
      apply_distance_buffer_at_shift_start: false,
      rounding_increment_mins: 15,
      post_break_buffer_mins: 15,
      distance_buffer_tiers: [],
      payments: {
        enabled_methods: ["card"],
        cancellation_window_hours: 12,
        require_payment_method_for_new_clients: false,
      },
    },
  };
}

function schedulingPolicyResponse(): SchedulingPolicy {
  return {
    revision: 0,
    distance_buffers_enabled: true,
    post_break_recovery_mins: 15,
    distance_buffer_tiers: [
      { id: "near", label: "Under 5 mi", min_miles: 0, max_miles: 4.999, buffer_mins: 10 },
      { id: "medium", label: "5-10 mi", min_miles: 5, max_miles: 10, buffer_mins: 20 },
      { id: "far", label: "Over 10 mi", min_miles: 10.001, max_miles: null, buffer_mins: 30 },
    ],
  };
}

function slotResponse(): Record<string, unknown> {
  return {
    id: "slot_1",
    start_time: "2026-08-27T16:00:00",
    end_time: "2026-08-27T16:45:00",
    travel_time_mins: 12,
    buffer_mins: 10,
    distance_miles: 2.1,
    target_location: {
      address: "820 Lakeview Dr, Austin, TX",
      lat: 30.289,
      lng: -97.739,
      is_instructor_facility: false,
    },
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
    available_slots: [{
      start_iso: "2026-08-27T16:00:00",
      end_iso: "2026-08-27T16:45:00",
      display: "4:00 PM",
      travel_time_mins: 12,
      buffer_mins: 10,
      distance_miles: 2.1,
    }],
  };
}

function apiError(
  code: string,
  message: string,
  details?: Record<string, unknown>,
): Record<string, unknown> {
  return { error: { code, message, ...(details ? { details } : {}) } };
}
