import type { InviteStep } from "../features/inviteFlow/flowModel";

const steps: Array<{ id: InviteStep; label: string }> = [
  { id: "intake", label: "Info" },
  { id: "preferences", label: "Times" },
  { id: "results", label: "Slots" },
  { id: "request", label: "Status" },
  { id: "calendar", label: "Calendar" },
];

type FlowProgressProps = {
  currentStep: InviteStep;
};

export function FlowProgress({ currentStep }: FlowProgressProps) {
  const currentIndex = steps.findIndex((step) => step.id === currentStep);

  return (
    <nav className="flow-progress" aria-label="Invite progress">
      {steps.map((step, index) => (
        <span className={index <= currentIndex ? "active" : ""} key={step.id}>
          {step.label}
        </span>
      ))}
    </nav>
  );
}

