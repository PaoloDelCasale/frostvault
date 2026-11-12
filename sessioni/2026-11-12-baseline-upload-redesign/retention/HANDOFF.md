# Operational retention — implementation handoff

## Scope and integration

Implementation commit: `a43d433a` (`feat(retention): bound DB-only pruning to non-evidentiary history`). Configuration docs and this handoff are in the following documentation commit.

Implements only the safe operational-retention subset of accepted ADR-0035 and #578/comments. No subagents, push, merge, issue mutation, service operation, Docker, AWS, live database or real `.env` edits.

The implementation worktree is `/mnt/cache/appdata/t3code-native/worktrees/frostvault/t3-e4e80d77-0c9396b961ab398f`.
Scratch and this handoff: `/mnt/cache/appdata/t3code-native/worktrees/frostvault/t3-e4e80d77-0c9396b961ab398f/sessioni/2026-11-12-baseline-upload-redesign/retention/`.

Read AGENTS, CONTEXT, full ADR-0035, #578 body/comments/native parent/dependencies (none found), related #575/#577 bodies/comments, and relevant ADR-0005/0016/0017/0019/0020/0024/0025/0029/0030/0031/0034. Inspected cleanup authority (`snapshot_cleanup._has_lineage`, `_keeper`, inventories/proofs), upload provenance/receipts/late acknowledgments, remediation schemas, notification episode/read/delivery transitions, audit and worker scheduling.

### New files

- `app/services/operational_retention.py`: explicit guarded SQL; fixed plans, bounded persistent high-water keyset cursors, row locks, reference checks, strict age cutoffs, DB-only deadlock/serialization retry including commit; statement/lock timeouts. Exported `CONSERVATIVE_EXCLUSIONS` and README describe exact subset.
- `migrations/versions/0076_operational_retention.py`: requested `down_revision = 0074_owner_singleton`; four cursor rows, partial eligibility and reference indexes. Metadata-only; no historical rows deleted by migration.
- `tests/test_operational_retention.py`: fake-clock, UTC/custom cutoffs, no-checkout validation, batch cap, cursor progress/wrap, commit deadlock replay, timeout rollback, notification/delivery serialization, DB-only SQL surface, periodic scheduler/disable and config hook.
- `tests/test_operational_retention_postgres.py`: actual PG16 fixtures/guarded-delete tests, mandatory `TEST_POSTGRES_URL` (never skipped).

### Minimal existing-file changes

- `app/config.py`: environment-only defaults operational history 30 days, audit 90 days, interval 60 seconds (0 disables), page size 100 (clamped 1–1000).
- `app/storage.py`: one maintenance callback and periodic submission, off Job claim path, existing no-overlap maintenance pool. Failure logs only fixed text, never SQL/DSNs/provider values.
- `README.md`, `.env.example`: configuration, strict final-state age, exact conservative exclusions, content/backups never affected.
- Three scheduler mock fixture updates set `operational_retention_interval_seconds=0` so their unrelated scan/refill/backlog contracts stay deterministic: `test_watcher_periodic_recovery`, `test_worker_completion_refill`, `test_small_upload_cost`.

**Parent integration required:** update `app/database.py:HEAD_SCHEMA_REVISION` to the final integrated revision/baseline. Intentionally did not touch parent-owned `app/database.py`, `tests/test_database.py`, CI, publishing, or Owner/file-state redesign. Rechain 0076 after the sibling migration as planned, then fold indexes/cursors into the final baseline. Port the new PG fixtures to the final native file/current-snapshot/evidence schema without weakening the tests or broadening deletion.

## Exact implemented eligibility

