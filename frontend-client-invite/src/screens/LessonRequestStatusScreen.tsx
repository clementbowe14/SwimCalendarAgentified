import { CalendarIcon, ChatBubbleIcon, CheckCircledIcon, ClockIcon } from "@radix-ui/react-icons";
import type { LessonRequest } from "../api/types";
import { SectionCard } from "../components/SectionCard";

type LessonRequestStatusScreenProps = {
  lessonRequest: LessonRequest;
  onRefreshStatus: () => void;
  onCalendar: () => void;
  checkingStatus: boolean;
};

export function LessonRequestStatusScreen({
  lessonRequest,
  onRefreshStatus,
  onCalendar,
  checkingStatus,
}: LessonRequestStatusScreenProps) {
  const confirmed = lessonRequest.status === "confirmed" && lessonRequest.confirmation;

  return (
    <div className="screen-stack">
      <SectionCard eyebrow="Lesson request" title={confirmed ? "Lesson confirmed" : "Request sent"}>
        <div className={confirmed ? "status-panel confirmed" : "status-panel"}>
          <span className="status-icon">{confirmed ? <CheckCircledIcon /> : <ClockIcon />}</span>
          <div>
            <strong>{lessonRequest.slot.display}</strong>
            <span>
              {confirmed
                ? lessonRequest.confirmation?.message
                : "Riley has the request and the slot is held while the final schedule check runs."}
            </span>
          </div>
        </div>
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
      ) : (
        <button className="primary-button" type="button" onClick={onRefreshStatus} disabled={checkingStatus}>
          {checkingStatus ? "Checking status" : "Check confirmation"}
        </button>
      )}
    </div>
  );
}

