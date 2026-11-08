import type { SchedulerRules } from "../api/types.js";
import { escapeHtml } from "../lib/html.js";

export type SettingsRenderState = {
  pending?: boolean;
  error?: string;
};

export function renderSettingsRules(
  rules: SchedulerRules,
  showSignOut = false,
  status: SettingsRenderState = {},
): string {
  const disabled = status.pending ? " disabled" : "";
  return `
    <section class="screen-grid settings-screen" aria-label="Settings">
      <form data-rules-form>
        <section class="rules-overview">
          <div>
            <p class="eyebrow">Constraint engine</p>
            <h2>Scheduling buffers</h2>
            <span>Configure travel recovery and safe start boundaries.</span>
          </div>
          <button class="primary-action" type="submit"${disabled}>${status.pending ? "Saving..." : "Save rules"}</button>
        </section>
        <div aria-live="polite" class="settings-status">${status.pending ? "Saving scheduler rules." : ""}</div>
        ${status.error ? `<div class="error-banner" role="alert" tabindex="-1" data-settings-error>${escapeHtml(status.error)}</div>` : ""}
        <fieldset class="rules-list"${disabled}>
          <legend class="sr-only">Buffer rules</legend>
          <label class="rule-row">
            <span><strong>Enable scheduling buffers</strong><span>Travel time and rounding remain enforced when disabled.</span></span>
            <input name="buffers_enabled" type="checkbox"${rules.buffers_enabled ? " checked" : ""} />
          </label>
          <label class="rule-row">
            <span><strong>Apply a buffer at shift start</strong><span>Add the distance-tier buffer before the first lesson.</span></span>
            <input name="apply_distance_buffer_at_shift_start" type="checkbox"${rules.apply_distance_buffer_at_shift_start ? " checked" : ""} />
          </label>
          <label class="rule-row">
            <span><strong>Rounding increment</strong><span>Available starts align to this local-time boundary.</span></span>
            <select name="rounding_increment_mins"${disabled}>
              ${[5, 10, 15, 30].map((value) => `<option value="${value}"${rules.rounding_increment_mins === value ? " selected" : ""}>${value} minutes</option>`).join("")}
            </select>
          </label>
          <label class="rule-row">
            <span><strong>Post-break buffer</strong><span>Recovery time after a protected break.</span></span>
            <input name="post_break_buffer_mins" type="number" min="0" max="240" step="1" value="${rules.post_break_buffer_mins}"${disabled} />
          </label>
          ${rules.distance_buffer_tiers.map((tier) => `
            <label class="rule-row">
              <span><strong>${escapeHtml(tier.label)}</strong><span>${rangeLabel(tier.min_miles, tier.max_miles)}</span></span>
              <input name="tier.${escapeHtml(tier.id)}.buffer_mins" type="number" min="0" max="240" step="1" value="${tier.buffer_mins}"${disabled} />
            </label>
          `).join("")}
        </fieldset>
      </form>
      <section class="payment-panel">
        <div class="section-heading"><p class="eyebrow">Payments</p><h2>Collection rules</h2></div>
        <div class="toggle-list">${rules.payments.enabled_methods.map((method) => `<span class="safe-chip">${escapeHtml(method.toUpperCase())}</span>`).join("")}</div>
        <dl>
          <div><dt>Cancellation window</dt><dd>${rules.payments.cancellation_window_hours} hours</dd></div>
          <div><dt>New client method</dt><dd>${rules.payments.require_payment_method_for_new_clients ? "Required" : "Optional"}</dd></div>
        </dl>
      </section>
      ${showSignOut ? `<section class="settings-actions" aria-label="Account actions"><button class="danger-action" data-sign-out type="button">Sign out</button></section>` : ""}
    </section>
  `;
}

function rangeLabel(min: number, max: number | null): string {
  if (max === null) return `Greater than ${min.toFixed(0)} miles`;
  if (min === 0) return `Less than ${max + 0.001} miles`;
  return `${min}-${max} miles`;
}
