# How FrostVault works

This page explains the model behind FrostVault: the single Owner, Vaults, protection states, automatic upload and how every upload is verified. For deployment see [deployment.md](deployment.md); for archive lifecycle, cloud deletion, local cleanup and recovery see [cloud-lifecycle.md](cloud-lifecycle.md).

## Owner and Vaults

- FrostVault has exactly one **Owner**, who owns every vault. There are no other
  users, roles, members, invites, or shared vaults.
- The Owner signs in with OIDC (for example Pocket ID), configured only through
  the `OIDC_*` environment variables. The first successful OIDC login binds the
  provider subject to the Owner; any other subject is refused.
- The **Emergency Password** from `BOOTSTRAP_ADMIN_*` is accepted only from
  loopback and the LAN networks listed in `BREAK_GLASS_ALLOWED_CIDRS`; it is
  refused from anywhere else. An empty value means loopback-only (`127.0.0.0/8`
  and `::1`); there is no allow-all value.
- Reauthentication is required only before downloading the Recovery Kit and
  before deleting from the cloud.
- Each Vault has its own local folder and its own S3 bucket (a **Vault Bucket**,
  created by FrostVault); Vaults from older installations keep a per-Vault prefix
  in a shared bucket.
- Every query and operation includes the vault identifier.
- Queued or active uploads, recoveries, and local cleanup operations can be
  stopped with the **Stop** button shown next to their progress.
- Operations run in parallel. `OPERATION_CONCURRENCY` controls the number of
  simultaneous processes (default: `4`, maximum: `32`). The previous
  `UPLOAD_CONCURRENCY` variable remains supported as a fallback.
- Local cleanup runs in the background and reports progress for each file or
  folder.
- Vaults are created from the UI with one cloud behavior: **Current Snapshot**
  keeps the last verified content of each file. A successful upload or safe
  rename verifies the new copy before destroying previous cloud bytes; a failed
  upload/verification preserves the previous copy. Recovery has no version picker.
  The S3 bucket stays versioned as an internal safety mechanism, not a
  recoverable history. Mistaken overwrites or ransomware can therefore replace
  the only good cloud copy after verification.
- A file whose Local Copy is gone (freed or deleted via SMB) keeps its verified
  cloud copy as the only copy. If **different** content later appears at the
  same path, FrostVault does not upload it: the file shows a conflict with a
  file kept only in the cloud, and an immediate notification (in-app, plus
  email when SMTP is configured) explains it. Rename the new file to protect
  it at its new path, or confirm the replacement with
  `POST /api/vaults/{vault_id}/files/cloud-only-conflict/replace`: the new
  content is uploaded and verified before the previous cloud copy is deleted.
  Identical content is never a conflict; it becomes the Local Copy again
  without an upload.
- New vaults can use an empty managed root or adopt any existing directory
  under a Source Volume that does not overlap another vault root.
- The interface has four places. **Overview** shows the protection status,
  sizes and estimated monthly cost of every Vault. Each Vault has **Files** (a
  file browser with breadcrumbs, protection counts per folder, All / Cloud only /
  Pending / Stopped by you / Excluded / Unsupported / Problems filters and a search
  across this Vault or all Vaults) and **Settings**. **Problems** lists open
  problems, and **System** holds the installation Recovery Kit, metadata backups,
  Source Volumes and the billing export.
- Selection offers **Recover** and **Free space now**; clicking a file opens its
  **Details**. Cloud internals and deliberate seven-day cloud deletion live only in
  Details. Cold recovery offers Normal (Standard) / Economical (Bulk, default), an
  estimate, return path and local-retention explanation. There are no previews.
- Vault Settings contain automatic protection, exclusions, upload verification,
  local space release, Mirror Cleanup, the storage plan / Move now, cloud storage
  and cost, and Stop Protection. There are no administration or access pages:
  installation settings remain deployment-owned. API clients should pin
  `vault_id` on every scoped request; an invalid explicit identity fails closed
  rather than using Session selection.

## Automatic protection

Browsing and statistics use these **protection states**, not copy-presence labels:

- **Pending**: new or changed local content not yet verified. A retained verified
  previous snapshot is indicated separately; it does not protect the new bytes.
