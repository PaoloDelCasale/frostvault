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
slug is lowercase ASCII, at most 40 characters; the Vault UUID lives in the
`frostvault:vault` tag, not in the name. IAM can be scoped to
`arn:aws:s3:::frostvault-*`.

## Consequences

Creating a Vault needs `s3:CreateBucket` and the bucket configuration
permissions. Bucket creation is idempotent and resumable; a created but
unregistered bucket opens a Problem instead of being silently orphaned. Vault
Deletion removes the bucket only when the Owner ticks it and the bucket is
empty.
