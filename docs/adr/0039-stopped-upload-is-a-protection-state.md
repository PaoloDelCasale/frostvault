# A Stopped Upload is a protection state, not pending work

Status: accepted by the Owner on 2026-11-13 (issue #630). Extends
[ADR-0028](0028-excluded-protection-state.md) and point 4 of
[ADR-0037](0037-uploads-share-one-tier-across-vaults.md).

Since #618 an upload the Owner cancelled keeps its File out of automatic
admission until the content is observed again. The protection CASE did not know
it: such a File stayed `pending` in Explore, Details and `GET /api/stats`
forever, so the Vault never became `protected`, while upload activity correctly
reported nothing queued. An Owner who did not remember the cancellation saw a
Vault stuck for no reason.

## Decision

- The shared vocabulary gains **Stopped Upload** (`stopped`, «Fermato da te»): a
  present Local Copy that automatic admission holds because of an upload Job
  (`stopped_upload_holds_automatic_admission_sql`, the predicate admission and
  upload activity already use) and that has no Job running or waiting. "Will not
  start by itself" and "shown as stopped" therefore cannot disagree.
- It is assigned only where the File would otherwise be `pending`. A failed
  upload stays `problem`, which keeps precedence, and an excluded path stays
  `excluded`. Content observed after the stop clears the hold, so the File is
  `pending` again; a manual upload the Owner asks for is an active Job, so the
  File is `pending` while it runs.
- Aggregate order: problem > unconfirmed > pending > stopped > protected >
  cloud_only > unsupported > excluded. Statistics count `stopped` apart; it is
  never pending or queued, and a Vault holding one is not `protected`. Delay
  monitoring does not count it as overdue work.
- The UI shows it as its own label and filter. Details says «Upload fermato da
  te: non parte in automatico finché il file non cambia.» with **Carica ora**
  (the existing manual upload); the folder listing says the same for the level
  and uploads its stopped files and folders; the Overview counts them apart from
  pending.
- No schema change, so v0.6.2 databases upgrade in place. The state depends on
  Jobs, which do not invalidate the directory projection: the projection keeps
  counting a Stopped Upload as `pending`, and a directory listing moves the live
  Stopped Uploads of its child directories out of `pending` with one query that
  starts from the Vault's failed and cancelled upload Jobs. Its cost follows
  those Jobs (bounded by the 30-day Job retention), not the Vault's size.

## Considered options

- A flag next to `pending` (`upload_hold: "stopped_by_owner"`): every consumer
  of the state would have to combine two fields, and the Vault aggregate would
  still read `pending`.
- A `state_stopped` column in the directory projection: needs a schema change
  and invalidation on every cancel, admission and Job retention; the read-time
  overlay cannot go stale.
