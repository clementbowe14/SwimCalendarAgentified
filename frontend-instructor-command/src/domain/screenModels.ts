import type {
  AvailabilityDateRange,
  AvailabilityRangeRequest,
  BookingRequestDTO,
  BlockerScheduleItem,
  Client,
  CommandCenterSnapshot,
  FinderClient,
  LessonRequest,
  LocationInfo,
  ScheduleItem,
  TimeSlotDTO,
} from "../api/types.js";
import { formatShortDate, formatTimeRange, minutesBetween } from "../lib/format.js";
import { PROVIDER_WEEKDAYS } from "./providerSettings.js";

export type AgendaRow = {
  id: string;
  tone: ScheduleItem["kind"] | "open";
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

export function buildAgendaRows(schedule: ScheduleItem[], timeZone?: string): AgendaRow[] {
  return schedule.map((item) => ({
    id: item.id,
    tone: agendaTone(item.kind),
    kind: item.kind,
    time: formatTimeRange(item.start_time, item.end_time, timeZone),
    title: item.kind === "calendar_busy" ? "Busy on Google Calendar" : item.title,
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

export function buildAvailabilityRangeRequest(client: FinderClient, range: AvailabilityDateRange): AvailabilityRangeRequest {
  if (!client.default_location) throw new Error("Save a lesson location for this client before searching.");
  const is_instructor_facility = client.location_mode === "instructor_facility";
  return {
    ...range,
    duration_mins: client.lesson_duration_mins,
    client_address: is_instructor_facility ? null : client.default_location.address,
    is_instructor_facility,
    windows: client.availability.map(({ days, start_time, end_time }) => ({
      days: days.map((day) => {
        const savedDay = day.toLowerCase();
        const weekday = PROVIDER_WEEKDAYS.find((value) => value === savedDay || value.slice(0, 3) === savedDay);
        if (!weekday) throw new Error("The client has an invalid saved availability weekday.");
        return weekday;
      }),
      start_time,
      end_time,
    })),
  };
}

export function isScheduleBlocker(item: ScheduleItem): item is BlockerScheduleItem {
  return item.kind === "break" || item.kind === "personal" || item.kind === "unavailable";
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

export function clientAvailabilityLabel(client: Pick<Client, "availability">): string {
  return client.availability
    .map((window) => `${window.days.join(", ")} ${window.start_time}-${window.end_time}`)
    .join(" | ");
}

export function buildReadinessReport(): ScreenReadiness[] {
  return [
    {
      screen: "Today schedule",
      isReadyForBackend: true,
      requiredApiMethods: ["getSnapshot", "getAgenda"],
      missingApiContracts: [],
    },
    {
      screen: "Availability Finder/New Client",
      isReadyForBackend: false,
      requiredApiMethods: ["getFinderClients", "searchAvailability", "bookLesson"],
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

export function requestDateLabel(request: LessonRequest, timeZone?: string): string {
  return `${formatShortDate(request.requested_slot.start_time, timeZone)} ${formatTimeRange(
    request.requested_slot.start_time,
    request.requested_slot.end_time,
    timeZone,
  )}`;
}

function agendaTone(kind: ScheduleItem["kind"]): AgendaRow["tone"] {
  switch (kind) {
    case "lesson": return "lesson";
    case "travel": return "travel";
    case "buffer": return "buffer";
    case "break": return "break";
    case "personal": return "personal";
    case "unavailable": return "unavailable";
    case "calendar_busy": return "calendar_busy";
    default: return "open";
  }
}

function itemDetail(item: ScheduleItem): string {
  if (item.kind === "travel") return [
    item.travel_time_mins == null ? null : `${item.travel_time_mins} min drive`,
    item.note ?? item.location?.address ?? "Travel",
  ].filter(Boolean).join(" | ");
  if (item.kind === "buffer") return [
    item.buffer_mins == null ? null : `${item.buffer_mins} min reset`,
    item.note ?? "Protected transition",
  ].filter(Boolean).join(" | ");
  if (item.kind === "calendar_busy") return "";
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
