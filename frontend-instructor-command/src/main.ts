import "./styles.css";
import "./dayweave-theme.css";
import "./dayweave-schedule.css";
import "@phosphor-icons/web/regular";
import {
  clearLegacyInstructorAuthState,
  createInstructorCommandApi,
  InstructorCommandApiError,
} from "./api/instructorApi.js";
import type {
  BookingRequestDTO,
  BlockerMutation,
  CalendarLinks,
  CoachEmailDelivery,
  CommandCenterSnapshot,
  ProviderBlocker,
  InstructorAuthSession,
  ProviderConfiguration,
  ProviderConfigurationUpdate,
  SchedulingPolicy,
  TimeSlotDTO,
} from "./api/types.js";
import { resolveInstructorDataMode } from "./config/dataMode.js";
import { renderAvailabilityFinder } from "./components/AvailabilityFinder.js";
import {
  renderBlockerEditor,
  type BlockerEditorModel,
} from "./components/BlockerEditor.js";
import {
  renderCalendarHandoffModal,
  type CalendarHandoffModalModel,
} from "./components/CalendarHandoffModal.js";
import { renderClientDetail } from "./components/ClientDetail.js";
import {
  renderGoogleCalendarSettings,
  renderGoogleCalendarSettingsContent,
  type GoogleCalendarAction,
  type GoogleCalendarReturn,
  type GoogleCalendarSettingsState,
} from "./components/GoogleCalendarSettings.js";
import { renderShell, type ScreenId } from "./components/Layout.js";
import {
  emptyDeliveryCardState,
  renderRequestManagement,
  type DeliveryCardUiState,
} from "./components/RequestManagement.js";
import {
  providerDraftWithField,
  renderProviderSettings,
} from "./components/ProviderSettings.js";
import { renderSettingsRules } from "./components/SettingsRules.js";
import { agendaTimeZone, renderTodaySchedule, scheduleItems, type AgendaLoadState } from "./components/TodaySchedule.js";
import { buildFindSlotsRequest, findFeaturedClient, isScheduleBlocker } from "./domain/screenModels.js";
import { mergeEmailDeliveries } from "./domain/emailDelivery.js";
import {
  availabilityAfterPolicySave,
  availabilityRefreshFailed,
  compareSchedulingPolicyRevision,
  MonotonicRequestGate,
  schedulingPolicyDraftMatchesPolicy,
  schedulingPolicyDraftFromPolicy,
  schedulingPolicyDraftWithMinute,
  schedulingPolicyDraftWithToggle,
  schedulingPolicyUpdateFromDraft,
  stepSchedulingPolicyDraft,
  type AvailabilityUiState,
  type SchedulingPolicyDraft,
  type SchedulingPolicyMinuteField,
} from "./domain/schedulingPolicy.js";
import {
  firstProviderErrorField,
  normalizeProviderDraft,
  projectProviderOntoInstructor,
  providerDraftFromConfiguration,
  providerFieldErrorsFromApi,
  validateProviderDraft,
  type ProviderFieldErrors,
} from "./domain/providerSettings.js";
import { dateKeyInTimeZone, scheduleLocalFields, scheduleTimeZone, shiftDateKey } from "./lib/format.js";
import { escapeHtml } from "./lib/html.js";

const root = document.querySelector<HTMLDivElement>("#app");

if (!root) {
  throw new Error("App root was not found.");
}

const app = root;
const instructorRuntimeConfig = resolveInstructorDataMode(import.meta.env);
const instructorCommandApi = createInstructorCommandApi(import.meta.env);

exposeDevelopmentDataMode(instructorRuntimeConfig.mode);

type AppState = {
  activeScreen: ScreenId;
  selectedClientId: string;
  authStatus:
    | "checking"
    | "unavailable"
    | "locked"
    | "expired"
    | "signing_in"
    | "signed_in"
    | "signing_out"
    | "sign_out_failed";
  authSession: InstructorAuthSession | null;
  snapshot: CommandCenterSnapshot | null;
  scheduleDate: string | null;
  agendaToday: { date: string; observedLocalDate: string } | null;
  agenda: AgendaLoadState;
  emailDeliveries: CoachEmailDelivery[];
  deliveryCards: Record<string, DeliveryCardUiState>;
  deliveryRefreshPending: boolean;
  deliveryRefreshResult: string;
  deliveryRefreshError: string;
  deliveryFocus: { primary: string; fallback?: string } | null;
  recommendedSlots: TimeSlotDTO[];
  availabilityContext: BookingRequestDTO | null;
  availability: AvailabilityUiState;
  calendarHandoff: CalendarHandoffModalModel | null;
  blockerEditor: BlockerEditorModel | null;
  blockerReturnFocus: { kind: "add" | "edit"; id?: string } | null;
  blockerFocusPending: { kind: "add" | "edit"; id?: string } | null;
  toast: string;
  error: string;
  schedulingPolicy: SchedulingPolicy | null;
  schedulingPolicyDraft: SchedulingPolicyDraft | null;
  policyPending: boolean;
  policyError: string;
  policySuccess: string;
  policyConflict: boolean;
  policyFocus: string | null;
  provider: ProviderConfiguration | null;
  providerDraft: ProviderConfigurationUpdate | null;
  providerPending: boolean;
  providerError: string;
  providerFieldErrors: ProviderFieldErrors;
  providerSuccess: string;
  providerSetupPrompt: string;
  googleCalendar: GoogleCalendarSettingsState;
};

const initialGoogleCalendarSettings: GoogleCalendarSettingsState = {
  connection: null,
  pending: null,
  error: "",
  returnResult: null,
};

const googleCalendarActionErrors: Record<GoogleCalendarAction, string> = {
  status: "We couldn’t check your Google Calendar connection. Retry status.",
  authorize: "We couldn’t open Google Calendar authorization. Try again.",
  disconnect: "We couldn’t confirm the disconnect. Try disconnecting again.",
};

const state: AppState = {
  activeScreen: "today",
  selectedClientId: "client_maya",
  authStatus: instructorCommandApi.authMode === "live" ? "checking" : "signed_in",
  authSession: null,
  snapshot: null,
  scheduleDate: null,
  agendaToday: null,
  agenda: { status: "loading" },
  emailDeliveries: [],
  deliveryCards: {},
  deliveryRefreshPending: false,
  deliveryRefreshResult: "",
  deliveryRefreshError: "",
  deliveryFocus: null,
  recommendedSlots: [],
  availabilityContext: null,
  availability: { status: "idle", message: "" },
  calendarHandoff: null,
  blockerEditor: null,
  blockerReturnFocus: null,
  blockerFocusPending: null,
  toast: "",
  error: "",
  schedulingPolicy: null,
  schedulingPolicyDraft: null,
  policyPending: false,
  policyError: "",
  policySuccess: "",
  policyConflict: false,
  policyFocus: null,
  provider: null,
  providerDraft: null,
  providerPending: false,
  providerError: "",
  providerFieldErrors: {},
  providerSuccess: "",
  providerSetupPrompt: "",
  googleCalendar: { ...initialGoogleCalendarSettings },
};

const instructorId = import.meta.env?.VITE_INSTRUCTOR_ID || "inst_riley_001";
const configuredCommandDate = parseCommandDate(import.meta.env?.VITE_INSTRUCTOR_COMMAND_DATE)
  ?? (instructorCommandApi.authMode === "mock" ? "2026-08-27" : null);
const instructorApiBaseUrl = instructorRuntimeConfig.mode === "http"
  ? instructorRuntimeConfig.apiBaseUrl
  : "";
let authGeneration = 0;
const availabilityRequestGate = new MonotonicRequestGate();
const policyRequestGate = new MonotonicRequestGate();
const agendaRequestGate = new MonotonicRequestGate();
const scheduleSnapshotRequestGate = new MonotonicRequestGate();
const blockerEditRequestGate = new MonotonicRequestGate();

window.addEventListener("pageshow", (event) => {
  if (
    event.persisted
    && instructorCommandApi.authMode === "live"
    && window.history.state?.googleCalendarAuthorizationPending === true
  ) {
    void validateLiveSession();
  }
});

void boot();

function exposeDevelopmentDataMode(mode: "mock" | "http"): void {
  if (!import.meta.env.DEV) return;

  document.documentElement.dataset.dataMode = mode;
  document.querySelector<HTMLElement>("[data-mock-data-badge]")?.remove();
  if (mode !== "mock") return;

  const badge = document.createElement("div");
  badge.className = "mock-data-badge";
  badge.dataset.mockDataBadge = "";
  badge.setAttribute("role", "status");
  badge.textContent = "Mock data";
  document.body.append(badge);
}

async function boot(): Promise<void> {
  if (instructorCommandApi.authMode === "live") {
    clearLegacyInstructorAuthState();
    await validateLiveSession();
    return;
  }

  state.authStatus = "signed_in";
  await loadCommandCenter(authGeneration);
}

async function validateLiveSession(): Promise<void> {
  const generation = nextAuthGeneration();
  clearProtectedState();
  state.authStatus = "checking";
  state.error = "";
  state.toast = "";
  render();

  try {
    const session = await instructorCommandApi.validateSession();
    if (!isCurrentGeneration(generation)) return;
    state.authSession = session;
    state.authStatus = "signed_in";
    await loadCommandCenter(generation);
  } catch (error) {
    if (!isCurrentGeneration(generation)) return;
    clearProtectedState();
    if (isAuthenticationFailure(error)) {
      state.authStatus = "locked";
      state.error = "";
    } else {
      state.authStatus = "unavailable";
      state.error = "We could not verify your session. Check your connection and try again.";
    }
    render();
  }
}

