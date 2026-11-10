# Issue #74: preserve the client home-pool draft while comparing a facility

Status: approved for implementation. Baseline: `master` at `4fdece93`.
Issue: https://github.com/clementbowe14/SwimCalendarAgentified/issues/74

## Goal

Keep the client intake form reversible. A client who enters a home-pool address,
briefly selects the instructor's facility, and then returns to **Home pool** must
see the same home address and coordinates they had before the comparison.

## Existing defect and root cause

`InviteLandingScreen.updateLocationType` uses the facility location when
`Facility` is selected, then assigns an empty address and null coordinates when
the client changes back to `home`. The single `intake.location` field therefore
cannot retain the hidden home draft while the read-only facility is displayed.

## Behavior

- Retain an in-memory copy of the most recent home location for the lifetime of
  the intake screen.
- Update that retained copy whenever the client edits the home-pool address.
- Selecting `Facility` continues to display the instructor facility as a
  read-only field with its configured coordinates and
  `is_instructor_facility=true`.
- Selecting `Home pool` restores the retained address and coordinates with
  `is_instructor_facility=false`.
- Re-selecting the already active location type is a no-op.
- If no home address was supplied, returning to Home pool preserves the current
  empty/prompt state rather than fabricating a value.

## Scope boundaries

This change is local client-side draft state. It does not add a second location
to the public API, persist both choices on the server, geocode free-form text,
or change facility configuration.

## Verification

- Mock browser coverage on mobile and desktop for Home pool → Facility → Home
  pool after editing the address.
- HTTP-bootstrap browser coverage proving a server-supplied home location is
  restored after the same toggle.
- Existing client unit, mock, intercepted, typecheck, and production-build
  checks remain green.
