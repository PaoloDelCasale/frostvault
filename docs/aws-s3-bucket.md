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

- bucket list, location, versioning, lifecycle, and tagging
- object upload, version read, restore, and tagging
- delete object/version for the accepted single-identity deletion model

### Accepted single-identity deletion risk

The initial product accepts one IAM identity for ordinary archive operations and
for permanent `DeleteObject` / `DeleteObjectVersion` calls used by reversible
archival (Delete Markers) and permanent purge. That identity can therefore both
write Archive Versions and destroy them.

Mitigations built into the application:

- vault cloud-deletion setting defaults to **off**
- permanent purge requires owner/admin reauthentication, vault-name or generated
  phrase confirmation, a typed reason, and a cancellable delay (default 24 hours)
- operators and viewers cannot approve or execute permanent purge
- every request, cancellation, partial failure, and completion is audited and
  notified

Prefer a dedicated deletion role and stricter IAM boundaries when your threat
model requires separating ordinary archive work from permanent purge.

## Application preflight

Before cloud upload, recovery, or local cleanup, `validate_cloud_vault()` runs
`check_bucket_readiness()` against the vault bucket. Failures are actionable and
include remediation hints, for example:

- bucket name still a placeholder
- versioning suspended or disabled
- region mismatch with `AWS_DEFAULT_REGION`
- missing `s3:GetBucketVersioning` or `s3:HeadBucket`

Fix the reported issue and retry the operation. Upload and cleanup remain
blocked until versioning is enabled.

## Lifecycle policy tagging

Archive Versions carry immutable policy UUIDs in the database (`desired_policy_id`
and `applied_policy_id`) and on the matching S3 object version via the
`psa:policy-id` tag.

- **Vault default** — `vaults.default_lifecycle_policy_id` applies when no folder
  override matches the logical path.
- **Folder overrides** — `folder_policy_overrides` map a folder prefix to a policy
  UUID; the longest matching prefix wins.
- **Upload** — after a successful upload the application tags the new object
  version and records both policy columns.
- **Cloud scan** — reads existing object tags, recomputes the desired policy from
  current assignments, and records drift when `desired_policy_id !=
  applied_policy_id`.
- **Reconciliation** — each vault scan retries pending tags in batches so policy
  changes remain safe across restarts.

Policy identities are UUIDs only; mutable display names live in
`lifecycle_policies.name` and are never written to S3 tags.

### Guided profiles and S3 lifecycle rules

Each `lifecycle_policies.profile_json` stores a guided transition profile.
Validation enforces supported storage classes, strictly increasing transition
days, and AWS minimum durations. Glacier-family transitions also return cost
warnings about retrieval and minimum storage-duration billing.

During each vault scan the application rebuilds only its own lifecycle rules
(`psa-policy-<uuid>` IDs) and merges them with existing bucket rules such as the
Terraform multipart-abort baseline. Rules filter objects by the `psa:policy-id`
tag, so crypt vaults do not need readable key prefixes. The sync aborts before
exceeding the S3 limit of 1,000 lifecycle rules per bucket.

Built-in guided profiles:

- `standard_only` — no transitions
- `ia_after_30` — Standard → Standard-IA after 30 days
- `archive_tiered` — Standard-IA at 30 days, Glacier Instant Retrieval at 90
  days, Deep Archive at 365 days

Vault owners configure these from **Manage access → Lifecycle policy**. Mutations
require recent reauthentication and are audited.

## Weekly catalog audit

`AUDIT_INTERVAL_SECONDS` (default one week) drives `audit_all_vaults()` in the
background loop. Each run lists object versions and delete markers, reads policy
tags, and compares them to the catalog without restoring Glacier content.
Detected drift updates storage class, applied policy tags, and availability
(`missing` when a catalogued version is gone from S3).

## Upgrades

- **Terraform changes**: run `terraform plan` in a maintenance window; bucket
  settings are updated in place where AWS allows it.
- **Versioning must stay enabled**: suspending versioning blocks destructive
  local cleanup and new uploads that rely on immutable Archive Versions.
- **Lifecycle rules**: the Terraform module only manages the multipart-abort
  baseline. Application-managed lifecycle profiles (future work) add their own
  rules and enforce the 1,000-rule bucket limit.

## Costs

- **Versioning** retains every Archive Version; storage grows with each content
  change until lifecycle transitions or explicit deletion.
- **Storage classes** (Standard, IA, Glacier tiers) affect retrieval cost and
  minimum retention; guided profiles will surface estimates before apply.
- **Restore requests** from Glacier tiers incur per-GB and per-request charges.

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
| Region mismatch | Set `AWS_DEFAULT_REGION` to the bucket region from `terraform output` |
| Access denied on preflight | Attach or update IAM policy from `iam/archive-operator-policy.json` |
| Upload fails without VersionId | Same as versioning disabled; fix bucket baseline first |
