import assert from "node:assert/strict";
import test from "node:test";
import {
  clearLegacyInstructorAuthState,
  HttpInstructorCommandApi,
  InstructorCommandApiError,
  MockInstructorCommandApi,
} from "../src/api/instructorApi.js";
import { providerDraftFromConfiguration } from "../src/domain/providerSettings.js";
import type {
  BookingRequestDTO,
  CoachEmailDelivery,
  CommandCenterSnapshot,
  GoogleCalendarConnection,
  InstructorAuthSession,
  ProviderConfiguration,
  ProviderConfigurationUpdate,
  SchedulingPolicy,
  SchedulingPolicyUpdate,
} from "../src/api/types.js";

type FetchCall = {
  url: string;
  init?: RequestInit;
};

test("HTTP instructor adapter signs in with cookie credentials, CSRF protection, and metadata-only JSON", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("http://api.test/api");

  await withMockFetch(calls, async () => jsonResponse({
    ...authSessionContract(),
    access_token: "must-not-reach-application-state",
    token_type: "bearer",
  }), async () => {
    const session = await api.signIn({ email: "coach@example.test", password: "dev-secret" });
    const body = JSON.parse(String(calls[0].init?.body));

    assert.equal(calls[0].url, "http://api.test/api/auth/coach/sign-in");
    assert.equal(calls[0].init?.method, "POST");
    assert.equal(calls[0].init?.credentials, "include");
    assert.equal(headerValue(calls[0].init?.headers, "x-csrf-protection"), "1");
    assert.equal(headerValue(calls[0].init?.headers, "authorization"), null);
    assert.deepEqual(body, { email: "coach@example.test", password: "dev-secret" });
    assert.equal(session.authenticated, true);
    assert.deepEqual(session.coach, { instructor_id: "inst_001", display_name: "Coach Sarah" });
    assert.equal("access_token" in session, false);
    assert.equal("token_type" in session, false);
  });
});

test("HTTP instructor adapter sends cookie credentials and no bearer header with protected coach requests", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("http://api.test/api");

  await withMockFetch(calls, async () => jsonResponse(snapshotContract()), async () => {
    await api.getSnapshot("frontend-should-not-route-by-this-id", "2026-08-20");

    assert.equal(calls[0].url, "http://api.test/api/coach/command-center/snapshot?date=2026-08-20");
    assert.equal(calls[0].init?.credentials, "include");
    assert.equal(headerValue(calls[0].init?.headers, "authorization"), null);
    assert.equal(headerValue(calls[0].init?.headers, "x-csrf-protection"), null);
  });
});

test("HTTP instructor adapter validates the HttpOnly session with the backend", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("http://api.test/api");

  await withMockFetch(calls, async () => jsonResponse(authSessionContract()),
    async () => {
      const session = await api.validateSession();

      assert.equal(calls[0].url, "http://api.test/api/auth/coach/session");
      assert.equal(calls[0].init?.method, "GET");
      assert.equal(calls[0].init?.credentials, "include");
      assert.equal(headerValue(calls[0].init?.headers, "authorization"), null);
      assert.deepEqual(session.coach, {
        instructor_id: "inst_001",
        display_name: "Coach Sarah",
      });
    },
  );
});

test("HTTP instructor adapter classifies only protected 401 responses as authentication failures", async () => {
  const api = new HttpInstructorCommandApi("http://api.test/api");
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

  await withMockFetch([], async () =>
    jsonResponse({ error: { code: "CSRF_VALIDATION_FAILED", message: "Request origin was rejected." } }, { status: 403 }),
    async () => {
      await assert.rejects(
        () => api.bookLesson({
          instructor_id: "inst_001",
          client_id: "client_001",
          client_name: "Alice",
          start_time_iso: "2026-08-29T12:30:00",
          duration_mins: 45,
          location_address: "450 North Blvd, Austin, TX",
        }),
        (error: unknown) => error instanceof InstructorCommandApiError && !error.isAuthFailure,
      );
    },
  );
});

test("legacy browser bearer state is deleted without being parsed or migrated", () => {
  const removals: string[] = [];
  clearLegacyInstructorAuthState({ removeItem: (key) => removals.push(key) });
  assert.deepEqual(removals, ["swim-command:coach-session:v1"]);
});

