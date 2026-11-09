import type { CoachEmailDelivery } from "../api/types.js";

export type EmailDeliveryPresentation = {
  label: string;
  detail: string;
  tone: "neutral" | "progress" | "success" | "warning" | "danger";
};

export function emailDeliveryPresentation(delivery: CoachEmailDelivery): EmailDeliveryPresentation {
  if (delivery.status === "queued" && delivery.issue === "delivery_uncertain") {
    return {
      label: "Delivery status uncertain",
      detail: "This email may already have sent. An automatic retry is queued, so do not resend it manually.",
      tone: "warning",
    };
  }
  if (delivery.status === "queued") {
    return {
      label: "Queued to send",
      detail: "Waiting for the email service. Delivery is not confirmed.",
      tone: "progress",
    };
  }
  if (delivery.status === "processing") {
    return {
      label: "Sending",
      detail: "The email service is processing this message. Delivery is not confirmed.",
      tone: "progress",
    };
  }
  if (delivery.status === "accepted") {
    return {
      label: "Accepted by email service",
      detail: "The email service accepted this message. Delivery is not confirmed.",
      tone: "neutral",
    };
  }
  if (delivery.status === "delivered") {
    return {
      label: "Delivered",
      detail: "The email service reported this message as delivered.",
      tone: "success",
    };
  }
  if (delivery.status === "failed" && delivery.issue === "delivery_uncertain") {
    return {
      label: "Delivery status uncertain",
      detail: "This email may already have sent. Do not resend it.",
      tone: "warning",
    };
  }
  if (delivery.status === "failed") {
    return {
      label: "Delivery failed",
      detail: failedDeliveryDetail(delivery),
      tone: "danger",
    };
  }
  return {
    label: "Not sent",
    detail: skippedDeliveryDetail(delivery),
    tone: "warning",
  };
}

export function emailDeliveryLifecycleCopy(delivery: CoachEmailDelivery): string {
  if (delivery.source_state === "invite_inactive") {
    return "Invite link is no longer active. Delivery recovery is unavailable.";
  }
  if (delivery.source_state === "appointment_inactive") {
    return "This appointment changed or is no longer active. Delivery recovery is unavailable.";
  }
  return "";
}

export function mergeEmailDeliveries(
  current: readonly CoachEmailDelivery[],
  incoming: readonly CoachEmailDelivery[],
): CoachEmailDelivery[] {
  const merged = new Map(current.map((delivery) => [delivery.id, delivery]));

  for (const delivery of incoming) {
    const existing = merged.get(delivery.id);
    if (!existing || isNewer(delivery.updated_at, existing.updated_at)) {
      merged.set(delivery.id, delivery);
    }
  }

  return [...merged.values()].sort((left, right) => {
    const byUpdated = timestamp(right.updated_at) - timestamp(left.updated_at);
    return byUpdated || left.id.localeCompare(right.id);
  });
}

function isNewer(candidate: string, current: string): boolean {
  const candidateTime = timestamp(candidate);
  const currentTime = timestamp(current);
  if (!Number.isFinite(candidateTime)) return false;
  if (!Number.isFinite(currentTime)) return true;
  return candidateTime > currentTime;
}

function timestamp(value: string): number {
  return Date.parse(value);
}

function failedDeliveryDetail(delivery: CoachEmailDelivery): string {
  if (delivery.issue === "retries_exhausted") {
    return "Automatic delivery attempts ended without a confirmed send.";
  }
  if (delivery.issue === "temporary_failure") {
    return "The email service could not send this message. It can be safely queued again.";
  }
  if (delivery.issue === "permanent_failure") {
    return "The email service rejected this message. Review the contact before future notifications.";
  }
  return "The email could not be delivered. No safe resend is available.";
}

function skippedDeliveryDetail(delivery: CoachEmailDelivery): string {
  if (delivery.issue === "invalid_address") {
    return delivery.can_edit_contact
      ? "The saved email address was invalid. Enter a corrected address to queue this email."
      : "The saved email address was invalid, and contact editing is unavailable for this delivery.";
  }
  if (delivery.issue === "missing_address") {
    return delivery.can_edit_contact
      ? "No email address was available. Add one to queue this email."
      : "No email address was available, and contact editing is unavailable for this delivery.";
  }
  return "No email was sent.";
}
