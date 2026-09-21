import { useEffect, useMemo, useState } from "react";
import type {
  AvailabilitySearchResponse,
  ClientIntake,
  ClientInviteApi,
  ClientInviteEnvelope,
  LessonRequest,
} from "./api/types";
import { ClientInviteApiError, createClientInviteApi } from "./api/clientInviteApi";
import { BrandHeader } from "./components/BrandHeader";
import { FlowProgress } from "./components/FlowProgress";
import { defaultWindows, inviteEnvelope } from "./data/mockInvite";
import {
  buildSearchRequest,
  selectRecommendedSlot,
  type InviteFlowState,
  type InviteStep,
} from "./features/inviteFlow/flowModel";
import { AvailabilityPreferencesScreen } from "./screens/AvailabilityPreferencesScreen";
import { CalendarHandoffScreen } from "./screens/CalendarHandoffScreen";
import { InviteLandingScreen } from "./screens/InviteLandingScreen";
import { LessonRequestStatusScreen } from "./screens/LessonRequestStatusScreen";
import { SlotResultsScreen } from "./screens/SlotResultsScreen";

const requestedDate = readConfigValue("VITE_CLIENT_INVITE_DATE", "2026-08-29");
const inviteToken = readInviteToken();

const api: ClientInviteApi = createClientInviteApi();

export default function App() {
  const [envelope, setEnvelope] = useState<ClientInviteEnvelope>(inviteEnvelope);
  const [intake, setIntake] = useState<ClientIntake>(inviteEnvelope.intake);
  const [flow, setFlow] = useState<InviteFlowState>({
    step: "intake",
    selectedSlotId: null,
    recurrence: "single",
    duration_mins: inviteEnvelope.preferred_duration_mins,
    windows: defaultWindows,
  });
  const [slotResponse, setSlotResponse] = useState<AvailabilitySearchResponse | null>(null);
  const [lessonRequest, setLessonRequest] = useState<LessonRequest | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    api
      .getInvite(inviteToken)
      .then((nextEnvelope) => {
        if (!active) return;
        setEnvelope(nextEnvelope);
        setIntake(nextEnvelope.intake);
        setFlow((current) => ({ ...current, duration_mins: nextEnvelope.preferred_duration_mins }));
      })
      .catch((error: unknown) => {
        if (!active) return;
        setErrorMessage(readApiError(error));
      });

    return () => {
      active = false;
    };
  }, []);

  const heroContent = useMemo(() => {
    if ((flow.step === "request" || flow.step === "calendar") && lessonRequest?.status === "confirmed") {
      return {
        eyebrow: flow.step === "request" ? "Request status" : "Calendar handoff",
        title: "Lesson confirmed",
        copy: lessonRequest.confirmation ? "Your active lesson dates and confirmation status." : "This request was confirmed. There are no active calendar dates available.",
      };
    }
    return getHeroContent(flow.step, envelope.instructor_name);
  }, [flow.step, envelope.instructor_name, lessonRequest]);

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
    setLoading(true);
    setErrorMessage(null);
    try {
      const response = await api.findAvailableSlots(
        envelope.invite_token,
        buildSearchRequest(
          intake,
          requestedDate,
          flow.duration_mins,
          flow.recurrence,
          flow.windows,
        ),
      );
      const recommended = selectRecommendedSlot(response.slots, flow.recurrence);
      setSlotResponse(response);
      setFlow((current) => ({
        ...current,
        step: "results",
        selectedSlotId: recommended?.id ?? null,
      }));
    } catch (error) {
      setErrorMessage(readApiError(error));
    } finally {
      setLoading(false);
    }
  };

  const requestSelectedSlot = async () => {
    if (!flow.selectedSlotId) return;
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
      <BrandHeader
        eyebrow={heroContent.eyebrow}
        title={heroContent.title}
        copy={heroContent.copy}
        businessName={envelope.business_name}
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
          onChange={setIntake}
          onContinue={continueFromIntake}
          saving={loading}
        />
      ) : null}

      {flow.step === "preferences" ? (
        <AvailabilityPreferencesScreen
          durationMins={flow.duration_mins}
          allowedDurationsMins={envelope.allowed_durations_mins}
          recurrence={flow.recurrence}
          windows={flow.windows}
          onDurationChange={(duration) => setFlow((current) => ({ ...current, duration_mins: duration }))}
          onRecurrenceChange={(recurrence) => setFlow((current) => ({ ...current, recurrence }))}
          onWindowsChange={(windows) => setFlow((current) => ({ ...current, windows }))}
          onFindSlots={findSlots}
          searching={loading}
        />
      ) : null}

      {flow.step === "results" && slotResponse ? (
        <SlotResultsScreen
          response={slotResponse}
          selectedSlotId={flow.selectedSlotId}
          recurrence={flow.recurrence}
          onSelectSlot={(selectedSlotId) => setFlow((current) => ({ ...current, selectedSlotId }))}
          onRequestSlot={requestSelectedSlot}
          onBack={() => goToStep("preferences")}
          requesting={loading}
        />
      ) : null}

      {flow.step === "request" && lessonRequest ? (
        <LessonRequestStatusScreen
          lessonRequest={lessonRequest}
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

function getHeroContent(step: InviteStep, instructorName: string) {
  if (step === "intake") {
    return {
      eyebrow: `Invite from ${instructorName} Swim`,
      title: "Tell us when you can swim",
      copy: "Share your location and availability, then choose from lesson times Riley has available.",
    };
  }

  if (step === "preferences") {
    return {
      eyebrow: "Availability",
      title: "Choose your open windows",
      copy: "Riley only sees options that fit your schedule, travel time, and lesson length.",
    };
  }

  if (step === "results") {
    return {
      eyebrow: "Backend-generated slots",
      title: "Pick a lesson time",
      copy: "These options are shaped like the Python scheduler output, including buffers and travel facts.",
    };
  }

  if (step === "request") {
    return {
      eyebrow: "Request status",
      title: "Lesson request sent",
      copy: "The time is held while Riley confirms the final appointment.",
    };
  }

  return {
    eyebrow: "Calendar handoff",
    title: "Lesson confirmed",
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

function readInviteToken(): string {
  if (typeof window === "undefined") {
    return readConfigValue("VITE_CLIENT_INVITE_TOKEN", "invite_sofia_0826");
  }

  const params = new URLSearchParams(window.location.search);
  return (
    params.get("invite_token") ??
    params.get("invite") ??
    window.localStorage.getItem("invite-token-under-test") ??
    readConfigValue("VITE_CLIENT_INVITE_TOKEN", "invite_sofia_0826")
  );
}

function readConfigValue(name: string, fallback: string): string {
  return ((import.meta.env[name] as string | undefined) ?? fallback).trim() || fallback;
}
