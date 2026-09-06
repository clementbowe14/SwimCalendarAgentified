import { CalendarIcon, ChatBubbleIcon, CheckCircledIcon, ClockIcon } from "@radix-ui/react-icons";
import type { LessonRequest } from "../api/types";
import { SectionCard } from "../components/SectionCard";

type LessonRequestStatusScreenProps = {
  lessonRequest: LessonRequest;
  onRefreshStatus: () => void;
  onSelectAlternative: (slotId: string) => void;
  onCalendar: () => void;
  checkingStatus: boolean;
};

export function LessonRequestStatusScreen({
  lessonRequest,
  onRefreshStatus,
  onSelectAlternative,
  onCalendar,
  checkingStatus,
}: LessonRequestStatusScreenProps) {
  const confirmed = lessonRequest.status === "confirmed" && lessonRequest.confirmation;
  const needsNewTimes = lessonRequest.status === "needs_new_times";
  const expired = lessonRequest.status === "expired";

  return (
    <div className="screen-stack">
      <SectionCard eyebrow="Lesson request" title={confirmed ? "Lesson confirmed" : needsNewTimes ? "Choose a new time" : expired ? "Request expired" : "Request sent"}>
        <div className={confirmed ? "status-panel confirmed" : "status-panel"}>
          <span className="status-icon">{confirmed ? <CheckCircledIcon /> : <ClockIcon />}</span>
          <div>
            <strong>{lessonRequest.slot.display}</strong>
            <span>
              {confirmed
                ? lessonRequest.confirmation?.message
                : expired
                  ? "This request expired before it could be confirmed. Please choose another time from your invite."
                  : needsNewTimes
                    ? "Riley sent new lesson times for you to review."
                : "Riley has the request and the slot is held while the final schedule check runs."}
            </span>
          </div>
        </div>
        {needsNewTimes && lessonRequest.alternative_slots.length ? (
          <div className="alternative-slot-list" aria-label="Alternative lesson times">
            {lessonRequest.alternative_slots.map((slot) => (
              <button
                className="slot-option compact"
                type="button"
                key={slot.id}
                onClick={() => onSelectAlternative(slot.id)}
              >
                <strong>{slot.display}</strong>
                <span>{slot.safety_reason}</span>
              </button>
            ))}
          </div>
        ) : null}
        <div className="request-timeline">
          <span className="done">
            <ChatBubbleIcon />
            Text and email request sent
          </span>
          <span className={confirmed ? "done" : ""}>
            <CheckCircledIcon />
            Instructor confirms the appointment
          </span>
          <span className={confirmed ? "done" : ""}>
            <CalendarIcon />
            Calendar links unlock for the client
          </span>
        </div>
      </SectionCard>

      {confirmed ? (
        <button className="primary-button" type="button" onClick={onCalendar}>
          Add to calendar
        </button>
      ) : needsNewTimes || expired ? null : (
        <button className="primary-button" type="button" onClick={onRefreshStatus} disabled={checkingStatus}>
          {checkingStatus ? "Checking status" : "Check confirmation"}
        </button>
      )}
    </div>
  );
}
