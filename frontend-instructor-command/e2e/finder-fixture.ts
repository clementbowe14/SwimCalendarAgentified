import type { Request } from "@playwright/test";
import type { AvailabilityRangeRequest } from "../src/api/types";

const clientLocation = { address: "820 Lakeview Dr, Austin, TX", lat: 30.289, lng: -97.739 };

export const finderClientList = {
  clients: [{
    id: "client_sofia",
    name: "Sofia Martinez",
    phone: null,
    email: null,
    notes: "",
    revision: 1,
    location_mode: "client_location",
    client_location: clientLocation,
    effective_location: clientLocation,
    default_duration_mins: 45,
    availability: [],
  }],
};

export function finderSearchResult(request: Request) {
  const { start_date, end_date, duration_mins } = request.postDataJSON() as AvailabilityRangeRequest;
  return {
    start_date, end_date, duration_mins,
    timezone: "America/Chicago",
    location: { ...clientLocation, is_instructor_facility: false },
    empty_reason: null,
  };
}
