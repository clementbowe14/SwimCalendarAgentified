import type { AvailabilityDateRange, AvailabilityRangeResult, CommandCenterSnapshot, FinderClient, NewClientDraft, TimeSlotDTO } from "../api/types.js";
import { clientAvailabilityLabel, slotExplanation, slotSafetyFacts } from "../domain/screenModels.js";
import type { AvailabilityUiState } from "../domain/schedulingPolicy.js";
import { escapeHtml } from "../lib/html.js";
import { formatShortDate, formatTime } from "../lib/format.js";

export function defaultNewClientDraft(snapshot: CommandCenterSnapshot): NewClientDraft {
  return {
    client_name: "Sofia Martinez",
    phone: "(512) 555-0194",
    email: "sofia.guardian@example.test",
    location_mode: "client_home",
    location: snapshot.clients.find((client) => client.id === "client_sofia")?.default_location ?? {
      address: snapshot.instructor.main_address,
      lat: snapshot.instructor.lat,
      lng: snapshot.instructor.lng,
      is_instructor_facility: true,
    },
    availability: [
      { id: "draft_thu", days: ["Thu"], start_time: "18:00", end_time: "20:00" },
      { id: "draft_sat", days: ["Sat"], start_time: "10:00", end_time: "12:00" },
    ],
    duration_mins: 45,
  };
}

export function renderAvailabilityFinder(
  snapshot: CommandCenterSnapshot,
  model: {
    clients: FinderClient[] | null;
    selectedClient: FinderClient | undefined;
    range: AvailabilityDateRange;
    bounds: AvailabilityDateRange;
    rangeError: string;
    availability: AvailabilityUiState;
    result: AvailabilityRangeResult | null;
  },
): string {
  const { clients, selectedClient, range, bounds, rangeError, availability, result } = model;
  const hasLocation = Boolean(selectedClient?.default_location);
  const refreshing = availability.status === "refreshing";
  const disabled = !snapshot.instructor.setup_complete || refreshing || (clients !== null && !hasLocation);
  const dateConstraints = `min="${bounds.start_date}" max="${bounds.end_date}" required aria-describedby="finder-date-help finder-date-error" aria-invalid="${Boolean(rangeError)}"`;

  return `
    <section class="screen-grid finder-screen" aria-label="Availability finder and new client intake">
      <form class="finder-form" data-finder-form novalidate>
        <div class="section-heading row">
          <div>
            <p class="eyebrow">Availability Finder</p>
            <h2>${escapeHtml(selectedClient?.name ?? "Find a lesson time")}</h2>
          </div>
          ${selectedClient ? `<span class="metric-pill">${selectedClient.lesson_duration_mins} min</span>` : ""}
        </div>
        <div class="field-grid">
          <label>
            <span>Client</span>
            <select data-client-picker${clients?.length ? "" : " disabled"}>
              ${clients?.length ? clients.map(
                  (client) => `
                    <option value="${escapeHtml(client.id)}" ${client.id === selectedClient?.id ? "selected" : ""}>
                      ${escapeHtml(client.name)}
                    </option>
                  `,
                ).join("") : `<option>${clients ? "No saved clients" : "Saved clients"}</option>`}
            </select>
          </label>
          <label>
            <span>${selectedClient?.location_mode === "instructor_facility" ? "Facility location" : "Client location"}</span>
            <input value="${escapeHtml(selectedClient?.default_location?.address ?? "")}" readonly />
          </label>
          <label class="finder-windows">
            <span>Availability windows</span>
            <input value="${escapeHtml(selectedClient ? clientAvailabilityLabel(selectedClient) || "No saved windows" : "")}" readonly />
          </label>
          <label>
            <span>Start date</span>
            <input type="date" name="start_date" value="${escapeHtml(range.start_date)}" ${dateConstraints} />
          </label>
          <label>
            <span>End date</span>
            <input type="date" name="end_date" value="${escapeHtml(range.end_date)}" ${dateConstraints} />
          </label>
        </div>
        <p id="finder-date-help">Search includes both dates within the next 14 days in your scheduling time zone.</p>
        <p id="finder-date-error" class="provider-field-error" role="alert"${rangeError ? "" : " hidden"}>${escapeHtml(rangeError)}</p>
        ${selectedClient && !hasLocation ? `<p>Save a lesson location for this client before searching.</p>` : ""}
        <button class="primary-action" type="submit" data-refresh-slots${disabled ? " disabled" : ""}>
          ${refreshing ? "Checking safe starts…" : "Find safe starts"}
        </button>
      </form>
      <section class="new-client-panel">
        <p class="eyebrow">New client intake</p>
        <h2>Convert invite data into a slot search</h2>
        <div class="intake-summary">
          ${renderDraft(defaultNewClientDraft(snapshot))}
        </div>
      </section>
      <section class="slot-results" data-finder-results>
        ${renderAvailabilityResults(rangeError ? null : result, availability)}
      </section>
    </section>
  `;
}

