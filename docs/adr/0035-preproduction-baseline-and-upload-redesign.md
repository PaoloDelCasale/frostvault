# Preproduction baseline and bounded per-file upload redesign

Status: accepted by the Owner during the 2026-11-12 design interview for #575,
#577 and #578. This decision amends ADR-0016, ADR-0017 and ADR-0034 only as
specified below; their safety and recovery guarantees otherwise remain in force.

## Compatibility and schema

The next release is a new, incompatible preproduction baseline, not an upgrade
of existing test installations. Consolidate the single Owner, current file state,
explicit Current Snapshot reference and upload evidence, using native PostgreSQL
types where appropriate. Retain immutable Archive Version identities and every
proof or reference needed by open work. Closed path history remains history, not
a second recoverable cloud snapshot. Simplifying tables must not erase the
meaning of reconnect suspension, root release or pending deletion consent.

Every entrypoint, including manual upgrade and metadata restore, must reject old,
unknown, divergent or unversioned nonempty catalogs before mutation. Never stamp
the new revision onto an existing catalog, reset a database automatically, or
interpret decryptability of an old backup as schema compatibility. A truly empty
database may be initialized. New-format metadata restore retains the existing
Reconnect, no-delete reconciliation, consent withdrawal and rehash guarantees.

No import/adoption of old Vault objects is part of this release. New Vaults use
new namespaces. Old objects remain untouched and recoverable using the old
release or external tools and the independently held Recovery Kit. No compatible
upgrade of an old catalog or metadata backup is promised.

## Cloud and execution

AWS S3 is the supported production provider. Test emulators do not certify
third-party endpoints. FrostVault is the sole writer of its managed keys;
external reads are permitted, and observed external anomalies still fail closed.
Keep one independently recoverable, readable S3 object per file: no packs.
Preserve the frozen original mtime evidence and recovery behavior from #573.

Batch replayable database work where useful, but not per-file authority. Pause
remains a per-file claim boundary: already claimed files may finish; a batch must
not preclaim another 50–200 files past that boundary. Cancellation, failures and
retry remain independent per file. Pause is not a revocation generation. Other
lifecycle generations cannot replace checks on live filesystem/root identity,
lease, cancellation or external effects that they cannot observe.

Keep committed intent/send-started before provider effects, immediate independent
receipt journaling, frozen expected integrity proof, exact ambiguous-send
reconciliation, verified replacement before cleanup, and completion only after
cleanup. DB retries never replay provider or filesystem effects. Immutable
evidence and outstanding obligations, not a growing redundant facts queue,
should drive remediation discovery.

Conditional PUT/CompleteMultipartUpload is not enabled without real AWS proof.
No live AWS test is authorized by this decision. HTTP 412 only demonstrates a
failed precondition, not authorship or a successful earlier send. ETag is not a
VersionId. Conditional writes alone cannot replace lineage, exact cleanup
inventory, multipart ownership or independent verification. Preserve the existing
safe protocol until the proposed replacement is demonstrated; leave that part of
#577 explicitly open rather than claiming completion.

## Operational retention

Default retention is 30 days for terminal operational history and read/resolved
notifications, and 90 days for audit, configurable and documented. Never expire
the keeper, live obligations, unresolved problems or evidence required by future
decisions. Purged versions and settled evidence may be removed only when closed
and no longer referenced; retention does not authorize any local/cloud content
deletion or relax proof requirements. Referenced closed history must be retained
until its final safety obligation ends.

## Acceptance and release

Measure small-file work against fixed v0.5.5 with repeatable isolated workloads:
completed-after-cleanup, at most 15 DB transactions and 120 SQL statements per
file, work not growing with accumulated history, and no significant throughput
regression. Report synthetic evidence as synthetic; 50–100 files/s on AWS is not
a promised or demonstrated result. Preserve all security, recovery, concurrency
and publication gates; port superseded migration fixtures to equivalent safety
contracts rather than dropping coverage to turn CI green.

Release v0.6.0 with an explicit compatibility warning and images tagged 0.6.0 and
0.6, but do not move latest from the old line. The publication workflow must
implement and test this distinction before the version tag is pushed. PRs may be
merged after applicable checks pass; release requires the final exact main SHA's
full gates. No deployment, service operation, real-data reset or production
benchmark is authorized. #564 requires field validation and cannot be closed on
synthetic results alone.

## Trade-offs

A clean baseline avoids carrying a multi-user/version-history shape into the
single-Owner/Current Snapshot model, at the explicit cost of old-catalog
compatibility. Independent objects and per-file claim semantics cost some
amortization but preserve understandable recovery and Pause. Conditional-write
optimizations are deferred rather than mistaking provider conflict responses for
proof. SQL/pipeline layout, module internals and measured concurrency tuning are
implementation choices, not further Owner decisions.