async function loadCommandCenter(generation: number): Promise<void> {
  try {
    const provider = await instructorCommandApi.getProvider();
    if (!isCurrentSignedInGeneration(generation)) return;
    if (instructorCommandApi.authMode === "live" && provider.id !== state.authSession?.coach.instructor_id) {
      invalidateChangedProvider();
      return;
    }
    state.provider = provider;
    state.providerDraft = providerDraftFromConfiguration(provider);
    state.scheduleDate = initialCommandDate();
    const date = activeScheduleDate();
    const snapshot = await instructorCommandApi.getSnapshot(instructorId, date);
    if (!isCurrentSignedInGeneration(generation)) return;
    if (snapshot.date !== date) throw new Error("We couldn’t load the requested schedule date.");
    if (!applySnapshot(snapshot)) return;
    const calendarReturn = instructorCommandApi.authMode === "live" ? consumeGoogleCalendarReturn() : null;
    if (!state.snapshot) return;
    if (!state.snapshot.clients.some((client) => client.id === state.selectedClientId)) {
      state.selectedClientId = state.snapshot.clients[0]?.id ?? "client_new";
    }
    state.googleCalendar.returnResult = calendarReturn;
    if (calendarReturn || !provider.setup_complete || state.activeScreen === "settings") openSettings();
    void refreshAgenda(generation);
    if (!provider.setup_complete) {
      clearAvailability("idle");
    } else {
      await refreshSlots(generation);
    }
  } catch (error) {
    if (!isCurrentGeneration(generation)) return;
    if (!handleAuthFailure(error, undefined, generation) && !handleProviderSetupRequired(error)) {
      state.error = formatApiError(error);
    }
  }
  if (isCurrentGeneration(generation)) render();
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
    app.innerHTML = `<main class="loading-screen">Loading Dayweave${renderError()}</main>`;
    return;
  }

  const body = renderActiveScreen(state.snapshot);
  const focused = document.activeElement instanceof HTMLButtonElement ? document.activeElement : null;
  const modal = state.calendarHandoff
    ? renderCalendarHandoffModal(state.calendarHandoff, instructorApiBaseUrl, window.location.href)
    : "";
  const blockerModal = state.blockerEditor ? renderBlockerEditor(state.blockerEditor) : "";
  const hasModal = Boolean(state.calendarHandoff || state.blockerEditor);
  document.body.classList.toggle("calendar-modal-open", hasModal);
  app.innerHTML = `
    <div data-command-surface ${hasModal ? "inert" : ""}>
      ${renderShell(state.snapshot, state.activeScreen, `${renderError()}${body}`)}
      ${renderToast()}
    </div>
    ${modal}
    ${blockerModal}
  `;
  wireEvents();
  wireCalendarHandoffEvents();
  wireBlockerEditorEvents();
  restoreDeliveryFocus();
  restorePolicyFocus();
  restoreBlockerFocus();
  if (state.activeScreen === "today" && focused && !hasModal) {
    const { scheduleDate, scheduleStep, editBlocker } = focused.dataset;
    if (scheduleDate) focusScheduleControl(`[data-schedule-date="${scheduleDate}"]`);
    if (scheduleStep) focusScheduleControl(`[data-schedule-step="${scheduleStep}"]`);
    if (editBlocker) focusScheduleControl(`[data-edit-blocker="${CSS.escape(editBlocker)}"]`);
    if (focused.hasAttribute("data-add-blocker")) {
      const emptyAction = focused.closest(".empty-schedule-state")
        && app.querySelector<HTMLButtonElement>(".empty-schedule-state [data-add-blocker]");
      if (emptyAction) emptyAction.focus({ preventScroll: true });
      else focusScheduleControl(".schedule-add-button");
    }
    if (focused.matches('.nav-item[data-screen="today"]')) focusActiveScreenNavigation("today");
  }
}