export function renderAvailabilityResults(
  result: AvailabilityRangeResult | null,
  availability: AvailabilityUiState,
): string {
  const resultsHeading = result ? `${result.available_slots.length} safe options`
    : availability.status === "refreshing" ? "Rechecking safe starts"
      : availability.status === "stale" ? "Safe starts need a retry" : "Find safe starts";
  const startDate = result ? formatShortDate(result.start_date) : "";
  const searchedRange = result && result.start_date !== result.end_date
    ? `${startDate} to ${formatShortDate(result.end_date)}` : startDate;
  return `
    <div class="section-heading row">
      <div>
        <p class="eyebrow">Recommended starts</p>
        <h2>${resultsHeading}</h2>
      </div>
      <span class="safe-chip">15 min rounding</span>
    </div>
    ${result ? `<p class="finder-result-context">Searched ${escapeHtml(searchedRange)} · ${escapeHtml(result.timezone)} · ${escapeHtml(result.location.address)}</p>` : ""}
    ${renderSlotResults(result, availability)}
  `;
}

function renderSlotResults(
  result: AvailabilityRangeResult | null,
  availability: AvailabilityUiState,
): string {
  if (availability.status === "refreshing") {
    return `
      <div class="availability-state refreshing" role="status" aria-live="polite">
        <span class="availability-spinner" aria-hidden="true"></span>
        <strong>${escapeHtml(availability.message || "Rechecking safe starts…")}</strong>
      </div>
    `;
  }
  if (availability.status === "stale") {
    return `
      <div class="availability-state stale" role="alert">
        <strong>Do not book the old options</strong>
        <span>${escapeHtml(availability.message)}</span>
        <button class="secondary-action" type="button" data-retry-availability>Retry safe starts</button>
      </div>
    `;
  }
  if (!result) {
    return `
      <div class="availability-state idle" role="status">
        <strong>No search yet</strong>
        <span>Choose a saved client and date range to find safe starts.</span>
      </div>
    `;
  }
  if (!result.available_slots.length) {
    const reason = result.empty_reason === "no_working_hours" ? "No provider working hours fall in this range."
      : result.empty_reason === "outside_client_windows" ? "The client’s saved windows fall outside the provider’s working hours in this range."
        : "The schedule has no safe starts for this client in the selected range.";
    return `
      <div class="availability-state idle" role="status">
        <strong>No safe starts found</strong>
        <span>${reason}</span>
      </div>
    `;
  }
  return result.available_slots.map((slot) => renderSlot(slot, result.timezone)).join("");
}

function renderDraft(draft: NewClientDraft): string {
  return `
    <dl>
      <div><dt>Name</dt><dd>${escapeHtml(draft.client_name)}</dd></div>
      <div><dt>Phone</dt><dd>${escapeHtml(draft.phone)}</dd></div>
      <div><dt>Mode</dt><dd>${escapeHtml(draft.location_mode.replaceAll("_", " "))}</dd></div>
      <div><dt>Duration</dt><dd>${draft.duration_mins} min</dd></div>
    </dl>
  `;
}

function renderSlot(slot: TimeSlotDTO, timezone: string): string {
  const time = `${formatTime(slot.start_time, timezone, true)}–${formatTime(slot.end_time, timezone, true)}`;
  const distance = slot.target_location.is_instructor_facility ? ` | ${slot.distance_miles.toFixed(1)} mi` : "";
  return `
    <article class="slot-card">
      <div>
        <strong>${escapeHtml(formatShortDate(slot.start_time, timezone))} ${escapeHtml(time)}</strong>
        <span>${escapeHtml(slotSafetyFacts(slot) + distance)}</span>
        <p>${escapeHtml(slotExplanation(slot))}</p>
      </div>
      <button class="secondary-action" type="button" data-slot-id="${escapeHtml(slot.id)}" data-calendar-focus="slot:${escapeHtml(slot.id)}">Book lesson</button>
    </article>
  `;
}
