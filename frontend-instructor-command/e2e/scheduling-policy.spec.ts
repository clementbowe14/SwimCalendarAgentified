import { expect, type Page, test } from "@playwright/test";
import type { SchedulingPolicy, SchedulingPolicyUpdate } from "../src/api/types";

const SESSION_COOKIE_NAME = "swim_session";
const SESSION_COOKIE_VALUE = "scheduling-policy-session";

test.describe("provider scheduling policy", () => {
  for (const width of [320, 375]) {
    test(`keeps one-handed policy controls accessible at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 720 });
      await installSchedulingPolicyApi(page);
      await page.goto("/");
      await page.getByRole("button", { name: "Settings" }).click();

      const form = page.locator("[data-scheduling-policy-form]");
      const toggle = form.getByRole("switch", { name: "Distance-based buffers" });
      await expect(toggle).toBeChecked();
      const near = form.locator('[data-policy-minute="tier.near.buffer_mins"]');
      const decrement = form.locator('[data-policy-step="tier.near.buffer_mins"][data-policy-step-delta="-1"]');
      const increment = form.locator('[data-policy-step="tier.near.buffer_mins"][data-policy-step-delta="1"]');

      await near.fill("30");
      await expect(decrement).toHaveAttribute("aria-label", /currently 30 minutes/i);
      await increment.click();
      await expect(near).toHaveValue("31");
      await expect(increment).toBeFocused();
      await near.fill("240");
      await increment.click();
      await expect(near).toHaveValue("240");
      await near.fill("0");
      await decrement.click();
      await expect(near).toHaveValue("0");

      for (const control of await form.locator("input, button").all()) {
        expect((await control.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);
      }
      const overflow = await page.evaluate(() => Math.max(
        document.documentElement.scrollWidth - document.documentElement.clientWidth,
        document.body.scrollWidth - document.body.clientWidth,
      ));
      expect(overflow).toBeLessThanOrEqual(1);

      await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
      const finalControl = await form.locator("[data-policy-minute-row]").last().boundingBox();
      const saveBar = await form.locator(".policy-save-bar").boundingBox();
      expect((finalControl?.y ?? 0) + (finalControl?.height ?? 0)).toBeLessThanOrEqual((saveBar?.y ?? 0) + 1);
    });
  }

  test("preserves the draft through validation, pending save, and server failure", async ({ page }) => {
    const api = await installSchedulingPolicyApi(page);
    await page.goto("/");
    await page.getByRole("button", { name: "Settings" }).click();
    const form = page.locator("[data-scheduling-policy-form]");
    const recovery = form.locator('[data-policy-minute="post_break_recovery_mins"]');

    await recovery.fill("241");
    await form.getByRole("button", { name: "Save scheduling policy" }).click();
    await expect(form.getByRole("alert")).toContainText(/0 through 240/i);
    await expect(form.getByRole("alert")).toBeFocused();
    await expect(recovery).toHaveValue("241");
    expect(api.policyPutCalls()).toBe(0);

    await recovery.fill("33");
    api.delayNextPolicySave();
    await form.getByRole("button", { name: "Save scheduling policy" }).click();
    await expect(form).toHaveAttribute("aria-busy", "true");
    await expect(form.getByRole("switch", { name: "Distance-based buffers" })).toBeDisabled();
    await expect(recovery).toBeDisabled();
    api.releasePolicySave();
    await expect(form.locator("[data-policy-save-status]")).toContainText("Scheduling policy saved");
    await expect(recovery).toHaveValue("33");

    api.failNextPolicySave(500);
    await recovery.fill("41");
    await form.getByRole("button", { name: "Save scheduling policy" }).click();
    await expect(form.locator("[data-policy-error]")).toBeFocused();
    await expect(recovery).toHaveValue("41");
  });

  test("keeps a conflicting draft until the instructor reloads the saved policy", async ({ page }) => {
    const api = await installSchedulingPolicyApi(page);
    await page.goto("/");
    await page.getByRole("button", { name: "Settings" }).click();
    const form = page.locator("[data-scheduling-policy-form]");
    const recovery = form.locator('[data-policy-minute="post_break_recovery_mins"]');
    await recovery.fill("44");
    api.conflictNextPolicySave({ post_break_recovery_mins: 25 });

    await form.getByRole("button", { name: "Save scheduling policy" }).click();

    const conflict = form.locator("[data-policy-conflict]");
    await expect(conflict).toBeFocused();
    await expect(recovery).toHaveValue("44");
    await expect(form.getByRole("button", { name: "Save scheduling policy" })).toBeDisabled();
    await conflict.getByRole("button", { name: "Reload saved policy" }).click();
    await expect(recovery).toHaveValue("25");
    await expect(conflict).toHaveCount(0);
  });

  test("ignores a successful policy response older than the policy already on screen", async ({ page }) => {
    const api = await installSchedulingPolicyApi(page);
    await page.goto("/");
    await page.getByRole("button", { name: "Settings" }).click();
    const form = page.locator("[data-scheduling-policy-form]");
    const recovery = form.locator('[data-policy-minute="post_break_recovery_mins"]');

    await recovery.fill("33");
    await form.getByRole("button", { name: "Save scheduling policy" }).click();
    await expect(form.getByText("Revision 1", { exact: true })).toBeVisible();

    await recovery.fill("44");
    api.returnOlderPolicyOnNextSave();
    await form.getByRole("button", { name: "Save scheduling policy" }).click();

    await expect(form.locator("[data-policy-conflict]")).toBeFocused();
    await expect(form.getByText("Revision 1", { exact: true })).toBeVisible();
    await expect(recovery).toHaveValue("44");
  });

  test("does not invent a search when policy is saved before availability runs", async ({ page }) => {
    const api = await installSchedulingPolicyApi(page, { setupComplete: false });
    await page.goto("/");
    const form = page.locator("[data-scheduling-policy-form]");
    await form.locator('[data-policy-minute="post_break_recovery_mins"]').fill("22");
    await form.getByRole("button", { name: "Save scheduling policy" }).click();

    await expect(form.locator("[data-policy-save-status]")).toContainText("Scheduling policy saved");
    await expect(form.locator(".availability-policy-status")).toHaveCount(0);
    expect(api.availabilityCalls()).toBe(0);
  });

  test("clears old slots after save failure and requires a successful retry", async ({ page }) => {
    const api = await installSchedulingPolicyApi(page);
    await page.goto("/");
    await page.getByRole("button", { name: "Find", exact: true }).click();
    await expect(page.getByRole("button", { name: "Book lesson" })).toHaveCount(1);

    await page.getByRole("button", { name: "Settings" }).click();
    api.failNextAvailability(503);
    const form = page.locator("[data-scheduling-policy-form]");
    await form.locator('[data-policy-minute="post_break_recovery_mins"]').fill("36");
    await form.getByRole("button", { name: "Save scheduling policy" }).click();
    await expect(form.locator(".availability-policy-status.stale")).toContainText(/Policy saved.*Retry/i);

    await page.getByRole("button", { name: "Find", exact: true }).click();
    await expect(page.getByRole("button", { name: "Book lesson" })).toHaveCount(0);
    await expect(page.getByText("Do not book the old options")).toBeVisible();
    await page.getByRole("button", { name: "Retry safe starts" }).click();
    await expect(page.getByRole("button", { name: "Book lesson" })).toHaveCount(1);
  });

  test("ignores an older availability response after a policy save refresh", async ({ page }) => {
    const api = await installSchedulingPolicyApi(page);
    await page.goto("/");
    await page.getByRole("button", { name: "Find", exact: true }).click();
    api.delayNextAvailability();
    await page.getByRole("button", { name: "Find safe starts" }).click();

    await page.getByRole("button", { name: "Settings" }).click();
    const form = page.locator("[data-scheduling-policy-form]");
    await form.locator('[data-policy-minute="tier.near.buffer_mins"]').fill("90");
    await form.getByRole("button", { name: "Save scheduling policy" }).click();
    await expect(form.locator("[data-policy-save-status]")).toContainText("Scheduling policy saved");
    await page.getByRole("button", { name: "Find", exact: true }).click();
    await expect(page.locator(".slot-card")).toContainText("Buffer 90 min");
    const currentSlot = await page.locator(".slot-card").textContent();

    await api.releaseAvailability();
    await expect(page.locator(".slot-card")).toHaveText(currentSlot ?? "");
    await expect(page.locator(".slot-card")).not.toContainText("Buffer 10 min");
  });
});

type ApiOptions = { setupComplete?: boolean };

async function installSchedulingPolicyApi(page: Page, options: ApiOptions = {}): Promise<{
  policyPutCalls(): number;
  availabilityCalls(): number;
  delayNextPolicySave(): void;
  releasePolicySave(): void;
  failNextPolicySave(status: number): void;
  conflictNextPolicySave(update: Partial<SchedulingPolicy>): void;
  returnOlderPolicyOnNextSave(): void;
  failNextAvailability(status: number): void;
  delayNextAvailability(): void;
  releaseAvailability(): Promise<void>;
}> {
  let policy = policyResponse();
  let policyPutCallCount = 0;
  let availabilityCallCount = 0;
  let nextPolicyFailure: number | null = null;
  let nextAvailabilityFailure: number | null = null;
  let conflictUpdate: Partial<SchedulingPolicy> | null = null;
  let olderPolicyResponseOnNextSave = false;
  let policySaveGate: Promise<void> | null = null;
  let releasePolicySaveGate: (() => void) | null = null;
  let availabilityGate: Promise<void> | null = null;
  let releaseAvailabilityGate: (() => void) | null = null;
  let availabilityCompletion: Promise<void> | null = null;
  let completeAvailability: (() => void) | null = null;
  const setupComplete = options.setupComplete ?? true;

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
      await route.fulfill({ json: authSession() });
      return;
    }
    if (pathname.endsWith("/coach/provider")) {
      await route.fulfill({ json: providerResponse(setupComplete) });
      return;
    }
    if (pathname.endsWith("/coach/command-center/snapshot")) {
      await route.fulfill({ json: snapshotResponse(policy, setupComplete) });
      return;
    }
    if (pathname.endsWith("/coach/scheduling-policy") && request.method() === "GET") {
      await route.fulfill({ json: policy });
      return;
    }
    if (pathname.endsWith("/coach/scheduling-policy") && request.method() === "PUT") {
      policyPutCallCount += 1;
      if (policySaveGate) await policySaveGate;
      if (conflictUpdate) {
        policy = { ...policy, ...conflictUpdate, revision: policy.revision + 1 };
        conflictUpdate = null;
        await route.fulfill({ status: 409, json: apiError("SCHEDULING_POLICY_STALE", "Reload the saved policy.") });
        return;
      }
      if (nextPolicyFailure) {
        const status = nextPolicyFailure;
        nextPolicyFailure = null;
        await route.fulfill({ status, json: apiError("POLICY_SAVE_FAILED", "The policy could not be saved.") });
        return;
      }
      if (olderPolicyResponseOnNextSave) {
        olderPolicyResponseOnNextSave = false;
        await route.fulfill({ json: policyResponse() });
        return;
      }
      const update = request.postDataJSON() as SchedulingPolicyUpdate;
      if (update.expected_revision !== policy.revision) {
        await route.fulfill({ status: 409, json: apiError("SCHEDULING_POLICY_STALE", "Reload the saved policy.") });
        return;
      }
      policy = applyPolicyUpdate(policy, update);
      await route.fulfill({ json: policy });
      return;
    }
    if (pathname.endsWith("/coach/availability")) {
      availabilityCallCount += 1;
      const response = availabilityResponse(policy);
      const gate = availabilityGate;
      const completeDelayedAvailability = gate ? completeAvailability : null;
      if (gate) {
        availabilityGate = null;
        completeAvailability = null;
        await gate;
      }
      try {
        if (nextAvailabilityFailure) {
          const status = nextAvailabilityFailure;
          nextAvailabilityFailure = null;
          await route.fulfill({ status, json: apiError("AVAILABILITY_FAILED", "Availability failed.") });
          return;
        }
        await route.fulfill({ json: response });
      } finally {
        completeDelayedAvailability?.();
      }
      return;
    }
    if (pathname.endsWith("/coach/email-deliveries")) {
      await route.fulfill({ json: [] });
      return;
    }
    await route.fulfill({ json: {} });
  });

  return {
    policyPutCalls: () => policyPutCallCount,
    availabilityCalls: () => availabilityCallCount,
    delayNextPolicySave(): void {
      policySaveGate = new Promise<void>((resolve) => { releasePolicySaveGate = resolve; });
    },
    releasePolicySave(): void {
      releasePolicySaveGate?.();
      releasePolicySaveGate = null;
      policySaveGate = null;
    },
    failNextPolicySave(status): void { nextPolicyFailure = status; },
    conflictNextPolicySave(update): void { conflictUpdate = update; },
    returnOlderPolicyOnNextSave(): void { olderPolicyResponseOnNextSave = true; },
    failNextAvailability(status): void { nextAvailabilityFailure = status; },
    delayNextAvailability(): void {
      availabilityGate = new Promise<void>((resolve) => { releaseAvailabilityGate = resolve; });
      availabilityCompletion = new Promise<void>((resolve) => { completeAvailability = resolve; });
    },
    async releaseAvailability(): Promise<void> {
      const completion = availabilityCompletion;
      if (!completion) throw new Error("No delayed availability request is waiting to complete.");
      releaseAvailabilityGate?.();
      releaseAvailabilityGate = null;
      await completion;
      availabilityCompletion = null;
    },
  };
}

function policyResponse(): SchedulingPolicy {
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

function applyPolicyUpdate(current: SchedulingPolicy, update: SchedulingPolicyUpdate): SchedulingPolicy {
  return {
    revision: current.revision + 1,
    distance_buffers_enabled: update.distance_buffers_enabled,
    post_break_recovery_mins: update.post_break_recovery_mins,
    distance_buffer_tiers: current.distance_buffer_tiers.map((tier, index) => ({
      ...tier,
      buffer_mins: update.distance_buffer_tiers[index].buffer_mins,
    })),
  };
}

function authSession(): Record<string, unknown> {
  return {
    authenticated: true,
    expires_at: "2026-09-01T00:00:00Z",
    coach: { instructor_id: "inst_001", display_name: "Coach Sarah" },
  };
}

function providerResponse(setupComplete: boolean): Record<string, unknown> {
  const weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];
  return {
    id: "inst_001",
    display_name: setupComplete ? "Coach Sarah" : "",
    business_name: setupComplete ? "Swim Command" : null,
    timezone: "America/Chicago",
    base_location: setupComplete ? { address: "100 Main St, Austin, TX", lat: 30.2672, lng: -97.7431 } : null,
    service: setupComplete
      ? { name: "Private lesson", default_duration_mins: 45, location: { address: "Riverside Pool", lat: 30.27, lng: -97.74 } }
      : { name: null, default_duration_mins: null, location: null },
    working_hours: weekdays.map((weekday) => ({
      weekday,
      enabled: setupComplete,
      start_time: setupComplete ? "08:00" : null,
      end_time: setupComplete ? "17:00" : null,
    })),
    setup_complete: setupComplete,
    setup_missing_fields: setupComplete ? [] : ["display_name", "base_address", "service_address", "service_name", "working_hours"],
  };
}

function snapshotResponse(policy: SchedulingPolicy, setupComplete: boolean): Record<string, unknown> {
  const provider = providerResponse(setupComplete);
  return {
    instructor: {
      id: "inst_001",
      name: setupComplete ? "Coach Sarah" : "Provider",
      business_name: setupComplete ? "Swim Command" : "Provider",
      timezone: "America/Chicago",
      base_location: provider.base_location,
      service: provider.service,
      working_hours: provider.working_hours,
      setup_complete: setupComplete,
      setup_missing_fields: provider.setup_missing_fields,
      main_address: setupComplete ? "Riverside Pool" : "",
      lat: setupComplete ? 30.27 : 0,
      lng: setupComplete ? -97.74 : 0,
      work_start_time: setupComplete ? "08:00" : "",
      work_end_time: setupComplete ? "17:00" : "",
    },
    date: "2026-08-27",
    clients: [{
      id: "client_sofia",
      name: "Sofia Martinez",
      phone: "",
      email: "",
      default_location: { address: "820 Lakeview Dr", lat: 30.289, lng: -97.739, is_instructor_facility: false },
      location_mode: "client_home",
      availability: [],
      lesson_duration_mins: 45,
      notes: "",
      history_summary: "New client",
      progress_focus: "Water comfort",
    }],
    schedule: [],
    requests: [],
    email_deliveries: [],
    scheduling_policy: policy,
    rules: {
      buffers_enabled: true,
      apply_distance_buffer_at_shift_start: false,
      rounding_increment_mins: 15,
      post_break_buffer_mins: 15,
      distance_buffer_tiers: policy.distance_buffer_tiers,
      payments: { enabled_methods: ["card"], cancellation_window_hours: 12, require_payment_method_for_new_clients: false },
    },
  };
}

function availabilityResponse(policy: SchedulingPolicy): Record<string, unknown> {
  const delayed = policy.distance_buffer_tiers[0].buffer_mins >= 60;
  return {
    status: "PRESENTING",
    client_name: "Sofia Martinez",
    requested_date: "2026-08-27",
    duration_mins: 45,
    location: { address: "820 Lakeview Dr", lat: 30.289, lng: -97.739, is_instructor_facility: false },
    available_slots: [{
      start_iso: delayed ? "2026-08-27T16:30:00-05:00" : "2026-08-27T16:00:00-05:00",
      end_iso: delayed ? "2026-08-27T17:15:00-05:00" : "2026-08-27T16:45:00-05:00",
      display: delayed ? "Thu, Aug 27 at 4:30 PM" : "Thu, Aug 27 at 4:00 PM",
      travel_time_mins: 12,
      buffer_mins: policy.distance_buffer_tiers[0].buffer_mins,
      distance_miles: 2.1,
    }],
  };
}

function apiError(code: string, message: string): Record<string, unknown> {
  return { error: { code, message } };
}
