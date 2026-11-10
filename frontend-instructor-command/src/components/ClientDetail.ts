import type { CommandCenterSnapshot } from "../api/types.js";
import { clientAvailabilityLabel, nextLessonForClient } from "../domain/screenModels.js";
import { escapeHtml } from "../lib/html.js";
import { formatShortDate, formatTimeRange, fullNameInitials, titleCase } from "../lib/format.js";

export function renderClientDetail(snapshot: CommandCenterSnapshot, clientId: string): string {
  const client = snapshot.clients.find((item) => item.id === clientId) ?? snapshot.clients[0];
  const nextLesson = nextLessonForClient(snapshot, client.id);

  return `
    <section class="screen-grid client-screen" aria-label="Client detail">
      <section class="client-profile">
        <span class="avatar">${escapeHtml(fullNameInitials(client.name))}</span>
        <div>
          <p class="eyebrow">${escapeHtml(client.history_summary)}</p>
          <h2>${escapeHtml(client.name)}</h2>
          <span>${escapeHtml(client.guardian_name ?? "Primary guardian")} | ${escapeHtml(client.phone)}</span>
        </div>
      </section>
      <section class="detail-grid">
        <article>
          <p class="eyebrow">Next lesson</p>
          <h3>${nextLesson ? `${formatShortDate(nextLesson.start_time)} ${formatTimeRange(nextLesson.start_time, nextLesson.end_time)}` : "No lesson scheduled"}</h3>
          <span>${nextLesson ? escapeHtml(nextLesson.location?.address ?? "Location unavailable") : "Use Find to recommend safe times."}</span>
        </article>
        <article>
          <p class="eyebrow">Progress focus</p>
          <h3>${escapeHtml(client.progress_focus)}</h3>
          <span>${escapeHtml(client.notes)}</span>
        </article>
        <article>
          <p class="eyebrow">Availability</p>
          <h3>${escapeHtml(clientAvailabilityLabel(client))}</h3>
          <span>${escapeHtml(titleCase(client.location_mode))} | ${client.lesson_duration_mins} min lessons</span>
        </article>
        <article>
          <p class="eyebrow">Location</p>
          <h3>${escapeHtml(client.default_location.address)}</h3>
          <span>${client.default_location.is_instructor_facility ? "Instructor facility" : "Client pool"}</span>
        </article>
      </section>
      <section class="history-strip">
        <button class="primary-action" type="button" data-screen="finder">Find new time</button>
        <button class="secondary-action" type="button">Text guardian</button>
      </section>
    </section>
  `;
}
