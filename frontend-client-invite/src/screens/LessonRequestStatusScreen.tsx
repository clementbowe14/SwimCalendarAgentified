import { CalendarIcon, ChatBubbleIcon, CheckCircledIcon, ClockIcon } from "@radix-ui/react-icons";
import type { LessonRequest } from "../api/types";
import { SectionCard } from "../components/SectionCard";
import { NotificationDelivery } from "../components/NotificationDelivery";
import { ConfirmedOccurrences } from "../components/ConfirmedOccurrences";

type LessonRequestStatusScreenProps = {
  lessonRequest: LessonRequest;
  instructorName: string;
  onRefreshStatus: () => void;
  onSelectAlternative: (slotId: string) => void;
  onCalendar: () => void;
  checkingStatus: boolean;
};

export function LessonRequestStatusScreen({
  lessonRequest,
  instructorName,
  onRefreshStatus,
  onSelectAlternative,
  onCalendar,
  checkingStatus,
}: LessonRequestStatusScreenProps) {
  const confirmed = lessonRequest.status === "confirmed";
  const calendarAvailable = confirmed && Boolean(lessonRequest.confirmation);
  const needsNewTimes = lessonRequest.status === "needs_new_times";
  const expired = lessonRequest.status === "expired";

  return (
    <div className="screen-stack request-screen">
      <SectionCard eyebrow="Appointment request" title={confirmed ? "Appointment confirmed" : needsNewTimes ? "Choose a new time" : expired ? "Request expired" : "Request sent"}>
        <div className={confirmed ? "status-panel confirmed" : "status-panel"}>
          <span className="status-icon">{confirmed ? <CheckCircledIcon /> : <ClockIcon />}</span>
          <div>
            {confirmed ? <ConfirmedOccurrences confirmation={lessonRequest.confirmation} /> : <strong>{lessonRequest.slot.display}</strong>}
            <span>
              {confirmed
                ? lessonRequest.confirmation?.message ?? "This request was confirmed. Calendar links are no longer available."
                : expired
                  ? "This request expired before it could be confirmed. Please choose another time from your invite."
                  : needsNewTimes
                    ? `${instructorName} sent new appointment times for you to review.`
                : `${instructorName} has the request and the slot is held while the final schedule check runs.`}
            </span>
          </div>
        </div>
        {needsNewTimes && lessonRequest.alternative_slots.length ? (
          <div className="alternative-slot-list" aria-label="Alternative appointment times">
            {lessonRequest.alternative_slots.map((slot) => (
              <button
                className="slot-option compact"
                type="button"
                key={slot.id}
                onClick={() => onSelectAlternative(slot.id)}
              >
                <strong>{slot.display}</strong>
                <span>Available to request</span>
              </button>
            ))}
          </div>
        ) : null}
        <div className="request-timeline">
          <span className="done">
            <ChatBubbleIcon />
            Appointment request received
          </span>
          <span className={confirmed ? "done" : ""}>
            <CheckCircledIcon />
            Provider confirms the appointment
          </span>
          <span className={calendarAvailable ? "done" : ""}>
            <CalendarIcon />
            {confirmed && !calendarAvailable ? "Calendar links unavailable" : "Calendar links unlock for the client"}
          </span>
        </div>
        <NotificationDelivery notifications={lessonRequest.notifications ?? []} />
      </SectionCard>

      {calendarAvailable ? (
        <button className="primary-button" type="button" onClick={onCalendar}>
          Add to calendar
        </button>
      ) : confirmed || needsNewTimes || expired ? null : (
        <button className="primary-button" type="button" onClick={onRefreshStatus} disabled={checkingStatus}>
          {checkingStatus ? "Checking status" : "Check confirmation"}
        </button>
      )}
      {confirmed && <button className="primary-button" type="button" onClick={onRefreshStatus} disabled={checkingStatus}>{checkingStatus ? "Checking status" : "Refresh status"}</button>}
    </div>
  );
}
