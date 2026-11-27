import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:5174",
    trace: "on-first-retry",
  },
  webServer: {
    command: "npm run dev",
    env: {
      ...process.env,
      VITE_USE_MOCK_API: process.env.VITE_USE_MOCK_API ?? "",
      VITE_INSTRUCTOR_API_URL: process.env.VITE_INSTRUCTOR_API_URL ?? "",
      VITE_INSTRUCTOR_COMMAND_DATE: process.env.VITE_INSTRUCTOR_COMMAND_DATE ?? "2026-08-27",
    },
    url: "http://127.0.0.1:5174",
    reuseExistingServer: false,
  },
  projects: [
    {
      name: "desktop-chrome",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
