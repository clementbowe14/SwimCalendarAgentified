import { candidateSlots, snapshot } from "./mockData.js";
import { trustedInstructorCalendarUrl } from "./calendar.js";
import {
  resolveInstructorDataMode,
  type InstructorDataModeEnvironment,
} from "../config/dataMode.js";
import type {
  BookLessonPayload,
  BookLessonResponse,
  BookingRequestDTO,
  CalendarLinks,
  CoachEmailDelivery,
  CoachLessonRequestActionResponse,
  CoachLessonRequestConfirmResponse,
  CommandCenterSnapshot,
  Id,
  InstructorAuthCredentials,
  InstructorAuthSession,
  InstructorCommandApi,
  LessonRequest,
  SchedulerRules,
  TimeSlotDTO,
} from "./types.js";

const AUTH_STORAGE_KEY = "swim-command:coach-session:v1";

function clone<T>(value: T): T {
  return structuredClone(value);
}

function sameDate(isoDateTime: string, date: string): boolean {
  return isoDateTime.slice(0, 10) === date;
}

export class MockInstructorCommandApi implements InstructorCommandApi {
  readonly authMode = "mock" as const;

  private currentSnapshot: CommandCenterSnapshot = clone(snapshot);
  private readonly calendarHandoffs = new Map<Id, CalendarLinks>();

  getStoredSession(): InstructorAuthSession | null {
    return null;
  }

  async validateSession(): Promise<InstructorAuthSession["coach"]> {
    return {
      instructor_id: this.currentSnapshot.instructor.id,
      display_name: this.currentSnapshot.instructor.name,
    };
  }

  async signIn(_credentials: InstructorAuthCredentials): Promise<InstructorAuthSession> {
    return {
      access_token: "mock-token",
      token_type: "bearer",
      coach: {
        instructor_id: this.currentSnapshot.instructor.id,
        display_name: this.currentSnapshot.instructor.name,
      },
    };
  }

  async signOut(): Promise<void> {
    return;
  }

  clearSession(): void {
    return;
  }

  async getSnapshot(instructor_id: Id, date: string): Promise<CommandCenterSnapshot> {
    const fullSnapshot = clone(this.currentSnapshot);
    return {
      ...fullSnapshot,
      date,
      schedule: fullSnapshot.schedule.filter((item) => sameDate(item.start_time, date)),
      instructor:
        fullSnapshot.instructor.id === instructor_id
          ? fullSnapshot.instructor
          : { ...fullSnapshot.instructor, id: instructor_id },
    };
  }

  async findAvailableSlots(request: BookingRequestDTO): Promise<TimeSlotDTO[]> {
    const targetDate = request.requested_date;
    const isFacility = request.location.is_instructor_facility;
    return clone(candidateSlots)
      .filter((slot) => sameDate(slot.start_time, targetDate))
      .filter((slot) => slot.target_location.is_instructor_facility === isFacility || !isFacility)
      .map((slot) => ({
        ...slot,
        end_time: addMinutes(slot.start_time, request.duration_mins),
        target_location: request.location,
      }));
  }

  async bookLesson(payload: BookLessonPayload): Promise<BookLessonResponse> {
    const appointment_id = `appt_${payload.client_id}_${payload.start_time_iso.replace(/[^0-9]/g, "").slice(0, 12)}`;
    const calendar_links = mockCalendarLinks(appointment_id, [
      {
        startIso: payload.start_time_iso,
        endIso: addMinutes(payload.start_time_iso, payload.duration_mins),
      },
    ]);
    this.calendarHandoffs.set(appointment_id, calendar_links);
    return {
      success: true,
      appointment_id,
      message: `Successfully booked swim lesson for ${payload.client_name}.`,
      calendar_links,
    };
  }

