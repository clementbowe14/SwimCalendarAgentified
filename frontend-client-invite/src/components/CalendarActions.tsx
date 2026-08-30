import { CalendarIcon, DownloadIcon } from "@radix-ui/react-icons";
import type { CalendarProvider, LessonConfirmation } from "../api/types";

type CalendarActionsProps = {
  links: LessonConfirmation["calendar_links"];
  selectedProvider: CalendarProvider | null;
  onSelect: (provider: CalendarProvider) => void;
};

export function CalendarActions({ links, selectedProvider, onSelect }: CalendarActionsProps) {
  return (
    <div className="calendar-button-row" aria-label="Add confirmed lesson to calendar">
      <button
        className={selectedProvider === "google" ? "calendar-save-button selected" : "calendar-save-button"}
        type="button"
        onClick={() => onSelect("google")}
      >
        <span className="calendar-mark google">G</span>
        Google Calendar
      </button>
      <button
        className={selectedProvider === "apple" ? "calendar-save-button selected" : "calendar-save-button"}
        type="button"
        onClick={() => onSelect("apple")}
      >
        <span className="calendar-mark apple">
          <CalendarIcon />
        </span>
        Apple Calendar
      </button>
      <button
        className={selectedProvider === "ics" ? "calendar-save-button selected wide" : "calendar-save-button wide"}
        type="button"
        onClick={() => onSelect("ics")}
      >
        <span className="calendar-mark ics">
          <DownloadIcon />
        </span>
        Download .ics
      </button>
      <span className="sr-only">{links.google}</span>
    </div>
  );
}