test("HTTP instructor sign-out is unconditional, bodyless, credentialed, and CSRF protected", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("http://api.test/api");

  await withMockFetch(calls, async () => jsonResponse({ success: true, message: "Signed out." }), async () => {
    await api.signOut();
  });

  assert.equal(calls[0].url, "http://api.test/api/auth/coach/sign-out");
  assert.equal(calls[0].init?.method, "POST");
  assert.equal(calls[0].init?.credentials, "include");
  assert.equal(calls[0].init?.body, undefined);
  assert.equal(headerValue(calls[0].init?.headers, "content-type"), null);
  assert.equal(headerValue(calls[0].init?.headers, "x-csrf-protection"), "1");
  assert.equal(headerValue(calls[0].init?.headers, "authorization"), null);
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

test("HTTP instructor adapter uses provider-scoped blocker CRUD with CSRF and idempotency", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("http://api.test/api");
  const blocker = {
    id: "app/blocker one",
    kind: "personal" as const,
    title: "School pickup",
    start_time: "2026-08-21T14:00:00-05:00",
    end_time: "2026-08-21T14:45:00-05:00",
    local_date: "2026-08-21",
    local_start_time: "14:00",
    local_end_time: "14:45",
    timezone: "America/Chicago",
    revision: 1,
    location: {
      address: "500 North Ave, Austin, TX",
      lat: 30.3,
      lng: -97.7,
      is_instructor_facility: false,
    },
  };
  const mutation = {
    kind: "personal" as const,
    title: "School pickup",
    date: "2026-08-21",
    start_time: "14:00",
    end_time: "14:45",
    location_address: "500 North Ave, Austin, TX",
  };

  await withMockFetch(calls, async (_url, init) => {
    if (init?.method === "DELETE") return new Response(null, { status: 204 });
    return jsonResponse(blocker);
  }, async () => {
    await api.createBlocker(mutation, "blocker-idempotency-001");
    await api.updateBlocker(blocker.id, { ...mutation, expected_revision: 1 });
    await api.deleteBlocker(blocker.id, 1);
  });

  assert.equal(calls[0].url, "http://api.test/api/coach/blockers");
  assert.equal(calls[0].init?.method, "POST");
  assert.equal(calls[0].init?.credentials, "include");
  assert.equal(headerValue(calls[0].init?.headers, "x-csrf-protection"), "1");
  assert.equal(headerValue(calls[0].init?.headers, "idempotency-key"), "blocker-idempotency-001");
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), mutation);

  assert.equal(calls[1].url, "http://api.test/api/coach/blockers/app%2Fblocker%20one");
  assert.equal(calls[1].init?.method, "PUT");
  assert.equal(headerValue(calls[1].init?.headers, "x-csrf-protection"), "1");
  assert.deepEqual(JSON.parse(String(calls[1].init?.body)), { ...mutation, expected_revision: 1 });

  assert.equal(
    calls[2].url,
    "http://api.test/api/coach/blockers/app%2Fblocker%20one?expected_revision=1",
  );
  assert.equal(calls[2].init?.method, "DELETE");
  assert.equal(calls[2].init?.credentials, "include");
  assert.equal(headerValue(calls[2].init?.headers, "x-csrf-protection"), "1");
  assert.equal(calls[2].init?.body, undefined);
});

test("HTTP instructor adapter encodes the provider-local blocker date", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("http://api.test/api");
  await withMockFetch(calls, async () => jsonResponse([]), async () => {
    await api.getBlockers("2026-08-21&provider=other");
  });
  assert.equal(
    calls[0].url,
    "http://api.test/api/coach/blockers?date=2026-08-21%26provider%3Dother",
  );
  assert.equal(calls[0].init?.credentials, "include");
});

test("mock blocker timestamps use the provider timezone winter offset", async () => {
  const api = new MockInstructorCommandApi();
  const blocker = await api.createBlocker({
    kind: "break",
    title: "Winter break",
    date: "2026-12-15",
    start_time: "12:00",
    end_time: "12:30",
    location_address: null,
  }, "mock-winter-offset");

  assert.equal(blocker.start_time, "2026-12-15T12:00:00-06:00");
  assert.equal(blocker.end_time, "2026-12-15T12:30:00-06:00");
  assert.equal(blocker.timezone, "America/Chicago");
});

