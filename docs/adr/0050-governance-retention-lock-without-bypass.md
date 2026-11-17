# Retention Lock is Governance-only, chosen at creation, never bypassed

Status: accepted by the Owner on 2026-11-17 (issue #732).

A Vault may be created with a Retention Lock: S3 Object Lock in Governance mode
with a bucket default retention of 30, 90, 180 (default) or 365 days. It cannot
be changed from FrostVault afterwards, and FrostVault's credentials never hold
`s3:BypassGovernanceRetention`, so a leaked key or a FrostVault bug cannot
delete a locked version early. Compliance mode is not offered: not even the
account root can shorten or remove it.

Every deletion path (superseded Current Snapshots, duplicate cleanup, Delayed
Cloud Deletion, Vault Deletion) defers a locked version to its
`RetainUntilDate` and shows «deletable from …» instead of failing. With Deep
Archive and 180 days the lock costs nothing extra, because the Minimum Storage
Duration is billed anyway. The System Bucket has no lock: metadata-backup
retention must be able to delete.
