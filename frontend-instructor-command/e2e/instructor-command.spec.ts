import { expect, test } from "@playwright/test";

test("navigates the instructor command screens with the local adapter", async ({ page }) => {
  await page.goto("/");

  await expect(page.locator("html")).toHaveAttribute("data-data-mode", "mock");
  await expect(page.getByText("Mock data", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Today schedule" })).toBeVisible();
  await page.getByRole("button", { name: "Find" }).click();
  await expect(page.getByRole("heading", { name: "Availability finder" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Find safe starts" })).toBeVisible();

  await page.getByRole("button", { name: "Client", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Client detail" })).toBeVisible();

  await page.getByRole("button", { name: "Requests", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Request management" })).toBeVisible();

  await page.getByRole("button", { name: "Settings" }).click();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
});

test("saves one canonical provider configuration while keeping scheduler rules separate", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Settings" }).click();

  await expect(page.getByRole("heading", { name: "Scheduling is ready" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Save provider settings" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Save scheduling policy" })).toBeVisible();

  await page.getByLabel("Display name").fill("Jordan Lee");
  await page.getByLabel("Business name Optional").fill("");
  await page.getByLabel("Service name").fill("Private aquatic coaching");
  const sundayHours = page.getByRole("group", { name: "Sunday working hours" });
  await sundayHours.getByRole("switch").check();
  await sundayHours.getByLabel("Opens").fill("10:00");
  await sundayHours.getByLabel("Closes").fill("14:00");
  await page.getByRole("button", { name: "Save provider settings" }).click();

  await expect(page.locator("[data-provider-save-status]")).toContainText("Provider settings saved");
  await expect(page.getByRole("heading", { name: "Scheduling is ready" })).toBeVisible();
  await page.getByRole("button", { name: "Today" }).click();
  await expect(page.locator(".operator-card")).toContainText("Jordan Lee");
  await page.getByRole("button", { name: "Settings" }).click();
  await expect(page.getByLabel("Display name")).toHaveValue("Jordan Lee");
  await expect(page.getByLabel("Business name Optional")).toHaveValue("");
  await expect(page.getByRole("group", { name: "Sunday working hours" }).getByRole("switch")).toBeChecked();
});

for (const width of [320, 375]) {
  test(`keeps provider setup one-handed without page overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 720 });
    await page.goto("/");
    await page.getByRole("button", { name: "Settings" }).click();

    const providerForm = page.locator("[data-provider-form]");
    for (const control of await providerForm.locator("input, select, button").all()) {
      const bounds = await control.boundingBox();
      expect(bounds?.height ?? 0).toBeGreaterThanOrEqual(44);
    }
    const overflow = await page.evaluate(() => ({
      document: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      body: document.body.scrollWidth - document.body.clientWidth,
    }));
    expect(overflow.document).toBeLessThanOrEqual(1);
    expect(overflow.body).toBeLessThanOrEqual(1);

    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    const sunday = page.locator('[data-weekday="sunday"]');
    const saveBar = page.locator("[data-provider-form] .provider-save-bar");
    await expect(sunday).toBeVisible();
    await expect(saveBar).toBeVisible();
    const sundayBounds = await sunday.boundingBox();
    const saveBounds = await saveBar.boundingBox();
    expect((sundayBounds?.y ?? 0) + (sundayBounds?.height ?? 0)).toBeLessThanOrEqual((saveBounds?.y ?? 0) + 1);
  });
}

test("renders truthful email delivery states and server-authorized recovery", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Requests", exact: true }).click();

  const deliverySection = page.getByRole("region", { name: "Email delivery" });
  await expect(deliverySection).toContainText("Queued to send");
  await expect(deliverySection).toContainText("Sending");
  await expect(deliverySection).toContainText("Accepted by email service");
  await expect(deliverySection).toContainText("Delivered");
  await expect(deliverySection).toContainText("Delivery failed");
  await expect(deliverySection).toContainText("Not sent");

  const accepted = page.locator('[data-email-delivery-card="delivery_accepted"]');
  await expect(accepted).toContainText("Delivery is not confirmed");
  const queuedUncertain = page.locator('[data-email-delivery-card="delivery_uncertain_queued"]');
  await expect(queuedUncertain).toContainText("may already have sent");
  await expect(queuedUncertain).toContainText("automatic retry is queued");
  await expect(queuedUncertain.getByRole("button", { name: "Resend safely" })).toHaveCount(0);
  const failedUncertain = page.locator('[data-email-delivery-card="delivery_uncertain_failed"]');
  await expect(failedUncertain).toContainText("Do not resend it");
  await expect(failedUncertain.getByRole("button", { name: "Resend safely" })).toHaveCount(0);

  const inactiveInvite = page.locator('[data-email-delivery-card="delivery_inactive_invite"]');
  await expect(inactiveInvite).toContainText("Invite link is no longer active");
  await expect(inactiveInvite.locator("button, input")).toHaveCount(0);
  const missingIntake = page.locator('[data-email-delivery-card="delivery_missing_intake"]');
  await expect(missingIntake).toContainText("contact editing is unavailable");
  await expect(missingIntake.getByRole("textbox")).toHaveCount(0);
});

test("resends and corrects contact with persistent focus and card-local feedback", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Requests", exact: true }).click();

  const resendCard = page.locator('[data-email-delivery-card="delivery_failed_resend"]');
  await resendCard.getByRole("button", { name: "Resend safely" }).click();
  await expect(resendCard).toContainText("Queued to send");
  await expect(resendCard.getByRole("status")).toContainText("Queued to send again");
  await expect(resendCard).toBeFocused();

  const editCard = page.locator('[data-email-delivery-card="delivery_skipped_editable"]');
  const input = editCard.getByLabel("Corrected email address");
  await input.fill("not-an-email");
  await editCard.getByRole("button", { name: "Update email and queue" }).click();
  await expect(editCard.getByRole("alert")).toContainText("Enter a valid email address");
  await expect(input).toBeFocused();

  await input.fill("maya@example.test");
  await editCard.getByRole("button", { name: "Update email and queue" }).click();
  await expect(editCard).toContainText("Queued to send");
  await expect(editCard.getByRole("status")).toContainText("Email updated and queued");
  await expect(editCard).toBeFocused();

  const refresh = page.getByRole("button", { name: "Refresh delivery status" });
  await refresh.click();
  await expect(page.getByRole("region", { name: "Email delivery" }).getByRole("status").first()).toContainText(
    "Delivery status refreshed",
  );
  await expect(refresh).toBeFocused();
});

test("keeps email recovery controls one-handed at 320px", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 720 });
  await page.goto("/");
  await page.getByRole("button", { name: "Requests", exact: true }).click();

  const section = page.getByRole("region", { name: "Email delivery" });
  const controls = section.locator("button, input");
  for (const control of await controls.all()) {
    const bounds = await control.boundingBox();
    expect(bounds?.height ?? 0).toBeGreaterThanOrEqual(44);
  }
  const overflow = await section.evaluate((element) => {
    const sectionBounds = element.getBoundingClientRect();
    return {
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      descendants: Array.from(element.querySelectorAll<HTMLElement>("*"))
        .map((child) => {
          const bounds = child.getBoundingClientRect();
          return {
            className: child.className,
            clientWidth: child.clientWidth,
            extendsLeft: bounds.left < sectionBounds.left - 1,
            extendsRight: bounds.right > sectionBounds.right + 1,
            scrollWidth: child.scrollWidth,
            tagName: child.tagName,
            text: child.textContent?.trim().slice(0, 80) ?? "",
          };
        })
        .filter(
          (child) =>
            child.scrollWidth > child.clientWidth + 1 || child.extendsLeft || child.extendsRight,
        ),
    };
  });
  expect(
    overflow.scrollWidth > overflow.clientWidth + 1,
    `Email delivery overflow: ${JSON.stringify(overflow)}`,
  ).toBe(false);

  const pageOverflow = await page.evaluate(() => ({
    body: { clientWidth: document.body.clientWidth, scrollWidth: document.body.scrollWidth },
    root: {
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    },
  }));
  expect(
    pageOverflow.body.scrollWidth > pageOverflow.body.clientWidth + 1 ||
      pageOverflow.root.scrollWidth > pageOverflow.root.clientWidth + 1,
    `Requests page overflow: ${JSON.stringify(pageOverflow)}`,
  ).toBe(false);
});

test("hands a directly booked persisted appointment to the instructor calendar", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Find" }).click();

  const bookLesson = page.getByRole("button", { name: "Book lesson" }).first();
  await bookLesson.click();

  const dialog = page.getByRole("dialog", { name: "Ready for your calendar" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toBeFocused();
  await expect(dialog.getByRole("link", { name: /Open Google Calendar/ })).toHaveAttribute(
    "href",
    /^https:\/\/calendar\.google\.com\/calendar\/render/,
  );
  await expect(dialog).toContainText("does not add the appointment automatically");
  await expect(dialog).not.toContainText(/added to (?:your|the) calendar/i);

  await page.keyboard.press("Shift+Tab");
  await expect(dialog.getByRole("button", { name: "Done" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "Close calendar options" })).toBeFocused();

  const downloadPromise = page.waitForEvent("download");
  await dialog.getByRole("button", { name: "Download .ics" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("appointment.ics");
  await expect(dialog.getByRole("status")).toContainText(/downloaded/i);

  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "Close calendar options" })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(dialog.getByRole("button", { name: "Done" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(bookLesson).toBeFocused();
});

test("uses confirmation IDs for a recurring request handoff", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Requests", exact: true }).click();
  await page.getByRole("button", { name: "Confirm" }).first().click();

  const dialog = page.getByRole("dialog", { name: "Ready for your calendar" });
  const occurrences = dialog.getByRole("list", { name: "Google Calendar occurrences" }).getByRole("link");
  await expect(dialog).toBeVisible();
  await expect(occurrences).toHaveCount(2);
  await expect(occurrences.nth(0)).toHaveAttribute("href", /dates=/);
  await expect(occurrences.nth(1)).toHaveAttribute("href", /dates=/);
  await dialog.getByRole("button", { name: "Done" }).click();

  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Requests", exact: true })).toBeFocused();
  await expect(page.locator('[data-email-delivery-card="delivery_confirmation_req_sofia_0827"]')).toContainText(
    "Queued to send",
  );
});

test("keeps calendar handoff controls usable at 320px", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 720 });
  await page.goto("/");
  await page.getByRole("button", { name: "Find" }).click();
  await page.getByRole("button", { name: "Book lesson" }).first().click();

  const dialog = page.getByRole("dialog", { name: "Ready for your calendar" });
  const bounds = await dialog.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(320);
  for (const control of await dialog.locator("a.calendar-action, button.calendar-action, button.calendar-done-action").all()) {
    const controlBounds = await control.boundingBox();
    expect(controlBounds?.height ?? 0).toBeGreaterThanOrEqual(44);
  }
  const hasHorizontalOverflow = await dialog.evaluate((element) => element.scrollWidth > element.clientWidth + 1);
  expect(hasHorizontalOverflow).toBe(false);
});
