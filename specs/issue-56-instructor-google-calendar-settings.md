# Issue #56 Google Calendar in instructor Settings

The Google Calendar card in authenticated Settings shows the saved connection state. It reads
`GET /api/coach/calendar-connections/google` when Settings opens and offers a status retry if
the read fails.

- Disconnected instructors can connect. An authorization-required connection offers reconnect
  and disconnect. A connected instructor can disconnect.
- Connect and reconnect use the bodyless
  `POST /api/coach/calendar-connections/google/authorize`. The app accepts the returned
  `authorization_url` only at `https://accounts.google.com/o/oauth2/v2/auth` with a query
  string and opens it in the current tab. Returning with browser Back revalidates the session
  and reopens Settings with a fresh status, including when the browser restores the page.
- Disconnect uses the bodyless `DELETE /api/coach/calendar-connections/google` and displays
  the status it returns. The adapter sends the existing session credentials and CSRF header
  for both mutations. A successful disconnect clears cached Find results so the next search
  uses the updated scheduling state.
- A return to `/?calendar=connected` or `/?calendar=failed` opens Settings after authentication.
  The app removes that marker with history replacement, preserving other query parameters and
  the fragment, and loads the saved status. A failed or unconfirmed return offers a way to
  try again based on that status.
- Calendar actions are pending one at a time. Responses from an earlier auth session cannot
  update Settings or open Google. Errors give the instructor a retry without displaying API
  details or the authorization URL.
- Status, authorize, and disconnect send `X-Expected-Instructor-Id` with the URI-encoded
  instructor ID from the page's signed-in session snapshot. The app needs that snapshot to
  send a Calendar request. A `409 CALENDAR_CONNECTION_OWNER_CHANGED` clears the card's
  saved status and offers a reload so the page can validate the current session. Cross-origin
  API deployments need to allow the header in CORS.

Browser tests intercept both the coach API and Google navigation and cover the return flow,
browser Back, retry, disconnect with cleared Find results, and auth changes during requests.
