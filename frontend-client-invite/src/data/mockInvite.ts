import type {
  AvailabilitySearchResponse,
  AvailabilitySlot,
  ClientInviteEnvelope,
  LessonRequest,
} from "../api/types.js";

export const inviteEnvelope: ClientInviteEnvelope = {
  invite_token: "invite_sofia_0826",
  instructor_name: "Riley",
  business_name: "Riley & Co.",
  service_name: "Private appointment",
  service_location: {
    address: "Riverside Studio, Austin, TX",
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
    notes: "Please call upon arrival.",
    location_type: "home",
    location: {
      address: "820 Lakeview Dr, Austin, TX",
      lat: 30.289,
      lng: -97.739,
      is_instructor_facility: false,
    },
  },
};

export const schedulerSlots: AvailabilitySlot[] = [
  {
    id: "slot_2026_08_29_1230",
    start_iso: "2026-08-29T12:30:00-05:00",
    end_iso: "2026-08-29T13:15:00-05:00",
    display: "Sat, Aug 29 at 12:30 PM",
    location_address: "820 Lakeview Dr, Austin, TX",
  },
  {
    id: "slot_2026_09_03_1600",
    start_iso: "2026-09-03T16:00:00-05:00",
    end_iso: "2026-09-03T16:45:00-05:00",
    display: "Thu, Sep 3 at 4:00 PM",
    location_address: "Riverside Studio",
  },
  {
    id: "slot_2026_09_05_1115",
    start_iso: "2026-09-05T11:15:00-05:00",
    end_iso: "2026-09-05T12:00:00-05:00",
    display: "Sat, Sep 5 at 11:15 AM",
    location_address: "820 Lakeview Dr, Austin, TX",
  },
];

export const slotResponse: AvailabilitySearchResponse = {
  start_date: "2026-08-29",
  end_date: "2026-09-11",
  duration_mins: 45,
  slots: schedulerSlots,
};

export const createRequestedLesson = (slot: AvailabilitySlot): LessonRequest => ({
  id: "request_sofia_001",
  invite_token: inviteEnvelope.invite_token,
  slot: { ...slot, recurrence_available: true, recurrence: "single", recurrence_count: 1 },
  status: "requested",
  requested_at_iso: "2026-08-27T13:30:00-05:00",
  alternative_slots: [],
  confirmation: null,
  notifications: [],
});

export const createConfirmedLesson = (slot: AvailabilitySlot): LessonRequest => ({
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
