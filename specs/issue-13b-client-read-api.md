# Issue 13B: Provider-scoped client read API

## Objective

Expose the canonical client profiles introduced by issue 13A through a dedicated authenticated
read API. This is the second small, independently deployable slice of issue #13.

This slice adds provider-scoped list and detail reads only. It does not add create, update, or
deactivate mutations; change the existing command-center snapshot; or alter the instructor UI.
Those remain separate follow-up PRs so read-contract and write-transaction regressions stay
isolated.

## HTTP contract

Add two authenticated routes under the existing coach router:

- `GET /api/coach/clients`
- `GET /api/coach/clients/{client_id}`

Provider identity comes only from the authenticated session. Neither route accepts an instructor
ID in the path or query. Both routes return `Cache-Control: private, no-store` and
`Referrer-Policy: no-referrer`.

The list response is an envelope so pagination or filtering metadata can be added later without
changing the top-level shape:

```json
{
  "clients": [
    {
      "id": "client_123",
      "name": "Maya Chen",
      "phone": "+15125550100",
      "email": null,
      "notes": "Gate code 2468",
      "location_mode": "client_location",
      "client_location": {
        "address": "901 Home Rd, Austin, TX",
        "lat": 30.3,
        "lng": -97.3
      },
      "effective_location": {
        "address": "901 Home Rd, Austin, TX",
        "lat": 30.3,
        "lng": -97.3
      },
      "default_duration_mins": 45,
      "availability": [
        {
          "id": "window_123",
          "days": ["monday", "wednesday", "friday"],
          "start_time": "08:00",
          "end_time": "10:00"
        }
      ],
      "revision": 1
    }
  ]
}
```

The detail route returns the same client object without the envelope. Phone and email are nullable
because legacy contactless rows remain readable until edited. Notes are the persisted essential
notes only. The contract does not contain guardian, lesson-history, progress, appointment, invite,
request, or payment fields.

## Read semantics

- Return active clients only.
- Sort the list case-insensitively by name, then by ID for deterministic ties.
- Sort availability windows by persisted position, then by ID.
- Decode `day_mask` from bit 0 Monday through bit 6 Sunday and emit days in Monday-to-Sunday order.
- Format availability times as provider-local wall-clock `HH:MM` strings. Overnight windows remain
  impossible at the database boundary.
- Preserve the persisted client-specific duration and positive revision.
- Normalize blank phone/email values to `null`; do not fabricate contact data.

`client_location` is the saved client-owned location tuple and may be null. `effective_location` is
the location scheduling should use now:

- for `client_location`, it is the saved client location;
- for `service_location`, it is the provider's current configured service location, not a copied
  legacy client value; and
- when provider setup is incomplete and no service location exists, it is null while the profile
  remains readable.

The service-location projection must therefore load authoritative provider configuration but must
not require setup completion.

## Tenancy and privacy

Every client query filters by both authenticated instructor ID and active status. Unknown,
deactivated, and cross-provider client IDs return the same `404 RESOURCE_NOT_FOUND` response with
the generic message `The client could not be found.` No response exposes instructor IDs, the
provider's private base location, or other clients' data.

Load availability with the client query rather than issuing one query per profile. Database rows
are converted to detached dictionaries before the session closes.

## Compatibility boundary

- Keep `CoachClientResponse` and the command-center snapshot unchanged in this PR.
- Do not remove fabricated legacy fields from the snapshot until its frontend consumers migrate to
  the dedicated client API.
- Do not change mock frontend data, navigation, or browser workflows.
- Do not add client mutations, idempotency behavior, geocoding, optimistic writes, or soft removal.
- This PR is part of issue #13 and must not close it.

## Contract artifacts

Add generated private coach fixtures for one client profile and the list envelope. Extend OpenAPI
tests to assert both paths, response schemas, cookie authentication, absence of CSRF requirements
for these GETs, the stable `404` error envelope, nullable locations/contacts, and required revision
and location-mode fields.

## Test contract

- active list and detail round-trip persisted notes, generic location mode, duration, revision, and
  grouped availability;
- deterministic client/window/day ordering and exact `HH:MM` formatting;
- service mode follows a changed provider service location while preserving the saved client tuple;
- incomplete provider setup returns a readable profile with null effective service location;
- empty list returns `{"clients": []}`;
- deactivated, unknown, and cross-provider IDs share one non-disclosing `404`;
- list and detail require authentication and ignore spoofed instructor query parameters;
- success and error responses use private no-store/no-referrer headers;
- database access is eager-loaded without an availability N+1 query;
- generated fixtures validate against response models and committed contracts have no drift; and
- the complete existing backend suite remains green.
