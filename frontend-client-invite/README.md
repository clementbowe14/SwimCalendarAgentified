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

The real-backend Playwright skeleton is skipped unless `VITE_CLIENT_INVITE_API_URL` is set before running `npm run test:e2e`.
