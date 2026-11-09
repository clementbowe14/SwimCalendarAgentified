import assert from "node:assert/strict";
import test from "node:test";
import { InstructorCommandApiError, MockInstructorCommandApi } from "../src/api/instructorApi.js";
import { candidateSlots, snapshot } from "../src/api/mockData.js";
import { renderAvailabilityFinder } from "../src/components/AvailabilityFinder.js";
import { renderClientDetail } from "../src/components/ClientDetail.js";
import { renderRequestManagement } from "../src/components/RequestManagement.js";
import { renderSettingsRules } from "../src/components/SettingsRules.js";
import { renderTodaySchedule } from "../src/components/TodaySchedule.js";
import {
  buildAgendaRows,
  buildFindSlotsRequest,
  buildReadinessReport,
  findFeaturedClient,
  summarizeRequests,
} from "../src/domain/screenModels.js";

test("Today schedule renders lessons, travel, buffers, and open gaps", () => {
  const html = renderTodaySchedule(snapshot);
  const agendaRows = buildAgendaRows(snapshot.schedule);

  assert.equal(agendaRows.some((row) => row.tone === "buffer"), true);
  assert.match(html, /Maya Chen/);
  assert.match(html, /Recovery buffer/);
  assert.match(html, /Open gap/);
});

test("Availability Finder builds backend-shaped slot requests and renders safety evidence", async () => {
  const api = new MockInstructorCommandApi();
  const client = findFeaturedClient(snapshot, "client_sofia");
  const request = buildFindSlotsRequest(snapshot.instructor.id, client, snapshot.date);
  const slots = await api.findAvailableSlots(request);
  const html = renderAvailabilityFinder(snapshot, client, slots);

  assert.equal(request.instructor_id, "inst_riley_001");
  assert.equal(request.location.address, client.default_location.address);
  assert.equal(slots.length, 3);
  assert.match(html, /Travel 8 min/);
  assert.match(html, /15 min rounding/);
});

test("Client Detail renders contact, availability, notes, and next lesson state", () => {
  const html = renderClientDetail(snapshot, "client_maya");

  assert.match(html, /Maya Chen/);
  assert.match(html, /Parent prefers text/);
  assert.match(html, /Mon, Wed, Thu 16:00-18:00/);
  assert.match(html, /Riverside Aquatic Center/);
});

test("Settings screen stays aligned to Python engine buffer rules", () => {
  const html = renderSettingsRules(snapshot.rules);

  assert.deepEqual(
    snapshot.rules.distance_buffer_tiers.map((tier) => tier.buffer_mins),
    [10, 20, 30],
  );
  assert.equal(snapshot.rules.post_break_buffer_mins, 15);
  assert.match(html, /Under 5 mi/);
  assert.match(html, /Over 10 mi/);
  assert.match(html, /name="buffers_enabled" type="checkbox" checked/);
  assert.match(html, /name="rounding_increment_mins"/);
  assert.match(html, /name="tier\.near\.buffer_mins"/);
  assert.match(html, /<form data-rules-form>/);
});

test("Request management summarizes clear and at-risk pending requests", () => {
  const summary = summarizeRequests(snapshot.requests);
  const html = renderRequestManagement(snapshot);

  assert.equal(summary.totalPending, 2);
  assert.equal(summary.clear, 1);
  assert.equal(summary.atRisk, 1);
  assert.match(html, /Sofia Martinez/);
  assert.match(html, /At risk/);
  assert.doesNotMatch(html, /data-confirm-slot-id=/);
});

test("Request management carries action data only for the selected request-bound alternative", () => {
  const selectedAlternative = candidateSlots[1];
  const selectedSnapshot = {
    ...snapshot,
    requests: [
      {
        ...snapshot.requests[0],
        requested_slot: selectedAlternative,
        alternative_slots: [selectedAlternative, candidateSlots[2]],
      },
    ],
  };

  const html = renderRequestManagement(selectedSnapshot);

  assert.match(
    html,
    /data-confirm-request="req_sofia_0827" data-confirm-slot-id="slot_sofia_0827_1845"/,
  );
  assert.equal((html.match(/data-confirm-slot-id=/g) ?? []).length, 1);
});

