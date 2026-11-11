export function formatTime(isoDateTime: string, timeZone?: string): string {
  const date = new Date(isoDateTime);
  return new Intl.DateTimeFormat("en-US", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: explicitTimeZone(isoDateTime, timeZone),
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
