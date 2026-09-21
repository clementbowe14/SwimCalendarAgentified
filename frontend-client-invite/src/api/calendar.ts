export function trustedCalendarUrl(value: string, provider: "google" | "apple" | "ics", apiBase: string, documentUrl: string): string {
  if (!value.trim()) throw new Error("Calendar link is unavailable.");
  const backend = new URL(apiBase || "/", documentUrl);
  const url = new URL(value, `${backend.origin}/`);
  if (url.username || url.password || !["https:", "http:"].includes(url.protocol)) throw new Error("Calendar link is unavailable.");
  if (provider === "google") {
    if (url.protocol !== "https:" || url.hostname !== "calendar.google.com" || url.port || !url.pathname.startsWith("/calendar/")) throw new Error("Calendar link is unavailable.");
  } else if (url.origin !== backend.origin) throw new Error("Calendar download is unavailable.");
  return url.href;
}

export async function fetchCalendarDownload(url: string): Promise<Blob> {
  const response = await fetch(url, { referrerPolicy: "no-referrer", redirect: "error", credentials: "omit", cache: "no-store" });
  if (!response.ok || response.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "text/calendar") throw new Error("Calendar download failed. Please refresh and try again.");
  return response.blob();
}
