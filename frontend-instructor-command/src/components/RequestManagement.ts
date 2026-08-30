import type { CommandCenterSnapshot, LessonRequest } from "../api/types.js";
import { requestDateLabel, slotSafetyFacts, summarizeRequests } from "../domain/screenModels.js";
import { escapeHtml } from "../lib/html.js";

export function renderRequestManagement(snapshot: CommandCenterSnapshot): string {
  const summary = summarizeRequests(snapshot.requests);

  return `
    <section class="screen-grid requests-screen" aria-label="Request management">
      <section class="request-summary">
        <article><strong>${summary.clear}</strong><span>clear</span></article>
        <article><strong>${summary.atRisk}</strong><span>at risk</span></article>
        <article><strong>${summary.totalPending}</strong><span>pending</span></article>
      </section>
      <section class="request-list">
        ${snapshot.requests.map(renderRequest).join("")}
      </section>
    </section>
  `;
}

function renderRequest(request: LessonRequest): string {
  return `
    <article class="request-card ${request.risk === "at_risk" ? "warn" : "clear"}">
      <div class="request-head">
        <div>
          <p class="eyebrow">${escapeHtml(request.source.replaceAll("_", " "))}</p>
          <h2>${escapeHtml(request.client_name)}</h2>
          <span>${escapeHtml(requestDateLabel(request))}</span>
        </div>
        <span class="status-chip">${escapeHtml(request.risk === "at_risk" ? "At risk" : "Clear")}</span>
      </div>
      <p>${escapeHtml(request.note)}</p>
      <div class="request-facts">${escapeHtml(slotSafetyFacts(request.requested_slot))}</div>
      <div class="request-actions">
        <button class="primary-action" type="button" data-approve-request="${escapeHtml(request.id)}" data-slot-id="${escapeHtml(request.requested_slot.id)}">Approve</button>
        <button class="secondary-action" type="button" data-alternatives-request="${escapeHtml(request.id)}">Alternatives</button>
      </div>
    </article>
  `;
}
