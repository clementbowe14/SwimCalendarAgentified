import assert from "node:assert/strict";
import test from "node:test";
import {
  HttpInstructorCommandApi,
  InstructorCommandApiError,
} from "../src/api/instructorApi.js";
import type { BookingRequestDTO } from "../src/api/types.js";

type FetchCall = {
  url: string;
  init?: RequestInit;
};

test("HTTP instructor adapter reads the coach schedule contract without raw instructor routing", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("http://api.test/api/");

  await withMockFetch(calls, async () =>
    jsonResponse({
      instructor: {
        id: "inst_001",
        name: "Coach Sarah",
        main_address: "100 Main St, Austin, TX",
        lat: 30.2672,
        lng: -97.7431,
        work_start_time: "08:00",
        work_end_time: "17:00",
      },
      date: "2026-08-20",
      appointments: [
        {
          id: "app_101",
          title: "Lesson with Alice",
          start_time: "2026-08-20T09:00:00",
          end_time: "2026-08-20T10:00:00",
          location_address: "450 North Blvd, Austin, TX",
          lat: 30.33,
          lng: -97.72,
          is_break: false,
        },
      ],
    }),
    async () => {
      const snapshot = await api.getSnapshot("frontend-should-not-route-by-this-id", "2026-08-20");

      assert.equal(calls[0].url, "http://api.test/api/coach/schedule?date=2026-08-20");
      assert.equal(snapshot.instructor.id, "inst_001");
      assert.equal(snapshot.clients.length, 1);
      assert.equal(snapshot.clients[0].name, "Alice");
      assert.equal(snapshot.requests.length, 0);
      assert.equal(snapshot.schedule[0].kind, "lesson");
      assert.equal(snapshot.rules.rounding_increment_mins, 15);
    },
  );
});

test("HTTP instructor adapter maps prototype availability requests onto the coach availability contract", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("http://api.test/api");
  const request: BookingRequestDTO = {
    instructor_id: "inst_001",
    client_id: "client_sofia",
    client_name: "Sofia Martinez",
    location: {
      address: "820 Lakeview Dr, Austin, TX",
      lat: 30.289,
      lng: -97.739,
      is_instructor_facility: false,
    },
    requested_date: "2026-08-29",
    duration_mins: 45,
  };

  await withMockFetch(calls, async () =>
    jsonResponse({
      status: "PRESENTING",
      client_name: "Sofia Martinez",
      requested_date: "2026-08-29",
      duration_mins: 45,
      location: {
        address: "820 Lakeview Dr, Austin, TX",
        lat: 30.289,
        lng: -97.739,
        is_instructor_facility: false,
      },
      available_slots: [
        {
          start_iso: "2026-08-29T12:30:00",
          end_iso: "2026-08-29T13:15:00",
          display: "Sat, Aug 29 at 12:30 PM",
          travel_time_mins: 12,
          buffer_mins: 10,
          distance_miles: 2.1,
        },
      ],
    }),
    async () => {
      const slots = await api.findAvailableSlots(request);
      const body = JSON.parse(String(calls[0].init?.body));

      assert.equal(calls[0].url, "http://api.test/api/coach/availability");
      assert.deepEqual(body, {
        client_name: "Sofia Martinez",
        client_address: "820 Lakeview Dr, Austin, TX",
        requested_date: "2026-08-29",
        duration_mins: 45,
        is_instructor_facility: false,
      });
      assert.equal(slots[0].start_time, "2026-08-29T12:30:00");
      assert.equal(slots[0].target_location.address, "820 Lakeview Dr, Austin, TX");
    },
  );
});

test("HTTP instructor adapter preserves stable backend error codes", async () => {
  const api = new HttpInstructorCommandApi("http://api.test/api");
  let caught: unknown;

  await withMockFetch([], async () =>
    jsonResponse(
      {
        error: {
          code: "SCHEDULING_CONFLICT",
          message: "The requested time is unavailable.",
        },
      },
      { status: 409 },
    ),
    async () => {
      try {
        await api.bookLesson({
          instructor_id: "inst_001",
          client_id: "client_sofia",
          client_name: "Sofia Martinez",
          start_time_iso: "2026-08-29T12:30:00",
          duration_mins: 45,
          location_address: "820 Lakeview Dr, Austin, TX",
          idempotency_key: "book-sofia-0829-1230",
        });
      } catch (error) {
        caught = error;
      }
    },
  );

  assert.equal(caught instanceof InstructorCommandApiError, true);
  assert.equal((caught as InstructorCommandApiError).status, 409);
  assert.equal((caught as InstructorCommandApiError).code, "SCHEDULING_CONFLICT");
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
