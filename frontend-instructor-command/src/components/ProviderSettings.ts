import type {
  ProviderConfiguration,
  ProviderConfigurationUpdate,
  ProviderWeekday,
  ProviderWorkingHour,
} from "../api/types.js";
import {
  PROVIDER_WEEKDAYS,
  providerSetupSections,
  weekdayLabel,
  type ProviderFieldErrors,
} from "../domain/providerSettings.js";
import { escapeHtml } from "../lib/html.js";

export type ProviderSettingsRenderState = {
  pending: boolean;
  error: string;
  fieldErrors: ProviderFieldErrors;
  success: string;
  setupPrompt: string;
};

const COMMON_TIMEZONES = [
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Phoenix",
  "America/Los_Angeles",
  "America/Anchorage",
  "Pacific/Honolulu",
  "UTC",
];

export function renderProviderSettings(
  provider: ProviderConfiguration,
  draft: ProviderConfigurationUpdate,
  status: ProviderSettingsRenderState,
): string {
  const disabled = status.pending ? " disabled" : "";
  const setupSections = providerSetupSections(provider.setup_missing_fields);
  const timezoneOptions = timezoneValues(draft.timezone);

  return `
    <section class="setup-status-card ${provider.setup_complete ? "complete" : "incomplete"}" data-setup-status>
      <span class="setup-status-mark" aria-hidden="true">${provider.setup_complete ? "Ready" : "Setup"}</span>
      <div>
        <p class="eyebrow">Provider setup</p>
        <h2>${provider.setup_complete ? "Scheduling is ready" : "Complete your scheduling setup"}</h2>
        <p>${provider.setup_complete
          ? "Your identity, locations, service, and weekly hours are saved."
          : "Finish the sections below before finding or confirming new lesson times."}</p>
        ${
          !provider.setup_complete && setupSections.length
            ? `<p class="setup-missing-copy"><strong>Still needed:</strong> ${setupSections.map(escapeHtml).join(", ")}</p>`
            : ""
        }
        ${status.setupPrompt ? `<p class="setup-route-message" role="status">${escapeHtml(status.setupPrompt)}</p>` : ""}
      </div>
    </section>
    <form class="provider-settings-form" data-provider-form novalidate aria-busy="${status.pending}">
      <header class="provider-form-heading">
        <div>
          <p class="eyebrow">Scheduling identity</p>
          <h2>Provider configuration</h2>
          <p>These details power safe starts, travel, and client-facing invitations.</p>
        </div>
      </header>
      ${status.error ? `<div class="error-banner provider-form-error" role="alert" tabindex="-1" data-provider-error>${escapeHtml(status.error)}</div>` : ""}
      <section class="provider-form-card" aria-labelledby="provider-profile-title">
        <div class="section-heading">
          <p class="eyebrow">Profile</p>
          <h3 id="provider-profile-title">Your profile</h3>
        </div>
        <div class="provider-field-grid">
          ${textField({
            field: "display_name",
            label: "Display name",
            value: draft.display_name,
            autocomplete: "name",
            required: true,
            maxLength: 160,
            error: status.fieldErrors.display_name,
            disabled,
          })}
          ${textField({
            field: "business_name",
            label: "Business name",
            value: draft.business_name ?? "",
            autocomplete: "organization",
            hint: "Optional",
            maxLength: 160,
            error: status.fieldErrors.business_name,
            disabled,
          })}
          <label class="provider-field provider-field-wide" for="provider-timezone">
            <span>Timezone</span>
            <select
              id="provider-timezone"
              name="timezone"
              data-provider-field="timezone"
              aria-invalid="${Boolean(status.fieldErrors.timezone)}"
              ${status.fieldErrors.timezone ? 'aria-describedby="provider-timezone-error"' : ""}${disabled}
            >
              ${draft.timezone ? "" : '<option value="" selected disabled>Choose a timezone</option>'}
              ${timezoneOptions.map((timezone) => `<option value="${escapeHtml(timezone)}"${timezone === draft.timezone ? " selected" : ""}>${escapeHtml(timezone.replaceAll("_", " "))}</option>`).join("")}
            </select>
            ${fieldError("timezone", status.fieldErrors.timezone)}
          </label>
        </div>
      </section>
      <section class="provider-form-card" aria-labelledby="provider-locations-title">
        <div class="section-heading">
          <p class="eyebrow">Route boundaries</p>
          <h3 id="provider-locations-title">Locations</h3>
          <p>Your base stays private. Clients only see the service location.</p>
        </div>
        <div class="provider-field-grid">
          ${textField({
            field: "base_address",
            label: "Base / start-of-shift address",
            value: draft.base_address,
            autocomplete: "street-address",
            required: true,
            maxLength: 300,
            error: status.fieldErrors.base_address,
            disabled,
            wide: true,
          })}
          ${textField({
            field: "service_address",
            label: "Client-facing service address",
            value: draft.service_address,
            autocomplete: "street-address",
            required: true,
            maxLength: 300,
            error: status.fieldErrors.service_address,
            disabled,
            wide: true,
          })}
        </div>
      </section>
      <section class="provider-form-card" aria-labelledby="provider-service-title">
        <div class="section-heading">
          <p class="eyebrow">Default booking</p>
          <h3 id="provider-service-title">Service</h3>
        </div>
        <div class="provider-field-grid">
          ${textField({
            field: "service_name",
            label: "Service name",
            value: draft.service_name,
            required: true,
            maxLength: 160,
            error: status.fieldErrors.service_name,
            disabled,
          })}
          <label class="provider-field" for="provider-duration">
            <span>Default duration</span>
            <select
              id="provider-duration"
              name="default_duration_mins"
              data-provider-field="default_duration_mins"
              aria-invalid="${Boolean(status.fieldErrors.default_duration_mins)}"
              ${status.fieldErrors.default_duration_mins ? 'aria-describedby="provider-default-duration-mins-error"' : ""}${disabled}
            >
              ${durationOptions(draft.default_duration_mins)}
            </select>
            ${fieldError("default_duration_mins", status.fieldErrors.default_duration_mins)}
          </label>
        </div>
      </section>
      <section class="provider-form-card working-hours-card" aria-labelledby="provider-hours-title">
        <div class="section-heading">
          <p class="eyebrow">Weekly availability</p>
          <h3 id="provider-hours-title">Working hours</h3>
          <p>Turn a day off to offer no new lesson times on that weekday.</p>
        </div>
        ${fieldError("working_hours", status.fieldErrors.working_hours)}
        <div class="weekday-list">
          ${PROVIDER_WEEKDAYS.map((weekday) => renderWeekday(
            draft.working_hours.find((hours) => hours.weekday === weekday)
              ?? { weekday, enabled: false, start_time: null, end_time: null },
            status.fieldErrors,
            disabled,
          )).join("")}
        </div>
      </section>
      <div class="provider-save-bar">
        <p class="provider-save-status" role="status" aria-live="polite" data-provider-save-status>
          ${escapeHtml(status.pending ? "Saving provider settings." : status.success)}
        </p>
        <button class="primary-action" type="submit"${disabled}>${status.pending ? "Saving…" : "Save provider settings"}</button>
      </div>
    </form>
  `;
}

