# Vault-wide colder-only movement with durable copy provenance

Decision #366 Q5/Q5ter, implemented by #429, adds Move now only for an entire
Vault and only to a colder class on its saved Storage Plan. It creates per-file
background Jobs, not a per-file class-change API or a new expiration policy.
Mixed classes are filtered per exact Current Snapshot; equal/colder, unverified,
unavailable, busy and unrestored cold sources are reported as skipped. Standard
has no target. GLACIER sources need an explicitly restored exact VersionId before
copying to DEEP_ARCHIVE; this action never requests RestoreObject or warms content.
Preview/admission observe the exact cold source's HEAD, recognizing an explicit
external restore even with a Local Copy present, rather than trusting a cached
restore flag or treating expired/unreachable restores as eligible.

## Consequences

Migration 0060 adds a metadata-only copy ledger. Unlike the legacy internal
class-copy seam, the new path never mutates an Archive Version's provider identity:
it records a new immutable destination, verifies it against the captured verified
plaintext digest, then removes only the captured source VersionId. The original
local release verification clock and Cloud-only Conflict holds survive
representation-only movement. Confirmation is bound to the displayed Vault,
not a different Session selection in another tab; a changed file/path identity
or revoked source verification also fences publication. Unknown
extra versions/markers, changed current content, failed listing, lost claim or
failed checksum never authorize deletion. Existing sole copies remain intact.

A durable opaque metadata token preserves copy provenance across the provider
response/database crash gap. Restart requeues an expired Job and reconciles exact
versions before issuing another copy; a completed copy is reused, not guessed
from class, ETag or size. Multiple token matches, concurrent writes or ambiguous
provider postconditions are held for explicit reconciliation. Cancellation stops
at checkpoints, retains source/new copies, and never promises to reverse an
in-flight/completed replacement. Cancellation/lease and Vault row locks fence the
single source DeleteObject call. Reconnect and Stop Protection reject active Jobs;
pending delayed deletion excludes the same Vault File. External S3 writers are
outside these locks and must be kept quiescent for deliberate class movement.

Copies up to 5 GiB use CopyObject with exact source VersionId, copied tags and
preserved metadata/headers plus the intent token. Larger copies reuse multipart
UploadPartCopy, pinning every source part, preserving tags/metadata and aborting
on cancellation/failure. AWS SHA256 multipart checksums are COMPOSITE, not
FULL_OBJECT: the move computes the expected part-digest tree while independently
hashing the entire exact source against the verified plaintext digest. The new
cold destination must match that proof or an independently available full SHA256;
providers without suitable checksum metadata may require exact GET readback.
Nonconforming composite metadata is not accepted: only a complete exact-VersionId
GET/SHA256 can substitute, still compared to the captured verified plaintext digest.
If a new cold object cannot be read/verified, all copies remain; the move fails
closed rather than silently restoring it or lowering verification standards.
Incomplete multipart uploads after process death expire by the existing seven-day
abort lifecycle rule. Metadata/header/tag preservation does not grant integrity.

Cost estimates show bundled regional monthly storage and source retrieval list
prices, date and explicit exclusions (requests, minimum billable object sizes,
archive metadata, temporary restored storage, early-deletion fees). They are not
bills or all-in quotes. Confirmation acknowledges source early-removal charges,
target minimum billing (IA 30, Glacier IR/Flexible 90, Deep Archive 180 days), cold
recovery delay and the restarted provider object-age/lifecycle clock. Explicit
copies include small files; the lifecycle default 128 KB filter does not apply.

Downgrade requires completing or cancelling pending/failed moves before discarding
the ledger. Cancellation does not delete retained extra copies; inspect/reconcile
those with the current release before downgrading. No schema migration contacts S3.
