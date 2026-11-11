import assert from "node:assert/strict";
import test from "node:test";
import {
  createInstructorCommandApi,
  HttpInstructorCommandApi,
  MockInstructorCommandApi,
} from "../src/api/instructorApi.js";
import {
  InstructorDataModeConfigurationError,
  resolveInstructorDataMode,
} from "../src/config/dataMode.js";

test("explicit true selects mock only when the API URL is absent", () => {
  const config = resolveInstructorDataMode({ VITE_USE_MOCK_API: "true" }, "serve");
  assert.deepEqual(config, { mode: "mock" });
  assert.equal(Object.isFrozen(config), true);
  assert.equal(createInstructorCommandApi({ VITE_USE_MOCK_API: "true" }) instanceof MockInstructorCommandApi, true);
  assert.equal(createInstructorCommandApi({
    VITE_USE_MOCK_API: "true",
    VITE_INSTRUCTOR_API_URL: "   ",
  }) instanceof MockInstructorCommandApi, true);

  assert.throws(
    () => resolveInstructorDataMode({
      VITE_USE_MOCK_API: "true",
      VITE_INSTRUCTOR_API_URL: "http://api.test/api",
    }, "serve"),
    (error: unknown) => configurationErrorNames(error, "VITE_USE_MOCK_API", "VITE_INSTRUCTOR_API_URL"),
  );
});

test("adapter classes are importable in Node without evaluating browser configuration", () => {
  assert.equal(typeof HttpInstructorCommandApi, "function");
  assert.equal(typeof MockInstructorCommandApi, "function");
});

test("production builds reject explicit mock mode", () => {
  assert.throws(
    () => resolveInstructorDataMode({ VITE_USE_MOCK_API: "true" }, "build"),
    (error: unknown) => configurationErrorNames(error, "Dayweave", "VITE_USE_MOCK_API"),
  );
});

for (const mockFlag of [undefined, "", "false"] as const) {
  test(`HTTP mode is the default for mock flag ${String(mockFlag)}`, () => {
    const environment = {
      ...(mockFlag === undefined ? {} : { VITE_USE_MOCK_API: mockFlag }),
      VITE_INSTRUCTOR_API_URL: "  /api  ",
    };
    const config = resolveInstructorDataMode(environment, "serve");
    assert.deepEqual(config, { mode: "http", apiBaseUrl: "/api" });
    assert.equal(createInstructorCommandApi(environment) instanceof HttpInstructorCommandApi, true);
  });
}

for (const mockFlag of [undefined, "", "false"] as const) {
  test(`HTTP mode fails closed without an API URL for mock flag ${String(mockFlag)}`, () => {
    const environment = {
      ...(mockFlag === undefined ? {} : { VITE_USE_MOCK_API: mockFlag }),
      VITE_INSTRUCTOR_API_URL: "   ",
    };
    assert.throws(
      () => createInstructorCommandApi(environment),
      (error: unknown) => configurationErrorNames(error, "Dayweave", "VITE_INSTRUCTOR_API_URL"),
    );
  });
}

for (const invalidFlag of ["TRUE", "1", "yes", " true "]) {
  test(`invalid mock flag ${JSON.stringify(invalidFlag)} is rejected exactly`, () => {
    assert.throws(
      () => resolveInstructorDataMode({
        VITE_USE_MOCK_API: invalidFlag,
        VITE_INSTRUCTOR_API_URL: "/api",
      }),
      (error: unknown) => configurationErrorNames(error, "Dayweave", "VITE_USE_MOCK_API"),
    );
  });
}

test("the factory passes a trimmed API URL to the HTTP adapter", async () => {
  const originalFetch = globalThis.fetch;
  let requestedUrl = "";
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    requestedUrl = input instanceof Request ? input.url : String(input);
    return new Response(JSON.stringify({
      authenticated: true,
      expires_at: "2026-08-31T20:00:00Z",
      coach: { instructor_id: "inst_001", display_name: "Coach Sarah" },
    }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;

  try {
    const api = createInstructorCommandApi({
      VITE_USE_MOCK_API: "false",
      VITE_INSTRUCTOR_API_URL: "  http://api.test/api/  ",
    });
    await api.signIn({ email: "coach@example.test", password: "secret" });
    assert.equal(requestedUrl, "http://api.test/api/auth/coach/sign-in");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

function configurationErrorNames(error: unknown, ...names: string[]): boolean {
  assert.equal(error instanceof InstructorDataModeConfigurationError, true);
  if (!(error instanceof InstructorDataModeConfigurationError)) return false;
  for (const name of names) assert.match(error.message, new RegExp(name));
  assert.doesNotMatch(error.message, /http:\/\/api\.test/);
  return true;
}
