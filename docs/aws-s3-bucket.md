# AWS S3 bucket provisioning and validation

This guide covers the [buckets FrostVault creates itself](#buckets-frostvault-creates)
(a **Vault Bucket** for every new Vault and the System Bucket), the shared
archive bucket of installations from before Vault Buckets, and the application
preflight checks that validate them before upload or local cleanup.

## No bucket to provision

Since #735 ([ADR-0049](adr/0049-one-bucket-per-vault.md)) FrostVault creates a
dedicated Vault Bucket for every new Vault, in `AWS_DEFAULT_REGION`. Give it
AWS credentials, `AWS_DEFAULT_REGION` and the IAM policy
[below](#iam-for-frostvault-created-buckets); there is no bucket setting.
`VAULT_S3_BUCKET` (and its alias `S3_BUCKET`) was removed and is ignored.

### Upgrading an installation with a shared bucket

An installation from before #735 keeps its shared bucket for the Vaults it
already has: migration `0010_vault_buckets` records, on their Vault Targets,
that their objects live under `vaults/<uuid>/` in that bucket, and FrostVault
reads the bucket from the Vault Target, never from configuration. Nothing in S3
is moved, copied or renamed, and an existing bucket is never registered as a
Vault Bucket. Vaults created after the upgrade get their own bucket. When you
upgrade:

1. Attach `iam/frostvault-buckets-policy.json` (it now also covers the object
   operations of Vault Buckets). Keep `iam/archive-operator-policy.json` on the
   shared bucket while older Vaults use it.
2. Remove `VAULT_S3_BUCKET` and `S3_BUCKET` from the environment; keep
   `AWS_DEFAULT_REGION`.

The Terraform module at `infra/terraform/archive-bucket/` creates that shared
bucket. It enables versioning, blocks public access, enforces bucket-owner
object ownership, applies SSE-S3 (`AES256`) by default, and aborts incomplete
multipart uploads after seven days. Object Lock is intentionally **not**
enabled. A new installation does not need it.

## IAM

FrostVault's identity needs `iam/frostvault-buckets-policy.json`
([IAM for FrostVault-created buckets](#iam-for-frostvault-created-buckets)).
For a shared bucket from before #735, attach
`infra/terraform/archive-bucket/iam/archive-operator-policy.json` as well,
replacing `${archive_bucket_name}` with its name. Both grant the same object
operations, on their own buckets:

- bucket list, location, versioning, Block Public Access read, lifecycle, and tagging
  (`s3:GetBucketPublicAccessBlock` is required at bucket scope)
- `s3:ListBucketVersions` and `s3:ListBucketMultipartUploads`: exact upload
  reconciliation lists a key's versions and its incomplete multipart uploads to
  prove what an interrupted send did. Without them the affected file stays
  blocked as `list_permission_denied` (a per-file block; uploads of other files
  are not affected) until the policy is updated and **Retry failed uploads** is used
- object upload, version read, restore, tagging and `s3:AbortMultipartUpload`
  (required for interrupted/cancelled SDK multipart transfers, and to abort exactly the
  incomplete multipart upload an interrupted upload journaled before its proof of
  absence, ADR-0029; an upload that was not journaled is never aborted). A refused
  `s3:GetObject`/`s3:GetObjectVersion` or `s3:PutObject` blocks only the affected
  file as `read_permission_denied` / `write_permission_denied` while the bucket
  itself stays ready; it never suspends the whole installation
- delete object/version for the accepted single-identity deletion model


### Accepted single-identity deletion risk

The initial product accepts one IAM identity for ordinary archive operations and
for `DeleteObject` / `DeleteObjectVersion` calls used by verified automatic
rename cleanup, Current Snapshot replacement and permanent purge. There is no
manual reversible cloud-hiding action. That identity can therefore both write
Archive Versions and destroy them.

Mitigations built into the application:

- only Cloud-only files can be explicitly deleted; files present on the server,
  unobservable sources and pending file work block the entire selection
- the Owner types the exact Vault name and a reason, with recent reauthentication
- every request has a fixed **seven-day cancellable delay**, never accelerated
- exact VersionIds are captured once; reappearance or changed cloud content
  revokes consent instead of broadening the targets
- every request, cancellation, partial failure and completion is audited
- Stop Protection retains every Local Copy and uses exactly the same cloud gates;
  the cloud-keep choice performs no provider deletion calls

Prefer a dedicated deletion role and stricter IAM boundaries when your threat
model requires separating ordinary archive work from permanent purge.

## Buckets FrostVault creates

FrostVault creates and verifies its own buckets
([ADR-0049](adr/0049-one-bucket-per-vault.md)):

- a **Vault Bucket** for every new Vault, `frostvault-<slug>-<8 random [a-z0-9]>`
  in `AWS_DEFAULT_REGION`. The slug is the Vault name in lowercase ASCII
  (accents dropped, other characters become `-`), at most 40 characters; a
  name with no letter or digit becomes `vault`, and a slug that is `system` or
  starts with `system-` gets the prefix `vault-` (`vault-system`,
  `vault-system-photos`, still at most 40 characters): no Vault Bucket name
  starts with `frostvault-system-`, the System Bucket's shape. The Vault's files are at the bucket **root**: the key of a
  file is its relative path;
- the **System Bucket** for metadata backups, `frostvault-system-<8 random [a-z0-9]>`
  in `AWS_DEFAULT_REGION`, unless `METADATA_BACKUP_BUCKET` names an
  operator-supplied one (see [metadata-backups.md](metadata-backups.md#system-bucket)).

Every such bucket gets, and is then verified to have:

- all four Block Public Access settings;
- versioning Enabled;
- default encryption SSE-S3 (`AES256`);
- Object Ownership `BucketOwnerEnforced` (ACLs off);
- FrostVault's lifecycle rules. A Vault Bucket's lifecycle is exactly its
  Vault's Storage Plan (`frostvault-plan-<uuid>`, no filter, only when the plan
  has transitions) plus the seven-day abort of incomplete multipart uploads,
  with `TransitionDefaultMinimumObjectSize` `all_storage_classes_128K`: objects
  smaller than 128 KiB never transition and stay in Standard. Any other rule
  fails the verification. On the System Bucket the abort rule is required and
  extra rules the Owner adds (for example a noncurrent-version expiry) are allowed;
- its `frostvault:*` tags (`frostvault:vault=<uuid>` and `frostvault:name=<Vault name>`
  for a Vault Bucket, `frostvault:role=system` for the System Bucket); other
  tags are kept. The System Bucket's tags are applied at creation and not
  required afterwards;
- no Object Lock, unless a Governance-mode default retention was chosen for it.
  The System Bucket never has one.

A new Vault is `provisioning` until its bucket is created, configured and
verified, and then for 15 more minutes: AWS recommends waiting 15 minutes after
enabling versioning on a new bucket before writing objects to it
([S3 versioning](https://docs.aws.amazon.com/AmazonS3/latest/userguide/manage-versioning-examples.html)),
so FrostVault records when the verification passed and the background loop
activates the Vault once that time has elapsed (checked every minute, after a
restart too). Only then is it `active` and selectable. The Vault creation page
and the Overview show the progress and, after the verification, "active in about
N minutes"; when a step fails the Vault stays saved and provisioning, the page
and the Overview show the failing step and the S3 error code, a Problem opens
(the bucket may already exist in the account without being in use), and
FrostVault retries every 15 minutes, after a restart too. **Retry now** retries
at once, for example after an IAM fix. A failure FrostVault did not expect is
recorded as `internal error (<type>)`, never with its message. An S3 error
names the step it happened in: the bucket creation, configuration or
verification.

**Cancel creation** removes a Vault that is still `provisioning` from the
catalog, after reauthentication, and releases its folder (an empty managed
folder is removed; an adopted folder and local files are never touched). A
bucket FrostVault already created is **not** deleted: the outcome and the audit record (`vault.creation_cancelled`)
name it, and say whether it was created or only possibly reached AWS, so the
Owner can check and remove it by hand. The bucket's `frostvault:vault` tag
still names the cancelled Vault's UUID. FrostVault writes a Vault Bucket's whole
lifecycle only at its creation and when the Vault's Storage Plan changes (or the
lifecycle no longer matches it); the rest of the configuration only at creation.

Creation is idempotent and resumable: the name is saved before `CreateBucket`,
`BucketAlreadyOwnedByYou` counts as created, and the configuration is applied
again and verified on the next attempt. `BucketAlreadyExists` means the name is
taken: FrostVault picks a new random name without probing the bucket. Any other
error is an error, never a reason to rename. Provider error codes are shown,
provider messages never.

### Retention Lock (optional, per Vault)

A new Vault may get a **Retention Lock**
([ADR-0050](adr/0050-governance-retention-lock-without-bypass.md), #736): its
Vault Bucket is created with S3 Object Lock and a default retention in
**Governance** mode of 30, 90, 180 (proposed) or 365 days. It is chosen only
on the Vault creation page (or `retention_lock_days` of `POST /api/vaults`) and
cannot be changed or removed from FrostVault afterwards; Compliance mode is
not offered, and a shared bucket from before Vault Buckets never has one.

Every version written to the bucket is locked for those days. FrostVault never
holds `s3:BypassGovernanceRetention` (the policy denies it explicitly), so a
stolen key, ransomware or a FrostVault bug cannot delete a locked version
early. Every FrostVault deletion waits for the lock instead of failing:

- **Superseded versions** (a verified replacement, a rename or Vault move that
  copies a file and deletes its source): the old version is handed over to
  the `retained_versions` table as a **Retained Version** and the background
  loop deletes it, by exact VersionId, from its RetainUntilDate (checked
  hourly, `DeleteObjects` in batches of 1000; a key being uploaded or copied
  waits). A failed attempt is retried after an hour when transient, after a
  day otherwise; three failures in a row open a Problem. The Vault settings
  show how many versions wait, until when, and the last failure. After a
  metadata restore, a superseded locked version no table names any more is
  adopted by the Reconnect or the weekly audit (#749).
- **Delayed Cloud Deletion** («Delete from cloud») and **Automatic Duplicate
  Cleanup**: a file still locked when the seven-day delay ends is not deleted;
  its Job waits again, «deletable from» its date, and the group runs again
  from the earliest one (at least an hour later). The Owner can still cancel
  what is left.
- **Vault Deletion**: when only locked versions are left, the request goes
  back to *pending* with how many versions remain and the date the last one
  can be deleted; it runs again from the earliest date (at most once a day)
  and the Vault leaves the catalog only when its bucket is empty. This is not
  a failure.

The previews of these deletions say how many files (or versions) will still be
locked when the deletion runs, and until when.

**How FrostVault knows the RetainUntilDate.** It never changes a retention
(`s3:PutObjectRetention` is not granted). The date is **estimated** as the
version's creation (the catalog's record of the upload, or `LastModified` in a
listing) plus the bucket's default days: S3 computes the default retention from
the moment the version is written, so the estimate matches up to seconds. A
version whose estimate is still ahead is not sent to S3 at all. When S3 refuses
a DELETE anyway (`AccessDenied`), FrostVault reads the exact version's
`GetObjectRetention` and waits for that observed date (a date up to five
minutes past still counts, for clock skew; the retry is at least an hour
later). If S3 refuses `GetObjectRetention` the version waits an hour and the
Vault gets a Problem: grant `s3:GetObjectRetention` (a locked Vault Bucket is
created only once FrostVault can read a retention). Lifecycle transitions
are not affected by Object Lock.

**Delete Markers.** FrostVault deletes only by exact VersionId and never
creates a Delete Marker. On a bucket with a Retention Lock the catalog audit
(weekly by default, `AUDIT_INTERVAL_SECONDS`) reports any Delete Marker as a
Problem, so one can stay unnoticed for up to that interval: someone deleted with the bucket's keys
without a version, and the lock kept the data hidden behind the marker. Check
who used the keys, then delete the marker version (for example from the AWS
console) to make the file visible again.

Locked versions are billed until they are deleted: superseded or deleted
files cost storage for up to the chosen days. A rename or Vault move copies
each file and hands the source over as a Retained Version, so the moved files
take twice their storage until the lock of the sources ends.

The default retention is applied by S3 when a version is written:
`PutBucketObjectLockConfiguration` (used once, at creation) and any later
change of the bucket's default made outside FrostVault affect only versions
uploaded afterwards. Existing versions keep their RetainUntilDate, and
FrostVault keeps estimating with the days chosen at creation, then waits for
the observed date whenever S3 refuses a DELETE.

### IAM for FrostVault-created buckets

`iam/frostvault-buckets-policy.json` is scoped to `arn:aws:s3:::frostvault-*`,
apart from its one CloudWatch statement:

| Statement | Actions |
| --- | --- |
| Buckets | `s3:CreateBucket`, `s3:ListBucket`, `s3:ListBucketVersions`, `s3:ListBucketMultipartUploads`, `s3:GetBucketLocation`, `s3:Get/PutBucketPublicAccessBlock`, `s3:Get/PutBucketVersioning`, `s3:Get/PutEncryptionConfiguration`, `s3:Get/PutLifecycleConfiguration`, `s3:Get/PutBucketTagging`, `s3:Get/PutBucketOwnershipControls`, `s3:Get/PutBucketObjectLockConfiguration` (a Vault Bucket's Governance Retention Lock only) |
| Objects (`frostvault-*/*`) | `s3:GetObject`, `s3:GetObjectVersion`, `s3:GetObjectTagging`, `s3:GetObjectVersionTagging`, `s3:GetObjectRetention` (the RetainUntilDate of a version a Retention Lock keeps), `s3:PutObject`, `s3:PutObjectTagging`, `s3:RestoreObject`, `s3:ListMultipartUploadParts`, `s3:AbortMultipartUpload` (Vault content, metadata backups and their read-back) |
| Deletion (`frostvault-*/*`) | `s3:DeleteObject`, `s3:DeleteObjectVersion`: the [accepted single-identity deletion model](#accepted-single-identity-deletion-risk) |
| CloudWatch (`"*"`) | `cloudwatch:GetMetricStatistics`: the daily [Vault Bucket sizes](#vault-bucket-sizes-from-cloudwatch). CloudWatch actions take no resource ARN |
| Deny | `s3:BypassGovernanceRetention`, always |

The same `s3:ListBucket` and `s3:GetObject` let FrostVault read the AWS billing
export under `billing/cur/` in the System Bucket ([billed-cost.md](billed-cost.md));
no Cost Explorer (`ce:*`) permission is used.

FrostVault never holds `s3:BypassGovernanceRetention`. `s3:DeleteBucket` is
used only by a Vault Deletion whose Owner ticked «also delete the bucket», once
the bucket is empty and tagged with that Vault (#739). Unlike
the shared archive bucket, whose settings the application only reads,
FrostVault configures the buckets it creates, so this policy grants the
matching `Put*` actions only on `frostvault-*`.

## Application preflight

Before cloud upload, recovery, or local cleanup, `validate_cloud_vault()` checks
the installation-wide readiness gate: one probe checks the bucket of every
active Vault (Vault Buckets and a shared bucket alike), so one bucket that
fails blocks cloud work until it is fixed. One read-only
`check_bucket_readiness()` probe is allowed at a time across workers/processes;
a successful result is cached for at most 60 seconds. Probe requests use short
2-second connect/3-second read timeouts and one SDK attempt; ordinary transfer
retry configuration is unchanged. Failures are actionable
and include remediation hints, for example:

- bucket name still a placeholder
- versioning suspended or disabled
- any of `BlockPublicAcls`, `IgnorePublicAcls`, `BlockPublicPolicy`, or
  `RestrictPublicBuckets` is false, missing, or not an explicit boolean `true`
- `GetPublicAccessBlock` is denied, unsupported, fails, or returns missing state
- region mismatch with `AWS_DEFAULT_REGION`
- missing `s3:GetBucketVersioning`, `s3:GetBucketPublicAccessBlock`, or `s3:ListBucket` (the permission `HeadBucket` needs; there is no `s3:HeadBucket` IAM action)
- rejected credentials (`SignatureDoesNotMatch`, `InvalidAccessKeyId`, expired token): a `HeadBucket` 403 has no body, so the probe makes one extra read-only `GetBucketLocation` (already in the minimum policy) to read the provider error code. When no code distinguishes the cases the message names both possible causes: wrong/expired credentials or a missing `s3:ListBucket`. Provider messages are never shown

Fix the reported issue; queued work resumes automatically after the next
successful probe, including after IAM propagation. Failures back off at
5/10/20/40/60 seconds (never longer); an independent one-second background tick
checks due probes even when all operation workers are occupied or Vaults are
suspended. Probe outcomes/backoff survive process restart; PostgreSQL releases
the single-flight lock on connection loss. A clock rollback/future restored
timestamp cannot leave the gate indefinitely blocked.

One installation warning and the app banner show the remedy. Affected Jobs stay
queued (or keep their prior waiting state), without consuming their transfer
retry budget. While the gate is blocked, upload activity counts Jobs that have sent no bytes as waiting (queued), even if a lease left by a stopped process still marks them `uploading`. Local catalog browsing/scanning, heartbeat and notifications remain
available. Cloud scans, uploads, recovery, rename and local cleanup remain
blocked until versioning and **all four bucket-level BPA flags** are confirmed enabled. Account-level BPA alone is not sufficient proof.
S3-compatible endpoints must implement the same API; there is no CI/provider
bypass. Grant read permission to the application, not permission to change BPA.

Vault keys are readable: `<relative-path>` at the root of a Vault Bucket,
`vaults/<uuid>/<relative-path>` in a shared bucket. Content is stored
without client-side encryption. AWS SSE-S3 (`AES256`) and TLS remain in place;
metadata backup ciphertext under `system/backups/` in the System Bucket remains
Fernet-encrypted.
Anyone with authorized object-read credentials can read photos and names: use
MFA and a bucket-scoped IAM policy. No real bucket configuration is changed by
an application migration or preflight.

### Retrying historical failed uploads (#461)

**General → Retry failed uploads** selects all Vaults; the same action in
**Vault settings** selects only that Vault. `POST /api/upload/retry-failed`
accepts an optional `vault_id` query parameter: omission means global scope,
never the Session's selected Vault. A repeat click safely returns zero when
there is no newly eligible work. Existing failed attempts remain unchanged.

Only files whose latest upload attempt failed and which still pass ordinary
stable automatic-upload admission receive fresh Jobs. Active Jobs, cancelled
uploads, already protected content, Cloud-only Conflicts, missing/untrusted or
replaced Source Volumes/roots, disabled/suspended/stopped Vaults, pending deletion
and exclusion rules remain fenced. Failed recover/free-space/cloud-purge Jobs
are never retried. This action neither completes historical verified failures
(#459) nor grants authority over unknown cloud versions (#464).

**Vault settings → Upload whole Vault** uses the existing `/api/upload` action
with `{"path":"","is_directory":true}` and explicit `vault_id`. This narrow root
allowance applies only to uploads; traversal, absolute paths and empty paths for
other operations remain rejected. The catalog's usual eligibility/active-Job
checks still apply.

Migration `0062_cloud_preflight_gate` adds only the readiness cache table. It
makes no S3 calls, changes no historical Job or catalog trust, and requires no
manual queue reset. Deploy API and generated SPA contracts together after the
normal authorized metadata migration/upgrade procedure.

## Vault storage plans

The Owner chooses one plan in **Vault settings → Advanced** (GET/PUT
`/api/vault/lifecycle`). `vaults.storage_plan_json` stores only an ordered chain
of current-version transitions. There are no folder assignments, pins, manual
per-file class changes, expiration or noncurrent-version actions.

| Plan | Timeline and cost |
| --- | --- |
| Always accessible (`standard_only`) | Standard, immediate access without retrieval fees; highest storage cost. |
| Economical instant access (`economical`) | Standard-IA at day 30, Glacier Instant Retrieval at day 90. Lower storage cost; retrieval fees and 30/90-day minimum billing. |
| Tiered archive — recommended (`archive_tiered`) | IA30 → GlacierIR90 → DeepArchive365. Progressively cheaper storage; after day 365 restores take hours and have retrieval fees and 180-day minimum billing. |
| Early Deep Archive (`deep_archive_early`) | Deep Archive at day 0 (next lifecycle evaluation). Lowest storage cost; hours to recover, retrieval fees, 180-day minimum billing even after early replacement/deletion. |
| Custom | Ordered `after N days → class` chain, with transition/retrieval fees and minimum-duration billing. |

Days are object ages since upload, not delays between steps. Standard can go
directly to Glacier Flexible Retrieval or Deep Archive at day 0; there is no
absolute 90/180-day minimum from Standard. IA requires age 30. A single chain
must leave at least 30 days in IA and 90 in Glacier Instant/Flexible Retrieval
before a subsequent transition. The AWS waterfall is validated (One Zone-IA
cannot go to Glacier Instant Retrieval; Standard-IA can go to One Zone-IA).
See [AWS transition constraints](https://docs.aws.amazon.com/AmazonS3/latest/userguide/lifecycle-transition-general-considerations.html).

A Vault Bucket's lifecycle is the Vault's alone (see
[Buckets FrostVault creates](#buckets-frostvault-creates)): its plan rule has no
filter, and a plan change replaces the whole lifecycle. In a shared bucket the
application generates one exact `vaults/<uuid>/` prefix rule per transitioning
Vault (`frostvault-plan-<uuid>`). Existing legacy prefixes are retained exactly
with the same normalization as the cloud reader/writer, never replaced with an
invented UUID namespace. Empty/overlapping bucket namespaces stop synchronization
before any write (including overlap with a Standard Vault). No object retagging
is necessary. A Standard plan
removes its transition rule, but does not warm existing cold objects. AWS applies
transitions asynchronously; old objects can transition shortly after changing a
plan. Objects below 128 KiB never transition: every lifecycle FrostVault writes
sets `TransitionDefaultMinimumObjectSize` to `all_storage_classes_128K` (the AWS
default since September 2024), so they stay in Standard. Early replacement can
incur minimum-duration fees. Changing a plan never copies or deletes content.

Save attempts bucket synchronization immediately; failure reports `sync_pending`
and preserves the plan for retry at the next scan. Each sync replaces all
application rules, including legacy `psa-policy-*` rules, while preserving external
rules and `frostvault-abort-incomplete-multipart` (prefix `vaults/`, abort after
7 days). Only unfinished multipart uploads are aborted, never completed content.
The combined configuration is checked against the 1,000-rule limit. Review
external expiration rules yourself: FrostVault deliberately does not own them.

### Storage-plan upgrade (0053)

Migration `0053_vault_storage_plans` performs no S3 calls or content deletion and
preserves catalog identities, integrity/availability, Jobs, notifications and
metadata backups. A missing default maps to Standard; identical folder policies
collapse into the Vault default. Retired expiration and noncurrent actions are
discarded under #418/#421, not translated into deletion. Heterogeneous folder
plans, pins conflicting with a transitioning default, missing/cross-Vault
references, invalid chains or empty/overlapping namespaces in a transitioning
bucket stop **before DDL**. Resolve assignments deliberately
using the previous release before retrying; do not relabel or delete cloud data.
Pins on an already Standard Vault are safely redundant. Unassigned policies are
retired metadata only. Legacy cloud tags can remain inert: prefix rules no longer
use or rewrite them. The next successful sync replaces old application rules;
confirm it succeeded before using an upgraded bucket. Downgrade recreates only
an equivalent single default, never historical folder/pin/expiration promises.

## Weekly catalog audit

`AUDIT_INTERVAL_SECONDS` (default one week) drives `audit_all_vaults()` in the
background loop. Each run lists object versions and delete markers and compares
them to the catalog without restoring Glacier content or reading policy tags.
Detected drift updates storage class and availability
(`missing` when a catalogued version is gone from S3).

The interval counts from each Vault's last **successful** audit. A failed audit
leaves the Vault unconfirmed (Free space is refused) and opens an in-app
Problem with the reason. It is retried after 5, 10, 20 and 40 minutes, then
hourly; at startup; and as soon as the S3 readiness gate returns to `ready`. The
next successful audit of that Vault closes the Problem.

## SDK transfer upgrade (0052)

Migration `0052_s3_sdk` removes only the retired transport alias column. It makes
no S3 calls, changes no cloud key/VersionId, and preserves catalog trust, Jobs and
metadata backups. Remove obsolete remote/config and multi-thread environment
variables and the former config bind mount when deploying. AWS credentials,
region, versioning and BPA remain required. Downgrade recreates an empty legacy
alias column, requiring deliberate operator configuration before old uploads.
SDK retries use botocore standard mode in addition to the existing eight-attempt
Job retry policy. Verify `s3:AbortMultipartUpload` permission before deployment.

## Upgrades

- **Terraform changes**: run `terraform plan` in a maintenance window; bucket
  settings are updated in place where AWS allows it.
- **New listing permissions**: existing application IAM identities need
  `s3:ListBucketVersions` and `s3:ListBucketMultipartUploads` before deployment,
  or interrupted uploads cannot be reconciled and stay blocked per file.
- **Versioning and BPA must stay enabled**: suspending versioning or disabling
  any BPA flag blocks operations. Existing application IAM identities need the
  new `s3:GetBucketPublicAccessBlock` permission before deployment. Review/apply
  bucket flags yourself; the application only reads them.
- **Lifecycle rules**: the Terraform module manages its multipart-abort baseline.
  The application also ensures its own seven-day `vaults/` abort rule during
  lifecycle synchronization, including when there are no transition profiles.
  External rules are preserved; the combined configuration enforces the
  1,000-rule bucket limit.
  Migration `0050_current_snapshot_only` makes no S3 calls; existing
  application-owned expiration rules are removed on the next successful
  lifecycle synchronization. Before using an upgraded test bucket, confirm
  that synchronization succeeded; externally managed expiration rules are
  preserved and must be reviewed by the operator.

## Costs

- **Versioning** is internal to verified replacement. Each successful verified
  upload or safe rename deletes superseded versions. Failed uploads/verifications
  and legacy copies without a verified available keeper remain fail-closed;
  storage may temporarily include those extra bytes. There is no recovery history.
- **Storage classes** (Standard, IA, Glacier tiers) affect retrieval cost and
  minimum-duration billing; each plan explains timing and cost before saving.
- **Restore requests** from Glacier tiers incur per-GB and per-request charges.
- FrostVault estimates storage and restore costs from AWS list prices bundled
  per region (`app/services/aws_list_prices.json`, selected by
  `AWS_DEFAULT_REGION`). Regenerate them with
  `python scripts/update_aws_list_prices.py` when AWS changes S3 prices.

### Vault Bucket sizes from CloudWatch

Once a day per Vault, off the request path, the background loop reads the S3
storage metrics of every Vault Bucket from CloudWatch, in the bucket's region
(#737, [ADR-0051](adr/0051-billed-cost-from-the-billing-export.md)):

- `BucketSizeBytes` for each of the 26 `StorageType` values of a general
  purpose bucket (one `GetMetricStatistics` call per type: `StandardStorage`,
  `StandardIAStorage`, `GlacierInstantRetrievalStorage`, `GlacierStorage`,
  `DeepArchiveStorage`, the Intelligent-Tiering tiers and the overhead types
  below);
- `NumberOfObjects` (`AllStorageTypes`).

Only `cloudwatch:GetMetricStatistics` is used, which stays in CloudWatch's free
API tier (27 requests per read: about 27 per Vault a day, at most about 108 when
every read of the day fails); `GetMetricData` is always billed and never called.
Each read asks for the daily data points of the last 30 UTC days, today
included, and FrostVault keeps exactly those 30 days in the catalog: every
recorded read, a failed one too, prunes the older ones. The time and outcome of
the last read are kept as well, so a restart never reads again early: a Vault is
read again 24 hours after a successful read and 6 hours after any other outcome
(no data yet, no permission, an error).

The CloudWatch client gives up fast (3 s to connect, 10 s to read, two attempts
in all). A pass over the Vaults stops at the first connection error (the network
or the CloudWatch endpoint is unreachable), on shutdown, or once it has run 2
minutes (a Vault whose read started is finished first); the Vaults it did not
reach are read at the next poll, 5 minutes later.

The Vault page shows them next to the catalog's Cost Estimate: bytes per
storage class, the object count and the day of the data point. S3 publishes
these metrics once a day with about a day of delay, so the newest figures are
one or two days old, and a new bucket has none for a day or two. The overhead
AWS bills on top of the objects is shown apart from their size:

| StorageType | What it is | Billed as |
| --- | --- | --- |
| `GlacierObjectOverhead`, `DeepArchiveObjectOverhead` | 32 KB of index per archived object | the object's class |
| `IntAAObjectOverhead` | 32 KB of index per object in the Intelligent-Tiering Archive Access tier | Glacier Flexible Retrieval |
| `IntDAAObjectOverhead` | 32 KB of index per object in the Intelligent-Tiering Deep Archive Access tier | Deep Archive |
| `GlacierS3ObjectOverhead`, `DeepArchiveS3ObjectOverhead`, `IntAAS3ObjectOverhead`, `IntDAAS3ObjectOverhead` | 8 KB of metadata per archived object | Standard |
| `StandardIAObjectOverhead` | listed by AWS among the Standard-IA types but not described; shown as per-object metadata | Standard-IA (assumed) |
| `StandardIASizeOverhead`, `OneZoneIASizeOverhead`, `GlacierIRSizeOverhead` | the difference to the 128 KB billed for a smaller object | the object's class |
| `GlacierStagingStorage`, `DeepArchiveStagingStorage` | parts of a multipart upload not completed yet | Standard |

`NumberOfObjects` counts every version, noncurrent ones included, and the parts
of incomplete multipart uploads, so it can differ from the catalog's versions.

A Vault on a shared bucket (an older Vault, under `vaults/<uuid>/`) shows only
the catalog estimate: the bucket's metrics cover the other Vaults too. Without
`cloudwatch:GetMetricStatistics` the Vault page shows a note and the estimate;
it is not a Problem. Any other CloudWatch error is shown by its code and
retried 6 hours later; an unexpected error is recorded the same way, with its
error type, and reported as the CloudWatch worker's Problem.

## Teardown and recovery

- Empty the bucket or remove noncurrent versions before `terraform destroy`.
- Deleting the bucket does **not** restore local copies removed after verified
  cloud upload; keep independent backups of the database and `ARCHIVE_MASTER_KEY`
  (see [metadata-backups.md](metadata-backups.md) for automated encrypted metadata
  backups under `system/backups/` in the System Bucket and separate master-key
  custody). Metadata backups made before the System Bucket stay in this bucket.
- Glacier restores cannot be cancelled after AWS accepts the request; plan
  retention transitions accordingly.

## Failure recovery

| Symptom | Action |
|--------|--------|
| Preflight reports versioning disabled | Re-enable versioning in S3 console or re-apply Terraform |
| BPA not fully enabled or not verifiable | Enable all four bucket flags; grant `s3:GetBucketPublicAccessBlock`; use an API-compatible provider |
| Region mismatch | Set `AWS_DEFAULT_REGION` to the bucket region from `terraform output` |
| Preflight reports rejected credentials (`SignatureDoesNotMatch`, `InvalidAccessKeyId`) | Correct `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` and restart or wait for the next probe; queued Jobs resume automatically |
| Access denied on preflight | Attach or update IAM policy from `iam/archive-operator-policy.json` |
| A file is blocked as `list_permission_denied` | Grant `s3:ListBucketVersions` and `s3:ListBucketMultipartUploads` on the bucket (see `iam/archive-operator-policy.json`), then use Retry failed uploads |
| A file is blocked as `read_permission_denied` | Grant `s3:GetObject` and `s3:GetObjectVersion` on the bucket objects, then use Retry failed uploads |
| A file fails as `write_permission_denied` | Grant `s3:PutObject` and `s3:AbortMultipartUpload` on the bucket objects, then use Retry failed uploads |
| A file is blocked as `abort_permission_denied` | S3 refused to abort an incomplete multipart upload of an interrupted upload; its parts are still stored. Grant `s3:AbortMultipartUpload` on the bucket objects, then use Retry failed uploads (the lifecycle rule also reaps them after 7 days) |
| Every upload waits for S3 readiness after a 403 | The bucket probe itself fails (installation-wide revocation): fix the IAM policy or credentials; queued Jobs resume automatically |
| Uploads of a Vault wait with `source_volume_unavailable` | The Source Volume or Vault root cannot be read. Jobs back off (up to 5 minutes between checks) and resume by themselves; an in-app notice appears at the first check after 10 minutes of outage (about 14 minutes with that backoff) and resolves by itself when the volume is readable again or no upload waits for it |
| Upload fails without VersionId | Same as versioning disabled; fix bucket baseline first |
