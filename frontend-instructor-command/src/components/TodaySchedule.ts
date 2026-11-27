import type { AgendaNextUp, CoachAgenda, CommandCenterSnapshot, LessonScheduleItem, ScheduleItem } from "../api/types.js";
import { buildAgendaRows, isScheduleBlocker, requestDateLabel } from "../domain/screenModels.js";
import { dateKeyInTimeZone, formatTime, formatTimeRange, scheduleTimeZone, shiftDateKey, supportedScheduleTimeZone } from "../lib/format.js";
import { escapeHtml } from "../lib/html.js";

type SchedulePhase = "past" | "today" | "future";
type DepartureTiming = { regionLabel: string; eyebrow: string; badge: string; heading: string };

export type AgendaLoadState =
  | { status: "loading" | "error" }
  | { status: "loaded"; response: CoachAgenda };

export function scheduleItems(
  snapshot: CommandCenterSnapshot,
  date: string,
  agenda?: AgendaLoadState,
): ScheduleItem[] {
  if (!agenda) return snapshot.schedule;
  if (agenda.status === "loaded" && agenda.response.date === date) {
    return agenda.response.items.filter((item) => item.kind !== "open_gap" || item.start_times?.length);
  }
  if (agenda.status === "error" && snapshot.date === date) {
    return snapshot.schedule.filter((item) => item.kind === "lesson" || isScheduleBlocker(item));
  }
  return [];
}

