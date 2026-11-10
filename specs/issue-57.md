# Issue #57: fail closed without production routing

Status: approved for implementation. Baseline: `master` at `4fdece93`.
Issue: https://github.com/clementbowe14/SwimCalendarAgentified/issues/57

## Goal

Prevent staging and production from starting with deterministic Haversine travel
estimates. Deployed environments must explicitly select the supported external
routing provider and provide its credential. Local, development, and test
environments retain deterministic providers for demos and automated tests.

## Configuration contract

- `SWIM_ENV=staging|production` requires `SWIM_ROUTING_PROVIDER=mapbox`.
- `haversine`, `mock`, and `local` are synthetic providers and are rejected in
  staging and production, including when Haversine is selected implicitly by
  the current default.
- Mapbox requires a non-empty, non-whitespace
  `SWIM_MAPBOX_ACCESS_TOKEN` in every environment.
- Unsupported providers, traffic modes, timeouts, and cache values retain their
  existing `RoutingProviderNotConfigured` boundary.
- Local, development, and test continue to default to Haversine and may
  explicitly select `haversine`, `mock`, or `local`.

Configuration validation and provider construction happen while the FastAPI
application is created. A deployed process with an invalid routing contract
therefore fails startup instead of installing an unconfigured placeholder and
waiting for the first scheduling request.

## Runtime provider

The provider validated during application creation becomes the default routing
provider used by scheduling. Explicit test overrides remain supported and are
not silently replaced by later test-app construction; resetting an override
returns to the environment-derived provider.

There is no silent fallback from Mapbox failures to Haversine. Existing stable,
sanitized availability errors remain authoritative for provider timeouts,
outages, rejected credentials, geocoding failures, and unusable routes.

## Credential-safe readiness

Keep `GET /health` backward compatible and add public `GET /ready`:

```json
{
  "status": "ready",
  "routing": {
    "provider": "mapbox",
    "mode": "external",
    "configured": true
  }
}
```

Local deterministic readiness reports provider `haversine` and mode
`synthetic`. Readiness never includes tokens, token fragments, provider URLs,
raw exceptions, or environment-variable values. It describes validated local
configuration, not a live outbound Mapbox probe.

## Scope boundaries

- Do not add a second external routing provider.
- Do not call Mapbox during startup or readiness; an outbound dependency probe
  would make transient network availability prevent process startup.
- Do not add fallback travel estimates in deployed environments.
- Do not change route-estimate caching or public scheduling response shapes.

## Verification

- Configuration tests cover every synthetic provider in staging/production,
  implicit default rejection, missing/blank Mapbox credentials, and retained
  local/test behavior.
- Application-startup tests prove deployed misconfiguration fails before
  serving traffic and configured Mapbox exposes credential-safe readiness.
- API contract, health compatibility, stable routing-error, and scheduling
  tests remain green.
