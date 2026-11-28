import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { createClientInviteApi } from "./api/clientInviteApi";
import { resolveClientInviteDataMode, type ClientInviteDataMode } from "./api/dataMode";
import { inviteEnvelope } from "./data/mockInvite";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("Client Invite app root was not found.");

const environment = import.meta.env;
const dataMode = resolveClientInviteDataMode(environment, "serve");
const api = createClientInviteApi(environment, "serve");
const inviteToken = readInviteToken(dataMode);

if (import.meta.env.DEV) {
  document.documentElement.dataset.dataMode = dataMode.mode;
} else {
  delete document.documentElement.dataset.dataMode;
}

createRoot(root).render(
  <StrictMode>
    <App
      api={api}
      dataMode={dataMode.mode}
      inviteToken={inviteToken}
      showMockBadge={import.meta.env.DEV && dataMode.mode === "mock"}
    />
  </StrictMode>,
);

function readInviteToken(dataMode: ClientInviteDataMode): string | null {
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get("invite_token")?.trim() || params.get("invite")?.trim();
  if (fromUrl) return fromUrl;

  const configured = environment.VITE_CLIENT_INVITE_TOKEN?.trim();
  if (configured) return configured;

  return dataMode.mode === "mock" ? inviteEnvelope.invite_token : null;
}