test("mock blocker overlap treats naive seeded times and offset times as instants", async () => {
  const api = new MockInstructorCommandApi();
  const [seeded] = await api.getBlockers("2026-08-27");
  assert.equal(seeded.start_time, "2026-08-27T12:15:00-05:00");
  assert.equal(seeded.end_time, "2026-08-27T12:45:00-05:00");

  const blocker = await api.createBlocker({
    kind: "unavailable",
    title: "Ends at lunch",
    date: "2026-08-27",
    start_time: "11:45",
    end_time: "12:15",
    location_address: null,
  }, "mock-adjacent-seeded-blocker");

  assert.equal(blocker.end_time, "2026-08-27T12:15:00-05:00");
});

test("mock blocker timestamps reject nonexistent and ambiguous local times", async () => {
  const api = new MockInstructorCommandApi();
  await assert.rejects(
    () => api.createBlocker({
      kind: "break",
      title: "Spring transition",
      date: "2026-03-08",
      start_time: "02:15",
      end_time: "03:15",
      location_address: null,
    }, "mock-spring-transition"),
    (error: unknown) => (
      error instanceof InstructorCommandApiError
      && error.code === "VALIDATION_ERROR"
      && (error.details as { field?: string; reason?: string }).field === "start_time"
      && (error.details as { field?: string; reason?: string }).reason === "nonexistent_local_time"
    ),
  );
  await assert.rejects(
    () => api.createBlocker({
      kind: "break",
      title: "Fall transition",
      date: "2026-11-01",
      start_time: "01:15",
      end_time: "01:45",
      location_address: null,
    }, "mock-fall-transition"),
    (error: unknown) => (
      error instanceof InstructorCommandApiError
      && error.code === "VALIDATION_ERROR"
      && (error.details as { field?: string; reason?: string }).field === "start_time"
      && (error.details as { field?: string; reason?: string }).reason === "ambiguous_local_time"
    ),
  );
});

test("mock blocker idempotency replays the immutable create after edit and delete", async () => {
  const api = new MockInstructorCommandApi();
  const originalMutation = {
    kind: "personal" as const,
    title: "School pickup",
    date: "2026-12-16",
    start_time: "13:00",
    end_time: "13:45",
    location_address: "500 North Ave, Austin, TX",
  };
  const created = await api.createBlocker(originalMutation, "mock-immutable-create");

  await api.updateBlocker(created.id, {
    kind: "unavailable",
    title: "Family commitment",
    date: "2026-12-16",
    start_time: "15:00",
    end_time: "15:45",
    location_address: null,
    expected_revision: 1,
  });
  await api.saveProvider({
    ...providerDraftFromConfiguration(await api.getProvider()),
    timezone: "America/New_York",
  });
  assert.deepEqual(
    await api.createBlocker(originalMutation, "mock-immutable-create"),
    created,
  );

  await api.deleteBlocker(created.id, 2);
  assert.deepEqual(
    await api.createBlocker(originalMutation, "mock-immutable-create"),
    created,
  );
});

test("mock schedule reads reproject canonical blocker instants after a timezone change", async () => {
  const api = new MockInstructorCommandApi();
  const mutation = {
    kind: "unavailable" as const,
    title: "Late admin",
    date: "2026-12-16",
    start_time: "23:00",
    end_time: "23:30",
    location_address: null,
  };
  const created = await api.createBlocker(mutation, "mock-timezone-reprojection");
  assert.equal(created.start_time, "2026-12-16T23:00:00-06:00");

  await api.saveProvider({
    ...providerDraftFromConfiguration(await api.getProvider()),
    timezone: "America/New_York",
  });

  assert.equal(
    (await api.getBlockers("2026-12-16")).some(({ id }) => id === created.id),
    false,
  );
  const [projected] = (await api.getBlockers("2026-12-17"))
    .filter(({ id }) => id === created.id);
  assert.equal(projected.start_time, "2026-12-17T00:00:00-05:00");
  assert.equal(projected.end_time, "2026-12-17T00:30:00-05:00");
  assert.equal(projected.local_date, "2026-12-17");
  assert.equal(projected.timezone, "America/New_York");
  assert.equal(
    (await api.getSnapshot("inst_riley_001", "2026-12-17")).schedule
      .some(({ id }) => id === created.id),
    true,
  );
  assert.deepEqual(
    await api.createBlocker(mutation, "mock-timezone-reprojection"),
    created,
  );
});

