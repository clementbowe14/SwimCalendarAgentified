# Client Invite Frontend

Token-scoped client booking prototype for the public invite flow.

## Local Data Modes

By default the app uses `MockClientInviteApi`.

Set `VITE_CLIENT_INVITE_API_URL` to use the FastAPI backend:

```bash
VITE_CLIENT_INVITE_API_URL=http://127.0.0.1:8000/api/public npm run dev
```

Optional values:

```text
VITE_CLIENT_INVITE_TOKEN=invite_sofia_0826
VITE_CLIENT_INVITE_DATE=2026-08-29
```

The invite token can also be supplied in the browser:

```text
/?invite_token=invite_sofia_0826
```

Public requests should not include raw `instructor_id`, `client_id`, or appointment IDs. The invite token scopes the backend relationship.

## Tests

```bash
npm run test:unit
npm run test:e2e
```

The live suite is skipped unless `VITE_CLIENT_INVITE_API_URL` is set. When set, only live tests run, with no public API fixtures. The suite creates weekly requests, approves them with the seeded development coach account, refreshes notifications, follows Google occurrence links, and parses actual Apple/ICS downloads using `ical.js`.

```bash
VITE_CLIENT_INVITE_API_URL=http://127.0.0.1:8016/api/public npm run test:e2e:live
```

Use an isolated, seeded backend database. Mobile and desktop Vite servers use ports 4173 and 5173 and booking windows 35 days apart to prevent concurrent booking conflicts. The first window defaults to a Saturday at least one week from today; `VITE_CLIENT_INVITE_DATE` overrides it and should match the selected availability days. The backend must allow both frontend origins (included in its development defaults). Google navigation is intercepted only at Google's external host; public API responses and downloads remain live. Actual Google/Apple import and provider delivery need separate manual verification.

Calendar URLs are snapshots. Google links must use HTTPS on `calendar.google.com`; downloads must remain on the configured backend origin. Download errors preserve the screen without claiming an import. Notification history comes from the API, with accepted explicitly distinguished from delivered; the demo has no delivery history.
