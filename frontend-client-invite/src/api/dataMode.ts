export type ClientInviteDataMode =
  | Readonly<{ mode: "mock"; apiBaseUrl: null }>
  | Readonly<{ mode: "http"; apiBaseUrl: string }>;

export type ClientInviteDataModeEnvironment = Readonly<{
  VITE_USE_MOCK_API?: string;
  VITE_CLIENT_INVITE_API_URL?: string;
}>;

export type ClientInviteDataModeCommand = "serve" | "build" | "test";

const APP_NAME = "Client Invite";
const MOCK_FLAG = "VITE_USE_MOCK_API";
const API_URL = "VITE_CLIENT_INVITE_API_URL";

export function resolveClientInviteDataMode(
  environment: ClientInviteDataModeEnvironment,
  command: ClientInviteDataModeCommand,
): ClientInviteDataMode {
  const mockFlag = environment.VITE_USE_MOCK_API;
  const apiBaseUrl = environment.VITE_CLIENT_INVITE_API_URL?.trim() ?? "";

  if (mockFlag !== undefined && mockFlag !== "" && mockFlag !== "true" && mockFlag !== "false") {
    throw configurationError(`${MOCK_FLAG} must be exactly "true", "false", empty, or unset.`);
  }

  if (mockFlag === "true") {
    if (command === "build") {
      throw configurationError(`${MOCK_FLAG}=true is not allowed for production builds.`);
    }
    if (apiBaseUrl) {
      throw configurationError(`${MOCK_FLAG}=true cannot be combined with ${API_URL}.`);
    }
    return Object.freeze({ mode: "mock", apiBaseUrl: null });
  }

  if (!apiBaseUrl) {
    throw configurationError(
      `${API_URL} is required for HTTP mode. For local mock development, set ${MOCK_FLAG}=true.`,
    );
  }

  return Object.freeze({ mode: "http", apiBaseUrl });
}

function configurationError(message: string): Error {
  return new Error(`${APP_NAME} data-mode configuration error: ${message}`);
}
