# Plaintext Vaults with fail-closed bucket public-access checks

Decision #366 Q13, implemented by #419, removes per-Vault encryption in favor of
file-only emergency recovery using any authorized S3 tool. Original bytes and
readable names live under `vaults/<uuid>/<relative-path>`; there are no per-Vault
secrets, recovery exports, custody confirmations or filename transforms. The
Owner accepts that AWS object-read credentials expose photos and filenames;
MFA and bucket-scoped IAM are recommended. AWS SSE-S3, TLS and encrypted metadata
backups remain independent protections, not retired encryption modes.

## Consequences

Bucket preflight requires versioning and all four bucket Block Public Access
flags explicitly true. Denial, unsupported APIs and missing state fail closed;
account-level policy is not proof of the required bucket configuration.
Migration 0051 refuses encrypted legacy rows before DDL and does not touch S3,
convert keys or discard legacy secrets. Legacy crypt remotes are also refused,
not reinterpreted as plaintext. Test Vaults have no compatibility promise (#362):
recover deliberately with the old release before creating a fresh installation.
Metadata backup ciphertext and `ARCHIVE_MASTER_KEY` remain necessary for metadata
restore. The unified Recovery Kit belongs to #427, not this removal.

Rclone remains the plaintext upload/read-back transport until #420. Official
Rclone cannot pin an S3 VersionId; upload verification additionally reuses the
existing SDK exact-version integrity reader before granting trust or deleting
anything. This temporary double read favors safety over cost. Rename/recovery
keep their existing exact-version SDK reads. Verified replacement guards from
ADR-0001/0016 remain mandatory.
