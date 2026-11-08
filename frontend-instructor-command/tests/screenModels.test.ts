import assert from "node:assert/strict";
import test from "node:test";
import { MockInstructorCommandApi } from "../src/api/instructorApi.js";
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
});

test("API adapter confirms a request and returns its persisted calendar handoff", async () => {
  const api = new MockInstructorCommandApi();
  const confirmed = await api.confirmRequest("req_sofia_0827");
  const updated = await api.getSnapshot(snapshot.instructor.id, snapshot.date);

  assert.equal(confirmed.status, "confirmed");
  assert.equal(confirmed.appointment_ids.length, 2);
  assert.equal(confirmed.calendar_links?.google_events.length, 2);
  assert.equal(updated.requests.find((request) => request.id === "req_sofia_0827")?.status, "approved");
  assert.equal(candidateSlots[0].start_time, "2026-08-27T18:00:00");
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
