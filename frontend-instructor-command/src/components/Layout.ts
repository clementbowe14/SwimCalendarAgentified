import type { CommandCenterSnapshot } from "../api/types.js";
import { summarizeRequests } from "../domain/screenModels.js";
import { escapeHtml } from "../lib/html.js";

export type ScreenId = "today" | "finder" | "client" | "requests" | "settings";

const navItems: Array<{ id: ScreenId; label: string; icon: string }> = [
  { id: "today", label: "Schedule", icon: "ph-calendar-check" },
  { id: "finder", label: "Find", icon: "ph-magnifying-glass" },
  { id: "client", label: "Clients", icon: "ph-users-three" },
  { id: "requests", label: "Requests", icon: "ph-tray" },
  { id: "settings", label: "Settings", icon: "ph-gear-six" },
];

export function renderShell(
  snapshot: CommandCenterSnapshot,
  activeScreen: ScreenId,
  body: string,
): string {
  const requestSummary = summarizeRequests(snapshot.requests);
  const schedulingReady = snapshot.instructor.setup_complete;
  const navButtons = renderNavButtons(activeScreen, schedulingReady, requestSummary.totalPending);

  return `
    <main class="command-shell">
      <aside class="sidebar" aria-label="Dayweave navigation">
        ${renderBrand(snapshot)}
        <nav class="section-nav">
          ${navButtons}
        </nav>
        <section class="operator-card">
          <p class="eyebrow">Provider</p>
          <h2>${escapeHtml(snapshot.instructor.name)}</h2>
          <span>${escapeHtml(snapshot.instructor.work_start_time)}-${escapeHtml(snapshot.instructor.work_end_time)}</span>
        </section>
      </aside>
      <section class="workspace">
        <header class="topbar">
          <div class="mobile-brandbar">
            ${renderBrand(snapshot)}
            <button class="mobile-request-action" data-screen="requests" type="button" aria-label="${requestSummary.totalPending} pending requests">
              <i class="ph ph-bell" aria-hidden="true"></i>
              ${requestSummary.totalPending > 0 ? `<span>${requestSummary.totalPending}</span>` : ""}
            </button>
          </div>
          <div class="topbar-copy">
            <p class="eyebrow">Your day</p>
            <h1>${screenTitle(activeScreen)}</h1>
          </div>
          <div class="topbar-actions">
            <button class="metric-pill request-pill" data-screen="requests" type="button">
              <i class="ph ph-tray" aria-hidden="true"></i>
              ${requestSummary.totalPending} pending
            </button>
            <button
              class="primary-compact${schedulingReady ? "" : " setup-gated"}"
              ${schedulingReady ? 'data-screen="finder"' : 'data-setup-required-action="find availability" title="Complete provider setup first"'}
              type="button"
            ><i class="ph ph-plus" aria-hidden="true"></i> New client</button>
          </div>
        </header>
        ${body}
      </section>
      <nav class="mobile-tabbar" aria-label="Dayweave navigation">
        ${navButtons}
      </nav>
    </main>
  `;
}

function renderBrand(snapshot: CommandCenterSnapshot): string {
  return `
    <div class="brand-lockup">
      <span class="brand-mark" aria-hidden="true"><img src="/assets/dayweave-mark.png" alt="" /></span>
      <div>
        <strong>Dayweave</strong>
        <span>${escapeHtml(snapshot.instructor.business_name)}</span>
      </div>
    </div>
  `;
}

function renderNavButtons(activeScreen: ScreenId, schedulingReady: boolean, pendingRequests: number): string {
  return navItems
    .map(
      (item) => `
        <button
          class="nav-item ${item.id === activeScreen ? "selected" : ""}${item.id === "finder" && !schedulingReady ? " setup-gated" : ""}"
          aria-label="${escapeHtml(item.label)}"
          ${item.id === activeScreen ? 'aria-current="page"' : ""}
          ${item.id === "finder" && !schedulingReady ? 'data-setup-required-action="find availability" title="Complete provider setup first"' : `data-screen="${item.id}"`}
          type="button"
        >
          <span class="nav-icon">
            <i class="ph ${item.icon}" aria-hidden="true"></i>
            ${item.id === "requests" && pendingRequests > 0 ? `<small>${pendingRequests}</small>` : ""}
          </span>
          <span>${escapeHtml(item.label)}</span>
        </button>
      `,
    )
    .join("");
}

function screenTitle(screen: ScreenId): string {
  if (screen === "today") return "Today schedule";
  if (screen === "finder") return "Availability finder";
  if (screen === "client") return "Client detail";
  if (screen === "requests") return "Request management";
  return "Settings";
}
