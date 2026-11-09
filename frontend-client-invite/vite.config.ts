import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";
import { resolveClientInviteDataMode } from "./src/api/dataMode";

export default defineConfig(({ command, mode }) => {
  const loadedEnvironment = loadEnv(mode, process.cwd(), "");
  resolveClientInviteDataMode(
    {
      VITE_USE_MOCK_API: process.env.VITE_USE_MOCK_API ?? loadedEnvironment.VITE_USE_MOCK_API,
      VITE_CLIENT_INVITE_API_URL:
        process.env.VITE_CLIENT_INVITE_API_URL ?? loadedEnvironment.VITE_CLIENT_INVITE_API_URL,
    },
    command,
  );

  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: 3001,
      fs: {
        allow: [".."],
      },
    },
  };
});