  async confirmRequest(request_id: Id): Promise<CoachLessonRequestConfirmResponse> {
    const updated = this.updateRequest(request_id, (request) => ({ ...request, status: "approved" }));
    const occurrenceStarts = request_id === "req_sofia_0827"
      ? [updated.requested_slot.start_time, addDays(updated.requested_slot.start_time, 7)]
      : [updated.requested_slot.start_time];
    const appointment_ids = occurrenceStarts.map((_, index) => `appt_${request_id}_${index + 1}`);
    const calendar_links = mockCalendarLinks(
      appointment_ids[0],
      occurrenceStarts.map((startIso) => ({
        startIso,
        endIso: addMinutes(startIso, minutesBetweenIso(updated.requested_slot.start_time, updated.requested_slot.end_time)),
      })),
    );
    for (const appointmentId of appointment_ids) {
      this.calendarHandoffs.set(appointmentId, calendar_links);
    }
    const deliveryId = `delivery_confirmation_${request_id}`;
    const existingDelivery = this.currentSnapshot.email_deliveries.find((delivery) => delivery.id === deliveryId);
    const email_delivery = existingDelivery ?? {
      id: deliveryId,
      kind: "confirmation" as const,
      status: "queued" as const,
      updated_at: nextMockUpdatedAt(this.currentSnapshot.email_deliveries),
      client_id: updated.client_id,
      client_name: updated.client_name,
      request_token: request_id,
      issue: null,
      source_state: "active" as const,
      can_resend: false,
      can_edit_contact: false,
    };
    if (!existingDelivery) {
      this.currentSnapshot = {
        ...this.currentSnapshot,
        email_deliveries: [email_delivery, ...this.currentSnapshot.email_deliveries],
      };
    }
    return {
      id: request_id,
      status: "confirmed",
      appointment_id: appointment_ids[0],
      appointment_ids,
      message: "Lesson request is confirmed.",
      calendar_links,
      email_delivery: clone(email_delivery),
    };
  }

  async getEmailDeliveries(): Promise<CoachEmailDelivery[]> {
    return clone(this.currentSnapshot.email_deliveries);
  }

  async resendEmailDelivery(delivery_id: Id): Promise<CoachEmailDelivery> {
    return this.updateEmailDelivery(delivery_id, (delivery) => {
      if (!delivery.can_resend) {
        throw new InstructorCommandApiError(
          409,
          "EMAIL_DELIVERY_NOT_RESENDABLE",
          "This email cannot be safely queued again.",
        );
      }
      return {
        ...delivery,
        status: "queued",
        updated_at: nextMockUpdatedAt(this.currentSnapshot.email_deliveries),
        issue: null,
        can_resend: false,
        can_edit_contact: false,
      };
    });
  }

  async updateEmailDeliveryContact(delivery_id: Id, email: string): Promise<CoachEmailDelivery> {
    if (!isMockEmailAddress(email)) {
      throw new InstructorCommandApiError(422, "VALIDATION_ERROR", "Enter a valid email address.");
    }
    return this.updateEmailDelivery(delivery_id, (delivery) => {
      if (!delivery.can_edit_contact) {
        throw new InstructorCommandApiError(
          409,
          "EMAIL_CONTACT_NOT_EDITABLE",
          "Contact editing is unavailable for this email.",
        );
      }
      return {
        ...delivery,
        status: "queued",
        updated_at: nextMockUpdatedAt(this.currentSnapshot.email_deliveries),
        issue: null,
        can_resend: false,
        can_edit_contact: false,
      };
    });
  }

  async getAppointmentCalendarLinks(appointment_id: Id): Promise<CalendarLinks> {
    const links = this.calendarHandoffs.get(appointment_id);
    if (!links) {
      throw new InstructorCommandApiError(
        409,
        "CALENDAR_LINKS_UNAVAILABLE",
        "Calendar options are unavailable for this appointment.",
      );
    }
    return clone(links);
  }

