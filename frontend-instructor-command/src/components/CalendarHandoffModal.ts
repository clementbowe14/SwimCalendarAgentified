import type { CalendarLinks } from "../api/types.js";
import { trustedGoogleCalendarUrl, trustedInstructorCalendarUrl } from "../api/calendar.js";
import { escapeHtml } from "../lib/html.js";

export type CalendarHandoffModalModel = {
  status: "ready" | "unavailable";
  appointmentId: string | null;
  links: CalendarLinks | null;
  returnFocusKey: string;
  returnFocusFallback: "finder" | "requests";
  downloadStatus: "idle" | "downloading" | "downloaded";
  inlineError: string;
};

type SafeGoogleEvent = {
  startIso?: string;
  endIso?: string;
  url: string;
};

export function renderCalendarHandoffModal(
  model: CalendarHandoffModalModel,
  apiBase: string,
  documentUrl: string,
): string {
  const artifacts = prepareArtifacts(model.links, apiBase, documentUrl);
  const isReady = model.status === "ready" && Boolean(model.links);
  const warnings = [artifacts.warning, model.inlineError].filter(Boolean);

  return `
    <div class="calendar-modal-backdrop" data-calendar-backdrop>
      <section
        class="calendar-handoff-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="calendar-handoff-title"
        aria-describedby="calendar-handoff-description calendar-handoff-note"
        tabindex="-1"
        data-calendar-dialog
      >
        <button class="calendar-modal-close" type="button" aria-label="Close calendar options" data-calendar-close>
          <span aria-hidden="true">×</span>
        </button>
        <div class="calendar-modal-success" aria-hidden="true">✓</div>
        <p class="eyebrow">Appointment confirmed</p>
        <h2 id="calendar-handoff-title">${isReady ? "Ready for your calendar" : "Calendar options unavailable"}</h2>
        <p id="calendar-handoff-description" class="calendar-modal-description">
          ${
            isReady
              ? "The appointment is saved. Choose a calendar handoff below."
              : "The appointment is saved, but calendar options are not available right now."
          }
        </p>
        ${isReady ? renderGoogleActions(artifacts.googleEvents) : ""}
        ${
          isReady && artifacts.icsUrl
            ? `<button class="calendar-action calendar-action-ics" type="button" data-calendar-ics="${escapeHtml(artifacts.icsUrl)}" ${
                model.downloadStatus === "downloading" ? "disabled aria-busy=\"true\"" : ""
              }>${model.downloadStatus === "downloading" ? "Downloading…" : "Download .ics"}</button>`
            : ""
        }
        ${
          model.downloadStatus === "downloaded"
            ? '<p class="calendar-download-status" role="status">Calendar file downloaded. Open it to finish adding the appointment.</p>'
            : ""
        }
        ${warnings.map((warning) => `<p class="calendar-modal-alert" role="alert">${escapeHtml(warning)}</p>`).join("")}
        <p id="calendar-handoff-note" class="calendar-handoff-note">
          Opening or downloading is a snapshot handoff. It does not add the appointment automatically.
        </p>
        <button class="primary-action calendar-done-action" type="button" data-calendar-close>Done</button>
      </section>
    </div>
  `;
}

function prepareArtifacts(links: CalendarLinks | null, apiBase: string, documentUrl: string): {
  googleEvents: SafeGoogleEvent[];
  icsUrl: string | null;
  warning: string;
} {
  if (!links) return { googleEvents: [], icsUrl: null, warning: "" };

  const googleEvents: SafeGoogleEvent[] = [];
  let rejectedGoogle = false;
  const candidates = links.google_events.length
    ? links.google_events
    : [{ start_iso: undefined, end_iso: undefined, url: links.google }];

  for (const event of candidates) {
    try {
      googleEvents.push({ startIso: event.start_iso, endIso: event.end_iso, url: trustedGoogleCalendarUrl(event.url) });
    } catch {
      rejectedGoogle = true;
    }
  }

  let icsUrl: string | null = null;
  let rejectedIcs = false;
  try {
    icsUrl = trustedInstructorCalendarUrl(links.ics, apiBase, documentUrl);
  } catch {
    rejectedIcs = true;
  }

  const warning = rejectedGoogle || rejectedIcs
    ? "One or more calendar options could not be verified and were disabled."
    : "";
  return { googleEvents, icsUrl, warning };
}

function renderGoogleActions(events: SafeGoogleEvent[]): string {
  if (!events.length) return "";
  if (events.length === 1) {
    return `
      <a
        class="calendar-action calendar-action-google"
        href="${escapeHtml(events[0].url)}"
        target="_blank"
        rel="noopener noreferrer"
        referrerpolicy="no-referrer"
        aria-label="Open Google Calendar (opens in a new tab)"
      >Open Google Calendar</a>
    `;
  }

  return `
    <div class="calendar-occurrences">
      <h3>Google Calendar occurrences</h3>
      <ul aria-label="Google Calendar occurrences">
        ${events
          .map(
            (event) => `
              <li>
                <a
                  class="calendar-action calendar-action-google"
                  href="${escapeHtml(event.url)}"
                  target="_blank"
                  rel="noopener noreferrer"
                  referrerpolicy="no-referrer"
                  aria-label="Open ${escapeHtml(formatOccurrence(event.startIso, event.endIso))} in Google Calendar (opens in a new tab)"
                >${escapeHtml(formatOccurrence(event.startIso, event.endIso))}</a>
              </li>
            `,
          )
          .join("")}
      </ul>
    </div>
  `;
}

function formatOccurrence(startIso?: string, endIso?: string): string {
  if (!startIso || !endIso) return "Appointment occurrence";
  const start = new Date(startIso);
  const end = new Date(endIso);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return "Appointment occurrence";
  const date = new Intl.DateTimeFormat("en-US", { weekday: "short", month: "short", day: "numeric" }).format(start);
  const time = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit" });
  return `${date}, ${time.format(start)}–${time.format(end)}`;
}
