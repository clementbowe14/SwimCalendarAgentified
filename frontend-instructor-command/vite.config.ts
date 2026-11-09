import { defineConfig, loadEnv } from "vite";
import { resolveInstructorDataMode } from "./src/config/dataMode";

export default defineConfig(({ command, mode }) => {
  const environment = loadEnv(mode, process.cwd(), "VITE_");
  resolveInstructorDataMode(environment, command);

  return {
    server: {
      host: "127.0.0.1",
      port: 5174,
      strictPort: true,
    },
  };
});
