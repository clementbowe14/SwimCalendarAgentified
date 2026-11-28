import { useEffect, useRef, useState } from "react";
import type {
  AvailabilitySearchResponse,
  ClientIntake,
  ClientInviteApi,
  ClientInviteEnvelope,
  LessonRequest,
  RequestStatus,
} from "./api/types";
import { ClientInviteApiError } from "./api/clientInviteApi";
import type { ClientInviteDataMode } from "./api/dataMode";
import { BrandHeader } from "./components/BrandHeader";
import { FlowProgress } from "./components/FlowProgress";
import { inviteEnvelope } from "./data/mockInvite";
import {
  buildSearchRequest,
  initialAvailabilityWindows,
  type InviteFlowState,
  type InviteStep,
} from "./features/inviteFlow/flowModel";
import { AvailabilityPreferencesScreen } from "./screens/AvailabilityPreferencesScreen";
import { CalendarHandoffScreen } from "./screens/CalendarHandoffScreen";
import { InviteLandingScreen } from "./screens/InviteLandingScreen";
import { LessonRequestStatusScreen } from "./screens/LessonRequestStatusScreen";
import { SlotResultsScreen } from "./screens/SlotResultsScreen";

type AppProps = {
  api: ClientInviteApi;
  dataMode: ClientInviteDataMode["mode"];
  inviteToken: string | null;
  showMockBadge: boolean;
};

type BootstrapStatus = "loading" | "ready" | "error" | "missing-token";

