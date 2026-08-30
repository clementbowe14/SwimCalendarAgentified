import { candidateSlots, snapshot } from "./mockData.js";
import type {
  BookLessonPayload,
  BookLessonResponse,
  BookingRequestDTO,
  CommandCenterSnapshot,
  Id,
  InstructorCommandApi,
  LessonRequest,
  SchedulerRules,
  TimeSlotDTO,
} from "./types.js";

function clone<T>(value: T): T {
  return structuredClone(value);
}

function sameDate(isoDateTime: string, date: string): boolean {
  return isoDateTime.slice(0, 10) === date;
}

export class MockInstructorCommandApi implements InstructorCommandApi {
  private currentSnapshot: CommandCenterSnapshot = clone(snapshot);

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
    return {
      success: true,
      appointment_id,
      message: `Successfully booked swim lesson for ${payload.client_name}.`,
    };
  }

  async approveRequest(request_id: Id, slot_id: Id): Promise<LessonRequest> {
    return this.updateRequest(request_id, (request) => {
      const selectedSlot = [request.requested_slot, ...request.alternative_slots].find((slot) => slot.id === slot_id);
      return {
        ...request,
        status: "approved",
        requested_slot: selectedSlot ?? request.requested_slot,
      };
    });
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
}

export class HttpInstructorCommandApi implements InstructorCommandApi {
  private readonly baseUrl: string;

  constructor(baseUrl = "/api") {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
  }

  async getSnapshot(_instructor_id: Id, date: string): Promise<CommandCenterSnapshot> {
    const response = await this.get<CoachScheduleResponse>(`/coach/schedule?date=${encodeURIComponent(date)}`);
    return mapScheduleToSnapshot(response);
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

  approveRequest(_request_id: Id, _slot_id: Id): Promise<LessonRequest> {
    throw new InstructorCommandApiError(501, "ENDPOINT_NOT_AVAILABLE", "Coach request approval is not exposed by the current API contract.");
  }

  requestAlternatives(_request_id: Id, _preferred_slot_ids: Id[]): Promise<LessonRequest> {
    throw new InstructorCommandApiError(501, "ENDPOINT_NOT_AVAILABLE", "Coach request alternatives are not exposed by the current API contract.");
  }

  saveRules(_rules: SchedulerRules): Promise<SchedulerRules> {
    throw new InstructorCommandApiError(501, "ENDPOINT_NOT_AVAILABLE", "Scheduler rule mutation is not exposed by the current API contract.");
  }

  private async get<T>(path: string): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`);
    return parseResponse<T>(response);
  }

  private async post<T>(path: string, body: unknown): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, requestInit("POST", body));
    return parseResponse<T>(response);
  }

}

export const createInstructorCommandApi = (): InstructorCommandApi => {
  const baseUrl = readFrontendEnv("VITE_INSTRUCTOR_API_URL");
  return baseUrl ? new HttpInstructorCommandApi(baseUrl) : new MockInstructorCommandApi();
};

export const instructorCommandApi: InstructorCommandApi = createInstructorCommandApi();

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
}

type CoachScheduleResponse = {
  instructor: {
    id: Id;
    name: string;
    business_name?: string;
    main_address: string;
    lat: number;
    lng: number;
    work_start_time: string;
    work_end_time: string;
  };
  date: string;
  appointments: Array<{
    id: Id;
    title: string;
    start_time: string;
    end_time: string;
    location_address: string;
    lat: number;
    lng: number;
    is_break: boolean;
  }>;
};

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

function requestInit(method: "POST" | "PUT", body: unknown): RequestInit {
  return {
    method,
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  };
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw await toApiError(response);
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

function mapScheduleToSnapshot(response: CoachScheduleResponse): CommandCenterSnapshot {
  const schedule = response.appointments.map(mapAppointmentToScheduleItem);
  return {
    instructor: {
      id: response.instructor.id,
      name: response.instructor.name,
      business_name: response.instructor.business_name ?? "Swim Command",
      main_address: response.instructor.main_address,
      lat: response.instructor.lat,
      lng: response.instructor.lng,
      work_start_time: response.instructor.work_start_time,
      work_end_time: response.instructor.work_end_time,
    },
    date: response.date,
    clients: clientsFromSchedule(schedule),
    schedule,
    requests: [],
    rules: defaultSchedulerRules(),
  };
}

function mapAppointmentToScheduleItem(appointment: CoachScheduleResponse["appointments"][number]): CommandCenterSnapshot["schedule"][number] {
  const isLesson = !appointment.is_break;
  return {
    id: appointment.id,
    kind: appointment.is_break ? "break" : "lesson",
    client_id: isLesson ? clientIdFromTitle(appointment.title) : undefined,
    title: isLesson ? appointment.title.replace(/^Lesson with\s+/i, "") : appointment.title,
    start_time: appointment.start_time,
    end_time: appointment.end_time,
    location: {
      address: appointment.location_address,
      lat: appointment.lat,
      lng: appointment.lng,
      is_instructor_facility: appointment.is_break,
    },
    status: isLesson ? "scheduled" : undefined,
  };
}

function clientsFromSchedule(schedule: CommandCenterSnapshot["schedule"]): CommandCenterSnapshot["clients"] {
  const clients = new Map<string, CommandCenterSnapshot["clients"][number]>();
  schedule
    .filter((item) => item.kind === "lesson")
    .forEach((item) => {
      const id = item.client_id ?? clientIdFromTitle(item.title);
      clients.set(id, {
        id,
        name: item.title,
        phone: "",
        email: "",
        default_location: item.location,
        location_mode: item.location.is_instructor_facility ? "instructor_facility" : "client_home",
        availability: [],
        lesson_duration_mins: minutesBetweenIso(item.start_time, item.end_time),
        notes: "",
        history_summary: "Loaded from schedule",
        progress_focus: "",
      });
    });

  return [...clients.values()];
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

function defaultSchedulerRules(): SchedulerRules {
  return {
    rounding_increment_mins: 15,
    post_break_buffer_mins: 15,
    distance_buffer_tiers: [
      { id: "near", label: "Under 5 mi", min_miles: 0, max_miles: 4.999, buffer_mins: 10 },
      { id: "medium", label: "5-10 mi", min_miles: 5, max_miles: 10, buffer_mins: 20 },
      { id: "far", label: "Over 10 mi", min_miles: 10.001, max_miles: null, buffer_mins: 30 },
    ],
    payments: {
      enabled_methods: [],
      cancellation_window_hours: 24,
      require_payment_method_for_new_clients: false,
    },
  };
}

function clientIdFromTitle(title: string): string {
  const name = title.replace(/^Lesson with\s+/i, "").trim().toLowerCase();
  const slug = name.replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  return `client_${slug || "scheduled"}`;
}

function minutesBetweenIso(start: string, end: string): number {
  return Math.max(15, Math.round((new Date(end).getTime() - new Date(start).getTime()) / 60000));
}

function readFrontendEnv(name: string): string | undefined {
  const maybeImportMeta = import.meta as ImportMeta & { env?: Record<string, string | undefined> };
  return maybeImportMeta.env?.[name];
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
