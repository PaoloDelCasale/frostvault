# Fixed delayed cloud deletion and Stop Protection without local removal

Decision #366 Q4, implemented by #428, replaces ADR-0015's independent local
removal/cloud dispositions with Stop Protection. The Owner keeps local files
and chooses keep cloud or the same explicit seven-day Cloud-only deletion used
in the browser. The issue's no-deletion-for-files-present-on-server criterion
applies to both entry points: a local cloud-backed file blocks the delete choice,
not an invitation to silently free it first. Keeping cloud requires no recent
reauthentication; deleting does, together with the exact Vault name and reason.

## Consequences

Deletion consent captures exact provider VersionIds once, including retained
legacy versions/markers. New cloud content is never added later. Catalog
Cloud-only evidence is necessary but insufficient: a healthy trusted Source
Volume, enrolled root, no local entry (including symlinks/special files), confirmed
cloud observation and no concurrent work are independently required. A missing
mount is not absence. Typed confirmation cannot be satisfied by echoing a
caller-supplied generated phrase. The fixed seven-day delay has no environment
setting, per-Vault toggle or acceleration endpoint.

Admission uses the same Vault row lock as upload/recovery/cleanup admission.
Reconnect rejects pending Jobs. Watcher/full-scan observations cancel the entire
pending deletion group when a Local Copy reappears, even if it is later removed
again. The worker rechecks exact identities, current cloud content, physical
local absence and the elapsed delay before provider batches; ambiguous or stale
consent is revoked, never widened. Durable group leases and exact-version restart
reconciliation remain. Cancellation is guaranteed only before execution starts;
it never claims to undo completed/in-flight S3 deletion.

Stop Protection never removes a local file or directory. It excludes the Vault
from normal watchers, scans, automatic protection/release and Job admission.
Keeping cloud releases root allocation once application work is quiescent.
Stop+delete retains that allocation through the delay and deletion, preventing
another Vault from inheriting a destructive request's root. Cancellation means
keep cloud, not resume protection. Tombstone, cloud namespace, catalog, Jobs and
audit evidence remain; retained files are recovered with the Recovery Kit's manual
S3 instructions, not by reconnecting a released root. The historical
`decommission_state`/timestamps remain internal occupancy fencing; the old
service/table, local-removal workflow, API and texts are gone.

Migration 0059 cancels unstarted legacy delayed deletions rather than inheriting
shorter delays/old consent. Interrupted destructive work and unfinished legacy
decommissions must be reconciled with the old release first. It performs no
filesystem/provider operations. Downgrade refuses persisted Stop Protection.

## External-write limitation

FrostVault serializes application operations, not SMB or external S3 writers.
The last local-absence check cannot be atomically coupled to an S3 DeleteObjects
call. A host write after that check, or while a provider call is in flight, is
outside its locking boundary; earlier completed batches cannot be undone. Keep
sources quiescent around deliberate irreversible deletion. New provider versions
not explicitly captured are never targeted, including after stale metadata restore.
