import { CheckIcon, ClockIcon, DrawingPinIcon } from "@radix-ui/react-icons";
import type { SchedulerSlot } from "../api/types";

type SlotCardProps = {
  slot: SchedulerSlot;
  selected: boolean;
  onSelect: () => void;
};

export function SlotCard({ slot, selected, onSelect }: SlotCardProps) {
  return (
    <button className={selected ? "slot-card selected" : "slot-card"} type="button" onClick={onSelect}>
      <span className="slot-dot" aria-hidden="true">
        {selected ? <CheckIcon /> : null}
      </span>
      <div>
        <strong>{slot.display}</strong>
        <span className="slot-facts">Available to request</span>
        <div className="slot-meta-grid">
          <span>
            <DrawingPinIcon />
            {slot.location_address}
          </span>
          <span>
            <ClockIcon />
            {slot.recurrence_available ? "Weekly eligible" : "One-time only"}
          </span>
        </div>
      </div>
    </button>
  );
}

