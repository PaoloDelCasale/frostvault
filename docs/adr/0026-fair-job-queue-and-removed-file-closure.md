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

1. **Candidates are windowed per Vault.** `ArchiveCatalog.list_claimable_jobs`
   takes the oldest runnable Jobs of *each* Vault (a lateral window), never one
   window over all Vaults, and applies the exclusion list before the cut.
2. **Scheduling tiers.** Non-upload Jobs (restore, free-space, rename,
   storage-class, cloud purge) run before manual uploads, which run before
   automatic uploads. Inside a tier Vaults are interleaved, oldest first. An
   Owner operation therefore never waits behind a burst of automatic uploads.
3. **Capacity first, lazily.** With no free operation slot nothing is read. The
   automatic-upload stability fence is evaluated lazily, only for as many
   candidates as there are free slots.
4. **Deferral.** An automatic upload that is not runnable now (paused, unstable,
   unreadable root, changed) is excluded from the candidate query for 15
   seconds, in memory, keyed by database and Job id. A restart simply rechecks.
   Nothing durable changes, so it cannot hide work for longer than the window.

### Removed files

5. A Job whose Local Copy is **provably gone** (healthy enrolled root, ENOENT or
   ENOTDIR, no linked Archive Version, no started send) is closed `cancelled`
   with the message `job.upload_file_removed`; its remediation episode, if any,
   ends as `cancelled` with reason `file_removed`. It is not a failure: no
   `job_failed` notification, and an existing one resolves. Missing, offline or
   replaced roots are never treated as a removed file.
6. A file that reappears with different content is admitted again, because its
   content observation postdates the cancelled Job.
7. A Job that already holds a started send, a receipt or a linked version is
   never closed from database state alone; it stays with the reconcile and
   remediation machinery. Known limit: if the file disappears after
   send-started was recorded and before the first S3 request, the Job ends
   blocked until one Resume proves absence ([ADR-0025](0025-proven-absence-closes-an-ambiguous-send.md)),
   rearms, finds the file gone and closes it as removed.

### Notifications

8. More than ten upload failures of one Vault inside ten minutes fold into one
   open `job_failed_group` problem with a count; the rest keep their own rows.
   The group resolves itself once every folded failure is resolved or moot.
   Failures whose linked episode ended as `file_removed` count as resolved.

### Visibility and control

9. A queue with runnable work, nothing running and no claim for five minutes is
   **stalled**: gauge `queue_stalled`, informational `queue` object in `/ready`
   (it never fails readiness), and an open `queue_stalled` problem. The alert
   also fires when the queue is blocked by the S3 gate, which is accurate: it is
   not moving.
10. `POST /api/upload/cancel-pending` cancels every unfinished upload of a Vault
    or of one folder in a single call, through the same cancel path as a single
    Job. Cancelled automatic uploads are not re-queued until the content changes.

## Not decided here

The per-file cost of small uploads (many S3 round trips per object and the Vault
row lock taken by every status write) was not measured and is not changed. A
change would need measurements against a real bucket.
