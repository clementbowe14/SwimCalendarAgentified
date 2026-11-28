export type LessonLocationType = "home" | "facility";
export type BookingRecurrence = "single" | "weekly";
export type RequestStatus =
  | "draft"
  | "searching"
  | "slots_ready"
  | "requested"
  | "confirmed"
  | "needs_new_times"
  | "expired";
export type CalendarProvider = "google" | "apple" | "ics";

export type DayKey = "monday" | "tuesday" | "wednesday" | "thursday" | "friday" | "saturday" | "sunday";

export type LocationInfo = {
  address: string;
  lat: number | null;
  lng: number | null;
  is_instructor_facility: boolean;
};

export type AvailabilityWindow = {
  id: string;
  days: DayKey[];
  start_time: string;
  end_time: string;
};

export type ClientIntake = {
  client_name: string;
  guardian_phone: string;
  guardian_email: string;
  notes: string;
  location_type: LessonLocationType;
  location: LocationInfo;
};

export type ClientAvailabilitySearchRequest = {
  client_name: string;
  client_address?: string;
  is_instructor_facility: boolean;
  duration_mins: number;
  limit?: number;
  windows?: Array<Omit<AvailabilityWindow, "id">>;
};

export type AvailabilitySlot = {
  id: string;
  start_iso: string;
  end_iso: string;
  display: string;
  location_address: string;
};

export type SchedulerSlot = AvailabilitySlot & {
  recurrence_available: boolean;
  recurrence: BookingRecurrence;
  recurrence_count: number;
};

export type AvailabilitySearchResponse = {
  start_date: string;
  end_date: string;
  duration_mins: number;
  slots: AvailabilitySlot[];
};

export type LessonRequest = {
  notifications: NotificationStatus[];
  id: string;
  invite_token: string;
  slot: SchedulerSlot;
  status: RequestStatus;
  requested_at_iso: string;
  alternative_slots: SchedulerSlot[];
  confirmation: LessonConfirmation | null;
};

export type LessonConfirmation = {
  appointment_id: string;
  appointment_ids: string[];
  message: string;
  confirmed_at_iso: string;
  calendar_links: Record<CalendarProvider, string> & { google_events: Array<{ start_iso: string; end_iso: string; url: string }> };
};

export type NotificationStatus = {
  kind: "invite" | "confirmation";
  channel: "sms" | "email";
  status: "queued" | "processing" | "accepted" | "delivered" | "failed" | "skipped";
  updated_at: string;
};

export type ClientInviteEnvelope = {
  invite_token: string;
  instructor_name: string;
  business_name: string;
  service_name: string | null;
  service_location: {
    address: string;
    lat: number;
    lng: number;
  } | null;
  expires_at_iso: string;
  intake: ClientIntake;
  preferred_duration_mins: number;
  allowed_durations_mins: number[];
};

export interface ClientInviteApi {
  getInvite(inviteToken: string): Promise<ClientInviteEnvelope>;
  submitIntake(inviteToken: string, intake: ClientIntake): Promise<ClientInviteEnvelope>;
  findAvailableSlots(inviteToken: string, request: ClientAvailabilitySearchRequest): Promise<AvailabilitySearchResponse>;
  requestLessonSlot(inviteToken: string, slotId: string, idempotencyKey?: string): Promise<LessonRequest>;
  getLessonRequestStatus(requestId: string): Promise<LessonRequest>;
  selectAlternativeSlot(requestId: string, slotId: string, idempotencyKey?: string): Promise<LessonRequest>;
  getCalendarLinks(requestId: string): Promise<LessonConfirmation["calendar_links"]>;
}
