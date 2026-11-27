# Single-day agenda API

Serve an authenticated provider's agenda at `GET /api/coach/agenda`. Require the signed-in instructor ID in `X-Expected-Instructor-Id`. An optional `date` selects a provider-local day. The default is the provider's current local date.

Return `instructor_id`, `date`, `today`, `timezone`, `status`, `items`, and nullable `next_up`. Items reuse the command-center schedule shape and add the opaque `calendar_busy` kind. `next_up` contains an `appointment_id` and nullable `leave_by`, `travel_time_mins`, and `buffer_mins`. It points to the first unfinished appointment today or first appointment on a future date.

Return booked appointments and blockers even if calendar or routing planning is unavailable. Status is `ready`, `setup_required`, `calendar_unavailable`, `routing_unavailable`, or `schedule_changed`. Opaque Google periods use the provider's actual local day and never expose event metadata. Only verified planning produces travel, buffers, or bookable windows.

Blocker reads and writes verify `X-Expected-Instructor-Id` when supplied by an agenda editor. Creates and edits can include `expected_timezone` so a saved time is interpreted in the timezone the editor displayed. Existing request fields remain supported.

Use the availability and booking engine with the same schedule anchors and buffer policy. Open windows list exact verified `start_times` for the provider's default duration at their configured service location. Keep adjacent appointments outside working hours in the routing context, exclude elapsed starts at publication, and avoid overlap with displayed transitions. Verify a traffic-aware route at the exact published departure. Read the schedule, configuration, policy, and calendar connection together after routing. Return the known schedule with a retry status when those planning inputs change.

Cover populated and empty days, multiple locations, blockers, Google busy periods, routing failure, midnight and DST boundaries, and provider isolation. Preserve existing snapshot and booking contracts.
