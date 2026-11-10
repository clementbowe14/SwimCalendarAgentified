import type { CommandCenterSnapshot } from "../api/types.js";
import { buildAgendaRows, scheduleUtilization } from "../domain/screenModels.js";
import { formatTimeRange } from "../lib/format.js";
import { escapeHtml } from "../lib/html.js";

export function renderTodaySchedule(snapshot: CommandCenterSnapshot): string {
  const rows = buildAgendaRows(snapshot.schedule);
  const lessonCount = snapshot.schedule.filter((item) => item.kind === "lesson").length;
  const nextLesson = snapshot.schedule.find((item) => item.kind === "lesson");
  const utilization = scheduleUtilization(snapshot.schedule);

  return `
    <section class="screen-grid today-screen" aria-label="Today schedule">
      <section class="hero-panel schedule-hero">
        <div>
          <p class="eyebrow">Next lesson</p>
          <h2>${escapeHtml(nextLesson ? `${nextLesson.title} at ${formatTimeRange(nextLesson.start_time, nextLesson.end_time).split("-")[0]}` : "No lessons scheduled")}</h2>
          <span>${escapeHtml(nextLesson ? `${nextLesson.location?.address ?? "Location unavailable"} | ${nextLesson.note ?? "Schedule loaded from backend"}` : "Use the finder to create the next safe start.")}</span>
        </div>
        <div class="schedule-hero-actions">
          <button class="secondary-action" type="button" data-screen="requests">Review requests</button>
          <button class="primary-action" type="button" data-add-blocker>Add schedule event</button>
        </div>
      </section>
      <section class="stats-grid" aria-label="Schedule summary">
        <article><strong>${lessonCount}</strong><span>lessons</span></article>
        <article><strong>${utilization}%</strong><span>teaching load</span></article>
        <article><strong>${snapshot.schedule.length}</strong><span>schedule items</span></article>
      </section>
      <section class="timeline-list">
        ${rows
          .map(
            (row) => `
              <article class="agenda-row ${row.tone}" data-agenda-row="${escapeHtml(row.id)}">
                <time>${escapeHtml(row.time)}</time>
                <div>
                  <strong>${escapeHtml(row.title)}</strong>
                  <span>${escapeHtml(row.detail)}</span>
                  ${row.status ? `<small>${escapeHtml(row.status)}</small>` : ""}
                </div>
                ${["break", "personal", "unavailable"].includes(row.kind)
                  ? `<button class="agenda-edit-button" type="button" data-edit-blocker="${escapeHtml(row.id)}" aria-label="Edit ${escapeHtml(row.title)}">Edit</button>`
                  : ""}
              </article>
            `,
          )
          .join("")}
      </section>
    </section>
  `;
}
