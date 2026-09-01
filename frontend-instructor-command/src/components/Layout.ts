import type { CommandCenterSnapshot } from "../api/types.js";
import { escapeHtml } from "../lib/html.js";
import { summarizeRequests } from "../domain/screenModels.js";

export type ScreenId = "today" | "finder" | "client" | "requests" | "settings";

const navItems: Array<{ id: ScreenId; label: string }> = [
  { id: "today", label: "Today" },
  { id: "finder", label: "Find" },
  { id: "client", label: "Client" },
  { id: "requests", label: "Requests" },
  { id: "settings", label: "Rules" },
];

type ShellOptions = {
  showSignOut?: boolean;
};

export function renderShell(
  snapshot: CommandCenterSnapshot,
  activeScreen: ScreenId,
  body: string,
  options: ShellOptions = {},
): string {
  const requestSummary = summarizeRequests(snapshot.requests);

  return `
    <main class="command-shell">
      <aside class="sidebar" aria-label="Instructor command navigation">
        <div class="brand-lockup">
          <span class="brand-mark">SC</span>
          <div>
            <strong>Swim Command</strong>
            <span>${escapeHtml(snapshot.instructor.business_name)}</span>
          </div>
        </div>
        <nav class="section-nav">
          ${navItems
            .map(
              (item) => `
                <button class="${item.id === activeScreen ? "selected" : ""}" data-screen="${item.id}" type="button">
                  ${escapeHtml(item.label)}
                </button>
              `,
            )
            .join("")}
        </nav>
        <section class="operator-card">
          <p class="eyebrow">Instructor</p>
          <h2>${escapeHtml(snapshot.instructor.name)}</h2>
          <span>${escapeHtml(snapshot.instructor.work_start_time)}-${escapeHtml(snapshot.instructor.work_end_time)}</span>
        </section>
      </aside>
      <section class="workspace">
        <header class="topbar">
          <div>
            <p class="eyebrow">Command center</p>
            <h1>${screenTitle(activeScreen)}</h1>
          </div>
          <div class="topbar-actions">
            <span class="metric-pill">${requestSummary.totalPending} pending</span>
            <button class="primary-compact" data-screen="finder" type="button">New client</button>
            ${options.showSignOut ? `<button class="secondary-compact" data-sign-out type="button">Sign out</button>` : ""}
          </div>
        </header>
        ${body}
      </section>
    </main>
  `;
}

function screenTitle(screen: ScreenId): string {
  if (screen === "today") return "Today schedule";
  if (screen === "finder") return "Availability finder";
  if (screen === "client") return "Client detail";
  if (screen === "requests") return "Request management";
  return "Settings and rules";
}
