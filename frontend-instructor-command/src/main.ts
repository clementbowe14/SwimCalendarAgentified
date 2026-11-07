import "./styles.css";
import { InstructorCommandApiError, instructorCommandApi } from "./api/instructorApi.js";
import type { CalendarLinks, CommandCenterSnapshot, InstructorAuthSession, TimeSlotDTO } from "./api/types.js";
import { renderAvailabilityFinder } from "./components/AvailabilityFinder.js";
import {
  renderCalendarHandoffModal,
  type CalendarHandoffModalModel,
} from "./components/CalendarHandoffModal.js";
import { renderClientDetail } from "./components/ClientDetail.js";
import { renderShell, type ScreenId } from "./components/Layout.js";
import { renderRequestManagement } from "./components/RequestManagement.js";
import { renderSettingsRules } from "./components/SettingsRules.js";
import { renderTodaySchedule } from "./components/TodaySchedule.js";
import { buildFindSlotsRequest, findFeaturedClient } from "./domain/screenModels.js";
import { escapeHtml } from "./lib/html.js";

const root = document.querySelector<HTMLDivElement>("#app");

if (!root) {
  throw new Error("App root was not found.");
}

const app = root;

type AppState = {
  activeScreen: ScreenId;
  selectedClientId: string;
  authStatus: "checking" | "locked" | "expired" | "signed_in";
  authSession: InstructorAuthSession | null;
  snapshot: CommandCenterSnapshot | null;
  recommendedSlots: TimeSlotDTO[];
  calendarHandoff: CalendarHandoffModalModel | null;
  toast: string;
  error: string;
};

const state: AppState = {
  activeScreen: "today",
  selectedClientId: "client_maya",
  authStatus: instructorCommandApi.authMode === "live" ? "checking" : "signed_in",
  authSession: instructorCommandApi.getStoredSession(),
  snapshot: null,
  recommendedSlots: [],
  calendarHandoff: null,
  toast: "",
  error: "",
};

const instructorId = import.meta.env?.VITE_INSTRUCTOR_ID || "inst_riley_001";
const commandDate = import.meta.env?.VITE_INSTRUCTOR_COMMAND_DATE || "2026-08-27";
const instructorApiBaseUrl = import.meta.env?.VITE_INSTRUCTOR_API_URL || "";

void boot();

async function boot(): Promise<void> {
  if (instructorCommandApi.authMode === "live") {
    const storedSession = instructorCommandApi.getStoredSession();
    if (!storedSession) {
      state.authStatus = "locked";
      render();
      return;
    }

    try {
      state.authSession = {
        ...storedSession,
        coach: await instructorCommandApi.validateSession(),
      };
    } catch (error) {
      if (!handleAuthFailure(error)) {
        state.authStatus = "locked";
        state.error = formatApiError(error);
      }
      render();
      return;
    }
  }

  state.authStatus = "signed_in";
  await loadCommandCenter();
}

async function loadCommandCenter(): Promise<void> {
  try {
    state.snapshot = await instructorCommandApi.getSnapshot(instructorId, commandDate);
    if (!state.snapshot.clients.some((client) => client.id === state.selectedClientId)) {
      state.selectedClientId = state.snapshot.clients[0]?.id ?? "client_new";
    }
    const selectedClient = findFeaturedClient(state.snapshot, state.selectedClientId);
    state.recommendedSlots = await instructorCommandApi.findAvailableSlots(
      buildFindSlotsRequest(state.snapshot.instructor.id, selectedClient, state.snapshot.date),
    );
  } catch (error) {
    if (!handleAuthFailure(error)) {
      state.error = formatApiError(error);
    }
  }
  render();
}

function render(): void {
  if (instructorCommandApi.authMode === "live" && state.authStatus !== "signed_in") {
    document.body.classList.remove("calendar-modal-open");
    app.innerHTML = renderLockedScreen();
    wireAuthEvents();
    return;
  }

  if (!state.snapshot) {
    document.body.classList.remove("calendar-modal-open");
    app.innerHTML = `<main class="loading-screen">Loading command center${renderError()}</main>`;
    return;
  }

  const body = renderActiveScreen(state.snapshot);
  const modal = state.calendarHandoff
    ? renderCalendarHandoffModal(state.calendarHandoff, instructorApiBaseUrl, window.location.href)
    : "";
  document.body.classList.toggle("calendar-modal-open", Boolean(state.calendarHandoff));
  app.innerHTML = `
    <div data-command-surface ${state.calendarHandoff ? "inert" : ""}>
      ${renderShell(state.snapshot, state.activeScreen, `${renderError()}${body}`)}
      ${renderToast()}
    </div>
    ${modal}
  `;
  wireEvents();
  wireCalendarHandoffEvents();
}