function renderLockedScreen(): string {
  if (state.authStatus === "checking" || state.authStatus === "signing_in" || state.authStatus === "signing_out") {
    const isSignIn = state.authStatus === "signing_in";
    const isSignOut = state.authStatus === "signing_out";
    const heading = isSignIn ? "Signing you in…" : isSignOut ? "Signing out…" : "Checking your session…";
    const body = isSignIn
      ? "Securely opening your Dayweave workspace."
      : isSignOut
        ? "Revoking this browser session."
        : "Verifying this browser before any protected schedule data loads.";
    return renderAuthStatusScreen(heading, body);
  }

  if (state.authStatus === "unavailable") {
    return renderAuthStatusScreen(
      "Session check unavailable",
      "Your protected schedule remains hidden until the session can be verified.",
      `<button class="primary-action" data-retry-session type="button">Retry session check</button>`,
    );
  }

  if (state.authStatus === "sign_out_failed") {
    return renderAuthStatusScreen(
      "Sign-out could not be confirmed",
      "Your schedule is hidden, but the server may still recognize this browser. Retry to revoke the session.",
      `<button class="danger-action" data-retry-sign-out type="button">Retry sign out</button>`,
    );
  }

  const isExpired = state.authStatus === "expired";
  const heading = isExpired ? "Session expired" : "Dayweave locked";
  const body = isExpired
    ? "Sign in again to continue managing protected coach data."
    : "Sign in to open your Dayweave workspace.";

  return `
    <main class="auth-shell">
      <section class="auth-panel" aria-labelledby="auth-title">
        <div class="brand-lockup">
          <span class="brand-mark" aria-hidden="true"><img src="/assets/dayweave-mark.png" alt="" /></span>
          <div>
            <strong>Dayweave</strong>
            <span>Provider access</span>
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

function renderAuthStatusScreen(heading: string, body: string, action = ""): string {
  return `
    <main class="auth-shell">
      <section class="auth-panel auth-status-panel" aria-labelledby="auth-title" aria-live="polite">
        <div class="brand-lockup">
          <span class="brand-mark" aria-hidden="true"><img src="/assets/dayweave-mark.png" alt="" /></span>
          <div><strong>Dayweave</strong><span>Provider access</span></div>
        </div>
        <div class="auth-copy">
          <p class="eyebrow">Protected workspace</p>
          <h1 id="auth-title">${escapeHtml(heading)}</h1>
          <p>${escapeHtml(body)}</p>
        </div>
        ${renderError()}
        ${action}
      </section>
    </main>
  `;
}

function renderActiveScreen(snapshot: CommandCenterSnapshot): string {
  if (state.activeScreen === "today") {
    return instructorCommandApi.authMode === "live"
      ? renderTodaySchedule(snapshot, activeScheduleDate(), state.agenda)
      : renderTodaySchedule(snapshot);
  }
  if (state.activeScreen === "finder") {
    return renderAvailabilityFinder(
      { ...snapshot, date: activeScheduleDate() },
      findFeaturedClient(snapshot, state.selectedClientId),
      state.recommendedSlots,
      state.availability,
    );
  }
  if (state.activeScreen === "client") return renderClientDetail(snapshot, state.selectedClientId);
  if (state.activeScreen === "requests") {
    return renderRequestManagement(snapshot, {
      deliveries: state.emailDeliveries,
      cards: state.deliveryCards,
      refreshPending: state.deliveryRefreshPending,
      refreshResult: state.deliveryRefreshResult,
      refreshError: state.deliveryRefreshError,
    }, isProviderSetupComplete());
  }
  const providerSettings = state.provider && state.providerDraft
    ? renderProviderSettings(state.provider, state.providerDraft, {
        pending: state.providerPending,
        error: state.providerError,
        fieldErrors: state.providerFieldErrors,
        success: state.providerSuccess,
        setupPrompt: state.providerSetupPrompt,
      })
    : `<section class="provider-settings-loading" aria-live="polite">Loading provider settings…</section>`;
  const policy = state.schedulingPolicy ?? snapshot.scheduling_policy;
  const draft = state.schedulingPolicyDraft ?? schedulingPolicyDraftFromPolicy(policy);
  const isLive = instructorCommandApi.authMode === "live";
  return renderSettingsRules(policy, draft, isLive, {
    pending: state.policyPending,
    error: state.policyError,
    success: state.policySuccess,
    conflict: state.policyConflict,
    availability: state.availability,
  }, providerSettings, isLive ? renderGoogleCalendarSettings(state.googleCalendar) : "");
}

function wireEvents(): void {
  app.querySelector<HTMLButtonElement>("[data-sign-out]")?.addEventListener("click", async () => {
    await signOut();
  });

  app.querySelectorAll<HTMLButtonElement>("[data-screen]").forEach((button) => {
    button.addEventListener("click", () => {
      const nextScreen = button.dataset.screen as ScreenId;
      if (nextScreen !== state.activeScreen) blockerEditRequestGate.invalidate();
      if (nextScreen === "settings") openSettings();
      else state.activeScreen = nextScreen;
      state.providerSetupPrompt = "";
      if (nextScreen === "today") void refreshAgenda(authGeneration);
      render();
      focusActiveScreenNavigation(nextScreen);
    });
  });

  app.querySelectorAll<HTMLButtonElement>("[data-schedule-date]").forEach((button) => {
    button.addEventListener("click", () => {
      const date = parseCommandDate(button.dataset.scheduleDate);
      if (date) void changeScheduleDate(date);
    });
  });

  app.querySelectorAll<HTMLButtonElement>("[data-schedule-step]").forEach((button) => {
    button.addEventListener("click", () => {
      const step = button.dataset.scheduleStep;
      void changeScheduleDate(step === "today" ? null : shiftDateKey(activeScheduleDate(), step === "previous" ? -1 : 1));
    });
  });

  app.querySelector<HTMLButtonElement>("[data-retry-agenda]")?.addEventListener("click", () => {
    void refreshAgenda(authGeneration);
    void refreshAgendaSnapshot(authGeneration, activeScheduleDate());
    render();
    focusScheduleControl('[data-schedule-date][aria-current="date"]');
  });
  app.querySelectorAll<HTMLButtonElement>("[data-add-blocker]").forEach((button) => {
    button.addEventListener("click", openCreateBlockerEditor);
  });

  app.querySelectorAll<HTMLButtonElement>("[data-edit-blocker]").forEach((button) => {
    button.addEventListener("click", () => {
      openEditBlockerEditor(button.dataset.editBlocker ?? "");
    });
  });

  app.querySelectorAll<HTMLButtonElement>("[data-setup-required-action], [data-open-provider-settings]").forEach((button) => {
    button.addEventListener("click", () => {
      const action = button.dataset.setupRequiredAction;
      openSettings();
      state.providerSetupPrompt = action
        ? `Save provider settings before you ${action}.`
        : "Complete provider setup to schedule new lesson times.";
      render();
      app.querySelector<HTMLElement>("[data-setup-status]")?.focus({ preventScroll: true });
      app.querySelector<HTMLElement>("[data-provider-field='display_name']")?.focus();
    });
  });

  app.querySelector<HTMLSelectElement>("[data-client-picker]")?.addEventListener("change", async (event) => {
    const target = event.currentTarget as HTMLSelectElement;
    await runAction(async (generation) => {
      state.selectedClientId = target.value;
      await refreshSlots(generation);
    });
  });

  app.querySelector<HTMLButtonElement>("[data-refresh-slots]")?.addEventListener("click", async () => {
    await runAction(async (generation) => {
      const refreshed = await refreshSlots(generation);
      if (!refreshed || !isCurrentSignedInGeneration(generation)) return;
      state.toast = "Safe starts refreshed.";
    });
  });

  app.querySelectorAll<HTMLButtonElement>("[data-retry-availability]").forEach((button) => {
    button.addEventListener("click", async () => {
      await retryAvailability();
    });
  });

  app.querySelectorAll<HTMLButtonElement>("[data-slot-id]").forEach((button) => {
    button.addEventListener("click", async () => {
      await bookSlot(button.dataset.slotId ?? "", button.dataset.calendarFocus ?? "");
    });
  });

  app.querySelectorAll<HTMLButtonElement>("[data-confirm-request]").forEach((button) => {
    button.addEventListener("click", async () => {
      await runAction(async (generation) => {
        const confirmation = await instructorCommandApi.confirmRequest(
          button.dataset.confirmRequest ?? "",
          button.dataset.confirmSlotId,
        );
        if (!isCurrentSignedInGeneration(generation)) return;
        if (confirmation.email_delivery) patchEmailDeliveries([confirmation.email_delivery]);
        const appointmentId = confirmation.appointment_id ?? confirmation.appointment_ids[0] ?? null;
        await completePersistedAction(
          appointmentId,
          confirmation.calendar_links ?? null,
          button.dataset.calendarFocus ?? "",
          "requests",
          generation,
        );
      });
    });
  });

  app.querySelectorAll<HTMLButtonElement>("[data-alternatives-request]").forEach((button) => {
    button.addEventListener("click", async () => {
      await runAction(async (generation) => {
        await instructorCommandApi.requestAlternatives(button.dataset.alternativesRequest ?? "", []);
        if (!isCurrentSignedInGeneration(generation)) return;
        await refreshScheduleSnapshot(generation);
        if (!isCurrentSignedInGeneration(generation)) return;
        state.toast = "Alternatives requested.";
      });
    });
  });

  wireEmailDeliveryEvents();
  wireGoogleCalendarSettingsEvents();
  wireProviderSettingsEvents();
  wireSchedulingPolicyEvents();
}

function focusActiveScreenNavigation(screen: ScreenId): void {
  const controls = [...app.querySelectorAll<HTMLButtonElement>(`.nav-item[data-screen="${screen}"]`)]
    .filter((control) => control.getClientRects().length > 0);
  controls.at(-1)?.focus({ preventScroll: true });
}

function focusScheduleControl(selector: string): void {
  app.querySelector<HTMLButtonElement>(selector)?.focus({ preventScroll: true });
}

function renderScheduleUpdate(): void {
  if (state.activeScreen === "today" && !state.blockerEditor && !state.calendarHandoff) render();
}

async function changeScheduleDate(date: string | null): Promise<void> {
  if (date !== null && date === activeScheduleDate()) return;
  blockerEditRequestGate.invalidate();
  const generation = authGeneration;
  state.scheduleDate = date ?? (instructorCommandApi.authMode === "mock" ? providerLocalToday() : null);
  if (date === null) state.agendaToday = null;
  persistScheduleSelection();
  clearAvailability();
  state.error = "";
  void refreshAgenda(generation);
  render();
  try {
    if (await refreshScheduleSnapshot(generation)) renderScheduleUpdate();
  } catch (error) {
    if (handleAuthFailure(error, undefined, generation)) return;
    if (instructorCommandApi.authMode === "mock" && state.snapshot) {
      state.scheduleDate = state.snapshot.date;
      persistScheduleSelection();
      state.error = formatApiError(error);
      renderScheduleUpdate();
    }
  }
}

async function refreshScheduleSnapshot(
  generation: number,
  date = activeScheduleDate(),
): Promise<boolean> {
  const token = scheduleSnapshotRequestGate.next();
  const current = () => (
    isCurrentSignedInGeneration(generation)
    && scheduleSnapshotRequestGate.isCurrent(token)
    && activeScheduleDate() === date
  );
  try {
    const snapshot = await instructorCommandApi.getSnapshot(instructorId, date);
    if (!isCurrentSignedInGeneration(generation)) return false;
    if (!snapshotBelongsToSession(snapshot)) {
      invalidateChangedProvider();
      return false;
    }
    if (!current()) return false;
    if (snapshot.date !== date) throw new Error("We couldn’t load the requested schedule date.");
    return applySnapshot(snapshot);
  } catch (error) {
    if (!current()) return false;
    throw error;
  }
}

async function refreshAgenda(
  generation: number, persistedMessage?: string, retryMidnight = true,
): Promise<void> {
  if (
    instructorCommandApi.authMode !== "live"
    || !isCurrentSignedInGeneration(generation)
    || state.activeScreen !== "today"
    || !state.snapshot
  ) return;
  const instructor = state.authSession?.coach.instructor_id;
  if (!instructor || instructor !== state.snapshot.instructor.id) {
    invalidateChangedProvider();
    return;
  }

  const token = agendaRequestGate.next();
  const date = state.scheduleDate;
  const previousTimeZone = agendaTimeZone(state.agenda);
  const localDateAtRequest = providerLocalToday();
  const sameSession = () => (
    isCurrentSignedInGeneration(generation)
    && state.authSession?.coach.instructor_id === instructor
  );
  const current = () => (
    sameSession()
    && state.activeScreen === "today"
    && state.scheduleDate === date
    && agendaRequestGate.isCurrent(token)
  );
  state.agenda = { status: "loading", timezone: previousTimeZone };
  try {
    const agenda = await instructorCommandApi.getAgenda(instructor, date ?? undefined);
    if (!sameSession()) return;
    if (agenda.instructor_id !== instructor) {
      invalidateChangedProvider();
      return;
    }
    if (!current()) return;
    const localDate = providerLocalToday();
    const rolledOver = agenda.date !== agenda.today
      || (localDateAtRequest !== localDate && agenda.today !== localDate);
    if (date === null && retryMidnight && rolledOver) {
      state.agendaToday = null;
      void refreshAgenda(generation, persistedMessage, false);
      return;
    }
    if (
      agenda.date !== (date ?? agenda.today)
      || parseCommandDate(agenda.today) !== agenda.today
      || typeof agenda.timezone !== "string" || !agenda.timezone
      || !Array.isArray(agenda.items)
    ) {
      state.agenda = { status: "error", timezone: previousTimeZone };
    } else {
      state.agenda = { status: "loaded", response: agenda };
      state.agendaToday = { date: agenda.today, observedLocalDate: providerLocalToday() };
      const snapshotTimeZoneChanged = agenda.timezone !== state.snapshot?.instructor.timezone;
      if (snapshotTimeZoneChanged || (date === null && agenda.date !== state.snapshot?.date)) {
        if (date === null) persistScheduleSelection();
        clearAvailability();
        void refreshAgendaSnapshot(generation, agenda.date, persistedMessage);
      }
    }
  } catch (error) {
    if (!sameSession() || handleAuthFailure(error, persistedMessage, generation)) return;
    if (handleChangedProvider(error, generation)) return;
    if (!current()) return;
    if (date === null) {
      state.agendaToday = null;
      const fallbackDate = activeScheduleDate();
      if (state.snapshot?.date !== fallbackDate) {
        clearAvailability();
        void refreshAgendaSnapshot(generation, fallbackDate, persistedMessage);
      }
    }
    state.agenda = { status: "error", timezone: previousTimeZone };
  }
  renderScheduleUpdate();
}

async function refreshAgendaSnapshot(generation: number, date: string, persistedMessage?: string): Promise<void> {
  try {
    if (await refreshScheduleSnapshot(generation, date)) renderScheduleUpdate();
  } catch (error) {
    handleAuthFailure(error, persistedMessage, generation);
  }
}

function openCreateBlockerEditor(): void {
  if (!state.snapshot) return;
  blockerEditRequestGate.invalidate();
  const date = activeScheduleDate();
  state.blockerReturnFocus = { kind: "add" };
  const draft: BlockerMutation = {
    kind: "break",
    title: "Break",
    date,
    start_time: "12:00",
    end_time: "12:30",
    location_address: null,
  };
  state.blockerEditor = {
    mode: "create",
    instructorId: state.snapshot.instructor.id,
    timeZone: agendaTimeZone(state.agenda) ?? state.snapshot.instructor.timezone,
    blockerId: null,
    revision: null,
    crossesMidnight: false,
    idempotencyKey: newBlockerIdempotencyKey(date),
    draft: copyBlockerDraft(draft),
    initialDraft: copyBlockerDraft(draft),
    pending: false,
    error: "",
    deleteConfirm: false,
    discardConfirm: false,
  };
  render();
}

async function openEditBlockerEditor(blockerId: string): Promise<void> {
  const snapshot = state.snapshot;
  if (!snapshot) return;
  const generation = authGeneration;
  const token = blockerEditRequestGate.next();
  const agenda = instructorCommandApi.authMode === "live" ? state.agenda : undefined;
  const date = activeScheduleDate();
  const blocker = scheduleItems(snapshot, date, agenda).find((item) => item.id === blockerId);
  if (!blocker || !isScheduleBlocker(blocker)) return;
  if (!Number.isInteger(blocker.revision) || blocker.revision < 1) {
    state.error = "This schedule event is missing its revision. Reload before editing it.";
    render();
    return;
  }
  const current = () => (
    isCurrentSignedInGeneration(generation)
    && blockerEditRequestGate.isCurrent(token)
    && state.activeScreen === "today"
    && activeScheduleDate() === date
    && !state.blockerEditor
  );
  let saved: ProviderBlocker | undefined;
  // Agenda intervals may be clipped at midnight, so edit only the full saved event.
  if (agenda) {
    try {
      const blockers = await instructorCommandApi.getBlockers(date, snapshot.instructor.id);
      if (!current()) return;
      saved = blockers.find((item) => item.id === blockerId && item.revision === blocker.revision);
    } catch (error) {
      if (handleAuthFailure(error, undefined, generation) || handleChangedProvider(error, generation)) return;
      if (!current()) return;
    }
    if (!saved) {
      state.error = "The saved event couldn’t be loaded. Refresh Schedule before editing it.";
      render();
      return;
    }
  }
  const timeZone = saved?.timezone ?? snapshot.instructor.timezone;
  const start = saved
    ? { date: saved.local_date, time: saved.local_start_time }
    : scheduleLocalFields(blocker.start_time, scheduleTimeZone(timeZone));
  const end = saved
    ? { date: saved.end_time.slice(0, 10), time: saved.local_end_time }
    : scheduleLocalFields(blocker.end_time, scheduleTimeZone(timeZone));
  const draft: BlockerMutation = {
    kind: blocker.kind,
    title: blocker.title,
    date: start.date,
    start_time: start.time,
    end_time: end.time,
    location_address: blocker.location?.address ?? null,
  };
  state.blockerReturnFocus = { kind: "edit", id: blocker.id };
  state.blockerEditor = {
    mode: "edit",
    instructorId: snapshot.instructor.id,
    timeZone,
    blockerId: blocker.id,
    revision: blocker.revision,
    crossesMidnight: start.date !== end.date,
    idempotencyKey: "",
    draft: copyBlockerDraft(draft),
    initialDraft: copyBlockerDraft(draft),
    pending: false,
    error: "",
    deleteConfirm: false,
    discardConfirm: false,
  };
  render();
}

function wireBlockerEditorEvents(): void {
  const dialog = app.querySelector<HTMLElement>("[data-blocker-dialog]");
  const editor = state.blockerEditor;
  if (!dialog || !editor) return;

  app.querySelector<HTMLButtonElement>("[data-blocker-close]")?.addEventListener("click", closeBlockerEditor);
  app.querySelector<HTMLElement>("[data-blocker-backdrop]")?.addEventListener("click", (event) => {
    if (event.target === event.currentTarget && !editor.pending && !editor.deleteConfirm) {
      closeBlockerEditor();
    }
  });
  app.querySelectorAll<HTMLInputElement>('input[name="kind"]').forEach((control) => {
    control.addEventListener("change", () => {
      const form = app.querySelector<HTMLFormElement>("[data-blocker-form]");
      if (!form || !state.blockerEditor) return;
      state.blockerEditor.draft = blockerDraftFromForm(form);
      state.blockerEditor.error = "";
      render();
      app.querySelector<HTMLInputElement>(`input[name="kind"][value="${control.value}"]`)?.focus();
    });
  });
  app.querySelector<HTMLFormElement>("[data-blocker-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (event.currentTarget instanceof HTMLFormElement) {
      await saveBlockerEditor(event.currentTarget);
    }
  });
  app.querySelector<HTMLButtonElement>("[data-blocker-remove]")?.addEventListener("click", () => {
    if (!state.blockerEditor || state.blockerEditor.pending) return;
    const form = app.querySelector<HTMLFormElement>("[data-blocker-form]");
    if (form && !state.blockerEditor.crossesMidnight) state.blockerEditor.draft = blockerDraftFromForm(form);
    state.blockerEditor.deleteConfirm = true;
    state.blockerEditor.error = "";
    render();
  });
  app.querySelector<HTMLButtonElement>("[data-blocker-keep]")?.addEventListener("click", () => {
    if (!state.blockerEditor || state.blockerEditor.pending) return;
    state.blockerEditor.deleteConfirm = false;
    state.blockerEditor.error = "";
    render();
  });
  app.querySelector<HTMLButtonElement>("[data-blocker-discard-keep]")?.addEventListener("click", () => {
    if (!state.blockerEditor || state.blockerEditor.pending) return;
    state.blockerEditor.discardConfirm = false;
    state.blockerEditor.error = "";
    render();
  });
  app.querySelector<HTMLButtonElement>("[data-blocker-confirm-discard]")?.addEventListener("click", () => {
    if (!state.blockerEditor || state.blockerEditor.pending) return;
    dismissBlockerEditor();
  });
  app.querySelector<HTMLButtonElement>("[data-blocker-confirm-remove]")?.addEventListener("click", async () => {
    await removeBlockerEditorEvent();
  });
  dialog.addEventListener("keydown", (event) => trapBlockerDialogFocus(event, dialog));
  dialog.focus();
}

function closeBlockerEditor(): void {
  if (!state.blockerEditor || state.blockerEditor.pending) return;
  if (state.blockerEditor.deleteConfirm) {
    state.blockerEditor.deleteConfirm = false;
    state.blockerEditor.error = "";
    render();
    return;
  }
  if (state.blockerEditor.discardConfirm) {
    state.blockerEditor.discardConfirm = false;
    state.blockerEditor.error = "";
    render();
    return;
  }
  const form = app.querySelector<HTMLFormElement>("[data-blocker-form]");
  if (form && !state.blockerEditor.crossesMidnight) {
    state.blockerEditor.draft = blockerDraftFromForm(form);
  }
  if (!sameBlockerDraft(state.blockerEditor.draft, state.blockerEditor.initialDraft)) {
    state.blockerEditor.discardConfirm = true;
    state.blockerEditor.error = "";
    render();
    return;
  }
  dismissBlockerEditor();
}

function dismissBlockerEditor(): void {
  state.blockerEditor = null;
  state.blockerFocusPending = state.blockerReturnFocus;
  state.blockerReturnFocus = null;
  render();
}

function copyBlockerDraft(draft: BlockerMutation): BlockerMutation {
  return { ...draft };
}

function sameBlockerDraft(left: BlockerMutation, right: BlockerMutation): boolean {
  return left.kind === right.kind
    && left.title === right.title
    && left.date === right.date
    && left.start_time === right.start_time
    && left.end_time === right.end_time
    && left.location_address === right.location_address;
}

function blockerDraftFromForm(form: HTMLFormElement): BlockerMutation {
  const data = new FormData(form);
  const kindValue = String(data.get("kind") ?? "break");
  const kind = isBlockerKind(kindValue) ? kindValue : "break";
  const location = kind === "personal" ? String(data.get("location_address") ?? "").trim() : "";
  return {
    kind,
    title: String(data.get("title") ?? "").trim(),
    date: String(data.get("date") ?? ""),
    start_time: String(data.get("start_time") ?? ""),
    end_time: String(data.get("end_time") ?? ""),
    location_address: location || null,
  };
}

function blockerDraftError(draft: BlockerMutation): string {
  if (!draft.title) return "Enter a title for this schedule event.";
  if (!draft.date || !draft.start_time || !draft.end_time) return "Choose a date, start time, and end time.";
  if (draft.end_time <= draft.start_time) return "End time must be later than start time.";
  return "";
}

async function saveBlockerEditor(form: HTMLFormElement): Promise<void> {
  const editor = state.blockerEditor;
  if (!editor || editor.pending || editor.crossesMidnight) return;
  editor.draft = blockerDraftFromForm(form);
  editor.error = blockerDraftError(editor.draft);
  if (editor.error) {
    render();
    app.querySelector<HTMLElement>("[data-blocker-status]")?.focus();
    return;
  }
  if (editor.mode === "edit" && editor.revision === null) {
    editor.error = "This schedule event is missing its revision. Reload before saving it.";
    render();
    app.querySelector<HTMLElement>("[data-blocker-status]")?.focus();
    return;
  }

  const generation = authGeneration;
  editor.pending = true;
  editor.error = "";
  render();
  let persisted = false;
  try {
    const payload = { ...editor.draft, expected_timezone: editor.timeZone || undefined };
    const saved = editor.mode === "create"
      ? await instructorCommandApi.createBlocker(payload, editor.idempotencyKey, editor.instructorId)
      : await instructorCommandApi.updateBlocker(editor.blockerId ?? "", {
          ...payload,
          expected_revision: editor.revision!,
        }, editor.instructorId);
    persisted = true;
    if (!isCurrentSignedInGeneration(generation)) return;
    await refreshAfterBlockerMutation(generation, saved.local_date, saved.id, "saved");
    if (!isCurrentSignedInGeneration(generation)) return;
    state.blockerEditor = null;
    state.blockerReturnFocus = null;
    state.blockerFocusPending = { kind: "edit", id: saved.id };
    state.toast = editor.mode === "create" ? "Schedule event added." : "Schedule event updated.";
    render();
  } catch (error) {
    if (!isCurrentGeneration(generation)) return;
    if (handleAuthFailure(
      error,
      persisted
        ? "The schedule event was saved, but your session expired before the agenda refreshed. Sign in again; do not save it again."
        : undefined,
      generation,
    ) || handleChangedProvider(error, generation)) return;
    if (persisted) {
      state.blockerEditor = null;
      state.blockerReturnFocus = null;
      state.blockerFocusPending = { kind: "add" };
      clearAvailability("stale");
      state.error = "The schedule event was saved, but the agenda could not refresh. Reload to see it; do not save it again.";
    } else if (state.blockerEditor) {
      state.blockerEditor.pending = false;
      state.blockerEditor.error = formatApiError(error);
    }
    render();
    if (!persisted && state.blockerEditor) {
      app.querySelector<HTMLElement>("[data-blocker-status]")?.focus();
    }
  }
}

async function removeBlockerEditorEvent(): Promise<void> {
  const editor = state.blockerEditor;
  if (
    !editor
    || editor.mode !== "edit"
    || editor.pending
    || !editor.blockerId
    || editor.revision === null
  ) return;
  const generation = authGeneration;
  const date = activeScheduleDate();
  editor.pending = true;
  editor.error = "";
  render();
  let persisted = false;
  try {
    await instructorCommandApi.deleteBlocker(editor.blockerId, editor.revision, editor.instructorId);
    persisted = true;
    if (!isCurrentSignedInGeneration(generation)) return;
    await refreshAfterBlockerMutation(generation, date, editor.blockerId, "removed");
    if (!isCurrentSignedInGeneration(generation)) return;
    state.blockerEditor = null;
    state.blockerReturnFocus = null;
    state.blockerFocusPending = { kind: "add" };
    state.toast = "Schedule event removed.";
    render();
  } catch (error) {
    if (!isCurrentGeneration(generation)) return;
    if (handleAuthFailure(
      error,
      persisted
        ? "The schedule event was removed, but your session expired before the agenda refreshed. Sign in again; do not remove it again."
        : undefined,
      generation,
    ) || handleChangedProvider(error, generation)) return;
    if (persisted) {
      state.blockerEditor = null;
      state.blockerReturnFocus = null;
      state.blockerFocusPending = { kind: "add" };
      clearAvailability("stale");
      state.error = "The schedule event was removed, but the agenda could not refresh. Reload to verify it; do not remove it again.";
    } else if (state.blockerEditor) {
      state.blockerEditor.pending = false;
      state.blockerEditor.error = formatApiError(error);
    }
    render();
    if (!persisted && state.blockerEditor) {
      app.querySelector<HTMLElement>("[data-blocker-status]")?.focus();
    }
  }
}

async function refreshAfterBlockerMutation(
  generation: number,
  date: string,
  updatedId: string,
  action: "saved" | "removed",
): Promise<void> {
  if (!isCurrentSignedInGeneration(generation)) return;
  if (date !== activeScheduleDate()) state.scheduleDate = date;
  persistScheduleSelection();
  clearAvailability("stale");
  if (state.snapshot && instructorCommandApi.authMode === "live") {
    state.snapshot.schedule = state.snapshot.schedule.filter((item) => item.id !== updatedId);
  }
  await Promise.all([
    refreshAgenda(generation, `The schedule event was ${action}. Sign in again to see the updated agenda.`),
    refreshScheduleSnapshot(generation, date).catch((error) => {
      if (instructorCommandApi.authMode === "mock" || isAuthenticationFailure(error)) throw error;
    }),
  ]);
}

function trapBlockerDialogFocus(event: KeyboardEvent, dialog: HTMLElement): void {
  if (event.key === "Escape") {
    event.preventDefault();
    closeBlockerEditor();
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = [...dialog.querySelectorAll<HTMLElement>(
    'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
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
  } else if (event.shiftKey && (active === first || !dialog.contains(active))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

function restoreBlockerFocus(): void {
  const target = state.blockerFocusPending;
  if (!target || state.blockerEditor || state.calendarHandoff) return;
  state.blockerFocusPending = null;
  if (target.kind === "edit" && target.id) {
    const edit = [...app.querySelectorAll<HTMLButtonElement>("[data-edit-blocker]")]
      .find((button) => button.dataset.editBlocker === target.id);
    if (edit) {
      edit.focus();
      return;
    }
  }
  app.querySelector<HTMLButtonElement>("[data-add-blocker]")?.focus();
}

function isBlockerKind(value: string): value is "break" | "personal" | "unavailable" {
  return value === "break" || value === "personal" || value === "unavailable";
}

function newBlockerIdempotencyKey(date: string): string {
  const random = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `blocker:${date}:${random}`;
}

function wireSchedulingPolicyEvents(): void {
  const form = app.querySelector<HTMLFormElement>("[data-scheduling-policy-form]");
  if (!form || !state.schedulingPolicy || !state.schedulingPolicyDraft) return;

  form.querySelector<HTMLInputElement>("[data-policy-toggle]")?.addEventListener("change", (event) => {
    if (!state.schedulingPolicyDraft) return;
    const control = event.currentTarget as HTMLInputElement;
    state.schedulingPolicyDraft = schedulingPolicyDraftWithToggle(state.schedulingPolicyDraft, control.checked);
    clearPolicyFeedbackInPlace();
  });

  form.querySelectorAll<HTMLInputElement>("[data-policy-minute]").forEach((control) => {
    control.addEventListener("input", () => {
      if (!state.schedulingPolicyDraft) return;
      const field = policyMinuteField(control.dataset.policyMinute);
      if (!field) return;
      state.schedulingPolicyDraft = schedulingPolicyDraftWithMinute(
        state.schedulingPolicyDraft,
        field,
        control.value,
      );
      updatePolicyStepperAccessibleNames(control);
      clearPolicyFeedbackInPlace();
    });
  });

  form.querySelectorAll<HTMLButtonElement>("[data-policy-step]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!state.schedulingPolicyDraft) return;
      const field = policyMinuteField(button.dataset.policyStep);
      const delta = button.dataset.policyStepDelta === "-1" ? -1 : 1;
      if (!field) return;
      state.schedulingPolicyDraft = stepSchedulingPolicyDraft(state.schedulingPolicyDraft, field, delta);
      state.policyError = "";
      state.policySuccess = "";
      state.policyFocus = `${field}:${delta === -1 ? "decrease" : "increase"}`;
      render();
    });
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.policyPending || state.policyConflict) return;
    await saveSchedulingPolicy(form);
  });

  form.querySelector<HTMLButtonElement>("[data-reload-scheduling-policy]")?.addEventListener("click", async () => {
    await reloadSchedulingPolicy();
  });
}

async function saveSchedulingPolicy(form: HTMLFormElement): Promise<void> {
  if (!state.schedulingPolicy || !state.schedulingPolicyDraft) return;
  state.schedulingPolicyDraft = schedulingPolicyDraftFromForm(form, state.schedulingPolicyDraft);
  const result = schedulingPolicyUpdateFromDraft(state.schedulingPolicy, state.schedulingPolicyDraft);
  state.policyError = "";
  state.policySuccess = "";
  state.policyConflict = false;
  if (!result.ok) {
    state.policyError = result.error;
    state.policyFocus = "error";
    render();
    return;
  }

  const generation = authGeneration;
  const requestToken = policyRequestGate.next();
  state.policyPending = true;
  render();

  let saved: SchedulingPolicy | null = null;
  try {
    const response = await instructorCommandApi.saveSchedulingPolicy(result.payload);
    if (
      !isCurrentSignedInGeneration(generation)
      || !policyRequestGate.isCurrent(requestToken)
    ) return;
    saved = response;
    if (!replaceSchedulingPolicy(response)) {
      state.policyConflict = true;
      state.policyFocus = "conflict";
      return;
    }
    state.policySuccess = "Scheduling policy saved.";
    state.toast = "Scheduling policy saved.";
    void refreshAgenda(generation);
  } catch (error) {
    if (!isCurrentGeneration(generation) || !policyRequestGate.isCurrent(requestToken)) return;
    if (handleAuthFailure(error, undefined, generation)) return;
    if (
      error instanceof InstructorCommandApiError
      && error.status === 409
      && error.code === "SCHEDULING_POLICY_STALE"
    ) {
      state.policyConflict = true;
      state.policyFocus = "conflict";
      if (state.availabilityContext) {
        availabilityRequestGate.invalidate();
        state.recommendedSlots = [];
        state.availability = {
          status: "stale",
          message: "The saved policy changed elsewhere. Reload it and retry safe starts before booking.",
        };
      }
    } else {
      state.policyError = formatApiError(error);
      state.policyFocus = "error";
    }
  } finally {
    if (!isCurrentGeneration(generation) || !policyRequestGate.isCurrent(requestToken)) return;
    state.policyPending = false;
    render();
  }

  if (!saved || !state.availabilityContext || !isCurrentSignedInGeneration(generation)) return;
  const context = structuredClone(state.availabilityContext);
  state.recommendedSlots = [];
  state.availability = availabilityAfterPolicySave(true);
  render();
  await refreshAvailabilityContext(context, generation, true);
}

async function reloadSchedulingPolicy(): Promise<void> {
  if (state.policyPending) return;
  const generation = authGeneration;
  const requestToken = policyRequestGate.next();
  state.policyPending = true;
  state.policyError = "";
  state.policySuccess = "";
  render();

  let reloaded = false;
  try {
    const policy = await instructorCommandApi.getSchedulingPolicy();
    if (
      !isCurrentSignedInGeneration(generation)
      || !policyRequestGate.isCurrent(requestToken)
    ) return;
    if (!replaceSchedulingPolicy(policy)) {
      state.policyConflict = true;
      state.policyFocus = "conflict";
      return;
    }
    state.policyConflict = false;
    state.policySuccess = "Latest saved policy loaded.";
    reloaded = true;
  } catch (error) {
    if (!isCurrentGeneration(generation) || !policyRequestGate.isCurrent(requestToken)) return;
    if (!handleAuthFailure(error, undefined, generation)) {
      state.policyError = formatApiError(error);
      state.policyFocus = "error";
    }
  } finally {
    if (!isCurrentGeneration(generation) || !policyRequestGate.isCurrent(requestToken)) return;
    state.policyPending = false;
    render();
  }

  if (!reloaded || !state.availabilityContext || !isCurrentSignedInGeneration(generation)) return;
  const context = structuredClone(state.availabilityContext);
  state.recommendedSlots = [];
  state.availability = { status: "refreshing", message: "Rechecking safe starts with the saved policy…" };
  render();
  await refreshAvailabilityContext(context, generation, false);
}

function schedulingPolicyDraftFromForm(
  form: HTMLFormElement,
  current: SchedulingPolicyDraft,
): SchedulingPolicyDraft {
  let draft = schedulingPolicyDraftWithToggle(
    current,
    form.querySelector<HTMLInputElement>("[data-policy-toggle]")?.checked ?? false,
  );
  form.querySelectorAll<HTMLInputElement>("[data-policy-minute]").forEach((control) => {
    const field = policyMinuteField(control.dataset.policyMinute);
    if (field) draft = schedulingPolicyDraftWithMinute(draft, field, control.value);
  });
  return draft;
}

function policyMinuteField(value: string | undefined): SchedulingPolicyMinuteField | null {
  if (value === "post_break_recovery_mins") return value;
  return value?.startsWith("tier.") && value.endsWith(".buffer_mins")
    ? value as SchedulingPolicyMinuteField
    : null;
}

function clearPolicyFeedbackInPlace(): void {
  state.policyError = "";
  state.policySuccess = "";
  app.querySelector<HTMLElement>("[data-policy-error]")?.remove();
  const success = app.querySelector<HTMLElement>("[data-policy-save-status]");
  if (success) success.textContent = "";
}

function updatePolicyStepperAccessibleNames(control: HTMLInputElement): void {
  const row = control.closest<HTMLElement>("[data-policy-minute-row]");
  const label = row?.querySelector<HTMLElement>(".policy-minute-copy strong")?.textContent?.trim();
  if (!row || !label) return;
  const current = control.value.trim();
  row.querySelectorAll<HTMLButtonElement>("[data-policy-step]").forEach((button) => {
    const action = button.dataset.policyStepDelta === "-1" ? "Decrease" : "Increase";
    const valueDescription = current ? `${current} minutes` : "no value";
    button.setAttribute("aria-label", `${action} ${label}, currently ${valueDescription}`);
  });
}

function replaceSchedulingPolicy(policy: SchedulingPolicy): boolean {
  if (compareSchedulingPolicyRevision(state.schedulingPolicy, policy) === "older") return false;
  state.schedulingPolicy = policy;
  state.schedulingPolicyDraft = schedulingPolicyDraftFromPolicy(policy);
  state.policyConflict = false;
  if (state.snapshot) state.snapshot = { ...state.snapshot, scheduling_policy: policy };
  return true;
}

function restorePolicyFocus(): void {
  const focus = state.policyFocus;
  if (!focus || state.activeScreen !== "settings" || state.calendarHandoff) return;
  state.policyFocus = null;
  const target = focus === "error"
    ? app.querySelector<HTMLElement>("[data-policy-error]")
    : focus === "conflict"
      ? app.querySelector<HTMLElement>("[data-policy-conflict]")
      : [...app.querySelectorAll<HTMLElement>("[data-policy-focus]")]
        .find((element) => element.dataset.policyFocus === focus);
  target?.focus({ preventScroll: true });
}

async function retryAvailability(): Promise<void> {
  const generation = authGeneration;
  const context = state.availabilityContext;
  if (!context || state.availability.status === "refreshing") return;
  state.recommendedSlots = [];
  state.availability = { status: "refreshing", message: "Rechecking safe starts…" };
  render();
  await refreshAvailabilityContext(structuredClone(context), generation, false);
}

function openSettings(): void {
  const calendar = state.googleCalendar;
  const refreshStatus = state.activeScreen !== "settings" || (!calendar.connection && !calendar.error);
  state.activeScreen = "settings";
  if (refreshStatus) void runGoogleCalendarAction("status");
}

function consumeGoogleCalendarReturn(): GoogleCalendarReturn | null {
  const url = new URL(window.location.href);
  const results = url.pathname === "/"
    ? url.searchParams.getAll("calendar").filter(
        (value): value is "connected" | "failed" => value === "connected" || value === "failed",
      )
    : [];
  const result = results.length === 1 ? results[0] : null;
  const historyState = window.history.state;
  const resumeSettings = historyState?.googleCalendarAuthorizationPending === true;
  if (!result && !resumeSettings) return null;

  if (result) url.searchParams.delete("calendar", result);
  const updatedHistoryState = { ...historyState };
  delete updatedHistoryState.googleCalendarAuthorizationPending;
  window.history.replaceState(resumeSettings ? updatedHistoryState : historyState, "", url);
  return result ?? "back";
}

function openGoogleCalendarAuthorization(authorizationUrl: string): void {
  const historyState = window.history.state;
  // Returning with Back can reload this history entry.
  window.history.replaceState({ ...historyState, googleCalendarAuthorizationPending: true }, "");
  try {
    window.location.assign(authorizationUrl);
  } catch (error) {
    window.history.replaceState(historyState, "");
    throw error;
  }
}

function wireGoogleCalendarSettingsEvents(): void {
  const card = app.querySelector<HTMLElement>("[data-google-calendar-settings]");
  card?.addEventListener("click", (event) => {
    const button = event.target instanceof Element
      ? event.target.closest<HTMLButtonElement>("[data-google-calendar-action]")
      : null;
    const action = button?.dataset.googleCalendarAction;
    if (action === "status" || action === "authorize" || action === "disconnect") {
      void runGoogleCalendarAction(action);
    }
  });
}

function updateGoogleCalendarSettings(action: GoogleCalendarAction): void {
  const card = app.querySelector<HTMLElement>("[data-google-calendar-settings]");
  const content = card?.querySelector<HTMLElement>("[data-google-calendar-content]");
  if (!card || !content) return;

  const restoreFocus = card.contains(document.activeElement);
  content.innerHTML = renderGoogleCalendarSettingsContent(state.googleCalendar);
  if (restoreFocus) {
    const target = card.querySelector<HTMLButtonElement>(`[data-google-calendar-action="${action}"]:enabled`)
      ?? card.querySelector<HTMLButtonElement>("button:enabled")
      ?? card;
    target.focus({ preventScroll: true });
  }
}

async function runGoogleCalendarAction(action: GoogleCalendarAction): Promise<void> {
  const generation = authGeneration;
  const calendar = state.googleCalendar;
  const expectedInstructorId = state.authSession?.coach.instructor_id;
  if (
    instructorCommandApi.authMode !== "live"
    || !isCurrentSignedInGeneration(generation)
    || !expectedInstructorId
    || calendar.pending
  ) return;

  calendar.pending = action;
  calendar.error = "";
  if (action === "status") calendar.connection = null;
  updateGoogleCalendarSettings(action);

  try {
    if (action === "authorize") {
      const authorizationUrl = await instructorCommandApi.authorizeGoogleCalendar(expectedInstructorId);
      if (isCurrentSignedInGeneration(generation)) openGoogleCalendarAuthorization(authorizationUrl);
      return;
    }

    const connection = await (action === "status"
      ? instructorCommandApi.getGoogleCalendarConnection(expectedInstructorId)
      : instructorCommandApi.disconnectGoogleCalendar(expectedInstructorId));
    if (!isCurrentSignedInGeneration(generation)) return;

    calendar.connection = connection.state;
    if (action === "disconnect" || (calendar.returnResult === "connected" && connection.state === "connected")) {
      calendar.returnResult = null;
    }
    if (action === "disconnect" && connection.state === "disconnected") {
      clearAvailability();
      void refreshAgenda(generation);
      if (state.activeScreen === "finder") render();
    }
  } catch (error) {
    if (!isCurrentSignedInGeneration(generation) || handleAuthFailure(error, undefined, generation)) return;
    if (handleChangedProvider(error, generation)) return;
    calendar.error = googleCalendarActionErrors[action];
  }

  calendar.pending = null;
  updateGoogleCalendarSettings(action);
}

function wireProviderSettingsEvents(): void {
  const form = app.querySelector<HTMLFormElement>("[data-provider-form]");
  if (!form || !state.providerDraft) return;

  form.querySelectorAll<HTMLInputElement | HTMLSelectElement>("[data-provider-field]").forEach((control) => {
    const eventName = control instanceof HTMLInputElement && control.type === "text" ? "input" : "change";
    control.addEventListener(eventName, () => {
      if (!state.providerDraft) return;
      const field = control.dataset.providerField ?? "";
      const value = control instanceof HTMLInputElement && control.type === "checkbox"
        ? control.checked
        : control.value;
      state.providerDraft = providerDraftWithField(state.providerDraft, field, value);
      delete state.providerFieldErrors[field];
      if (control instanceof HTMLInputElement && control.type === "checkbox") {
        const weekdayPrefix = field.replace(/\.enabled$/, "");
        delete state.providerFieldErrors[`${weekdayPrefix}.start_time`];
        delete state.providerFieldErrors[`${weekdayPrefix}.end_time`];
      }
      state.providerError = "";
      state.providerSuccess = "";
      control.setAttribute("aria-invalid", "false");
      control.removeAttribute("aria-describedby");
      control.closest("label")?.querySelector(".provider-field-error")?.remove();
      const saveStatus = app.querySelector<HTMLElement>("[data-provider-save-status]");
      if (saveStatus) saveStatus.textContent = "";
      app.querySelector<HTMLElement>("[data-provider-error]")?.remove();

      if (control instanceof HTMLInputElement && control.type === "checkbox") {
        render();
        findProviderField(field)?.focus();
      }
    });
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.providerPending || !state.providerDraft) return;
    await saveProviderSettings(form);
  });
}

async function saveProviderSettings(form: HTMLFormElement): Promise<void> {
  if (!state.providerDraft) return;
  const payload = normalizeProviderDraft(providerDraftFromForm(form, state.providerDraft));
  const errors = validateProviderDraft(payload);
  state.providerDraft = payload;
  state.providerFieldErrors = errors;
  state.providerError = "";
  state.providerSuccess = "";

  if (Object.keys(errors).length) {
    render();
    focusProviderField(firstProviderErrorField(errors));
    return;
  }

  const generation = authGeneration;
  state.providerPending = true;
  render();

  try {
    const saved = await instructorCommandApi.saveProvider(payload);
    if (!isCurrentSignedInGeneration(generation)) return;
    const knownTimeZone = agendaTimeZone(state.agenda) ?? state.snapshot?.instructor.timezone ?? state.provider?.timezone;
    const timezoneChanged = saved.timezone !== knownTimeZone;
    if (timezoneChanged) {
      state.agendaToday = null;
      agendaRequestGate.invalidate();
      state.agenda = { status: "loading", timezone: saved.timezone };
    }
    state.provider = saved;
    state.providerDraft = providerDraftFromConfiguration(saved);
    state.providerFieldErrors = {};
    state.providerError = "";
    state.providerSuccess = "Provider settings saved.";
    state.providerSetupPrompt = "";
    if (state.snapshot) {
      state.snapshot = {
        ...state.snapshot,
        instructor: {
          ...projectProviderOntoInstructor(state.snapshot.instructor, saved),
          timezone: state.snapshot.instructor.timezone,
        },
        schedule: timezoneChanged ? [] : state.snapshot.schedule,
      };
    }
    if (timezoneChanged) void refreshAgendaSnapshot(generation, activeScheduleDate());
    void refreshAgenda(generation);

    if (saved.setup_complete) {
      try {
        await refreshSlots(generation);
      } catch (error) {
        if (!isCurrentGeneration(generation)) return;
        if (!handleAuthFailure(error, undefined, generation) && !handleProviderSetupRequired(error)) {
          state.error = "Settings were saved, but safe starts could not be refreshed. Try Find again.";
        }
      }
    } else {
      clearAvailability("idle");
    }
  } catch (error) {
    if (!isCurrentGeneration(generation)) return;
    if (!handleAuthFailure(error, undefined, generation)) {
      const fieldErrors = providerFieldErrorsFromApi(error, payload);
      state.providerFieldErrors = fieldErrors;
      state.providerError = Object.keys(fieldErrors).length ? "" : formatApiError(error);
    }
  } finally {
    if (!isCurrentGeneration(generation)) return;
    state.providerPending = false;
    render();
    if (Object.keys(state.providerFieldErrors).length) {
      focusProviderField(firstProviderErrorField(state.providerFieldErrors));
    } else {
      app.querySelector<HTMLElement>("[data-provider-error]")?.focus();
    }
  }
}

function providerDraftFromForm(
  form: HTMLFormElement,
  current: ProviderConfigurationUpdate,
): ProviderConfigurationUpdate {
  let draft = current;
  form.querySelectorAll<HTMLInputElement>("input[type='checkbox'][data-provider-field]").forEach((control) => {
    draft = providerDraftWithField(draft, control.dataset.providerField ?? "", control.checked);
  });
  form.querySelectorAll<HTMLInputElement | HTMLSelectElement>("[data-provider-field]:not([type='checkbox'])").forEach((control) => {
    if (!control.disabled) {
      draft = providerDraftWithField(draft, control.dataset.providerField ?? "", control.value);
    }
  });
  return draft;
}

function findProviderField(field: string | null): HTMLElement | null {
  if (!field) return null;
  return [...app.querySelectorAll<HTMLElement>("[data-provider-field]")]
    .find((element) => element.dataset.providerField === field)
    ?? (field === "working_hours" ? app.querySelector<HTMLElement>(".working-hours-card") : null);
}

function focusProviderField(field: string | null): void {
  const target = findProviderField(field);
  if (!target) return;
  if (!target.hasAttribute("tabindex") && !(target instanceof HTMLInputElement) && !(target instanceof HTMLSelectElement)) {
    target.tabIndex = -1;
  }
  target.focus();
}

function wireEmailDeliveryEvents(): void {
  app.querySelector<HTMLButtonElement>("[data-refresh-email-deliveries]")?.addEventListener("click", async () => {
    await refreshEmailDeliveries();
  });

  app.querySelectorAll<HTMLInputElement>("[data-email-delivery-draft]").forEach((input) => {
    input.addEventListener("input", () => {
      const deliveryId = input.dataset.emailDeliveryDraft ?? "";
      if (!deliveryId) return;
      deliveryCardState(deliveryId).draft = input.value;
    });
  });

  app.querySelectorAll<HTMLButtonElement>("[data-resend-email-delivery]").forEach((button) => {
    button.addEventListener("click", async () => {
      await resendEmailDelivery(button.dataset.resendEmailDelivery ?? "");
    });
  });

  app.querySelectorAll<HTMLFormElement>("[data-email-contact-form]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const deliveryId = form.dataset.emailContactForm ?? "";
      const email = String(new FormData(form).get("email") ?? "").trim();
      await updateEmailDeliveryContact(deliveryId, email);
    });
  });
}

async function refreshEmailDeliveries(): Promise<void> {
  if (state.deliveryRefreshPending) return;
  const generation = authGeneration;
  state.deliveryRefreshPending = true;
  state.deliveryRefreshResult = "";
  state.deliveryRefreshError = "";
  state.deliveryFocus = { primary: "refresh" };
  render();

  try {
    const deliveries = await instructorCommandApi.getEmailDeliveries();
    if (!isCurrentSignedInGeneration(generation)) return;
    patchEmailDeliveries(deliveries);
    state.deliveryRefreshResult = "Delivery status refreshed.";
  } catch (error) {
    if (!isCurrentGeneration(generation)) return;
    if (!handleAuthFailure(error, undefined, generation)) {
      state.deliveryRefreshError = "Delivery status could not be refreshed. Your last-known statuses are still shown.";
    }
  } finally {
    if (!isCurrentGeneration(generation)) return;
    state.deliveryRefreshPending = false;
    if (state.authStatus === "signed_in") state.deliveryFocus = { primary: "refresh" };
    render();
  }
}

async function resendEmailDelivery(deliveryId: string): Promise<void> {
  if (!deliveryId) return;
  const generation = authGeneration;
  const card = deliveryCardState(deliveryId);
  if (card.pending) return;
  card.pending = "resend";
  card.result = "";
  card.error = "";
  card.fieldError = "";
  state.deliveryFocus = { primary: `card:${deliveryId}` };
  render();

  try {
    const delivery = await instructorCommandApi.resendEmailDelivery(deliveryId);
    if (!isCurrentSignedInGeneration(generation)) return;
    patchEmailDeliveries([delivery]);
    card.pending = null;
    card.result = delivery.status === "queued"
      ? "Queued to send again"
      : "Delivery status updated. No new email was queued.";
    state.deliveryFocus = { primary: `resend:${deliveryId}`, fallback: `card:${deliveryId}` };
  } catch (error) {
    if (!isCurrentGeneration(generation)) return;
    card.pending = null;
    if (!handleAuthFailure(error, undefined, generation)) {
      card.error = safeDeliveryActionError(error);
      state.deliveryFocus = { primary: `resend:${deliveryId}`, fallback: `card:${deliveryId}` };
    }
  }
  if (isCurrentGeneration(generation)) render();
}

async function updateEmailDeliveryContact(deliveryId: string, email: string): Promise<void> {
  if (!deliveryId) return;
  const generation = authGeneration;
  const card = deliveryCardState(deliveryId);
  if (card.pending) return;
  card.draft = email;
  card.result = "";
  card.error = "";

  if (!isValidEmailAddress(email)) {
    card.fieldError = "Enter a valid email address.";
    state.deliveryFocus = { primary: `email:${deliveryId}`, fallback: `card:${deliveryId}` };
    render();
    return;
  }

  card.fieldError = "";
  card.pending = "contact";
  state.deliveryFocus = { primary: `card:${deliveryId}` };
  render();

  try {
    const delivery = await instructorCommandApi.updateEmailDeliveryContact(deliveryId, email);
    if (!isCurrentSignedInGeneration(generation)) return;
    patchEmailDeliveries([delivery]);
    card.pending = null;
    card.draft = "";
    card.result = "Email updated and queued";
    state.deliveryFocus = { primary: `contact:${deliveryId}`, fallback: `card:${deliveryId}` };
  } catch (error) {
    if (!isCurrentGeneration(generation)) return;
    card.pending = null;
    if (handleAuthFailure(error, undefined, generation)) {
      render();
      return;
    }
    if (error instanceof InstructorCommandApiError && error.status === 422) {
      card.fieldError = "Enter a valid email address.";
      state.deliveryFocus = { primary: `email:${deliveryId}`, fallback: `card:${deliveryId}` };
    } else {
      card.error = safeDeliveryActionError(error);
      state.deliveryFocus = { primary: `contact:${deliveryId}`, fallback: `card:${deliveryId}` };
    }
  }
  if (isCurrentGeneration(generation)) render();
}

function safeDeliveryActionError(error: unknown): string {
  if (error instanceof InstructorCommandApiError && error.status === 409) {
    return "This delivery changed before the update completed. Refresh delivery status before trying again.";
  }
  if (error instanceof InstructorCommandApiError && error.status === 404) {
    return "This delivery is no longer available. Refresh delivery status to see the latest activity.";
  }
  return "We could not confirm that change. Your last-known delivery status is still shown; refresh before trying again.";
}

function deliveryCardState(deliveryId: string): DeliveryCardUiState {
  state.deliveryCards[deliveryId] ??= emptyDeliveryCardState();
  return state.deliveryCards[deliveryId];
}

function isValidEmailAddress(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
}

function snapshotBelongsToSession(snapshot: CommandCenterSnapshot): boolean {
  return instructorCommandApi.authMode === "mock"
    || (snapshot.instructor.id === state.authSession?.coach.instructor_id
      && snapshot.instructor.id === state.provider?.id);
}

function applySnapshot(snapshot: CommandCenterSnapshot): boolean {
  if (!snapshotBelongsToSession(snapshot)) {
    invalidateChangedProvider();
    return false;
  }
  const currentPolicy = state.schedulingPolicy;
  const draft = state.schedulingPolicyDraft;
  const incomingPolicy = snapshot.scheduling_policy;
  const revisionOrder = compareSchedulingPolicyRevision(currentPolicy, incomingPolicy);
  if (!currentPolicy) {
    replaceSchedulingPolicy(incomingPolicy);
  } else if (revisionOrder === "newer") {
    availabilityRequestGate.invalidate();
    state.recommendedSlots = [];
    if (state.availabilityContext) {
      state.availability = {
        status: "stale",
        message: "The saved policy changed. Retry safe starts before booking.",
      };
    }
    if (!draft || schedulingPolicyDraftMatchesPolicy(currentPolicy, draft)) {
      replaceSchedulingPolicy(incomingPolicy);
    } else {
      state.schedulingPolicy = incomingPolicy;
      state.policyConflict = true;
      state.policyError = "";
      state.policySuccess = "";
      if (state.activeScreen === "settings") state.policyFocus = "conflict";
    }
  }
  const mergedDeliveries = mergeEmailDeliveries(state.emailDeliveries, snapshot.email_deliveries ?? []);
  state.emailDeliveries = mergedDeliveries;
  state.snapshot = {
    ...snapshot,
    scheduling_policy: state.schedulingPolicy ?? incomingPolicy,
    instructor: state.provider
      ? {
          ...projectProviderOntoInstructor(snapshot.instructor, state.provider),
          // Keep the server timezone that determined which rows belong to the snapshot date.
          timezone: snapshot.instructor.timezone,
        }
      : snapshot.instructor,
    email_deliveries: mergedDeliveries,
  };
  return true;
}

function initialCommandDate(): string | null {
  return parseCommandDate(new URLSearchParams(window.location.search).get("date")) ?? configuredCommandDate;
}

function parseCommandDate(value?: string | null): string | null {
  const date = value?.trim() ?? "";
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return null;
  const parsed = new Date(`${date}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === date ? date : null;
}

