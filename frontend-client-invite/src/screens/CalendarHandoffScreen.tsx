import type { LessonRequest } from "../api/types";
import { NotificationDelivery } from "../components/NotificationDelivery";
import { CalendarActions } from "../components/CalendarActions";
import { SectionCard } from "../components/SectionCard";
import { ConfirmedOccurrences } from "../components/ConfirmedOccurrences";

type CalendarHandoffScreenProps = {
  lessonRequest: LessonRequest;
  onRefreshStatus: () => void;
  checkingStatus: boolean;
};

export function CalendarHandoffScreen({
  lessonRequest,
  onRefreshStatus,
  checkingStatus,
}: CalendarHandoffScreenProps) {
  return (
    <div className="screen-stack">
      <SectionCard eyebrow="Confirmed lesson" title={lessonRequest.confirmation ? "Ready for your calendar" : "Calendar unavailable"}>
        <section className="client-confirmed-card">
          <NotificationDelivery notifications={lessonRequest.notifications ?? []} />
          <div className="confirmed-message">
            <span>Lesson request confirmed</span>
            <ConfirmedOccurrences confirmation={lessonRequest.confirmation} />
            {lessonRequest.confirmation ? <small>{lessonRequest.slot.location_address}</small> : <p role="status">Calendar links are no longer available.</p>}
          </div>
          {lessonRequest.confirmation && <CalendarActions
            links={lessonRequest.confirmation.calendar_links}
          />}
          <button className="primary-button" type="button" onClick={onRefreshStatus} disabled={checkingStatus}>{checkingStatus ? "Checking status" : "Refresh status"}</button>
        </section>
      </SectionCard>
    </div>
  );
}
