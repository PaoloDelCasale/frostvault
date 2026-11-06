# #458 — aggregate deadlocks: implementation / handoff

Baseline: `0baaaabee194c7be3362c396e2d9c0a85c7f7312`.
Dedicated checkout: `/mnt/cache/appdata/t3code-native/worktrees/frostvault/t3-674fc9f0`.

## Scope and constraints

- Preserve atomic, durable invalidation of the derived directory projection.
- Remove shared dirty-row upsert/delete lock cycles; consume immutable marks by identity.
- Recognize PostgreSQL `40P01` / `40001` via structured exception chains, never numeric message fragments.
- Retry bounded, complete **DB-only** transactions, never replay a provider PUT because a later DB unit aborted; retain its exact response in the current invocation.
- Preserve verified replacement / fail-closed Current Snapshot cleanup. Verified is not completed until cleanup succeeds.
- #461 follows this integration. No preflight UI, general recovery (#459), or persistent orphan ledger (#464).
- No production services/data/secrets, Docker operations, system packages, real S3/databases, or backup copies.

## Validation plan

- Local: targeted non-DB retry / SDK tests only (Python 3.12 virtualenv). PostgreSQL server binaries unavailable; DB tests must execute in PR CI, not silently skip.
- CI: 16 workers, >=1,000 nested files, actual `process_job` upload seam with concurrent aggregate maintenance; no failed jobs from deadlocks, final projection parity.
- Forced transactional failures at DB seams including after provider response: exact provider version retained, no extra PUT, transaction fully rolled back and replayed.
- Full PostgreSQL/Python/MinIO/frontend/build/security checks only in dedicated draft PR CI.

## Design / integration handoff

Migration `0061_aggregate_dirty_journal` extends the dirty primary key with `mark_id`, defaulting to PostgreSQL's full transaction identity plus server-start epoch (`pg_current_xact_id()` + `pg_postmaster_start_time()`). The epoch distinguishes imported signals from transaction IDs allocated on a different/restored cluster. A writer inserts an immutable signal per directory/transaction, coalescing repeats only within that transaction (`ON CONFLICT DO NOTHING`). It never updates a mark from another transaction. Catalog mutations and their signals still commit/roll back together; there is no best-effort post-commit invalidation window. Reused psycopg connections and savepoint/outer rollback cannot suppress the next mark: correctness no longer depends on the lifetime of Python tracker state.

Consumers snapshot exact `(vault_id, path, mark_id)` identities before rollup and delete only those identities. A late commit survives even if its timestamp predates the cutoff. The same rule applies to incremental refresh and full rebuild. Derived rollups still coalesce by directory. Maintenance retries its own complete DB transaction and clears uncommitted event/results before replay; caller-owned connections are never replayed.

`database_retry.retry_database_unit` retries only psycopg SQLSTATE `40P01` and `40001`, found through cycle-safe `__cause__`/`__context__` traversal, up to 8 total attempts with bounded jitter. Retry scopes own/close the complete transaction, including COMMIT, before replay. Upload digest preparation, record+link, verification persistence, post-delete catalog persistence, claim checkpoints, progress, status and retry-state writes use these scopes. Provider response/HEAD/digest and verification stream results live outside replay closures. No decorator wraps `process_upload`, SDK transfer, verification GET, or provider deletion.

`classify_upload_failure` distinguishes `database_transient` from provider `transient`. DB errors exhausted inline are never scheduled as a fresh upload; other psycopg errors (including ambiguous connection failures) fail closed rather than falling through S3 text heuristics. Numeric `500`/`503` substrings are removed. Actual HTTP 5xx statuses remain retryable via structured botocore `ClientError` metadata. Existing named transport heuristics remain for compatibility.

The Job completion contract is unchanged: verified evidence alone does not complete a Job. Exact-version Current Snapshot listing/unknown-content/keeper checks and cleanup still have to succeed. No safety gate or authentication/routing/API contract was removed. #461 can build on `process_job`'s existing provider-transient scheduler retry without consuming that budget for these inline DB retries.

## Evidence / outcome

Dedicated draft PR: https://github.com/PaoloDelCasale/frostvault/pull/466