function renderLockedScreen(): string {
  const isExpired = state.authStatus === "expired";
  const heading = isExpired ? "Session expired" : "Swim Command locked";
  const body = isExpired
    ? "Sign in again to continue managing protected coach data."
    : "Sign in to open the instructor command center.";

  return `
    <main class="auth-shell">
      <section class="auth-panel" aria-labelledby="auth-title">
        <div class="brand-lockup">
          <span class="brand-mark">SC</span>
          <div>
            <strong>Swim Command</strong>
            <span>Instructor access</span>
          </div>
        </div>
        <div class="auth-copy">
          <p class="eyebrow">Protected workspace</p>
          <h1 id="auth-title">${escapeHtml(heading)}</h1>
          <p>${escapeHtml(body)}</p>
        </div>
        ${renderError()}
        <form class="auth-form" data-sign-in-form>
          <label>
            <span>Email</span>
            <input name="email" type="email" autocomplete="username" required />
          </label>
          <label>
            <span>Password</span>
            <input name="password" type="password" autocomplete="current-password" required />
          </label>
          <button class="primary-action" type="submit">Sign in</button>
        </form>
      </section>
    </main>
  `;
}

function renderActiveScreen(snapshot: CommandCenterSnapshot): string {
  if (state.activeScreen === "today") return renderTodaySchedule(snapshot);
  if (state.activeScreen === "finder") {
    return renderAvailabilityFinder(snapshot, findFeaturedClient(snapshot, state.selectedClientId), state.recommendedSlots);
  }
  if (state.activeScreen === "client") return renderClientDetail(snapshot, state.selectedClientId);
  if (state.activeScreen === "requests") return renderRequestManagement(snapshot);
  return renderSettingsRules(snapshot.rules, instructorCommandApi.authMode === "live");
}

function wireEvents(): void {
  app.querySelector<HTMLButtonElement>("[data-sign-out]")?.addEventListener("click", async () => {
    await signOut();
  });

  app.querySelectorAll<HTMLButtonElement>("[data-screen]").forEach((button) => {
    button.addEventListener("click", () => {
      state.activeScreen = button.dataset.screen as ScreenId;
      render();
    });
  });

  app.querySelector<HTMLSelectElement>("[data-client-picker]")?.addEventListener("change", async (event) => {
    const target = event.currentTarget as HTMLSelectElement;
    await runAction(async () => {
      state.selectedClientId = target.value;
      await refreshSlots();
    });
  });

  app.querySelector<HTMLButtonElement>("[data-refresh-slots]")?.addEventListener("click", async () => {
    await runAction(async () => {
      await refreshSlots();
      state.toast = "Safe starts refreshed from the adapter.";
    });
  });

  app.querySelectorAll<HTMLButtonElement>("[data-slot-id]").forEach((button) => {
    button.addEventListener("click", async () => {
      await bookSlot(button.dataset.slotId ?? "", button.dataset.calendarFocus ?? "");
    });
  });

  app.querySelectorAll<HTMLButtonElement>("[data-confirm-request]").forEach((button) => {
    button.addEventListener("click", async () => {
      await runAction(async () => {
        const confirmation = await instructorCommandApi.confirmRequest(button.dataset.confirmRequest ?? "");
        const appointmentId = confirmation.appointment_id ?? confirmation.appointment_ids[0] ?? null;
        await completePersistedAction(
          appointmentId,
          confirmation.calendar_links ?? null,
          button.dataset.calendarFocus ?? "",
          "requests",
        );
      });
    });
  });

  app.querySelectorAll<HTMLButtonElement>("[data-alternatives-request]").forEach((button) => {
    button.addEventListener("click", async () => {
      await runAction(async () => {
        await instructorCommandApi.requestAlternatives(button.dataset.alternativesRequest ?? "", []);
        state.snapshot = await instructorCommandApi.getSnapshot(instructorId, commandDate);
        state.toast = "Alternatives requested.";
      });
    });
  });

  app.querySelector<HTMLButtonElement>("[data-save-rules]")?.addEventListener("click", async () => {
    if (!state.snapshot) return;
    await runAction(async () => {
      if (!state.snapshot) return;
      await instructorCommandApi.saveRules(state.snapshot.rules);
      state.toast = "Rules saved to the adapter.";
    });
  });
}

function wireAuthEvents(): void {
  app.querySelector<HTMLFormElement>("[data-sign-in-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (event.currentTarget instanceof HTMLFormElement) {
      await signIn(event.currentTarget);
    }
  });
}