test("mock date reads include a reprojected blocker that began on the prior local day", async () => {
  const api = new MockInstructorCommandApi();
  const created = await api.createBlocker({
    kind: "break",
    title: "Early paperwork",
    date: "2026-12-16",
    start_time: "00:30",
    end_time: "02:30",
    location_address: null,
  }, "mock-timezone-day-overlap");

  await api.saveProvider({
    ...providerDraftFromConfiguration(await api.getProvider()),
    timezone: "America/Los_Angeles",
  });

  const projected = (await api.getBlockers("2026-12-16"))
    .find(({ id }) => id === created.id);
  assert.equal(projected?.start_time, "2026-12-15T22:30:00-08:00");
  assert.equal(projected?.end_time, "2026-12-16T00:30:00-08:00");
  assert.equal(
    (await api.getSnapshot("inst_riley_001", "2026-12-16")).schedule
      .some(({ id }) => id === created.id),
    true,
  );
});

test("mock date reads tolerate IANA zones whose local day does not begin at midnight", async () => {
  const api = new MockInstructorCommandApi();
  await api.saveProvider({
    ...providerDraftFromConfiguration(await api.getProvider()),
    timezone: "America/Santiago",
  });
  const blocker = await api.createBlocker({
    kind: "unavailable",
    title: "Transition-day event",
    date: "2026-09-06",
    start_time: "04:00",
    end_time: "04:30",
    location_address: null,
  }, "mock-midnight-transition-day");

  assert.equal((await api.getSnapshot("inst_riley_001", "2026-09-06")).date, "2026-09-06");
  assert.equal(
    (await api.getBlockers("2026-09-06")).some(({ id }) => id === blocker.id),
    true,
  );
});

test("mock availability excludes slots occupied by provider blockers", async () => {
  const api = new MockInstructorCommandApi();
  const request: BookingRequestDTO = {
    instructor_id: "inst_riley_001",
    client_id: "client_sofia",
    client_name: "Sofia Martinez",
    location: {
      address: "450 North Blvd, Austin, TX",
      lat: 30.33,
      lng: -97.72,
      is_instructor_facility: false,
    },
    requested_date: "2026-08-27",
    duration_mins: 45,
  };
  assert.equal(
    (await api.findAvailableSlots(request)).some((slot) => slot.id === "slot_sofia_0827_1800"),
    true,
  );

  await api.createBlocker({
    kind: "unavailable",
    title: "Evening commitment",
    date: "2026-08-27",
    start_time: "18:00",
    end_time: "18:45",
    location_address: null,
  }, "mock-blocked-availability");
  const after = await api.findAvailableSlots(request);

  assert.equal(after.some((slot) => slot.id === "slot_sofia_0827_1800"), false);
  assert.equal(after.some((slot) => slot.id === "slot_sofia_0827_1845"), true);
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
      assert.equal(calls[0].init?.credentials, "include");
      assert.equal(headerValue(calls[0].init?.headers, "x-csrf-protection"), "1");
      assert.equal(headerValue(calls[0].init?.headers, "authorization"), null);
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
  const api = new HttpInstructorCommandApi("http://api.test/api");
  const calendarLinks = calendarLinksContract("app_request_1");
  const emailDelivery = emailDeliveryContract({ id: "delivery-confirm-1", status: "queued" });

  await withMockFetch(calls, async () =>
    jsonResponse({
      id: "request/one",
      status: "confirmed",
      appointment_id: "app_request_1",
      appointment_ids: ["app_request_1", "app_request_2"],
      message: "Lesson request is confirmed.",
      calendar_links: calendarLinks,
      email_delivery: emailDelivery,
    }),
    async () => {
      const response = await api.confirmRequest("request/one");
      const body = JSON.parse(String(calls[0].init?.body));

      assert.equal(calls[0].url, "http://api.test/api/coach/lesson-requests/request%2Fone/confirm");
      assert.equal(calls[0].init?.method, "POST");
      assert.equal(calls[0].init?.credentials, "include");
      assert.equal(headerValue(calls[0].init?.headers, "authorization"), null);
      assert.equal(headerValue(calls[0].init?.headers, "x-csrf-protection"), "1");
      assert.deepEqual(body, {});
      assert.deepEqual(response.appointment_ids, ["app_request_1", "app_request_2"]);
      assert.equal(response.calendar_links?.google_events.length, 2);
      assert.equal(response.email_delivery?.id, "delivery-confirm-1");
    },
  );
});

