import { candidateSlots, providerConfiguration, snapshot } from "./mockData.js";
import { trustedGoogleCalendarAuthorizationUrl, trustedInstructorCalendarUrl } from "./calendar.js";
import { shiftDateKey } from "../lib/format.js";
import {
  resolveInstructorDataMode,
  type InstructorDataModeEnvironment,
} from "../config/dataMode.js";
import type {
  AvailabilityRangeRequest,
  AvailabilityRangeResult,
  BookLessonPayload,
  BookLessonResponse,
  BlockerScheduleItem,
  BlockerMutation,
  BlockerUpdate,
  BookingRequestDTO,
  CalendarLinks,
  CoachAgenda,
  CoachEmailDelivery,
  CoachLessonRequestActionResponse,
  CoachLessonRequestConfirmResponse,
  CommandCenterSnapshot,
  FinderClient,
  GoogleCalendarConnection,
  Id,
  InstructorAuthCredentials,
  InstructorAuthSession,
  InstructorCommandApi,
  LessonRequest,
  ProviderConfiguration,
  ProviderConfigurationUpdate,
  ProviderLocation,
  ProviderWeekday,
  ProviderBlocker,
  SchedulingPolicy,
  SchedulingPolicyUpdate,
  ScheduleItem,
  TimeSlotDTO,
} from "./types.js";

const LEGACY_AUTH_STORAGE_KEY = "swim-command:coach-session:v1";
const CSRF_HEADER_NAME = "X-CSRF-Protection";
const CSRF_HEADER_VALUE = "1";
const GOOGLE_CALENDAR_CONNECTION_PATH = "/coach/calendar-connections/google";
const mockLocalDayBoundaryCache = new Map<string, number>();

function clone<T>(value: T): T {
  return structuredClone(value);
}

function sameDate(isoDateTime: string, date: string): boolean {
  return isoDateTime.slice(0, 10) === date;
}

function mockDistanceBufferMinutes(policy: SchedulingPolicy, distanceMiles: number): number {
  if (!policy.distance_buffers_enabled) return 0;
  const normalizedDistance = Math.round(distanceMiles * 100) / 100;
  return policy.distance_buffer_tiers.find((tier) => (
    normalizedDistance >= tier.min_miles
    && (tier.max_miles === null || normalizedDistance <= tier.max_miles)
  ))?.buffer_mins ?? 0;
}

function mockPolicyStartShiftMinutes(previousBuffer: number, currentBuffer: number): number {
  const roundedPrevious = Math.ceil(previousBuffer / 15) * 15;
  const roundedCurrent = Math.ceil(currentBuffer / 15) * 15;
  return roundedCurrent - roundedPrevious;
}

export class MockInstructorCommandApi implements InstructorCommandApi {
  readonly authMode = "mock" as const;

  private currentProvider: ProviderConfiguration = clone(providerConfiguration);
  private currentSnapshot: CommandCenterSnapshot = canonicalMockSnapshot(
    snapshot,
    providerConfiguration.timezone,
  );
  private readonly blockerCreations = new Map<string, ProviderBlocker>();
  private readonly calendarHandoffs = new Map<Id, CalendarLinks>();
  private readonly confirmations = new Map<Id, {
    slotId: Id | null;
    response: CoachLessonRequestConfirmResponse;
  }>();

  async validateSession(): Promise<InstructorAuthSession> {
    return {
      authenticated: true,
      coach: {
        instructor_id: this.currentSnapshot.instructor.id,
        display_name: this.currentSnapshot.instructor.name,
      },
      expires_at: "2099-01-01T00:00:00Z",
    };
  }

  async signIn(_credentials: InstructorAuthCredentials): Promise<InstructorAuthSession> {
    return {
      authenticated: true,
      coach: {
        instructor_id: this.currentSnapshot.instructor.id,
        display_name: this.currentSnapshot.instructor.name,
      },
      expires_at: "2099-01-01T00:00:00Z",
    };
  }

  async signOut(): Promise<void> {
    return;
  }

  async getProvider(): Promise<ProviderConfiguration> {
    return clone(this.currentProvider);
  }

  async getGoogleCalendarConnection(_expectedInstructorId: Id): Promise<GoogleCalendarConnection> {
    return {
      provider: "google",
      state: "disconnected",
      connected_at: null,
      updated_at: null,
      authorization_required_at: null,
    };
  }

  async authorizeGoogleCalendar(_expectedInstructorId: Id): Promise<string> {
    throw new Error("Google Calendar authorization requires the instructor API.");
  }

  disconnectGoogleCalendar(expectedInstructorId: Id): Promise<GoogleCalendarConnection> {
    return this.getGoogleCalendarConnection(expectedInstructorId);
  }

  async saveProvider(payload: ProviderConfigurationUpdate, _expectedInstructorId?: Id): Promise<ProviderConfiguration> {
    const base_location = mockGeocodedLocation(payload.base_address, this.currentProvider.base_location, 0);
    const service_location = mockGeocodedLocation(
      payload.service_address,
      this.currentProvider.service.location,
      1,
    );
    this.currentProvider = {
      id: this.currentProvider.id,
      display_name: payload.display_name,
      business_name: payload.business_name,
      timezone: payload.timezone,
      base_location,
      service: {
        name: payload.service_name,
        default_duration_mins: payload.default_duration_mins,
        location: service_location,
      },
      working_hours: clone(payload.working_hours),
      setup_complete: true,
      setup_missing_fields: [],
    };
    const firstEnabled = payload.working_hours.find((hours) => hours.enabled);
    this.currentSnapshot = {
      ...this.currentSnapshot,
      instructor: {
        ...this.currentSnapshot.instructor,
        name: payload.display_name,
        business_name: payload.business_name || payload.display_name,
        timezone: payload.timezone,
        base_location,
        service: this.currentProvider.service,
        working_hours: clone(payload.working_hours),
        setup_complete: true,
        setup_missing_fields: [],
        main_address: service_location.address,
        lat: service_location.lat,
        lng: service_location.lng,
        work_start_time: firstEnabled?.start_time ?? "",
        work_end_time: firstEnabled?.end_time ?? "",
      },
    };
    return clone(this.currentProvider);
  }