function persistScheduleSelection(): void {
  const url = new URL(window.location.href);
  const date = state.scheduleDate ?? (configuredCommandDate ? activeScheduleDate() : null);
  if (!date || date === configuredCommandDate) {
    url.searchParams.delete("date");
  } else {
    url.searchParams.set("date", date);
  }
  window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
}

function activeScheduleDate(): string {
  if (state.scheduleDate) return state.scheduleDate;
  const localDate = providerLocalToday();
  return state.agendaToday?.observedLocalDate === localDate ? state.agendaToday.date : localDate;
}

function providerLocalToday(): string {
  const timeZone = scheduleTimeZone(agendaTimeZone(state.agenda) ?? state.snapshot?.instructor.timezone ?? state.provider?.timezone);
  return dateKeyInTimeZone(new Date(), timeZone);
}

function patchEmailDeliveries(deliveries: CoachEmailDelivery[]): void {
  state.emailDeliveries = mergeEmailDeliveries(state.emailDeliveries, deliveries);
  if (state.snapshot) {
    state.snapshot = { ...state.snapshot, email_deliveries: state.emailDeliveries };
  }
}

function clearEmailDeliveryState(): void {
  state.emailDeliveries = [];
  state.deliveryCards = {};
  state.deliveryRefreshPending = false;
  state.deliveryRefreshResult = "";
  state.deliveryRefreshError = "";
  state.deliveryFocus = null;
}

