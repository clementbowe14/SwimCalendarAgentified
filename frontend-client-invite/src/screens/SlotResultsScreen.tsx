import { ArrowLeftIcon, ExclamationTriangleIcon } from "@radix-ui/react-icons";
import type { AvailabilitySearchResponse, BookingRecurrence } from "../api/types";
import { SectionCard } from "../components/SectionCard";
import { SlotCard } from "../components/SlotCard";

type SlotResultsScreenProps = {
  response: AvailabilitySearchResponse;
  selectedSlotId: string | null;
  recurrence: BookingRecurrence;
  onSelectSlot: (slotId: string) => void;
  onRequestSlot: () => void;
  onBack: () => void;
  requesting: boolean;
};

export function SlotResultsScreen({
  response,
  selectedSlotId,
  recurrence,
  onSelectSlot,
  onRequestSlot,
  onBack,
  requesting,
}: SlotResultsScreenProps) {
  const selectedSlot = response.slots.find((slot) => slot.id === selectedSlotId);

  return (
    <div className="screen-stack">
      <button className="back-link" type="button" onClick={onBack}>
        <ArrowLeftIcon />
        Edit preferences
      </button>
      <SectionCard eyebrow="Open lesson times" title="Available to request" meta={`${response.slots.length} found`}>
        <div className="booking-note">
          <strong>Pick the time you prefer</strong>
          <span>{recurrence === "weekly" ? "Weekly request" : "One-time request"} - Riley confirms after final schedule check.</span>
        </div>
        <div className="slot-list">
          {response.slots.map((slot) => (
            <SlotCard
              slot={slot}
              selected={selectedSlotId === slot.id}
              key={slot.id}
              onSelect={() => onSelectSlot(slot.id)}
            />
          ))}
        </div>
      </SectionCard>

      {response.unavailable_windows.map((window) => (
        <div className="no-slots" key={window.id}>
          <ExclamationTriangleIcon />
          <div>
            <strong>No safe options {window.label}</strong>
            <span>{window.reason}</span>
          </div>
        </div>
      ))}

      {selectedSlot ? (
        <div className="confirmation-strip">
          <strong>{selectedSlot.display}</strong>
          <span>Riley will confirm or send a backup if anything changes.</span>
        </div>
      ) : null}

      <button className="primary-button" type="button" onClick={onRequestSlot} disabled={!selectedSlot || requesting}>
        {requesting ? "Sending request" : selectedSlot ? "Request this lesson" : "Select a time"}
      </button>
    </div>
  );
}