  async getSchedulingPolicy(): Promise<SchedulingPolicy> {
    return clone(this.currentSnapshot.scheduling_policy);
  }

  async saveSchedulingPolicy(payload: SchedulingPolicyUpdate): Promise<SchedulingPolicy> {
    const current = this.currentSnapshot.scheduling_policy;
    if (payload.expected_revision !== current.revision) {
      throw new InstructorCommandApiError(
        409,
        "SCHEDULING_POLICY_STALE",
        "The scheduling policy changed in another tab.",
      );
    }
    const currentIds = current.distance_buffer_tiers.map(({ id }) => id);
    const updateIds = payload.distance_buffer_tiers.map(({ id }) => id);
    if (
      currentIds.length !== updateIds.length
      || currentIds.some((id, index) => id !== updateIds[index])
    ) {
      throw new InstructorCommandApiError(
        422,
        "VALIDATION_ERROR",
        "Distance tier IDs must match the saved policy in order.",
      );
    }
    const saved: SchedulingPolicy = {
      revision: current.revision + 1,
      distance_buffers_enabled: payload.distance_buffers_enabled,
      post_break_recovery_mins: payload.post_break_recovery_mins,
      distance_buffer_tiers: current.distance_buffer_tiers.map((tier, index) => ({
        ...tier,
        buffer_mins: payload.distance_buffer_tiers[index].buffer_mins,
      })),
    };
    this.currentSnapshot = { ...this.currentSnapshot, scheduling_policy: saved };
    return clone(saved);
  }

  async getSnapshot(instructor_id: Id, date: string): Promise<CommandCenterSnapshot> {
    const fullSnapshot = clone(this.currentSnapshot);
    const projectedSchedule = mockScheduleInTimezone(
      fullSnapshot.schedule,
      this.currentProvider.timezone,
    );
    return {
      ...fullSnapshot,
      date,
      schedule: projectedSchedule.filter((item) => (
        mockScheduleOverlapsLocalDate(item, date, this.currentProvider.timezone)
      )),
      instructor:
        fullSnapshot.instructor.id === instructor_id
          ? fullSnapshot.instructor
          : { ...fullSnapshot.instructor, id: instructor_id },
    };
  }

  async getBlockers(date: string): Promise<ProviderBlocker[]> {
    return mockScheduleInTimezone(
      this.currentSnapshot.schedule,
      this.currentProvider.timezone,
    )
      .filter((item): item is BlockerScheduleItem => (
        isBlockerScheduleItem(item)
        && mockScheduleOverlapsLocalDate(item, date, this.currentProvider.timezone)
      ))
      .map((item) => scheduleItemToBlocker(item, this.currentProvider.timezone));
  }

  async createBlocker(
    blocker: BlockerMutation,
    idempotencyKey: string,
  ): Promise<ProviderBlocker> {
    const existing = this.blockerCreations.get(idempotencyKey);
    if (existing) {
      const candidate = mockBlocker("pending", blocker, 1, existing.timezone);
      if (!sameMockBlocker(existing, candidate)) {
        throw new InstructorCommandApiError(
          409,
          "IDEMPOTENCY_KEY_CONFLICT",
          "The idempotency key was already used for a different mutation.",
        );
      }
      return clone(existing);
    }
    const created: ProviderBlocker = mockBlocker(
      `blocker_${idempotencyKey.replace(/[^a-z0-9]/gi, "").slice(-12)}`,
      blocker,
      1,
      this.currentProvider.timezone,
    );
    this.assertMockBlockerDoesNotOverlap(created);
    this.upsertMockBlocker(created, `idempotency:${idempotencyKey}`);
    this.blockerCreations.set(idempotencyKey, clone(created));
    return clone(created);
  }

  async updateBlocker(blocker_id: Id, blocker: BlockerUpdate): Promise<ProviderBlocker> {
    const existing = this.currentSnapshot.schedule.find((item) => item.id === blocker_id);
    if (!existing || !isBlockerScheduleItem(existing)) {
      throw new InstructorCommandApiError(404, "RESOURCE_NOT_FOUND", "The schedule event could not be found.");
    }
    if (existing.revision !== blocker.expected_revision) {
      throw new InstructorCommandApiError(409, "BLOCKER_STALE", "This schedule event changed. Reload it before saving again.");
    }
    const updated = mockBlocker(
      blocker_id,
      blocker,
      blocker.expected_revision + 1,
      this.currentProvider.timezone,
    );
    this.assertMockBlockerDoesNotOverlap(updated, blocker_id);
    this.upsertMockBlocker(updated, existing.note ?? null);
    return clone(updated);
  }

