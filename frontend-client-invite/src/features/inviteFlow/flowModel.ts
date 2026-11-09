import type {
  AvailabilityWindow,
  BookingRecurrence,
  ClientAvailabilitySearchRequest,
  ClientIntake,
  DayKey,
  SchedulerSlot,
} from "../../api/types.js";

export type InviteStep = "intake" | "preferences" | "results" | "request" | "calendar";

export type InviteFlowState = {
  step: InviteStep;
  selectedSlotId: string | null;
  recurrence: BookingRecurrence;
  duration_mins: number;
  windows: AvailabilityWindow[];
};

export const weekdayOptions: DayKey[] = ["Mon", "Tue", "Wed", "Thu", "Fri"];
export const weekendOptions: DayKey[] = ["Sat", "Sun"];

export const initialAvailabilityWindows: AvailabilityWindow[] = [
  {
    id: "weekday_after_school",
    days: ["Tue", "Thu"],
    start_time: "16:00",
    end_time: "18:00",
  },
  {
    id: "weekend_late_morning",
    days: ["Sat"],
    start_time: "10:00",
    end_time: "12:00",
  },
];

export const timeWindowPresets = [
  { id: "early", label: "6 AM-8 AM", start_time: "06:00", end_time: "08:00" },
  { id: "morning", label: "8 AM-10 AM", start_time: "08:00", end_time: "10:00" },
  { id: "late_morning", label: "10 AM-12 PM", start_time: "10:00", end_time: "12:00" },
  { id: "midday", label: "12 PM-2 PM", start_time: "12:00", end_time: "14:00" },
  { id: "afternoon", label: "2 PM-4 PM", start_time: "14:00", end_time: "16:00" },
  { id: "after_school", label: "4 PM-6 PM", start_time: "16:00", end_time: "18:00" },
  { id: "evening", label: "6 PM-8 PM", start_time: "18:00", end_time: "20:00" },
  { id: "late", label: "8 PM-10 PM", start_time: "20:00", end_time: "22:00" },
];

export const summarizeAvailability = (windows: AvailabilityWindow[]): string => {
  if (windows.length === 0) {
    return "No availability selected";
  }

  return windows
    .map((window) => `${window.days.join(", ") || "No days"} ${formatTimeRange(window.start_time, window.end_time)}`)
    .join(" / ");
};

export const formatTimeRange = (startTime: string, endTime: string): string => {
  const format = (value: string) => {
    const [hoursText, minutes] = value.split(":");
    const hours = Number(hoursText);
    const suffix = hours >= 12 ? "PM" : "AM";
    const displayHour = hours % 12 || 12;
    return minutes === "00" ? `${displayHour} ${suffix}` : `${displayHour}:${minutes} ${suffix}`;
  };

  return `${format(startTime)}-${format(endTime)}`;
};

export const buildSearchRequest = (
  intake: ClientIntake,
  requestedDate: string,
  durationMins: number,
  recurrence: BookingRecurrence,
  windows: AvailabilityWindow[],
): ClientAvailabilitySearchRequest => ({
  client_name: intake.client_name,
  location: intake.location,
  requested_date: requestedDate,
  duration_mins: durationMins,
  recurrence,
  windows,
});

export const selectRecommendedSlot = (slots: SchedulerSlot[], recurrence: BookingRecurrence): SchedulerSlot | null => {
  return slots.find((slot) => recurrence === "single" || slot.recurrence_available) ?? slots[0] ?? null;
};

export const upsertWindowDays = (
  windows: AvailabilityWindow[],
  windowId: string,
  days: DayKey[],
): AvailabilityWindow[] =>
  windows.map((window) => (window.id === windowId ? { ...window, days } : window));

export const upsertWindowTime = (
  windows: AvailabilityWindow[],
  windowId: string,
  startTime: string,
  endTime: string,
): AvailabilityWindow[] =>
  windows.map((window) =>
    window.id === windowId
      ? {
          ...window,
          start_time: startTime,
          end_time: endTime,
        }
      : window,
  );
