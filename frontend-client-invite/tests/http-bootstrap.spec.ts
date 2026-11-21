import { expect, test, type Page } from "@playwright/test";
import type { ClientInviteEnvelope } from "../src/api/types";

const apiPattern = "http://127.0.0.1:3109/api/public/client-invites/**";
const serverEnvelope: ClientInviteEnvelope = {
  invite_token: "server-invite-token",
  instructor_name: "Jordan Lee",
  business_name: "Clearwater Services",
  service_name: "On-site session",
  service_location: {
    address: "500 Provider Plaza",
    lat: 30.25,
    lng: -97.75,
  },
  expires_at_iso: "2027-01-01T00:00:00Z",
  preferred_duration_mins: 60,
  allowed_durations_mins: [30, 60],
  intake: {
    client_name: "Avery Chen",
    guardian_phone: "(555) 010-2020",
    guardian_email: "avery@example.test",
    notes: "Server-provided notes.",
    location_type: "home",
    location: {
      address: "42 Server Lane",
      lat: 30.2,
      lng: -97.7,
      is_instructor_facility: false,
    },
  },
};

test("delayed HTTP bootstrap remains neutral and fixture-free until server data arrives", async ({ page }) => {
  let releaseResponse: () => void = () => undefined;
  const submittedIntakes: ClientInviteEnvelope["intake"][] = [];
  const responseGate = new Promise<void>((resolve) => { releaseResponse = resolve; });
  await page.route(apiPattern, async (route) => {
    await responseGate;
    if (route.request().method() === "POST") {
      submittedIntakes.push(route.request().postDataJSON() as ClientInviteEnvelope["intake"]);
    }
    await route.fulfill({ json: serverEnvelope });
  });

  await page.goto("/?invite_token=server-invite-token");

  await expectHttpMode(page);
  await expect(page.getByRole("heading", { name: "Loading your invitation" })).toBeVisible();
  await expectFixtureFree(page);

  releaseResponse();
  await expect(page.getByLabel("Client name")).toHaveValue("Avery Chen");
  await expect(page.getByText("Clearwater Services", { exact: true })).toBeVisible();
  await expect(page.getByText(/Jordan Lee/).first()).toBeVisible();
  await expectNoMockFixtureText(page);
  await page.getByRole("button", { name: "Provider location" }).click();
  await expect(page.getByLabel("Service location")).toHaveValue("500 Provider Plaza");
  await expect(page.getByLabel("Service location")).toHaveAttribute("readonly", "");
  await page.getByRole("button", { name: "My location" }).click();
  await expect(page.getByLabel("Appointment address")).toHaveValue("42 Server Lane");
  await page.getByRole("button", { name: "Continue to availability" }).click();
  await expect(page.getByRole("heading", { name: "Choose the times that suit you" })).toBeVisible();
  expect(submittedIntakes.at(-1)?.location).toEqual({
    address: "42 Server Lane",
    lat: 30.2,
    lng: -97.7,
    is_instructor_facility: false,
  });
  await expectNoMockFixtureText(page);
});

test("facility selection submits the configured facility coordinates and flag", async ({ page }) => {
  const submittedIntakes: ClientInviteEnvelope["intake"][] = [];
  await page.route(apiPattern, async (route) => {
    if (route.request().method() === "POST") {
      submittedIntakes.push(route.request().postDataJSON() as ClientInviteEnvelope["intake"]);
    }
    await route.fulfill({ json: serverEnvelope });
  });

  await page.goto("/?invite_token=server-invite-token");
  await page.getByRole("button", { name: "Provider location" }).click();
  await page.getByRole("button", { name: "Continue to availability" }).click();
  await expect(page.getByRole("heading", { name: "Choose the times that suit you" })).toBeVisible();
  expect(submittedIntakes.at(-1)?.location).toEqual({
    address: "500 Provider Plaza",
    lat: 30.25,
    lng: -97.75,
    is_instructor_facility: true,
  });
});

test("default backend address sentinel stays empty until the client enters a location", async ({ page }) => {
  const submittedIntakes: ClientInviteEnvelope["intake"][] = [];
  const emptyAddressEnvelope: ClientInviteEnvelope = {
    ...serverEnvelope,
    intake: {
      ...serverEnvelope.intake,
      location: {
        address: "Enter home pool address",
        lat: null,
        lng: null,
        is_instructor_facility: false,
      },
    },
  };
  await page.route(apiPattern, async (route) => {
    if (route.request().method() === "POST") {
      submittedIntakes.push(route.request().postDataJSON() as ClientInviteEnvelope["intake"]);
    }
    await route.fulfill({ json: emptyAddressEnvelope });
  });

  await page.goto("/?invite_token=server-invite-token");
  const address = page.getByLabel("Appointment address");
  await expect(address).toHaveValue("");
  await expect(address).toHaveAttribute("placeholder", "Enter appointment address");

  await page.getByRole("button", { name: "Continue to availability" }).click();
  expect(submittedIntakes).toHaveLength(0);
  await expect(address).toBeFocused();

  await address.fill("77 Client Way");
  await page.getByRole("button", { name: "Continue to availability" }).click();
  await expect(page.getByRole("heading", { name: "Choose the times that suit you" })).toBeVisible();
  expect(submittedIntakes.at(-1)?.location.address).toBe("77 Client Way");
});

test("missing HTTP invite token shows an invalid-link state without making a request", async ({ page }) => {
  let inviteRequests = 0;
  await page.route(apiPattern, async (route) => {
    inviteRequests += 1;
    await route.abort();
  });

  await page.goto("/");

  await expectHttpMode(page);
  await expect(page.getByRole("heading", { name: "Invitation link needed" })).toBeVisible();
  await expectFixtureFree(page);
  expect(inviteRequests).toBe(0);
});

test("failed HTTP bootstrap stays fixture-free and retries the same explicit token", async ({ page }) => {
  let failRequests = true;
  const requestedTokens: string[] = [];
  await page.route(apiPattern, async (route) => {
    requestedTokens.push(new URL(route.request().url()).pathname.split("/").at(-1) ?? "");
    if (failRequests) {
      await route.fulfill({
        status: 503,
        json: { error: { code: "INVITE_UNAVAILABLE", message: "Invitation service is unavailable." } },
      });
      return;
    }
    await route.fulfill({ json: serverEnvelope });
  });

  await page.goto("/?invite_token=server-invite-token");

  await expectHttpMode(page);
  await expect(page.getByRole("heading", { name: "We couldn’t load this invitation" })).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("Invitation service is unavailable");
  await expectFixtureFree(page);

  failRequests = false;
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByLabel("Client name")).toHaveValue("Avery Chen");
  expect(requestedTokens.length).toBeGreaterThanOrEqual(2);
  expect(new Set(requestedTokens)).toEqual(new Set(["server-invite-token"]));
});

async function expectHttpMode(page: Page) {
  await expect(page.locator("html")).toHaveAttribute("data-data-mode", "http");
  await expect(page.getByText("Mock data", { exact: true })).toHaveCount(0);
}

async function expectFixtureFree(page: Page) {
  await expectNoMockFixtureText(page);
  await expect(page.getByLabel("Client name")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Continue to availability" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Show available times" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Request this time" })).toHaveCount(0);
}

async function expectNoMockFixtureText(page: Page) {
  for (const fixtureValue of [
    "Sofia Martinez",
    "Riley",
    "Swim Command",
    "ana.martinez",
    "820 Lakeview",
    "Riverside facility",
  ]) {
    await expect(page.getByText(new RegExp(fixtureValue, "i"))).toHaveCount(0);
  }
}