1. Notifications: read, ordinary or resolved problem (resolved_at mandatory for problems), with max(created_at, read_at, resolved_at) strictly older than operational cutoff. Unread/open/invalidly closed states stay. Parents with any delivery stay.
2. Notification deliveries: delivered/failed only, own updated_at old, parent eligible. Explicit bounded deletion, not cascade. Lock/skip parent before guarded DELETE to serialize with concurrent reopen/reset to pending; skipped parents cannot authorize deletion. Pending deliveries remain, including pending deliveries of resolved problems (conservative exclusion).
3. Audit: created_at strictly older than independent audit period; no actor rewriting (owner/emergency/system).
4. Jobs: **only cancelled uploads never prepared/sent**: no snapshot/digest, Archive Version, reconcile-only flag, live claim, pending terminal notification, original/bound intent, inventory, episode/link/discovery, deletion/move, audit or notification reference. Also exclude open per-file remediation/incident/reconciliation and open per-Vault problems. All completed/failed Jobs and all other actions remain.

Every category examines at most one page per tick, including excluded candidates, so old referenced rows cannot starve the next page. A finite indexed max(id) high water does not grow with concurrent insertion; wrapped sweeps revisit skips, late commits and state transitions. Guards run in a second READ COMMITTED statement after row locks. FK constraints stay enabled; no dynamic runtime schema introspection. Four transactions/tick, <= configured batch rows/category (plus corresponding notification parent locks), bounded statement/lock waits and up to existing eight DB-only retry attempts per page.

## Exact remaining gaps / proof dependencies

This does **not** implement full history garbage collection and must not be reported as such.

- **All upload intents, receipts and multipart journals remain**, even settled/closed. `_has_lineage` requires the frozen intent target/snapshot/inventory and exact receipt to attribute an old write, and `_keeper` reads original target scope. A settled attempt can remain independent authority for future cleanup; removing it merely because settled would lose proof (or change fallback behavior). No reliable settled-at clock exists for safe age accounting either.
- **All inventories and cleanup proofs remain**, including completed ones. Keeper scope and exact historical proof are still consulted in retry/delete-to-DB-gap/future cleanup decisions and can be referenced by episodes. JSON pins are not all relational references.
- **All Archive Versions remain**, including purged. There is no safe consolidated closure/reference accounting independent of Job/intent/local-conflict/move/incident/proof/serialized-inventory references. Retention does not infer a purge proof from availability alone.
- **All remediation history, originals/current/resolved links and discovery remain**, including cancelled episodes (cancelled does not prove no provider obligation).
- **All Jobs other than the narrow cancelled-unprepared subset remain.** Completed upload/rename Job expected digests are independent cleanup evidence; failed Jobs also support un-FK'd grouped-problem and future resolution queries. Prepared-but-unsent closed attempts remain until explicit closure/evidence redesign can prove all uses dead.
- Unread notifications/open problems/pending deliveries remain indefinitely. Terminal delivery rows need both own and parent final-state age. This deliberately retains too much rather than dropping proof.
- No Local Copies, path history, cloud content, backup artifacts or filesystem files are deleted. No evidence consolidation, provider conditional writes, retention API/UI, metrics or broader schema redesign is introduced.

## Validation

Python 3.12 `.venv` local; installed requirements and parser-only `pglast` in that venv, not system packages. No dependency manifest changes.

- 15 targeted DB-independent tests pass: `tests.test_operational_retention`, `tests.test_watcher_periodic_recovery`, `tests.test_small_upload_cost.BacklogObservationTests`.
- `compileall` for changed/new Python files: pass.
- 12 retention SQL plan statements parsed with PostgreSQL parser: pass (syntax only, not catalog semantics).
- `alembic heads`: one head `0076_operational_retention`.
- Offline upgrade/downgrade DDL generation and parsing: pass (14 upgrade, 13 downgrade PostgreSQL statements).
- `git diff --check`: pass.

**PG16 unavailable locally** (`initdb`, `pg_ctl`, server/client binaries absent; `TEST_POSTGRES_URL` unset). The command for the new PostgreSQL module was attempted: all 12 tests error in fixture setup solely because `TEST_POSTGRES_URL` is unset, before any database connection or SQL. They are not certified green and were not skipped. No live DB substitution, Docker, system package installation or skipped DB coverage. Parent must execute `tests.test_operational_retention_postgres` and affected worker/notification/migration/backup/security tests in PR CI, together with full CI and final baseline integration. No AWS/performance, full Python suite, UI/build or publication verification is claimed.
