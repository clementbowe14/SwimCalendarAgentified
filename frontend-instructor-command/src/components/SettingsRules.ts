import type { SchedulingPolicy } from "../api/types.js";
import type {
  AvailabilityUiState,
  SchedulingPolicyDraft,
  SchedulingPolicyMinuteField,
} from "../domain/schedulingPolicy.js";
import { escapeHtml } from "../lib/html.js";

export type SchedulingPolicyRenderState = {
  pending?: boolean;
  error?: string;
  success?: string;
  conflict?: boolean;
  availability?: AvailabilityUiState;
};

export function renderSettingsRules(
  policy: SchedulingPolicy,
  draft: SchedulingPolicyDraft,
  showSignOut = false,
  status: SchedulingPolicyRenderState = {},
  providerSettings = "",
  calendarSettings = "",
): string {
  const controlsDisabled = status.pending ? " disabled" : "";
  const saveDisabled = status.pending || status.conflict ? " disabled" : "";
  const availability = status.availability ?? { status: "idle", message: "" };
  return `
    <section class="screen-grid settings-screen" aria-label="Settings">
      ${calendarSettings}
      ${providerSettings}
      <form
        class="scheduler-rules-form scheduling-policy-form"
        data-scheduling-policy-form
        novalidate
        aria-busy="${status.pending ? "true" : "false"}"
      >
        <section class="rules-overview scheduling-policy-heading">
          <div>
            <p class="eyebrow">Scheduling policy</p>
            <h2>Safe transition time</h2>
            <span>Set the recovery time the availability engine protects between appointments.</span>
          </div>
          <span class="metric-pill">Revision ${policy.revision}</span>
        </section>

        ${status.conflict ? `
          <div class="policy-conflict" role="alert" tabindex="-1" data-policy-conflict>
            <div>
              <strong>Saved policy changed elsewhere</strong>
              <span>Your entries are still here. Reload the saved policy before making another update.</span>
            </div>
            <button class="secondary-action" type="button" data-reload-scheduling-policy${controlsDisabled}>
              Reload saved policy
            </button>
          </div>
        ` : ""}
        ${status.error ? `
          <div class="error-banner policy-error" role="alert" tabindex="-1" data-policy-error>
            ${escapeHtml(status.error)}
          </div>
        ` : ""}

        <fieldset class="rules-list scheduling-policy-controls"${controlsDisabled}>
          <legend class="sr-only">Scheduling policy controls</legend>
          <div class="policy-switch-row">
            <div>
              <strong>Distance-based buffers</strong>
              <span>Protect extra transition time after travel from a previous appointment.</span>
            </div>
            <label class="switch-control policy-switch">
              <span class="sr-only">Distance-based buffers</span>
              <input
                name="distance_buffers_enabled"
                type="checkbox"
                role="switch"
                aria-label="Distance-based buffers"
                data-policy-toggle
                ${draft.distance_buffers_enabled ? "checked" : ""}${controlsDisabled}
              />
              <span class="switch-track" aria-hidden="true"><span></span></span>
            </label>
          </div>

          <section class="policy-control-group" aria-labelledby="distance-tier-title">
            <div class="section-heading">
              <p class="eyebrow">Distance tiers</p>
              <h3 id="distance-tier-title">Buffer minutes</h3>
              <p>Ranges are managed by the service. Adjust only the protected minutes.</p>
            </div>
            <div class="policy-stepper-list">
              ${policy.distance_buffer_tiers.map((tier, index) => {
                const draftTier = draft.distance_buffer_tiers[index];
                const value = draftTier?.id === tier.id ? draftTier.buffer_mins : String(tier.buffer_mins);
                return renderMinuteControl(
                  `tier.${tier.id}.buffer_mins`,
                  tier.label,
                  rangeLabel(tier.min_miles, tier.max_miles),
                  value,
                  Boolean(status.pending),
                );
              }).join("")}
            </div>
          </section>

          <section class="policy-control-group" aria-labelledby="recovery-title">
            <div class="section-heading">
              <p class="eyebrow">After a break</p>
              <h3 id="recovery-title">Post-break recovery</h3>
              <p>This recovery applies even when distance-based buffers are off.</p>
            </div>
            ${renderMinuteControl(
              "post_break_recovery_mins",
              "Post-break recovery",
              "Protected reset time before the next appointment",
              draft.post_break_recovery_mins,
              Boolean(status.pending),
            )}
          </section>

          <aside class="policy-invariants" aria-label="Always-on scheduling rules">
            <strong>Always accounted for</strong>
            <ul>
              <li>Safe starts align to 15-minute local-time boundaries.</li>
              <li>The first lesson includes travel from your base, but no distance buffer.</li>
              <li>Travel, working hours, and overlap protection stay on.</li>
            </ul>
          </aside>
        </fieldset>

        ${availability.status === "refreshing" ? `
          <div class="availability-policy-status refreshing" role="status" aria-live="polite">
            ${escapeHtml(availability.message || "Rechecking safe starts…")}
          </div>
        ` : ""}
        ${availability.status === "stale" ? `
          <div class="availability-policy-status stale" role="alert">
            <span>${escapeHtml(availability.message)}</span>
            <button class="secondary-action" type="button" data-retry-availability>Retry safe starts</button>
          </div>
        ` : ""}

        <div class="provider-save-bar policy-save-bar">
          <p class="provider-save-status" data-policy-save-status role="status" aria-live="polite">
            ${escapeHtml(status.success ?? "")}
          </p>
          <button class="primary-action" type="submit"${saveDisabled}>
            ${status.pending ? "Saving policy…" : "Save scheduling policy"}
          </button>
        </div>
      </form>
      ${showSignOut ? `<section class="settings-actions" aria-label="Account actions"><button class="danger-action" data-sign-out type="button">Sign out</button></section>` : ""}
    </section>
  `;
}

