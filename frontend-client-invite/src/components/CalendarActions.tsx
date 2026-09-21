import { useState } from "react";
import { CalendarIcon, DownloadIcon } from "@radix-ui/react-icons";
import type { LessonConfirmation } from "../api/types";
import { fetchCalendarDownload, trustedCalendarUrl } from "../api/calendar";

export function CalendarActions({ links }: { links: LessonConfirmation["calendar_links"] }) {
  const [showOccurrences, setShowOccurrences] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const resolve = (value: string, provider: "google" | "apple" | "ics") => trustedCalendarUrl(value, provider, import.meta.env.VITE_CLIENT_INVITE_API_URL || "", window.location.href);
  const safeGoogle = (value: string) => { try { return resolve(value, "google"); } catch { return undefined; } };
  const download = async (provider: "apple" | "ics") => {
    setError(null);
    setDownloading(true);
    try {
      const blob = await fetchCalendarDownload(resolve(links[provider], provider));
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "swim-lessons.ics";
      anchor.rel = "noreferrer noopener";
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Calendar download failed."); }
    finally { setDownloading(false); }
  };
  const events = links.google_events ?? [];
  return <>
    <div className="calendar-button-row" aria-label="Add confirmed lesson to calendar">
      {events.length > 1 ? <button className="calendar-save-button" type="button" aria-expanded={showOccurrences} onClick={() => setShowOccurrences(!showOccurrences)}><span className="calendar-mark google" aria-hidden="true">G</span>Google Calendar</button>
        : <a className="calendar-save-button" href={safeGoogle(events[0]?.url ?? links.google)} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer" aria-disabled={!safeGoogle(events[0]?.url ?? links.google)}><span className="calendar-mark google" aria-hidden="true">G</span>Google Calendar</a>}
      <button className="calendar-save-button" type="button" disabled={downloading} onClick={() => void download("apple")}><span className="calendar-mark apple"><CalendarIcon /></span>Apple Calendar</button>
      <button className="calendar-save-button wide" type="button" disabled={downloading} onClick={() => void download("ics")}><span className="calendar-mark ics"><DownloadIcon /></span>{downloading ? "Downloading" : "Download .ics"}</button>
    </div>
    {showOccurrences && <ul aria-label="Google Calendar occurrences">{events.map((event) => <li key={event.start_iso}><a href={safeGoogle(event.url)} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer" aria-disabled={!safeGoogle(event.url)}>{new Date(event.start_iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</a></li>)}</ul>}
    {error && <p role="alert" className="error-strip">{error}</p>}
    <p className="calendar-handoff-note">Calendar entries are snapshots. Save each Google occurrence or import the downloaded file in your calendar.</p>
  </>;
}
