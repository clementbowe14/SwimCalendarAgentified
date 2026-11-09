import { InstructorCommandApiError } from "../api/instructorApi.js";
import type {
  InstructorProfile,
  ProviderConfiguration,
  ProviderConfigurationUpdate,
  ProviderWeekday,
  ProviderWorkingHour,
} from "../api/types.js";

export const PROVIDER_WEEKDAYS: readonly ProviderWeekday[] = [
  "monday",
  "tuesday",
  "wednesday",
  "thursday",
  "friday",
  "saturday",
  "sunday",
];

export const PROVIDER_FIELD_ORDER = [
  "display_name",
  "business_name",
  "timezone",
  "base_address",
  "service_name",
  "service_address",
  "default_duration_mins",
  ...PROVIDER_WEEKDAYS.flatMap((weekday) => [
    `working_hours.${weekday}.enabled`,
    `working_hours.${weekday}.start_time`,
    `working_hours.${weekday}.end_time`,
  ]),
] as const;

export type ProviderFieldErrors = Record<string, string>;

export function providerDraftFromConfiguration(
  provider: ProviderConfiguration,
): ProviderConfigurationUpdate {
  const savedHours = new Map(provider.working_hours.map((hours) => [hours.weekday, hours]));
  return {
    display_name: provider.display_name,
    business_name: provider.business_name,
    timezone: provider.timezone,
    base_address: provider.base_location?.address ?? "",
    service_address: provider.service.location?.address ?? "",
    service_name: provider.service.name ?? "",
    default_duration_mins: provider.service.default_duration_mins ?? 60,
    working_hours: PROVIDER_WEEKDAYS.map((weekday) => {
      const saved = savedHours.get(weekday);
      return saved
        ? { ...saved }
        : { weekday, enabled: false, start_time: null, end_time: null };
    }),
  };
}

export function normalizeProviderDraft(
  draft: ProviderConfigurationUpdate,
): ProviderConfigurationUpdate {
  const indexedHours = new Map(draft.working_hours.map((hours) => [hours.weekday, hours]));
  return {
    display_name: draft.display_name.trim(),
    business_name: draft.business_name?.trim() || null,
    timezone: draft.timezone.trim(),
    base_address: draft.base_address.trim(),
    service_address: draft.service_address.trim(),
    service_name: draft.service_name.trim(),
    default_duration_mins: Number(draft.default_duration_mins),
    working_hours: PROVIDER_WEEKDAYS.map((weekday) => normalizeWorkingHour(
      indexedHours.get(weekday) ?? { weekday, enabled: false, start_time: null, end_time: null },
    )),
  };
}

export function validateProviderDraft(
  draft: ProviderConfigurationUpdate,
): ProviderFieldErrors {
  const normalized = normalizeProviderDraft(draft);
  const errors: ProviderFieldErrors = {};

  requiredText(errors, "display_name", normalized.display_name, "Enter your display name.", 160);
  optionalText(errors, "business_name", normalized.business_name, 160);
  requiredText(errors, "base_address", normalized.base_address, "Enter your base address.", 300);
  requiredText(errors, "service_name", normalized.service_name, "Enter a service name.", 160);
  requiredText(errors, "service_address", normalized.service_address, "Enter your service address.", 300);

  if (!normalized.timezone) {
    errors.timezone = "Choose a timezone.";
  } else if (!isValidTimeZone(normalized.timezone)) {
    errors.timezone = "Choose a valid IANA timezone.";
  }

  if (
    !Number.isInteger(normalized.default_duration_mins)
    || normalized.default_duration_mins < 15
    || normalized.default_duration_mins > 240
    || normalized.default_duration_mins % 15 !== 0
  ) {
    errors.default_duration_mins = "Choose a duration from 15 to 240 minutes in 15-minute steps.";
  }

  const rawWeekdays = draft.working_hours.map(({ weekday }) => weekday);
  if (
    rawWeekdays.length !== PROVIDER_WEEKDAYS.length
    || new Set(rawWeekdays).size !== PROVIDER_WEEKDAYS.length
    || PROVIDER_WEEKDAYS.some((weekday) => !rawWeekdays.includes(weekday))
  ) {
    errors.working_hours = "Working hours must include every weekday exactly once.";
  }

  for (const hours of normalized.working_hours) {
    if (!hours.enabled) continue;
    const startKey = `working_hours.${hours.weekday}.start_time`;
    const endKey = `working_hours.${hours.weekday}.end_time`;
    if (!hours.start_time || !isMinuteTime(hours.start_time)) {
      errors[startKey] = "Enter an opening time.";
    }
    if (!hours.end_time || !isMinuteTime(hours.end_time)) {
      errors[endKey] = "Enter a closing time.";
    } else if (hours.start_time && isMinuteTime(hours.start_time) && hours.end_time <= hours.start_time) {
      errors[endKey] = "Closing time must be later than opening time.";
    }
  }

  return errors;
}

