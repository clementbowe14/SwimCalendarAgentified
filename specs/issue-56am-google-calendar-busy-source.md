# Issue 56am: synchronous Google Calendar busy source

## Goal

Adapt the verified async Google busy-snapshot service to the existing synchronous,
provider-neutral `CalendarBusySource` contract consumed by scheduling code.

This is a route-inert bridge only. It does not yet change availability results.

## Contract

`GoogleCalendarBusySource` receives a service-owned SQLAlchemy session factory, a
validated Google token-exchange configuration, and the calendar credential cipher.
Its synchronous `query_busy_periods(...)` method forwards one instructor and bounded
UTC-capable query window to `query_google_calendar_busy_snapshot(...)` and returns the
provider-neutral snapshot as a freshly validated, canonical copy.

The adapter:

- satisfies `CalendarBusySource` without exposing Google claims, credentials, tokens,
  payloads, or event details;
- creates and closes one `asyncio.Runner` per synchronous call, and creates the
  provider coroutine only inside that initialized runner;
- validates snapshot state, tuple shape, interval types, ordering, bounds, and query
  window before returning a canonical copy;
- fails closed without allocating a coroutine if invoked from a running event loop;
- closes an unconsumed runner driver without allocating the provider coroutine, and
  applies attempt- and time-bounded isolated cleanup before rejecting a coroutine
  returned as malformed data, reserving process-wide bounded cleanup ownership before
  invoking the provider so saturation cannot create an unowned result; cleanup
  ownership is not recycled until both its caller and worker relinquish it, and a
  result that cannot be closed remains in the bounded quarantine instead of falling
  back to synchronous caller-thread finalization;
- canonicalizes all ordinary failures and cancellations to provider-neutral calendar
  errors with no collaborator exception chain;
- performs bounded traversal of exception groups and re-emits sanitized process
  controls; and
- is immutable, redacted, non-subclassable, and non-serializable, with guarded public
  boundaries that clear configuration, cipher, tenant, and malformed-result references
  before exposing a rejection traceback.

The injected configuration and cipher are immutable and safe to share across
concurrent scheduler threads. Each call creates its own event loop and every database
phase continues to use the service-owned session factory.

## Files and non-goals

This PR is limited to:

- `app/google_calendar_busy_source.py`;
- `tests/test_google_calendar_busy_source.py`; and
- this specification.

It does not add environment loading, application startup wiring, routes, OpenAPI,
schema, migrations, caching, retries, telemetry, availability or booking integration,
agenda changes, frontend behavior, calendar writes, or additional providers.
