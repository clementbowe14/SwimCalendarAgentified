import type { Client, CommandCenterSnapshot, NewClientDraft, TimeSlotDTO } from "../api/types.js";
import { buildFindSlotsRequest, clientAvailabilityLabel, slotExplanation, slotSafetyFacts } from "../domain/screenModels.js";
import { escapeHtml } from "../lib/html.js";
import { formatShortDate, formatTimeRange } from "../lib/format.js";

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
  selectedClient: Client,
  recommendedSlots: TimeSlotDTO[],
): string {
  const request = buildFindSlotsRequest(snapshot.instructor.id, selectedClient, snapshot.date);

  return `
    <section class="screen-grid finder-screen" aria-label="Availability finder and new client intake">
      <section class="finder-form">
        <div class="section-heading row">
          <div>
            <p class="eyebrow">Availability Finder</p>
            <h2>${escapeHtml(selectedClient.name)}</h2>
          </div>
          <span class="metric-pill">${request.duration_mins} min</span>
        </div>
        <div class="field-grid">
          <label>
            <span>Client</span>
            <select data-client-picker>
              ${snapshot.clients
                .map(
                  (client) => `
                    <option value="${escapeHtml(client.id)}" ${client.id === selectedClient.id ? "selected" : ""}>
                      ${escapeHtml(client.name)}
                    </option>
                  `,
                )
                .join("")}
            </select>
          </label>
          <label>
            <span>Lesson date</span>
            <input value="${escapeHtml(request.requested_date)}" readonly />
          </label>
          <label>
            <span>Location</span>
            <input value="${escapeHtml(request.location.address)}" readonly />
          </label>
          <label>
            <span>Availability windows</span>
            <input value="${escapeHtml(clientAvailabilityLabel(selectedClient))}" readonly />
          </label>
        </div>
        <div class="segmented-control" aria-label="Lesson location mode">
          <button class="${selectedClient.location_mode === "client_home" ? "selected" : ""}" type="button">Home pool</button>
          <button class="${selectedClient.location_mode === "instructor_facility" ? "selected" : ""}" type="button">Facility</button>
        </div>
        <button class="primary-action" type="button" data-refresh-slots>Find safe starts</button>
      </section>
      <section class="new-client-panel">
        <p class="eyebrow">New client intake</p>
        <h2>Convert invite data into a slot search</h2>
        <div class="intake-summary">
          ${renderDraft(defaultNewClientDraft(snapshot))}
        </div>
      </section>
      <section class="slot-results">
        <div class="section-heading row">
          <div>
            <p class="eyebrow">Recommended starts</p>
            <h2>${recommendedSlots.length} safe options</h2>
          </div>
          <span class="safe-chip">15 min rounding</span>
        </div>
        ${recommendedSlots.map((slot) => renderSlot(slot, snapshot)).join("")}
      </section>
    </section>
  `;
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

function renderSlot(slot: TimeSlotDTO, snapshot: CommandCenterSnapshot): string {
  return `
    <article class="slot-card">
      <div>
        <strong>${escapeHtml(formatShortDate(slot.start_time))} ${escapeHtml(formatTimeRange(slot.start_time, slot.end_time))}</strong>
        <span>${escapeHtml(slotSafetyFacts(slot))}</span>
        <p>${escapeHtml(slotExplanation(slot, snapshot.rules))}</p>
      </div>
      <button class="secondary-action" type="button" data-slot-id="${escapeHtml(slot.id)}">Hold</button>
    </article>
  `;
}
