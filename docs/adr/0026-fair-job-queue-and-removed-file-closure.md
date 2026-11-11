# Fair Job queue and removed-file closure

Issues #493, #499 and #500 show one cause seen three ways: the worker read the
oldest 160 claimable Jobs of the whole installation, rechecked every automatic
upload on every tick, and a file deleted before or during its upload ended as a
failure. A burst of thousands of small automatic uploads (or a moved folder)
then starved other Vaults, delayed restores and free-space runs, left Jobs
`queued` forever and produced one failure notification per file.

This ADR changes scheduling and closing behaviour only. It does not change what
may be written to S3: [ADR-0023](0023-new-upload-ingestion-proof.md),
[ADR-0024](0024-upload-facts-and-proven-duplicate-cleanup.md) and
[ADR-0025](0025-proven-absence-closes-an-ambiguous-send.md) are unchanged.

## Decision

### Scheduling

1. **Candidates are windowed per Vault and per tier.** `ArchiveCatalog.list_claimable_jobs`
   takes the oldest runnable Jobs of *each Vault and scheduling tier* (a lateral
   window with `row_number()`), never one window over all Vaults, and applies
   the exclusion list before the cut. A flood of Owner operations in a Vault
   therefore cannot hide that Vault's own uploads from the candidate set.
2. **Scheduling tiers** (`job_scheduler.job_priority`, mirrored in SQL):
   0 Owner operations (restore, free-space, rename, storage-class, cloud purge,
   Stop Protection purges), 1 uploads the Owner asked for, 2 everything the
   system admitted on its own (automatic uploads **and automatic free-space
   cleanups**, which never outrank an Owner upload).
3. **Aging.** Work that has waited 15 minutes moves up one tier (and one more per
   further 15 minutes), so a huge Owner operation ("Move the whole Vault") cannot
   stop every upload of every other Vault. Aging promotes a *Vault's turn*, not
   single Jobs: a Vault competes with the best effective tier among its waiting
   Jobs, and inside a Vault the original tier still orders Jobs, so neither an
   Owner operation nor an Owner upload is ever queued behind aged automatic
   work of its own Vault.
4. **Fair sharing counts running work.** Inside a tier the next slot goes to the
   Vault with the fewest running Jobs (live leases) plus Jobs already picked in
   this pass, ties broken by the oldest waiting Job. With concurrency 1 and slots
   freed one at a time, a Vault that already runs a Job yields to idle Vaults.
5. **Capacity first, lazily.** With no free operation slot nothing is read. The
   automatic-upload stability fence is evaluated lazily, only until the free slots
   are filled.
6. **Deferral with backoff and deeper scans.** An automatic upload that is not
   runnable now (paused, excluded path, unstable, not a regular file, unreadable,
   changed) is excluded from the candidate query, in memory, keyed by database
   and Job id, for 15 s, doubling per recheck up to 2 minutes. When a window
   yields no runnable Job the same tick reads the next window (at most 20
   rounds), so any number of unrunnable Jobs ahead of a ready one no longer hides
   it. Deferral is not durable: a restart simply rechecks.

### Removed files

7. A Job whose Local Copy is **provably gone** (healthy enrolled root checked
   before *and after* the `ENOENT`, no linked Archive Version, no started send)
   is closed `cancelled` with the message `job.upload_file_removed`; its
   remediation episode, if any, ends as `cancelled` with reason `file_removed`.
   The closure marks the Local Copy `missing` in the same transaction, so the
   same bytes coming back (same size and mtime) open a new stability window and
   are admitted again. It is not a failure: no `job_failed` notification, and an
   existing one resolves. Missing, offline or replaced roots are never treated
   as a removed file.
8. A Job that already holds a started send, a receipt or a linked version is
   never closed from database state alone; it stays with the reconcile and
   remediation machinery. Known limit: if the file disappears after
   send-started was recorded and before the first S3 request, the Job ends
   blocked until one Resume proves absence ([ADR-0025](0025-proven-absence-closes-an-ambiguous-send.md)),
   rearms, finds the file gone and closes it as removed.

### Notifications

9. More than ten upload failures of one Vault inside ten minutes fold into one
   `job_failed_group` problem with a count. The group absorbs a failure only
   while the Vault is still in a burst (more than ten failures in the last ten
   minutes) and it folded a failure within the last ten minutes. Otherwise it
   is retired (it stays open until its own failures are resolved or moot,
   judged on the failures between its `since` and `last`) and the failure gets
   a notification of its own, so a slow trickle of real failures after a burst
   is never hidden; a new burst starts a new group that does not re-count the
   retired one. Failures whose linked episode ended as `file_removed` count as
   resolved.

### Visibility and control

10. A queue with **runnable** work (Jobs the worker has set aside as not
    runnable are not counted), nothing running and no claim for five minutes is
    **stalled**: gauge `queue_stalled`, informational `queue` object in `/ready`
    (it never fails readiness), and an open `queue_stalled` problem whose text is
    refreshed every minute. A claim does not resolve the problem: it resolves
    after ten minutes without a stall, so a queue that alternates between
    claiming and idling raises one problem, not one unread alert per cycle. The
    alert also fires when the queue is blocked by the S3 gate, which is accurate:
    it is not moving.
11. `POST /api/upload/cancel-pending` cancels the uploads of a Vault or of one
    folder (`foo` matches `foo/...`, never `foobar`) that **have not begun**:
    queued or retrying, not reconcile-only, no bound upload intent, no started
    send, no linked version, no remediation episode. Anything else keeps its own
    cancel and Resume, so a bulk action can never close an ambiguous send for
    good. Cancelled automatic uploads are not re-queued until the content changes.

## Not decided here

The per-file cost of small uploads (many S3 round trips per object and the Vault
row lock taken by every status write) was not measured and is not changed. A
change would need measurements against a real bucket. [ADR-0031](0031-pooled-connections-and-shared-vault-lock-for-held-job-steps.md)
later pooled the database connections and shared the Vault lock for held Job
steps (#535); the S3 round trips are still unchanged.
