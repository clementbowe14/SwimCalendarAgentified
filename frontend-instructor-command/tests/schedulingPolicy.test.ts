import assert from "node:assert/strict";
import test from "node:test";
import { InstructorCommandApiError, MockInstructorCommandApi } from "../src/api/instructorApi.js";
import { schedulingPolicy } from "../src/api/mockData.js";
import {
  availabilityAfterPolicySave,
  availabilityRefreshFailed,
  compareSchedulingPolicyRevision,
  MonotonicRequestGate,
  schedulingPolicyDraftMatchesPolicy,
  schedulingPolicyDraftFromPolicy,
  schedulingPolicyDraftWithMinute,
  schedulingPolicyUpdateFromDraft,
  stepSchedulingPolicyDraft,
} from "../src/domain/schedulingPolicy.js";

test("canonical scheduling policy draft submits only editable fields in server tier order", () => {
  let draft = schedulingPolicyDraftFromPolicy(schedulingPolicy);
  draft = schedulingPolicyDraftWithMinute(draft, "post_break_recovery_mins", "25");
  draft = schedulingPolicyDraftWithMinute(draft, "tier.medium.buffer_mins", "35");
  const result = schedulingPolicyUpdateFromDraft(schedulingPolicy, {
    ...draft,
    distance_buffers_enabled: false,
  });

  assert.equal(result.ok, true);
  if (!result.ok) return;
  assert.deepEqual(result.payload, {
    expected_revision: 0,
    distance_buffers_enabled: false,
    post_break_recovery_mins: 25,
    distance_buffer_tiers: [
      { id: "near", buffer_mins: 10 },
      { id: "medium", buffer_mins: 35 },
      { id: "far", buffer_mins: 30 },
    ],
  });
  assert.equal("payments" in result.payload, false);
  assert.equal("rounding_increment_mins" in result.payload, false);
  assert.equal("apply_distance_buffer_at_shift_start" in result.payload, false);
});

test("policy draft keeps invalid direct entry and validates without replacing saved state", () => {
  const draft = schedulingPolicyDraftWithMinute(
    schedulingPolicyDraftFromPolicy(schedulingPolicy),
    "tier.near.buffer_mins",
    "",
  );
  const result = schedulingPolicyUpdateFromDraft(schedulingPolicy, draft);

  assert.equal(draft.distance_buffer_tiers[0].buffer_mins, "");
  assert.deepEqual(schedulingPolicy.distance_buffer_tiers.map(({ buffer_mins }) => buffer_mins), [10, 20, 30]);
  assert.equal(result.ok, false);
  if (!result.ok) assert.match(result.error, /Under 5 mi.*0 through 240/i);
});

test("policy draft reconciliation distinguishes clean and locally edited drafts", () => {
  const clean = schedulingPolicyDraftFromPolicy(schedulingPolicy);
  const edited = schedulingPolicyDraftWithMinute(clean, "tier.near.buffer_mins", "35");

  assert.equal(schedulingPolicyDraftMatchesPolicy(schedulingPolicy, clean), true);
  assert.equal(schedulingPolicyDraftMatchesPolicy(schedulingPolicy, edited), false);
});

test("policy revisions never accept an older response", () => {
  assert.equal(compareSchedulingPolicyRevision(null, schedulingPolicy), "newer");
  assert.equal(compareSchedulingPolicyRevision(schedulingPolicy, schedulingPolicy), "same");
  assert.equal(
    compareSchedulingPolicyRevision(
      { ...schedulingPolicy, revision: schedulingPolicy.revision + 2 },
      { ...schedulingPolicy, revision: schedulingPolicy.revision + 1 },
    ),
    "older",
  );
  assert.equal(
    compareSchedulingPolicyRevision(
      schedulingPolicy,
      { ...schedulingPolicy, revision: schedulingPolicy.revision + 1 },
    ),
    "newer",
  );
});

test("one-handed steppers clamp values and preserve the exact field", () => {
  let draft = schedulingPolicyDraftFromPolicy(schedulingPolicy);
  draft = schedulingPolicyDraftWithMinute(draft, "post_break_recovery_mins", "240");
  draft = stepSchedulingPolicyDraft(draft, "post_break_recovery_mins", 1);
  assert.equal(draft.post_break_recovery_mins, "240");

  draft = schedulingPolicyDraftWithMinute(draft, "tier.near.buffer_mins", "0");
  draft = stepSchedulingPolicyDraft(draft, "tier.near.buffer_mins", -1);
  assert.equal(draft.distance_buffer_tiers[0].buffer_mins, "0");
  assert.equal(draft.distance_buffer_tiers[1].buffer_mins, "20");
});

test("policy-save availability transitions distinguish idle, refreshing, and retryable stale", () => {
  assert.deepEqual(availabilityAfterPolicySave(false), { status: "idle", message: "" });
  assert.equal(availabilityAfterPolicySave(true).status, "refreshing");
  assert.match(availabilityRefreshFailed(true).message, /Policy saved.*Retry before booking/i);
  assert.match(availabilityRefreshFailed(false).message, /could not be refreshed/i);
});

test("monotonic request gate rejects older availability responses", () => {
  const gate = new MonotonicRequestGate();
  const older = gate.next();
  const newer = gate.next();
  assert.equal(gate.isCurrent(older), false);
  assert.equal(gate.isCurrent(newer), true);
  gate.invalidate();
  assert.equal(gate.isCurrent(newer), false);
});

test("mock canonical policy API increments revisions and rejects stale writes", async () => {
  const api = new MockInstructorCommandApi();
  const current = await api.getSchedulingPolicy();
  const update = {
    expected_revision: current.revision,
    distance_buffers_enabled: false,
    post_break_recovery_mins: 30,
    distance_buffer_tiers: current.distance_buffer_tiers.map(({ id, buffer_mins }) => ({ id, buffer_mins })),
  };
  const saved = await api.saveSchedulingPolicy(update);
  assert.equal(saved.revision, current.revision + 1);
  assert.equal(saved.distance_buffers_enabled, false);

  await assert.rejects(
    () => api.saveSchedulingPolicy(update),
    (error: unknown) =>
      error instanceof InstructorCommandApiError
      && error.status === 409
      && error.code === "SCHEDULING_POLICY_STALE",
  );
});

test("mock availability refreshes its times and evidence from the saved policy", async () => {
  const api = new MockInstructorCommandApi();
  const request = {
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
  const before = await api.findAvailableSlots(request);
  const current = await api.getSchedulingPolicy();
  await api.saveSchedulingPolicy({
    expected_revision: current.revision,
    distance_buffers_enabled: true,
    post_break_recovery_mins: current.post_break_recovery_mins,
    distance_buffer_tiers: current.distance_buffer_tiers.map(({ id }) => ({ id, buffer_mins: 90 })),
  });

  const after = await api.findAvailableSlots(request);

  assert.equal(before[0].buffer_mins, 10);
  assert.equal(after[0].buffer_mins, 90);
  assert.equal(after[0].start_time === before[0].start_time, false);
});
