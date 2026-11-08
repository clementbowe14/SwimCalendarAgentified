import assert from "node:assert/strict";
import test from "node:test";
import {
  createMemoryInstructorAuthStore,
  HttpInstructorCommandApi,
  InstructorCommandApiError,
} from "../src/api/instructorApi.js";
import type { BookingRequestDTO, CommandCenterSnapshot, InstructorAuthSession, SchedulerRules } from "../src/api/types.js";

type FetchCall = {
  url: string;
  init?: RequestInit;
};

test("HTTP instructor adapter signs in through the MVP auth endpoint and persists the bearer session", async () => {
  const calls: FetchCall[] = [];
  const store = createMemoryInstructorAuthStore();
  const api = new HttpInstructorCommandApi("http://api.test/api", store);

  await withMockFetch(calls, async () => jsonResponse(authSessionContract()), async () => {
    const session = await api.signIn({ email: "coach@example.test", password: "dev-secret" });
    const body = JSON.parse(String(calls[0].init?.body));

    assert.equal(calls[0].url, "http://api.test/api/auth/coach/sign-in");
    assert.equal(calls[0].init?.method, "POST");
    assert.deepEqual(body, { email: "coach@example.test", password: "dev-secret" });
    assert.equal(session.access_token, "signed.jwt");
    assert.equal(store.get()?.access_token, "signed.jwt");
  });
});

test("HTTP instructor adapter sends bearer auth with protected coach requests", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("http://api.test/api", createMemoryInstructorAuthStore(authSessionContract()));

  await withMockFetch(calls, async () => jsonResponse(snapshotContract()), async () => {
    await api.getSnapshot("frontend-should-not-route-by-this-id", "2026-08-20");

    assert.equal(calls[0].url, "http://api.test/api/coach/command-center/snapshot?date=2026-08-20");
    assert.equal(headerValue(calls[0].init?.headers, "authorization"), "Bearer signed.jwt");
  });
});

test("HTTP instructor adapter validates stored bearer sessions with the backend", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("http://api.test/api", createMemoryInstructorAuthStore(authSessionContract()));

  await withMockFetch(calls, async () =>
    jsonResponse({
      authenticated: true,
      coach: {
        instructor_id: "inst_001",
        display_name: "Coach Sarah",
      },
    }),
    async () => {
      const coach = await api.validateSession();

      assert.equal(calls[0].url, "http://api.test/api/auth/coach/session");
      assert.equal(calls[0].init?.method, "GET");
      assert.equal(headerValue(calls[0].init?.headers, "authorization"), "Bearer signed.jwt");
      assert.deepEqual(coach, {
        instructor_id: "inst_001",
        display_name: "Coach Sarah",
      });
    },
  );
});

test("HTTP instructor adapter clears stored auth after protected 401 responses", async () => {
  const store = createMemoryInstructorAuthStore(authSessionContract());
  const api = new HttpInstructorCommandApi("http://api.test/api", store);
  let caught: unknown;

  await withMockFetch([], async () =>
    jsonResponse(
      {
        error: {
          code: "UNAUTHENTICATED",
          message: "Sign in is required.",
        },
      },
      { status: 401 },
    ),
    async () => {
      try {
        await api.getSnapshot("inst_001", "2026-08-20");
      } catch (error) {
        caught = error;
      }
    },
  );

  assert.equal(caught instanceof InstructorCommandApiError, true);
  assert.equal((caught as InstructorCommandApiError).isAuthFailure, true);
  assert.equal(store.get(), null);
});

