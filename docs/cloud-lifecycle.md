# Archive lifecycle, cloud deletion and recovery

Storage plans, minimum storage durations, Retention Lock, cloud deletion, Mirror Cleanup, local space release and recovery. See [how-it-works.md](how-it-works.md) for protection states.

## Archive lifecycle and cloud deletion

- Each Vault has one **storage plan** in **Advanced**: Always accessible
  (Standard), Economical instant access (IA30 → GlacierIR90), Tiered archive
  (recommended: IA30 → GlacierIR90 → DeepArchive365), Early Deep Archive (day 0),
  Recent backups (GlacierIR from day 0, for rotating backups kept a few weeks
  and restored at once), or Custom (ordered object-age/class chain). Each explains timing and cost.
  There are no folder assignments, pins or manual per-file class changes.
- Plans emit only current-version transitions, never expiration or noncurrent
  rules. AWS requires age 30 for IA and minimum residence in the source class
  before the next step (30 days in IA, 90 in Glacier Instant/Flexible Retrieval),
  not 90/180 days from Standard before archiving. Objects below 128 KB stay put
  by default. Early replacement can incur minimum-duration charges; cold recovery
  takes hours. A changed plan does not warm existing cold objects or move them
  immediately. See [storage plans and upgrade safety](aws-s3-bucket.md#vault-storage-plans).
- **Move the entire Vault now**, in the same Advanced panel, queues verified
  Current Snapshots for a colder class on the saved plan (not Standard). Mixed
  classes are evaluated per file: equal/colder copies, unverified/missing copies,
  pending work (including delayed deletion), and cold sources without an explicit
  exact-version restore are skipped. It never restores automatically or moves
  warmer. A regional storage/source-retrieval estimate and minimum-duration warning
  must be acknowledged first; request fees and early-deletion charges are excluded.
  A copy resets the provider object-age/plan clock. Exact destination checksum
  verification precedes removal of the exact old source; interruption resumes from
  a durable ledger without discarding the only verified copy. Follow/cancel Jobs
  in Operations; cancellation retains copies and does not undo finished movement.
  Reconnect and Stop Protection require these Jobs to finish or be cancelled.
  See [copy/restart safety and limitations](adr/0021-vault-wide-colder-moves.md).
- **Minimum storage duration** (#731): STANDARD_IA (30 days), GLACIER_IR and
  GLACIER (90) and DEEP_ARCHIVE (180) bill a version for those days from its **Class Entry**,
  not from its upload, even if it is deleted earlier. Migration **0009**
  records the Class Entry (`archive_versions.storage_class_since`) from then
  on: a direct PUT or copy enters its class when written; an observed class
  change (listing, audit, restore check) moves it. Existing versions are not
  backfilled: their entry is estimated as upload + plan transition days + 1
  and marked as estimated. Objects under 128 KB stay Standard. The Vault's
  **Minimum storage duration** settings section shows, per class, the versions
  within and beyond it, the remaining minimum cost and the month-by-month
  release; «Delete from cloud», Automatic Duplicate Cleanup and Vault Deletion
  say how many versions are still within it and what deleting them still
  bills. All amounts are list-price estimates, not your AWS bill. The duplicate
  cleanup can wait for the minimum to end (off by default).
- **Retention Lock** (#736, optional): a new Vault can get S3 Object Lock in
  Governance mode with a default retention of 30, 90, 180 or 365 days, chosen
  only on the creation page and never changeable afterwards. It protects
  against stolen keys and ransomware: FrostVault never holds
  `s3:BypassGovernanceRetention`, so no one using its keys can delete a locked
  version early. Superseded versions, «Delete from cloud», Automatic Duplicate
  Cleanup and Vault Deletion wait for the RetainUntilDate («deletable from …»)
  instead of failing, and their previews say how many files stay locked and
  until when. A Delete Marker in such a bucket is a Problem. See
  [Retention Lock](aws-s3-bucket.md#retention-lock-optional-per-vault).
- Lifecycle synchronization preserves external bucket rules and the seven-day
  incomplete-multipart abort rule. Changing a plan never deletes cloud content.
- **Delete from cloud** is available only for **Cloud-only** files (or folders
  whose selected files are all Cloud-only). Type the exact Vault name and a
  reason, with recent Owner reauthentication. A fixed **seven-day cancellable
  delay** applies; there is no immediate deletion or acceleration. Every exact
  VersionId is captured once, never expanded to new content during execution.
  Local reappearance, changed cloud content or an untrusted/unavailable source
  cancels the request instead of deleting the only cloud copy by mistake.
- **Mirror Cleanup** (#771, «Elimina dal cloud i file spariti dal server», off by
  default): once a day it schedules one ordinary seven-day cancellable deletion
  of the Cloud-only files whose Local Copy was last seen at least N days ago
  (1–180, default 7), optionally only under listed folders. Unlike the duplicate
  cleanup these can be the only copy of their content, so the cloud no longer
  protects against accidental deletion: use it only for data that is already a
  backup (rotating dumps, exports), never for originals. Files never seen on the
  server are never taken. Above 5,000 files or 50 GiB per run nothing is
  scheduled and a Vault Problem opens. A file back on the server, changed or busy
  is skipped alone at execution. Enabling it, or making it delete more, needs a
  fresh sign-in; it can also wait for the Minimum Storage Duration. See
  [ADR-0052](adr/0052-mirror-cleanup.md).
- **Stop protecting this folder** always keeps local files. Choose **keep cloud**
  or the same delayed cloud deletion. A cloud-backed file present on the server
  blocks the delete choice: choose keep instead. Pending Jobs/scans must finish
  or be cancelled first. Stop+delete reserves the root until terminal; cancelling
  means keep cloud, not resume protection. Retained cloud content stays under its
  original S3 namespace and can be recovered using the Recovery Kit's manual S3
  instructions; the stopped Vault is a non-operational tombstone.
- Safe renames are recognized automatically only for unique, identical content
  observed within the scan window. The new cloud key is verified before cleaning
  up the old key (permanently under Current Snapshot). Ambiguous matches stay new
  files; no cloud content is deleted on their behalf. Path History is internal,
  not a UI action, and there is no manual rename confirmation.
- Archive statistics, file-state badges, and Job progress refresh automatically
  while background operations are active.

## Local cleanup safety

- Each Vault has one optional **Free local space after** rule, off by default:
  **3 months / 6 months / 1 year / 2 years** (fixed 90/180/365/730 days).
  Enabling preselects 6 months. It applies to every file, regardless of upload
  include/exclude filters. Put files that must stay local in a separate Vault
  with release off. Cold copies can take hours to recover and incur restore costs.
- Retention starts at the latest actual content observation, cloud content
  verification, local modification, or explicit recovery time. Old photo imports
  and recovered files get a full new period without changing their mtime.
- **Free local space now** remains an explicit, confirmed manual action even
  with the automatic rule off. Only Protected files are eligible; an absent or
  untrusted Source Volume, unconfirmed protection, mismatch, changed content or
  unavailable cloud blocks release. GLACIER and DEEP_ARCHIVE are allowed.
- Policy updates preserve omitted fields, so saving upload rules cannot silently
  replay an old automatic-release choice from another settings panel.
- Release outcomes and blockages are audited and included in the weekly summary,
  never sent as per-file notifications.
- Before deleting the server copy, the application checks the exact S3
  `VersionId`, atomically claims the local file, and rehashes it; object
  existence at the same key or a stale local fingerprint is not sufficient.
- Cleanup deletes local copies only. It never deletes the S3 object.
- Recovery downloads the exact archived S3 `VersionId`, verifies the plaintext
  SHA-256 digest, then atomically replaces the local file. Glacier/Deep Archive
  versions request a restore first (Bulk by default); high-impact restores are
  held for one hour awaiting the Owner's approval before any `RestoreObject`
  call. An accepted S3 restore request cannot be cancelled. Large recoveries
  use parallel S3 range downloads (`S3_DOWNLOAD_*`).
- Sign in with the Owner's configured OIDC provider or, from loopback/allowed
  LAN, the Emergency Password.