- **Protected**: the regular Local Copy's fingerprint and matched Archive Version
  agree, with `integrity=verified` and `availability=available`.
- **Problem**: terminal upload failure, mismatched content verification, missing
  Archive Version, unsupported local entry, or a held Cloud-only Conflict.
- **Unconfirmed**: the Source Volume/reconnect gate prevents observation or cloud
  requests cannot confirm availability. Last catalog evidence and its observation
  date remain visible; absence of a volume never turns files into Cloud only.
- **Cloud only**: no Local Copy is present and the retained cloud snapshot is
  verified and available. Recovery progress is independent of protection state.
- **Excluded**: a present Local Copy that automatic upload deliberately skips:
  the fixed noise exclusions (`._*`, `.DS_Store`, `Thumbs.db`, `*.lock`, `*-wal`,
  `*-journal`) or the Vault's own include/exclude globs. It is not pending work
  and does not count against the Vault's protection, but its current content is
  not expected to be in the cloud (an older verified copy is still indicated).
  A Problem, an unconfirmed observation or a verified cloud copy of the same
  bytes (Protected) take precedence. Manual upload requests stay explicit.
  An exclude glob that covers a whole folder (`dir/**`, `**/dir/**`) makes it an
  **Excluded Folder**: scans and the watcher no longer look inside it, files
  already known there keep their last known state and are marked "not
  observed", and removing the rule starts a full scan (ADR-0044).
- **Stopped by you** (`stopped`): a present Local Copy whose upload the Owner
  stopped (single or bulk cancel, manual or automatic upload). Automatic upload
  leaves it alone until its content is observed again; it is not pending work,
  not queued and not protected. Details and the folder listing say so and offer
  **Upload now**. A failed upload stays a Problem. See
  [ADR-0039](adr/0039-stopped-upload-is-a-protection-state.md).

Folders show counts for every state; Vault statistics use the same derivation.
The aggregate headline prioritizes Problem, Unconfirmed, Pending, Stopped by you,
Protected, then Cloud only; Excluded appears as the headline only when nothing else is present
(empty Vaults have no headline state). **Protection suspended** is a
separate per-Vault flag: pausing uploads does not invalidate already verified
content. None of these display states authorizes deleting a Local Copy or cloud
content; operation-specific safety checks remain mandatory.

New Vaults enable automatic upload by default. The per-Vault **Enable automatic
upload** switch (or `PUT /api/vaults/{vault_id}/operation-policy`, `auto_upload`) enables or
disables that policy only. Disabling holds queued automatic uploads; an already
uploaded Archive Version still finishes verification. Explicit manual uploads
remain available.

**Pause cloud work** in Overview or Vault settings is a separate manual hold,
persisted across restart until **Resume cloud work**. Authenticated, CSRF-protected
`POST /api/vaults/{id}/pause` and `/resume` leave queued Jobs unchanged, including
Owner uploads, and let already claimed Jobs finish. Recoveries at every stage and
storage-class Jobs already restoring remain runnable so temporary cloud copies
are downloaded before expiry; pending Restore Approvals still expire. Local scans,
watchers and automatic admission continue, but no new held cloud Job starts. Cloud
inventory scans, shared Storage Plan synchronization and provider lifecycle
transitions continue: this is not a zero-S3-call maintenance switch. No data is
deleted, no protection is stopped, and resume does not create duplicate Jobs.
See [ADR-0033](adr/0033-manual-vault-pause-at-the-claim-boundary.md) for the exact scope.
Bulk cancellation remains the existing `POST /api/vaults/{vault_id}/upload/cancel-pending` (empty
`path` for the entire Vault), not pause/resume.

**Upload whole Vault** and manual directory uploads return HTTP 202 after
persisting an admission request, not after creating every transfer Job. A dedicated
DB-only producer creates at most 100 candidates per transaction, releasing the
Vault lock between batches. Upload activity shows paths considered and Jobs
admitted separately from transfer progress. The accepted response includes
`admission` and a stable `group_id`; read durable progress with
`GET /api/vaults/{vault_id}/upload/admissions/{group_id}`. Optional UUID `request_id` on
`POST /api/vaults/{vault_id}/upload` makes retries return the same request, including terminal
requests; reuse for a different scope is rejected. Acceptance is catalog-only, not
a confirmation of mount availability: transfer workers retain Source Volume checks
and wait safely when unavailable. Single-file behavior is unchanged.

