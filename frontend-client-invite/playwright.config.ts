import { defineConfig, devices, type PlaywrightTestConfig } from "@playwright/test";
import { resolveClientInviteDataMode } from "./src/api/dataMode";

type ClientInviteE2EMode = "mock" | "intercepted" | "live";

const e2eMode = readE2EMode(process.env.CLIENT_INVITE_E2E_MODE);
const firstDate = liveFirstDate();
const interceptedApiUrl = "http://127.0.0.1:3109/api/public";
const liveApiUrl = process.env.VITE_CLIENT_INVITE_API_URL?.trim() ?? "";

if (e2eMode === "live" && !liveApiUrl) {
  throw new Error(
    "Client Invite live Playwright mode requires VITE_CLIENT_INVITE_API_URL and cannot run as a skipped suite.",
  );
}

const serverEnvironment = e2eMode === "mock"
  ? { VITE_USE_MOCK_API: "true", VITE_CLIENT_INVITE_API_URL: "" }
  : {
      VITE_USE_MOCK_API: "false",
      VITE_CLIENT_INVITE_API_URL: e2eMode === "live" ? liveApiUrl : interceptedApiUrl,
    };

resolveClientInviteDataMode(serverEnvironment, "test");

const modeConfig: Record<ClientInviteE2EMode, Pick<PlaywrightTestConfig, "testMatch" | "webServer" | "projects">> = {
  mock: {
    testMatch: "**/client-invite-flow.spec.ts",
    webServer: {
      command: "npm run dev",
      url: "http://127.0.0.1:3001",
      env: {
        ...serverEnvironment,
        VITE_CLIENT_INVITE_DATE: "2026-08-29",
        VITE_CLIENT_INVITE_TOKEN: "",
      },
      reuseExistingServer: false,
    },
    projects: browserProjects("http://127.0.0.1:3001"),
  },
  intercepted: {
    testMatch: ["**/calendar-handoff.spec.ts", "**/http-bootstrap.spec.ts"],
    webServer: {
      command: "npm run dev -- --port 3003 --strictPort",
      url: "http://127.0.0.1:3003",
      env: {
        ...serverEnvironment,
        VITE_CLIENT_INVITE_DATE: "2026-08-29",
        VITE_CLIENT_INVITE_TOKEN: "",
      },
      reuseExistingServer: false,
    },
    projects: browserProjects("http://127.0.0.1:3003"),
  },
  live: {
    testMatch: "**/real-backend-integration.spec.ts",
    webServer: [liveServer(4173, 0), liveServer(5173, 35)],
    projects: [
      {
        name: "mobile-chrome",
        use: { ...devices["Pixel 7"], baseURL: "http://127.0.0.1:4173" },
      },
      {
        name: "desktop-chrome",
        use: { ...devices["Desktop Chrome"], baseURL: "http://127.0.0.1:5173" },
      },
    ],
  },
};

export default defineConfig({
  testDir: "./tests",
  reporter: "list",
  forbidOnly: Boolean(process.env.CI),
  use: {
    trace: "on-first-retry",
  },
  ...modeConfig[e2eMode],
});

function readE2EMode(value: string | undefined): ClientInviteE2EMode {
  if (value === "mock" || value === "intercepted" || value === "live") return value;
  throw new Error(
    "CLIENT_INVITE_E2E_MODE must be exactly mock, intercepted, or live. Use a package test:e2e script.",
  );
}

function browserProjects(baseURL: string): PlaywrightTestConfig["projects"] {
  return [
    { name: "mobile-chrome", use: { ...devices["Pixel 7"], baseURL } },
    { name: "desktop-chrome", use: { ...devices["Desktop Chrome"], baseURL } },
  ];
}

function liveFirstDate(): Date {
  const configured = process.env.VITE_CLIENT_INVITE_DATE;
  const date = new Date(`${configured || new Date(Date.now() + 7 * 86400000).toISOString().slice(0, 10)}T12:00:00Z`);
  if (!configured) date.setUTCDate(date.getUTCDate() + (6 - date.getUTCDay() + 7) % 7);
  return date;
}

function liveServer(port: number, offset: number) {
  return {
    command: `npm run dev -- --port ${port} --strictPort`,
    url: `http://127.0.0.1:${port}`,
    env: {
      VITE_USE_MOCK_API: "false",
      VITE_CLIENT_INVITE_API_URL: liveApiUrl,
      VITE_CLIENT_INVITE_DATE: new Date(firstDate.getTime() + offset * 86400000).toISOString().slice(0, 10),
      VITE_CLIENT_INVITE_TOKEN: process.env.VITE_CLIENT_INVITE_TOKEN ?? "",
    },
    reuseExistingServer: false,
  };
}
