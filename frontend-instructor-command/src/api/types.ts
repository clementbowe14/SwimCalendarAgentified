export type Id = string;

export type LessonLocationMode = "client_home" | "instructor_facility";

export type RequestStatus = "pending" | "approved" | "needs_alternatives" | "declined" | "expired";

export type RequestRisk = "clear" | "at_risk";

export type AppointmentKind = "lesson" | "travel" | "buffer" | "break" | "open_gap";

export type LessonStatus = "scheduled" | "rescheduled" | "canceled";

export type EmailDeliveryKind = "invite" | "confirmation";

export type EmailDeliveryStatus = "queued" | "processing" | "accepted" | "delivered" | "failed" | "skipped";

export type EmailDeliveryIssue =
  | "invalid_address"
  | "missing_address"
  | "temporary_failure"
  | "permanent_failure"
  | "delivery_uncertain"
  | "retries_exhausted";

export type EmailDeliverySourceState = "active" | "invite_inactive" | "appointment_inactive";

export type CoachEmailDelivery = {
  id: Id;
  kind: EmailDeliveryKind;
  status: EmailDeliveryStatus;
  updated_at: string;
  client_id?: Id | null;
  client_name: string;
  request_token?: string | null;
  issue: EmailDeliveryIssue | null;
  source_state: EmailDeliverySourceState;
  can_resend: boolean;
  can_edit_contact: boolean;
};

export type AvailabilityWindow = {
  id: Id;
  days: string[];
  start_time: string;
  end_time: string;
};

export type LocationInfo = {
  address: string;
  lat: number;
  lng: number;
  is_instructor_facility: boolean;
};

export type TimeSlotDTO = {
  id: Id;
  start_time: string;
  end_time: string;
  travel_time_mins: number;
  buffer_mins: number;
  distance_miles: number;
  target_location: LocationInfo;
  previous_event_id?: Id;
  next_event_id?: Id;
};

export type BookingRequestDTO = {
  instructor_id: Id;
  client_id: Id;
  client_name: string;
  location: LocationInfo;
  requested_date: string;
  duration_mins: number;
};

export type BookLessonPayload = {
  instructor_id: Id;
  client_id: Id;
  client_name: string;
  start_time_iso: string;
  duration_mins: number;
  location_address: string;
  is_instructor_facility?: boolean;
  idempotency_key?: string;
};

export type BookLessonResponse = {
  success: boolean;
  appointment_id: Id;
  message: string;
  calendar_links: CalendarLinks | null;
};

export type GoogleCalendarEvent = {
  start_iso: string;
  end_iso: string;
  url: string;
};

export type CalendarLinks = {
  google: string;
  google_events: GoogleCalendarEvent[];
  apple: string;
  ics: string;
};

export type InstructorAuthCredentials = {
  email: string;
  password: string;
};

export type InstructorAuthCoach = {
  instructor_id: Id;
  display_name: string;
};

export type InstructorAuthSession = {
  access_token: string;
  token_type: "bearer";
  coach: InstructorAuthCoach;
  expires_at?: string;
};

export type CoachLessonRequestActionResponse = {
  id: Id;
  status: "approved" | "needs_alternatives" | "declined";
  appointment_id?: Id | null;
  message: string;
};

export type CoachLessonRequestConfirmResponse = {
  id: Id;
  status: "confirmed";
  appointment_id?: Id | null;
  appointment_ids: Id[];
  message: string;
  calendar_links: CalendarLinks | null;
  email_delivery: CoachEmailDelivery | null;
};

export type InstructorProfile = {
  id: Id;
  name: string;
  business_name: string;
  main_address: string;
  lat: number;
  lng: number;
  work_start_time: string;
  work_end_time: string;
};

export type Client = {
  id: Id;
  name: string;
  phone: string;
  email: string;
  guardian_name?: string;
  default_location: LocationInfo;
  location_mode: LessonLocationMode;
  availability: AvailabilityWindow[];
  lesson_duration_mins: number;
  notes: string;
  history_summary: string;
  progress_focus: string;
};

export type ScheduleItem = {
  id: Id;
  kind: AppointmentKind;
  client_id?: Id;
  title: string;
  start_time: string;
  end_time: string;
  location: LocationInfo;
  travel_time_mins?: number;
  buffer_mins?: number;
  status?: LessonStatus;
  note?: string;
};

export type LessonRequest = {
  id: Id;
  client_id: Id;
  client_name: string;
  requested_slot: TimeSlotDTO;
  source: "client_invite" | "manual_intake" | "reschedule";
  status: RequestStatus;
  risk: RequestRisk;
  submitted_at: string;
  note: string;
  alternative_slots: TimeSlotDTO[];
};

export type SchedulerRules = {
  buffers_enabled: boolean;
  apply_distance_buffer_at_shift_start: boolean;
  rounding_increment_mins: 5 | 10 | 15 | 30;
  post_break_buffer_mins: number;
  distance_buffer_tiers: Array<{
    id: Id;
    label: string;
    min_miles: number;
    max_miles: number | null;
    buffer_mins: number;
  }>;
  payments: {
    enabled_methods: Array<"card" | "ach" | "cash" | "wallet">;
    cancellation_window_hours: number;
    require_payment_method_for_new_clients: boolean;
  };
};

export type CommandCenterSnapshot = {
  instructor: InstructorProfile;
  date: string;
  clients: Client[];
  schedule: ScheduleItem[];
  requests: LessonRequest[];
  email_deliveries: CoachEmailDelivery[];
  rules: SchedulerRules;
};

export type NewClientDraft = {
  client_name: string;
  phone: string;
  email: string;
  location_mode: LessonLocationMode;
  location: LocationInfo;
  availability: AvailabilityWindow[];
  duration_mins: number;
};

export type InstructorCommandApi = {
  readonly authMode: "mock" | "live";
  getStoredSession(): InstructorAuthSession | null;
  validateSession(): Promise<InstructorAuthCoach>;
  signIn(credentials: InstructorAuthCredentials): Promise<InstructorAuthSession>;
  signOut(): Promise<void>;
  clearSession(): void;
  getSnapshot(instructor_id: Id, date: string): Promise<CommandCenterSnapshot>;
  findAvailableSlots(request: BookingRequestDTO): Promise<TimeSlotDTO[]>;
  bookLesson(payload: BookLessonPayload): Promise<BookLessonResponse>;
  confirmRequest(requestId: Id, slotId?: Id): Promise<CoachLessonRequestConfirmResponse>;
  getEmailDeliveries(): Promise<CoachEmailDelivery[]>;
  resendEmailDelivery(delivery_id: Id): Promise<CoachEmailDelivery>;
  updateEmailDeliveryContact(delivery_id: Id, email: string): Promise<CoachEmailDelivery>;
  getAppointmentCalendarLinks(appointment_id: Id): Promise<CalendarLinks>;
  downloadCalendarIcs(calendar_url: string): Promise<Blob>;
  requestAlternatives(request_id: Id, preferred_slot_ids: Id[]): Promise<LessonRequest | CoachLessonRequestActionResponse>;
  saveRules(rules: SchedulerRules): Promise<SchedulerRules>;
};