export function renderTodaySchedule(
  snapshot: CommandCenterSnapshot,
  date = snapshot.date,
  agenda?: AgendaLoadState,
): string {
  const now = new Date();
  const response = agenda?.status === "loaded" && agenda.response.date === date ? agenda.response : null;
  const providerTimeZone = response?.timezone ?? snapshot.instructor.timezone;
  const supportedTimeZone = supportedScheduleTimeZone(providerTimeZone);
  const timeZone = supportedTimeZone ?? (agenda ? "UTC" : scheduleTimeZone());
  const today = response?.today ?? dateKeyInTimeZone(now, supportedTimeZone ?? scheduleTimeZone());
  const phase = schedulePhase(date, today);
  const scheduleChanged = response?.status === "schedule_changed";
  const items = scheduleItems(snapshot, date, agenda);
  const rows = buildAgendaRows(items, timeZone);
  const repeatedLabels = agenda ? repeatedStartLabels(items, timeZone) : new Set<string>();
  const pendingRequest = snapshot.requests.find((request) => request.status === "pending");
  const heading = calendarDateLabel(date, { weekday: "long", month: "long", day: "numeric" });
  const activeLessonCount = items.filter(isActiveLesson).length;
  const appointmentCount = `${activeLessonCount} ${activeLessonCount === 1 ? "appointment" : "appointments"}`;
  const summary = agenda
    ? (response || rows.length > 0 ? appointmentCount : "")
    : (rows.length === 0 ? "A clear day, ready when you are." : `${activeLessonCount} appointments · travel and recovery time included`);
  const schedulingReady = snapshot.instructor.setup_complete && response?.status !== "setup_required";
  const departure = agenda
    ? (response && !scheduleChanged && phase !== "past" ? renderLiveDeparture(response, phase, now, timeZone) : "")
    : renderMockDeparture(items, timeZone, phase, now, schedulingReady);

  return `
    <section class="screen-grid today-screen schedule-dashboard" aria-label="Today schedule">
      <header class="schedule-heading">
        <div>
          <p class="schedule-kicker">${phase === "today" ? "Today" : agenda && phase === "past" ? "Past schedule" : "Schedule"}</p>
          <h2>${escapeHtml(heading)}</h2>
          ${summary ? `<p class="schedule-summary">${escapeHtml(summary)}</p>` : ""}
        </div>
        <button class="schedule-add-button" type="button" data-add-blocker aria-label="Add schedule event">
          <i class="ph ph-plus" aria-hidden="true"></i>
          <span>Add event</span>
        </button>
      </header>

      ${renderDateNavigation(date)}

      ${agenda && !supportedTimeZone && rows.length > 0 ? '<div class="agenda-notice">Times shown in UTC because the provider timezone is unavailable in this browser.</div>' : ""}
      ${agenda ? renderAgendaNotice(agenda.status === "loaded" && !response ? { status: "loading" } : agenda, rows.length > 0) : ""}
      ${departure}

      ${!agenda || (response && !scheduleChanged) || rows.length > 0 ? `<section class="route-section" aria-labelledby="day-route-title">
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
                const item = items[index];
                const starts = agenda && item.kind === "open_gap"
                  ? item.start_times?.map((start) => {
                      const label = formatTime(start, timeZone);
                      return repeatedLabels.has(label) ? formatTime(start, timeZone, true) : label;
                    }) ?? []
                  : null;
                const location = item.kind === "calendar_busy" ? undefined : item.location?.address;
                const supportingDetail = starts ? item.note ?? location : row.kind === "lesson" ? item.note : row.detail;
                const detail = starts ? starts.join(" · ") : location ?? row.detail;
                return `
                  <li class="route-row ${row.tone}" data-agenda-row="${escapeHtml(row.id)}">
                    <time>${escapeHtml(starts ? starts[0] ?? "" : row.time)}</time>
                    <span class="route-node" aria-hidden="true"><i class="ph ${iconForKind(row.kind)}"></i></span>
                    <article class="route-content">
                      <div class="route-content-copy">
                        <span class="route-label">${escapeHtml(starts ? "Availability" : labelForKind(row.kind))}</span>
                        <strong>${escapeHtml(starts ? "Bookable starts" : row.title)}</strong>
                        ${detail ? `<span${starts ? ' class="agenda-start-times"' : ""}>${escapeHtml(detail)}</span>` : ""}
                        ${(starts || location) && supportingDetail ? `<small>${escapeHtml(supportingDetail)}</small>` : ""}
                        ${row.kind !== "calendar_busy" && row.status && row.status !== "scheduled" ? `<small class="route-status">${escapeHtml(row.status.replaceAll("_", " "))}</small>` : ""}
                      </div>
                      ${isScheduleBlocker(item)
                        ? `<button class="agenda-edit-button icon-button" type="button" data-edit-blocker="${escapeHtml(row.id)}" aria-label="Edit ${escapeHtml(row.title)}"><i class="ph ph-pencil-simple" aria-hidden="true"></i></button>`
                        : ""}
                    </article>
                  </li>
                `;
              })
              .join("")}
          </ol>
        ` : renderEmptyTimeline(schedulingReady, response?.status)}
      </section>` : ""}

      ${pendingRequest ? `
        <section class="request-cue" aria-label="Pending request from ${escapeHtml(pendingRequest.client_name)}">
          <span class="request-cue-icon" aria-hidden="true"><i class="ph ph-tray"></i></span>
          <div>
            <p class="eyebrow">Needs your review</p>
            <h3>${escapeHtml(pendingRequest.client_name)} requested a time</h3>
            <span>${escapeHtml(requestDateLabel(pendingRequest, timeZone))}</span>
          </div>
          <button type="button" data-screen="requests">Review <i class="ph ph-arrow-right" aria-hidden="true"></i></button>
        </section>
      ` : ""}
    </section>
  `;
}

function repeatedStartLabels(items: ScheduleItem[], timeZone: string): Set<string> {
  const seen = new Set<string>();
  const repeated = new Set<string>();
  for (const item of items) {
    if (item.kind !== "open_gap") continue;
    for (const start of item.start_times ?? []) {
      const label = formatTime(start, timeZone);
      if (seen.has(label)) repeated.add(label);
      seen.add(label);
    }
  }
  return repeated;
}

function renderDateNavigation(selectedDate: string): string {
  const days = Array.from({ length: 7 }, (_, index) => shiftDateKey(selectedDate, index - 3));
  return `
    <div class="schedule-date-navigation" role="group" aria-label="Schedule date navigation">
      <div class="schedule-date-actions">
        <button class="secondary-action" type="button" data-schedule-step="previous">Previous</button>
        <button class="secondary-action" type="button" data-schedule-step="today">Today</button>
        <button class="secondary-action" type="button" data-schedule-step="next">Next</button>
      </div>
      <ol class="week-strip" aria-label="Seven day calendar">
      ${days
        .map((dateKey, index) => {
          const dateLabel = calendarDateLabel(dateKey, {
            weekday: "long",
            month: "long",
            day: "numeric",
            year: "numeric",
          });
          return `
            <li>
              <button
                class="${index === 3 ? "selected" : ""}"
                type="button"
                data-schedule-date="${dateKey}"
                aria-label="${escapeHtml(dateLabel)}"
                ${index === 3 ? 'aria-current="date"' : ""}
              >
                <span aria-hidden="true">${calendarDateLabel(dateKey, { weekday: "narrow" })}</span>
                <strong aria-hidden="true">${Number(dateKey.slice(-2))}</strong>
              </button>
            </li>
          `;
        })
        .join("")}
      </ol>
    </div>
  `;
}

function renderMockDeparture(
  items: ScheduleItem[],
  timeZone: string,
  phase: SchedulePhase,
  now: Date,
  schedulingReady: boolean,
): string {
  const index = findFeaturedLessonIndex(items, phase, now);
  const lesson = items[index];
  if (!lesson || !isActiveLesson(lesson)) return renderOpenDayCard(schedulingReady, phase, items.length > 0);
  const travel = findPreviousItem(items, index, "travel");
  const buffer = findPreviousItem(items, index, "buffer");
  const departureTime = travel?.start_time ?? lesson.start_time;
  const facts = [
    travel?.travel_time_mins != null ? `${travel.travel_time_mins} min travel` : "Already there",
    buffer?.buffer_mins != null ? `${buffer.buffer_mins} min recovery` : "Safe transition",
  ];
  return renderDepartureCard(lesson, departureTiming(phase, departureTime, lesson, now, timeZone), facts, timeZone, phase !== "past");
}

function renderLiveDeparture(agenda: CoachAgenda, phase: SchedulePhase, now: Date, timeZone: string): string {
  const plan = agenda.next_up;
  const lesson = agenda.items.find((item): item is LessonScheduleItem => (
    isActiveLesson(item) && item.id === plan?.appointment_id
  ));
  if (!plan || !lesson) return "";
  if (phase === "today" && Date.parse(lesson.end_time) <= now.getTime()) return "";
  const facts = [
    plan.travel_time_mins == null ? "" : `${plan.travel_time_mins} min travel`,
    plan.buffer_mins == null ? "" : `${plan.buffer_mins} min recovery`,
  ].filter(Boolean);
  const timing = liveDepartureTiming(plan, lesson, phase, now, timeZone);
  return renderDepartureCard(lesson, timing, facts, timeZone, plan.leave_by !== null && plan.travel_time_mins !== 0);
}

function renderDepartureCard(
  lesson: LessonScheduleItem,
  timing: DepartureTiming,
  facts: string[],
  timeZone: string,
  navigate: boolean,
): string {
  const destination = lesson.location.address;
  const navigationUrl = `https://www.google.com/maps/dir/?api=1&destination=${encodeURIComponent(destination)}`;
  return `
    <section class="departure-card" aria-label="${escapeHtml(timing.regionLabel)}">
      <div class="departure-icon" aria-hidden="true"><i class="ph ph-navigation-arrow"></i></div>
      <div class="departure-copy">
        <div class="departure-meta"><span>${escapeHtml(timing.eyebrow)}</span>${timing.badge ? `<strong>${escapeHtml(timing.badge)}</strong>` : ""}</div>
        <h3>${escapeHtml(timing.heading)}</h3>
        <p>${escapeHtml(lesson.title)} · ${escapeHtml(formatTimeRange(lesson.start_time, lesson.end_time, timeZone))}</p>
        <span>${escapeHtml(destination)}</span>
        ${facts.length > 0 ? `<div class="departure-facts">
          ${facts.map((fact) => `<span><i class="ph ph-checks" aria-hidden="true"></i>${escapeHtml(fact)}</span>`).join("")}
        </div>` : ""}
      </div>
      <a class="navigate-action" href="${escapeHtml(navigationUrl)}" target="_blank" rel="noreferrer">
        ${navigate ? "Navigate" : "Open map"} <i class="ph ph-arrow-square-out" aria-hidden="true"></i>
      </a>
    </section>
  `;
}

