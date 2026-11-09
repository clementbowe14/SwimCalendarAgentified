import assert from "node:assert/strict";
import test from "node:test";
import {
  createClientInviteApi,
  FetchClientInviteApi,
  MockClientInviteApi,
} from "../src/api/clientInviteApi.js";
import { resolveClientInviteDataMode } from "../src/api/dataMode.js";

test("exact true selects mock only for serve and test without an API URL", () => {
  assert.deepEqual(resolveClientInviteDataMode({ VITE_USE_MOCK_API: "true" }, "serve"), {
    mode: "mock",
    apiBaseUrl: null,
  });
  assert.equal(
    createClientInviteApi({ VITE_USE_MOCK_API: "true" }, "test") instanceof MockClientInviteApi,
    true,
  );

  assert.throws(
    () => resolveClientInviteDataMode({ VITE_USE_MOCK_API: "true" }, "build"),
    /VITE_USE_MOCK_API=true is not allowed for production builds/,
  );
});

test("mock mode rejects a simultaneous API URL", () => {
  const secretUrl = "https://private.example.test/api/public?token=do-not-print";
  assert.throws(
    () => resolveClientInviteDataMode({
      VITE_USE_MOCK_API: "true",
      VITE_CLIENT_INVITE_API_URL: secretUrl,
    }, "test"),
    (error: unknown) => {
      assert.equal(error instanceof Error, true);
      assert.match((error as Error).message, /VITE_USE_MOCK_API=true cannot be combined with VITE_CLIENT_INVITE_API_URL/);
      assert.doesNotMatch((error as Error).message, /do-not-print|private\.example/);
      return true;
    },
  );
});

test("missing, empty, and exact false flags select HTTP with a trimmed URL", async () => {
  for (const flag of [undefined, "", "false"]) {
    assert.deepEqual(resolveClientInviteDataMode({
      VITE_USE_MOCK_API: flag,
      VITE_CLIENT_INVITE_API_URL: "  https://api.example.test/api/public/  ",
    }, "test"), {
      mode: "http",
      apiBaseUrl: "https://api.example.test/api/public/",
    });
  }

  const api = createClientInviteApi({
    VITE_USE_MOCK_API: "false",
    VITE_CLIENT_INVITE_API_URL: "  https://api.example.test/api/public/  ",
  }, "test");
  assert.equal(api instanceof FetchClientInviteApi, true);

  const originalFetch = globalThis.fetch;
  let requestedUrl = "";
  globalThis.fetch = (async (input) => {
    requestedUrl = String(input);
    return new Response(JSON.stringify({}), { headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  try {
    await api.getInvite("invite-token");
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(requestedUrl, "https://api.example.test/api/public/client-invites/invite-token");
});

test("HTTP mode fails closed when its URL is absent or blank", () => {
  assert.throws(
    () => createClientInviteApi(),
    /VITE_CLIENT_INVITE_API_URL is required for HTTP mode/,
  );

  for (const flag of [undefined, "", "false"]) {
    for (const apiUrl of [undefined, "", "   "]) {
      assert.throws(
        () => resolveClientInviteDataMode({
          VITE_USE_MOCK_API: flag,
          VITE_CLIENT_INVITE_API_URL: apiUrl,
        }, "serve"),
        /VITE_CLIENT_INVITE_API_URL is required for HTTP mode/,
      );
    }
  }
});

test("coercible or case-folded mock flags are rejected", () => {
  for (const flag of ["1", "yes", "TRUE", " true ", "False"]) {
    assert.throws(
      () => resolveClientInviteDataMode({
        VITE_USE_MOCK_API: flag,
        VITE_CLIENT_INVITE_API_URL: "https://api.example.test/api/public",
      }, "test"),
      /VITE_USE_MOCK_API must be exactly/,
    );
  }
});

test("HTTP request failures propagate without changing adapters or returning fixtures", async () => {
  const api = createClientInviteApi({
    VITE_USE_MOCK_API: "false",
    VITE_CLIENT_INVITE_API_URL: "https://api.example.test/api/public",
  }, "test");
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => {
    throw new TypeError("network unavailable");
  }) as typeof fetch;
  try {
    await assert.rejects(() => api.getInvite("invite-token"), /network unavailable/);
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(api instanceof FetchClientInviteApi, true);
});
