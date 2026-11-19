# Issue 56ar: lifecycle-fenced Google availability source

## Goal

Compose the credential-free lifecycle fence with the existing per-application
Google busy-source factory behind one provider-neutral `CalendarBusySource`.
This remains route-inert so provider composition can be verified independently
from HTTP behavior.

## Contract

`GoogleCalendarAvailabilitySource` is bound to one trusted instructor and
stores only that instructor ID, the SQLAlchemy session factory, and a
non-secret callable that creates a fresh provider-neutral busy source from
that exact session factory. It is
final, immutable, redacted, and non-serializable. Cross-instructor use and an
invalid or oversized query window fail before database or factory access.

Each query:

1. validates the bound instructor and timezone-aware bounded window;
2. claims one credential-free availability lifecycle fence;
3. for `disconnected`, revalidates the same fence and returns an empty
   disconnected snapshot without calling the source factory;
4. for `authorization_required`, revalidates the same fence and raises the
   canonical authorization-required error without calling the source factory;
5. for `connected`, passes the same captured session factory to the source
   factory exactly once, queries that source exactly once through the
   provider-neutral validation boundary, requires a verified snapshot, and
   clears the concrete source; and
6. revalidates the original lifecycle fence before returning the verified
   snapshot.

No database lock is held while configuration is loaded, credentials are
decrypted or refreshed, or Google is queried. A disconnected result from a
query that began connected is rejected. The adapter never retries against a
new lifecycle generation and never caches a fence, concrete source, snapshot,
failure, configuration, or credential.

Invalid caller windows become a fresh fixed-message `ValueError`. Provider,
factory, database, stale-fence, malformed-snapshot, and ownership failures
remain provider-neutral external-calendar failures. Authorization-required,
timeout, and invalid-response categories are preserved internally for recovery
semantics. Cancellation and process controls are re-emitted fresh after source
and tenant references are cleared. No collaborator chain, tenant identifier,
credential, provider payload, or busy interval may escape through public error
messages, traceback locals, representations, serialization, or logs.

## Files and non-goals

This slice is limited to:

- `app/google_calendar_availability_source.py`;
- the session-bound `GoogleCalendarBusySourceFactory.create_for` entry point;
- focused tests; and
- this specification.

It does not change routes, application state, OpenAPI, service schemas,
booking or invite persistence, confirmation or recurrence workflows,
settings, frontends, OAuth commands, credential storage, Google event writes,
or non-Google providers. Route injection into the non-mutating coach and public
availability endpoints follows separately.