  async downloadCalendarIcs(calendar_url: string): Promise<Blob> {
    const documentUrl = typeof window === "undefined" ? "http://mock.local/" : window.location.href;
    const trustedUrl = trustedInstructorCalendarUrl(calendar_url, "/api", documentUrl);
    const path = new URL(trustedUrl).pathname;
    const available = [...this.calendarHandoffs.values()].some((links) => links.ics === path);
    if (!available) {
      throw new InstructorCommandApiError(
        409,
        "CALENDAR_LINKS_UNAVAILABLE",
        "Calendar options are unavailable for this appointment.",
      );
    }
    return new Blob(["BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"], { type: "text/calendar" });
  }

  async requestAlternatives(request_id: Id, preferred_slot_ids: Id[]): Promise<LessonRequest> {
    return this.updateRequest(request_id, (request) => ({
      ...request,
      status: "needs_alternatives",
      alternative_slots: request.alternative_slots.filter((slot) => preferred_slot_ids.includes(slot.id)),
    }));
  }

  async saveRules(rules: SchedulerRules): Promise<SchedulerRules> {
    this.currentSnapshot = {
      ...this.currentSnapshot,
      rules: clone(rules),
    };
    return clone(rules);
  }

  private updateRequest(request_id: Id, update: (request: LessonRequest) => LessonRequest): LessonRequest {
    let updated: LessonRequest | undefined;
    this.currentSnapshot = {
      ...this.currentSnapshot,
      requests: this.currentSnapshot.requests.map((request) => {
        if (request.id !== request_id) return request;
        updated = update(request);
        return updated;
      }),
    };

    if (!updated) {
      throw new Error(`Request ${request_id} was not found.`);
    }

    return clone(updated);
  }

  private updateEmailDelivery(
    delivery_id: Id,
    update: (delivery: CoachEmailDelivery) => CoachEmailDelivery,
  ): CoachEmailDelivery {
    let updated: CoachEmailDelivery | undefined;
    this.currentSnapshot = {
      ...this.currentSnapshot,
      email_deliveries: this.currentSnapshot.email_deliveries.map((delivery) => {
        if (delivery.id !== delivery_id) return delivery;
        updated = update(delivery);
        return updated;
      }),
    };
    if (!updated) {
      throw new InstructorCommandApiError(404, "EMAIL_DELIVERY_NOT_FOUND", "Email delivery was not found.");
    }
    return clone(updated);
  }
}

export class HttpInstructorCommandApi implements InstructorCommandApi {
  readonly authMode = "live" as const;

  private readonly baseUrl: string;
  private readonly authStore: InstructorAuthStore;