function renderWeekday(
  hours: ProviderWorkingHour,
  errors: ProviderFieldErrors,
  disabled: string,
): string {
  const weekday = hours.weekday;
  const label = weekdayLabel(weekday);
  const startField = `working_hours.${weekday}.start_time`;
  const endField = `working_hours.${weekday}.end_time`;
  const timesDisabled = disabled || !hours.enabled ? " disabled" : "";
  return `
    <fieldset class="weekday-row" data-weekday="${weekday}">
      <legend class="sr-only">${label} working hours</legend>
      <label class="weekday-switch" for="provider-${weekday}-enabled">
        <span class="weekday-name">${label}</span>
        <span class="switch-control">
          <input
            id="provider-${weekday}-enabled"
            name="working_hours.${weekday}.enabled"
            type="checkbox"
            role="switch"
            data-provider-field="working_hours.${weekday}.enabled"
            data-weekday-toggle="${weekday}"
            ${hours.enabled ? "checked" : ""}${disabled}
          />
          <span class="switch-track" aria-hidden="true"><span></span></span>
        </span>
      </label>
      <div class="weekday-times">
        <label for="provider-${weekday}-start">
          <span>Opens</span>
          <input
            id="provider-${weekday}-start"
            name="${startField}"
            type="time"
            step="60"
            value="${escapeHtml(hours.start_time ?? "08:00")}"
            data-provider-field="${startField}"
            aria-invalid="${Boolean(errors[startField])}"
            ${errors[startField] ? `aria-describedby="${fieldErrorId(startField)}"` : ""}${timesDisabled}
          />
          ${fieldError(startField, errors[startField])}
        </label>
        <label for="provider-${weekday}-end">
          <span>Closes</span>
          <input
            id="provider-${weekday}-end"
            name="${endField}"
            type="time"
            step="60"
            value="${escapeHtml(hours.end_time ?? "17:00")}"
            data-provider-field="${endField}"
            aria-invalid="${Boolean(errors[endField])}"
            ${errors[endField] ? `aria-describedby="${fieldErrorId(endField)}"` : ""}${timesDisabled}
          />
          ${fieldError(endField, errors[endField])}
        </label>
      </div>
    </fieldset>
  `;
}

