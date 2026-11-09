import { expect, type Page, type Route, test } from "@playwright/test";

const SESSION_COOKIE_NAME = "swim_session";
const SESSION_COOKIE_VALUE = "playwright-opaque-session";
const COACH_EMAIL = "coach@example.com";
const COACH_PASSWORD = "correct-horse-battery-staple";
const LOCKED_OR_EXPIRED_HEADING = /sign in|locked|session expired/i;

test.describe("MVP coach auth flow", () => {
  test.describe.configure({ timeout: 15_000 });

  test("gates protected requests and rendering behind delayed session validation", async ({ page }) => {
    const api = await mockAuthenticatedCoachApi(page, { initialSession: true, delaySessionValidation: true });

    await page.goto("/");

    await expect(page.getByRole("heading", { name: "Checking your session…" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Today schedule" })).toHaveCount(0);
    await expect(page.locator("[data-command-surface]")).toHaveCount(0);
    expect(api.protectedCallsBeforeValidation()).toHaveLength(0);

    api.releaseSessionValidation();
    await expect(page.getByRole("heading", { name: "Today schedule" })).toBeVisible();
    expect(api.requestOrder()[0]).toBe("session");
    expect(api.protectedCallsBeforeValidation()).toHaveLength(0);
  });

  test("signs in with an HttpOnly cookie, keeps command-center styling, and signs out", async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem("swim-command:coach-session:v1", JSON.stringify({ access_token: "legacy-secret" }));
    });
    const api = await mockAuthenticatedCoachApi(page);

    await page.goto("/");

    await expect(page.locator("html")).toHaveAttribute("data-data-mode", "http");
    await expect(page.getByText("Mock data", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: LOCKED_OR_EXPIRED_HEADING })).toBeVisible();
    await expect(page.getByLabel(/email/i)).toBeVisible();
    await expect(page.getByLabel(/password/i)).toBeVisible();
    await expect(page.getByRole("heading", { name: "Today schedule" })).toHaveCount(0);
    expect(api.protectedCallsBeforeValidation()).toHaveLength(0);

    await page.getByLabel(/email/i).fill(COACH_EMAIL);
    await page.getByLabel(/password/i).fill(COACH_PASSWORD);
    await page.getByRole("button", { name: /sign in/i }).click();

    await expect(page.getByRole("heading", { name: "Today schedule" })).toBeVisible();
    await expect(page.locator(".command-shell")).toBeVisible();
    await expect(page.locator(".sidebar")).toBeVisible();
    await expect(page.locator(".topbar")).toBeVisible();
    await expect(page.getByRole("button", { name: /sign out/i })).toHaveCount(0);

    const shellColumns = await page.locator(".command-shell").evaluate((node) => {
      return window.getComputedStyle(node).gridTemplateColumns;
    });
    expect(shellColumns).toContain("280px");

    const brandRadius = await page.locator(".brand-mark").evaluate((node) => {
      return window.getComputedStyle(node).borderRadius;
    });
    expect(brandRadius).toBe("8px");

    const cookie = (await page.context().cookies()).find((item) => item.name === SESSION_COOKIE_NAME);
    expect(cookie?.httpOnly).toBe(true);
    expect(cookie?.sameSite).toBe("Strict");
    expect(await page.evaluate(() => document.cookie)).not.toContain(SESSION_COOKIE_NAME);
    expect(await page.evaluate(() => localStorage.getItem("swim-command:coach-session:v1"))).toBeNull();
    expect(await page.evaluate(() => sessionStorage.length)).toBe(0);
    expect(api.protectedCalls().every((call) => call.authorization === null && call.hasSessionCookie)).toBe(true);
    expect(api.protectedCalls().filter((call) => call.method !== "GET").every((call) => call.csrf === "1")).toBe(true);

    await page.getByRole("button", { name: "Settings" }).click();
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    await expect(page.getByRole("button", { name: /sign out/i })).toBeVisible();
    await page.getByRole("button", { name: /sign out/i }).click();

    await expect(page.getByRole("heading", { name: LOCKED_OR_EXPIRED_HEADING })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Today schedule" })).toHaveCount(0);
    expect(api.logoutCalls()).toBe(1);
    expect(api.signOutBodies()).toEqual([null]);
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

  test("reload validates the cookie before restoring protected command data", async ({ page }) => {
    const api = await mockAuthenticatedCoachApi(page);
    await signInThroughUi(page);
    const priorSessionChecks = api.sessionCalls();
    const priorProtectedCalls = api.protectedCalls().length;

    await page.reload();

    await expect(page.getByRole("heading", { name: "Today schedule" })).toBeVisible();
    expect(api.sessionCalls()).toBe(priorSessionChecks + 1);
    const reloadOrder = api.requestOrder().slice(-3);
    expect(reloadOrder[0]).toBe("session");
    expect(api.protectedCalls().slice(priorProtectedCalls).every((call) => !call.beforeValidation)).toBe(true);
  });

  test("shows a retryable unavailable state when session validation cannot complete", async ({ page }) => {
    const api = await mockAuthenticatedCoachApi(page, { initialSession: true });
    api.failNextSessionCheckWith(503);

    await page.goto("/");

    await expect(page.getByRole("heading", { name: "Session check unavailable" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Today schedule" })).toHaveCount(0);
    expect(api.protectedCalls()).toHaveLength(0);
    await page.getByRole("button", { name: "Retry session check" }).click();
    await expect(page.getByRole("heading", { name: "Today schedule" })).toBeVisible();
  });

  test("uses the exact direct-booking ID for an authenticated calendar fallback", async ({ page }) => {
    const api = await mockAuthenticatedCoachApi(page);
    await signInThroughUi(page);
    await page.getByRole("button", { name: "Find" }).click();
    await page.getByRole("button", { name: "Book lesson" }).first().click();

    const dialog = page.getByRole("dialog", { name: "Ready for your calendar" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("link", { name: /Open Google Calendar/ })).toBeVisible();
    expect(api.protectedCalls().some((call) => call.path.endsWith("/coach/bookings"))).toBe(true);
    expect(
      api.protectedCalls().some((call) =>
        call.path.endsWith("/coach/appointments/app_direct_123/calendar-links"),
      ),
    ).toBe(true);
  });

  test("confirms through the canonical route and renders every persisted occurrence", async ({ page }) => {
    const api = await mockAuthenticatedCoachApi(page);
    await signInThroughUi(page);
    await page.getByRole("button", { name: "Requests", exact: true }).click();
    await page.getByRole("button", { name: "Confirm" }).click();

    const dialog = page.getByRole("dialog", { name: "Ready for your calendar" });
    await expect(dialog.getByRole("list", { name: "Google Calendar occurrences" }).getByRole("link")).toHaveCount(2);
    const confirmCall = api.protectedCalls().find((call) =>
      call.path.endsWith("/lesson-requests/req_live_1/confirm"),
    );
    expect(confirmCall?.body).toEqual({ slot_id: "offer_alt_req_live_1" });
    expect(api.protectedCalls().some((call) => call.path.endsWith("/approve"))).toBe(false);
  });

  test("keeps a conflicting confirmation actionable without an optimistic handoff", async ({ page }) => {
    const api = await mockAuthenticatedCoachApi(page);
    await signInThroughUi(page);
    await page.getByRole("button", { name: "Requests", exact: true }).click();
    api.failNextConfirmationWithConflict();

    await page.getByRole("button", { name: "Confirm" }).click();

    await expect(page.getByRole("alert")).toContainText(/already confirmed for a different time/i);
    await expect(page.getByRole("alert")).toContainText("LESSON_REQUEST_CONFIRM_CONFLICT");
    await expect(page.getByRole("button", { name: "Confirm" })).toBeVisible();
    await expect(page.getByRole("dialog", { name: "Ready for your calendar" })).toHaveCount(0);
    await expect(page.locator('[data-email-delivery-card="delivery-confirm-live"]')).toHaveCount(0);
    const confirmCall = api.protectedCalls().find((call) =>
      call.path.endsWith("/lesson-requests/req_live_1/confirm"),
    );
    expect(confirmCall?.body).toEqual({ slot_id: "offer_alt_req_live_1" });
    expect(api.protectedCalls().some((call) => call.path.endsWith("/calendar-links"))).toBe(false);
  });

  test("keeps newer and omitted delivery cards when refresh data is stale or unavailable", async ({ page }) => {
    const api = await mockAuthenticatedCoachApi(page);
    await signInThroughUi(page);
    await page.getByRole("button", { name: "Requests", exact: true }).click();

    const failedCard = page.locator('[data-email-delivery-card="delivery-live-resend"]');
    const deliveredCard = page.locator('[data-email-delivery-card="delivery-live-delivered"]');
    const refreshButton = page.getByRole("button", { name: "Refresh delivery status" });
    await expect(failedCard.getByText("Delivery failed", { exact: true })).toBeVisible();
    await expect(deliveredCard).toBeVisible();

    api.respondToNextEmailRefreshWith([
      emailDeliveryResponse({
        id: "delivery-live-resend",
        status: "accepted",
        issue: null,
        can_resend: false,
        client_name: "Sofia Martinez",
        updated_at: "2026-08-27T15:05:00Z",
      }),
    ]);
    await refreshButton.click();

    await expect(failedCard.getByText("Delivery failed", { exact: true })).toBeVisible();
    await expect(failedCard.getByRole("button", { name: "Resend safely" })).toBeVisible();
    await expect(deliveredCard).toBeVisible();

    api.failNextEmailRefreshWith(503);
    await refreshButton.click();

    await expect(page.getByRole("alert")).toContainText(/last-known statuses are still shown/i);
    await expect(failedCard).toBeVisible();
    await expect(deliveredCard).toBeVisible();
    await expect(refreshButton).toBeFocused();
  });

  test("keeps the last-known delivery card when a safe resend cannot be confirmed", async ({ page }) => {
    const api = await mockAuthenticatedCoachApi(page);
    await signInThroughUi(page);
    await page.getByRole("button", { name: "Requests", exact: true }).click();

    const failedCard = page.locator('[data-email-delivery-card="delivery-live-resend"]');
    const resendButton = failedCard.getByRole("button", { name: "Resend safely" });
    api.failNextEmailActionWith(503);
    await resendButton.click();

    await expect(failedCard.getByRole("alert")).toContainText(/last-known delivery status is still shown/i);
    await expect(failedCard.getByText("Delivery failed", { exact: true })).toBeVisible();
    await expect(resendButton).toBeVisible();
    await expect(resendButton).toBeFocused();
  });

  test("preserves confirmation delivery state when the post-booking snapshot refresh fails", async ({ page }) => {
    const api = await mockAuthenticatedCoachApi(page);
    await signInThroughUi(page);
    await page.getByRole("button", { name: "Requests", exact: true }).click();

    api.failNextSnapshotWith(503);
    await page.getByRole("button", { name: "Confirm" }).click();

    const dialog = page.getByRole("dialog", { name: "Ready for your calendar" });
    await expect(dialog).toBeVisible();
    await expect(page.getByRole("alert")).toContainText(/appointment was saved.*schedule could not be refreshed/i);
    await dialog.getByRole("button", { name: "Done" }).click();

    const confirmationCard = page.locator('[data-email-delivery-card="delivery-confirm-live"]');
    await expect(confirmationCard).toBeVisible();
    await expect(confirmationCard.getByText("Queued to send", { exact: true })).toBeVisible();
    expect(api.protectedCalls().some((call) => call.path.endsWith("/lesson-requests/req_live_1/confirm"))).toBe(true);
  });

  test("keeps persistence truthful when the handoff becomes unavailable", async ({ page }) => {
    const api = await mockAuthenticatedCoachApi(page);
    api.failNextCalendarReadWith(409);
    await signInThroughUi(page);
    await page.getByRole("button", { name: "Find" }).click();
    await page.getByRole("button", { name: "Book lesson" }).first().click();

    const dialog = page.getByRole("dialog", { name: "Calendar options unavailable" });
    await expect(dialog).toContainText(/appointment is saved/i);
    await expect(dialog.getByRole("link", { name: /Google Calendar/ })).toHaveCount(0);
    await expect(dialog.getByRole("button", { name: "Download .ics" })).toHaveCount(0);
    expect(api.protectedCalls().filter((call) => call.path.endsWith("/coach/bookings"))).toHaveLength(1);
  });

  test("locks an expired session without erasing the persisted booking outcome", async ({ page }) => {
    const api = await mockAuthenticatedCoachApi(page);
    api.failNextCalendarReadWith(401);
    await signInThroughUi(page);
    await page.getByRole("button", { name: "Find" }).click();
    await page.getByRole("button", { name: "Book lesson" }).first().click();

    await expect(page.getByRole("heading", { name: "Session expired" })).toBeVisible();
    await expect(page.getByRole("alert")).toContainText(/appointment was saved/i);
    await expect(page.getByRole("alert")).toContainText(/do not book it again/i);
    expect(api.protectedCalls().filter((call) => call.path.endsWith("/coach/bookings"))).toHaveLength(1);
  });

  test("clears protected UI after a revoked session returns 401", async ({ page }) => {
    const api = await mockAuthenticatedCoachApi(page);
    await signInThroughUi(page);

    api.revokeSession();
    await page.getByRole("button", { name: "Find" }).click();
    await page.getByRole("button", { name: "Find safe starts" }).click();

    await expect(page.getByRole("heading", { name: "Session expired" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Availability finder" })).toHaveCount(0);
  });

  test("keeps a valid session and protected UI on provider 403", async ({ page }) => {
    const api = await mockAuthenticatedCoachApi(page);
    await signInThroughUi(page);
    api.failProtectedRequestsWith(403);

    await page.getByRole("button", { name: "Find" }).click();
    await page.getByRole("button", { name: "Find safe starts" }).click();

    await expect(page.getByRole("alert")).toContainText(/permission|forbidden/i);
    await expect(page.getByRole("heading", { name: "Availability finder" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Session expired" })).toHaveCount(0);
  });

  test("rejects an unsafe command without optimistic booking or session expiry", async ({ page }) => {
    const api = await mockAuthenticatedCoachApi(page);
    await signInThroughUi(page);
    await page.getByRole("button", { name: "Find" }).click();
    api.failNextBookingWithCsrf();

    await page.getByRole("button", { name: "Book lesson" }).first().click();

    await expect(page.getByRole("alert")).toContainText(/origin|csrf/i);
    await expect(page.getByRole("dialog", { name: /calendar/i })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "Availability finder" })).toBeVisible();
    const bookingCall = api.protectedCalls().find((call) => call.path.endsWith("/coach/bookings"));
    expect(bookingCall?.csrf).toBe("1");
  });

  test("successful sign-out revokes the session and rejects a replayed captured cookie", async ({ page }) => {
    const api = await mockAuthenticatedCoachApi(page);
    await signInThroughUi(page);
    const captured = (await page.context().cookies()).find((item) => item.name === SESSION_COOKIE_NAME);
    expect(captured).toBeDefined();

    await page.getByRole("button", { name: "Settings" }).click();
    await page.getByRole("button", { name: /sign out/i }).click();
    await expect(page.getByRole("heading", { name: LOCKED_OR_EXPIRED_HEADING })).toBeVisible();

    await page.context().addCookies([captured!]);
    await page.reload();

    await expect(page.getByRole("heading", { name: LOCKED_OR_EXPIRED_HEADING })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Today schedule" })).toHaveCount(0);
    expect(api.invalidSessionReplays()).toBeGreaterThan(0);
  });

  test("failed sign-out hides protected data and offers a revocation retry", async ({ page }) => {
    const api = await mockAuthenticatedCoachApi(page);
    await signInThroughUi(page);
    api.failNextSignOutWith(503);

    await page.getByRole("button", { name: "Settings" }).click();
    await page.getByRole("button", { name: /sign out/i }).click();

    await expect(page.getByRole("heading", { name: "Sign-out could not be confirmed" })).toBeVisible();
    await expect(page.locator("[data-command-surface]")).toHaveCount(0);
    await page.getByRole("button", { name: "Retry sign out" }).click();
    await expect(page.getByRole("heading", { name: LOCKED_OR_EXPIRED_HEADING })).toBeVisible();
    expect(api.logoutCalls()).toBe(2);
  });

  test("a delayed protected response cannot repopulate state after sign-out", async ({ page }) => {
    const api = await mockAuthenticatedCoachApi(page);
    await signInThroughUi(page);
    await page.getByRole("button", { name: "Find" }).click();
    api.delayNextAvailability();

    await page.getByRole("button", { name: "Find safe starts" }).click({ noWaitAfter: true });
    await page.getByRole("button", { name: "Settings" }).click();
    await page.getByRole("button", { name: /sign out/i }).click();
    await expect(page.getByRole("heading", { name: LOCKED_OR_EXPIRED_HEADING })).toBeVisible();

    api.releaseAvailability();
    await page.waitForTimeout(50);
    await expect(page.locator("[data-command-surface]")).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "Today schedule" })).toHaveCount(0);
  });
});

type ProtectedCall = {
  path: string;
  method: string;
  authorization: string | null;
  csrf: string | null;
  hasSessionCookie: boolean;
  beforeValidation: boolean;
  body: Record<string, unknown> | null;
};

type MockCoachApiState = {
  emailDeliveries: Array<Record<string, unknown>>;
  nextEmailRefreshStatus: number | null;
  nextEmailActionStatus: number | null;
  nextEmailRefreshResponse: Array<Record<string, unknown>> | null;
  nextSnapshotStatus: number | null;
  nextConfirmationConflict: boolean;
  nextBookingCsrfFailure: boolean;
};

type MockCoachApiOptions = {
  initialSession?: boolean;
  delaySessionValidation?: boolean;
};

async function mockAuthenticatedCoachApi(page: Page, options: MockCoachApiOptions = {}): Promise<{
  failProtectedRequestsWith(status: 401 | 403): void;
  revokeSession(): void;
  failNextCalendarReadWith(status: 401 | 409): void;
  failNextEmailRefreshWith(status: number): void;
  failNextEmailActionWith(status: number): void;
  respondToNextEmailRefreshWith(deliveries: Array<Record<string, unknown>>): void;
  failNextSnapshotWith(status: number): void;
  failNextConfirmationWithConflict(): void;
  failNextSessionCheckWith(status: number): void;
  failNextSignOutWith(status: number): void;
  failNextBookingWithCsrf(): void;
  delayNextAvailability(): void;
  releaseAvailability(): void;
  releaseSessionValidation(): void;
  logoutCalls(): number;
  sessionCalls(): number;
  invalidSessionReplays(): number;
  signOutBodies(): Array<Record<string, unknown> | null>;
  requestOrder(): string[];
  protectedCalls(): ProtectedCall[];
  protectedCallsBeforeValidation(): ProtectedCall[];
}> {
  let sessionActive = options.initialSession ?? false;
  let sessionRevoked = false;
  let sessionValidated = false;
  let expiredStatus: 401 | 403 | null = null;
  let nextCalendarReadStatus: 401 | 409 | null = null;
  let nextSessionStatus: number | null = null;
  let nextSignOutStatus: number | null = null;
  let logoutCallCount = 0;
  let sessionCallCount = 0;
  let invalidSessionReplayCount = 0;
  let delayAvailability = false;
  let releaseAvailabilityGate: (() => void) | null = null;
  let availabilityGate: Promise<void> | null = null;
  let releaseSessionGate: (() => void) | null = null;
  let sessionGate = options.delaySessionValidation
    ? new Promise<void>((resolve) => { releaseSessionGate = resolve; })
    : null;
  const signOutBodies: Array<Record<string, unknown> | null> = [];
  const requestOrder: string[] = [];
  const protectedCalls: ProtectedCall[] = [];
  const state: MockCoachApiState = {
    emailDeliveries: emailDeliveryResponses(),
    nextEmailRefreshStatus: null,
    nextEmailActionStatus: null,
    nextEmailRefreshResponse: null,
    nextSnapshotStatus: null,
    nextConfirmationConflict: false,
    nextBookingCsrfFailure: false,
  };

  if (sessionActive) {
    await page.context().addCookies([{
      name: SESSION_COOKIE_NAME,
      value: SESSION_COOKIE_VALUE,
      url: "http://127.0.0.1:5173/api",
      httpOnly: true,
      sameSite: "Strict",
    }]);
  }

  await page.route(/\/api\/(?:auth|coach)\//, async (route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname.endsWith("/auth/coach/session")) {
      sessionCallCount += 1;
      requestOrder.push("session");
      if (sessionGate) await sessionGate;
      if (nextSessionStatus) {
        const status = nextSessionStatus;
        nextSessionStatus = null;
        await route.fulfill({ status, json: apiError("SESSION_UNAVAILABLE", "Session validation is unavailable.") });
        return;
      }
      const hasCookie = requestHasSessionCookie(request.headers().cookie);
      if (sessionActive && !sessionRevoked && hasCookie) {
        sessionValidated = true;
        await route.fulfill({ json: authLoginResponse() });
        return;
      }
      if (hasCookie && sessionRevoked) invalidSessionReplayCount += 1;
      sessionValidated = false;
      await route.fulfill({
        status: 401,
        headers: { "set-cookie": expiredSessionCookie() },
        json: apiError(hasCookie ? "INVALID_AUTH_TOKEN" : "AUTHENTICATION_REQUIRED", "Sign in is required."),
      });
      return;
    }

    if (url.pathname.endsWith("/auth/coach/sign-in")) {
      if (request.headers()["x-csrf-protection"] !== "1") {
        await route.fulfill({ status: 403, json: apiError("CSRF_VALIDATION_FAILED", "CSRF protection is required.") });
        return;
      }
      const body = postBody(request.postData());
      if (body.email === COACH_EMAIL && body.password === COACH_PASSWORD) {
        sessionActive = true;
        sessionRevoked = false;
        sessionValidated = true;
        expiredStatus = null;
        await route.fulfill({ headers: { "set-cookie": activeSessionCookie() }, json: authLoginResponse() });
        return;
      }
      await route.fulfill({ status: 401, json: apiError("INVALID_CREDENTIALS", "Invalid sign in credentials.") });
      return;
    }

    if (url.pathname.endsWith("/auth/coach/sign-out")) {
      logoutCallCount += 1;
      signOutBodies.push(request.postData() === null ? null : postBody(request.postData()));
      if (request.headers()["x-csrf-protection"] !== "1") {
        await route.fulfill({ status: 403, json: apiError("CSRF_VALIDATION_FAILED", "CSRF protection is required.") });
        return;
      }
      if (nextSignOutStatus) {
        const status = nextSignOutStatus;
        nextSignOutStatus = null;
        await route.fulfill({ status, json: apiError("SIGN_OUT_UNAVAILABLE", "Sign-out is unavailable.") });
        return;
      }
      sessionActive = false;
      sessionRevoked = true;
      sessionValidated = false;
      await route.fulfill({
        headers: { "set-cookie": expiredSessionCookie() },
        json: { success: true, message: "Signed out." },
      });
      return;
    }

    if (url.pathname.startsWith("/api/coach/")) {
      const authorization = request.headers().authorization ?? null;
      const csrf = request.headers()["x-csrf-protection"] ?? null;
      const hasSessionCookie = requestHasSessionCookie(request.headers().cookie);
      protectedCalls.push({
        path: url.pathname,
        method: request.method(),
        authorization,
        csrf,
        hasSessionCookie,
        beforeValidation: !sessionValidated,
        body: request.postData() === null ? null : postBody(request.postData()),
      });
      requestOrder.push(`protected:${url.pathname}`);

      if (!sessionActive || sessionRevoked || !hasSessionCookie) {
        await route.fulfill({ status: 401, json: apiError("INVALID_AUTH_TOKEN", "Sign in is required.") });
        return;
      }

      if (request.method() !== "GET" && csrf !== "1") {
        await route.fulfill({ status: 403, json: apiError("CSRF_VALIDATION_FAILED", "CSRF protection is required.") });
        return;
      }

      if (expiredStatus) {
        await route.fulfill({
          status: expiredStatus,
          json: apiError(
            expiredStatus === 401 ? "INVALID_AUTH_TOKEN" : "FORBIDDEN",
            expiredStatus === 401 ? "Your session expired. Sign in again." : "You do not have permission for this action.",
          ),
        });
        return;
      }

      if (url.pathname.endsWith("/coach/bookings") && state.nextBookingCsrfFailure) {
        state.nextBookingCsrfFailure = false;
        await route.fulfill({
          status: 403,
          json: apiError("CSRF_VALIDATION_FAILED", "The request origin is not trusted."),
        });
        return;
      }

      if (url.pathname.endsWith("/coach/availability") && delayAvailability && availabilityGate) {
        await availabilityGate;
        delayAvailability = false;
        availabilityGate = null;
      }

      if (url.pathname.endsWith("/calendar-links") && nextCalendarReadStatus) {
        const status = nextCalendarReadStatus;
        nextCalendarReadStatus = null;
        await route.fulfill({
          status,
          json: apiError(
            status === 401 ? "AUTH_REQUIRED" : "CALENDAR_LINKS_UNAVAILABLE",
            status === 401 ? "Your session expired. Sign in again." : "Calendar options are unavailable.",
          ),
        });
        return;
      }

      if (url.pathname.endsWith("/coach/command-center/snapshot") && state.nextSnapshotStatus) {
        const status = state.nextSnapshotStatus;
        state.nextSnapshotStatus = null;
        await route.fulfill({ status, json: apiError("SNAPSHOT_UNAVAILABLE", "Command center refresh is unavailable.") });
        return;
      }

      if (url.pathname.endsWith("/lesson-requests/req_live_1/confirm") && state.nextConfirmationConflict) {
        state.nextConfirmationConflict = false;
        await route.fulfill({
          status: 409,
          json: apiError(
            "LESSON_REQUEST_CONFIRM_CONFLICT",
            "This lesson request was already confirmed for a different time.",
          ),
        });
        return;
      }

      if (url.pathname.endsWith("/coach/email-deliveries") && request.method() === "GET") {
        if (state.nextEmailRefreshStatus) {
          const status = state.nextEmailRefreshStatus;
          state.nextEmailRefreshStatus = null;
          await route.fulfill({ status, json: apiError("DELIVERY_REFRESH_FAILED", "Delivery refresh failed.") });
          return;
        }
        const response = state.nextEmailRefreshResponse ?? state.emailDeliveries;
        state.nextEmailRefreshResponse = null;
        await route.fulfill({ json: response });
        return;
      }

      if (/\/coach\/email-deliveries\/[^/]+\/(?:resend|contact)$/.test(url.pathname)) {
        if (state.nextEmailActionStatus) {
          const status = state.nextEmailActionStatus;
          state.nextEmailActionStatus = null;
          await route.fulfill({ status, json: apiError("DELIVERY_ACTION_FAILED", "Delivery action failed.") });
          return;
        }
        const deliveryId = decodeURIComponent(url.pathname.split("/").at(-2) ?? "");
        const current = state.emailDeliveries.find((delivery) => delivery.id === deliveryId);
        if (!current) {
          await route.fulfill({ status: 404, json: apiError("NOT_FOUND", "Delivery was not found.") });
          return;
        }
        const updated = {
          ...current,
          status: "queued",
          issue: null,
          can_resend: false,
          can_edit_contact: false,
          updated_at: "2026-08-27T17:00:00Z",
        };
        state.emailDeliveries = state.emailDeliveries.map((delivery) => delivery.id === deliveryId ? updated : delivery);
        await route.fulfill({ json: updated });
        return;
      }

      await fulfillProtectedCoachRoute(route, url.pathname, state);
      return;
    }

    await route.fulfill({ status: 404, json: apiError("NOT_FOUND", "Route not mocked.") });
  });

  return {
    failProtectedRequestsWith(status: 401 | 403): void {
      expiredStatus = status;
    },
    revokeSession(): void {
      sessionRevoked = true;
      sessionActive = false;
    },
    failNextCalendarReadWith(status: 401 | 409): void {
      nextCalendarReadStatus = status;
    },
    failNextEmailRefreshWith(status: number): void {
      state.nextEmailRefreshStatus = status;
    },
    failNextEmailActionWith(status: number): void {
      state.nextEmailActionStatus = status;
    },
    respondToNextEmailRefreshWith(deliveries: Array<Record<string, unknown>>): void {
      state.nextEmailRefreshResponse = deliveries;
    },
    failNextSnapshotWith(status: number): void {
      state.nextSnapshotStatus = status;
    },
    failNextConfirmationWithConflict(): void {
      state.nextConfirmationConflict = true;
    },
    failNextSessionCheckWith(status: number): void {
      nextSessionStatus = status;
    },
    failNextSignOutWith(status: number): void {
      nextSignOutStatus = status;
    },
    failNextBookingWithCsrf(): void {
      state.nextBookingCsrfFailure = true;
    },
    delayNextAvailability(): void {
      delayAvailability = true;
      availabilityGate = new Promise<void>((resolve) => { releaseAvailabilityGate = resolve; });
    },
    releaseAvailability(): void {
      releaseAvailabilityGate?.();
      releaseAvailabilityGate = null;
    },
    releaseSessionValidation(): void {
      releaseSessionGate?.();
      releaseSessionGate = null;
      sessionGate = null;
    },
    logoutCalls(): number {
      return logoutCallCount;
    },
    sessionCalls(): number {
      return sessionCallCount;
    },
    invalidSessionReplays(): number {
      return invalidSessionReplayCount;
    },
    signOutBodies(): Array<Record<string, unknown> | null> {
      return [...signOutBodies];
    },
    requestOrder(): string[] {
      return [...requestOrder];
    },
    protectedCalls(): ProtectedCall[] {
      return [...protectedCalls];
    },
    protectedCallsBeforeValidation(): ProtectedCall[] {
      return protectedCalls.filter((call) => call.beforeValidation);
    },
  };
}

async function fulfillProtectedCoachRoute(
  route: Route,
  pathname: string,
  state: MockCoachApiState,
): Promise<void> {
  if (pathname.endsWith("/coach/command-center/snapshot")) {
    await route.fulfill({ json: commandCenterSnapshot(state.emailDeliveries) });
    return;
  }

  if (pathname.endsWith("/coach/availability")) {
    await route.fulfill({ json: availabilityResponse() });
    return;
  }

  if (pathname.endsWith("/coach/bookings")) {
    await route.fulfill({
      json: {
        success: true,
        appointment_id: "app_direct_123",
        message: "Appointment booked.",
        calendar_links: null,
      },
    });
    return;
  }

  if (pathname.endsWith("/lesson-requests/req_live_1/confirm")) {
    const emailDelivery = emailDeliveryResponse({
      id: "delivery-confirm-live",
      kind: "confirmation",
      status: "queued",
      client_name: "Sofia Martinez",
      request_token: "req_live_1",
      updated_at: "2026-08-27T17:05:00Z",
    });
    state.emailDeliveries = [emailDelivery, ...state.emailDeliveries.filter((item) => item.id !== emailDelivery.id)];
    await route.fulfill({
      json: {
        id: "req_live_1",
        status: "confirmed",
        appointment_id: "app_request_1",
        appointment_ids: ["app_request_1", "app_request_2"],
        message: "Lesson request is confirmed.",
        calendar_links: calendarLinksResponse("app_request_1", true),
        email_delivery: emailDelivery,
      },
    });
    return;
  }

  if (pathname.endsWith("/coach/appointments/app_direct_123/calendar-links")) {
    await route.fulfill({ json: calendarLinksResponse("app_direct_123", false) });
    return;
  }

  if (pathname.endsWith("/calendar.ics")) {
    await route.fulfill({
      contentType: "text/calendar; charset=utf-8",
      headers: { "content-disposition": 'attachment; filename="appointment.ics"' },
      body: "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n",
    });
    return;
  }

  await route.fulfill({ json: {} });
}

async function signInThroughUi(page: Page): Promise<void> {
  await page.goto("/");
  await page.getByLabel(/email/i).fill(COACH_EMAIL);
  await page.getByLabel(/password/i).fill(COACH_PASSWORD);
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page.getByRole("heading", { name: "Today schedule" })).toBeVisible();
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
    authenticated: true,
    expires_at: "2026-09-01T00:00:00Z",
    coach: authSessionResponse(),
  };
}

function activeSessionCookie(): string {
  return `${SESSION_COOKIE_NAME}=${SESSION_COOKIE_VALUE}; Path=/api; HttpOnly; SameSite=Strict; Max-Age=28800`;
}

function expiredSessionCookie(): string {
  return `${SESSION_COOKIE_NAME}=; Path=/api; HttpOnly; SameSite=Strict; Max-Age=0`;
}

function requestHasSessionCookie(cookieHeader: string | undefined): boolean {
  return cookieHeader?.split(";").some((part) => part.trim() === `${SESSION_COOKIE_NAME}=${SESSION_COOKIE_VALUE}`) ?? false;
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

function calendarLinksResponse(anchor: string, recurring: boolean): Record<string, unknown> {
  const google = "https://calendar.google.com/calendar/render?action=TEMPLATE";
  const googleEvents = [
    {
      start_iso: "2026-08-27T20:00:00Z",
      end_iso: "2026-08-27T20:45:00Z",
      url: `${google}&dates=20260827T200000Z%2F20260827T204500Z`,
    },
  ];
  if (recurring) {
    googleEvents.push({
      start_iso: "2026-09-03T20:00:00Z",
      end_iso: "2026-09-03T20:45:00Z",
      url: `${google}&dates=20260903T200000Z%2F20260903T204500Z`,
    });
  }
  const ics = `/api/coach/appointments/${anchor}/calendar.ics`;
  return { google: googleEvents[0].url, google_events: googleEvents, apple: ics, ics };
}

function commandCenterSnapshot(emailDeliveries: Array<Record<string, unknown>>): Record<string, unknown> {
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
    requests: [
      {
        id: "req_live_1",
        client_id: "client_sofia",
        client_name: "Sofia Martinez",
        requested_slot: {
          id: "offer_alt_req_live_1",
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
        },
        source: "client_invite",
        status: "pending",
        risk: "clear",
        submitted_at: "2026-08-26T18:00:00",
        note: "Client selected this time from an invite.",
        alternative_slots: [
          {
            id: "offer_alt_req_live_1",
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
          },
          {
            id: "offer_alt_req_live_2",
            start_time: "2026-08-27T17:00:00",
            end_time: "2026-08-27T17:45:00",
            travel_time_mins: 12,
            buffer_mins: 10,
            distance_miles: 2.1,
            target_location: {
              address: "820 Lakeview Dr, Austin, TX",
              lat: 30.289,
              lng: -97.739,
              is_instructor_facility: false,
            },
          },
        ],
      },
    ],
    email_deliveries: emailDeliveries,
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

function emailDeliveryResponses(): Array<Record<string, unknown>> {
  return [
    emailDeliveryResponse({
      id: "delivery-live-resend",
      status: "failed",
      issue: "temporary_failure",
      can_resend: true,
      client_name: "Sofia Martinez",
      updated_at: "2026-08-27T16:05:00Z",
    }),
    emailDeliveryResponse({
      id: "delivery-live-edit",
      kind: "invite",
      status: "skipped",
      issue: "missing_address",
      can_edit_contact: true,
      client_name: "Maya Chen",
      request_token: null,
      updated_at: "2026-08-27T16:04:00Z",
    }),
    emailDeliveryResponse({
      id: "delivery-live-delivered",
      status: "delivered",
      client_name: "Noah Patel",
      updated_at: "2026-08-27T16:03:00Z",
    }),
  ];
}

function emailDeliveryResponse(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "delivery-live",
    kind: "confirmation",
    status: "accepted",
    updated_at: "2026-08-27T16:00:00Z",
    client_id: "client_sofia",
    client_name: "Sofia Martinez",
    request_token: "req_live_1",
    issue: null,
    source_state: "active",
    can_resend: false,
    can_edit_contact: false,
    ...overrides,
  };
}
