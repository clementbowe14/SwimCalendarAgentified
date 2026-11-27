# One-time availability across client windows

[Issue #18](https://github.com/clementbowe14/SwimCalendarAgentified/issues/18)

## Shared search and provider API

- `POST /api/coach/availability/search` accepts inclusive provider-local start and end dates, optional weekday windows, a session duration, the location mode, and a limit of 1 to 48. Today is day one of the 14-day pilot horizon.
- The scheduler uses the provider’s saved working hours and buffer policy, appointments and blockers, and the existing inbound and outbound routing checks. Client windows constrain the full session. Touching windows on the same weekday form a continuous window. Slot ordering uses UTC instants, including during a repeated local hour.
- Resolve the location once. For connected providers, verify Google busy periods for every evaluated day and its routing anchors. Query at most 15 days at a time and discard results if any read fails or the calendar connection changes during planning.
- Apply client windows and the result limit before extra outbound route checks. Stop starting work after 15 seconds or 96 routing calls, and cap the schedule rows and busy periods processed per search. A search that reaches a work limit returns a retryable error.
- The provider receives travel, buffer and distance evidence plus a reason when no options fit. The existing one-day API still supports its callers.

## Provider Finder

- Let the provider set a date range and use the selected client’s saved weekday windows, duration and location. Show the searched dates even when no starts fit. Use server slot instants for bookings and keep the chosen search context on retry.

## Client invite

- Search the same one-time range from the canonical invite workflow. Expose time, duration, location and selection only. Commit offers under the invite and calendar protections and save each offer’s provider-local date so a later-day selection revalidates on the correct day.
- Keep the legacy single-day API and existing offer redemption compatible. Issue #58 owns the wider pilot recurrence cleanup.