function renderMinuteControl(
  field: SchedulingPolicyMinuteField,
  label: string,
  description: string,
  value: string,
  disabled: boolean,
): string {
  const domId = `policy-${field.replace(/[^a-z0-9_-]+/gi, "-")}`;
  const disabledAttribute = disabled ? " disabled" : "";
  const safeLabel = escapeHtml(label);
  const safeValue = escapeHtml(value);
  return `
    <div class="policy-minute-row" data-policy-minute-row="${escapeHtml(field)}">
      <div class="policy-minute-copy">
        <strong id="${domId}-label">${safeLabel}</strong>
        <span id="${domId}-description">${escapeHtml(description)}</span>
      </div>
      <div class="minute-stepper" role="group" aria-labelledby="${domId}-label">
        <button
          type="button"
          class="stepper-action"
          aria-label="Decrease ${safeLabel}, currently ${safeValue} minutes"
          data-policy-step="${escapeHtml(field)}"
          data-policy-step-delta="-1"
          data-policy-focus="${escapeHtml(field)}:decrease"${disabledAttribute}
        >−</button>
        <label class="minute-input" for="${domId}">
          <span class="sr-only">${safeLabel} minutes</span>
          <input
            id="${domId}"
            name="${escapeHtml(field)}"
            type="number"
            inputmode="numeric"
            min="0"
            max="240"
            step="1"
            value="${safeValue}"
            aria-labelledby="${domId}-label"
            aria-describedby="${domId}-description ${domId}-unit"
            data-policy-minute="${escapeHtml(field)}"${disabledAttribute}
          />
          <span id="${domId}-unit" aria-hidden="true">min</span>
        </label>
        <button
          type="button"
          class="stepper-action"
          aria-label="Increase ${safeLabel}, currently ${safeValue} minutes"
          data-policy-step="${escapeHtml(field)}"
          data-policy-step-delta="1"
          data-policy-focus="${escapeHtml(field)}:increase"${disabledAttribute}
        >+</button>
      </div>
    </div>
  `;
}

function rangeLabel(min: number, max: number | null): string {
  if (max === null) return `${formatMiles(min)} miles and farther`;
  if (min === 0) return `Up to ${formatMiles(max)} miles`;
  return `${formatMiles(min)}–${formatMiles(max)} miles`;
}

function formatMiles(value: number): string {
  return Number.isInteger(value) ? value.toFixed(0) : String(value);
}
