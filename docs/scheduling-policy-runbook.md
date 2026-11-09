# Provider scheduling policy rollout

Issue #47 introduces the canonical `GET/PUT /api/coach/scheduling-policy`
resource and optimistic policy revisions. The database change is additive, but
the write cutover must be coordinated because a pre-0011 application does not
send revision preconditions.

## Preflight

Run the canonical policy validator against the target database before and after
the migration:

```bash
python3 scripts/check_scheduler_settings.py
```

A successful run prints `scheduling_policy_preflight_ok rows=<count>`. Failures
identify only the provider and one safe reason code:

- `invalid_json`: the stored tier document is not valid JSON.
- `invalid_shape`: the revision, enablement, recovery minutes, or tier document
  violates the canonical policy contract.

The command intentionally does not validate payment, legacy rounding, or
legacy shift-start fields because the canonical policy neither reads nor writes
those values.

## Deployment order

1. Apply Alembic revision `20261109_0011` while the old application is still
   serving. The new trigger increments `policy_revision` when an old writer
   changes a physical policy field without changing the revision itself.
2. Run the preflight. Then verify the trigger with a known provider row using a
   legacy-shaped update inside an explicit transaction. Compare the two
   revisions, require `revision_after == revision_before + 1`, and always roll
   the transaction back so neither the policy nor its revision is changed:

   ```sql
   BEGIN;

   SELECT policy_revision AS revision_before
   FROM scheduler_settings
   WHERE instructor_id = '<canary-instructor-id>'
   FOR UPDATE;

   UPDATE scheduler_settings
   SET buffers_enabled = NOT buffers_enabled,
       updated_at = CURRENT_TIMESTAMP
   WHERE instructor_id = '<canary-instructor-id>';

   SELECT policy_revision AS revision_after
   FROM scheduler_settings
   WHERE instructor_id = '<canary-instructor-id>';

   ROLLBACK;
   ```

   Treat a missing canary row, an update count other than one, or any revision
   result other than exactly `+1` as a failed rollout check.
3. Drain every old application process and stop legacy settings write traffic.
4. After all legacy writers are drained, rerun
   `python3 scripts/check_scheduler_settings.py` immediately before deploy. Do
   not deploy if it reports any invalid row. This final preflight closes the
   window in which an old writer could persist an invalid policy after the
   earlier check.
5. Deploy the new API and instructor application together. Do not allow old and
   new binaries to serve policy writes concurrently.
6. Verify canonical GET returns the expected policy and revision. Perform one
   canonical PUT with that `expected_revision`, verify the response increments
   it by exactly one, and verify a replay of the stale request returns
   `409 SCHEDULING_POLICY_STALE`.
7. Verify `PUT /api/coach/settings` returns
   `410 SCHEDULING_POLICY_ENDPOINT_MOVED`, then resume scheduling writes.

Canonical writes update only `buffers_enabled`, `post_break_buffer_mins`,
`distance_buffer_tiers`, `policy_revision`, and `updated_at`. Payment fields,
legacy rounding, and legacy shift-start values must remain byte-for-byte
unchanged.

## Rollback

1. Stop and drain every new application process so no canonical writer remains.
2. Downgrade Alembic from `20261109_0011` to `20261109_0010`. The downgrade
   removes only the trigger, trigger function, revision check, and revision
   column.
3. Restore the old application and resume legacy writes only after the schema
   downgrade completes.

The migration does not rename, normalize, or delete any pre-existing scheduler
or payment field, so downgrade restores the old binary's exact persisted
settings behavior.
