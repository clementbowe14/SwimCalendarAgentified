# Client Invite Frontend

Token-scoped client booking prototype for the public invite flow.

## Local Data Modes

Data mode is fail-closed. Mock data is available only when explicitly selected:

```bash
VITE_USE_MOCK_API=true npm run dev
```

HTTP is the default mode and requires the FastAPI base URL:

```bash
VITE_USE_MOCK_API=false VITE_CLIENT_INVITE_API_URL=http://127.0.0.1:8000/api/public npm run dev
```

The `VITE_USE_MOCK_API=false` value may be omitted because HTTP is the default. Starting or building
without an API URL fails instead of falling back to fixtures. Mock mode and an API URL cannot be set
together, and production builds reject mock mode. See `.env.example` for the local HTTP configuration.

To use a default invite token:

```text
VITE_CLIENT_INVITE_TOKEN=invite_sofia_0826
```

The invite token can also be supplied in the browser:

```text
/?invite_token=invite_sofia_0826
```

Public requests should not include raw `instructor_id`, `client_id`, or appointment IDs. The invite
token scopes the backend relationship. The HTTP adapter explicitly uses `credentials: "omit"` for every
public API call and calendar download, so a same-site instructor `swim_session` cookie is never attached.
One-time availability searches use the server's next 14 provider-local days.

## Tests

```bash
npm run test:unit
npm run test:e2e
```

`npm run test:e2e` explicitly runs the mock UI suite followed by the intercepted-HTTP suite. The live
command requires an API URL and fails instead of passing with skipped tests when it is absent. The live
suite uses no public API fixtures; it creates requests, reads the persisted request back, confirms them
through the canonical coach `/confirm` endpoint with an isolated cookie-preserving request context,
matches the
client-selected occurrence to refreshed confirmation calendar events, refreshes notifications, follows
Google occurrence links, and parses actual Apple/ICS downloads using `ical.js`. The legacy coach
`/approve` route is a deprecated compatibility alias and is not used by this product-flow suite.

```bash
VITE_CLIENT_INVITE_API_URL=http://127.0.0.1:8016/api/public npm run test:e2e:live
```

Use an isolated backend started with `SWIM_ENV=test`, `SWIM_ENABLE_DEMO_DATA=true`, and
`SWIM_ENABLE_DEMO_AUTH=true`. Mobile and desktop Vite servers use ports 4173 and 5173.
Mobile searches Saturdays and desktop searches weekdays to prevent concurrent booking conflicts.
The backend must allow both frontend origins (included in its test defaults). Google
navigation is intercepted only at Google's external host; public API responses and downloads remain
live. Actual Google/Apple import and provider delivery need separate manual verification.

Calendar URLs are snapshots. Google links must use HTTPS on `calendar.google.com`; downloads must remain on the configured backend origin. Download errors preserve the screen without claiming an import. Notification history comes from the API, with accepted explicitly distinguished from delivered; the demo has no delivery history.