function restoreDeliveryFocus(): void {
  const request = state.deliveryFocus;
  if (!request || state.activeScreen !== "requests" || state.calendarHandoff) return;
  const candidates = [...app.querySelectorAll<HTMLElement>("[data-delivery-focus]")];
  const target = candidates.find((element) => element.dataset.deliveryFocus === request.primary)
    ?? candidates.find((element) => element.dataset.deliveryFocus === request.fallback);
  state.deliveryFocus = null;
  target?.focus();
}

function wireAuthEvents(): void {
  app.querySelector<HTMLButtonElement>("[data-retry-session]")?.addEventListener("click", async () => {
    await validateLiveSession();
  });
  app.querySelector<HTMLButtonElement>("[data-retry-sign-out]")?.addEventListener("click", async () => {
    await signOut();
  });
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

  const generation = nextAuthGeneration();
  clearProtectedState();
  state.authStatus = "signing_in";
  render();

  try {
    const session = await instructorCommandApi.signIn({ email, password });
    if (!isCurrentGeneration(generation)) return;
    state.authSession = session;
    state.authStatus = "signed_in";
    state.toast = "Signed in.";
    await loadCommandCenter(generation);
  } catch (error) {
    if (!isCurrentGeneration(generation)) return;
    clearProtectedState();
    state.authStatus = "locked";
    state.error = formatApiError(error);
    render();
  }
}

