# Explicit per-Vault reconnect and one independently custodied Recovery Kit

Decision #364, as amended by #366's plaintext/SDK-only model and implemented by
#427, replaces relocation/rebind with **Reconnect Folder**. The Owner may accept
a new Source Volume or different directory for an existing Vault; content
similarity is not authority to replace cloud bytes. This supersedes ADR-0014's
same-inode/same-volume restriction without weakening ADR-0012's containment or
ADR-0016's sole-copy safety.

## Consequences

Acceptance stores the observed opaque mount fingerprint on this Vault only;
shared Source Volume enrollment remains immutable. Sibling Vaults on a replaced
alias stay blocked. Missing/ambiguous/unsupported mounts, symlinks, nested mounts,
overlapping allocations and active Jobs/scans still fail closed. Existing
allocation, Job admission and scan locks serialize the change. The new directory
is enrolled and pinned for the scan; future mount or directory replacement
blocks local work again. The historical `relocation_state` fields are internal
reconnect recovery state, not a second operation or public option.

A managed Vault (#641) may also reconnect to its own `/sources/managed/<uuid>`,
never another Vault's managed folder or other path under `managed`, and a custom
Source Volume Vault never moves into `managed`. The folder is never created. Its
boundary is the `/sources` mount: that mount's fingerprint must be available and
unchanged across validation and no nested mount may intersect the folder. As for
a newly created managed Vault, only the directory identity is stored; no
per-Vault mount fingerprint is accepted. Gates, audit and the mandatory scan are
the same; nothing reconnects automatically after a restore.

Reconnect performs cloud reconciliation first, with cleanup disabled, then a
mandatory complete local rehash. Unknown current cloud content needs a full
exact-VersionId read before its digest can become recovery evidence; inaccessible
cold objects leave reconnect suspended until manually restored. It never trusts
old local size/mtime or propagates missing local files to S3. Every existing cloud
path is held: matching content becomes Protected, absent content Cloud-only,
different bytes a Cloud-only Conflict. Incomplete/failed scans remain suspended
across restart. Automatic rename inference is not authority to delete cloud
copies during this recovery operation.

The installation kit includes the metadata master key, cloud bucket/region and
manual PostgreSQL/S3 recovery instructions, never catalog data, AWS credentials,
per-Vault exports or crypt configuration. Its domain-separated short fingerprint
changes with key or cloud location; one custody acknowledgment applies only to
that fingerprint. Download requires recent Owner reauthentication, has no-store
headers and records only a sensitive-action audit event. Key changes invalidate
custody; they do not re-encrypt old metadata artifacts. Keep old independently
custodied keys while those artifacts remain needed.

The manual metadata restore records the manifest's backup date in the restored
database, invalidates active Vault local observations and cancels stale
nonterminal Jobs without filesystem/cloud operations. Cloud discovery accumulates
distinct updated Vault Files durably and
the recovery panel keeps the date/count warning visible (also after reading
notifications/restarting). Settings changed since that backup remain manual.
There is no first-start restore wizard or in-app catalog reconstruction from S3;
without metadata, the independent emergency route recovers files only.
