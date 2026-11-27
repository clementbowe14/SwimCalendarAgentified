export function formatTime(isoDateTime: string, timeZone?: string, showTimeZone = false): string {
  const date = new Date(isoDateTime);
  return new Intl.DateTimeFormat("en-US", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: explicitTimeZone(isoDateTime, timeZone),
    timeZoneName: showTimeZone ? "short" : undefined,
  }).format(date);
}

export function formatShortDate(isoDateTime: string, timeZone?: string): string {
  const date = new Date(isoDateTime);
  return new Intl.DateTimeFormat("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
    timeZone: explicitTimeZone(isoDateTime, timeZone),
  }).format(date);
}

export function formatTimeRange(startIso: string, endIso: string, timeZone?: string): string {
  return `${formatTime(startIso, timeZone)}-${formatTime(endIso, timeZone)}`;
}

export function minutesBetween(startIso: string, endIso: string): number {
  return Math.round((new Date(endIso).getTime() - new Date(startIso).getTime()) / 60000);
}

export function dateKeyInTimeZone(date: Date, timeZone: string): string {
  return new Intl.DateTimeFormat("en-CA-u-ca-iso8601-nu-latn", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

export function supportedScheduleTimeZone(value?: string): string | null {
  if (value) {
    try {
      return new Intl.DateTimeFormat("en-US", { timeZone: value }).resolvedOptions().timeZone;
    } catch (error) {
      if (!(error instanceof RangeError)) throw error;
    }
  }
  return null;
}

export function scheduleTimeZone(value?: string): string {
  return supportedScheduleTimeZone(value) ?? "America/Chicago";
}

export function shiftDateKey(date: string, days: number): string {
  const day = new Date(`${date}T00:00:00Z`);
  day.setUTCDate(day.getUTCDate() + days);
  return day.toISOString().slice(0, 10);
}

export function scheduleLocalFields(isoDateTime: string, timeZone: string): { date: string; time: string } {
  if (!explicitTimeZone(isoDateTime, timeZone)) {
    return { date: isoDateTime.slice(0, 10), time: isoDateTime.slice(11, 16) };
  }
  const instant = new Date(isoDateTime);
  return {
    date: dateKeyInTimeZone(instant, timeZone),
    time: new Intl.DateTimeFormat("en-GB", {
      timeZone,
      hourCycle: "h23",
      hour: "2-digit",
      minute: "2-digit",
    }).format(instant),
  };
}

export function fullNameInitials(name: string): string {
  return name
    .split(" ")
    .map((part) => part[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

export function titleCase(value: string): string {
  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function explicitTimeZone(isoDateTime: string, timeZone?: string): string | undefined {
  if (!timeZone) return undefined;
  return /(?:Z|[+-]\d{2}:\d{2})$/i.test(isoDateTime) ? timeZone : undefined;
}
