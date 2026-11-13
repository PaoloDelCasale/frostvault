# Fair Job queue and removed-file closure

> **Amended by [ADR-0037](0037-uploads-share-one-tier-across-vaults.md) (issue #618).**
> The tiers of points 2–4 order work inside a Vault; across Vaults, manual and
> automatic uploads share one tier after Owner operations. Point 11 now covers
> every upload the Owner cancelled, manual or automatic.

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
   the exclusion list before the cut. There is **no final global LIMIT** ordered
   by base tier: that would hide eligible aged automatic turns behind enough
   Owner work. The bounded per-Vault/per-tier windows of every eligible Vault
   reach the turn selector. A flood of Owner operations in a Vault therefore
   cannot hide that Vault's own uploads from the candidate set.
2. **Scheduling tiers** (`job_scheduler.job_priority`, mirrored in SQL):
   0 Owner operations (restore, free-space, rename, storage-class, cloud purge,
   Stop Protection purges), 1 uploads the Owner asked for, 2 everything the
   system admitted on its own (automatic uploads **and automatic free-space
   cleanups**, which never outrank an Owner upload).
3. **Aging belongs to a runnable, unserved Vault turn (#552).** Job creation
   time is not starvation evidence: 26,000 old manual uploads already occupying
   workers must not all become tier 0. The durable `scheduler_vault_turns` row
   banks only observed runnable time without a live lease. An automatic Vault's
   next turn gets **one** tier-1 opportunity after 900 seconds, and **one** tier-0
   anti-starvation opportunity after 1800 seconds, as already allowed here.
   Manual turns can likewise age from tier 1 to 0; bulk manual work stays manual.
   Successful acquisition (one Job or one purge group) atomically resets credit,
   records last service and advances a generation; selecting, a failed CAS,
   deferring, or a stale concurrent nomination never consumes credit. A stale
   aged nomination must match that generation before it can claim. Remaining
   backlog starts fresh rather than inheriting the promotion. Inside a Vault,
   base tier and then oldest Job still order work: its Owner operations and
   Owner uploads never fall behind its own automatic work.
4. **Fair sharing counts running work and previous turns.** Equal effective
   tiers prefer fewer live leases plus nominations in this pass, then longer
   accounted unserved time, then least-recent service, then deterministic Job
   timestamps/IDs and Vault ID. Thus even a genuinely idle single worker, where
   every active count returns to zero between claims, serves competing turns
   rather than always selecting the oldest backlog. Owner operations retain
   precedence across Vaults **except** for the explicit aged-turn exception.
   This bounds the next **claim opportunity**, not elapsed start time: no
   preemption, hard latency guarantee, worker reservation, per-Vault quota or
   operating window is introduced.
5. **Observe one runnable head, then validate the backlog lazily.** Even when
   every worker is busy, a bounded head per unserved Vault/tier is observed so
   its clock can start before capacity returns; already leased Vaults' bulk
   windows are not loaded for this accounting-only pass. The automatic-upload local
   fence must pass before banking credit; additional backlog elements are checked
   only until free slots are filled. Live service, pause, retry/delay, local
   deferral, lifecycle/source suspension and the cloud gate earn no new credit.
   Banked credit and last service survive restart. Eligibility-changing SQL
   transitions invalidate the open observation interval even if pause/resume or
   retry backoff happens entirely between polls (or while the scheduler is down).
   This deliberately discards an unobserved partial runnable interval rather
   than accidentally credit blocked time; it does not reset banked credit.
   No trigger runs on heartbeat/progress updates or queue admission INSERTs.
   Claims lock accounting before Jobs and acquire Job locks **only with SKIP
   LOCKED**: a multi-Job status writer may hold Jobs before invalidating the turn,
   so a claimant must neither hold a Job while waiting for accounting nor wait
   for a Job while holding accounting. A purge group claims all its members or
   none (including under partial row contention). Aged nominations also skip a
   contended turn row. Ordinary base-tier claims can briefly queue on accounting
   so simultaneous different-Job claims still succeed. Claims do not upgrade the
   shared Vault fence of ADR-0031/0033.
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
    **stalled**: gauge `queue_stalled` and an open `queue_stalled` problem. Issue
    #555 removes the informational `queue` object from anonymous HTTP `/ready`;
    it stays in the internal report and never fails readiness. The problem text is
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