Each request freezes manual origin, verification mode, Vault identity and a finite
catalog path watermark. New or renamed paths are not added; unavailable, protected
or busy files can be skipped, so selected paths need not equal admitted Jobs.
Restart resumes the committed cursor without reproducing completed batches.
Remediation still uses its original frozen proof and reconcile-only restrictions.
Group cancellation also stops unproduced work; cancel-pending records folder
exclusions for later batches (or stops all admissions for an empty path), without
cancelling ambiguous sends. Stop Protection and Reconnect require pending admission
to finish or be cancelled first. Paused Vaults can prepare Jobs but cannot claim
them. See [the admission decision](bulk-upload-admission.md).

Filesystem watcher observations are the upload input. After five minutes without
changes, an independent scheduler admits them to the durable Job queue (checked
every ten seconds), without waiting for the default six-hour full scan. Both
mtime and the last observed content change must be old enough, so copying a file
with a preserved old timestamp does not bypass stability. Edits restart the
window; unchanged reconciliation scans and restarts do not. Full scans remain
the reconciliation net for events missed while stopped or with the watcher off.
With adequate bandwidth and a healthy provider, the target is verified protection
within about one hour of finishing the copy, not a transfer-time guarantee.

Automatic uploads always exclude `.DS_Store`, `Thumbs.db`, `._*`, `*.lock`,
`*-wal`, and `*-journal` at any path depth. These fixed exclusions cannot be
removed or overridden by include patterns. Existing additional include/exclude
rules still narrow eligibility; explicit manual operations are unaffected.

Enqueueing performs no cloud writes and does not depend on cloud validation or
storage-plan/lifecycle synchronization succeeding. Those failures remain visible
in scan health/errors. The upload worker still requires successful bucket
preflight before writing, honors retries and leases, and verifies the exact S3
VersionId with a full SHA-256 readback. Terminal automatic failures/cancellations
are not silently requeued for the same observation; retry manually or make a new
content change. Missing/replaced Source Volumes or Vault roots suspend local work
without clearing the catalog. SMB deletion never propagates to cloud deletion
unless the Owner turns on Mirror Cleanup for that Vault (below), and Cloud-only
Conflicts remain held back.

## Upload verification

All Vaults store readable object keys `vaults/<uuid>/<relative-path>` and original
file bytes, without client-side encryption or extension transformation. Managed
SDK uploads use multipart transfers, a shared optional upload bandwidth cap and
throttled progress. New uploads default to **S3 SHA-256 ingestion proof**
(`UPLOAD_VERIFICATION=s3_checksum`); `read_back` requests an independent complete
GET/SHA-256. Vault settings offer inherit / S3 SHA-256 / full read-back and show
the deployment default and effective mode. The mode is frozen at Job admission;
later setting changes affect only new uploads, not retries or existing evidence.

Both plaintext SHA-256 and independently expected provider proof come from the
same stable Upload Snapshot. Single PUT uses SHA256 `FULL_OBJECT`; multipart uses
SHA256 `COMPOSITE` with independently hashed parts and recorded boundaries, **not**
the plaintext digest. Bounded validated part buffers prevent a changed Local Copy
from being sent under an earlier digest; no verification artifacts are written
inside the Vault or as an unbounded whole-file temporary copy. Normal buffers are
8 MiB, adjusted up to 1 GiB for large objects (5 TiB maximum), at most two per Job;
hashing reads 1 MiB chunks. Below 8 MiB the hashing pass is the only read of the
file: the single PUT sends exactly the bytes it hashed, from a send buffer
reserved before the read. Completed snapshot A never protects later Local Copy B.

