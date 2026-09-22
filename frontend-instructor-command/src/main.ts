import "./styles.css";
import { InstructorCommandApiError, instructorCommandApi } from "./api/instructorApi.js";
import type { CommandCenterSnapshot, InstructorAuthSession, SchedulerRules, TimeSlotDTO } from "./api/types.js";
import { renderAvailabilityFinder } from "./components/AvailabilityFinder.js";
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
  toast: string;
  error: string;
  settingsPending: boolean;
  settingsError: string;
};

const state: AppState = {
  activeScreen: "today",
  selectedClientId: "client_maya",
  authStatus: instructorCommandApi.authMode === "live" ? "checking" : "signed_in",
  authSession: instructorCommandApi.getStoredSession(),
  snapshot: null,
  recommendedSlots: [],
  toast: "",
  error: "",
  settingsPending: false,
  settingsError: "",
};

const instructorId = import.meta.env?.VITE_INSTRUCTOR_ID || "inst_riley_001";
const commandDate = import.meta.env?.VITE_INSTRUCTOR_COMMAND_DATE || "2026-08-27";

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
    app.innerHTML = renderLockedScreen();
    wireAuthEvents();
    return;
  }

  if (!state.snapshot) {
    app.innerHTML = `<main class="loading-screen">Loading command center${renderError()}</main>`;
    return;
  }

  const body = renderActiveScreen(state.snapshot);
  app.innerHTML = `${renderShell(state.snapshot, state.activeScreen, `${renderError()}${body}`)}${renderToast()}`;
  wireEvents();
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
  return renderSettingsRules(snapshot.rules, instructorCommandApi.authMode === "live", {
    pending: state.settingsPending,
    error: state.settingsError,
  });
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
    if (button.dataset.approveRequest) return;
    button.addEventListener("click", async () => {
      await holdSlot(button.dataset.slotId ?? "");
    });
  });

  app.querySelectorAll<HTMLButtonElement>("[data-approve-request]").forEach((button) => {
    button.addEventListener("click", async () => {
      await runAction(async () => {
        await instructorCommandApi.approveRequest(button.dataset.approveRequest ?? "", button.dataset.slotId ?? "");
        state.snapshot = await instructorCommandApi.getSnapshot(instructorId, commandDate);
        state.toast = "Request approved.";
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

  app.querySelector<HTMLFormElement>("[data-rules-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.snapshot || state.settingsPending) return;
    const payload = schedulerRulesFromForm(event.currentTarget as HTMLFormElement, state.snapshot.rules);
    state.settingsPending = true;
    state.settingsError = "";
    render();
    try {
      const saved = await instructorCommandApi.saveRules(payload);
      if (state.snapshot) state.snapshot = { ...state.snapshot, rules: saved };
      state.toast = "Scheduling rules saved.";
    } catch (error) {
      if (!handleAuthFailure(error)) state.settingsError = formatApiError(error);
    } finally {
      state.settingsPending = false;
      render();
      app.querySelector<HTMLElement>("[data-settings-error]")?.focus();
    }
  });
}

function schedulerRulesFromForm(form: HTMLFormElement, current: SchedulerRules): SchedulerRules {
  const data = new FormData(form);
  return {
    ...current,
    buffers_enabled: data.has("buffers_enabled"),
    apply_distance_buffer_at_shift_start: data.has("apply_distance_buffer_at_shift_start"),
    rounding_increment_mins: Number(data.get("rounding_increment_mins")) as SchedulerRules["rounding_increment_mins"],
    post_break_buffer_mins: Number(data.get("post_break_buffer_mins")),
    distance_buffer_tiers: current.distance_buffer_tiers.map((tier) => ({
      ...tier,
      buffer_mins: Number(data.get(`tier.${tier.id}.buffer_mins`)),
    })),
  };
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

async function holdSlot(slotId: string): Promise<void> {
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
    state.toast = booking.message;
    state.snapshot = await instructorCommandApi.getSnapshot(instructorId, commandDate);
    await refreshSlots();
  });
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

function handleAuthFailure(error: unknown): boolean {
  if (instructorCommandApi.authMode !== "live") return false;
  if (!(error instanceof InstructorCommandApiError) || !error.isAuthFailure) return false;

  instructorCommandApi.clearSession();
  state.authSession = null;
  state.snapshot = null;
  state.recommendedSlots = [];
  state.authStatus = "expired";
  state.error = "Your session expired. Sign in again to continue.";
  return true;
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
