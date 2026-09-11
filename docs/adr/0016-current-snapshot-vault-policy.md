# Current Snapshot is a creation-time Vault policy

FrostVault's shared archive bucket is versioned. A Vault's Cloud History Policy
is chosen at creation and is immutable: Archive History keeps every verified
Archive Version recoverable; Current Snapshot keeps only the last verified
upload. Current Snapshot is implemented by deleting previous S3 VersionIds after
the new Archive Version is verified — not by an unversioned bucket, a mutable
catalog row, or in-place conversion of an Archive History Vault.

The catalog still stores immutable Archive Versions; under Current Snapshot at
most one stays `available` per Vault File. Upload automation remains a separate
operation policy. Interactive creation requires an explicit Cloud History Policy;
there is no implicit default. Lifecycle, storage-class, and Glacier apply to
that single Archive Version as they do under Archive History.

Rename still keeps one Vault File and Path History. After the new key is
verified, every VersionId at the old key is deleted rather than hidden
(derogates ADR-0001 for this policy only). Hide in cloud and permanent purge
both remain: Hide is a reversible Delete Marker on the single copy; Purge
destroys it. A Local Copy can still be removed without touching cloud.

The Vault S3 prefix is application-owned. Manual edits in the bucket are
unsupported. Extra VersionIds (crash before purge, external drift) are not
recoverable; they raise an incident and the worker retries purge.

Quota admission still charges the full size of the incoming Local Copy, not the
net after purge. Bootstrap Vaults (`BOOTSTRAP_*`) are Archive History so
existing deploys keep their promise; only the interactive form forces an
explicit choice.

## Considered Options

- A second unversioned bucket: splits provisioning, preflight, and IAM for one
  installation.
- Updating one catalog row in place: Jobs, Integrity, and Availability already
  target Archive Version identity.
- Converting an existing history Vault: a mass purge disguised as a setting.
