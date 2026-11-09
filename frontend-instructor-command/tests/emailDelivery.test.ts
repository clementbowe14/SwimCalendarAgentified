import assert from "node:assert/strict";
import test from "node:test";
import type { CoachEmailDelivery } from "../src/api/types.js";
import {
  emptyDeliveryCardState,
  renderRequestManagement,
} from "../src/components/RequestManagement.js";
import {
  emailDeliveryLifecycleCopy,
  emailDeliveryPresentation,
  mergeEmailDeliveries,
} from "../src/domain/emailDelivery.js";
import { snapshot } from "../src/api/mockData.js";

test("email delivery copy distinguishes every truthful provider state", () => {
  assert.equal(emailDeliveryPresentation(delivery({ status: "queued" })).label, "Queued to send");
  assert.equal(emailDeliveryPresentation(delivery({ status: "processing" })).label, "Sending");
  assert.equal(emailDeliveryPresentation(delivery({ status: "accepted" })).label, "Accepted by email service");
  assert.match(emailDeliveryPresentation(delivery({ status: "accepted" })).detail, /delivery is not confirmed/i);
  assert.equal(emailDeliveryPresentation(delivery({ status: "delivered" })).label, "Delivered");
  assert.equal(emailDeliveryPresentation(delivery({ status: "failed" })).label, "Delivery failed");
  assert.equal(emailDeliveryPresentation(delivery({ status: "skipped" })).label, "Not sent");
});

test("uncertain queued and failed states say the email may have sent and never imply a safe resend", () => {
  const queued = emailDeliveryPresentation(delivery({ status: "queued", issue: "delivery_uncertain" }));
  const failed = emailDeliveryPresentation(delivery({ status: "failed", issue: "delivery_uncertain" }));

  assert.equal(queued.label, "Delivery status uncertain");
  assert.match(queued.detail, /may already have sent/i);
  assert.match(queued.detail, /automatic retry is queued/i);
  assert.equal(failed.label, "Delivery status uncertain");
  assert.match(failed.detail, /do not resend/i);
});

test("delivery renderer exposes only server-authorized recovery with card-local accessibility state", () => {
  const editable = delivery({
    id: "editable",
    status: "skipped",
    issue: "invalid_address",
    can_edit_contact: true,
  });
  const resendable = delivery({
    id: "resendable",
    status: "failed",
    issue: "temporary_failure",
    can_resend: true,
  });
  const uncertain = delivery({
    id: "uncertain",
    status: "queued",
    issue: "delivery_uncertain",
  });
  const inactive = delivery({
    id: "inactive",
    status: "failed",
    issue: "temporary_failure",
    source_state: "invite_inactive",
  });
  const card = emptyDeliveryCardState();
  card.draft = "typed@example.test";
  card.pending = "contact";
  card.fieldError = "Enter a valid email address.";

  const html = renderRequestManagement(
    { ...snapshot, email_deliveries: [editable, resendable, uncertain, inactive] },
    {
      deliveries: [editable, resendable, uncertain, inactive],
      cards: { editable: card },
      refreshPending: false,
      refreshResult: "",
      refreshError: "",
    },
  );

  assert.match(html, /Refresh delivery status/);
  assert.match(html, /Resend safely/);
  assert.match(html, /Corrected email address/);
  assert.match(html, /aria-busy="true"/);
  assert.match(html, /aria-invalid="true"/);
  assert.match(html, /role="status" aria-live="polite"/);
  assert.match(html, /Invite link is no longer active/);
  assert.equal((html.match(/Resend safely/g) ?? []).length, 1);
  assert.equal((html.match(/data-email-contact-form=/g) ?? []).length, 1);
});

test("missing-intake and inactive delivery rows remain readable but non-editable", () => {
  const missingIntake = delivery({
    status: "skipped",
    issue: "missing_address",
    can_edit_contact: false,
  });
  const inactive = delivery({ source_state: "appointment_inactive" });
  const missingHtml = renderRequestManagement(
    { ...snapshot, email_deliveries: [missingIntake] },
    {
      deliveries: [missingIntake], cards: {}, refreshPending: false, refreshResult: "", refreshError: "",
    },
  );

  assert.match(missingHtml, /contact editing is unavailable/i);
  assert.doesNotMatch(missingHtml, /data-email-contact-form/);
  assert.match(emailDeliveryLifecycleCopy(inactive), /appointment changed or is no longer active/i);
});

test("delivery merge is monotonic by ID and updated_at and preserves omitted last-known cards", () => {
  const current = delivery({ id: "same", status: "delivered", updated_at: "2026-08-27T16:05:00Z" });
  const stale = delivery({ id: "same", status: "queued", updated_at: "2026-08-27T16:04:00Z" });
  const newer = delivery({ id: "newer", status: "accepted", updated_at: "2026-08-27T16:06:00Z" });

  const merged = mergeEmailDeliveries([current], [stale, newer]);

  assert.deepEqual(merged.map((item) => item.id), ["newer", "same"]);
  assert.equal(merged.find((item) => item.id === "same")?.status, "delivered");
});

function delivery(overrides: Partial<CoachEmailDelivery> = {}): CoachEmailDelivery {
  return {
    id: "delivery",
    kind: "confirmation",
    status: "failed",
    updated_at: "2026-08-27T16:00:00Z",
    client_id: "client_sofia",
    client_name: "Sofia Martinez",
    request_token: "request-sofia",
    issue: null,
    source_state: "active",
    can_resend: false,
    can_edit_contact: false,
    ...overrides,
  };
}