export function providerFieldErrorsFromApi(
  error: unknown,
  payload: ProviderConfigurationUpdate,
): ProviderFieldErrors {
  if (!(error instanceof InstructorCommandApiError)) return {};

  const details = isRecord(error.details) ? error.details : null;
  if (error.code === "GEOCODING_FAILED" && typeof details?.field === "string") {
    const field = normalizeServerField(details.field);
    return field ? { [field]: error.message } : {};
  }

  if (error.code !== "VALIDATION_ERROR" || !Array.isArray(details?.fields)) return {};
  const errors: ProviderFieldErrors = {};
  for (const item of details.fields) {
    if (!isRecord(item) || !Array.isArray(item.loc) || typeof item.msg !== "string") continue;
    const field = fieldFromValidationLocation(item.loc, payload);
    if (field && !errors[field]) errors[field] = item.msg;
  }
  return errors;
}

export function firstProviderErrorField(errors: ProviderFieldErrors): string | null {
  if (errors.working_hours) return "working_hours";
  return PROVIDER_FIELD_ORDER.find((field) => errors[field]) ?? Object.keys(errors)[0] ?? null;
}

export function projectProviderOntoInstructor(
  instructor: InstructorProfile,
  provider: ProviderConfiguration,
): InstructorProfile {
  const firstEnabled = provider.working_hours.find((hours) => hours.enabled);
  return {
    ...instructor,
    id: provider.id,
    name: provider.display_name,
    business_name: provider.business_name || provider.display_name,
    timezone: provider.timezone,
    base_location: provider.base_location,
    service: provider.service,
    working_hours: provider.working_hours.map((hours) => ({ ...hours })),
    setup_complete: provider.setup_complete,
    setup_missing_fields: [...provider.setup_missing_fields],
    main_address: provider.service.location?.address ?? "",
    lat: provider.service.location?.lat ?? 0,
    lng: provider.service.location?.lng ?? 0,
    work_start_time: firstEnabled?.start_time ?? "",
    work_end_time: firstEnabled?.end_time ?? "",
  };
}

export function providerSetupSections(missingFields: readonly string[]): string[] {
  const sections = new Set<string>();
  for (const field of missingFields) {
    if (field === "display_name" || field === "business_name" || field === "timezone") {
      sections.add("Your profile");
    } else if (field.includes("address") || field.includes("location")) {
      sections.add("Locations");
    } else if (field.startsWith("service") || field === "default_duration_mins") {
      sections.add("Service");
    } else if (field.startsWith("working_hours")) {
      sections.add("Working hours");
    }
  }
  return [...sections];
}

export function weekdayLabel(weekday: ProviderWeekday): string {
  return weekday[0].toUpperCase() + weekday.slice(1);
}

function normalizeWorkingHour(hours: ProviderWorkingHour): ProviderWorkingHour {
  if (!hours.enabled) {
    return { weekday: hours.weekday, enabled: false, start_time: null, end_time: null };
  }
  return {
    weekday: hours.weekday,
    enabled: true,
    start_time: hours.start_time?.trim() || null,
    end_time: hours.end_time?.trim() || null,
  };
}

function requiredText(
  errors: ProviderFieldErrors,
  field: string,
  value: string,
  blankMessage: string,
  maxLength: number,
): void {
  if (!value) errors[field] = blankMessage;
  else if (value.length > maxLength) errors[field] = `Use ${maxLength} characters or fewer.`;
}

function optionalText(
  errors: ProviderFieldErrors,
  field: string,
  value: string | null,
  maxLength: number,
): void {
  if (value && value.length > maxLength) errors[field] = `Use ${maxLength} characters or fewer.`;
}

function isValidTimeZone(value: string): boolean {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: value }).format();
    return true;
  } catch {
    return false;
  }
}

function isMinuteTime(value: string): boolean {
  return /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value);
}

function fieldFromValidationLocation(
  location: unknown[],
  payload: ProviderConfigurationUpdate,
): string | null {
  const parts = location.filter((part) => part !== "body");
  if (parts[0] === "working_hours") {
    const index = typeof parts[1] === "number" ? parts[1] : Number(parts[1]);
    const hours = Number.isInteger(index) ? payload.working_hours[index] : undefined;
    const leaf = typeof parts[2] === "string" ? parts[2] : "end_time";
    return hours ? `working_hours.${hours.weekday}.${leaf}` : "working_hours";
  }
  const field = parts.find((part): part is string => typeof part === "string");
  return field ? normalizeServerField(field) : null;
}

function normalizeServerField(field: string): string | null {
  const aliases: Record<string, string> = {
    "base_location.address": "base_address",
    "service.location.address": "service_address",
    "service.name": "service_name",
    "service.default_duration_mins": "default_duration_mins",
  };
  const candidate = aliases[field] ?? field;
  if (PROVIDER_FIELD_ORDER.includes(candidate as (typeof PROVIDER_FIELD_ORDER)[number])) return candidate;
  return candidate === "working_hours" ? candidate : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
