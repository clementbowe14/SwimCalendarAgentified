import type { GoogleCalendarConnection } from "../api/types.js";
import { escapeHtml } from "../lib/html.js";

export type GoogleCalendarAction = "status" | "authorize" | "disconnect";
export type GoogleCalendarReturn = "connected" | "failed" | "back";

export type GoogleCalendarSettingsState = {
  connection: GoogleCalendarConnection["state"] | null;
  pending: GoogleCalendarAction | null;
  error: string;
  returnResult: GoogleCalendarReturn | null;
  ownerChanged: boolean;
};

const connectionLabels = {
  disconnected: "Disconnected",
  connected: "Connected",
  authorization_required: "Reconnect required",
};

const pendingLabels = {
  status: "Checking Google Calendar connection…",
  authorize: "Opening Google Calendar…",
  disconnect: "Disconnecting Google Calendar…",
};

export function renderGoogleCalendarSettings(state: GoogleCalendarSettingsState): string {
  return `
    <section class="provider-form-card google-calendar-settings" aria-labelledby="google-calendar-title"
      data-google-calendar-settings tabindex="-1">
      <div class="section-heading">
        <h2 id="google-calendar-title">Google Calendar</h2>
        <p>Use Google Calendar busy times to protect your availability.</p>
      </div>
      <div class="google-calendar-content" data-google-calendar-content>
        ${renderGoogleCalendarSettingsContent(state)}
      </div>
    </section>
  `;
}

export function renderGoogleCalendarSettingsContent(state: GoogleCalendarSettingsState): string {
  if (state.ownerChanged) {
    return `
      <p class="error-banner" role="alert">Your signed-in provider changed. Reload Dayweave and sign in if prompted.</p>
      <div class="google-calendar-actions">
        <button class="primary-action" data-google-calendar-action="reload" type="button">Reload Dayweave</button>
      </div>
    `;
  }

  const { connection, pending, returnResult } = state;
  const status = pending ? pendingLabels[pending] : connection ? connectionLabels[connection] : "";
  const error = state.error || (!pending && connection ? returnError(returnResult, connection) : "");
  const canAuthorize = connection === "disconnected" || connection === "authorization_required"
    || (connection === "connected" && (returnResult === "failed" || returnResult === "back"));
  const canDisconnect = connection === "connected" || connection === "authorization_required";
  const disabled = pending ? " disabled" : "";

  return `
    ${status ? `<p role="status"><strong>${status}</strong></p>` : ""}
    ${error ? `<p class="error-banner" role="alert">${escapeHtml(error)}</p>` : ""}
    <div class="google-calendar-actions">
      ${!connection && error ? `
        <button class="secondary-action" data-google-calendar-action="status" type="button">Retry status</button>
      ` : ""}
      ${canAuthorize ? `
        <button class="primary-action" data-google-calendar-action="authorize" type="button"${disabled}>
          ${connection === "disconnected" ? "Connect" : "Reconnect"} Google Calendar
        </button>
      ` : ""}
      ${canDisconnect ? `
        <button class="secondary-action" data-google-calendar-action="disconnect" type="button"${disabled}>
          Disconnect Google Calendar
        </button>
      ` : ""}
    </div>
  `;
}

function returnError(
  result: GoogleCalendarReturn | null,
  connection: GoogleCalendarConnection["state"],
): string {
  if (result === "failed") return "Google Calendar authorization didn’t finish. Try again.";
  if (result === "connected" && connection !== "connected") {
    return "We couldn’t confirm the Google Calendar connection. Try connecting again.";
  }
  return "";
}