async function signIn(form: HTMLFormElement): Promise<void> {
  const formData = new FormData(form);
  const email = String(formData.get("email") ?? "").trim();
  const password = String(formData.get("password") ?? "");

  state.error = "";
  if (!email || !password) {
    state.error = "Email and password are required.";
    render();
    return;
  }

  state.authStatus = "checking";
  render();

  try {
    state.authSession = await instructorCommandApi.signIn({ email, password });
    state.snapshot = null;
    state.recommendedSlots = [];
    state.calendarHandoff = null;
    state.authStatus = "signed_in";
    state.toast = "Signed in.";
    await loadCommandCenter();
  } catch (error) {
    state.authStatus = "locked";
    state.authSession = null;
    state.error = formatApiError(error);
    render();
  }
}

async function signOut(): Promise<void> {
  state.error = "";
  try {
    await instructorCommandApi.signOut();
  } catch (error) {
    state.error = formatApiError(error);
  } finally {
    state.authSession = null;
    state.snapshot = null;
    state.recommendedSlots = [];
    state.calendarHandoff = null;
    state.authStatus = "locked";
    state.toast = "";
    render();
  }
}

async function refreshSlots(): Promise<void> {
  if (!state.snapshot) return;
  const selectedClient = findFeaturedClient(state.snapshot, state.selectedClientId);
  state.recommendedSlots = await instructorCommandApi.findAvailableSlots(
    buildFindSlotsRequest(state.snapshot.instructor.id, selectedClient, state.snapshot.date),
  );
}

async function bookSlot(slotId: string, returnFocusKey: string): Promise<void> {
  await runAction(async () => {
    if (!state.snapshot) return;
    const selectedClient = findFeaturedClient(state.snapshot, state.selectedClientId);
    const slot = state.recommendedSlots.find((item) => item.id === slotId);
    if (!slot) return;

    const booking = await instructorCommandApi.bookLesson({
      instructor_id: state.snapshot.instructor.id,
      client_id: selectedClient.id,
      client_name: selectedClient.name,
      start_time_iso: slot.start_time,
      duration_mins: selectedClient.lesson_duration_mins,
      location_address: slot.target_location.address,
      is_instructor_facility: slot.target_location.is_instructor_facility,
      idempotency_key: `command:${selectedClient.id}:${slot.start_time}`,
    });
    await completePersistedAction(
      booking.appointment_id,
      booking.calendar_links ?? null,
      returnFocusKey,
      "finder",
    );
  });
}

async function completePersistedAction(
  appointmentId: string | null,
  mutationLinks: CalendarLinks | null,
  returnFocusKey: string,
  returnFocusFallback: "finder" | "requests",
): Promise<void> {
  state.toast = "";
  let links = mutationLinks;

  if (!links && appointmentId) {
    try {
      links = await instructorCommandApi.getAppointmentCalendarLinks(appointmentId);
    } catch (error) {
      if (handleAuthFailure(error, persistedAuthMessage())) return;
      state.calendarHandoff = unavailableHandoff(appointmentId, returnFocusKey, returnFocusFallback);
      await refreshAfterPersistence();
      return;
    }
  }

  state.calendarHandoff = links
    ? {
        status: "ready",
        appointmentId,
        links,
        returnFocusKey,
        returnFocusFallback,
        downloadStatus: "idle",
        inlineError: "",
      }
    : unavailableHandoff(appointmentId, returnFocusKey, returnFocusFallback);
  await refreshAfterPersistence();
}

function unavailableHandoff(
  appointmentId: string | null,
  returnFocusKey: string,
  returnFocusFallback: "finder" | "requests",
): CalendarHandoffModalModel {
  return {
    status: "unavailable",
    appointmentId,
    links: null,
    returnFocusKey,
    returnFocusFallback,
    downloadStatus: "idle",
    inlineError: "",
  };
}

async function refreshAfterPersistence(): Promise<void> {
  if (state.authStatus !== "signed_in") return;
  try {
    state.snapshot = await instructorCommandApi.getSnapshot(instructorId, commandDate);
    await refreshSlots();
  } catch (error) {
    if (!handleAuthFailure(error, persistedRefreshAuthMessage())) {
      state.error = "The appointment was saved, but the schedule could not be refreshed. Reload to see the latest schedule.";
    }
  }
}

async function runAction(action: () => Promise<void>): Promise<void> {
  state.error = "";
  try {
    await action();
    render();
  } catch (error) {
    if (!handleAuthFailure(error)) {
      state.error = formatApiError(error);
    }
    render();
  }
}

function handleAuthFailure(error: unknown, persistedMessage?: string): boolean {
  if (instructorCommandApi.authMode !== "live") return false;
  if (!(error instanceof InstructorCommandApiError) || !error.isAuthFailure) return false;

  instructorCommandApi.clearSession();
  state.authSession = null;
  state.snapshot = null;
  state.recommendedSlots = [];
  state.calendarHandoff = null;
  state.authStatus = "expired";
  state.error = persistedMessage ?? "Your session expired. Sign in again to continue.";
  return true;
}

