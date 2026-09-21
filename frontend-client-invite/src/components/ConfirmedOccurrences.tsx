import type { LessonConfirmation } from "../api/types";

export function ConfirmedOccurrences({ confirmation }: { confirmation: LessonConfirmation | null }) {
  const events = confirmation?.calendar_links.google_events ?? [];
  if (!events.length) return <strong>No active lesson dates available.</strong>;

  return <div className="confirmed-occurrences" aria-label="Active lesson dates">
    {events.map((event) => (
      <strong key={event.start_iso}>
        <time dateTime={event.start_iso}>{new Date(event.start_iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</time>
        {" - "}
        <time dateTime={event.end_iso}>{new Date(event.end_iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</time>
      </strong>
    ))}
  </div>;
}
