import { CheckIcon } from "@radix-ui/react-icons";
import type { CalendarProvider, LessonRequest } from "../api/types";
import { CalendarActions } from "../components/CalendarActions";
import { SectionCard } from "../components/SectionCard";

type CalendarHandoffScreenProps = {
  lessonRequest: LessonRequest;
  durationMins: number;
  selectedCalendarProvider: CalendarProvider | null;
  onSelectProvider: (provider: CalendarProvider) => void;
};

export function CalendarHandoffScreen({
  lessonRequest,
  durationMins,
  selectedCalendarProvider,
  onSelectProvider,
}: CalendarHandoffScreenProps) {
  if (!lessonRequest.confirmation) {
    return null;
  }

  return (
    <div className="screen-stack">
      <SectionCard eyebrow="Confirmed lesson" title="Ready for your calendar">
        <section className="client-confirmed-card">
          <div className="delivery-row">
            <span className="delivery-chip">Text sent</span>
            <span className="delivery-chip">Email sent</span>
          </div>
          <div className="confirmed-message">
            <span>Riley confirmed your lesson</span>
            <strong>{lessonRequest.slot.display}</strong>
            <small>{lessonRequest.slot.location_address} - {durationMins} min</small>
          </div>
          <CalendarActions
            links={lessonRequest.confirmation.calendar_links}
            selectedProvider={selectedCalendarProvider}
            onSelect={onSelectProvider}
          />
          {selectedCalendarProvider ? (
            <p className="calendar-saved-note">
              <CheckIcon />
              Added to {selectedCalendarProvider === "google" ? "Google Calendar" : selectedCalendarProvider === "apple" ? "Apple Calendar" : "your downloads"}.
            </p>
          ) : null}
        </section>
      </SectionCard>
    </div>
  );
}
