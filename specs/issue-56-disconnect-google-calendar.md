# Issue 56: Disconnect Google Calendar

Authenticated providers can use `DELETE /api/coach/calendar-connections/google`
with the existing CSRF header. The operation removes the current provider's
stored connection and cancels pending Google authorization state in one
transaction. It returns the same credential-free status shape as the read
endpoint, including when the connection was already disconnected.
