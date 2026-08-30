import "./styles.css";
import { InstructorCommandApiError, instructorCommandApi } from "./api/instructorApi.js";
import type { CommandCenterSnapshot, TimeSlotDTO } from "./api/types.js";
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
  snapshot: CommandCenterSnapshot | null;
  recommendedSlots: TimeSlotDTO[];
  toast: string;
  error: string;
};

const state: AppState = {
  activeScreen: "today",
  selectedClientId: "client_maya",
  snapshot: null,
  recommendedSlots: [],
  toast: "",
  error: "",
};

const instructorId = readFrontendEnv("VITE_INSTRUCTOR_ID") ?? "inst_riley_001";
const commandDate = readFrontendEnv("VITE_INSTRUCTOR_COMMAND_DATE") ?? "2026-08-27";

void boot();

async function boot(): Promise<void> {
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
    state.error = formatApiError(error);
  }
  render();
}

function render(): void {
  if (!state.snapshot) {
    app.innerHTML = `<main class="loading-screen">Loading command center${renderError()}</main>`;
    return;
  }

  const body = renderActiveScreen(state.snapshot);
  app.innerHTML = `${renderShell(state.snapshot, state.activeScreen, `${renderError()}${body}`)}${renderToast()}`;
  wireEvents();
}

function renderActiveScreen(snapshot: CommandCenterSnapshot): string {
  if (state.activeScreen === "today") return renderTodaySchedule(snapshot);
  if (state.activeScreen === "finder") {
    return renderAvailabilityFinder(snapshot, findFeaturedClient(snapshot, state.selectedClientId), state.recommendedSlots);
  }
  if (state.activeScreen === "client") return renderClientDetail(snapshot, state.selectedClientId);
  if (state.activeScreen === "requests") return renderRequestManagement(snapshot);
  return renderSettingsRules(snapshot.rules);
}

function wireEvents(): void {
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

  app.querySelector<HTMLButtonElement>("[data-save-rules]")?.addEventListener("click", async () => {
    if (!state.snapshot) return;
    await runAction(async () => {
      if (!state.snapshot) return;
      await instructorCommandApi.saveRules(state.snapshot.rules);
      state.toast = "Rules saved to the adapter.";
    });
  });
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
    state.error = formatApiError(error);
    render();
  }
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

function readFrontendEnv(name: string): string | undefined {
  const maybeImportMeta = import.meta as ImportMeta & { env?: Record<string, string | undefined> };
  return maybeImportMeta.env?.[name];
}
