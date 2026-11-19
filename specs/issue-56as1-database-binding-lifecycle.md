# Issue 56as1: freeze database binding while an app serves

## Goal

Prevent one request from spanning multiple process-wide SQLAlchemy session
factories if test or administrative code attempts to reconfigure the database
while an application is serving.

## Contract

Each active FastAPI lifespan owns one opaque database-serving lease. Database
reconfiguration is permitted only when no leases are active. Overlapping app
lifespans retain independent leases, and releasing one lease never releases a
peer.

Lease registration begins before database initialization. Startup failure,
normal shutdown, and process-control interruption all release the exact lease.
Cleanup is idempotent and retryable so an interruption at lock acquisition or
set removal cannot permanently disable later test reconfiguration. Re-emitted
process controls are fresh and carry no cleanup traceback context.

## Scope

This prerequisite changes only database reconfiguration lifecycle safety,
FastAPI lifespan registration, focused tests, and this specification. It does
not change schemas, migrations, request transactions, route payloads, provider
configuration, or production database selection.
