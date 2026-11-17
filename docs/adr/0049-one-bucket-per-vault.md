# One FrostVault-created bucket per Vault, plus a System Bucket

Status: accepted by the Owner on 2026-11-17 (issue #732).

Every new Vault gets its own Vault Bucket, created and verified by FrostVault
before the Vault becomes active (Block Public Access, versioning, SSE-S3,
unfiltered lifecycle plus incomplete-multipart abort, `frostvault:*` tags, and
an optional Retention Lock). Objects live at the bucket root. Metadata backups
move to a System Bucket (`METADATA_BACKUP_BUCKET`, or `frostvault-system-<random>`
created by FrostVault), which the Recovery Kit names. `VAULT_S3_BUCKET` is
removed: every operation reads the bucket and prefix from the Vault's own target.

## Why

Per-bucket CloudWatch metrics and billing lines give real per-Vault sizes and
costs; policies, lifecycle and Object Lock are per bucket, so one Vault's
mistake or leaked rule cannot touch another.

## Considered and rejected

- **Prefixes in one shared bucket** (the previous design): no per-Vault metrics
  or costs, lifecycle filters per prefix, no per-Vault Object Lock.
- **Registering an existing bucket**: not needed. Disaster recovery restores the
  catalog from the System Bucket, which already names every Vault Bucket;
  adopting a bucket the catalog does not know is a different feature.
- **Moving the existing shared-bucket Vault**: copying content out of Deep
  Archive means restoring it and paying the 180-day minimum again. It keeps its
  target (shared bucket plus `vaults/<uuid>/` prefix) with no special code path.

## Naming

`frostvault-<vault-name-slug>-<8 random [a-z0-9]>` in `AWS_DEFAULT_REGION`. The
slug is lowercase ASCII, at most 40 characters, and never `system` or
`system-…` (those become `vault-system…`): only the System Bucket is
`frostvault-system-*`. The Vault UUID lives in the `frostvault:vault` tag, not
in the name. IAM can be scoped to
`arn:aws:s3:::frostvault-*`.

## Consequences

Creating a Vault needs `s3:CreateBucket` and the bucket configuration
permissions. Bucket creation is idempotent and resumable; a created but
unregistered bucket opens a Problem instead of being silently orphaned. Vault
Deletion removes the bucket only when the Owner ticks it and the bucket is
empty.

## Implementation notes (#735)

- A Vault Target records its `key_layout`: `bucket_root` (a Vault Bucket, prefix
  empty) or `vault_prefix` (the shared bucket of an older Vault, prefix
  `vaults/<uuid>/`). Every key is built from the target's prefix.
- A new Vault is `provisioning` until its bucket is verified and 15 minutes
  more (AWS recommends that wait after enabling versioning before writing
  objects); the reserved name is saved first, and a name another account owns
  is replaced (the only change of target a Vault may make, and only while
  provisioning). A `bucket_root` target belongs to exactly one Vault.
- The Owner may cancel a creation while the Vault is provisioning: the Vault
  leaves the catalog, and a bucket already created stays in AWS, named in the
  outcome and the audit record for manual removal (no `s3:DeleteBucket`, #739).
- Every lifecycle FrostVault writes sets `TransitionDefaultMinimumObjectSize`
  to `all_storage_classes_128K`: objects smaller than 128 KiB stay in Standard.
- The readiness gate stays installation-wide and probes the bucket of every
  live Vault.
- Off-host metadata backups are required as soon as any Vault exists.
