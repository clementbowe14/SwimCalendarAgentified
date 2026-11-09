import type {
  AvailabilitySearchResponse,
  ClientInviteEnvelope,
  LessonRequest,
  SchedulerSlot,
} from "../api/types.js";

export const inviteEnvelope: ClientInviteEnvelope = {
  invite_token: "invite_sofia_0826",
  instructor_name: "Riley",
  business_name: "Swim Command",
  service_name: "Private swim lesson",
  service_location: {
    address: "Riverside facility, Austin, TX",
    lat: 30.2672,
    lng: -97.7431,
  },
  expires_at_iso: "2026-09-05T23:59:00-05:00",
  preferred_duration_mins: 45,
  allowed_durations_mins: [30, 45, 60],
  intake: {
    client_name: "Sofia Martinez",
    guardian_phone: "(512) 555-0194",
    guardian_email: "ana.martinez@example.com",
    notes: "Beginner swimmer. Prefers shallow end warmup.",
    location_type: "home",
    location: {
      address: "820 Lakeview Dr, Austin, TX",
      lat: 30.289,
      lng: -97.739,
      is_instructor_facility: false,
    },
  },
};

export const schedulerSlots: SchedulerSlot[] = [
  {
    id: "slot_2026_08_29_1230",
    start_iso: "2026-08-29T12:30:00-05:00",
    end_iso: "2026-08-29T13:15:00-05:00",
    display: "Sat, Aug 29 at 12:30 PM",
    location_address: "820 Lakeview Dr, Austin, TX",
    travel_time_mins: 12,
    buffer_mins: 10,
    distance_miles: 2.1,
    previous_event: "Maya ends at 11:45 AM",
    next_event: "Open until 2:00 PM",
    safety_reason: "Travel clears at 11:57, buffer clears at 12:07, then rounds up to 12:30.",
    recurrence_available: true,
    recurrence: "single",
    recurrence_count: 1,
  },
  {
    id: "slot_2026_08_27_1600",
    start_iso: "2026-08-27T16:00:00-05:00",
    end_iso: "2026-08-27T16:45:00-05:00",
    display: "Thu, Aug 27 at 4:00 PM",
    location_address: "Riverside facility",
    travel_time_mins: 0,
    buffer_mins: 10,
    distance_miles: 0,
    previous_event: "Previous lesson ends at Riverside",
    next_event: "Next lesson starts at 5:30 PM",
    safety_reason: "Same-facility booking removes travel and keeps the 10 minute reset buffer.",
    recurrence_available: true,
    recurrence: "single",
    recurrence_count: 1,
  },
  {
    id: "slot_2026_08_29_1515",
    start_iso: "2026-08-29T15:15:00-05:00",
    end_iso: "2026-08-29T16:00:00-05:00",
    display: "Sat, Aug 29 at 3:15 PM",
    location_address: "820 Lakeview Dr, Austin, TX",
    travel_time_mins: 8,
    buffer_mins: 10,
    distance_miles: 1.4,
    previous_event: "Hillcrest block ends at 2:45 PM",
    next_event: null,
    safety_reason: "Drive plus buffer clears at 3:03, then rounds up to 3:15.",
    recurrence_available: false,
    recurrence: "single",
    recurrence_count: 1,
  },
];

export const slotResponse: AvailabilitySearchResponse = {
  status: "PRESENTING",
  dialogue_response:
    "Here are the available swim lesson times accounting for the instructor schedule, travel buffers, and 15 minute rounding.",
  slots: schedulerSlots,
  unavailable_windows: [
    {
      id: "weekday_after_5",
      label: "Tue after 5 PM",
      reason: "Travel from Hillcrest plus the 10 minute buffer crosses the end of Sofia's preferred window.",
    },
  ],
};

export const createRequestedLesson = (slot: SchedulerSlot): LessonRequest => ({
  id: "request_sofia_001",
  invite_token: inviteEnvelope.invite_token,
  slot,
  status: "requested",
  requested_at_iso: "2026-08-27T13:30:00-05:00",
  alternative_slots: [],
  confirmation: null,
  notifications: [],
});

export const createConfirmedLesson = (slot: SchedulerSlot): LessonRequest => ({
  ...createRequestedLesson(slot),
  status: "confirmed",
  confirmation: {
    appointment_id: "app_sofia_1230",
    appointment_ids: ["app_sofia_1230"],
    confirmed_at_iso: "2026-08-27T13:35:00-05:00",
    message: `Riley confirmed ${slot.display}.`,
    calendar_links: {
      google: "https://calendar.google.com/calendar/render?action=TEMPLATE",
      google_events: [{ start_iso: slot.start_iso, end_iso: slot.end_iso, url: "https://calendar.google.com/calendar/render?action=TEMPLATE" }],
      apple: "/calendar/app_sofia_1230.ics",
      ics: "/calendar/app_sofia_1230.ics",
    },
  },
});