async function signOut(): Promise<void> {
  const generation = nextAuthGeneration();
  clearProtectedState();
  state.error = "";
  state.toast = "";
  state.authStatus = "signing_out";
  render();
  try {
    await instructorCommandApi.signOut();
    if (!isCurrentGeneration(generation)) return;
    state.authStatus = "locked";
  } catch (error) {
    if (!isCurrentGeneration(generation)) return;
    state.authStatus = "sign_out_failed";
    state.error = `Sign-out could not be confirmed. ${formatApiError(error)}`;
  }
  render();
}

async function refreshSlots(generation: number): Promise<boolean> {
  if (!state.snapshot) return false;
  if (!isProviderSetupComplete()) {
    clearAvailability("idle");
    return false;
  }
  const selectedClient = findFeaturedClient(state.snapshot, state.selectedClientId);
  const request = buildFindSlotsRequest(state.snapshot.instructor.id, selectedClient, activeScheduleDate());
  state.availabilityContext = structuredClone(request);
  state.recommendedSlots = [];
  state.availability = { status: "refreshing", message: "Finding safe starts…" };
  render();
  return refreshAvailabilityContext(request, generation, false);
}

async function refreshAvailabilityContext(
  request: BookingRequestDTO,
  generation: number,
  afterPolicySave: boolean,
): Promise<boolean> {
  const requestToken = availabilityRequestGate.next();
  try {
    const slots = await instructorCommandApi.findAvailableSlots(request);
    if (
      !isCurrentSignedInGeneration(generation)
      || !availabilityRequestGate.isCurrent(requestToken)
    ) return false;
    state.availabilityContext = structuredClone(request);
    state.recommendedSlots = slots;
    state.availability = { status: "current", message: "" };
    render();
    return true;
  } catch (error) {
    if (!isCurrentGeneration(generation) || !availabilityRequestGate.isCurrent(requestToken)) return false;
    if (handleAuthFailure(error, undefined, generation) || handleProviderSetupRequired(error)) return false;
    state.recommendedSlots = [];
    state.availability = availabilityRefreshFailed(afterPolicySave);
    if (error instanceof InstructorCommandApiError && error.status === 403) {
      state.availability = { status: "stale", message: formatApiError(error) };
    }
    render();
    return false;
  }
}

