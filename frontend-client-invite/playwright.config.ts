import { defineConfig, devices } from "@playwright/test";

const live = Boolean(process.env.VITE_CLIENT_INVITE_API_URL);
const firstDate = new Date(`${process.env.VITE_CLIENT_INVITE_DATE || new Date(Date.now() + 7 * 86400000).toISOString().slice(0, 10)}T12:00:00Z`);
if (!process.env.VITE_CLIENT_INVITE_DATE) firstDate.setUTCDate(firstDate.getUTCDate() + (6 - firstDate.getUTCDay() + 7) % 7);
const liveServer = (port: number, offset: number) => ({
  command: `npm run dev -- --port ${port} --strictPort`,
  url: `http://127.0.0.1:${port}`,
  env: { VITE_CLIENT_INVITE_DATE: new Date(firstDate.getTime() + offset * 86400000).toISOString().slice(0, 10) },
  reuseExistingServer: false,
});

export default defineConfig({
  testDir: "./tests",
  testMatch: live ? "**/real-backend-integration.spec.ts" : "**/*.spec.ts",
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:3001",
    trace: "on-first-retry",
  },
  webServer: live ? [liveServer(4173, 0), liveServer(5173, 35)] : [{
    command: "npm run dev",
    url: "http://127.0.0.1:3001",
    reuseExistingServer: false,
  }, {
    command: "npm run dev -- --port 3003 --strictPort",
    url: "http://127.0.0.1:3003",
    env: { VITE_CLIENT_INVITE_API_URL: "http://127.0.0.1:3109/api/public" },
    reuseExistingServer: false,
  }],
  projects: [
    {
      name: "mobile-chrome",
      use: { ...devices["Pixel 7"], ...(live ? { baseURL: "http://127.0.0.1:4173" } : {}) },
    },
    {
      name: "desktop-chrome",
      use: { ...devices["Desktop Chrome"], ...(live ? { baseURL: "http://127.0.0.1:5173" } : {}) },
    },
  ],
});
