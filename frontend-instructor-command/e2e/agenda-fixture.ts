import type { CoachAgenda } from "../src/api/types";

export function agendaResponse(overrides: Partial<CoachAgenda> = {}): CoachAgenda {
  return {
    instructor_id: "inst_001",
    today: "2026-08-27",
    timezone: "America/Chicago",
    status: "ready",
    items: [],
    next_up: null,
    ...overrides,
    date: overrides.date ?? overrides.today ?? "2026-08-27",
  };
}
