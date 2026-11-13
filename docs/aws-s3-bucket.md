# AWS S3 bucket provisioning and validation

This guide covers the dedicated archive bucket required by issue #8 and the
application preflight checks that validate it before upload or local cleanup.

## Provision with Terraform

Use the module at `infra/terraform/archive-bucket/`:

1. Choose a globally unique bucket name and region.
2. Run `terraform init`, `terraform plan`, and `terraform apply`.
3. Copy the `application_env` output into the application environment:
   - `AWS_DEFAULT_REGION`
   - `S3_BUCKET`
   - `VAULT_S3_BUCKET`

The module enables versioning, blocks public access, enforces bucket-owner object
ownership, applies SSE-S3 (`AES256`) by default, and aborts incomplete
multipart uploads after seven days. Object Lock is intentionally **not** enabled.

## IAM

Attach `infra/terraform/archive-bucket/iam/archive-operator-policy.json` to the
archive operator identity. Replace `${archive_bucket_name}` with your bucket
name. The policy grants:

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

## Application preflight

Before cloud upload, recovery, or local cleanup, `validate_cloud_vault()` checks
the installation-wide readiness gate for the shared Vault bucket. One read-only
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

Vault keys are readable `vaults/<uuid>/<relative-path>` and content is stored
without client-side encryption. AWS SSE-S3 (`AES256`) and TLS remain in place;
metadata backup ciphertext under `system/backups/` remains Fernet-encrypted.
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

The application generates one exact `vaults/<uuid>/` prefix rule per transitioning
Vault (`frostvault-plan-<uuid>`). Existing legacy prefixes are retained exactly
with the same normalization as the cloud reader/writer, never replaced with an
invented UUID namespace. Empty/overlapping bucket namespaces stop synchronization
before any write (including overlap with a Standard Vault). No object retagging
is necessary. A Standard plan
removes its transition rule, but does not warm existing cold objects. AWS applies
transitions asynchronously; old objects can transition shortly after changing a
plan. Objects below 128 KB do not transition by default. Early replacement can
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

## Teardown and recovery

- Empty the bucket or remove noncurrent versions before `terraform destroy`.
- Deleting the bucket does **not** restore local copies removed after verified
  cloud upload; keep independent backups of the database and `ARCHIVE_MASTER_KEY`
  (see [metadata-backups.md](metadata-backups.md) for automated encrypted metadata
  backups under `system/backups/` and separate master-key custody).
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
