# A key the catalog never wrote needs no listing before its PUT

Status: accepted by the Owner on 2026-11-13 (issue #622). Amends
[ADR-0034](0034-bounded-upload-execution.md) only as stated here; every rule of
ADR-0024, ADR-0025 and ADR-0036 stands.

In v0.6.1 a new small file cost six `ListObjectVersions` requests: two before the
PUT (the pinned inventory and its stability check) and four in the cleanup after
publication (the first listing and the final one, each taken twice), although
nothing was ever found for a key nobody had written. Parsing those responses in
boto3 took 54 % of the process's GIL time and capped bulk uploads at about seven
files per second.

## Decision

1. **Presumed-empty inventory for a never-written key.** When the catalog has
   never sent anything to the key of the file's path, the attempt pins an empty
   inventory without listing. "Never sent" means: no Archive Version at the
   key, no upload attempt of the path by the file or by any earlier holder of
   the path in its Path History, and no copy attempt to the key by them. It is
   read in the Job's first claim-fenced unit. Only the file's own active
   operation can write its key, and the target pin is checked again before
   send-started. ADR-0034's "fresh stable namespace observation" still applies
   to every other key: a key the catalog wrote gets two equal listings before
   the PUT, as before.
2. **What the presumed pin may decide.** It is a pin like any other for
   reconciliation. A version that is not the attempt's own token, or a marker,
   is a delta, so the proof of absence (ADR-0025) fails and the send stays
   blocked. It is never replacement authority. Cleanup deletes only catalogued
   candidates, after its own fresh listings (ADR-0024).
3. **Postcondition with nothing to delete: one listing.** When the publication
   unit finds no other version of the file and no open cleanup batch, the Job
   takes no cleanup unit. Before completing, one complete listing of the
   keeper's key must show the keeper alone, with no Delete Marker. Otherwise
   the Job blocks as before (`inventory_changed`, `markers_present`,
   `keeper_missing`). An object that was already at a new key, written outside
   FrostVault, still blocks the first completion. The same single listing
   replaces the first and final listings of a cleanup that has nothing to
   delete. A cleanup with candidates keeps every listing: before freezing,
   before each DELETE and after the last one.
4. **No provider request inside a catalog permit.** Cleanup, inventory and
   verification requests run between DB units, never inside one, so S3 latency
   never occupies a Vault's catalog slot. A test enforces this. The Job still
   completes only after cleanup.
5. **Permit waits do not poll.** A queued stage permit is woken by the release
   that makes room for it. Without a wake-up it runs its checkpoint once a
   second, the cadence at which the claim checkpoint already reads its lease.
   Local cancellation, shutdown and claim loss are seen within that second
   while queued. Effects stay fenced durably as before.

## Related cost changes (#622)

- Pending timers: `record_pending_files` reads the Vault's timers and writes
  only those that start or end. An unchanged pending file is never rewritten.
- Directory aggregates: the background loop folds dirty directories once every
  `DIRECTORY_AGGREGATE_BATCH_SECONDS` (2 s), not on every 100 ms cycle of a
  deep queue. **Freshness rule:** dirty marks stay durable in the uploading
  transaction, as before. A listing that finds dirty directories reports
  `stale` with the previous projection and schedules its own refresh
  immediately. Without a reader, the projection converges within one batch
  interval plus the rebuild time, and the rebuild publishes a catalog revision
  so that open browsers refresh.

## Consequences

- A new small file costs one `ListObjectVersions` instead of six. A replacement
  costs the same as before: two listings before the PUT, then the cleanup's own
  listings.
- An unknown object at a new key is found after the PUT, as in v0.6.1. If the
  send is interrupted, it keeps that send in `ambiguous_send` rather than
  letting a proof of absence succeed. The file stays blocked either way, and
  nothing is deleted.
