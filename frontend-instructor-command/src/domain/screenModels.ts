import type {
  BookingRequestDTO,
  Client,
  CommandCenterSnapshot,
  LessonRequest,
  LocationInfo,
  ScheduleItem,
  TimeSlotDTO,
} from "../api/types.js";
import { formatShortDate, formatTimeRange, minutesBetween } from "../lib/format.js";

export type AgendaRow = {
  id: string;
  tone: "lesson" | "travel" | "buffer" | "break" | "personal" | "unavailable" | "open";
  kind: ScheduleItem["kind"];
  time: string;
  title: string;
  detail: string;
  status?: string;
};

export type RequestSummary = {
  totalPending: number;
  clear: number;
  atRisk: number;
};

export type ScreenReadiness = {
  screen: string;
  isReadyForBackend: boolean;
  requiredApiMethods: string[];
  missingApiContracts: string[];
};

export function buildAgendaRows(schedule: ScheduleItem[]): AgendaRow[] {
  return schedule.map((item) => ({
    id: item.id,
    tone: agendaTone(item.kind),
    kind: item.kind,
    time: formatTimeRange(item.start_time, item.end_time),
    title: item.title,
    detail: itemDetail(item),
    status: item.status ?? undefined,
  }));
}

export function buildFindSlotsRequest(
  instructorId: string,
  client: Client,
  requestedDate: string,
): BookingRequestDTO {
  return {
    instructor_id: instructorId,
    client_id: client.id,
    client_name: client.name,
    location: client.default_location,
    requested_date: requestedDate,
    duration_mins: client.lesson_duration_mins,
  };
}

export function slotSafetyFacts(slot: TimeSlotDTO): string {
  const location = slot.target_location.is_instructor_facility ? "Facility" : `${slot.distance_miles.toFixed(1)} mi`;
  return `Travel ${slot.travel_time_mins} min | Buffer ${slot.buffer_mins} min | ${location}`;
}

export function slotExplanation(_slot: TimeSlotDTO): string {
  return "Starts on a 15-minute boundary after travel and required buffer time.";
}

export function summarizeRequests(requests: LessonRequest[]): RequestSummary {
  const pending = requests.filter((request) => request.status === "pending");
  return {
    totalPending: pending.length,
    clear: pending.filter((request) => request.risk === "clear").length,
    atRisk: pending.filter((request) => request.risk === "at_risk").length,
  };
}

export function findFeaturedClient(snapshot: CommandCenterSnapshot, clientId: string): Client {
  return snapshot.clients.find((client) => client.id === clientId) ?? snapshot.clients[0] ?? fallbackClient(snapshot);
}

export function nextLessonForClient(snapshot: CommandCenterSnapshot, clientId: string): ScheduleItem | undefined {
  return snapshot.schedule.find((item) => item.client_id === clientId && item.kind === "lesson");
}

export function clientAvailabilityLabel(client: Client): string {
  return client.availability
    .map((window) => `${window.days.join(", ")} ${window.start_time}-${window.end_time}`)
    .join(" | ");
}

export function buildReadinessReport(): ScreenReadiness[] {
  return [
    {
      screen: "Today schedule",
      isReadyForBackend: false,
      requiredApiMethods: ["getSnapshot"],
      missingApiContracts: ["schedule response does not include travel, buffer, open-gap, or computed next-lesson summary rows"],
    },
    {
      screen: "Availability Finder/New Client",
      isReadyForBackend: false,
      requiredApiMethods: ["findAvailableSlots", "bookLesson"],
      missingApiContracts: ["new-client intake persistence", "server-issued slot hold IDs for coach-created bookings"],
    },
    {
      screen: "Client Detail",
      isReadyForBackend: false,
      requiredApiMethods: ["getSnapshot"],
      missingApiContracts: ["coach client notes, availability, lesson history, and progress detail fields"],
    },
    {
      screen: "Settings",
      isReadyForBackend: true,
      requiredApiMethods: ["getSnapshot", "getSchedulingPolicy", "saveSchedulingPolicy"],
      missingApiContracts: [],
    },
    {
      screen: "Request management",
      isReadyForBackend: false,
      requiredApiMethods: ["confirmRequest", "requestAlternatives"],
      missingApiContracts: ["request alternatives mutation"],
    },
  ];
}

export function scheduleUtilization(schedule: ScheduleItem[]): number {
  const lessonMinutes = schedule
    .filter((item) => item.kind === "lesson")
    .reduce((sum, item) => sum + minutesBetween(item.start_time, item.end_time), 0);
  const availableMinutes = 10 * 60;
  return Math.round((lessonMinutes / availableMinutes) * 100);
}

export function requestDateLabel(request: LessonRequest): string {
  return `${formatShortDate(request.requested_slot.start_time)} ${formatTimeRange(
    request.requested_slot.start_time,
    request.requested_slot.end_time,
  )}`;
}

function agendaTone(kind: ScheduleItem["kind"]): AgendaRow["tone"] {
  if (kind === "lesson") return "lesson";
  if (kind === "travel") return "travel";
  if (kind === "buffer") return "buffer";
  if (kind === "break") return "break";
  if (kind === "personal") return "personal";
  if (kind === "unavailable") return "unavailable";
  return "open";
}

function itemDetail(item: ScheduleItem): string {
  if (item.kind === "travel") return `${item.travel_time_mins ?? 0} min drive | ${item.note ?? item.location?.address ?? "Travel"}`;
  if (item.kind === "buffer") return `${item.buffer_mins ?? 0} min reset | ${item.note ?? "Protected transition"}`;
  if (item.kind === "open_gap") return item.note ?? "Safe booking space";
  if (item.kind === "break") return `Break${item.location ? ` | ${item.location.address}` : ""}`;
  if (item.kind === "personal") return `Personal event${item.location ? ` | ${item.location.address}` : ""}`;
  if (item.kind === "unavailable") return "Unavailable";
  return `${item.location?.address ?? "Location unavailable"}${item.note ? ` | ${item.note}` : ""}`;
}

function fallbackClient(snapshot: CommandCenterSnapshot): Client {
  return {
    id: "client_new",
    name: "New client",
    phone: "",
    email: "",
    default_location: {
      address: snapshot.instructor.main_address,
      lat: snapshot.instructor.lat,
      lng: snapshot.instructor.lng,
      is_instructor_facility: true,
    },
    location_mode: "instructor_facility",
    availability: [],
    lesson_duration_mins: 60,
    notes: "",
    history_summary: "No scheduled clients loaded for this date",
    progress_focus: "",
  };
}