  async deleteBlocker(blocker_id: Id, expected_revision: number): Promise<void> {
    const existing = this.currentSnapshot.schedule.find((item) => item.id === blocker_id);
    if (!existing || !isBlockerScheduleItem(existing)) {
      throw new InstructorCommandApiError(404, "RESOURCE_NOT_FOUND", "The schedule event could not be found.");
    }
    if (existing.revision !== expected_revision) {
      throw new InstructorCommandApiError(409, "BLOCKER_STALE", "This schedule event changed. Reload it before removing it.");
    }
    this.currentSnapshot = {
      ...this.currentSnapshot,
      schedule: this.currentSnapshot.schedule.filter((item) => item.id !== blocker_id),
    };
  }

  async findAvailableSlots(request: BookingRequestDTO): Promise<TimeSlotDTO[]> {
    const targetDate = request.requested_date;
    const isFacility = request.location.is_instructor_facility;
    const policy = this.currentSnapshot.scheduling_policy;
    return clone(candidateSlots)
      .filter((slot) => sameDate(slot.start_time, targetDate))
      .filter((slot) => slot.target_location.is_instructor_facility === isFacility || !isFacility)
      .map((slot) => {
        const buffer_mins = mockDistanceBufferMinutes(policy, slot.distance_miles);
        const start_time = addMinutes(
          slot.start_time,
          mockPolicyStartShiftMinutes(slot.buffer_mins, buffer_mins),
        );
        return {
          ...slot,
          start_time,
          end_time: addMinutes(start_time, request.duration_mins),
          buffer_mins,
          target_location: request.location,
        };
      })
      .filter((slot) => !this.mockScheduleOverlaps(slot.start_time, slot.end_time));
  }

  async getFinderClients(_expectedInstructorId: Id): Promise<FinderClient[]> {
    const location = this.currentProvider.service.location;
    return clone(this.currentSnapshot.clients.map((client) => client.location_mode === "client_home" ? client : {
      ...client,
      default_location: location && { ...location, is_instructor_facility: true },
    }));
  }

