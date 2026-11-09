# Provider settings rollout and rollback

Migration `20261109_0010` adds the authoritative provider identity, private base location,
client-facing service location, default service duration, and seven-row weekly-hours table.
It backfills existing instructors without calling an external geocoder: both new locations come
from the legacy main location, the newest invite supplies business name and a valid preferred
duration, and valid legacy hours are copied to every weekday. Invalid legacy hours deliberately
leave setup incomplete.

## Upgrade preflight

Revision `0010` runs a read-only legacy-length preflight before adding any column, constraint, or
table. It refuses the upgrade instead of truncating identity or location data when either value the
backfill would copy cannot fit the new schema:

- `LEGACY_MAIN_ADDRESS_TOO_LONG`: `instructors.main_address` exceeds 300 characters; or
- `LEGACY_BUSINESS_NAME_TOO_LONG`: the newest nonblank invite business name for a provider, after
  trimming surrounding whitespace, exceeds 160 characters.

The exception lists affected provider IDs and reason codes in deterministic provider-ID order. It
does not include the address, business name, invite token, or other raw legacy values. Use the IDs
to inspect the records through approved operational access. Correct an oversized business name to
the intended public identity. For an oversized main address, preserve the address/coordinate
association: validate the corrected address and update its coordinates together rather than
blindly truncating the text. Retry the migration after every reported conflict is remediated.

A preflight refusal leaves the Alembic revision, legacy rows, and schema unchanged. The error uses
entries in this form:

```text
provider_id=<id>,reason_code=LEGACY_MAIN_ADDRESS_TOO_LONG
```

## Rollout

1. Take and verify a PostgreSQL backup.
2. Drain old writers and run `alembic upgrade head`.
3. Deploy the API and instructor app together.
4. Authenticate as each pilot provider and verify `GET /api/coach/provider` reports the expected
   locations, service, timezone, duration, seven weekdays, and `setup_complete` state.
5. Correct incomplete rows through `PUT /api/coach/provider`. Coordinates are server-owned and both
   changed addresses must geocode before the transaction commits.
6. Exercise a home-location and facility-location availability search. The former must route from
   the private base at shift start; the latter must use the public service location.

Scheduling mutations and availability calculations return `PROVIDER_SETUP_REQUIRED` until setup is
complete. Existing schedule, account, client, and settings reads remain available. Public responses
use a generic error and never disclose the private base or missing-field list.

## Downgrade preflight

Downgrade is intentionally limited because the legacy schema cannot represent separate locations or
weekday-specific hours. Revision `0010` performs every preflight before its first mutation and aborts
the transaction with the offending instructor ID unless all of these are true:

- base and service locations are complete and identical;
- all seven weekdays are enabled with one identical interval; and
- business name/default duration can be copied to at least one existing invite snapshot, or both are
  absent for a provider without invites.

All-disabled weeks, disabled weekdays, differing intervals, and distinct base/service locations must
be normalized before retrying a downgrade. After taking another verified backup, test the downgrade
against a restored copy first:

```bash
./venv/bin/alembic downgrade 20261109_0009
```

A refused downgrade leaves both schema and data unchanged. A successful downgrade copies the service
location and Monday interval into legacy instructor columns, copies provider presentation values into
invite snapshots, then removes the explicit provider columns and weekly-hours table. Re-upgrade and
restore the saved provider configuration before resuming scheduling if rollback is abandoned.