export default function App({ api, dataMode, inviteToken, showMockBadge }: AppProps) {
  const initialEnvelope = dataMode === "mock" ? createMockEnvelope(inviteToken ?? inviteEnvelope.invite_token) : null;
  const [envelope, setEnvelope] = useState<ClientInviteEnvelope | null>(initialEnvelope);
  const [intake, setIntake] = useState<ClientIntake | null>(initialEnvelope?.intake ?? null);
  const [flow, setFlow] = useState<InviteFlowState>(() => createFlow(initialEnvelope));
  const [slotResponse, setSlotResponse] = useState<AvailabilitySearchResponse | null>(null);
  const [lessonRequest, setLessonRequest] = useState<LessonRequest | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [bootstrapStatus, setBootstrapStatus] = useState<BootstrapStatus>(
    dataMode === "mock" ? "ready" : inviteToken ? "loading" : "missing-token",
  );
  const [bootstrapError, setBootstrapError] = useState("");
  const [bootstrapAttempt, setBootstrapAttempt] = useState(0);
  const stepTitleRef = useRef<HTMLHeadingElement>(null);
  const previousStepRef = useRef<InviteStep>(flow.step);
  const previousRequestStatusRef = useRef<RequestStatus | null>(lessonRequest?.status ?? null);

  useEffect(() => {
    if (dataMode !== "http" || !inviteToken) return;

    let active = true;
    setBootstrapStatus("loading");
    setBootstrapError("");
    setEnvelope(null);
    setIntake(null);
    setSlotResponse(null);
    setLessonRequest(null);
    setFlow(createFlow(null));

    api
      .getInvite(inviteToken)
      .then((nextEnvelope) => {
        if (!active) return;
        setEnvelope(nextEnvelope);
        setIntake(nextEnvelope.intake);
        setFlow(createFlow(nextEnvelope));
        setBootstrapStatus("ready");
      })
      .catch((error: unknown) => {
        if (!active) return;
        setBootstrapError(readApiError(error));
        setBootstrapStatus("error");
      });

    return () => {
      active = false;
    };
  }, [api, bootstrapAttempt, dataMode, inviteToken]);

  useEffect(() => {
    const didStepChange = previousStepRef.current !== flow.step;
    const requestStatus = lessonRequest?.status ?? null;
    const didRequestStatusChange = previousRequestStatusRef.current !== requestStatus;
    previousStepRef.current = flow.step;
    previousRequestStatusRef.current = requestStatus;
    if (!didStepChange && !didRequestStatusChange) return;

    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    const frame = window.requestAnimationFrame(() => {
      stepTitleRef.current?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [flow.step, lessonRequest?.status]);

  if (bootstrapStatus !== "ready" || !envelope || !intake) {
    return (
      <BootstrapScreen
        status={bootstrapStatus}
        error={bootstrapError}
        onRetry={() => setBootstrapAttempt((attempt) => attempt + 1)}
      />
    );
  }

  const heroContent = getHeroContent(flow.step, envelope.instructor_name, lessonRequest);
  const goToStep = (step: InviteStep) => setFlow((current) => ({ ...current, step }));

  const continueFromIntake = async () => {
    setLoading(true);
    setErrorMessage(null);
    try {
      const nextEnvelope = await api.submitIntake(envelope.invite_token, intake);
      setEnvelope(nextEnvelope);
      setIntake(nextEnvelope.intake);
      goToStep("preferences");
    } catch (error) {
      setErrorMessage(readApiError(error));
    } finally {
      setLoading(false);
    }
  };

  const findSlots = async () => {
    if (loading) return;
    setLoading(true);
    setErrorMessage(null);
    try {
      const response = await api.findAvailableSlots(
        envelope.invite_token,
        buildSearchRequest(intake, flow.duration_mins, flow.windows),
      );
      setSlotResponse(response);
      setFlow((current) => ({
        ...current,
        step: "results",
        selectedSlotId: response.slots[0]?.id ?? null,
      }));
    } catch (error) {
      setErrorMessage(readApiError(error));
    } finally {
      setLoading(false);
    }
  };

  const requestSelectedSlot = async () => {
    if (!flow.selectedSlotId || loading) return;
    setLoading(true);
    setErrorMessage(null);
    try {
      const idempotencyKey = `invite-request:${envelope.invite_token}:${flow.selectedSlotId}`;
      const request = await api.requestLessonSlot(envelope.invite_token, flow.selectedSlotId, idempotencyKey);
      setLessonRequest(request);
      setFlow((current) => ({ ...current, step: "request" }));
    } catch (error) {
      setErrorMessage(readApiError(error));
    } finally {
      setLoading(false);
    }
  };

  const refreshStatus = async () => {
    if (!lessonRequest) return;
    setLoading(true);
    setErrorMessage(null);
    try {
      const nextRequest = await api.getLessonRequestStatus(lessonRequest.id);
      setLessonRequest(nextRequest);
    } catch (error) {
      setErrorMessage(readApiError(error));
    } finally {
      setLoading(false);
    }
  };

  const selectAlternative = async (slotId: string) => {
    if (!lessonRequest) return;
    setLoading(true);
    setErrorMessage(null);
    try {
      const idempotencyKey = `invite-alternative:${lessonRequest.id}:${slotId}`;
      const nextRequest = await api.selectAlternativeSlot(lessonRequest.id, slotId, idempotencyKey);
      setLessonRequest(nextRequest);
    } catch (error) {
      setErrorMessage(readApiError(error));
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="client-page">
      {showMockBadge ? <div className="mock-data-badge">Mock data</div> : null}
      <BrandHeader
        eyebrow={heroContent.eyebrow}
        title={heroContent.title}
        copy={heroContent.copy}
        businessName={envelope.business_name}
        titleRef={stepTitleRef}
      />
      <FlowProgress currentStep={flow.step} />
      {errorMessage ? (
        <div className="error-strip" role="alert">
          {errorMessage}
        </div>
      ) : null}

      {flow.step === "intake" ? (
        <InviteLandingScreen
          intake={intake}
          instructorName={envelope.instructor_name}
          serviceName={envelope.service_name}
          serviceLocation={envelope.service_location}
          onChange={setIntake}
          onContinue={continueFromIntake}
          saving={loading}
        />
      ) : null}

      {flow.step === "preferences" ? (
        <AvailabilityPreferencesScreen
          durationMins={flow.duration_mins}
          allowedDurationsMins={envelope.allowed_durations_mins}
          windows={flow.windows}
          onDurationChange={(duration) => setFlow((current) => ({ ...current, duration_mins: duration }))}
          onWindowsChange={(windows) => setFlow((current) => ({ ...current, windows }))}
          onFindSlots={findSlots}
          searching={loading}
        />
      ) : null}

      {flow.step === "results" && slotResponse ? (
        <SlotResultsScreen
          response={slotResponse}
          instructorName={envelope.instructor_name}
          selectedSlotId={flow.selectedSlotId}
          onSelectSlot={(selectedSlotId) => setFlow((current) => ({ ...current, selectedSlotId }))}
          onRequestSlot={requestSelectedSlot}
          onBack={() => goToStep("preferences")}
          requesting={loading}
        />
      ) : null}

      {flow.step === "request" && lessonRequest ? (
        <LessonRequestStatusScreen
          lessonRequest={lessonRequest}
          instructorName={envelope.instructor_name}
          onRefreshStatus={refreshStatus}
          onSelectAlternative={selectAlternative}
          onCalendar={() => goToStep("calendar")}
          checkingStatus={loading}
        />
      ) : null}

      {flow.step === "calendar" && lessonRequest ? (
        <CalendarHandoffScreen
          lessonRequest={lessonRequest}
          onRefreshStatus={refreshStatus}
          checkingStatus={loading}
        />
      ) : null}
    </main>
  );
}

function BootstrapScreen({
  status,
  error,
  onRetry,
}: {
  status: BootstrapStatus;
  error: string;
  onRetry: () => void;
}) {
  if (status === "loading") {
    return (
      <main className="client-page bootstrap-page">
        <section className="bootstrap-card" role="status" aria-live="polite">
          <p className="eyebrow">Client invitation</p>
          <h1>Loading your invitation</h1>
          <p>We’re securely retrieving the appointment details from your scheduling link.</p>
          <div className="bootstrap-skeleton" aria-hidden="true"><span /><span /><span /></div>
        </section>
      </main>
    );
  }

  if (status === "missing-token") {
    return (
      <main className="client-page bootstrap-page">
        <section className="bootstrap-card" role="alert">
          <p className="eyebrow">Client invitation</p>
          <h1>Invitation link needed</h1>
          <p>Open the complete scheduling link your service provider sent you. This page cannot continue without it.</p>
        </section>
      </main>
    );
  }

  return (
    <main className="client-page bootstrap-page">
      <section className="bootstrap-card" role="alert">
        <p className="eyebrow">Client invitation</p>
        <h1>We couldn’t load this invitation</h1>
        <p>{error || "The scheduling service is unavailable right now."}</p>
        <button className="primary-button" type="button" onClick={onRetry}>Try again</button>
      </section>
    </main>
  );
}

function createFlow(envelope: ClientInviteEnvelope | null): InviteFlowState {
  return {
    step: "intake",
    selectedSlotId: null,
    duration_mins: envelope?.preferred_duration_mins ?? 0,
    windows: envelope ? cloneAvailabilityWindows() : [],
  };
}

function cloneAvailabilityWindows() {
  return initialAvailabilityWindows.map((window) => ({ ...window, days: [...window.days] }));
}

function createMockEnvelope(inviteToken: string): ClientInviteEnvelope {
  return {
    ...inviteEnvelope,
    invite_token: inviteToken,
    allowed_durations_mins: [...inviteEnvelope.allowed_durations_mins],
    intake: {
      ...inviteEnvelope.intake,
      location: { ...inviteEnvelope.intake.location },
    },
  };
}

function getHeroContent(step: InviteStep, instructorName: string, lessonRequest: LessonRequest | null) {
  if ((step === "request" || step === "calendar") && lessonRequest?.status === "confirmed") {
    return {
      eyebrow: step === "request" ? "Request status" : "Calendar handoff",
      title: "Appointment confirmed",
      copy: lessonRequest.confirmation
        ? "Your confirmed date and appointment details."
        : "This request was confirmed. There are no active calendar dates available.",
    };
  }

  if (step === "intake") {
    return {
      eyebrow: `Invite from ${instructorName}`,
      title: "Let’s find a time that works",
      copy: "Share the times that suit you, then choose from appointments that are genuinely open.",
    };
  }

  if (step === "preferences") {
    return {
      eyebrow: "Availability",
      title: "Choose the times that suit you",
      copy: "Choose any windows that work for you. We’ll show appointment times available to request.",
    };
  }

  if (step === "results") {
    return {
      eyebrow: "Available times",
      title: "Choose your preferred time",
    };
  }

  if (step === "request") {
    return {
      eyebrow: "Request status",
      title: "Your appointment request",
    };
  }

  return {
    eyebrow: "Calendar handoff",
    title: "Appointment confirmed",
    copy: "Add the confirmed appointment to the calendar you use most.",
  };
}

function readApiError(error: unknown): string {
  if (error instanceof ClientInviteApiError) {
    return `${error.message} (${error.code})`;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "Something went wrong while talking to the scheduling API.";
}