  async searchAvailability(request: AvailabilityRangeRequest, _expectedInstructorId: Id): Promise<AvailabilityRangeResult> {
    const { timezone, service, working_hours } = this.currentProvider;
    const { start_date, end_date, duration_mins, is_instructor_facility, client_address, windows, limit = 24 } = request;
    const savedLocation = this.currentSnapshot.clients.find((client) => client.default_location.address === client_address)?.default_location;
    const resolved = is_instructor_facility ? service.location : client_address && mockGeocodedLocation(client_address, savedLocation ?? null, 0);
    if (!resolved) throw new InstructorCommandApiError(422, "VALIDATION_ERROR", "Choose a saved lesson location.");
    const location = { ...resolved, is_instructor_facility };
    const distance_miles = is_instructor_facility ? 0 : 2.4;
    const travel_time_mins = is_instructor_facility ? 0 : 8;
    const buffer_mins = mockDistanceBufferMinutes(this.currentSnapshot.scheduling_policy, distance_miles);
    const available_slots: TimeSlotDTO[] = [];
    const weekdayFormat = new Intl.DateTimeFormat("en-US", { weekday: "long", timeZone: "UTC" });
    let empty_reason: AvailabilityRangeResult["empty_reason"] = "no_working_hours";
    const step = 15 * 60_000;
    for (let date = start_date; date <= end_date && available_slots.length < limit; date = shiftDateKey(date, 1)) {
      const weekday = weekdayFormat.format(new Date(`${date}T00:00:00Z`)).toLowerCase() as ProviderWeekday;
      const hours = working_hours.find((item) => item.weekday === weekday);
      if (!hours?.enabled || !hours.start_time || !hours.end_time) continue;
      if (empty_reason === "no_working_hours") empty_reason = "outside_client_windows";
      const dayWindows = windows.filter((window) => window.days.includes(weekday));
      if (windows.length && !dayWindows.length) continue;
      empty_reason = "no_safe_slots";
      const opening = Date.parse(`${date}T${hours.start_time}:00Z`);
      const closing = Date.parse(`${date}T${hours.end_time}:00Z`);
      const firstStart = Math.ceil((opening + (travel_time_mins + buffer_mins) * 60_000) / step) * step;
      const duration = duration_mins * 60_000;
      for (let start = firstStart; start + duration <= closing && available_slots.length < limit; start += step) {
        const startClock = new Date(start).toISOString().slice(11, 16);
        const endClock = new Date(start + duration).toISOString().slice(11, 16);
        if (dayWindows.length && !dayWindows.some((window) => window.start_time <= startClock && window.end_time >= endClock)) continue;
        const start_time = mockLocalDateTimeWithOffset(date, startClock, timezone, "start_time");
        const end_time = addMinutes(start_time, duration_mins);
        if (this.mockScheduleOverlaps(start_time, end_time)) continue;
        available_slots.push({ id: `slot_${start_time}`, start_time, end_time, travel_time_mins, buffer_mins, distance_miles, target_location: location });
      }
    }
    return { start_date, end_date, timezone, duration_mins, location, available_slots, empty_reason: available_slots.length ? null : empty_reason };
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

  async confirmRequest(requestId: Id, slotId?: Id): Promise<CoachLessonRequestConfirmResponse> {
    const existingConfirmation = this.confirmations.get(requestId);
    if (existingConfirmation) {
      if (slotId !== undefined && slotId !== existingConfirmation.slotId) {
        throw new InstructorCommandApiError(
          409,
          "LESSON_REQUEST_CONFIRM_CONFLICT",
          "This lesson request was already confirmed for a different time.",
        );
      }
      return clone(existingConfirmation.response);
    }

    const request = this.currentSnapshot.requests.find((candidate) => candidate.id === requestId);
    if (!request) {
      throw new Error(`Request ${requestId} was not found.`);
    }

    const selectedAlternative = slotId === undefined
      ? undefined
      : request.alternative_slots.find((candidate) => candidate.id === slotId);
    if (slotId !== undefined && !selectedAlternative) {
      throw new InstructorCommandApiError(
        400,
        "INVALID_SCHEDULING_REQUEST",
        "The selected time is not available for this lesson request.",
      );
    }

    const selectedSlot = selectedAlternative ?? request.requested_slot;
    const confirmedAlternativeId = selectedAlternative?.id
      ?? request.alternative_slots.find((candidate) => candidate.id === request.requested_slot.id)?.id
      ?? null;
    const updated = this.updateRequest(requestId, (current) => ({
      ...current,
      requested_slot: clone(selectedSlot),
      status: "approved",
    }));
    const occurrenceStarts = requestId === "req_sofia_0827"
      ? [updated.requested_slot.start_time, addDays(updated.requested_slot.start_time, 7)]
      : [updated.requested_slot.start_time];
    const appointment_ids = occurrenceStarts.map((_, index) => `appt_${requestId}_${index + 1}`);
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
    const deliveryId = `delivery_confirmation_${requestId}`;
    const existingDelivery = this.currentSnapshot.email_deliveries.find((delivery) => delivery.id === deliveryId);
    const email_delivery = existingDelivery ?? {
      id: deliveryId,
      kind: "confirmation" as const,
      status: "queued" as const,
      updated_at: nextMockUpdatedAt(this.currentSnapshot.email_deliveries),
      client_id: updated.client_id,
      client_name: updated.client_name,
      request_token: requestId,
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
    const response: CoachLessonRequestConfirmResponse = {
      id: requestId,
      status: "confirmed",
      appointment_id: appointment_ids[0],
      appointment_ids,
      message: "Lesson request is confirmed.",
      calendar_links,
      email_delivery: clone(email_delivery),
    };
    this.confirmations.set(requestId, {
      slotId: confirmedAlternativeId,
      response: clone(response),
    });
    return clone(response);
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

  private upsertMockBlocker(blocker: ProviderBlocker, note: string | null): void {
    const scheduleItem = {
      id: blocker.id,
      kind: blocker.kind,
      title: blocker.title,
      start_time: blocker.start_time,
      end_time: blocker.end_time,
      location: blocker.location,
      revision: blocker.revision,
      note,
    };
    this.currentSnapshot = {
      ...this.currentSnapshot,
      schedule: [
        ...this.currentSnapshot.schedule.filter((item) => item.id !== blocker.id),
        scheduleItem,
      ].sort((left, right) => (
        mockScheduleInstant(left.start_time, this.currentProvider.timezone)
        - mockScheduleInstant(right.start_time, this.currentProvider.timezone)
      )),
    };
  }

  private assertMockBlockerDoesNotOverlap(blocker: ProviderBlocker, excludeId?: Id): void {
    const blockerStart = mockScheduleInstant(blocker.start_time, this.currentProvider.timezone);
    const blockerEnd = mockScheduleInstant(blocker.end_time, this.currentProvider.timezone);
    const overlaps = this.currentSnapshot.schedule.some((item) => (
      item.id !== excludeId
      && (item.kind === "lesson" || isBlockerKind(item.kind))
      && mockScheduleInstant(item.start_time, this.currentProvider.timezone) < blockerEnd
      && mockScheduleInstant(item.end_time, this.currentProvider.timezone) > blockerStart
    ));
    if (overlaps) {
      throw new InstructorCommandApiError(
        409,
        "SCHEDULING_CONFLICT",
        "That event overlaps another active schedule item.",
      );
    }
  }

  private mockScheduleOverlaps(startTime: string, endTime: string): boolean {
    const start = mockScheduleInstant(startTime, this.currentProvider.timezone);
    const end = mockScheduleInstant(endTime, this.currentProvider.timezone);
    return this.currentSnapshot.schedule.some((item) => (
      (item.kind === "lesson" || isBlockerKind(item.kind))
      && mockScheduleInstant(item.start_time, this.currentProvider.timezone) < end
      && mockScheduleInstant(item.end_time, this.currentProvider.timezone) > start
    ));
  }
}

export class HttpInstructorCommandApi implements InstructorCommandApi {
  readonly authMode = "live" as const;

  private readonly baseUrl: string;

  constructor(baseUrl = "/api") {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
  }

  async validateSession(): Promise<InstructorAuthSession> {
    const response = await fetch(`${this.baseUrl}/auth/coach/session`, credentialedGetInit());
    const session = await parseResponse<InstructorAuthSession>(response);
    if (!session.authenticated) {
      throw new InstructorCommandApiError(401, "AUTHENTICATION_REQUIRED", "Coach authentication is required.");
    }
    return metadataOnlySession(session);
  }

  async signIn(credentials: InstructorAuthCredentials): Promise<InstructorAuthSession> {
    const response = await fetch(`${this.baseUrl}/auth/coach/sign-in`, unsafeJsonInit("POST", credentials));
    return metadataOnlySession(await parseResponse<InstructorAuthSession>(response));
  }

  async signOut(): Promise<void> {
    const response = await fetch(`${this.baseUrl}/auth/coach/sign-out`, unsafeNoBodyInit("POST"));
    if (!response.ok) {
      throw await toApiError(response);
    }
  }

  getProvider(): Promise<ProviderConfiguration> {
    return this.get("/coach/provider");
  }

  saveProvider(provider: ProviderConfigurationUpdate, expectedInstructorId?: Id): Promise<ProviderConfiguration> {
    return this.put("/coach/provider", provider, expectedInstructorId);
  }

  async getGoogleCalendarConnection(expectedInstructorId: Id): Promise<GoogleCalendarConnection> {
    const response = await fetch(
      `${this.baseUrl}${GOOGLE_CALENDAR_CONNECTION_PATH}`,
      googleCalendarRequestInit("GET", expectedInstructorId),
    );
    return validateGoogleCalendarConnection(await parseResponse(response));
  }

  async authorizeGoogleCalendar(expectedInstructorId: Id): Promise<string> {
    const response = await fetch(`${this.baseUrl}${GOOGLE_CALENDAR_CONNECTION_PATH}/authorize`, {
      ...googleCalendarRequestInit("POST", expectedInstructorId),
      redirect: "error",
    });
    const authorization = await parseResponse<{ authorization_url: unknown } | null>(response);
    return trustedGoogleCalendarAuthorizationUrl(authorization?.authorization_url);
  }

  async disconnectGoogleCalendar(expectedInstructorId: Id): Promise<GoogleCalendarConnection> {
    const response = await fetch(
      `${this.baseUrl}${GOOGLE_CALENDAR_CONNECTION_PATH}`,
      googleCalendarRequestInit("DELETE", expectedInstructorId),
    );
    return validateGoogleCalendarConnection(await parseResponse(response));
  }

  getSchedulingPolicy(): Promise<SchedulingPolicy> {
    return this.get("/coach/scheduling-policy");
  }

  saveSchedulingPolicy(policy: SchedulingPolicyUpdate): Promise<SchedulingPolicy> {
    return this.put("/coach/scheduling-policy", policy);
  }

  async getSnapshot(_instructor_id: Id, date: string): Promise<CommandCenterSnapshot> {
    return this.get<CommandCenterSnapshot>(`/coach/command-center/snapshot?date=${encodeURIComponent(date)}`);
  }

  async getAgenda(expectedInstructorId: Id, date?: string): Promise<CoachAgenda> {
    const query = date === undefined ? "" : `?date=${encodeURIComponent(date)}`;
    const response = await fetch(`${this.baseUrl}/coach/agenda${query}`, {
      ...withExpectedInstructor(credentialedGetInit(), expectedInstructorId),
      cache: "no-store",
    });
    return parseResponse<CoachAgenda>(response);
  }

  getBlockers(date: string, expectedInstructorId?: Id): Promise<ProviderBlocker[]> {
    return this.get(`/coach/blockers?date=${encodeURIComponent(date)}`, expectedInstructorId);
  }

  async createBlocker(
    blocker: BlockerMutation,
    idempotencyKey: string,
    expectedInstructorId?: Id,
  ): Promise<ProviderBlocker> {
    const response = await fetch(
      `${this.baseUrl}/coach/blockers`,
      withExpectedInstructor(unsafeJsonInit("POST", blocker, { "Idempotency-Key": idempotencyKey }), expectedInstructorId),
    );
    return parseResponse<ProviderBlocker>(response);
  }

  updateBlocker(blocker_id: Id, blocker: BlockerUpdate, expectedInstructorId?: Id): Promise<ProviderBlocker> {
    return this.put(`/coach/blockers/${encodeURIComponent(blocker_id)}`, blocker, expectedInstructorId);
  }

  async deleteBlocker(blocker_id: Id, expected_revision: number, expectedInstructorId?: Id): Promise<void> {
    const response = await fetch(
      `${this.baseUrl}/coach/blockers/${encodeURIComponent(blocker_id)}?expected_revision=${encodeURIComponent(expected_revision)}`,
      withExpectedInstructor(unsafeNoBodyInit("DELETE"), expectedInstructorId),
    );
    if (!response.ok) throw await toApiError(response);
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

  async getFinderClients(expectedInstructorId: Id): Promise<FinderClient[]> {
    const { clients } = await this.get<{ clients: CoachFinderClient[] }>("/coach/clients", expectedInstructorId);
    return clients.map((client) => ({
      id: client.id,
      name: client.name,
      availability: client.availability ?? [],
      lesson_duration_mins: client.default_duration_mins,
      location_mode: client.location_mode === "service_location" ? "instructor_facility" : "client_home",
      default_location: client.effective_location && {
        ...client.effective_location,
        is_instructor_facility: client.location_mode === "service_location",
      },
    }));
  }

  async searchAvailability(request: AvailabilityRangeRequest, expectedInstructorId: Id): Promise<AvailabilityRangeResult> {
    const response = await this.post<CoachAvailabilityRangeResponse>("/coach/availability/search", request, expectedInstructorId);
    return {
      ...response,
      available_slots: response.available_slots.map((slot, index) => mapAvailabilitySlot(slot, response.location, index)),
    };
  }

  bookLesson(payload: BookLessonPayload): Promise<BookLessonResponse> {
    return this.post("/coach/bookings", {
      client_id: payload.client_id,
      client_name: payload.client_name,
      client_address: payload.location_address,
      start_time: payload.start_time_iso,
      duration_mins: payload.duration_mins,
      is_instructor_facility: payload.is_instructor_facility ?? false,
      ...(payload.idempotency_key ? { idempotency_key: payload.idempotency_key } : {}),
    }, payload.instructor_id);
  }

  confirmRequest(requestId: Id, slotId?: Id): Promise<CoachLessonRequestConfirmResponse> {
    return this.post(
      `/coach/lesson-requests/${encodeURIComponent(requestId)}/confirm`,
      slotId === undefined ? {} : { slot_id: slotId },
    );
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
      credentials: "include",
      headers: { accept: "text/calendar" },
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
    });
    if (!response.ok) {
      throw await toApiError(response);
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

  private async get<T>(path: string, expectedInstructorId?: Id): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, withExpectedInstructor(credentialedGetInit(), expectedInstructorId));
    return parseResponse<T>(response);
  }

  private async post<T>(path: string, body: unknown, expectedInstructorId?: Id): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, withExpectedInstructor(unsafeJsonInit("POST", body), expectedInstructorId));
    return parseResponse<T>(response);
  }

