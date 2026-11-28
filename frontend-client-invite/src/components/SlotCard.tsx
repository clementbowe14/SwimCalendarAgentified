import { ClockIcon, DrawingPinIcon } from "@radix-ui/react-icons";
import type { AvailabilitySlot } from "../api/types";

type SlotCardProps = {
  slot: AvailabilitySlot;
  durationMins: number;
  selected: boolean;
  disabled: boolean;
  onSelect: () => void;
};

export function SlotCard({ slot, durationMins, selected, disabled, onSelect }: SlotCardProps) {
  return (
    <button
      aria-pressed={selected}
      className={selected ? "slot-card selected" : "slot-card"}
      type="button"
      disabled={disabled}
      onClick={onSelect}
    >
      <span className="slot-dot" aria-hidden="true" />
      <div>
        <strong><time dateTime={slot.start_iso}>{slot.display}</time></strong>
        <div className="slot-meta-grid">
          <span>
            <DrawingPinIcon aria-hidden="true" />
            {slot.location_address}
          </span>
          <span>
            <ClockIcon aria-hidden="true" />
            {durationMins} min
          </span>
        </div>
      </div>
    </button>
  );
}

