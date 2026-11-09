import type { CoachEmailDelivery, CommandCenterSnapshot, LessonRequest } from "../api/types.js";
import {
  emailDeliveryLifecycleCopy,
  emailDeliveryPresentation,
} from "../domain/emailDelivery.js";
import { requestDateLabel, slotSafetyFacts, summarizeRequests } from "../domain/screenModels.js";
import { escapeHtml } from "../lib/html.js";

export type DeliveryCardPendingAction = "resend" | "contact" | null;

export type DeliveryCardUiState = {
  draft: string;
  pending: DeliveryCardPendingAction;
  result: string;
  error: string;
  fieldError: string;
};

export type DeliverySectionUiState = {
  deliveries: CoachEmailDelivery[];
  cards: Record<string, DeliveryCardUiState>;
  refreshPending: boolean;
  refreshResult: string;
  refreshError: string;
};

export function renderRequestManagement(
  snapshot: CommandCenterSnapshot,
  deliveryState: DeliverySectionUiState = defaultDeliveryState(snapshot.email_deliveries),
): string {
  const summary = summarizeRequests(snapshot.requests);

  return `
    <section class="screen-grid requests-screen" aria-label="Request management">
      <section class="request-summary">
        <article><strong>${summary.clear}</strong><span>clear</span></article>
        <article><strong>${summary.atRisk}</strong><span>at risk</span></article>
        <article><strong>${summary.totalPending}</strong><span>pending</span></article>
      </section>
      <section class="request-list" aria-label="Lesson requests">
        ${snapshot.requests.map(renderRequest).join("")}
      </section>
      ${renderEmailDeliverySection(deliveryState)}
    </section>
  `;
}

function renderRequest(request: LessonRequest): string {
  const isPending = request.status === "pending";
  return `
    <article class="request-card ${request.risk === "at_risk" ? "warn" : "clear"}">
      <div class="request-head">
        <div>
          <p class="eyebrow">${escapeHtml(request.source.replaceAll("_", " "))}</p>
          <h2>${escapeHtml(request.client_name)}</h2>
          <span>${escapeHtml(requestDateLabel(request))}</span>
        </div>
        <span class="status-chip">${escapeHtml(requestStatusLabel(request))}</span>
      </div>
      <p>${escapeHtml(request.note)}</p>
      <div class="request-facts">${escapeHtml(slotSafetyFacts(request.requested_slot))}</div>
      ${
        isPending
          ? `<div class="request-actions">
              <button class="primary-action" type="button" data-confirm-request="${escapeHtml(request.id)}" data-calendar-focus="request:${escapeHtml(request.id)}">Confirm</button>
              <button class="secondary-action" type="button" data-alternatives-request="${escapeHtml(request.id)}">Alternatives</button>
            </div>`
          : ""
      }
    </article>
  `;
}

function renderEmailDeliverySection(state: DeliverySectionUiState): string {
  const refreshMessage = state.refreshError
    ? `<p class="delivery-section-error" role="alert">${escapeHtml(state.refreshError)}</p>`
    : `<p class="delivery-section-result" role="status" aria-live="polite">${escapeHtml(state.refreshResult)}</p>`;
  return `
    <section class="email-delivery-section" aria-labelledby="email-delivery-title">
      <header class="email-delivery-heading">
        <div>
          <p class="eyebrow">Client communication</p>
          <h2 id="email-delivery-title">Email delivery</h2>
          <p>Provider status only. Acceptance does not confirm delivery.</p>
        </div>
        <button
          class="secondary-action delivery-refresh-action"
          type="button"
          data-refresh-email-deliveries
          data-delivery-focus="refresh"
          ${state.refreshPending ? "disabled" : ""}
        >${state.refreshPending ? "Refreshing…" : "Refresh delivery status"}</button>
      </header>
      ${refreshMessage}
      ${
        state.deliveries.length
          ? `<ul class="email-delivery-list">${state.deliveries
              .map((delivery, index) => renderEmailDeliveryCard(delivery, state.cards[delivery.id], index))
              .join("")}</ul>`
          : `<p class="email-delivery-empty">No email delivery activity yet.</p>`
      }
    </section>
  `;
}