test("HTTP instructor adapter posts the exact selected-alternative confirmation body", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("http://api.test/api");

  await withMockFetch(calls, async () =>
    jsonResponse({
      id: "request/one",
      status: "confirmed",
      appointment_id: "app_request_1",
      appointment_ids: ["app_request_1"],
      message: "Lesson request is confirmed.",
      calendar_links: calendarLinksContract("app_request_1"),
      email_delivery: emailDeliveryContract({ id: "delivery-confirm-1", status: "queued" }),
    }),
    async () => {
      await api.confirmRequest("request/one", "alternative/selected");

      assert.equal(calls[0].url, "http://api.test/api/coach/lesson-requests/request%2Fone/confirm");
      assert.equal(calls[0].init?.method, "POST");
      assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { slot_id: "alternative/selected" });
    },
  );
});

test("HTTP instructor adapter refreshes, resends without a body, and corrects email delivery contact", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("http://api.test/api");
  const delivery = emailDeliveryContract({ id: "delivery/one", status: "failed", can_resend: true });

  await withMockFetch(
    calls,
    async (input) => {
      const url = requestUrl(input);
      if (url.endsWith("/coach/email-deliveries")) return jsonResponse([delivery]);
      return jsonResponse({ ...delivery, status: "queued", can_resend: false, updated_at: "2026-08-27T16:01:00Z" });
    },
    async () => {
      const deliveries = await api.getEmailDeliveries();
      const resent = await api.resendEmailDelivery("delivery/one");
      const corrected = await api.updateEmailDeliveryContact("delivery/one", "client@example.test");

      assert.equal(deliveries[0].id, "delivery/one");
      assert.equal(resent.status, "queued");
      assert.equal(corrected.status, "queued");
    },
  );

  assert.equal(calls[0].url, "http://api.test/api/coach/email-deliveries");
  assert.equal(calls[0].init?.method, "GET");
  assert.equal(calls[1].url, "http://api.test/api/coach/email-deliveries/delivery%2Fone/resend");
  assert.equal(calls[1].init?.method, "POST");
  assert.equal(calls[1].init?.body, undefined);
  assert.equal(calls[2].url, "http://api.test/api/coach/email-deliveries/delivery%2Fone/contact");
  assert.equal(calls[2].init?.method, "PUT");
  assert.deepEqual(JSON.parse(String(calls[2].init?.body)), { email: "client@example.test" });
  for (const call of calls) {
    assert.equal(call.init?.credentials, "include");
    assert.equal(headerValue(call.init?.headers, "authorization"), null);
  }
  assert.equal(headerValue(calls[1].init?.headers, "x-csrf-protection"), "1");
  assert.equal(headerValue(calls[2].init?.headers, "x-csrf-protection"), "1");
});

test("Google Calendar Settings sends the encoded instructor precondition on each credentialed request", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("https://api.test/api/");
  const expectedInstructorId = "inst /+%é";
  const authorizationUrl = "https://accounts.google.com/o/oauth2/v2/auth?state=local-state";
  const disconnected: GoogleCalendarConnection = {
    provider: "google",
    state: "disconnected",
    connected_at: null,
    updated_at: null,
    authorization_required_at: null,
  };
  const connected: GoogleCalendarConnection = {
    ...disconnected,
    state: "connected",
    connected_at: "2026-08-27T12:00:00Z",
    updated_at: "2026-08-27T12:00:00Z",
  };

  await withMockFetch(calls, async (_input, init) => {
    if (init?.method === "POST") return jsonResponse({ authorization_url: authorizationUrl });
    return jsonResponse(init?.method === "DELETE" ? disconnected : connected);
  }, async () => {
    assert.deepEqual(await api.getGoogleCalendarConnection(expectedInstructorId), connected);
    assert.equal(await api.authorizeGoogleCalendar(expectedInstructorId), authorizationUrl);
    assert.deepEqual(await api.disconnectGoogleCalendar(expectedInstructorId), disconnected);
    await assert.rejects(() => api.authorizeGoogleCalendar(""), /requires a signed-in provider/);
    await assert.rejects(() => api.disconnectGoogleCalendar(""), /requires a signed-in provider/);
  });

  const path = "https://api.test/api/coach/calendar-connections/google";
  assert.deepEqual(calls.map(({ url, init }) => [init?.method, url]), [
    ["GET", path],
    ["POST", `${path}/authorize`],
    ["DELETE", path],
  ]);
  for (const { init } of calls) {
    assert.equal(init?.credentials, "include");
    assert.equal(init?.body, undefined);
    assert.equal(headerValue(init?.headers, "authorization"), null);
    assert.equal(headerValue(init?.headers, "content-type"), null);
    assert.equal(headerValue(init?.headers, "x-csrf-protection"), init?.method === "GET" ? null : "1");
    assert.equal(headerValue(init?.headers, "x-expected-instructor-id"), "inst%20%2F%2B%25%C3%A9");
  }
  assert.equal(calls[1].init?.redirect, "error");
});

