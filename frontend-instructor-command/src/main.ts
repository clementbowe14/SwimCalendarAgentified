import "./styles.css";
import {
  clearLegacyInstructorAuthState,
  createInstructorCommandApi,
  InstructorCommandApiError,
} from "./api/instructorApi.js";
import type {
  BookingRequestDTO,
  CalendarLinks,
  CoachEmailDelivery,
  CommandCenterSnapshot,
  InstructorAuthSession,
  ProviderConfiguration,
  ProviderConfigurationUpdate,
  SchedulingPolicy,
  TimeSlotDTO,
} from "./api/types.js";
import { resolveInstructorDataMode } from "./config/dataMode.js";
import { renderAvailabilityFinder } from "./components/AvailabilityFinder.js";
import {
  renderCalendarHandoffModal,
  type CalendarHandoffModalModel,
} from "./components/CalendarHandoffModal.js";
import { renderClientDetail } from "./components/ClientDetail.js";
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
import { renderTodaySchedule } from "./components/TodaySchedule.js";
import { buildFindSlotsRequest, findFeaturedClient } from "./domain/screenModels.js";
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
};

const state: AppState = {
  activeScreen: "today",
  selectedClientId: "client_maya",
  authStatus: instructorCommandApi.authMode === "live" ? "checking" : "signed_in",
  authSession: null,
  snapshot: null,
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
};

const instructorId = import.meta.env?.VITE_INSTRUCTOR_ID || "inst_riley_001";
const commandDate = import.meta.env?.VITE_INSTRUCTOR_COMMAND_DATE || "2026-08-27";
const instructorApiBaseUrl = instructorRuntimeConfig.mode === "http"
  ? instructorRuntimeConfig.apiBaseUrl
  : "";
let authGeneration = 0;
const availabilityRequestGate = new MonotonicRequestGate();
const policyRequestGate = new MonotonicRequestGate();

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
    state.provider = provider;
    state.providerDraft = providerDraftFromConfiguration(provider);
    const snapshot = await instructorCommandApi.getSnapshot(instructorId, commandDate);
    if (!isCurrentSignedInGeneration(generation)) return;
    applySnapshot(snapshot);
    if (!state.snapshot) return;
    if (!state.snapshot.clients.some((client) => client.id === state.selectedClientId)) {
      state.selectedClientId = state.snapshot.clients[0]?.id ?? "client_new";
    }
    if (!provider.setup_complete) {
      state.activeScreen = "settings";
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
  restoreDeliveryFocus();
  restorePolicyFocus();
}

function renderLockedScreen(): string {
  if (state.authStatus === "checking" || state.authStatus === "signing_in" || state.authStatus === "signing_out") {
    const isSignIn = state.authStatus === "signing_in";
    const isSignOut = state.authStatus === "signing_out";
    const heading = isSignIn ? "Signing you in…" : isSignOut ? "Signing out…" : "Checking your session…";
    const body = isSignIn
      ? "Securely opening your instructor command center."
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

function renderAuthStatusScreen(heading: string, body: string, action = ""): string {
  return `
    <main class="auth-shell">
      <section class="auth-panel auth-status-panel" aria-labelledby="auth-title" aria-live="polite">
        <div class="brand-lockup">
          <span class="brand-mark">SC</span>
          <div><strong>Swim Command</strong><span>Instructor access</span></div>
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
  if (state.activeScreen === "today") return renderTodaySchedule(snapshot);
  if (state.activeScreen === "finder") {
    return renderAvailabilityFinder(
      snapshot,
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
  return renderSettingsRules(policy, draft, instructorCommandApi.authMode === "live", {
    pending: state.policyPending,
    error: state.policyError,
    success: state.policySuccess,
    conflict: state.policyConflict,
    availability: state.availability,
  }, providerSettings);
}

function wireEvents(): void {
  app.querySelector<HTMLButtonElement>("[data-sign-out]")?.addEventListener("click", async () => {
    await signOut();
  });

  app.querySelectorAll<HTMLButtonElement>("[data-screen]").forEach((button) => {
    button.addEventListener("click", () => {
      state.activeScreen = button.dataset.screen as ScreenId;
      state.providerSetupPrompt = "";
      render();
    });
  });

  app.querySelectorAll<HTMLButtonElement>("[data-setup-required-action], [data-open-provider-settings]").forEach((button) => {
    button.addEventListener("click", () => {
      const action = button.dataset.setupRequiredAction;
      state.activeScreen = "settings";
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
        const snapshot = await instructorCommandApi.getSnapshot(instructorId, commandDate);
        if (!isCurrentSignedInGeneration(generation)) return;
        applySnapshot(snapshot);
        state.toast = "Alternatives requested.";
      });
    });
  });

  wireEmailDeliveryEvents();
  wireProviderSettingsEvents();
  wireSchedulingPolicyEvents();
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
    state.provider = saved;
    state.providerDraft = providerDraftFromConfiguration(saved);
    state.providerFieldErrors = {};
    state.providerError = "";
    state.providerSuccess = "Provider settings saved.";
    state.providerSetupPrompt = "";
    if (state.snapshot) {
      state.snapshot = {
        ...state.snapshot,
        instructor: projectProviderOntoInstructor(state.snapshot.instructor, saved),
      };
    }

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

function applySnapshot(snapshot: CommandCenterSnapshot): void {
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
      ? projectProviderOntoInstructor(snapshot.instructor, state.provider)
      : snapshot.instructor,
    email_deliveries: mergedDeliveries,
  };
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
  const request = buildFindSlotsRequest(state.snapshot.instructor.id, selectedClient, state.snapshot.date);
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
  try {
    const snapshot = await instructorCommandApi.getSnapshot(instructorId, commandDate);
    if (!isCurrentSignedInGeneration(generation)) return;
    applySnapshot(snapshot);
    await refreshSlots(generation);
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
  state.activeScreen = "settings";
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
  state.authSession = null;
  state.snapshot = null;
  state.recommendedSlots = [];
  state.availabilityContext = null;
  state.availability = { status: "idle", message: "" };
  state.calendarHandoff = null;
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
