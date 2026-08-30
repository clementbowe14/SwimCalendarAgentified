import { DrawingPinIcon, EnvelopeClosedIcon, HomeIcon } from "@radix-ui/react-icons";
import type { ClientIntake, LessonLocationType } from "../api/types";
import { SectionCard } from "../components/SectionCard";
import { ToggleButton } from "../components/ToggleButton";

type InviteLandingScreenProps = {
  intake: ClientIntake;
  instructorName: string;
  onChange: (intake: ClientIntake) => void;
  onContinue: () => void;
  saving: boolean;
};

export function InviteLandingScreen({ intake, instructorName, onChange, onContinue, saving }: InviteLandingScreenProps) {
  const updateField = (field: keyof ClientIntake, value: string) => {
    onChange({ ...intake, [field]: value });
  };

  const updateLocationType = (type: LessonLocationType) => {
    onChange({
      ...intake,
      location_type: type,
      location: {
        ...intake.location,
        address: type === "facility" ? "Riverside facility" : "820 Lakeview Dr, Austin, TX",
        lat: type === "facility" ? 30.2672 : intake.location.lat,
        lng: type === "facility" ? -97.7431 : intake.location.lng,
        is_instructor_facility: type === "facility",
      },
    });
  };

  return (
    <form
      className="screen-stack"
      onSubmit={(event) => {
        event.preventDefault();
        onContinue();
      }}
    >
      <SectionCard eyebrow="Client info" title="Basics">
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
        <div className="segmented-control" aria-label="Lesson location">
          <ToggleButton selected={intake.location_type === "home"} onClick={() => updateLocationType("home")}>
            <HomeIcon />
            Home pool
          </ToggleButton>
          <ToggleButton selected={intake.location_type === "facility"} onClick={() => updateLocationType("facility")}>
            <DrawingPinIcon />
            Facility
          </ToggleButton>
        </div>
        <label>
          <span>{intake.location_type === "home" ? "Home pool address" : "Preferred facility"}</span>
          <input
            value={intake.location.address}
            onChange={(event) =>
              onChange({
                ...intake,
                location: {
                  ...intake.location,
                  address: event.target.value,
                },
              })
            }
          />
        </label>
      </SectionCard>

      <SectionCard eyebrow="Invite" title={`From ${instructorName}`}>
        <div className="summary-strip icon-strip">
          <EnvelopeClosedIcon />
          <span>
            Confirm your details once, then choose from lesson times that fit both your preferences and Riley's schedule.
          </span>
        </div>
        <label>
          <span>Anything Riley should know?</span>
          <input value={intake.notes} onChange={(event) => updateField("notes", event.target.value)} />
        </label>
      </SectionCard>

      <button className="primary-button" type="submit" disabled={saving}>
        {saving ? "Saving details" : "Continue to availability"}
      </button>
    </form>
  );
}