function clearAvailability(status: "idle" | "stale" = "idle"): void {
  availabilityRequestGate.invalidate();
  state.recommendedSlots = [];
  state.availabilityContext = null;
  state.availability = {
    status,
    message: status === "stale" ? "Safe starts need to be refreshed before booking." : "",
  };
}

async function bookSlot(slotId: string, returnFocusKey: string): Promise<void> {
  await runAction(async (generation) => {
    if (!state.snapshot || state.availability.status !== "current") return;
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
    if (!isCurrentSignedInGeneration(generation)) return;
    await completePersistedAction(
      booking.appointment_id,
      booking.calendar_links ?? null,
      returnFocusKey,
      "finder",
      generation,
    );
  });
}

async function completePersistedAction(
  appointmentId: string | null,
  mutationLinks: CalendarLinks | null,
  returnFocusKey: string,
  returnFocusFallback: "finder" | "requests",
  generation: number,
): Promise<void> {
  if (!isCurrentSignedInGeneration(generation)) return;
  state.toast = "";
  let links = mutationLinks;

  if (!links && appointmentId) {
    try {
      links = await instructorCommandApi.getAppointmentCalendarLinks(appointmentId);
      if (!isCurrentSignedInGeneration(generation)) return;
    } catch (error) {
      if (!isCurrentGeneration(generation)) return;
      if (handleAuthFailure(error, persistedAuthMessage(), generation)) return;
      state.calendarHandoff = unavailableHandoff(appointmentId, returnFocusKey, returnFocusFallback);
      await refreshAfterPersistence(generation);
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
  await refreshAfterPersistence(generation);
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

async function refreshAfterPersistence(generation: number): Promise<void> {
  if (!isCurrentSignedInGeneration(generation)) return;
  void refreshAgenda(generation, persistedRefreshAuthMessage());
  try {
    if (await refreshScheduleSnapshot(generation)) await refreshSlots(generation);
  } catch (error) {
    if (!isCurrentGeneration(generation)) return;
    if (
      !handleAuthFailure(error, persistedRefreshAuthMessage(), generation)
      && !handleProviderSetupRequired(error)
    ) {
      state.error = "The appointment was saved, but the schedule could not be refreshed. Reload to see the latest schedule.";
    }
  }
}

async function runAction(action: (generation: number) => Promise<void>): Promise<void> {
  const generation = authGeneration;
  state.error = "";
  try {
    await action(generation);
    if (!isCurrentGeneration(generation)) return;
    render();
  } catch (error) {
    if (!isCurrentGeneration(generation)) return;
    if (!handleAuthFailure(error, undefined, generation) && !handleProviderSetupRequired(error)) {
      state.error = formatApiError(error);
    }
    render();
  }
}

function handleAuthFailure(error: unknown, persistedMessage?: string, generation = authGeneration): boolean {
  if (instructorCommandApi.authMode !== "live") return false;
  if (!isAuthenticationFailure(error)) return false;
  if (!isCurrentGeneration(generation)) return true;

  nextAuthGeneration();
  clearProtectedState();
  state.authStatus = "expired";
  state.error = persistedMessage ?? "Your session expired. Sign in again to continue.";
  render();
  return true;
}

function invalidateChangedProvider(): void {
  nextAuthGeneration();
  clearProtectedState();
  state.authStatus = "unavailable";
  state.error = "Your signed-in provider changed. Verify your session to continue.";
  render();
}

function handleChangedProvider(error: unknown, generation: number): boolean {
  if (!(error instanceof InstructorCommandApiError) || error.code !== "CALENDAR_CONNECTION_OWNER_CHANGED") return false;
  if (isCurrentGeneration(generation)) invalidateChangedProvider();
  return true;
}

function isAuthenticationFailure(error: unknown): boolean {
  return error instanceof InstructorCommandApiError && error.isAuthFailure;
}

function handleProviderSetupRequired(error: unknown): boolean {
  if (!(error instanceof InstructorCommandApiError) || error.code !== "PROVIDER_SETUP_REQUIRED") return false;
  const details = typeof error.details === "object" && error.details !== null
    ? error.details as Record<string, unknown>
    : {};
  const missingFields = Array.isArray(details.missing_fields)
    ? details.missing_fields.filter((field): field is string => typeof field === "string")
    : state.provider?.setup_missing_fields ?? [];
  if (state.provider) {
    state.provider = {
      ...state.provider,
      setup_complete: false,
      setup_missing_fields: missingFields,
    };
  }
  if (state.snapshot) {
    state.snapshot = {
      ...state.snapshot,
      instructor: {
        ...state.snapshot.instructor,
        setup_complete: false,
        setup_missing_fields: missingFields,
      },
    };
  }
  clearAvailability("idle");
  openSettings();
  state.providerSetupPrompt = "Complete provider setup before scheduling new lesson times.";
  return true;
}

function nextAuthGeneration(): number {
  authGeneration += 1;
  return authGeneration;
}

function isCurrentGeneration(generation: number): boolean {
  return generation === authGeneration;
}

function isCurrentSignedInGeneration(generation: number): boolean {
  return isCurrentGeneration(generation) && state.authStatus === "signed_in";
}

function clearProtectedState(): void {
  availabilityRequestGate.invalidate();
  policyRequestGate.invalidate();
  agendaRequestGate.invalidate();
  scheduleSnapshotRequestGate.invalidate();
  blockerEditRequestGate.invalidate();
  state.authSession = null;
  state.snapshot = null;
  state.scheduleDate = null;
  state.agendaToday = null;
  state.agenda = { status: "loading" };
  state.recommendedSlots = [];
  state.availabilityContext = null;
  state.availability = { status: "idle", message: "" };
  state.calendarHandoff = null;
  state.blockerEditor = null;
  state.blockerReturnFocus = null;
  state.blockerFocusPending = null;
  state.schedulingPolicy = null;
  state.schedulingPolicyDraft = null;
  state.policyPending = false;
  state.policyError = "";
  state.policySuccess = "";
  state.policyConflict = false;
  state.policyFocus = null;
  state.provider = null;
  state.providerDraft = null;
  state.providerPending = false;
  state.providerError = "";
  state.providerFieldErrors = {};
  state.providerSuccess = "";
  state.providerSetupPrompt = "";
  state.googleCalendar = { ...initialGoogleCalendarSettings };
  clearEmailDeliveryState();
}

function isProviderSetupComplete(): boolean {
  return state.provider?.setup_complete ?? state.snapshot?.instructor.setup_complete ?? false;
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
  const generation = authGeneration;
  const appointmentId = state.calendarHandoff?.appointmentId;
  if (!state.calendarHandoff || state.calendarHandoff.status !== "ready") return;
  state.calendarHandoff.downloadStatus = "downloading";
  state.calendarHandoff.inlineError = "";
  render();

  try {
    const blob = await instructorCommandApi.downloadCalendarIcs(calendarUrl);
    if (!isCurrentSignedInGeneration(generation)) return;
    if (!state.calendarHandoff || state.calendarHandoff.appointmentId !== appointmentId) return;
    triggerCalendarDownload(blob);
    state.calendarHandoff.downloadStatus = "downloaded";
  } catch (error) {
    if (!isCurrentGeneration(generation)) return;
    if (handleAuthFailure(error, persistedAuthMessage(), generation)) return;
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
  if (isCurrentGeneration(generation)) render();
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
