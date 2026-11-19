# Issue 56ap: availability calendar-source service seam

## Goal

Allow the existing coach and public availability services to consume one
provider-neutral `CalendarBusySource` without changing HTTP route composition
yet. This keeps the next runtime wiring change small while making the service
boundary fail closed and independently testable.

## Contract

`search_coach_availability()` and `search_public_availability()` accept an
optional keyword-only calendar busy source and hand it to the shared
availability calculation. The source is never cached or stored globally and
is cleared from each service frame before the nested calculation begins.
Boundary-entry and cleanup interruptions propagate only after guarded cleanup,
without retaining the source in application traceback frames. Cancellation and
process-control interruptions raised while translating a provider failure are
re-emitted as fresh, cause-free values with no provider exception chain.

External calendar authorization, timeout, provider, and response-validation
failures all become the same stable API error:

- HTTP status: `503`;
- code: `CALENDAR_AVAILABILITY_UNAVAILABLE`;
- message: `Calendar availability could not be verified.`; and
- no provider reason, event data, credentials, or transport details.

This mapping applies equally to authenticated and public availability so no
unverified slots can be returned after a source failure and public callers
cannot infer private connection state.

## Non-goals

This slice does not read `app.state`, call the Google source factory, change
routes or OpenAPI, load deployment secrets, make provider requests by itself,
wire client-invite slot search, or change final booking revalidation. Those
composition points follow in separate slices.