  constructor(baseUrl = "/api", authStore: InstructorAuthStore = createBrowserInstructorAuthStore()) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.authStore = authStore;
  }

  getStoredSession(): InstructorAuthSession | null {
    return this.authStore.get();
  }

  async validateSession(): Promise<InstructorAuthSession["coach"]> {
    const response = await fetch(`${this.baseUrl}/auth/coach/session`, authenticatedInit("GET", this.authStore.get()));
    const session = await parseResponse<CoachSessionResponse>(response, this.authStore);
    if (!session.authenticated) {
      this.authStore.clear();
      throw new InstructorCommandApiError(401, "AUTHENTICATION_REQUIRED", "Coach authentication is required.");
    }
    return session.coach;
  }

  async signIn(credentials: InstructorAuthCredentials): Promise<InstructorAuthSession> {
    const response = await fetch(`${this.baseUrl}/auth/coach/sign-in`, requestInit("POST", credentials));
    const session = await parseResponse<InstructorAuthSession>(response, this.authStore);
    this.authStore.set(session);
    return session;
  }

  async signOut(): Promise<void> {
    const session = this.authStore.get();
    this.authStore.clear();
    if (!session) return;

    const response = await fetch(`${this.baseUrl}/auth/coach/sign-out`, requestInit("POST", {}, session));
    if (!response.ok && response.status !== 404) {
      throw await toApiError(response);
    }
  }

  clearSession(): void {
    this.authStore.clear();
  }

  async getSnapshot(_instructor_id: Id, date: string): Promise<CommandCenterSnapshot> {
    return this.get<CommandCenterSnapshot>(`/coach/command-center/snapshot?date=${encodeURIComponent(date)}`);
  }

  async findAvailableSlots(request: BookingRequestDTO): Promise<TimeSlotDTO[]> {
    const response = await this.post<CoachAvailabilityResponse>("/coach/availability", {
      client_name: request.client_name,
      client_address: request.location.address,
      requested_date: request.requested_date,
      duration_mins: request.duration_mins,
      is_instructor_facility: request.location.is_instructor_facility,
    });
    return response.available_slots.map((slot, index) => mapAvailabilitySlot(slot, response.location, index));
  }

  bookLesson(payload: BookLessonPayload): Promise<BookLessonResponse> {
    return this.post("/coach/bookings", {
      client_name: payload.client_name,
      client_address: payload.location_address,
      start_time: payload.start_time_iso,
      duration_mins: payload.duration_mins,
      is_instructor_facility: payload.is_instructor_facility ?? false,
      ...(payload.idempotency_key ? { idempotency_key: payload.idempotency_key } : {}),
    });
  }

  confirmRequest(request_id: Id): Promise<CoachLessonRequestConfirmResponse> {
    return this.post(`/coach/lesson-requests/${encodeURIComponent(request_id)}/confirm`, {});
  }

  getEmailDeliveries(): Promise<CoachEmailDelivery[]> {
    return this.get("/coach/email-deliveries");
  }

  resendEmailDelivery(delivery_id: Id): Promise<CoachEmailDelivery> {
    return this.postWithoutBody(`/coach/email-deliveries/${encodeURIComponent(delivery_id)}/resend`);
  }

  updateEmailDeliveryContact(delivery_id: Id, email: string): Promise<CoachEmailDelivery> {
    return this.put(`/coach/email-deliveries/${encodeURIComponent(delivery_id)}/contact`, { email });
  }

  getAppointmentCalendarLinks(appointment_id: Id): Promise<CalendarLinks> {
    return this.get(`/coach/appointments/${encodeURIComponent(appointment_id)}/calendar-links`);
  }

  async downloadCalendarIcs(calendar_url: string): Promise<Blob> {
    const target = trustedInstructorCalendarUrl(calendar_url, this.baseUrl, browserDocumentUrl(this.baseUrl));
    const response = await fetch(target, {
      method: "GET",
      headers: { ...requestHeaders(this.authStore.get()), accept: "text/calendar" },
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
    });
    if (!response.ok) {
      const error = await toApiError(response);
      if (error.isAuthFailure) this.authStore.clear();
      throw error;
    }
    const contentType = response.headers.get("content-type")?.split(";")[0].trim().toLowerCase();
    if (contentType !== "text/calendar") {
      throw new InstructorCommandApiError(
        502,
        "INVALID_CALENDAR_RESPONSE",
        "Calendar download failed. Please try again.",
      );
    }
    return response.blob();
  }

  requestAlternatives(_request_id: Id, _preferred_slot_ids: Id[]): Promise<CoachLessonRequestActionResponse> {
    return this.post(`/coach/lesson-requests/${encodeURIComponent(_request_id)}/alternatives`, {
      preferred_slot_ids: _preferred_slot_ids,
    });
  }

  saveRules(rules: SchedulerRules): Promise<SchedulerRules> {
    return this.put("/coach/settings", rules);
  }

  private async get<T>(path: string): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, authenticatedInit("GET", this.authStore.get()));
    return parseResponse<T>(response, this.authStore);
  }

  private async post<T>(path: string, body: unknown): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, requestInit("POST", body, this.authStore.get()));
    return parseResponse<T>(response, this.authStore);
  }

  private async postWithoutBody<T>(path: string): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, noBodyRequestInit("POST", this.authStore.get()));
    return parseResponse<T>(response, this.authStore);
  }

  private async put<T>(path: string, body: unknown): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, requestInit("PUT", body, this.authStore.get()));
    return parseResponse<T>(response, this.authStore);
  }

}

