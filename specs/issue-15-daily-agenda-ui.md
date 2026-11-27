# Single-day Schedule UI

Schedule reads the provider-local agenda after cookie session validation and only while the Schedule tab is active. It sends the URI-encoded session instructor ID as `X-Expected-Instructor-Id` after checking it against the command-center snapshot. [Dayweave issue #15](https://github.com/clementbowe14/SwimCalendarAgentified/issues/15)

- Initial live reads omit `date` to follow provider-local today. A valid URL date takes precedence over `VITE_INSTRUCTOR_COMMAND_DATE`. Previous, Today, Next, and the seven-day selector each view one day. Today uses the server default.
- Render agenda rows in server order and format timestamps in the response timezone. Open-gap rows list the exact `start_times` as bookable starts. Repeated starts on a daylight-saving change include a zone abbreviation. Google busy rows show only a generic title and time range.
- Match `next_up.appointment_id` to its lesson. Show the returned leave-by, travel, and buffer values when available. A zero travel estimate is explicit. Past days show a recap.
- Keep blocker editing, Settings links, and the mock schedule. Use the full saved event interval when editing. Verify the editor’s provider and the timezone used for wall-clock edits. Refresh after date changes, blocker mutations, and Schedule re-entry. Ignore responses for obsolete requests, dates, or sessions.
- Keep reported appointments and blockers visible when planning is unavailable. A conflicting schedule change offers a retry. On a failed read, fall back to known appointments and blockers from a snapshot matching the selected date and the last confirmed agenda timezone. Refresh the snapshot when Schedule observes a timezone change. Clear the protected workspace and require session verification when the session instructor changes.

The intercepted HTTP Playwright suite covers agenda states and navigation. The real-backend smoke test checks that the signed-in Schedule request succeeds.
