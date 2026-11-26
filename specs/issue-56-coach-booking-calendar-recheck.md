# Issue 56: Recheck Google Calendar before a direct booking

Use the provider's existing calendar availability source in
`POST /api/coach/bookings`. A new booking runs the existing scheduling
checks with current external busy periods before saving the appointment.
Return a scheduling conflict for an occupied time and the existing calendar
unavailable error when the source cannot verify availability. Neither
failure creates an appointment.

Replaying a completed booking reads the original result without a Google
lookup. The booking payload and calendar handoff keep their existing shapes.
The client invite and request confirmation workflows are separate changes.
