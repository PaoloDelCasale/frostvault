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

## Addendum (#749): Retained Versions after a restore, sweep failures, clock skew

- A metadata restore or an audit can find a superseded locked version that no
  table names (written and superseded after the backup). When the newest
  version of its key is a Vault File's Current, it has no Delete Marker and no
  write is in progress at the key, it is adopted as a Retained Version,
  deletable from its `LastModified` plus the bucket default days. Adoption
  writes only to the database; the lock itself is never read for it: the DELETE
  is sent from that date and a refusal is trusted. A version a reconnect
  promotes to Current is dated by its `LastModified` too (#743).
- The sweep deletes due Retained Versions with `DeleteObjects` in batches of
  1000, while no database connection is held, until nothing is due or its time
  budget ends; a key with an open upload or copy attempt waits. A failure is
  visible (count, last error) and retried after an hour when transient, after
  a day otherwise; three in a row open a Problem.
- A refusal is the lock while the observed `RetainUntilDate` is later than five
  minutes ago (clock skew); it is retried at the later of that date and an hour
  from now. When S3 refuses `GetObjectRetention`, the version waits an hour and
  the Vault gets a Problem; a locked Vault Bucket is created only if that
  permission is granted.