export const createInstructorCommandApi = (
  environment: InstructorDataModeEnvironment = import.meta.env ?? {},
): InstructorCommandApi => {
  const config = resolveInstructorDataMode(environment);
  return config.mode === "mock"
    ? new MockInstructorCommandApi()
    : new HttpInstructorCommandApi(config.apiBaseUrl);
};

export type InstructorAuthStore = {
  get(): InstructorAuthSession | null;
  set(session: InstructorAuthSession): void;
  clear(): void;
};

type ApiErrorResponse = {
  error?: {
    code?: string;
    message?: string;
    details?: unknown;
  };
};

export class InstructorCommandApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details?: unknown,
  ) {
    super(message);
    this.name = "InstructorCommandApiError";
  }

  get isAuthFailure(): boolean {
    return this.status === 401 || this.status === 403;
  }
}

type CoachAvailabilityResponse = {
  status: "PRESENTING";
  client_name: string;
  requested_date: string;
  duration_mins: number;
  location: {
    address: string;
    lat: number;
    lng: number;
    is_instructor_facility: boolean;
  };
  available_slots: Array<{
    start_iso: string;
    end_iso: string;
    display: string;
    travel_time_mins: number;
    buffer_mins: number;
    distance_miles: number;
  }>;
};

type CoachSessionResponse = {
  authenticated: boolean;
  coach: InstructorAuthSession["coach"];
};

function requestInit(method: "POST" | "PUT", body: unknown, session?: InstructorAuthSession | null): RequestInit {
  return {
    method,
    headers: requestHeaders(session),
    body: JSON.stringify(body),
  };
}

function authenticatedInit(method: "GET", session: InstructorAuthSession | null): RequestInit {
  return {
    method,
    headers: requestHeaders(session),
  };
}

function noBodyRequestInit(method: "POST", session: InstructorAuthSession | null): RequestInit {
  return {
    method,
    headers: requestHeaders(session),
  };
}

function requestHeaders(session?: InstructorAuthSession | null): HeadersInit {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (session?.access_token) {
    headers.authorization = `Bearer ${session.access_token}`;
  }
  return headers;
}

async function parseResponse<T>(response: Response, authStore?: InstructorAuthStore): Promise<T> {
  if (!response.ok) {
    const error = await toApiError(response);
    if (error.isAuthFailure) {
      authStore?.clear();
    }
    throw error;
  }

  return response.json() as Promise<T>;
}

async function toApiError(response: Response): Promise<InstructorCommandApiError> {
  let payload: ApiErrorResponse | null = null;
  try {
    payload = (await response.json()) as ApiErrorResponse;
  } catch {
    payload = null;
  }

  const code = payload?.error?.code ?? "REQUEST_FAILED";
  const message = payload?.error?.message ?? `Instructor API request failed with ${response.status}.`;
  return new InstructorCommandApiError(response.status, code, message, payload?.error?.details);
}

function mapAvailabilitySlot(
  slot: CoachAvailabilityResponse["available_slots"][number],
  location: CoachAvailabilityResponse["location"],
  index: number,
): TimeSlotDTO {
  return {
    id: `slot_${slot.start_iso.replace(/[^0-9]/g, "")}_${index}`,
    start_time: slot.start_iso,
    end_time: slot.end_iso,
    travel_time_mins: slot.travel_time_mins,
    buffer_mins: slot.buffer_mins,
    distance_miles: slot.distance_miles,
    target_location: location,
  };
}

export function createMemoryInstructorAuthStore(initialSession: InstructorAuthSession | null = null): InstructorAuthStore {
  let session = initialSession ? clone(initialSession) : null;
  return {
    get: () => (session ? clone(session) : null),
    set: (nextSession) => {
      session = clone(nextSession);
    },
    clear: () => {
      session = null;
    },
  };
}