test("HTTP instructor adapter reads the coach command-center snapshot without raw instructor routing", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("http://api.test/api/");

  await withMockFetch(calls, async () => jsonResponse(snapshotContract()),
    async () => {
      const snapshot = await api.getSnapshot("frontend-should-not-route-by-this-id", "2026-08-20");

      assert.equal(calls[0].url, "http://api.test/api/coach/command-center/snapshot?date=2026-08-20");
      assert.equal(snapshot.instructor.id, "inst_001");
      assert.equal(snapshot.clients.length, 1);
      assert.equal(snapshot.clients[0].name, "Alice");
      assert.equal(snapshot.requests.length, 1);
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

test("HTTP instructor adapter uses canonical confirmation and preserves returned occurrence IDs", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("http://api.test/api", createMemoryInstructorAuthStore(authSessionContract()));
  const calendarLinks = calendarLinksContract("app_request_1");

  await withMockFetch(calls, async () =>
    jsonResponse({
      id: "request/one",
      status: "confirmed",
      appointment_id: "app_request_1",
      appointment_ids: ["app_request_1", "app_request_2"],
      message: "Lesson request is confirmed.",
      calendar_links: calendarLinks,
    }),
    async () => {
      const response = await api.confirmRequest("request/one");
      const body = JSON.parse(String(calls[0].init?.body));

      assert.equal(calls[0].url, "http://api.test/api/coach/lesson-requests/request%2Fone/confirm");
      assert.equal(calls[0].init?.method, "POST");
      assert.equal(headerValue(calls[0].init?.headers, "authorization"), "Bearer signed.jwt");
      assert.deepEqual(body, {});
      assert.deepEqual(response.appointment_ids, ["app_request_1", "app_request_2"]);
      assert.equal(response.calendar_links?.google_events.length, 2);
    },
  );
});

test("HTTP instructor adapter reads handoff by exact appointment and downloads authenticated ICS", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("http://api.test/api", createMemoryInstructorAuthStore(authSessionContract()));
  const links = calendarLinksContract("app/direct one");

  await withMockFetch(
    calls,
    async (input) => {
      const url = requestUrl(input);
      if (url.endsWith("/calendar-links")) return jsonResponse(links);
      return new Response("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n", {
        headers: { "content-type": "text/calendar; charset=utf-8" },
      });
    },
    async () => {
      const read = await api.getAppointmentCalendarLinks("app/direct one");
      const blob = await api.downloadCalendarIcs(read.ics);

      assert.equal(calls[0].url, "http://api.test/api/coach/appointments/app%2Fdirect%20one/calendar-links");
      assert.equal(calls[1].url, "http://api.test/api/coach/appointments/app%2Fdirect%20one/calendar.ics");
      assert.equal(headerValue(calls[0].init?.headers, "authorization"), "Bearer signed.jwt");
      assert.equal(headerValue(calls[1].init?.headers, "authorization"), "Bearer signed.jwt");
      assert.equal(calls[1].init?.cache, "no-store");
      assert.equal(calls[1].init?.redirect, "error");
      assert.equal(calls[1].init?.referrerPolicy, "no-referrer");
      assert.equal(await blob.text(), "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n");
    },
  );
});

test("HTTP instructor adapter rejects untrusted and non-calendar downloads", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("https://api.test/api", createMemoryInstructorAuthStore(authSessionContract()));

  await assert.rejects(() => api.downloadCalendarIcs("https://evil.test/api/coach/appointments/app_1/calendar.ics"));
  assert.equal(calls.length, 0);

  await withMockFetch(
    calls,
    async () => new Response("not a calendar", { headers: { "content-type": "text/html" } }),
    async () => {
      await assert.rejects(
        () => api.downloadCalendarIcs("https://api.test/api/coach/appointments/app_1/calendar.ics"),
        (error: unknown) => error instanceof InstructorCommandApiError && error.code === "INVALID_CALENDAR_RESPONSE",
      );
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

test("HTTP instructor adapter saves scheduler rules through the coach settings contract", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("http://api.test/api");
  const rules = snapshotContract().rules;

  await withMockFetch(calls, async () => jsonResponse(rules), async () => {
    const saved = await api.saveRules(rules);
    const body = JSON.parse(String(calls[0].init?.body)) as SchedulerRules;

    assert.equal(calls[0].url, "http://api.test/api/coach/settings");
    assert.equal(calls[0].init?.method, "PUT");
    assert.deepEqual(body, rules);
    assert.deepEqual(saved, rules);
  });
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

function headerValue(headers: HeadersInit | undefined, name: string): string | null {
  if (!headers) return null;
  return new Headers(headers).get(name);
}

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
    ...init,
  });
}

function authSessionContract(): InstructorAuthSession {
  return {
    access_token: "signed.jwt",
    token_type: "bearer",
    coach: {
      instructor_id: "inst_001",
      display_name: "Coach Sarah",
    },
    expires_at: "2026-08-31T20:00:00Z",
  };
}

function calendarLinksContract(anchor: string) {
  const path = `/api/coach/appointments/${encodeURIComponent(anchor)}/calendar.ics`;
  const google = "https://calendar.google.com/calendar/render?action=TEMPLATE";
  return {
    google,
    google_events: [
      { start_iso: "2026-08-29T17:30:00Z", end_iso: "2026-08-29T18:15:00Z", url: `${google}&dates=one` },
      { start_iso: "2026-09-05T17:30:00Z", end_iso: "2026-09-05T18:15:00Z", url: `${google}&dates=two` },
    ],
    apple: path,
    ics: path,
  };
}

function snapshotContract(): CommandCenterSnapshot {
  return {
    instructor: {
      id: "inst_001",
      name: "Coach Sarah",
      business_name: "Swim Command",
      main_address: "100 Main St, Austin, TX",
      lat: 30.2672,
      lng: -97.7431,
      work_start_time: "08:00",
      work_end_time: "17:00",
    },
    date: "2026-08-20",
    clients: [
      {
        id: "client_alice",
        name: "Alice",
        phone: "",
        email: "",
        default_location: {
          address: "450 North Blvd, Austin, TX",
          lat: 30.33,
          lng: -97.72,
          is_instructor_facility: false,
        },
        location_mode: "client_home",
        availability: [],
        lesson_duration_mins: 45,
        notes: "",
        history_summary: "Loaded from client profile",
        progress_focus: "",
      },
    ],
    schedule: [
      {
        id: "app_101",
        kind: "lesson",
        client_id: "client_alice",
        title: "Alice",
        start_time: "2026-08-20T09:00:00",
        end_time: "2026-08-20T10:00:00",
        location: {
          address: "450 North Blvd, Austin, TX",
          lat: 30.33,
          lng: -97.72,
          is_instructor_facility: false,
        },
        status: "scheduled",
      },
    ],
    requests: [
      {
        id: "req_sofia_pending_0820",
        client_id: "client_sofia",
        client_name: "Sofia Martinez",
        requested_slot: {
          id: "slot_req_sofia_pending_0820",
          start_time: "2026-08-20T16:00:00",
          end_time: "2026-08-20T16:45:00",
          travel_time_mins: 12,
          buffer_mins: 10,
          distance_miles: 2.1,
          target_location: {
            address: "820 Lakeview Dr, Austin, TX",
            lat: 30.289,
            lng: -97.739,
            is_instructor_facility: false,
          },
        },
        source: "client_invite",
        status: "pending",
        risk: "clear",
        submitted_at: "2026-08-19T16:30:00",
        note: "Client selected this time from an invite.",
        alternative_slots: [],
      },
    ],
    rules: {
      buffers_enabled: true,
      apply_distance_buffer_at_shift_start: false,
      rounding_increment_mins: 15,
      post_break_buffer_mins: 15,
      distance_buffer_tiers: [
        { id: "near", label: "Under 5 mi", min_miles: 0, max_miles: 4.999, buffer_mins: 10 },
        { id: "medium", label: "5-10 mi", min_miles: 5, max_miles: 10, buffer_mins: 20 },
        { id: "far", label: "Over 10 mi", min_miles: 10.001, max_miles: null, buffer_mins: 30 },
      ],
      payments: {
        enabled_methods: ["card", "ach", "cash"],
        cancellation_window_hours: 12,
        require_payment_method_for_new_clients: true,
      },
    },
  };
}