function renderEmailDeliveryCard(
  delivery: CoachEmailDelivery,
  cardState: DeliveryCardUiState | undefined,
  index: number,
): string {
  const ui = cardState ?? emptyDeliveryCardState();
  const presentation = emailDeliveryPresentation(delivery);
  const lifecycleCopy = emailDeliveryLifecycleCopy(delivery);
  const cardId = `email-delivery-${index}`;
  const deliveryId = escapeHtml(delivery.id);
  const disabled = ui.pending ? "disabled" : "";
  const describedBy = `${cardId}-email-help${ui.fieldError ? ` ${cardId}-field-error` : ""}`;

  return `
    <li>
      <article
        class="email-delivery-card delivery-${presentation.tone}"
        aria-labelledby="${cardId}-title"
        aria-busy="${ui.pending ? "true" : "false"}"
        tabindex="-1"
        data-email-delivery-card="${deliveryId}"
        data-delivery-focus="card:${deliveryId}"
      >
        <div class="email-delivery-card-head">
          <div>
            <p class="eyebrow">${delivery.kind === "invite" ? "Invitation email" : "Confirmation email"}</p>
            <h3 id="${cardId}-title">${escapeHtml(delivery.client_name)}</h3>
          </div>
          <span class="delivery-status delivery-status-${presentation.tone}">${escapeHtml(presentation.label)}</span>
        </div>
        <p class="delivery-status-detail">${escapeHtml(presentation.detail)}</p>
        ${lifecycleCopy ? `<p class="delivery-lifecycle-note">${escapeHtml(lifecycleCopy)}</p>` : ""}
        <p class="delivery-updated"><time datetime="${escapeHtml(delivery.updated_at)}">Updated ${escapeHtml(formatDeliveryUpdatedAt(delivery.updated_at))}</time></p>
        ${renderDeliveryActions(delivery, ui, cardId, describedBy, disabled)}
        <div class="delivery-card-feedback">
          ${ui.error ? `<p class="delivery-card-error" role="alert">${escapeHtml(ui.error)}</p>` : ""}
          <p class="delivery-card-result" role="status" aria-live="polite">${escapeHtml(ui.result)}</p>
        </div>
      </article>
    </li>
  `;
}

function renderDeliveryActions(
  delivery: CoachEmailDelivery,
  ui: DeliveryCardUiState,
  cardId: string,
  describedBy: string,
  disabled: string,
): string {
  if (delivery.can_resend) {
    return `
      <div class="email-delivery-actions">
        <button
          class="primary-action"
          type="button"
          data-resend-email-delivery="${escapeHtml(delivery.id)}"
          data-delivery-focus="resend:${escapeHtml(delivery.id)}"
          ${disabled}
        >${ui.pending === "resend" ? "Queueing…" : "Resend safely"}</button>
      </div>
    `;
  }
  if (delivery.can_edit_contact) {
    return `
      <form class="email-correction-form" data-email-contact-form="${escapeHtml(delivery.id)}" novalidate>
        <label for="${cardId}-email">
          <span>Corrected email address</span>
          <input
            id="${cardId}-email"
            name="email"
            type="email"
            inputmode="email"
            autocomplete="email"
            value="${escapeHtml(ui.draft)}"
            aria-describedby="${describedBy}"
            aria-invalid="${ui.fieldError ? "true" : "false"}"
            data-email-delivery-draft="${escapeHtml(delivery.id)}"
            data-delivery-focus="email:${escapeHtml(delivery.id)}"
            ${disabled}
          />
        </label>
        <p id="${cardId}-email-help" class="delivery-field-help">This updates the saved contact and queues this email.</p>
        ${
          ui.fieldError
            ? `<p id="${cardId}-field-error" class="delivery-field-error" role="alert">${escapeHtml(ui.fieldError)}</p>`
            : ""
        }
        <button
          class="primary-action"
          type="submit"
          data-delivery-focus="contact:${escapeHtml(delivery.id)}"
          ${disabled}
        >${ui.pending === "contact" ? "Updating…" : "Update email and queue"}</button>
      </form>
    `;
  }
  return "";
}

function requestStatusLabel(request: LessonRequest): string {
  if (request.status === "pending") return request.risk === "at_risk" ? "At risk" : "Clear";
  if (request.status === "approved") return "Confirmed";
  if (request.status === "needs_alternatives") return "Needs alternatives";
  if (request.status === "expired") return "Expired";
  return "Declined";
}

function defaultDeliveryState(deliveries: CoachEmailDelivery[]): DeliverySectionUiState {
  return {
    deliveries,
    cards: {},
    refreshPending: false,
    refreshResult: "",
    refreshError: "",
  };
}

export function emptyDeliveryCardState(): DeliveryCardUiState {
  return {
    draft: "",
    pending: null,
    result: "",
    error: "",
    fieldError: "",
  };
}

function formatDeliveryUpdatedAt(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "recently";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}