The upload response supplies the exact VersionId and is registered before later
HEAD/proof checks. Valid S3 cryptographic proof grants Integrity verified and the
usual guarded Current Snapshot cleanup/local release. It proves **provider
ingestion**, not independently reread storage. Missing proof or an unsupported
checksum HEAD API triggers a visible full exact-VersionId GET/SHA-256 fallback;
wrong checksum/type/size/identity blocks, never silently downgrades. Read-back and
fallback download approximately the entire uploaded size (e.g. 9 GB upload +
9 GB download), potentially incurring request, retrieval and **S3 egress costs**.
File Details show the recorded method, fallback reason and completed read-back
byte count. Legacy versions without independent S3 expected evidence require
read-back against their durable plaintext digest, never remote checksum inference
or rehashing a changed Local Copy. Retries of linked versions do not issue another
PUT. Providers refusing checksum-bearing writes fail safely rather than silently
repeating an ambiguous upload without checksum.

Rename, Move now, Recovery and Reconnect retain their existing full/independent
verification behavior and exact VersionId pins; this is only a new-upload policy.
See [ADR-0023](adr/0023-new-upload-ingestion-proof.md) for the verification
contract and [ADR-0024](adr/0024-upload-facts-and-proven-duplicate-cleanup.md)
for durable upload facts and replacement cleanup.
Incomplete multipart uploads are aborted on failure/cancellation, with a seven-day
lifecycle safety net for process interruption. No external transfer binary or
remote configuration is required.

### Interrupted upload and retained cloud extras

FrostVault commits a target-specific upload intent,
independent Upload Snapshot and separate opaque metadata token before any cloud
mutation. Raw VersionId receipts append before proof/HEAD/progress flush or
cancellation, independently of a worker lease; a receipt is a fact, not permission
to publish or delete. PutObject/CreateMultipartUpload/CompleteMultipartUpload use
`total_max_attempts=1`; reads and same-MPU parts retain ordinary SDK retries.

Restart/retry reconciles the exact receipt or an exact unique token match against
that frozen expected proof before any new PUT. Zero matches after send-started,
multiple/fake tokens, incomplete/unstable pagination and observable external
writers remain **reconcile-only**, not evidence that another upload is safe.
Source A's receipt never verifies a later Local Copy B; an unavailable/replaced
Source Volume or changed Vault/path still suspends authority, even when individual
local-file absence would not prevent finishing an already-written target.

Upload and rename replacement cleanup share durable per-VersionId authority.
Scans discover content but do not directly delete it or grant trust by cataloging
it. Ordinary app-owned superseded versions require independent lineage. Historical
unknowns are adopted/cleaned only after a full exact-VersionId GET, checked length
and SHA256 prove a duplicate of a verified, available, current keeper. The proof
catalog does not promote an older duplicate to Current Snapshot or alter local
matches/verification clocks. A committed exact proof avoids repeated GET on retry.
Different, ambiguous, cold/unreadable versions stay in cloud with a deduplicated
Problem and a safe remedy. Historical reads may incur retrieval/egress/request
charges; explicitly restore an exact cold version externally if needed. No
arbitrary deletion or automatic RestoreObject action is offered.

File Details exposes **Retry exact reconciliation** independently of the normal
upload action (including already protected files for which upload returns 409).
`POST /api/vaults/{vault_id}/upload/reconcile` admits one stable Vault File into a claim-fenced,
reconcile-only Job incapable of PUT; `/api/vaults/{vault_id}/files/versions` exposes pending,
can_resume, bounded reason and remedy. Cancelled attempts retain facts and cannot
be automatically resurrected. New writers, path/owner/source changes, missing
keepers and Delete Markers block cleanup; only exact authorized IDs may be deleted,
never the keeper. Keep external S3/SMB writers quiescent: DB locks are not an atomic
provider snapshot, and in-flight deletes cannot be undone. Delayed deletion
consent, metadata-backup retention and generic prefix utilities are unchanged.

No automatic sweep or original-failure history resolution is added here; #459
owns that separate operational cadence. Verified is still not completed until
cleanup satisfies its postcondition. Downgrade refuses retained ambiguous sends
or pending cleanup authority before dropping metadata, without contacting S3.
No real AWS or object larger than 5 GiB acceptance is claimed by the compatible
CI emulator/SDK tests.
