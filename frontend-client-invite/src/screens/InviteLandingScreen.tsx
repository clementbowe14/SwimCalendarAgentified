import { DrawingPinIcon, EnvelopeClosedIcon, HomeIcon } from "@radix-ui/react-icons";
import { useRef } from "react";
import type { ClientIntake, LessonLocationType } from "../api/types";
import { SectionCard } from "../components/SectionCard";
import { ToggleButton } from "../components/ToggleButton";

const EMPTY_HOME_LOCATION_SENTINEL = "Enter home pool address";
const CLIENT_LOCATION_ADDRESS_PROMPT = "Enter appointment address";

type InviteLandingScreenProps = {
  intake: ClientIntake;
  instructorName: string;
  serviceName: string | null;
  serviceLocation: { address: string; lat: number; lng: number } | null;
  onChange: (intake: ClientIntake) => void;
  onContinue: () => void;
  saving: boolean;
};

export function InviteLandingScreen({
  intake,
  instructorName,
  serviceName,
  serviceLocation,
  onChange,
  onContinue,
  saving,
}: InviteLandingScreenProps) {
  const homeLocationDraft = useRef({
    ...(intake.location_type === "home"
      ? intake.location
      : { address: "", lat: null, lng: null, is_instructor_facility: false }),
    is_instructor_facility: false,
  });
  const needsHomePoolAddress =
    intake.location_type === "home" && intake.location.address === EMPTY_HOME_LOCATION_SENTINEL;

  const updateField = (field: keyof ClientIntake, value: string) => {
    onChange({ ...intake, [field]: value });
  };

  const updateLocationType = (type: LessonLocationType) => {
    if (type === intake.location_type) return;

    if (intake.location_type === "home") {
      homeLocationDraft.current = { ...intake.location, is_instructor_facility: false };
    }

    onChange({
      ...intake,
      location_type: type,
      location:
        type === "facility"
          ? {
              address: serviceLocation?.address ?? "Provider location unavailable",
              lat: serviceLocation?.lat ?? null,
              lng: serviceLocation?.lng ?? null,
              is_instructor_facility: true,
            }
          : { ...homeLocationDraft.current, is_instructor_facility: false },
    });
  };

  return (
    <form
      className="screen-stack intake-screen"
      onSubmit={(event) => {
        event.preventDefault();
        onContinue();
      }}
    >
      <SectionCard eyebrow="Your details" title="A few basics">
        <label>
          <span>Client name</span>
          <input value={intake.client_name} onChange={(event) => updateField("client_name", event.target.value)} />
        </label>
        <label>
          <span>Phone number</span>
          <input
            inputMode="tel"
            value={intake.guardian_phone}
            onChange={(event) => updateField("guardian_phone", event.target.value)}
          />
        </label>
        <label>
          <span>Email</span>
          <input
            inputMode="email"
            value={intake.guardian_email}
            onChange={(event) => updateField("guardian_email", event.target.value)}
          />
        </label>
        <div className="segmented-control" aria-label="Appointment location">
          <ToggleButton selected={intake.location_type === "home"} onClick={() => updateLocationType("home")}>
            <HomeIcon />
            My location
          </ToggleButton>
          <ToggleButton selected={intake.location_type === "facility"} onClick={() => updateLocationType("facility")}>
            <DrawingPinIcon />
            Provider location
          </ToggleButton>
        </div>
        <label>
          <span>{intake.location_type === "home" ? "Appointment address" : "Service location"}</span>
          <input
            value={needsHomePoolAddress ? "" : intake.location.address}
            placeholder={intake.location_type === "home" ? CLIENT_LOCATION_ADDRESS_PROMPT : undefined}
            required={intake.location_type === "home"}
            readOnly={intake.location_type === "facility"}
            onChange={(event) => {
              const location = {
                ...intake.location,
                address: event.target.value,
                is_instructor_facility: false,
              };
              homeLocationDraft.current = location;
              onChange({
                ...intake,
                location,
              });
            }}
          />
          {intake.location_type === "facility" && serviceName ? <small>{serviceName}</small> : null}
        </label>
      </SectionCard>

      <SectionCard eyebrow="Invite" title={`From ${instructorName}`}>
        <div className="summary-strip icon-strip">
          <EnvelopeClosedIcon />
          <span>
            Confirm your details once, then choose from appointment times that fit both your preferences and {instructorName}'s schedule.
          </span>
        </div>
        <label>
          <span>Anything {instructorName} should know?</span>
          <input value={intake.notes} onChange={(event) => updateField("notes", event.target.value)} />
        </label>
      </SectionCard>

      <button className="primary-button" type="submit" disabled={saving}>
        {saving ? "Saving details" : "Continue to availability"}
      </button>
    </form>
  );
}
