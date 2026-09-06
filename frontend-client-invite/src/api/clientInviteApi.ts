import {
  createConfirmedLesson,
  createRequestedLesson,
  inviteEnvelope,
  schedulerSlots,
  slotResponse,
} from "../data/mockInvite.js";
import type {
  AvailabilitySearchResponse,
  ClientAvailabilitySearchRequest,
  ClientInviteApi,
  ClientInviteEnvelope,
  ClientIntake,
  LessonConfirmation,
  LessonRequest,
} from "./types.js";

const wait = (ms: number) => new Promise((resolve) => globalThis.setTimeout(resolve, ms));

type ApiErrorResponse = {
  error?: {
    code?: string;
    message?: string;
    details?: unknown;
  };
};

export class ClientInviteApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details?: unknown,
  ) {
    super(message);
    this.name = "ClientInviteApiError";
  }
}

export class MockClientInviteApi implements ClientInviteApi {
  private lessonRequest: LessonRequest | null = null;

  async getInvite(inviteToken: string): Promise<ClientInviteEnvelope> {
    await wait(120);
    return {
      ...inviteEnvelope,
      invite_token: inviteToken,
    };
  }

  async submitIntake(inviteToken: string, intake: ClientIntake): Promise<ClientInviteEnvelope> {
    await wait(120);
    return {
      ...inviteEnvelope,
      invite_token: inviteToken,
      intake,
    };
  }

  async findAvailableSlots(
    _inviteToken: string,
    request: ClientAvailabilitySearchRequest,
  ): Promise<AvailabilitySearchResponse> {
    await wait(260);

    const locationFilteredSlots = schedulerSlots.map((slot) => ({
      ...slot,
      location_address: request.location.is_instructor_facility ? "Riverside facility" : request.location.address,
      travel_time_mins: request.location.is_instructor_facility ? 0 : slot.travel_time_mins,
      distance_miles: request.location.is_instructor_facility ? 0 : slot.distance_miles,
      safety_reason: request.location.is_instructor_facility
        ? "Facility selected, so the scheduler evaluates this as the instructor main location with no client-side travel."
        : slot.safety_reason,
      recurrence_available: request.recurrence === "weekly" ? slot.recurrence_available : true,
      recurrence: request.recurrence,
      recurrence_count: request.recurrence === "weekly" ? 4 : 1,
    }));

    return {
      ...slotResponse,
      slots: locationFilteredSlots,
    };
  }

  async requestLessonSlot(inviteToken: string, slotId: string, _idempotencyKey?: string): Promise<LessonRequest> {
    await wait(180);
    const slot = schedulerSlots.find((item) => item.id === slotId) ?? schedulerSlots[0];
    this.lessonRequest = {
      ...createRequestedLesson(slot),
      invite_token: inviteToken,
    };
    return this.lessonRequest;
  }

  async getLessonRequestStatus(requestId: string): Promise<LessonRequest> {
    await wait(160);
    const baseRequest = this.lessonRequest?.id === requestId ? this.lessonRequest : createRequestedLesson(schedulerSlots[0]);
    this.lessonRequest = createConfirmedLesson(baseRequest.slot);
    return this.lessonRequest;
  }

  async selectAlternativeSlot(_requestId: string, slotId: string, _idempotencyKey?: string): Promise<LessonRequest> {
    await wait(160);
    const slot = schedulerSlots.find((item) => item.id === slotId) ?? schedulerSlots[0];
    this.lessonRequest = createRequestedLesson(slot);
    return this.lessonRequest;
  }

  async getCalendarLinks(requestId: string): Promise<LessonConfirmation["calendar_links"]> {
    const status = await this.getLessonRequestStatus(requestId);
    if (!status.confirmation) {
      throw new Error("Calendar links are unavailable until the lesson is confirmed.");
    }

    return status.confirmation.calendar_links;
  }
}

export class FetchClientInviteApi implements ClientInviteApi {
  private readonly baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
  }

  getInvite(inviteToken: string): Promise<ClientInviteEnvelope> {
    return this.fetchJson(`/client-invites/${encodeURIComponent(inviteToken)}`);
  }

  submitIntake(inviteToken: string, intake: ClientIntake): Promise<ClientInviteEnvelope> {
    return this.fetchJson(`/client-invites/${encodeURIComponent(inviteToken)}/intake`, {
      method: "POST",
      body: JSON.stringify(intake),
    });
  }

  findAvailableSlots(
    inviteToken: string,
    request: ClientAvailabilitySearchRequest,
  ): Promise<AvailabilitySearchResponse> {
    return this.fetchJson(`/client-invites/${encodeURIComponent(inviteToken)}/slots`, {
      method: "POST",
      body: JSON.stringify(request),
    });
  }

  requestLessonSlot(inviteToken: string, slotId: string, idempotencyKey?: string): Promise<LessonRequest> {
    return this.fetchJson(`/client-invites/${encodeURIComponent(inviteToken)}/lesson-requests`, {
      method: "POST",
      body: JSON.stringify({
        slot_id: slotId,
        ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}),
      }),
    });
  }

  getLessonRequestStatus(requestToken: string): Promise<LessonRequest> {
    return this.fetchJson(`/lesson-requests/${encodeURIComponent(requestToken)}`);
  }

  selectAlternativeSlot(requestToken: string, slotId: string, idempotencyKey?: string): Promise<LessonRequest> {
    return this.fetchJson(`/lesson-requests/${encodeURIComponent(requestToken)}/select-alternative`, {
      method: "POST",
      body: JSON.stringify({
        slot_id: slotId,
        ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}),
      }),
    });
  }

  getCalendarLinks(requestToken: string): Promise<LessonConfirmation["calendar_links"]> {
    return this.fetchJson(`/lesson-requests/${encodeURIComponent(requestToken)}/calendar-links`);
  }

  private async fetchJson<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: {
        "content-type": "application/json",
        ...init.headers,
      },
    });

    if (!response.ok) {
      throw await toApiError(response);
    }

    return response.json() as Promise<T>;
  }
}

export const createClientInviteApi = (): ClientInviteApi => {
  const baseUrl = import.meta.env.VITE_CLIENT_INVITE_API_URL as string | undefined;
  return baseUrl ? new FetchClientInviteApi(baseUrl) : new MockClientInviteApi();
};

async function toApiError(response: Response): Promise<ClientInviteApiError> {
  let payload: ApiErrorResponse | null = null;
  try {
    payload = (await response.json()) as ApiErrorResponse;
  } catch {
    payload = null;
  }

  const code = payload?.error?.code ?? "REQUEST_FAILED";
  const message = payload?.error?.message ?? `Invite API request failed with ${response.status}.`;
  return new ClientInviteApiError(response.status, code, message, payload?.error?.details);
}
