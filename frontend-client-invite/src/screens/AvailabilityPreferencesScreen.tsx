import { HomeIcon } from "@radix-ui/react-icons";
import type { AvailabilityWindow, DayKey } from "../api/types";
import { SectionCard } from "../components/SectionCard";
import { ToggleButton } from "../components/ToggleButton";
import {
  dayLabel,
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
  windows: AvailabilityWindow[];
  onDurationChange: (duration: number) => void;
  onWindowsChange: (windows: AvailabilityWindow[]) => void;
  onFindSlots: () => void;
  searching: boolean;
};

export function AvailabilityPreferencesScreen({
  durationMins,
  allowedDurationsMins,
  windows,
  onDurationChange,
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
            <ToggleButton
              selected={durationMins === duration}
              key={duration}
              disabled={searching}
              onClick={() => onDurationChange(duration)}
            >
              {duration} min
            </ToggleButton>
          ))}
        </div>
      </SectionCard>

      <AvailabilityGroup
        title="Weekdays"
        days={weekdayOptions}
        window={weekdayWindow}
        disabled={searching}
        onToggleDay={toggleDay}
        onTimeChange={(start, end) => onWindowsChange(upsertWindowTime(windows, weekdayWindow.id, start, end))}
      />

      <AvailabilityGroup
        title="Weekend"
        days={weekendOptions}
        window={weekendWindow}
        disabled={searching}
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
  disabled: boolean;
  onToggleDay: (windowId: string, day: DayKey) => void;
  onTimeChange: (startTime: string, endTime: string) => void;
};

function AvailabilityGroup({ title, days, window, disabled, onToggleDay, onTimeChange }: AvailabilityGroupProps) {
  return (
    <SectionCard title={title} meta={formatTimeRange(window.start_time, window.end_time)}>
      <div className={days.length === 2 ? "day-grid weekend" : "day-grid"} aria-label={`${title} days`}>
        {days.map((day) => (
          <ToggleButton
            selected={window.days.includes(day)}
            key={day}
            disabled={disabled}
            onClick={() => onToggleDay(window.id, day)}
          >
            {dayLabel(day)}
          </ToggleButton>
        ))}
      </div>
      <div className="window-grid" aria-label={`${title} time availability`}>
        {timeWindowPresets.map((preset) => (
          <ToggleButton
            selected={window.start_time === preset.start_time && window.end_time === preset.end_time}
            key={preset.id}
            disabled={disabled}
            onClick={() => onTimeChange(preset.start_time, preset.end_time)}
          >
            {preset.label}
          </ToggleButton>
        ))}
      </div>
    </SectionCard>
  );
}