Local red-capable classifier regression was run before the fix: 10 tests, 3 failures + 3 errors (numeric substring misclassification and missing structured exception handling). Captured in the session's ignored `retry-red.log`.

Local validation (no PostgreSQL/S3/server/build):

```text
.venv/bin/python -m unittest tests.test_database_retry tests.test_upload_retry_policy tests.test_s3_upload tests.test_ci_contracts.PullRequestCiContractTests tests.test_ci_contracts.ContributorCiDocsTests -v
.venv/bin/python -m compileall -q tests/test_aggregate_deadlocks.py app/storage.py migrations/versions/0061_aggregate_dirty_journal.py
git diff --check
```

The targeted command passes: **36 tests, OK**. Final CI run/check evidence will be added after final validation.

CI caught a batching regression in the first journal implementation (`test_100_file_batch_uses_constant_statement_count`: 109 statements vs maximum 10, run 42139748069 / shard 8). Fixed by coalescing ancestor directories inside each `mark_paths_dirty` invocation, retaining the existing constant-statement budget without trusting connection-lifetime tracker state. The original assertion was not weakened; a targeted non-DB SQL-recording regression also covers per-call coalescing and re-marks on a reused handle. `targeted-local.log`, pip log and downloaded CI logs remain ignored session artifacts, not committed test output.

PR CI explicitly runs `tests.test_aggregate_deadlocks` in the required **PostgreSQL migration and concurrency tests** job, not an optional/skip path or duplicated shard. Acceptance covers:

- 1,024 nested files / 16 real Job workers / concurrent aggregate maintenance; all Jobs completed and exact provider references retained; every semantic aggregate column matches a fresh full rebuild.
- Real PostgreSQL lock cycle making the post-response linkage transaction the deadlock victim, retried through actual `process_job`, exactly one external PUT.
- Forced server-side `40P01` and `40001` after record+link, plus deferred-trigger COMMIT abort; rolled-back records do not survive, exact provider response does.
- Post-response progress flush, status, retry-state, verification and cleanup DB aborts; no re-upload or repeated verification stream.
- Bounded exhaustion fails closed without automatic re-upload/deletion or false Job completion.
- Writer proceeds with a 250ms lock timeout while a consumer holds DELETE locks on an older mark for the same path.
- Old-timestamp late commits survive incremental and full consumers; eventual root counts/sizes converge.
- Connection rollback/reuse and populated 0060 → 0061 → 0060 → HEAD migration preserve pending paths.

## Risks / limitations / future deployment

- This is not a persistent upload-intent/orphan ledger. Process death, 8 consecutive DB aborts, ambiguous connection/COMMIT outcome, or non-DB failure before exact-version linkage can still leave uncatalogued provider content. That is #464; existing verified/unverified failed-Job recovery remains #459. Fail-closed exhaustion is explicit and tested, not a claim of orphan recovery.
- Provider transport failures in the existing pre-link/checksum window are not solved here. Retries introduced by this PR are strictly DB-only and do not replay external effects.
- The journal may temporarily hold multiple rows per directory during bursts (one per committing transaction rather than one globally). Maintenance drains exact identities in bounded DELETE batches. Actual production CPU/lock pressure was not measured; no production access was performed.
- Migration 0061 is metadata-only but requires repository-standard coordinated schema upgrade before running the new code. No migration/deploy/restart was performed. Downgrade coalesces pending signals per path without dropping paths.
- Stress uses a thread-safe fake provider with actual PostgreSQL/catalog/claim/Job seams, not a live S3 account. Existing full SDK and S3-compatible integrity checks remain required in PR CI. No local PostgreSQL binaries were available and no local DB tests were silently skipped or pointed at a real service.
- Do not merge this draft until every applicable check is successful on its final HEAD and the coordinator has reviewed it. No merge or batch/issue-decision edit was performed.

GitHub native relationships: #458 has no parent/blockers and blocks #461; #459/#464 have no native parent/dependencies. All bodies/comments read. #410 roadmap and ADR-0001/0016 inspected. GitHub token lacks `read:project`, so project fields cannot be inspected; no project/batch decisions changed.
