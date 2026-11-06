# #459 — handoff implementation, draft / partial acceptance

PR: https://github.com/PaoloDelCasale/frostvault/pull/471
Branch: feat/459-upload-remediation
Worktree: /mnt/cache/appdata/t3code-native/worktrees/frostvault/t3-1b580cbb
Base: c7a3f24b061a1c7304f50236ba0ea95d7e8f5361 (#460/#464 merged).
Coordinator owns independent review and squash merge. DO NOT merge this draft merely because CI is green: the remaining acceptance gaps below are explicit.

## Implemented coherent slices

- Metadata-only migration0065, HEAD_SCHEMA_REVISION, exact target ledger, immutable original/remediation mappings with scoped FKs and unique AV/intent targets. One metadata historical discovery pass during upgrade; ticks do not scan the original Jobs history.
- Terminal materialized upload failures append to an indexed durable inbox. Separate DB-only executor/loop, immediate startup then10s cadence, round-robin Vault cursors, bounded10Vault x10target batches and1s time slice per discovery/admission phase; SQL lock/statement timeouts. No filesystem/provider work in sweep/admission. Historical discovery is initialized as metadata, not inferred verification.
- Common target admission used by explicit reconciliation, ordinary eligible upload interception, watcher admission and bounded bulk retry. Vault-first locking plus unchanged jobs_one_active_file_uq cover all active operations. Reconcile-only Jobs freeze expected digest/snapshot/mode and cannot enter PUT. Target-level budget/deadline/block/cancellation persists across attempts; failed remediation is never rediscovered as an original. Explicit retries retain consumed budget.
- Existing0064 upload intents, receipts and exact cleanup authority reused. snapshot_cleanup.cleanup now returns the exact inventory owner Job ID: #464 can reuse ORIGINAL inventory across remediation attempts. The ledger records that pointer under claim before completion. Resolution is in the authoritative completed transaction and requires that exact completed cleanup checkpoint, linked AV and settled intent; original error/status/dates/group remain untouched.
- Claim/target/root fences apply through upload_provenance.target, including immutable ledger/job evidence checks. Missing Source Volume suspends the same admitted Job/target; a single missing file on trustworthy storage can complete A. Lifecycle, conflict and active-operation admission fences remain.
- Notifications no longer resolve A from newer queued/failed/successful B; late failed-notification materialization consults demonstrable completed resolution. Read is separate. Group failed_count remains historical, with additional resolved_failed_count/unresolved_failed_count.
- Targeted scoped GET /api/jobs/{id}, paginated GET /api/files/jobs and bidirectional links beyond recent50. FileVersions/API/UI expose separate remediation phase/state and current Local Copy protection. Exact reconciliation optionally takes a scoped remediation_id (not arbitrary AV/delete authority), important when multiple historical targets share a file. FileDetails history and linked-job queries invalidate on catalog events. EN/IT messages and OpenAPI/generated TS updated.
- Safe queue/state/lag/last-tick metrics, without file/path/provider identifiers as labels.

## Validation evidence

- Local:40 targeted nonDB tests in tests.test_upload_snapshot + tests.test_upload_provenance PASS. FileDetails Vitest6 tests PASS (including cleanup-versus-Local-Copy separation and paginated targeted lookup). Python syntax and git diff --check PASS. OpenAPI export/generation performed, no local full lint/build.
- No local PG16; no database/migration test was run against local/live services and no skips are counted as coverage.
- CI for prior candidate22a3ac7cc4c8d44ac8fc9c93414c3ba663574665: https://github.com/PaoloDelCasale/frostvault/actions/runs/42223153116 . At13:12UTC all12 disposable PostgreSQL Python shards, frontend generation/typecheck/lint/Vitest/build, both Playwright viewports, MinIO integrity and production-image backup checks SUCCESS; migration/concurrency job still running. Security run42223153119 is separate. This is NOT evidence for final HEAD; the final push launches new runs.
- Added13 real admission/claim/process_job/notification/API PG16 tests, green in the prior candidate shards, plus populated0064->0065 migration contract in dedicated migration job. Final candidate adds a14th actual blocked-provider-I/O discovery/admission test and targeted-remediation payload changes, which await its own CI.
- First CI defects fixed: original inventory checkpoint linkage; old notification test incorrectly equating retry with resolution; regenerated OpenAPI after endpoint docstring changes. Guards were not relaxed.

## Remaining acceptance gaps / review priorities

1. Discovery currently anchors materialized FAILED originals (linked AV or send-started intent). A historically unverified AV with independent durable baseline but **no failed originating Job**, or a scan-created cleanup block whose keeper's original Job completed, is not automatically enrolled. #464's explicit manual claimed cleanup still works and is tracked, but automatic discovery of these independent provenance-owned obligations must be completed without synthesizing ownership or guessing latest/path/size. Version-only independent proof is not by itself deletion authority.
2. Cadence is bounded10s, historical metadata discovery is seeded in DDL and lag is honest, but a large admission inbox can take multiple ticks to become ledger/Job admission. Do not claim every target starts I/O or every arbitrarily large backlog is admitted within60s. Coordinator must review exact approved discovery/admission interpretation and throughput/fairness evidence. Dedicated blocked-I/O test is added; explicit multi-Vault backlog rotation/time-slice tests are still missing.
3. Expand #459-specific faults around verified->cleanup->completion COMMIT ambiguity, direct lifecycle cancellation/restart, exhaustion with process restart, superseded A and distinct orphan remedies. Existing #464/#458 strict provider and cleanup/deadlock fixtures are preserved and continue covering many lower-level fences, but they do not replace every requested #459 ledger/projection assertion.
4. Contradictory original evidence pointing at an already-resolved AV remains unlinked/open; it cannot reopen the resolved ledger. A durable targeted rejection projection could make that historical corruption clearer without associating B with A's resolution. Invalid intent target JSON also warrants a durable fail-closed discovery rejection rather than repeated sweep errors.
5. Review cancellation/suspension synchronization and ordinary B admission behind cancelled/superseded A. Current behavior is conservative/fail-closed; no automation policy grants resurrection. Owner-facing remedy wording for bounded reason codes could be more specific.
6. Final HEAD must pass all applicable checks, including the new populated migration test and actual blocked-I/O fixture, and receive coordinator's independent safety/spec review before draft promotion/merge. No successful prior run may be substituted.

## Safety / environment

No production DB/S3/Docker/IAM/secrets, package installation, backup, /boot, deployment or restart. No backup copies. Local tools used existing read-only Python/npm dependencies from the prior #464 checkout through untracked dependency symlinks; no packages were installed or modified. Notes only in this session folder. No subagents, extra branch/worktree/issue or merge.
