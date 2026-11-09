export const INSTRUCTOR_APP_NAME = "Instructor Command";
export const MOCK_API_ENV_KEY = "VITE_USE_MOCK_API";
export const INSTRUCTOR_API_URL_ENV_KEY = "VITE_INSTRUCTOR_API_URL";

export type InstructorDataModeEnvironment = Readonly<{
  VITE_USE_MOCK_API?: string;
  VITE_INSTRUCTOR_API_URL?: string;
}>;

export type InstructorDataModeCommand = "serve" | "build" | "test" | "runtime";

export type InstructorDataModeConfig =
  | Readonly<{ mode: "mock" }>
  | Readonly<{ mode: "http"; apiBaseUrl: string }>;

export class InstructorDataModeConfigurationError extends Error {
  constructor(message: string) {
    super(`${INSTRUCTOR_APP_NAME} configuration error: ${message}`);
    this.name = "InstructorDataModeConfigurationError";
  }
}

export function resolveInstructorDataMode(
  environment: InstructorDataModeEnvironment = {},
  command: InstructorDataModeCommand = "runtime",
): InstructorDataModeConfig {
  const mockFlag = environment.VITE_USE_MOCK_API;
  const apiBaseUrl = environment.VITE_INSTRUCTOR_API_URL?.trim() ?? "";

  if (mockFlag !== undefined && mockFlag !== "" && mockFlag !== "true" && mockFlag !== "false") {
    throw new InstructorDataModeConfigurationError(
      `${MOCK_API_ENV_KEY} must be the exact string true or false.`,
    );
  }

  if (mockFlag === "true") {
    if (command === "build") {
      throw new InstructorDataModeConfigurationError(
        `${MOCK_API_ENV_KEY}=true is not allowed for production builds.`,
      );
    }
    if (apiBaseUrl) {
      throw new InstructorDataModeConfigurationError(
        `${MOCK_API_ENV_KEY}=true cannot be combined with ${INSTRUCTOR_API_URL_ENV_KEY}.`,
      );
    }
    return Object.freeze({ mode: "mock" });
  }

  if (!apiBaseUrl) {
    throw new InstructorDataModeConfigurationError(
      `${INSTRUCTOR_API_URL_ENV_KEY} is required unless ${MOCK_API_ENV_KEY}=true.`,
    );
  }

  return Object.freeze({ mode: "http", apiBaseUrl });
}
