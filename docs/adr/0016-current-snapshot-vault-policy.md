# Current Snapshot is the only cloud behavior

Decision #362 removes user-facing cloud history and its creation-time policy
choice. Every Vault keeps only the last verified content of each Vault File.
The shared bucket stays versioned as an internal verified-replacement mechanism:
upload first, read and verify the exact VersionId, then delete superseded
VersionIds. Immutable Archive Version identities remain for Jobs, Integrity and
Availability; replaced versions become `purged`, not rewritten in place.

## Consequences

A failed upload or verification preserves the previous cloud copy. A cleanup
requires a verified, available keeper whose exact key and VersionId are present
in a successful listing. Discovery never grants verification: legacy unverified
or mismatched copies are retained fail-closed. A stale catalog, a missing keeper,
unknown rename content or a failed listing never authorizes cloud deletion.
Migration 0050 removes the policy column without calling S3 or modifying Archive
Version trust/content. There is no compatibility promise for test Vaults; the
owner can recreate them. Downgrade recreates the column as snapshot for every
Vault, not the retired history promise.

Rename retains stable Vault File identity and internal Path History. After
verifying the exact bytes at the new key, all versions at the old key are purged
(ADR-0001). Ambiguity is a new Vault File, never authority to remove another
file. Deleting a Local Copy, including via SMB, never deletes cloud content.
Content differing from the cloud copy at the path of a previously absent Local
Copy remains a Cloud-only Conflict until renamed away or explicitly replaced.
Identical content simply becomes the Local Copy again.

Recovery offers the current verified content, never a version picker. S3
VersionIds and Archive Version IDs are internal pins, not a history interface.
Each Vault has one Storage Plan (#421): only current-version transitions,
never expiration or noncurrent actions. Legacy expiration/noncurrent fields are
discarded during migration 0053, not exposed by the runtime. Identical effective
folder policies collapse; conflicting assignments/pins stop before DDL rather
than inventing a schedule. Missing defaults map to Standard. Catalog content and
trust are unchanged. Existing application-owned bucket rules are replaced with
exact Vault-prefix rules on lifecycle synchronization, not during migration;
external rules and the seven-day multipart abort are preserved. No retagging or
cloud deletion is required. Standard does not warm already cold content. The
legacy internal class-copy worker remains without a public per-file class-change
action. Vault-wide Move now (#429) uses immutable verified replacement and a
durable provenance ledger, not the legacy mutable placement update; see ADR-0021.

The owner accepts that mistaken SMB overwrites, corruption or ransomware can
replace the only good cloud copy after verification. Verification proves equality
with the uploaded local bytes, not that those bytes are desirable. There is no
previous-version retention window. Cold-storage early-deletion charges may apply.

## Considered Options

- Retaining user-facing history: extra choices and recovery complexity rejected
  by the owner in #362 in favor of one current recoverable copy.
- An unversioned bucket: cannot safely verify replacement before deleting the
  previous bytes, and splits provisioning/preflight assumptions.
- Updating one catalog row in place: loses exact immutable Job and integrity
  targets; internal identities remain necessary even without a history UI.
