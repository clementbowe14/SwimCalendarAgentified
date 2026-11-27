# Single-day agenda API

Serve an authenticated provider's agenda at `GET /api/coach/agenda`. An optional `date` selects a provider-local day. The default is the provider's current local date.

Return `instructor_id`, `date`, `today`, `timezone`, `status`, `items`, and nullable `next_up`. Items reuse the command-center schedule shape and add the opaque `calendar_busy` kind. `next_up` contains an `appointment_id` and nullable `leave_by`, `travel_time_mins`, and `buffer_mins`. It points to the first unfinished appointment today or first appointment on a future date.

Return booked appointments and blockers even if calendar or routing planning is unavailable. Status is `ready`, `setup_required`, `calendar_unavailable`, or `routing_unavailable`. Opaque Google periods use the provider's actual local day and never expose event metadata. Only verified planning produces travel, buffers, or bookable windows.

Use the existing availability engine and buffer policy. Open windows describe supported starts for the provider's default duration at their configured service location. They exclude elapsed time and displayed transition blocks. Derive departure from the route and buffer preceding a scheduled appointment. Preserve persisted appointments outside working hours and omit an unverified departure.

Cover populated and empty days, multiple locations, blockers, Google busy periods, routing failure, midnight and DST boundaries, and provider isolation. Preserve existing snapshot and booking contracts.
