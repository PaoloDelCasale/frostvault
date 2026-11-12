# A stale Upload Snapshot closes its unsent intent

> **Amended in part by [ADR-0036](0036-one-upload-snapshot-many-send-attempts.md)**
> (#588): the intent is now an Upload Attempt of a snapshot stored once; the stale
> snapshot is closed, its unsent attempt settled `unsent`, and the Job forgets it.
> Migration names below are historical (ADR-0035).

Issue #538. A file changed while it was being sent. The upload stopped correctly
and left nothing on S3, but every later attempt compared the now stable file with
the Upload Snapshot taken before the change, failed with "Local file changed since
fingerprinting" and was retried as a temporary source change. After
`UPLOAD_RETRY_MAX_ATTEMPTS` the Job failed with `job.source_changed_stalled`
although the file had stopped changing long before, and each attempt reread the
whole file for nothing.

## Decision

The Upload Snapshot stays immutable for an admitted intent
(`catalog.set_upload_snapshot` is unchanged). It is never re-pinned in place.
Instead, when an upload fails with a source change and is retried, the Job's
intent is **closed** if its send never started:

- no linked Archive Version, no receipt, `send_started = false` (never set, or
  cleared by a proven absence, ADR-0025), and the Job is not reconcile-only;
- the intent is settled with its snapshot and token intact, its open multipart
  rows are closed (it owes the provider nothing), and it is unbound from the Job;
- the Job forgets snapshot A (`upload_snapshot_json` and its digest are cleared).

The next attempt fingerprints the current Local Copy and admits a new intent, with
a new snapshot and a new token, for the same Job. Migration
`0069_upload_intent_per_attempt` therefore replaces the unique constraint on
`upload_intents.original_job_id` with a unique index on the **open** intents: a
Job has at most one unsettled intent, and its closed ones stay as history.

The attempt counter is not reset: a file that keeps changing still ends in
`job.source_changed_stalled` after `UPLOAD_RETRY_MAX_ATTEMPTS`. A file that is
stable after the change is uploaded at the next attempt.

### A started send keeps reconciliation

If the send had started, nothing is closed when the attempt fails: something may
have reached S3, so the next attempt reconciles the intent first (ADR-0024,
ADR-0025, ADR-0029) and never hashes or sends before it is decided. That attempt
leaves the send-started state for good, so the stale comparison cannot repeat:

- **proven absence**: the intent becomes unsent; the attempt then finds the Local
  Copy different from A and fails as a source change, which closes the intent as
  above. The upload of the new content follows at the next attempt;
- **version found by token**: it is published and verified against snapshot A, the
  content that was actually sent; the later Local Copy is a separate upload;
- **anything else** (an untraceable multipart upload, contradictory receipts): a
  durable reconciliation block, never a source-change retry. Provider faults during
  the reconciliation are the ordinary bounded transient retries (ADR-0025).

So a change during a transfer costs at most two stability windows (reconcile, then
send), instead of the whole retry budget.

## Alternatives considered

- *Re-pin the existing intent in place with the new snapshot.* `prepare` re-pins
  an unsent intent when its pinned inventory or target moved on; doing the same for
  the content would rewrite the snapshot of an admitted intent and require making
  `set_upload_snapshot` mutable. Rejected: closing keeps every snapshot that was
  ever admitted exactly as it was.
- *Fail the Job and let a new upload Job take over.* This is what happened after
  the budget ran out, with a false failure and its notification. Rejected.

## Consequences

- Downgrading below `0069` is refused once a Job has admitted more than one intent;
  closed intents carry receipts and remediation links and are never deleted to make
  the old constraint fit.
- A closed intent no longer binds its Job, so a retrying Job whose intent was closed
  counts as not started (for example, it can be cancelled with the queued uploads).
- Residual risk from ADR-0025 is unchanged: a late request from a dead sender that
  lands after a proven absence carries the old token, and the new intent's token
  differs, so cleanup reports it as an unknown version instead of a duplicate.
