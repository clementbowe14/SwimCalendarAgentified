import { ClockIcon, HomeIcon, LoopIcon } from "@radix-ui/react-icons";
import type { AvailabilityWindow, BookingRecurrence, DayKey } from "../api/types";
import { SectionCard } from "../components/SectionCard";
import { ToggleButton } from "../components/ToggleButton";
import {
  formatTimeRange,
  summarizeAvailability,
  timeWindowPresets,
  upsertWindowDays,
  upsertWindowTime,
  weekdayOptions,
  weekendOptions,
} from "../features/inviteFlow/flowModel";

type AvailabilityPreferencesScreenProps = {
  durationMins: number;
  allowedDurationsMins: number[];
  recurrence: BookingRecurrence;
  windows: AvailabilityWindow[];
  onDurationChange: (duration: number) => void;
  onRecurrenceChange: (recurrence: BookingRecurrence) => void;
  onWindowsChange: (windows: AvailabilityWindow[]) => void;
  onFindSlots: () => void;
  searching: boolean;
};

export function AvailabilityPreferencesScreen({
  durationMins,
  allowedDurationsMins,
  recurrence,
  windows,
  onDurationChange,
  onRecurrenceChange,
  onWindowsChange,
  onFindSlots,
  searching,
}: AvailabilityPreferencesScreenProps) {
  const weekdayWindow = windows.find((window) => window.id === "weekday_after_school") ?? windows[0];
  const weekendWindow = windows.find((window) => window.id === "weekend_late_morning") ?? windows[1];

  const toggleDay = (windowId: string, day: DayKey) => {
    const window = windows.find((item) => item.id === windowId);
    if (!window) return;
    const days = window.days.includes(day) ? window.days.filter((item) => item !== day) : [...window.days, day];
    onWindowsChange(upsertWindowDays(windows, windowId, days));
  };

  return (
    <div className="screen-stack preference-screen">
      <SectionCard eyebrow="Availability" title="Your open windows" meta={`${durationMins} min`}>
        <div className="session-grid" aria-label="Session length">
          {allowedDurationsMins.map((duration) => (
            <ToggleButton selected={durationMins === duration} key={duration} onClick={() => onDurationChange(duration)}>
              {duration} min
            </ToggleButton>
          ))}
        </div>
        <div className="segmented-control recurrence-control" aria-label="Booking recurrence">
          <ToggleButton selected={recurrence === "single"} onClick={() => onRecurrenceChange("single")}>
            <ClockIcon />
            One time
          </ToggleButton>
          <ToggleButton selected={recurrence === "weekly"} onClick={() => onRecurrenceChange("weekly")}>
            <LoopIcon />
            Weekly
          </ToggleButton>
        </div>
      </SectionCard>

      <AvailabilityGroup
        title="Weekdays"
        days={weekdayOptions}
        window={weekdayWindow}
        onToggleDay={toggleDay}
        onTimeChange={(start, end) => onWindowsChange(upsertWindowTime(windows, weekdayWindow.id, start, end))}
      />

      <AvailabilityGroup
        title="Weekend"
        days={weekendOptions}
        window={weekendWindow}
        onToggleDay={toggleDay}
        onTimeChange={(start, end) => onWindowsChange(upsertWindowTime(windows, weekendWindow.id, start, end))}
      />

      <div className="summary-strip icon-strip">
        <HomeIcon />
        <span>{summarizeAvailability(windows)}</span>
      </div>

      <button className="primary-button" type="button" onClick={onFindSlots} disabled={searching}>
        {searching ? "Finding available times" : "Show available times"}
      </button>
    </div>
  );
}

type AvailabilityGroupProps = {
  title: string;
  days: DayKey[];
  window: AvailabilityWindow;
  onToggleDay: (windowId: string, day: DayKey) => void;
  onTimeChange: (startTime: string, endTime: string) => void;
};

function AvailabilityGroup({ title, days, window, onToggleDay, onTimeChange }: AvailabilityGroupProps) {
  return (
    <SectionCard title={title} meta={formatTimeRange(window.start_time, window.end_time)}>
      <div className={days.length === 2 ? "day-grid weekend" : "day-grid"} aria-label={`${title} days`}>
        {days.map((day) => (
          <ToggleButton selected={window.days.includes(day)} key={day} onClick={() => onToggleDay(window.id, day)}>
            {day}
          </ToggleButton>
        ))}
      </div>
      <div className="window-grid" aria-label={`${title} time availability`}>
        {timeWindowPresets.map((preset) => (
          <ToggleButton
            selected={window.start_time === preset.start_time && window.end_time === preset.end_time}
            key={preset.id}
            onClick={() => onTimeChange(preset.start_time, preset.end_time)}
          >
            {preset.label}
          </ToggleButton>
        ))}
      </div>
    </SectionCard>
  );
}

