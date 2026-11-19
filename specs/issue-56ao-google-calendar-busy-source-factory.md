# Issue 56ao: per-app Google Calendar busy-source factory

## Goal

Install one non-secret factory on each FastAPI application so later route wiring
can create a fresh `GoogleCalendarBusySource` for each scheduling operation.
This is a route-inert composition slice: no existing endpoint consumes the
factory yet.

## Contract

`GoogleCalendarBusySourceFactory` has no instance state. Its `create()`
operation:

1. resolves the current application SQLAlchemy session factory;
2. loads the current Google token-exchange configuration from the environment;
3. loads the current calendar credential cipher from the environment;
4. requires the exact validated configuration and cipher types; and
5. returns a new, uncached `GoogleCalendarBusySource`.

Secrets are loaded only when `create()` is called. Importing the module,
calling `create_app()`, entering application lifespan, generating OpenAPI, and
reading health state do not load calendar secrets or construct a provider
source. Every `create_app()` call installs a distinct factory at
`app.state.calendar_busy_source_factory`; neither settings nor readiness output
contains it.

The factory is immutable, final, redacted, and non-serializable. It stores no
configuration, cipher, created source, failure, or loader closure. Successful
and failed calls are never cached, so later calls observe secret rotation and
can recover after a corrected deployment configuration.

Ordinary loader, validation, and constructor failures become a fresh,
cause-free `ExternalCalendarUnavailable("provider_unavailable")`. Task
cancellation and process-control exceptions, including controls nested in a
bounded exception group, are re-emitted as fresh values. Configuration,
cipher, partial source, collaborator exception, and secret values are cleared
before any failure leaves the factory boundary, including when construction or
cleanup is interrupted. Factory mutation, subclassing, and serialization
rejections are also cause- and context-free so they cannot inherit an ambient
secret-bearing exception.

## Files and non-goals

This slice is limited to:

- `app/google_calendar_busy_source_factory.py`;
- installing the factory in `app/api.py`;
- focused tests; and
- this specification.

It does not call the factory from a route or service, change availability or
booking responses, map errors to HTTP, add OAuth or disconnect endpoints,
change OpenAPI, query Google, open a database session, cache sources, expose
readiness, change the frontend, or write calendar events.
