import { ArrowLeftIcon, ExclamationTriangleIcon } from "@radix-ui/react-icons";
import type { AvailabilitySearchResponse } from "../api/types";
import { SectionCard } from "../components/SectionCard";
import { SlotCard } from "../components/SlotCard";

type SlotResultsScreenProps = {
  response: AvailabilitySearchResponse;
  instructorName: string;
  selectedSlotId: string | null;
  onSelectSlot: (slotId: string) => void;
  onRequestSlot: () => void;
  onBack: () => void;
  requesting: boolean;
};

const searchDateFormat = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
  timeZone: "UTC",
});

export function SlotResultsScreen({
  response,
  instructorName,
  selectedSlotId,
  onSelectSlot,
  onRequestSlot,
  onBack,
  requesting,
}: SlotResultsScreenProps) {
  const selectedSlot = response.slots.find((slot) => slot.id === selectedSlotId);
  const searchedDates = searchDateFormat.formatRange(
    new Date(`${response.start_date}T00:00:00Z`),
    new Date(`${response.end_date}T00:00:00Z`),
  );

  return (
    <div className="screen-stack results-screen">
      <button className="back-link" type="button" onClick={onBack} disabled={requesting}>
        <ArrowLeftIcon />
        Edit preferences
      </button>
      <SectionCard eyebrow="Open times" title="Available to request" meta={`${response.slots.length} found`}>
        <div className="booking-note">
          <strong>Searched {searchedDates}</strong>
        </div>
        {response.slots.length > 0 ? (
          <div className="slot-list">
            {response.slots.map((slot) => (
              <SlotCard
                slot={slot}
                durationMins={response.duration_mins}
                selected={selectedSlotId === slot.id}
                disabled={requesting}
                key={slot.id}
                onSelect={() => onSelectSlot(slot.id)}
              />
            ))}
          </div>
        ) : (
          <div className="no-slots" role="status">
            <ExclamationTriangleIcon aria-hidden="true" />
            <div>
              <strong>No appointment times found</strong>
              <span>Try changing your days, time windows, or duration.</span>
            </div>
          </div>
        )}
      </SectionCard>

      {selectedSlot ? (
        <div className="confirmation-strip">
          <strong>{selectedSlot.display}</strong>
          <span>{instructorName} will confirm or send a backup if anything changes.</span>
        </div>
      ) : null}

      {selectedSlot ? (
        <button className="primary-button" type="button" onClick={onRequestSlot} disabled={requesting}>
          {requesting ? "Sending request" : "Request this time"}
        </button>
      ) : null}
    </div>
  );
}