test("HTTP instructor adapter reads handoff by exact appointment and downloads authenticated ICS", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("http://api.test/api");
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
      assert.equal(calls[0].init?.credentials, "include");
      assert.equal(calls[1].init?.credentials, "include");
      assert.equal(headerValue(calls[0].init?.headers, "authorization"), null);
      assert.equal(headerValue(calls[1].init?.headers, "authorization"), null);
      assert.equal(calls[1].init?.cache, "no-store");
      assert.equal(calls[1].init?.redirect, "error");
      assert.equal(calls[1].init?.referrerPolicy, "no-referrer");
      assert.equal(await blob.text(), "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n");
    },
  );
});

test("HTTP instructor adapter rejects untrusted and non-calendar downloads", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("https://api.test/api");

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

test("HTTP instructor adapter reads and atomically replaces the current provider", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("http://api.test/api/");
  const provider = providerConfigurationContract();
  const update = providerUpdateContract();

  await withMockFetch(
    calls,
    async (_input, init) => jsonResponse(init?.method === "PUT" ? provider : provider),
    async () => {
      const loaded = await api.getProvider();
      const saved = await api.saveProvider(update);
      assert.deepEqual(loaded, provider);
      assert.deepEqual(saved, provider);
    },
  );

  assert.equal(calls[0].url, "http://api.test/api/coach/provider");
  assert.equal(calls[0].init?.method, "GET");
  assert.equal(calls[0].init?.credentials, "include");
  assert.equal(headerValue(calls[0].init?.headers, "x-csrf-protection"), null);
  assert.equal(headerValue(calls[0].init?.headers, "authorization"), null);

  assert.equal(calls[1].url, "http://api.test/api/coach/provider");
  assert.equal(calls[1].init?.method, "PUT");
  assert.equal(calls[1].init?.credentials, "include");
  assert.equal(headerValue(calls[1].init?.headers, "x-csrf-protection"), "1");
  assert.equal(headerValue(calls[1].init?.headers, "authorization"), null);
  assert.deepEqual(JSON.parse(String(calls[1].init?.body)), update);
  assert.equal("id" in JSON.parse(String(calls[1].init?.body)), false);
  assert.equal("lat" in JSON.parse(String(calls[1].init?.body)), false);
});

test("HTTP instructor adapter preserves provider field-error details", async () => {
  const api = new HttpInstructorCommandApi("http://api.test/api");
  let caught: unknown;
  const details = {
    fields: [{ loc: ["body", "working_hours", 0, "end_time"], msg: "Closing must be later." }],
  };

  await withMockFetch(
    [],
    async () => jsonResponse(
      { error: { code: "VALIDATION_ERROR", message: "Request validation failed.", details } },
      { status: 422 },
    ),
    async () => {
      try {
        await api.saveProvider(providerUpdateContract());
      } catch (error) {
        caught = error;
      }
    },
  );

  assert.equal(caught instanceof InstructorCommandApiError, true);
  assert.deepEqual((caught as InstructorCommandApiError).details, details);
});

