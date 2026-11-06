# Plaintext Vaults with fail-closed bucket public-access checks

> ADR-0023 supersedes the CRC32/read-back-only proof contract below **for new
> uploads only**, with independent SHA256 full/composite evidence and visible
> exact Read-back fallback. Shared-operation verification is not weakened.

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
convert keys or discard legacy secrets. Legacy ciphertext is not reinterpreted
as plaintext. Test Vaults have no compatibility promise (#362):
recover deliberately with the old release before creating a fresh installation.
Metadata backup ciphertext and `ARCHIVE_MASTER_KEY` remain necessary for metadata
restore. The unified Recovery Kit belongs to #427, not this removal.

#420 replaces the transport with managed SDK multipart uploads. S3 validates a
CRC32 FULL_OBJECT checksum supplied at completion (SHA256 multipart checksums
would be composite). VersionId is captured from PutObject/CompleteMultipartUpload,
never inferred from a later HEAD. If a compatible server omits the checksum in
its completion response, an exact-VersionId checksum-enabled HEAD must independently
confirm the FULL_OBJECT CRC32; missing/composite/wrong checksums fail closed.
A single streamed GetObject of that exact VersionId is compared to the durable
local SHA256 snapshot. Concurrent uploads
share one bandwidth budget; progress writes are throttled. Cancellation/failure
cancels the manager and explicitly aborts incomplete multipart uploads, while a
seven-day prefix-only lifecycle rule catches process crashes. Completed objects
are not deleted by transport cleanup. Rename/recovery retain exact-version SDK
reads; verified replacement guards from ADR-0001/0016 remain mandatory.