function textField(options: {
  field: string;
  label: string;
  value: string;
  autocomplete?: string;
  hint?: string;
  required?: boolean;
  maxLength: number;
  error?: string;
  disabled: string;
  wide?: boolean;
}): string {
  const id = `provider-${fieldToken(options.field)}`;
  const errorId = fieldErrorId(options.field);
  return `
    <label class="provider-field${options.wide ? " provider-field-wide" : ""}" for="${id}">
      <span>${escapeHtml(options.label)}${options.hint ? ` <small>${escapeHtml(options.hint)}</small>` : ""}</span>
      <input
        id="${id}"
        name="${escapeHtml(options.field)}"
        type="text"
        value="${escapeHtml(options.value)}"
        maxlength="${options.maxLength}"
        ${options.autocomplete ? `autocomplete="${escapeHtml(options.autocomplete)}"` : ""}
        ${options.required ? "required" : ""}
        data-provider-field="${escapeHtml(options.field)}"
        aria-invalid="${Boolean(options.error)}"
        ${options.error ? `aria-describedby="${errorId}"` : ""}${options.disabled}
      />
      ${fieldError(options.field, options.error)}
    </label>
  `;
}

function fieldError(field: string, message?: string): string {
  return message
    ? `<p class="provider-field-error" id="${fieldErrorId(field)}" role="alert">${escapeHtml(message)}</p>`
    : "";
}

function fieldErrorId(field: string): string {
  return `provider-${fieldToken(field)}-error`;
}

function fieldToken(field: string): string {
  return field.replaceAll(".", "-").replaceAll("_", "-");
}

function timezoneValues(current: string): string[] {
  const supportedValuesOf = (Intl as typeof Intl & {
    supportedValuesOf?: (key: "timeZone") => string[];
  }).supportedValuesOf;
  const supported = supportedValuesOf
    ? supportedValuesOf("timeZone")
    : [...COMMON_TIMEZONES];
  const values = new Set([...supported, ...COMMON_TIMEZONES]);
  if (current) values.add(current);
  return [...values].sort((left, right) => left.localeCompare(right));
}

function durationOptions(current: number): string {
  const values = Array.from({ length: 16 }, (_, index) => (index + 1) * 15);
  if (Number.isFinite(current) && !values.includes(current)) values.push(current);
  return values
    .sort((left, right) => left - right)
    .map((value) => `<option value="${value}"${value === current ? " selected" : ""}>${value} minutes</option>`)
    .join("");
}

export function providerDraftWithField(
  draft: ProviderConfigurationUpdate,
  field: string,
  value: string | boolean,
): ProviderConfigurationUpdate {
  if (!field.startsWith("working_hours.")) {
    if (field === "default_duration_mins") return { ...draft, default_duration_mins: Number(value) };
    if (field === "business_name") return { ...draft, business_name: String(value) };
    return { ...draft, [field]: String(value) };
  }

  const [, rawWeekday, property] = field.split(".");
  const weekday = rawWeekday as ProviderWeekday;
  return {
    ...draft,
    working_hours: draft.working_hours.map((hours) => {
      if (hours.weekday !== weekday) return hours;
      if (property === "enabled") {
        const enabled = Boolean(value);
        return {
          ...hours,
          enabled,
          start_time: enabled ? hours.start_time ?? "08:00" : null,
          end_time: enabled ? hours.end_time ?? "17:00" : null,
        };
      }
      return { ...hours, [property]: String(value) };
    }),
  };
}
