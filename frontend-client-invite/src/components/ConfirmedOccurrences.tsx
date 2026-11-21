import type { LessonConfirmation } from "../api/types";

export function formatCalendarDateTime(value: string) {
  return new Date(value).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

export function ConfirmedOccurrences({ confirmation }: { confirmation: LessonConfirmation | null }) {
  const events = confirmation?.calendar_links.google_events ?? [];
  if (!events.length) return <strong>No active appointment dates available.</strong>;

  return <div className="confirmed-occurrences" aria-label="Active appointment dates">
    {events.map((event) => (
      <strong key={event.start_iso}>
        <time dateTime={event.start_iso}>{formatCalendarDateTime(event.start_iso)}</time>
        {" - "}
        <time dateTime={event.end_iso}>{formatCalendarDateTime(event.end_iso)}</time>
      </strong>
    ))}
    <small>Times shown in your device timezone.</small>
  </div>;
}
