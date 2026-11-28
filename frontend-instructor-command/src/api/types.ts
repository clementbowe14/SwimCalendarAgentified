export type Id = string;

export type LessonLocationMode = "client_home" | "instructor_facility";

export type RequestStatus = "pending" | "approved" | "needs_alternatives" | "declined" | "expired";

export type RequestRisk = "clear" | "at_risk";

export type BlockerKind = "break" | "personal" | "unavailable";

export type AppointmentKind = "lesson" | "travel" | "buffer" | BlockerKind | "open_gap";

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
  previous_event_id?: Id | null;
  next_event_id?: Id | null;
};

export type BookingRequestDTO = {
  instructor_id: Id;
  client_id: Id;
  client_name: string;
  location: LocationInfo;
  requested_date: string;
  duration_mins: number;
};

export type AvailabilityDateRange = { start_date: string; end_date: string };

export type AvailabilityRangeRequest = AvailabilityDateRange & {
  duration_mins: number;
  windows: Array<Omit<AvailabilityWindow, "id" | "days"> & { days: ProviderWeekday[] }>;
  client_address: string | null;
  is_instructor_facility: boolean;
  limit?: number;
};

export type AvailabilityRangeResult = AvailabilityDateRange & {
  duration_mins: number;
  timezone: string;
  location: LocationInfo;
  available_slots: TimeSlotDTO[];
  empty_reason: "no_working_hours" | "outside_client_windows" | "no_safe_slots" | null;
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

export type GoogleCalendarConnection = {
  provider: "google";
  state: "disconnected" | "connected" | "authorization_required";
  connected_at: string | null;
  updated_at: string | null;
  authorization_required_at: string | null;
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
  authenticated: true;
  coach: InstructorAuthCoach;
  expires_at: string;
};

export type ProviderWeekday =
  | "monday"
  | "tuesday"
  | "wednesday"
  | "thursday"
  | "friday"
  | "saturday"
  | "sunday";

export type ProviderWorkingHour = {
  weekday: ProviderWeekday;
  enabled: boolean;
  start_time: string | null;
  end_time: string | null;
};

export type ProviderLocation = {
  address: string;
  lat: number;
  lng: number;
};

export type ProviderService = {
  name: string | null;
  default_duration_mins: number | null;
  location: ProviderLocation | null;
};

export type ProviderConfiguration = {
  id: Id;
  display_name: string;
  business_name: string | null;
  timezone: string;
  base_location: ProviderLocation | null;
  service: ProviderService;
  working_hours: ProviderWorkingHour[];
  setup_complete: boolean;
  setup_missing_fields: string[];
};

export type ProviderConfigurationUpdate = {
  display_name: string;
  business_name: string | null;
  timezone: string;
  base_address: string;
  service_address: string;
  service_name: string;
  default_duration_mins: number;
  working_hours: ProviderWorkingHour[];
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
  timezone: string;
  base_location: ProviderLocation | null;
  service: ProviderService;
  working_hours: ProviderWorkingHour[];
  setup_complete: boolean;
  setup_missing_fields: string[];
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
  guardian_name?: string | null;
  default_location: LocationInfo;
  location_mode: LessonLocationMode;
  availability: AvailabilityWindow[];
  lesson_duration_mins: number;
  notes: string;
  history_summary: string;
  progress_focus: string;
};

export type FinderClient = Pick<Client, "id" | "name" | "location_mode" | "availability" | "lesson_duration_mins"> & {
  default_location: LocationInfo | null;
};

type ScheduleItemBase = {
  id: Id;
  title: string;
  start_time: string;
  end_time: string;
  travel_time_mins?: number | null;
  buffer_mins?: number | null;
  note?: string | null;
  revision?: number | null;
};

export type LessonScheduleItem = ScheduleItemBase & {
  kind: "lesson";
  client_id: Id;
  location: LocationInfo;
  status: LessonStatus;
};

export type BlockerScheduleItem = ScheduleItemBase & {
  kind: BlockerKind;
  revision: number;
  client_id?: null;
  location: LocationInfo | null;
  status?: null;
};

export type UtilityScheduleItem = ScheduleItemBase & {
  kind: Exclude<AppointmentKind, "lesson" | BlockerKind>;
  start_times?: string[];
  client_id?: Id | null;
  location: LocationInfo | null;
  status?: LessonStatus | null;
};

export type CalendarBusyScheduleItem = ScheduleItemBase & {
  kind: "calendar_busy";
  client_id?: null;
  location: null;
  status?: null;
};

export type ScheduleItem = LessonScheduleItem | BlockerScheduleItem | UtilityScheduleItem | CalendarBusyScheduleItem;

export type AgendaNextUp = {
  appointment_id: Id;
  leave_by: string | null;
  travel_time_mins: number | null;
  buffer_mins: number | null;
};

export type CoachAgenda = {
  instructor_id: Id;
  date: string;
  today: string;
  timezone: string;
  status: "ready" | "setup_required" | "calendar_unavailable" | "routing_unavailable" | "schedule_changed";
  items: ScheduleItem[];
  next_up: AgendaNextUp | null;
};

export type ProviderBlocker = {
  id: Id;
  kind: BlockerKind;
  title: string;
  start_time: string;
  end_time: string;
  local_date: string;
  local_start_time: string;
  local_end_time: string;
  timezone: string;
  revision: number;
  location: LocationInfo | null;
};

export type BlockerMutation = {
  kind: BlockerKind;
  title: string;
  date: string;
  start_time: string;
  end_time: string;
  location_address: string | null;
  expected_timezone?: string;
};

export type BlockerUpdate = BlockerMutation & {
  expected_revision: number;
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

export type DistanceBufferTier = {
  id: Id;
  label: string;
  min_miles: number;
  max_miles: number | null;
  buffer_mins: number;
};

export type SchedulingPolicy = {
  revision: number;
  distance_buffers_enabled: boolean;
  post_break_recovery_mins: number;
  distance_buffer_tiers: DistanceBufferTier[];
};

export type SchedulingPolicyTierUpdate = {
  id: Id;
  buffer_mins: number;
};

export type SchedulingPolicyUpdate = {
  expected_revision: number;
  distance_buffers_enabled: boolean;
  post_break_recovery_mins: number;
  distance_buffer_tiers: SchedulingPolicyTierUpdate[];
};

export type CommandCenterSnapshot = {
  instructor: InstructorProfile;
  date: string;
  clients: Client[];
  schedule: ScheduleItem[];
  requests: LessonRequest[];
  email_deliveries: CoachEmailDelivery[];
  scheduling_policy: SchedulingPolicy;
  /** @deprecated Read compatibility only. New instructor code uses scheduling_policy. */
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
  validateSession(): Promise<InstructorAuthSession>;
  signIn(credentials: InstructorAuthCredentials): Promise<InstructorAuthSession>;
  signOut(): Promise<void>;
  getProvider(): Promise<ProviderConfiguration>;
  saveProvider(provider: ProviderConfigurationUpdate, expectedInstructorId?: Id): Promise<ProviderConfiguration>;
  getGoogleCalendarConnection(expectedInstructorId: Id): Promise<GoogleCalendarConnection>;
  authorizeGoogleCalendar(expectedInstructorId: Id): Promise<string>;
  disconnectGoogleCalendar(expectedInstructorId: Id): Promise<GoogleCalendarConnection>;
  getSchedulingPolicy(): Promise<SchedulingPolicy>;
  saveSchedulingPolicy(policy: SchedulingPolicyUpdate): Promise<SchedulingPolicy>;
  getSnapshot(instructor_id: Id, date: string): Promise<CommandCenterSnapshot>;
  getBlockers(date: string, expectedInstructorId?: Id): Promise<ProviderBlocker[]>;
  createBlocker(blocker: BlockerMutation, idempotencyKey: string, expectedInstructorId?: Id): Promise<ProviderBlocker>;
  updateBlocker(blocker_id: Id, blocker: BlockerUpdate, expectedInstructorId?: Id): Promise<ProviderBlocker>;
  deleteBlocker(blocker_id: Id, expected_revision: number, expectedInstructorId?: Id): Promise<void>;
  findAvailableSlots(request: BookingRequestDTO): Promise<TimeSlotDTO[]>;
  getFinderClients(expectedInstructorId: Id): Promise<FinderClient[]>;
  searchAvailability(request: AvailabilityRangeRequest, expectedInstructorId: Id): Promise<AvailabilityRangeResult>;
  bookLesson(payload: BookLessonPayload): Promise<BookLessonResponse>;
  confirmRequest(requestId: Id, slotId?: Id): Promise<CoachLessonRequestConfirmResponse>;
  getEmailDeliveries(): Promise<CoachEmailDelivery[]>;
  resendEmailDelivery(delivery_id: Id): Promise<CoachEmailDelivery>;
  updateEmailDeliveryContact(delivery_id: Id, email: string): Promise<CoachEmailDelivery>;
  getAppointmentCalendarLinks(appointment_id: Id): Promise<CalendarLinks>;
  downloadCalendarIcs(calendar_url: string): Promise<Blob>;
  requestAlternatives(request_id: Id, preferred_slot_ids: Id[]): Promise<LessonRequest | CoachLessonRequestActionResponse>;
};