test("API adapter confirms a request and returns its persisted calendar handoff", async () => {
  const api = new MockInstructorCommandApi();
  const confirmed = await api.confirmRequest("req_sofia_0827");
  const updated = await api.getSnapshot(snapshot.instructor.id, snapshot.date);

  assert.equal(confirmed.status, "confirmed");
  assert.equal(confirmed.appointment_ids.length, 2);
  assert.equal(confirmed.calendar_links?.google_events.length, 2);
  assert.equal(confirmed.email_delivery?.status, "queued");
  assert.equal(updated.requests.find((request) => request.id === "req_sofia_0827")?.status, "approved");
  assert.equal(candidateSlots[0].start_time, "2026-08-27T18:00:00");
});

test("Mock confirmation persists a selected alternative and enforces deterministic replay", async () => {
  const api = new MockInstructorCommandApi();
  const selectedAlternative = candidateSlots[1];

  const confirmed = await api.confirmRequest("req_sofia_0827", selectedAlternative.id);
  const explicitReplay = await api.confirmRequest("req_sofia_0827", selectedAlternative.id);
  const omittedReplay = await api.confirmRequest("req_sofia_0827");
  const updated = await api.getSnapshot(snapshot.instructor.id, snapshot.date);
  const updatedRequest = updated.requests.find((request) => request.id === "req_sofia_0827");

  assert.equal(updatedRequest?.status, "approved");
  assert.equal(updatedRequest?.requested_slot.id, selectedAlternative.id);
  assert.equal(updatedRequest?.requested_slot.start_time, selectedAlternative.start_time);
  assert.equal(confirmed.calendar_links?.google_events[0].start_iso, "2026-08-27T18:45:00.000Z");
  assert.deepEqual(explicitReplay, confirmed);
  assert.deepEqual(omittedReplay, confirmed);
  assert.equal(
    updated.email_deliveries.filter((delivery) => delivery.id === "delivery_confirmation_req_sofia_0827").length,
    1,
  );

  await assert.rejects(
    () => api.confirmRequest("req_sofia_0827", candidateSlots[2].id),
    (error: unknown) =>
      error instanceof InstructorCommandApiError
      && error.status === 409
      && error.code === "LESSON_REQUEST_CONFIRM_CONFLICT",
  );

  const afterConflict = await api.getSnapshot(snapshot.instructor.id, snapshot.date);
  assert.equal(
    afterConflict.requests.find((request) => request.id === "req_sofia_0827")?.requested_slot.id,
    selectedAlternative.id,
  );
});

test("Readiness report covers every representative screen and calls out backend gaps", () => {
  const report = buildReadinessReport();

  assert.deepEqual(
    report.map((screen) => screen.screen),
    ["Today schedule", "Availability Finder/New Client", "Client Detail", "Settings", "Request management"],
  );
  assert.equal(report.some((screen) => !screen.isReadyForBackend), true);
  assert.equal(report.find((screen) => screen.screen === "Settings")?.isReadyForBackend, true);
  assert.equal(
    report.some((screen) =>
      screen.missingApiContracts.includes("coach client notes, availability, lesson history, and progress detail fields"),
    ),
    true,
  );
});

test("Availability Finder can fall back when real backend schedule has no clients for the date", () => {
  const quietSnapshot = {
    ...snapshot,
    clients: [],
    schedule: [],
  };
  const client = findFeaturedClient(quietSnapshot, "missing_client");
  const request = buildFindSlotsRequest(quietSnapshot.instructor.id, client, quietSnapshot.date);

  assert.equal(client.id, "client_new");
  assert.equal(request.client_name, "New client");
  assert.equal(request.location.is_instructor_facility, true);
});
