import type { CommandCenterSnapshot, ScheduleItem } from "../api/types.js";
import { buildAgendaRows, requestDateLabel } from "../domain/screenModels.js";
import { formatTime, formatTimeRange } from "../lib/format.js";
import { escapeHtml } from "../lib/html.js";

type SchedulePhase = "past" | "today" | "future";

export function renderTodaySchedule(snapshot: CommandCenterSnapshot): string {
  const now = new Date();
  const phase = schedulePhase(snapshot.date, now, snapshot.instructor.timezone);
  const rows = buildAgendaRows(snapshot.schedule, snapshot.instructor.timezone);
  const featuredLessonIndex = findFeaturedLessonIndex(snapshot.schedule, phase, now);
  const featuredLesson = featuredLessonIndex >= 0 ? snapshot.schedule[featuredLessonIndex] : undefined;
  const travel = findPreviousItem(snapshot.schedule, featuredLessonIndex, "travel");
  const buffer = findPreviousItem(snapshot.schedule, featuredLessonIndex, "buffer");
  const pendingRequest = snapshot.requests.find((request) => request.status === "pending");
  const date = parseLocalDate(snapshot.date);
  const weekday = new Intl.DateTimeFormat("en-US", { weekday: "long" }).format(date);
  const monthDay = new Intl.DateTimeFormat("en-US", { month: "long", day: "numeric" }).format(date);
  const activeLessonCount = snapshot.schedule.filter(isActiveLesson).length;

  return `
    <section class="screen-grid today-screen schedule-dashboard" aria-label="Today schedule">
      <header class="schedule-heading">
        <div>
          <p class="schedule-kicker">${phase === "today" ? "Today" : "Schedule"}</p>
          <h2>${escapeHtml(weekday)}, ${escapeHtml(monthDay)}</h2>
          <p>${rows.length === 0 ? "A clear day, ready when you are." : `${activeLessonCount} appointments · travel and recovery time included`}</p>
        </div>
        <button class="schedule-add-button" type="button" data-add-blocker aria-label="Add schedule event">
          <i class="ph ph-plus" aria-hidden="true"></i>
          <span>Add event</span>
        </button>
      </header>

      ${renderWeekStrip(date)}

      ${featuredLesson && featuredLesson.kind === "lesson"
        ? renderDepartureCard(featuredLesson, travel, buffer, snapshot.instructor.timezone, phase, now)
        : renderOpenDayCard(snapshot.instructor.setup_complete, phase, rows.length > 0)}

      <section class="route-section" aria-labelledby="day-route-title">
        <div class="route-heading">
          <div>
            <p class="eyebrow">Day route</p>
            <h3 id="day-route-title">Your schedule</h3>
          </div>
          <span>${rows.length} blocks</span>
        </div>
        ${rows.length > 0 ? `
          <ol class="route-timeline">
            ${rows
              .map((row, index) => {
                const item = snapshot.schedule[index];
                const location = item.location?.address;
                const supportingDetail = row.kind === "lesson" && item.note ? item.note : row.detail;
                return `
                  <li class="route-row ${row.tone}" data-agenda-row="${escapeHtml(row.id)}">
                    <time>${escapeHtml(row.time)}</time>
                    <span class="route-node" aria-hidden="true"><i class="ph ${iconForKind(row.kind)}"></i></span>
                    <article class="route-content">
                      <div class="route-content-copy">
                        <span class="route-label">${escapeHtml(labelForKind(row.kind))}</span>
                        <strong>${escapeHtml(row.title)}</strong>
                        <span>${escapeHtml(location ?? row.detail)}</span>
                        ${location && supportingDetail ? `<small>${escapeHtml(supportingDetail)}</small>` : ""}
                        ${row.status && row.status !== "scheduled" ? `<small class="route-status">${escapeHtml(row.status.replaceAll("_", " "))}</small>` : ""}
                      </div>
                      ${["break", "personal", "unavailable"].includes(row.kind)
                        ? `<button class="agenda-edit-button icon-button" type="button" data-edit-blocker="${escapeHtml(row.id)}" aria-label="Edit ${escapeHtml(row.title)}"><i class="ph ph-pencil-simple" aria-hidden="true"></i></button>`
                        : ""}
                    </article>
                  </li>
                `;
              })
              .join("")}
          </ol>
        ` : renderEmptyTimeline(snapshot.instructor.setup_complete)}
      </section>

      ${pendingRequest ? `
        <section class="request-cue" aria-label="Pending request from ${escapeHtml(pendingRequest.client_name)}">
          <span class="request-cue-icon" aria-hidden="true"><i class="ph ph-tray"></i></span>
          <div>
            <p class="eyebrow">Needs your review</p>
            <h3>${escapeHtml(pendingRequest.client_name)} requested a time</h3>
            <span>${escapeHtml(requestDateLabel(pendingRequest, snapshot.instructor.timezone))}</span>
          </div>
          <button type="button" data-screen="requests">Review <i class="ph ph-arrow-right" aria-hidden="true"></i></button>
        </section>
      ` : ""}
    </section>
  `;
}

