# Issue 56: Use Google busy periods in provider availability search

`POST /api/coach/availability` uses the existing calendar source for the
authenticated instructor. The route constructs a fresh source using the
application's factory and the active database binding.

- A disconnected calendar keeps the current internal-only results and does not
  load Google configuration.
- A connected calendar removes options that overlap verified busy periods.
  An external block keeps any unelapsed post-break recovery in force.
- Connection lookup uses the exact stored instructor key. Active credential
  handoffs support printable UTF-8 keys within their existing byte limit.
  Google payloads and credential formats stay unchanged.
- If the calendar cannot be verified, return the existing
  `CALENDAR_AVAILABILITY_UNAVAILABLE` response with no available slots or
  provider diagnostics.

Tests cover authentication, disconnected and connected calendars, and failed
queries. The request and response shapes are unchanged. OAuth, settings, and
calendar checks on booking writes and client flows are separate changes.
