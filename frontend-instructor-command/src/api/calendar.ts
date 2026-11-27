const GOOGLE_CALENDAR_HOST = "calendar.google.com";
const GOOGLE_CALENDAR_AUTHORIZATION_PREFIX = "https://accounts.google.com/o/oauth2/v2/auth?";
const COACH_ICS_PATH = /^\/api\/coach\/appointments\/[^/]+\/calendar\.ics$/;

export function trustedGoogleCalendarAuthorizationUrl(value: unknown): string {
  if (
    typeof value !== "string"
    || !value.startsWith(GOOGLE_CALENDAR_AUTHORIZATION_PREFIX)
    || value.length === GOOGLE_CALENDAR_AUTHORIZATION_PREFIX.length
    || /[^\x21-\x7e]|#/.test(value)
  ) {
    throw new Error("Google Calendar authorization link is unavailable.");
  }
  return value;
}

export function trustedGoogleCalendarUrl(value: string): string {
  if (!value.trim()) throw new Error("Google Calendar link is unavailable.");
  const url = new URL(value);
  if (
    url.protocol !== "https:" ||
    url.hostname !== GOOGLE_CALENDAR_HOST ||
    url.port ||
    url.username ||
    url.password ||
    !url.pathname.startsWith("/calendar/")
  ) {
    throw new Error("Google Calendar link is unavailable.");
  }
  return url.href;
}

export function trustedInstructorCalendarUrl(
  value: string,
  apiBase: string,
  documentUrl: string,
): string {
  if (!value.trim()) throw new Error("Calendar download is unavailable.");
  const backend = new URL(apiBase || "/", documentUrl);
  const url = new URL(value, `${backend.origin}/`);
  if (
    !["https:", "http:"].includes(url.protocol) ||
    url.origin !== backend.origin ||
    url.username ||
    url.password ||
    url.hash ||
    url.search ||
    !COACH_ICS_PATH.test(url.pathname)
  ) {
    throw new Error("Calendar download is unavailable.");
  }
  return url.href;
}