function persistedAuthMessage(): string {
  return "The appointment was saved, but calendar options could not load before your session expired. Sign in again; do not book it again.";
}

function persistedRefreshAuthMessage(): string {
  return "The appointment was saved, but your session expired before the app finished refreshing. Sign in again; do not book it again.";
}

function wireCalendarHandoffEvents(): void {
  const dialog = app.querySelector<HTMLElement>("[data-calendar-dialog]");
  if (!dialog || !state.calendarHandoff) return;

  app.querySelectorAll<HTMLButtonElement>("[data-calendar-close]").forEach((button) => {
    button.addEventListener("click", closeCalendarHandoff);
  });
  app.querySelector<HTMLElement>("[data-calendar-backdrop]")?.addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeCalendarHandoff();
  });
  app.querySelector<HTMLButtonElement>("[data-calendar-ics]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget as HTMLButtonElement;
    await downloadCalendarFile(button.dataset.calendarIcs ?? "");
  });
  dialog.addEventListener("keydown", (event) => trapCalendarDialogFocus(event, dialog));
  dialog.focus();
}

function trapCalendarDialogFocus(event: KeyboardEvent, dialog: HTMLElement): void {
  if (event.key === "Escape") {
    event.preventDefault();
    closeCalendarHandoff();
    return;
  }
  if (event.key !== "Tab") return;

  const focusable = [...dialog.querySelectorAll<HTMLElement>(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )].filter((element) => !element.hasAttribute("hidden"));
  if (!focusable.length) {
    event.preventDefault();
    dialog.focus();
    return;
  }

  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  if (active === dialog) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
    return;
  }
  if (event.shiftKey && (active === first || !dialog.contains(active))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
    event.preventDefault();
    first.focus();
  }
}

function closeCalendarHandoff(): void {
  const closing = state.calendarHandoff;
  if (!closing) return;
  state.calendarHandoff = null;
  render();

  const origin = [...app.querySelectorAll<HTMLElement>("[data-calendar-focus]")].find(
    (element) => element.dataset.calendarFocus === closing.returnFocusKey,
  );
  const fallback = closing.returnFocusFallback === "finder"
    ? app.querySelector<HTMLElement>("[data-refresh-slots]")
    : app.querySelector<HTMLElement>('[data-screen="requests"]');
  const target = origin ?? fallback ?? app.querySelector<HTMLElement>(".topbar h1");
  if (target && !target.matches("button, a, input, select, textarea, [tabindex]")) target.tabIndex = -1;
  target?.focus();
}

async function downloadCalendarFile(calendarUrl: string): Promise<void> {
  const appointmentId = state.calendarHandoff?.appointmentId;
  if (!state.calendarHandoff || state.calendarHandoff.status !== "ready") return;
  state.calendarHandoff.downloadStatus = "downloading";
  state.calendarHandoff.inlineError = "";
  render();

  try {
    const blob = await instructorCommandApi.downloadCalendarIcs(calendarUrl);
    if (!state.calendarHandoff || state.calendarHandoff.appointmentId !== appointmentId) return;
    triggerCalendarDownload(blob);
    state.calendarHandoff.downloadStatus = "downloaded";
  } catch (error) {
    if (handleAuthFailure(error, persistedAuthMessage())) {
      render();
      return;
    }
    if (!state.calendarHandoff || state.calendarHandoff.appointmentId !== appointmentId) return;
    if (error instanceof InstructorCommandApiError && error.code === "CALENDAR_LINKS_UNAVAILABLE") {
      state.calendarHandoff = unavailableHandoff(
        appointmentId ?? null,
        state.calendarHandoff.returnFocusKey,
        state.calendarHandoff.returnFocusFallback,
      );
    } else {
      state.calendarHandoff.downloadStatus = "idle";
      state.calendarHandoff.inlineError = formatApiError(error);
    }
  }
  render();
}

function triggerCalendarDownload(blob: Blob): void {
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = "appointment.ics";
  anchor.rel = "noopener noreferrer";
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1_000);
}

function renderToast(): string {
  if (!state.toast) return "";
  const message = state.toast;
  window.setTimeout(() => {
    state.toast = "";
    render();
  }, 1800);
  return `<div class="toast" role="status">${message}</div>`;
}

function renderError(): string {
  if (!state.error) return "";
  return `<div class="error-banner" role="alert">${escapeHtml(state.error)}</div>`;
}

function formatApiError(error: unknown): string {
  if (error instanceof InstructorCommandApiError) {
    return `${error.message} (${error.code})`;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "Something went wrong while talking to the scheduling API.";
}