test("HTTP instructor adapter reads and saves only the canonical scheduling policy contract", async () => {
  const calls: FetchCall[] = [];
  const api = new HttpInstructorCommandApi("http://api.test/api");
  const policy = schedulingPolicyContract();
  const update: SchedulingPolicyUpdate = {
    expected_revision: policy.revision,
    distance_buffers_enabled: false,
    post_break_recovery_mins: 25,
    distance_buffer_tiers: policy.distance_buffer_tiers.map(({ id, buffer_mins }) => ({
      id,
      buffer_mins: buffer_mins + 5,
    })),
  };
  const saved: SchedulingPolicy = {
    ...policy,
    revision: policy.revision + 1,
    distance_buffers_enabled: update.distance_buffers_enabled,
    post_break_recovery_mins: update.post_break_recovery_mins,
    distance_buffer_tiers: policy.distance_buffer_tiers.map((tier, index) => ({
      ...tier,
      buffer_mins: update.distance_buffer_tiers[index].buffer_mins,
    })),
  };

  await withMockFetch(calls, async (_input, init) =>
    jsonResponse(init?.method === "PUT" ? saved : policy), async () => {
    const read = await api.getSchedulingPolicy();
    const response = await api.saveSchedulingPolicy(update);
    const body = JSON.parse(String(calls[1].init?.body)) as SchedulingPolicyUpdate;

    assert.equal(calls[0].url, "http://api.test/api/coach/scheduling-policy");
    assert.equal(calls[0].init?.method, "GET");
    assert.equal(calls[1].url, "http://api.test/api/coach/scheduling-policy");
    assert.equal(calls[1].init?.method, "PUT");
    assert.equal(headerValue(calls[1].init?.headers, "x-csrf-protection"), "1");
    assert.deepEqual(body, update);
    assert.equal("label" in body.distance_buffer_tiers[0], false);
    assert.equal("min_miles" in body.distance_buffer_tiers[0], false);
    assert.equal("payments" in body, false);
    assert.deepEqual(read, policy);
    assert.deepEqual(response, saved);
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
    authenticated: true,
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

function emailDeliveryContract(
  overrides: Partial<CoachEmailDelivery> = {},
): CoachEmailDelivery {
  return {
    id: "delivery-1",
    kind: "confirmation",
    status: "delivered",
    updated_at: "2026-08-27T16:00:00Z",
    client_id: "client_alice",
    client_name: "Alice",
    request_token: "request-1",
    issue: null,
    source_state: "active",
    can_resend: false,
    can_edit_contact: false,
    ...overrides,
  };
}

function snapshotContract(): CommandCenterSnapshot {
  const provider = providerConfigurationContract();
  return {
    instructor: {
      id: "inst_001",
      name: "Coach Sarah",
      business_name: "Swim Command",
      timezone: provider.timezone,
      base_location: provider.base_location,
      service: provider.service,
      working_hours: provider.working_hours,
      setup_complete: provider.setup_complete,
      setup_missing_fields: provider.setup_missing_fields,
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
    email_deliveries: [emailDeliveryContract()],
    scheduling_policy: schedulingPolicyContract(),
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

function schedulingPolicyContract(): SchedulingPolicy {
  return {
    revision: 4,
    distance_buffers_enabled: true,
    post_break_recovery_mins: 15,
    distance_buffer_tiers: [
      { id: "near", label: "Under 5 mi", min_miles: 0, max_miles: 4.999, buffer_mins: 10 },
      { id: "medium", label: "5-10 mi", min_miles: 5, max_miles: 10, buffer_mins: 20 },
      { id: "far", label: "Over 10 mi", min_miles: 10.001, max_miles: null, buffer_mins: 30 },
    ],
  };
}

function providerConfigurationContract(
  overrides: Partial<ProviderConfiguration> = {},
): ProviderConfiguration {
  return {
    id: "inst_001",
    display_name: "Coach Sarah",
    business_name: "Swim Command",
    timezone: "America/Chicago",
    base_location: {
      address: "100 Main St, Austin, TX",
      lat: 30.2672,
      lng: -97.7431,
    },
    service: {
      name: "Private lesson",
      default_duration_mins: 45,
      location: {
        address: "Riverside Pool, Austin, TX",
        lat: 30.2701,
        lng: -97.7402,
      },
    },
    working_hours: [
      { weekday: "monday", enabled: true, start_time: "08:00", end_time: "17:00" },
      { weekday: "tuesday", enabled: true, start_time: "08:00", end_time: "17:00" },
      { weekday: "wednesday", enabled: true, start_time: "08:00", end_time: "17:00" },
      { weekday: "thursday", enabled: true, start_time: "08:00", end_time: "17:00" },
      { weekday: "friday", enabled: true, start_time: "08:00", end_time: "17:00" },
      { weekday: "saturday", enabled: false, start_time: null, end_time: null },
      { weekday: "sunday", enabled: false, start_time: null, end_time: null },
    ],
    setup_complete: true,
    setup_missing_fields: [],
    ...overrides,
  };
}

function providerUpdateContract(): ProviderConfigurationUpdate {
  const provider = providerConfigurationContract();
  return {
    display_name: provider.display_name,
    business_name: provider.business_name,
    timezone: provider.timezone,
    base_address: provider.base_location!.address,
    service_address: provider.service.location!.address,
    service_name: provider.service.name!,
    default_duration_mins: provider.service.default_duration_mins!,
    working_hours: provider.working_hours,
  };
}
