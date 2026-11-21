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
  const matchedIndex = steps.findIndex((step) => step.id === currentStep);
  const currentIndex = matchedIndex >= 0 ? matchedIndex : 0;
  const currentStepConfig = steps[currentIndex];
  const stepNumber = currentIndex + 1;
  const percentComplete = Math.round((stepNumber / steps.length) * 100);

  return (
    <section className="flow-progress" aria-label="Invite progress">
      <div className="flow-progress-summary" aria-live="polite">
        <span>{`Step ${stepNumber} of ${steps.length}: ${currentStepConfig.label}`}</span>
        <strong>{`${percentComplete}% complete`}</strong>
      </div>
      <div
        className="flow-progress-meter"
        role="progressbar"
        aria-label="Invite completion"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percentComplete}
        aria-valuetext={`${percentComplete}% complete`}
      >
        <span style={{ width: `${percentComplete}%` }} />
      </div>
      <nav className="flow-progress-steps" aria-label="Invite progress steps">
        {steps.map((step, index) => {
          const stepState = index < currentIndex ? "complete" : index === currentIndex ? "current" : "upcoming";
          return (
            <span className={stepState} aria-current={stepState === "current" ? "step" : undefined} key={step.id}>
              {step.label}
            </span>
          );
        })}
      </nav>
    </section>
  );
}