  private async postWithoutBody<T>(path: string): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, unsafeNoBodyInit("POST"));
    return parseResponse<T>(response);
  }

  private async put<T>(path: string, body: unknown, expectedInstructorId?: Id): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, withExpectedInstructor(unsafeJsonInit("PUT", body), expectedInstructorId));
    return parseResponse<T>(response);
  }

}

export const createInstructorCommandApi = (
  environment: InstructorDataModeEnvironment = import.meta.env ?? {},
): MockInstructorCommandApi | HttpInstructorCommandApi => {
  const config = resolveInstructorDataMode(environment);
  return config.mode === "mock"
    ? new MockInstructorCommandApi()
    : new HttpInstructorCommandApi(config.apiBaseUrl);
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
    return this.status === 401;
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

type CoachAvailabilityRangeResponse = Omit<AvailabilityRangeResult, "available_slots">
  & Pick<CoachAvailabilityResponse, "available_slots">;

type CoachFinderClient = Pick<FinderClient, "id" | "name" | "availability"> & {
  location_mode: "client_location" | "service_location";
  effective_location: ProviderLocation | null;
  default_duration_mins: number;
};

function unsafeJsonInit(
  method: "POST" | "PUT",
  body: unknown,
  extraHeaders: Record<string, string> = {},
): RequestInit {
  return {
    method,
    credentials: "include",
    headers: {
      "content-type": "application/json",
      [CSRF_HEADER_NAME]: CSRF_HEADER_VALUE,
      ...extraHeaders,
    },
    body: JSON.stringify(body),
  };
}

function credentialedGetInit(): RequestInit {
  return {
    method: "GET",
    credentials: "include",
    headers: { accept: "application/json" },
  };
}

function unsafeNoBodyInit(method: "POST" | "DELETE"): RequestInit {
  return {
    method,
    credentials: "include",
    headers: { [CSRF_HEADER_NAME]: CSRF_HEADER_VALUE },
  };
}

function googleCalendarRequestInit(method: "GET" | "POST" | "DELETE", expectedInstructorId: Id): RequestInit {
  if (!expectedInstructorId) throw new Error("Google Calendar requires a signed-in provider.");

  const init = method === "GET" ? credentialedGetInit() : unsafeNoBodyInit(method);
  return withExpectedInstructor(init, expectedInstructorId);
}

function withExpectedInstructor(init: RequestInit, expectedInstructorId?: Id): RequestInit {
  if (expectedInstructorId === undefined) return init;
  const headers = new Headers(init.headers);
  headers.set("X-Expected-Instructor-Id", encodeURIComponent(expectedInstructorId));
  return { ...init, headers };
}

function isBlockerKind(value: string): value is ProviderBlocker["kind"] {
  return value === "break" || value === "personal" || value === "unavailable";
}

function isBlockerScheduleItem(item: ScheduleItem): item is BlockerScheduleItem {
  return isBlockerKind(item.kind);
}

function canonicalMockSnapshot(
  source: CommandCenterSnapshot,
  timezone: string,
): CommandCenterSnapshot {
  const canonical = clone(source);
  return {
    ...canonical,
    schedule: canonical.schedule.map((item) => ({
      ...item,
      start_time: mockCanonicalScheduleDateTime(item.start_time, timezone, "start_time"),
      end_time: mockCanonicalScheduleDateTime(item.end_time, timezone, "end_time"),
    })) as ScheduleItem[],
  };
}

function mockScheduleInTimezone(
  schedule: ScheduleItem[],
  timezone: string,
): ScheduleItem[] {
  return schedule.map((item) => ({
    ...item,
    start_time: mockScheduleDateTimeInTimezone(item.start_time, timezone),
    end_time: mockScheduleDateTimeInTimezone(item.end_time, timezone),
  })) as ScheduleItem[];
}

function mockScheduleOverlapsLocalDate(
  item: ScheduleItem,
  date: string,
  timezone: string,
): boolean {
  const nextDate = mockShiftIsoDate(date, 1);
  const dayStart = mockLocalDayBoundaryInstant(date, timezone);
  const dayEnd = mockLocalDayBoundaryInstant(nextDate, timezone);
  return mockScheduleInstant(item.start_time, timezone) < dayEnd
    && mockScheduleInstant(item.end_time, timezone) > dayStart;
}

function mockLocalDayBoundaryInstant(date: string, timezone: string): number {
  const cacheKey = `${timezone}|${date}`;
  const cached = mockLocalDayBoundaryCache.get(cacheKey);
  if (cached !== undefined) return cached;

  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  if (!match || mockShiftIsoDate(date, 0) !== date) {
    throw mockLocalTimeError("start_time", "invalid_local_time", "Choose a valid date.");
  }
  const targetKey = Number(`${match[1]}${match[2]}${match[3]}`);
  let formatter: Intl.DateTimeFormat;
  try {
    formatter = new Intl.DateTimeFormat("en-CA-u-ca-iso8601-nu-latn", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
  } catch {
    throw mockLocalTimeError("start_time", "invalid_timezone", "The provider timezone is invalid.");
  }
  const dateKeyAt = (instant: number): number => {
    const parts = Object.fromEntries(
      formatter.formatToParts(new Date(instant))
        .filter((part) => part.type !== "literal")
        .map((part) => [part.type, part.value]),
    );
    return Number(`${parts.year}${parts.month}${parts.day}`);
  };
  const utcMidnight = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  const searchStart = utcMidnight - 36 * 60 * 60_000;
  const searchEnd = utcMidnight + 36 * 60 * 60_000;
  const step = 60_000;
  let previous = searchStart;
  let firstGreater: { low: number; high: number } | null = null;
  for (let instant = searchStart + step; instant <= searchEnd; instant += step) {
    const key = dateKeyAt(instant);
    if (key === targetKey) {
      const boundary = mockRefineLocalDayBoundary(previous, instant, targetKey, dateKeyAt);
      mockLocalDayBoundaryCache.set(cacheKey, boundary);
      return boundary;
    }
    if (key > targetKey && firstGreater === null) {
      firstGreater = { low: previous, high: instant };
    }
    previous = instant;
  }
  if (firstGreater !== null) {
    const boundary = mockRefineLocalDayBoundary(
      firstGreater.low,
      firstGreater.high,
      targetKey,
      dateKeyAt,
    );
    mockLocalDayBoundaryCache.set(cacheKey, boundary);
    return boundary;
  }
  throw mockLocalTimeError("start_time", "invalid_local_time", "Choose a valid date.");
}

function mockRefineLocalDayBoundary(
  lowValue: number,
  highValue: number,
  targetKey: number,
  dateKeyAt: (instant: number) => number,
): number {
  let low = lowValue;
  let high = highValue;
  while (high - low > 1) {
    const middle = Math.floor((low + high) / 2);
    if (dateKeyAt(middle) >= targetKey) high = middle;
    else low = middle;
  }
  return high;
}

function mockShiftIsoDate(date: string, days: number): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  if (!match) {
    throw mockLocalTimeError("start_time", "invalid_local_time", "Choose a valid date.");
  }
  const shifted = new Date(Date.UTC(
    Number(match[1]),
    Number(match[2]) - 1,
    Number(match[3]) + days,
  ));
  return shifted.toISOString().slice(0, 10);
}

function scheduleItemToBlocker(
  item: ScheduleItem,
  timezone: string,
): ProviderBlocker {
  if (!isBlockerScheduleItem(item)) throw new Error("Schedule item is not a blocker.");
  const start_time = mockCanonicalScheduleDateTime(item.start_time, timezone, "start_time");
  const end_time = mockCanonicalScheduleDateTime(item.end_time, timezone, "end_time");
  return {
    id: item.id,
    kind: item.kind,
    title: item.title,
    start_time,
    end_time,
    local_date: start_time.slice(0, 10),
    local_start_time: start_time.slice(11, 16),
    local_end_time: end_time.slice(11, 16),
    timezone,
    revision: item.revision,
    location: item.location,
  };
}

function mockBlocker(
  id: string,
  blocker: BlockerMutation,
  revision: number,
  timezone: string,
): ProviderBlocker {
  const start_time = mockLocalDateTimeWithOffset(
    blocker.date,
    blocker.start_time,
    timezone,
    "start_time",
  );
  const end_time = mockLocalDateTimeWithOffset(
    blocker.date,
    blocker.end_time,
    timezone,
    "end_time",
  );
  const location = blocker.kind === "personal" && blocker.location_address
    ? {
        ...mockGeocodedLocation(blocker.location_address, null, revision),
        is_instructor_facility: false,
      }
    : null;
  return {
    id,
    kind: blocker.kind,
    title: blocker.title,
    start_time,
    end_time,
    local_date: blocker.date,
    local_start_time: blocker.start_time,
    local_end_time: blocker.end_time,
    timezone,
    revision,
    location,
  };
}

function mockLocalDateTimeWithOffset(
  date: string,
  time: string,
  timezone: string,
  field: "start_time" | "end_time",
): string {
  const dateMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  const timeMatch = /^(\d{2}):(\d{2})$/.exec(time);
  if (!dateMatch || !timeMatch) {
    throw mockLocalTimeError(field, "invalid_local_time", "Choose a valid date and time.");
  }

  const year = Number(dateMatch[1]);
  const month = Number(dateMatch[2]);
  const day = Number(dateMatch[3]);
  const hour = Number(timeMatch[1]);
  const minute = Number(timeMatch[2]);
  const localEpoch = Date.UTC(year, month - 1, day, hour, minute);
  const localDate = new Date(localEpoch);
  if (
    month < 1 || month > 12
    || day < 1 || day > 31
    || hour > 23
    || minute > 59
    || localDate.getUTCFullYear() !== year
    || localDate.getUTCMonth() !== month - 1
    || localDate.getUTCDate() !== day
  ) {
    throw mockLocalTimeError(field, "invalid_local_time", "Choose a valid date and time.");
  }

  let formatter: Intl.DateTimeFormat;
  try {
    formatter = new Intl.DateTimeFormat("en-CA-u-ca-iso8601-nu-latn", {
      timeZone: timezone,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    throw mockLocalTimeError(field, "invalid_timezone", "The provider timezone is invalid.");
  }

  const matches: Array<{ instant: number; offsetMinutes: number }> = [];
  for (let offsetMinutes = -14 * 60; offsetMinutes <= 14 * 60; offsetMinutes += 15) {
    const instant = localEpoch - offsetMinutes * 60_000;
    const parts = Object.fromEntries(
      formatter.formatToParts(new Date(instant))
        .filter((part) => part.type !== "literal")
        .map((part) => [part.type, part.value]),
    );
    if (
      Number(parts.year) === year
      && Number(parts.month) === month
      && Number(parts.day) === day
      && Number(parts.hour) === hour
      && Number(parts.minute) === minute
    ) {
      matches.push({ instant, offsetMinutes });
    }
  }

  if (matches.length === 0) {
    throw mockLocalTimeError(
      field,
      "nonexistent_local_time",
      "That local time does not exist because of daylight saving time.",
    );
  }
  if (matches.length > 1) {
    throw mockLocalTimeError(
      field,
      "ambiguous_local_time",
      "That local time occurs twice because of daylight saving time. Choose another time.",
    );
  }

  const offsetMinutes = matches[0].offsetMinutes;
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const offsetHours = Math.floor(Math.abs(offsetMinutes) / 60).toString().padStart(2, "0");
  const offsetRemainder = (Math.abs(offsetMinutes) % 60).toString().padStart(2, "0");
  return `${date}T${time}:00${sign}${offsetHours}:${offsetRemainder}`;
}

function mockScheduleInstant(value: string, timezone: string): number {
  const canonical = mockCanonicalScheduleDateTime(value, timezone, "start_time");
  const instant = Date.parse(canonical);
  if (Number.isNaN(instant)) {
    throw mockLocalTimeError("start_time", "invalid_local_time", "Choose a valid date and time.");
  }
  return instant;
}

function mockCanonicalScheduleDateTime(
  value: string,
  timezone: string,
  field: "start_time" | "end_time",
): string {
  return /(?:Z|[+-]\d{2}:\d{2})$/i.test(value)
    ? value
    : mockLocalDateTimeWithOffset(value.slice(0, 10), value.slice(11, 16), timezone, field);
}

function mockScheduleDateTimeInTimezone(value: string, timezone: string): string {
  const instant = Date.parse(value);
  if (Number.isNaN(instant)) {
    throw mockLocalTimeError("start_time", "invalid_local_time", "Choose a valid date and time.");
  }
  let formatter: Intl.DateTimeFormat;
  try {
    formatter = new Intl.DateTimeFormat("en-CA-u-ca-iso8601-nu-latn", {
      timeZone: timezone,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    throw mockLocalTimeError("start_time", "invalid_timezone", "The provider timezone is invalid.");
  }
  const parts = Object.fromEntries(
    formatter.formatToParts(new Date(instant))
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
  const year = Number(parts.year);
  const month = Number(parts.month);
  const day = Number(parts.day);
  const hour = Number(parts.hour);
  const minute = Number(parts.minute);
  const second = Number(parts.second);
  const localEpoch = Date.UTC(year, month - 1, day, hour, minute, second);
  const offsetMinutes = Math.round((localEpoch - instant) / 60_000);
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const offsetHours = Math.floor(Math.abs(offsetMinutes) / 60).toString().padStart(2, "0");
  const offsetRemainder = (Math.abs(offsetMinutes) % 60).toString().padStart(2, "0");
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}`
    + `${sign}${offsetHours}:${offsetRemainder}`;
}

function mockLocalTimeError(
  field: "start_time" | "end_time",
  reason: string,
  message: string,
): InstructorCommandApiError {
  return new InstructorCommandApiError(
    422,
    "VALIDATION_ERROR",
    message,
    { field, reason },
  );
}

function sameMockBlocker(left: ProviderBlocker, right: ProviderBlocker): boolean {
  return left.kind === right.kind
    && left.title === right.title
    && left.start_time === right.start_time
    && left.end_time === right.end_time
    && (left.location?.address ?? null) === (right.location?.address ?? null);
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw await toApiError(response);
  }

  return response.json() as Promise<T>;
}

function validateGoogleCalendarConnection(
  connection: GoogleCalendarConnection | null,
): GoogleCalendarConnection {
  if (
    connection?.provider !== "google"
    || !["disconnected", "connected", "authorization_required"].includes(connection.state)
  ) {
    throw new Error("Google Calendar connection status is unavailable.");
  }
  return connection;
}

function metadataOnlySession(session: InstructorAuthSession): InstructorAuthSession {
  return {
    authenticated: true,
    expires_at: session.expires_at,
    coach: {
      instructor_id: session.coach.instructor_id,
      display_name: session.coach.display_name,
    },
  };
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

export function clearLegacyInstructorAuthState(storage: Pick<Storage, "removeItem"> | null = browserStorage()): void {
  storage?.removeItem(LEGACY_AUTH_STORAGE_KEY);
}

function browserStorage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function nextMockUpdatedAt(deliveries: readonly CoachEmailDelivery[]): string {
  const newest = deliveries.reduce((latest, delivery) => Math.max(latest, Date.parse(delivery.updated_at)), 0);
  return new Date(newest + 1_000).toISOString();
}

function isMockEmailAddress(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim());
}

function mockGeocodedLocation(
  address: string,
  current: ProviderConfiguration["base_location"],
  offset: number,
): NonNullable<ProviderConfiguration["base_location"]> {
  if (current?.address === address) return clone(current);
  return {
    address,
    lat: 30.2672 + offset * 0.001,
    lng: -97.7431 - offset * 0.001,
  };
}

function addMinutes(isoDateTime: string, minutes: number): string {
  if (hasTimezone(isoDateTime)) return new Date(Date.parse(isoDateTime) + minutes * 60_000).toISOString();
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
