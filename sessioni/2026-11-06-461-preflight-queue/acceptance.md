# #461 — preflight queue and explicit upload retry: acceptance / handoff

## Delivery

- Dedicated draft PR: https://github.com/PaoloDelCasale/frostvault/pull/468 — **do not merge in this implementation lane**.
- Branch: `feat/s3-preflight-upload-retry`.
- Checkout: `/mnt/cache/appdata/t3code-native/worktrees/frostvault/t3-5b6fcb5a`.
- Session artifacts: `/mnt/cache/appdata/t3code-native/worktrees/frostvault/t3-5b6fcb5a/sessioni/2026-11-06-461-preflight-queue`.
- Initial integrated baseline: `0a65c09f9dae7a8484bc96b48716d3f4f518505e` (#458/#462).
- Integrated #463/main `411167546b13ba81e4d3a080dea674eb6bedae1c` by ordinary merge `4b0594b4fe71e1fb4735e2d5f858a6798c6d141d`; no rebase/force push. Deployment/probe files are identical to this main.
- Implementation SHA verified below: **`781a341307db85e7bfff6f1ce395aa8a045ccc81`**. This report is the subsequent documentation-only handoff. Its exact-HEAD CI must also be green before the parent merges; the final SHA/check snapshot is recorded on the PR and in the final implementation response, avoiding a self-referential commit hash here.

Read AGENTS.md, CONTEXT.md, issue-tracker instructions, ADR-0016/0017/0020, the coordinator's authorized plan, and full #461/#458/#459/#464/#410/#423/#424/#425/#430 including comments. Native #461 parent: none; blocked by #458, already closed/merged. GitHub Project fields unavailable because the existing token lacks `read:project`; no relationship or scope was invented. No subagents used.

## Behavior and safety

### Shared S3 preflight

`cloud_preflight_gate` persists one installation outcome/backoff, with a non-blocking PostgreSQL transaction advisory lock for single-flight across threads/processes. The lock is independent of Job/Vault admission locks and releases on connection loss; no durable in-progress lease can strand a restarted installation. Cached success lasts at most 60 seconds. Failures back off 5/10/20/40/60 seconds, capped at 60, including repeated IAM propagation failures. Invalid/future timestamps after clock rollback/restore trigger a new check rather than indefinite suspension.

A separate background task checks due blocked probes every second independently of operation-pool capacity, scans and notification delivery. Read-only preflight requests have 2-second connect/3-second read timeouts and one SDK attempt; ordinary transfer retry settings are unchanged. No cloud mutation is wrapped in a gate retry. A DB abort publishing preflight status suspends the Job rather than replaying provider work or failing the Job.

All handlers that require this preflight retain their waiting state/retry budget and release their scheduler claims when blocked. Purge claims are released as a complete unstarted group without widening exact deletion consent. Existing failed recover/free-space/cloud-purge Jobs are **not** retried. One installation notification and a shared banner expose safe remediation (including `s3:GetBucketPublicAccessBlock`); scan/policy/audit failures caused by that same gate do not produce duplicate per-Vault warnings. Local catalog scanning/browsing, health and notifications remain available.

### Historical failed upload retry

`POST /api/upload/retry-failed?vault_id=<id>` is explicitly per-Vault; omission of `vault_id` is explicitly global, never Session-selected scope. UI actions are in Vault settings and General. Authentication/CSRF remain required.

This action reuses the existing automatic-upload and catalog admission seams under the Vault admission lock. Only targets whose latest upload attempt failed, whose present regular Local Copy is stable and included, and whose enabled protection/root/Source Volume remain trusted get fresh Jobs. Suspended/disabled/stopped/reconnecting Vaults, changed/missing/replaced roots or copies, Cloud-only Conflicts, protected content, active operations and pending cloud deletion remain ineligible. Concurrent/repeated calls do not duplicate active Jobs. Failed attempt rows are never edited or retroactively completed; cancelled uploads and failed other operations are never relaunched. A catalogued unverified upload can reuse its recorded digest/exact provider VersionId without a new PUT, through the existing worker seam.

### Whole-Vault upload

The existing upload API accepts exactly an empty directory path (`{"path":"","is_directory":true}`), mapped to the catalog's existing `whole_vault` seam. Vault settings exposes the action. Absolute/traversal paths and empty paths for other operations remain rejected. Existing eligibility/active-Job fencing is retained.

OpenAPI response schemas, `frontend/openapi.json`, generated TypeScript and aliases were regenerated together. English/Italian UI and notification text were added. Operator documentation is in `docs/aws-s3-bucket.md`.

## Migration / deployment

**New migration: `0062_cloud_preflight_gate`**, following `0061_aggregate_dirty_journal`. It creates only the singleton cache table; no S3 calls, data-file mutations, Job history rewrites or trust changes. `HEAD_SCHEMA_REVISION` updated. A populated downgrade/upgrade test proves attempt history is unchanged. No migration was executed against production.

At a future separately authorized deploy, apply the normal metadata upgrade and distribute API + SPA together. Preserve #462's explicit proxy allowlists, #463's authorized-Host packaged readiness probe, and #458's append-only journal/DB-only retries. No live deploy/restart, IAM edits, real S3/DB access, system packages, production secrets, backup copies or `/boot` changes were performed.

## Actual local verification (targeted only)

Using the local Python 3.12 `.venv` and `frontend/npm ci`:

```text
.venv/bin/python -m unittest tests.test_readiness_probe tests.test_cloud_preflight_gate tests.test_s3_preflight tests.test_upload_retry_policy tests.test_database_retry tests.test_local_release.LocalReleasePolicyTests -v
50 tests — OK

.venv/bin/python -m unittest tests.test_openapi_contract -v
11 tests — OK

cd frontend && npx vitest run tests/upload-retry.test.tsx tests/app-essential.test.tsx tests/vault-settings-retention.test.tsx
3 files / 13 tests — passed

.venv/bin/python scripts/export_openapi.py frontend/openapi.json
cd frontend && npm run generate:api
Generated artifacts consistent (also checked by CI).

git diff --check origin/main
Clean.
```

No local full Python/DB/migration/MinIO/frontend/build/image/Playwright suite was run. Database coverage was not skipped: it ran in CI on disposable PostgreSQL 16. An initial existing pure preflight test needed its newly introduced DB boundary mocked; no local database connection succeeded or live database was used. The first draft CI failures (old preflight mocks, response contract/artifact drift) and a test fixture's invalid `presence='absent'` enum were corrected, not bypassed.

## Actual integrated CI evidence

On implementation SHA `781a341307db85e7bfff6f1ce395aa8a045ccc81`: **28/28 exact-HEAD checks SUCCESS**.

- CI: https://github.com/PaoloDelCasale/frostvault/actions/runs/42143499257 — SUCCESS.
- Security: https://github.com/PaoloDelCasale/frostvault/actions/runs/42143499251 — SUCCESS.
- #461 PostgreSQL tests: https://github.com/PaoloDelCasale/frostvault/actions/runs/42143499257/job/127651169495 — all **12** `PreflightQueueTests` passed: invalid IAM readiness/no retry consumption, automatic recovery, single-flight 16 workers with responsive catalog, fresh-process persisted backoff, capped repeated probes, safe errors, concurrent per-Vault/global idempotent retry, protected/conflicted/cancelled/other-operation exclusion, suspended/disabled/stopped/reconnecting Vaults, source/root changes, catalogued-unverified reuse without PUT, populated migration, whole-root/path/auth/CSRF.
- Integrated #458 PostgreSQL migration/concurrency check: https://github.com/PaoloDelCasale/frostvault/actions/runs/42143499257/job/127651169638 — **39 tests OK**, including 1024 nested uploads/16 workers plus maintenance; real deadlock/COMMIT/linkage/progress/deletion DB-only retry tests remain green.
- Integrated #463 packaged probe and PostgreSQL backup: https://github.com/PaoloDelCasale/frostvault/actions/runs/42143499257/job/127651169483 — SUCCESS; all 11 packaged HTTP/Host/failure tests passed under numeric unprivileged user/read-only image. The packaged-probe check and #458 stress job were preserved without edits.
- All twelve Python shards, PostgreSQL migrations, S3-compatible integrity, OpenAPI/TS checks, frontend lint/Vitest (Node 22/24), production SPA/image/backup, desktop/mobile Playwright, CodeQL, dependency audit, image/SBOM and secret scanning passed.

The machine-readable implementation check snapshot is `ci-implementation.json` in this session. Logs are session-local evidence, not production output. The final handoff commit receives its own full CI run and is not authorized for merge unless every applicable exact-HEAD check also succeeds.

## Files changed / inspected

Implementation files: `app/database.py`, `app/main.py`, `app/storage.py`, `app/services/cloud_preflight_gate.py`, `app/services/operation_policies.py`, `app/services/essential_interface.py`, `migrations/versions/0062_cloud_preflight_gate.py`.

Contracts/UI/docs: `app/openapi_schemas.json`, `app/locales/{en,it}.json`, `frontend/openapi.json`, `frontend/src/api/{openapi.generated,types,vault}.ts`, `frontend/src/App.tsx`, `frontend/src/components/UploadRetryActions.tsx`, `frontend/src/pages/general/GeneralPage.tsx`, `frontend/src/pages/vault-settings/VaultSettingsPanel.tsx`, `docs/aws-s3-bucket.md`.

Tests: `tests/test_cloud_preflight_gate.py`, `tests/test_preflight_queue.py`, updated `tests/test_{local_release,protection_notifications,s3_preflight}.py`, `frontend/tests/{app-essential,upload-retry}.test.tsx`. This acceptance document is the only new tracked session artifact.

Also inspected the existing catalog whole-Vault/admission/claim/reuse seams, notification episodes and health, Source Volume/root safeguards, migration fixtures and CI workflows. No edits to `app/catalog.py`, existing transfer/replacement integrity code, ADRs/domain, Dockerfile, Compose, proxy rules or #463 probe implementation.

## Limits and parent follow-up

- **#459/#464 remain open/out of scope**: no automatic orphan adoption/deletion, no crash/ambiguous-PUT recovery guarantee, no retrospective completion of already verified failed attempts. Unknown content is never granted deletion authority.
- A single configured installation bucket is the existing contract. The gate is not per-Vault cloud policy or a replacement for exact-VersionId worker checks.
- Reprobe due times are capped at 60 seconds; actual completion also depends on the one-second polling tick and provider/network response time. No real IAM propagation delay or live AWS/SMTP/OIDC test was performed. Provider probes are read-only; CI uses S3-compatible/fake seams as already documented.
- Source filesystem/S3 external writes remain outside PostgreSQL serialization; workers retain existing final eligibility/integrity fences. Retry skips scans currently holding the Vault's non-blocking local scan lock; a repeat action after the scan is safe.
- Parent must review and merge only after final exact-HEAD checks are green. Keep PR draft; this was the fourth/last authorized implementation. No fifth issue, production action or merge was started.
