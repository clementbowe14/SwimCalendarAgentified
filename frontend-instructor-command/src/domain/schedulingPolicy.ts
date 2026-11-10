import type {
  SchedulingPolicy,
  SchedulingPolicyUpdate,
} from "../api/types.js";

export const POLICY_MIN_MINUTES = 0;
export const POLICY_MAX_MINUTES = 240;

export type SchedulingPolicyDraftTier = {
  id: string;
  buffer_mins: string;
};

export type SchedulingPolicyDraft = {
  distance_buffers_enabled: boolean;
  post_break_recovery_mins: string;
  distance_buffer_tiers: SchedulingPolicyDraftTier[];
};

export type SchedulingPolicyMinuteField =
  | "post_break_recovery_mins"
  | `tier.${string}.buffer_mins`;

export type SchedulingPolicyDraftResult =
  | { ok: true; payload: SchedulingPolicyUpdate }
  | { ok: false; error: string };

export type AvailabilityFreshness = "idle" | "refreshing" | "current" | "stale";

export type AvailabilityUiState = {
  status: AvailabilityFreshness;
  message: string;
};

export type SchedulingPolicyRevisionOrder = "older" | "same" | "newer";

export function compareSchedulingPolicyRevision(
  current: SchedulingPolicy | null,
  incoming: SchedulingPolicy,
): SchedulingPolicyRevisionOrder {
  if (!current || incoming.revision > current.revision) return "newer";
  if (incoming.revision < current.revision) return "older";
  return "same";
}

export function schedulingPolicyDraftFromPolicy(policy: SchedulingPolicy): SchedulingPolicyDraft {
  return {
    distance_buffers_enabled: policy.distance_buffers_enabled,
    post_break_recovery_mins: String(policy.post_break_recovery_mins),
    distance_buffer_tiers: policy.distance_buffer_tiers.map((tier) => ({
      id: tier.id,
      buffer_mins: String(tier.buffer_mins),
    })),
  };
}

export function schedulingPolicyDraftWithToggle(
  draft: SchedulingPolicyDraft,
  enabled: boolean,
): SchedulingPolicyDraft {
  return { ...draft, distance_buffers_enabled: enabled };
}

export function schedulingPolicyDraftWithMinute(
  draft: SchedulingPolicyDraft,
  field: SchedulingPolicyMinuteField,
  value: string,
): SchedulingPolicyDraft {
  if (field === "post_break_recovery_mins") {
    return { ...draft, post_break_recovery_mins: value };
  }
  const tierId = tierIdFromField(field);
  return {
    ...draft,
    distance_buffer_tiers: draft.distance_buffer_tiers.map((tier) =>
      tier.id === tierId ? { ...tier, buffer_mins: value } : tier,
    ),
  };
}

export function stepSchedulingPolicyDraft(
  draft: SchedulingPolicyDraft,
  field: SchedulingPolicyMinuteField,
  delta: -1 | 1,
): SchedulingPolicyDraft {
  const current = field === "post_break_recovery_mins"
    ? draft.post_break_recovery_mins
    : draft.distance_buffer_tiers.find((tier) => tier.id === tierIdFromField(field))?.buffer_mins ?? "0";
  const parsed = Number(current);
  const baseline = Number.isFinite(parsed) ? Math.trunc(parsed) : POLICY_MIN_MINUTES;
  const next = Math.min(POLICY_MAX_MINUTES, Math.max(POLICY_MIN_MINUTES, baseline + delta));
  return schedulingPolicyDraftWithMinute(draft, field, String(next));
}

export function schedulingPolicyUpdateFromDraft(
  policy: SchedulingPolicy,
  draft: SchedulingPolicyDraft,
): SchedulingPolicyDraftResult {
  const currentIds = policy.distance_buffer_tiers.map(({ id }) => id);
  const draftIds = draft.distance_buffer_tiers.map(({ id }) => id);
  if (
    currentIds.length !== draftIds.length
    || currentIds.some((id, index) => id !== draftIds[index])
  ) {
    return {
      ok: false,
      error: "The saved distance tiers changed. Reload the saved policy before continuing.",
    };
  }

  const recovery = parseMinutes(draft.post_break_recovery_mins);
  if (recovery === null) {
    return {
      ok: false,
      error: "Post-break recovery must be a whole number from 0 through 240 minutes.",
    };
  }

  const tiers: SchedulingPolicyUpdate["distance_buffer_tiers"] = [];
  for (let index = 0; index < draft.distance_buffer_tiers.length; index += 1) {
    const tier = draft.distance_buffer_tiers[index];
    const minutes = parseMinutes(tier.buffer_mins);
    if (minutes === null) {
      const label = policy.distance_buffer_tiers[index]?.label ?? tier.id;
      return {
        ok: false,
        error: `${label} must use a whole buffer from 0 through 240 minutes.`,
      };
    }
    tiers.push({ id: tier.id, buffer_mins: minutes });
  }

  return {
    ok: true,
    payload: {
      expected_revision: policy.revision,
      distance_buffers_enabled: draft.distance_buffers_enabled,
      post_break_recovery_mins: recovery,
      distance_buffer_tiers: tiers,
    },
  };
}

export function schedulingPolicyDraftMatchesPolicy(
  policy: SchedulingPolicy,
  draft: SchedulingPolicyDraft,
): boolean {
  const result = schedulingPolicyUpdateFromDraft(policy, draft);
  if (!result.ok) return false;
  return result.payload.distance_buffers_enabled === policy.distance_buffers_enabled
    && result.payload.post_break_recovery_mins === policy.post_break_recovery_mins
    && result.payload.distance_buffer_tiers.every((tier, index) => (
      tier.id === policy.distance_buffer_tiers[index]?.id
      && tier.buffer_mins === policy.distance_buffer_tiers[index]?.buffer_mins
    ));
}

export function availabilityAfterPolicySave(hasSearchContext: boolean): AvailabilityUiState {
  return hasSearchContext
    ? { status: "refreshing", message: "Policy saved. Rechecking safe starts…" }
    : { status: "idle", message: "" };
}

export function availabilityRefreshFailed(afterPolicySave: boolean): AvailabilityUiState {
  return {
    status: "stale",
    message: afterPolicySave
      ? "Policy saved, but safe starts could not be refreshed. Retry before booking."
      : "Safe starts could not be refreshed. Retry before booking.",
  };
}

export class MonotonicRequestGate {
  private current = 0;

  next(): number {
    this.current += 1;
    return this.current;
  }

  invalidate(): void {
    this.current += 1;
  }

  isCurrent(token: number): boolean {
    return token === this.current;
  }
}

function parseMinutes(value: string): number | null {
  const trimmed = value.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const parsed = Number(trimmed);
  return Number.isInteger(parsed) && parsed >= POLICY_MIN_MINUTES && parsed <= POLICY_MAX_MINUTES
    ? parsed
    : null;
}

function tierIdFromField(field: `tier.${string}.buffer_mins`): string {
  return field.slice("tier.".length, -".buffer_mins".length);
}