function createBrowserInstructorAuthStore(): InstructorAuthStore {
  if (typeof window === "undefined" || !window.localStorage) {
    return createMemoryInstructorAuthStore();
  }

  return {
    get: () => readStoredSession(window.localStorage.getItem(AUTH_STORAGE_KEY)),
    set: (session) => {
      // MVP persistence keeps local development and CI demos stable across reloads;
      // the backend remains responsible for token lifetime and revocation.
      window.localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(session));
    },
    clear: () => {
      window.localStorage.removeItem(AUTH_STORAGE_KEY);
    },
  };
}

function readStoredSession(raw: string | null): InstructorAuthSession | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<InstructorAuthSession>;
    if (parsed.token_type === "bearer" && typeof parsed.access_token === "string" && parsed.coach) {
      return parsed as InstructorAuthSession;
    }
  } catch {
    return null;
  }
  return null;
}

function nextMockUpdatedAt(deliveries: readonly CoachEmailDelivery[]): string {
  const newest = deliveries.reduce((latest, delivery) => Math.max(latest, Date.parse(delivery.updated_at)), 0);
  return new Date(newest + 1_000).toISOString();
}

function isMockEmailAddress(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim());
}

function addMinutes(isoDateTime: string, minutes: number): string {
  const [datePart, timePart] = isoDateTime.split("T");
  const [hour = 0, minute = 0, second = 0] = timePart.split(":").map(Number);
  const date = new Date(`${datePart}T00:00:00`);
  date.setHours(hour, minute + minutes, second, 0);
  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, "0");
  const day = `${date.getDate()}`.padStart(2, "0");
  const nextHour = `${date.getHours()}`.padStart(2, "0");
  const nextMinute = `${date.getMinutes()}`.padStart(2, "0");
  const nextSecond = `${date.getSeconds()}`.padStart(2, "0");
  return `${year}-${month}-${day}T${nextHour}:${nextMinute}:${nextSecond}`;
}

function addDays(isoDateTime: string, days: number): string {
  const date = new Date(`${isoDateTime}${hasTimezone(isoDateTime) ? "" : "Z"}`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().replace(/\.000Z$/, "");
}

function minutesBetweenIso(startIso: string, endIso: string): number {
  const start = new Date(`${startIso}${hasTimezone(startIso) ? "" : "Z"}`);
  const end = new Date(`${endIso}${hasTimezone(endIso) ? "" : "Z"}`);
  return Math.round((end.getTime() - start.getTime()) / 60_000);
}

function hasTimezone(value: string): boolean {
  return /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value);
}

function mockUtcIso(value: string): string {
  return new Date(`${value}${hasTimezone(value) ? "" : "Z"}`).toISOString();
}

function mockCalendarLinks(
  anchorAppointmentId: Id,
  occurrences: Array<{ startIso: string; endIso: string }>,
): CalendarLinks {
  const google_events = occurrences.map((occurrence) => {
    const start = mockUtcIso(occurrence.startIso);
    const end = mockUtcIso(occurrence.endIso);
    const query = new URLSearchParams({
      action: "TEMPLATE",
      text: "Appointment",
      dates: `${start.replace(/[-:]/g, "").replace(".000", "")}/${end.replace(/[-:]/g, "").replace(".000", "")}`,
      details: "Confirmed appointment.",
    });
    return {
      start_iso: start,
      end_iso: end,
      url: `https://calendar.google.com/calendar/render?${query.toString()}`,
    };
  });
  const download = `/api/coach/appointments/${encodeURIComponent(anchorAppointmentId)}/calendar.ics`;
  return {
    google: google_events[0].url,
    google_events,
    apple: download,
    ics: download,
  };
}

function browserDocumentUrl(apiBase: string): string {
  if (typeof window !== "undefined" && window.location) return window.location.href;
  try {
    return `${new URL(apiBase).origin}/`;
  } catch {
    return "http://localhost/";
  }
}