function renderWeekStrip(selectedDate: Date): string {
  const days = Array.from({ length: 7 }, (_, index) => {
    const date = new Date(selectedDate);
    date.setDate(selectedDate.getDate() + index - 3);
    return date;
  });

  return `
    <ol class="week-strip" aria-label="Seven day calendar">
      ${days
        .map((date, index) => {
          const dateKey = toDateKey(date);
          const dateLabel = new Intl.DateTimeFormat("en-US", {
            weekday: "long",
            month: "long",
            day: "numeric",
            year: "numeric",
          }).format(date);
          return `
            <li>
              <button
                class="${index === 3 ? "selected" : ""}"
                type="button"
                data-schedule-date="${dateKey}"
                aria-label="${escapeHtml(dateLabel)}"
                ${index === 3 ? 'aria-current="date"' : ""}
              >
                <span aria-hidden="true">${new Intl.DateTimeFormat("en-US", { weekday: "narrow" }).format(date)}</span>
                <strong aria-hidden="true">${date.getDate()}</strong>
              </button>
            </li>
          `;
        })
        .join("")}
    </ol>
  `;
}

function renderDepartureCard(
  lesson: Extract<ScheduleItem, { kind: "lesson" }>,
  travel: ScheduleItem | undefined,
  buffer: ScheduleItem | undefined,
  timeZone: string,
  phase: SchedulePhase,
  now: Date,
): string {
  const departureTime = travel?.start_time ?? lesson.start_time;
  const destination = lesson.location.address;
  const navigationUrl = `https://www.google.com/maps/dir/?api=1&destination=${encodeURIComponent(destination)}`;
  const facts = [
    travel?.travel_time_mins != null ? `${travel.travel_time_mins} min travel` : "Already there",
    buffer?.buffer_mins != null ? `${buffer.buffer_mins} min recovery` : "Safe transition",
  ];
  const timing = departureTiming(phase, departureTime, lesson, now, timeZone);

  return `
    <section class="departure-card" aria-label="${escapeHtml(timing.regionLabel)}">
      <div class="departure-icon" aria-hidden="true"><i class="ph ph-navigation-arrow"></i></div>
      <div class="departure-copy">
        <div class="departure-meta"><span>${escapeHtml(timing.eyebrow)}</span><strong>${escapeHtml(timing.badge)}</strong></div>
        <h3>${escapeHtml(timing.heading)}</h3>
        <p>${escapeHtml(lesson.title)} · ${escapeHtml(formatTimeRange(lesson.start_time, lesson.end_time, timeZone))}</p>
        <span>${escapeHtml(destination)}</span>
        <div class="departure-facts">
          ${facts.map((fact) => `<span><i class="ph ph-checks" aria-hidden="true"></i>${escapeHtml(fact)}</span>`).join("")}
        </div>
      </div>
      <a class="navigate-action" href="${escapeHtml(navigationUrl)}" target="_blank" rel="noreferrer">
        ${phase === "past" ? "Open map" : "Navigate"} <i class="ph ph-arrow-square-out" aria-hidden="true"></i>
      </a>
    </section>
  `;
}

function departureTiming(
  phase: SchedulePhase,
  departureTime: string,
  lesson: Extract<ScheduleItem, { kind: "lesson" }>,
  now: Date,
  timeZone: string,
): { regionLabel: string; eyebrow: string; badge: string; heading: string } {
  const departureLabel = formatTime(departureTime, timeZone);
  if (phase === "past") {
    return {
      regionLabel: "Route recap",
      eyebrow: "Route recap",
      badge: "Past date",
      heading: `Departed at ${departureLabel}`,
    };
  }
  if (phase === "future") {
    return {
      regionLabel: "Upcoming departure",
      eyebrow: "Upcoming route",
      badge: "Planned",
      heading: `Leave by ${departureLabel}`,
    };
  }
  if (now.getTime() >= new Date(lesson.start_time).getTime()) {
    return {
      regionLabel: "Current lesson",
      eyebrow: "Current appointment",
      badge: "In progress",
      heading: `${lesson.title} now`,
    };
  }
  if (now.getTime() >= new Date(departureTime).getTime()) {
    return {
      regionLabel: "Leave now",
      eyebrow: "Next departure",
      badge: "Leave now",
      heading: "It is time to go",
    };
  }
  return {
    regionLabel: "Next departure",
    eyebrow: "Next departure",
    badge: "On track",
    heading: `Leave at ${departureLabel}`,
  };
}

