import assert from "node:assert/strict";
import test from "node:test";
import {
  ClientInviteApiError,
  FetchClientInviteApi,
} from "../src/api/clientInviteApi.js";
import type { AvailabilitySearchResponse, ClientIntake } from "../src/api/types.js";
import { buildSearchRequest } from "../src/features/inviteFlow/flowModel.js";

type FetchCall = {
  url: string;
  init?: RequestInit;
};

const intake: ClientIntake = {
  client_name: "Sofia Martinez",
  guardian_phone: "(512) 555-0194",
  guardian_email: "ana.martinez@example.com",
  notes: "Beginner swimmer.",
  location_type: "home",
  location: {
    address: "820 Lakeview Dr, Austin, TX",
    lat: 30.289,
    lng: -97.739,
    is_instructor_facility: false,
  },
};

test("client invite searches the server-selected date range with public weekday windows", async () => {
  const calls: FetchCall[] = [];
  const api = new FetchClientInviteApi("http://api.test/api/public/");
  const request = buildSearchRequest(intake, 45, [
    { id: "sat", days: ["saturday"], start_time: "10:00", end_time: "12:00" },
    { id: "weekdays", days: [], start_time: "16:00", end_time: "18:00" },
  ]);
  const response: AvailabilitySearchResponse = {
    start_date: "2026-08-29",
    end_date: "2026-09-11",
    duration_mins: 45,
    slots: [],
  };

  await withMockFetch(calls, async () => jsonResponse(response), async () => {
    assert.deepEqual(await api.findAvailableSlots("invite_sofia_0826", request), response);
  });

  assert.equal(calls[0].url, "http://api.test/api/public/client-invites/invite_sofia_0826/availability/search");
  assert.equal(calls[0].init?.method, "POST");
  assert.equal(calls[0].init?.credentials, "omit");
  assert.equal(new Headers(calls[0].init?.headers).get("authorization"), null);
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), {
    client_name: "Sofia Martinez",
    client_address: "820 Lakeview Dr, Austin, TX",
    is_instructor_facility: false,
    duration_mins: 45,
    windows: [{ days: ["saturday"], start_time: "10:00", end_time: "12:00" }],
  });
});

test("facility range searches use the configured provider location and can omit weekday filters", () => {
  const facilityIntake: ClientIntake = {
    ...intake,
    location_type: "facility",
    location: { ...intake.location, is_instructor_facility: true },
  };

  assert.deepEqual(buildSearchRequest(facilityIntake, 30, []), {
    client_name: "Sofia Martinez",
    is_instructor_facility: true,
    duration_mins: 30,
    windows: [],
  });
});

test("client invite intake and request creation use the public invite routes", async () => {
  const calls: FetchCall[] = [];
  const api = new FetchClientInviteApi("http://api.test/api/public");

  await withMockFetch(calls, async (_input, init) => {
    const body = JSON.parse(String(init?.body));
    if ("slot_id" in body) {
      return jsonResponse({
        id: "req_contract_pending",
        invite_token: "invite_sofia_0826",
        slot: {
          id: body.slot_id,
          start_iso: "2026-08-29T12:30:00",
          end_iso: "2026-08-29T13:15:00",
          display: "Sat, Aug 29 at 12:30 PM",
          location_address: "820 Lakeview Dr, Austin, TX",
          recurrence_available: true,
        },
        status: "requested",
        requested_at_iso: "2026-08-28T16:30:00",
        confirmation: null,
      });
    }

    return jsonResponse({
      invite_token: "invite_sofia_0826",
      instructor_name: "Coach Sarah",
      business_name: "Swim Command",
      expires_at_iso: "2026-09-27T16:20:44",
      intake: body,
      preferred_duration_mins: 45,
      allowed_durations_mins: [30, 45, 60],
    });
  }, async () => {
    await api.submitIntake("invite_sofia_0826", intake);
    await api.requestLessonSlot("invite_sofia_0826", "slot_contract_0829_1230", "invite-request:invite_sofia_0826:slot_contract_0829_1230");
  });

  assert.equal(calls[0].url, "http://api.test/api/public/client-invites/invite_sofia_0826/intake");
  assert.equal(calls[1].url, "http://api.test/api/public/client-invites/invite_sofia_0826/lesson-requests");
  assert.equal(calls[0].init?.credentials, "omit");
  assert.equal(calls[1].init?.credentials, "omit");
  assert.deepEqual(JSON.parse(String(calls[1].init?.body)), {
    slot_id: "slot_contract_0829_1230",
    idempotency_key: "invite-request:invite_sofia_0826:slot_contract_0829_1230",
  });
});

test("client invite adapter preserves stable invalid-token error codes", async () => {
  const api = new FetchClientInviteApi("http://api.test/api/public");
  let caught: unknown;

  await withMockFetch([], async () =>
    jsonResponse(
      {
        error: {
          code: "INVITE_NOT_FOUND",
          message: "The invite could not be found.",
        },
      },
      { status: 404 },
    ),
    async () => {
      try {
        await api.getInvite("unknown-token");
      } catch (error) {
        caught = error;
      }
    },
  );

  assert.equal(caught instanceof ClientInviteApiError, true);
  assert.equal((caught as ClientInviteApiError).status, 404);
  assert.equal((caught as ClientInviteApiError).code, "INVITE_NOT_FOUND");
});

async function withMockFetch(
  calls: FetchCall[],
  handler: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>,
  run: () => Promise<void>,
): Promise<void> {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input, init) => {
    calls.push({ url: requestUrl(input), init });
    return handler(input, init);
  }) as typeof fetch;

  try {
    await run();
  } finally {
    globalThis.fetch = originalFetch;
  }
}

function requestUrl(input: RequestInfo | URL): string {
  return input instanceof Request ? input.url : String(input);
}

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
    ...init,
  });
}
