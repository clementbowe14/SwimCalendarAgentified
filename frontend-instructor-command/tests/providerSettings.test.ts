import assert from "node:assert/strict";
import test from "node:test";
import { InstructorCommandApiError, MockInstructorCommandApi } from "../src/api/instructorApi.js";
import { providerConfiguration, snapshot } from "../src/api/mockData.js";
import { renderProviderSettings } from "../src/components/ProviderSettings.js";
import {
  PROVIDER_WEEKDAYS,
  firstProviderErrorField,
  normalizeProviderDraft,
  projectProviderOntoInstructor,
  providerDraftFromConfiguration,
  providerFieldErrorsFromApi,
  validateProviderDraft,
} from "../src/domain/providerSettings.js";

test("provider draft construction is canonical and normalizes optional business identity", () => {
  const draft = providerDraftFromConfiguration({
    ...providerConfiguration,
    business_name: null,
    working_hours: [...providerConfiguration.working_hours].reverse(),
  });
  const normalized = normalizeProviderDraft({
    ...draft,
    display_name: "  Riley Carter  ",
    business_name: "   ",
    base_address: "  100 Main St, Austin, TX ",
  });

  assert.deepEqual(normalized.working_hours.map(({ weekday }) => weekday), PROVIDER_WEEKDAYS);
  assert.equal(normalized.display_name, "Riley Carter");
  assert.equal(normalized.business_name, null);
  assert.equal(normalized.base_address, "100 Main St, Austin, TX");
  assert.equal(validateProviderDraft(normalized).display_name, undefined);
});

test("provider validation accepts seven disabled days and rejects bad identity, timezone, and intervals", () => {
  const allDisabled = {
    ...providerDraftFromConfiguration(providerConfiguration),
    working_hours: PROVIDER_WEEKDAYS.map((weekday) => ({
      weekday,
      enabled: false,
      start_time: null,
      end_time: null,
    })),
  };
  assert.deepEqual(validateProviderDraft(allDisabled), {});

  const invalid = {
    ...allDisabled,
    display_name: " ",
    timezone: "Not/A_Zone",
    default_duration_mins: 22,
    working_hours: allDisabled.working_hours.map((hours) => hours.weekday === "monday"
      ? { ...hours, enabled: true, start_time: "17:00", end_time: "08:00" }
      : hours),
  };
  const errors = validateProviderDraft(invalid);
  assert.match(errors.display_name, /display name/i);
  assert.match(errors.timezone, /IANA/i);
  assert.match(errors.default_duration_mins, /15-minute/i);
  assert.match(errors["working_hours.monday.end_time"], /later/i);
  assert.equal(firstProviderErrorField(errors), "display_name");
});

test("provider API errors map to the submitted weekday and address controls", () => {
  const payload = providerDraftFromConfiguration(providerConfiguration);
  const validation = new InstructorCommandApiError(
    422,
    "VALIDATION_ERROR",
    "Request validation failed.",
    {
      fields: [
        { loc: ["body", "working_hours", 5, "end_time"], msg: "Closing must be later." },
        { loc: ["body", "service_name"], msg: "Service name is required." },
      ],
    },
  );
  assert.deepEqual(providerFieldErrorsFromApi(validation, payload), {
    "working_hours.saturday.end_time": "Closing must be later.",
    service_name: "Service name is required.",
  });

  const geocoding = new InstructorCommandApiError(
    422,
    "GEOCODING_FAILED",
    "We could not locate that address.",
    { field: "base_address" },
  );
  assert.deepEqual(providerFieldErrorsFromApi(geocoding, payload), {
    base_address: "We could not locate that address.",
  });
});

test("provider renderer exposes all seven one-handed rows and inline errors without coordinates", () => {
  const draft = providerDraftFromConfiguration(providerConfiguration);
  const html = renderProviderSettings(providerConfiguration, draft, {
    pending: false,
    error: "",
    fieldErrors: { "working_hours.monday.end_time": "Closing must be later." },
    success: "",
    setupPrompt: "",
  });

  assert.equal((html.match(/role="switch"/g) ?? []).length, 7);
  assert.match(html, /data-provider-form/);
  assert.match(html, /Save provider settings/);
  assert.match(html, /aria-invalid="true"/);
  assert.match(html, /Closing must be later/);
  assert.doesNotMatch(html, /name="(?:lat|lng)"/);
});

test("mock provider adapter saves in memory and projects canonical values onto the snapshot", async () => {
  const api = new MockInstructorCommandApi();
  const draft = providerDraftFromConfiguration(await api.getProvider());
  const saved = await api.saveProvider({
    ...draft,
    display_name: "Jordan Lee",
    business_name: null,
    service_address: "500 River Rd, Austin, TX",
  });
  const reloaded = await api.getProvider();
  const projected = projectProviderOntoInstructor(snapshot.instructor, saved);

  assert.deepEqual(reloaded, saved);
  assert.equal(reloaded.display_name, "Jordan Lee");
  assert.equal(reloaded.business_name, null);
  assert.equal(projected.name, "Jordan Lee");
  assert.equal(projected.business_name, "Jordan Lee");
  assert.equal(projected.main_address, "500 River Rd, Austin, TX");
});