function renderOpenDayCard(schedulingReady: boolean, phase: SchedulePhase, hasScheduleRows: boolean): string {
  const isCompletedDay = phase === "today" && hasScheduleRows;
  const isPast = phase === "past";
  const eyebrow = isCompletedDay ? "Day complete" : isPast ? "Schedule recap" : "Open day";
  const badge = isCompletedDay ? "All done" : isPast ? "Past date" : "Flexible";
  const heading = isCompletedDay
    ? "No more departures today"
    : isPast
      ? "No active lessons that day"
      : "No lessons scheduled";
  const detail = isPast
    ? "Choose another date to review or create availability."
    : "Your travel-aware availability is ready to use.";
  return `
    <section class="departure-card open-day-card" aria-label="${escapeHtml(heading)}">
      <div class="departure-icon" aria-hidden="true"><i class="ph ph-sun-horizon"></i></div>
      <div class="departure-copy">
        <div class="departure-meta"><span>${escapeHtml(eyebrow)}</span><strong>${escapeHtml(badge)}</strong></div>
        <h3>${escapeHtml(heading)}</h3>
        <p>${escapeHtml(detail)}</p>
      </div>
      <button class="navigate-action" type="button" ${schedulingReady ? 'data-screen="finder"' : 'data-setup-required-action="find availability"'}>Find a time <i class="ph ph-arrow-right" aria-hidden="true"></i></button>
    </section>
  `;
}

function renderEmptyTimeline(schedulingReady: boolean): string {
  return `
    <div class="empty-schedule-state">
      <i class="ph ph-leaf" aria-hidden="true"></i>
      <h3>Nothing on the route yet</h3>
      <p>Add a personal event or find a safe client time. Dayweave will account for the rest.</p>
      <div>
        <button class="secondary-action" type="button" data-add-blocker aria-label="Add event from empty schedule">Add an event</button>
        <button class="primary-action" type="button" ${schedulingReady ? 'data-screen="finder"' : 'data-setup-required-action="find availability"'}>Find availability</button>
      </div>
    </div>
  `;
}

function findFeaturedLessonIndex(schedule: ScheduleItem[], phase: SchedulePhase, now: Date): number {
  const lessonIndices = schedule.flatMap((item, index) => isActiveLesson(item) ? [index] : []);
  if (phase === "today") {
    return lessonIndices.find((index) => new Date(schedule[index].end_time).getTime() > now.getTime()) ?? -1;
  }
  return preferredLessonIndex(schedule, lessonIndices);
}

function preferredLessonIndex(schedule: ScheduleItem[], lessonIndices: number[]): number {
  return lessonIndices.find((index) => {
    const leadIn = schedule.slice(Math.max(0, index - 3), index);
    return leadIn.some((item) => item.kind === "travel") && leadIn.some((item) => item.kind === "buffer");
  }) ?? lessonIndices.find((index) => schedule.slice(Math.max(0, index - 2), index).some((item) => item.kind === "travel")) ?? lessonIndices[0] ?? -1;
}

function isActiveLesson(item: ScheduleItem): item is Extract<ScheduleItem, { kind: "lesson" }> {
  return item.kind === "lesson" && item.status !== "canceled";
}

function findPreviousItem(
  schedule: ScheduleItem[],
  lessonIndex: number,
  kind: ScheduleItem["kind"],
): ScheduleItem | undefined {
  if (lessonIndex < 0) return undefined;
  return schedule.slice(Math.max(0, lessonIndex - 3), lessonIndex).reverse().find((item) => item.kind === kind);
}

function schedulePhase(selectedDate: string, now: Date, timeZone: string): SchedulePhase {
  const today = dateKeyInTimeZone(now, timeZone);
  if (selectedDate < today) return "past";
  if (selectedDate > today) return "future";
  return "today";
}

function dateKeyInTimeZone(date: Date, timeZone: string): string {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    })
      .formatToParts(date)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function iconForKind(kind: ScheduleItem["kind"]): string {
  if (kind === "lesson") return "ph-user-circle";
  if (kind === "travel") return "ph-car";
  if (kind === "buffer") return "ph-hourglass";
  if (kind === "break") return "ph-coffee";
  if (kind === "personal") return "ph-briefcase";
  if (kind === "unavailable") return "ph-circle-dashed";
  return "ph-plus";
}

function labelForKind(kind: ScheduleItem["kind"]): string {
  if (kind === "lesson") return "Appointment";
  if (kind === "travel") return "Travel";
  if (kind === "buffer") return "Recovery buffer";
  if (kind === "break") return "Break";
  if (kind === "personal") return "Personal event";
  if (kind === "unavailable") return "Unavailable";
  return "Safe opening";
}

function parseLocalDate(date: string): Date {
  return new Date(`${date}T12:00:00`);
}

function toDateKey(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}