function departureTiming(
  phase: SchedulePhase,
  departureTime: string,
  lesson: LessonScheduleItem,
  now: Date,
  timeZone: string,
): DepartureTiming {
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

function liveDepartureTiming(
  plan: AgendaNextUp,
  lesson: LessonScheduleItem,
  phase: SchedulePhase,
  now: Date,
  timeZone: string,
): DepartureTiming {
  if (phase === "today" && Date.parse(lesson.start_time) <= now.getTime()) {
    return { regionLabel: "Current lesson", eyebrow: "Current appointment", badge: "In progress", heading: `${lesson.title} now` };
  }
  const next = { regionLabel: "Next appointment", eyebrow: "Next appointment", badge: "" };
  if (plan.travel_time_mins === 0) {
    const readyBy = plan.leave_by && (plan.buffer_mins ?? 0) > 0 ? formatTime(plan.leave_by, timeZone) : "";
    return { ...next, heading: readyBy ? `Ready by ${readyBy}` : "No travel needed" };
  }
  if (!plan.leave_by) return { ...next, heading: "Departure time unavailable" };
  if (phase === "today" && Date.parse(plan.leave_by) < now.getTime()) {
    return { ...next, badge: "Leave now", heading: `Leave-by time passed (${formatTime(plan.leave_by, timeZone)})` };
  }
  return { ...next, heading: `Leave by ${formatTime(plan.leave_by, timeZone)}` };
}

function renderAgendaNotice(agenda: AgendaLoadState, hasKnownRows: boolean): string {
  const status = agenda.status === "loaded" ? agenda.response.status : agenda.status;
  if (status === "ready") return "";
  if (status === "loading") return '<div class="agenda-notice" role="status">Loading the day’s agenda…</div>';
  if (status === "error" || status === "schedule_changed") return `
    <div class="agenda-notice" role="alert">
      <p>${status === "error" ? "The agenda couldn’t load." : "The schedule changed while the agenda was loading."}${hasKnownRows ? " Showing known appointments and schedule events." : ""}</p>
      <button class="secondary-action" type="button" data-retry-agenda>Retry agenda</button>
    </div>
  `;
  const message = {
    setup_required: "Complete provider setup to plan travel and see bookable starts.",
    calendar_unavailable: "Google Calendar couldn’t be checked. Travel planning and bookable starts are unavailable.",
    routing_unavailable: "Travel estimates and bookable starts are unavailable.",
  }[status];
  return `
    <div class="agenda-notice" role="status">
      <p>${message}</p>
      ${status === "routing_unavailable" ? "" : '<button class="secondary-action" type="button" data-screen="settings">Open settings</button>'}
    </div>
  `;
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

function renderEmptyTimeline(schedulingReady: boolean, status?: CoachAgenda["status"]): string {
  const detail = status === undefined
    ? "Add a personal event or find a safe client time. Dayweave will account for the rest."
    : "Add an event or search for a lesson time.";
  return `
    <div class="empty-schedule-state">
      <i class="ph ph-leaf" aria-hidden="true"></i>
      <h3>Nothing on the route yet</h3>
      <p>${detail}</p>
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

function isActiveLesson(item: ScheduleItem): item is LessonScheduleItem {
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

function schedulePhase(selectedDate: string, today: string): SchedulePhase {
  if (selectedDate < today) return "past";
  if (selectedDate > today) return "future";
  return "today";
}

function iconForKind(kind: ScheduleItem["kind"]): string {
  if (kind === "lesson") return "ph-user-circle";
  if (kind === "travel") return "ph-car";
  if (kind === "buffer") return "ph-hourglass";
  if (kind === "break") return "ph-coffee";
  if (kind === "personal") return "ph-briefcase";
  if (kind === "unavailable") return "ph-circle-dashed";
  if (kind === "calendar_busy") return "ph-calendar-x";
  return "ph-plus";
}

function labelForKind(kind: ScheduleItem["kind"]): string {
  if (kind === "lesson") return "Appointment";
  if (kind === "travel") return "Travel";
  if (kind === "buffer") return "Recovery buffer";
  if (kind === "break") return "Break";
  if (kind === "personal") return "Personal event";
  if (kind === "unavailable") return "Unavailable";
  if (kind === "calendar_busy") return "Calendar busy";
  return "Safe opening";
}

function calendarDateLabel(date: string, options: Intl.DateTimeFormatOptions): string {
  return new Intl.DateTimeFormat("en-US", { ...options, timeZone: "UTC" }).format(new Date(`${date}T00:00:00Z`));
}
