import type { SchedulerRules } from "../api/types.js";
import { escapeHtml } from "../lib/html.js";

export function renderSettingsRules(rules: SchedulerRules): string {
  return `
    <section class="screen-grid settings-screen" aria-label="Settings and rules">
      <section class="rules-overview">
        <div>
          <p class="eyebrow">Constraint engine</p>
          <h2>${rules.rounding_increment_mins}-minute ceiling rounding</h2>
          <span>Post-break buffer stays fixed at ${rules.post_break_buffer_mins} minutes.</span>
        </div>
        <button class="primary-action" type="button" data-save-rules>Save rules</button>
      </section>
      <section class="rules-list">
        ${rules.distance_buffer_tiers
          .map(
            (tier) => `
              <article class="rule-row">
                <div>
                  <strong>${escapeHtml(tier.label)}</strong>
                  <span>${rangeLabel(tier.min_miles, tier.max_miles)}</span>
                </div>
                <label>
                  <span>Buffer</span>
                  <input value="${tier.buffer_mins}" inputmode="numeric" aria-label="${escapeHtml(tier.label)} buffer minutes" />
                </label>
              </article>
            `,
          )
          .join("")}
      </section>
      <section class="payment-panel">
        <div class="section-heading">
          <p class="eyebrow">Payments</p>
          <h2>Collection rules</h2>
        </div>
        <div class="toggle-list">
          ${rules.payments.enabled_methods
            .map((method) => `<span class="safe-chip">${escapeHtml(method.toUpperCase())}</span>`)
            .join("")}
        </div>
        <dl>
          <div><dt>Cancellation window</dt><dd>${rules.payments.cancellation_window_hours} hours</dd></div>
          <div><dt>New client method</dt><dd>${rules.payments.require_payment_method_for_new_clients ? "Required" : "Optional"}</dd></div>
        </dl>
      </section>
    </section>
  `;
}

function rangeLabel(min: number, max: number | null): string {
  if (max === null) return `Greater than ${min.toFixed(0)} miles`;
  if (min === 0) return `Less than ${max + 0.001} miles`;
  return `${min}-${max} miles`;
}
