# Issue #423 — watcher-driven automatic protection

## Scope and base

- Branch: `feat/auto-upload-watcher-queue`.
- Base verified after fetching origin/main: `fefc282448fa795307e4e9075ddf41d1ab8be9f0` (#444).
- Only #423 is authorized; no subagents, merge, or implementation of #424+.
- Read AGENTS.md, CONTEXT.md, issue-tracker instructions, #423/#410, native dependency #422 and downstream #430, siblings #424–#427, decisions #361/#366, ADRs 0001/0012/0014/0015/0016.
- Native relationships confirmed via GraphQL. Project metadata is unavailable with the current token (`read:project` missing); no credential changes attempted.

## Implementation plan

- Persist the start of observed content stability, retaining it across unchanged full scans and restart; reset it for edits.
- Periodically queue stable catalog observations independently of cloud validation/lifecycle, using existing Jobs, conflict eligibility, upload preflight, exact-VersionId verification, source-change and lease fences.
- Continue full scans as reconciliation, never as the prerequisite for watcher uploads.
- Default new Vaults to auto-upload; preserve existing intentional/off-by-default pauses during migration.
- Fixed automatic noise exclusions cannot be overridden by include globs; do not restore removed policy tags.
- Preserve cloud-sync health/error reporting without implementing the notification or state redesigns.
- Add deterministic simulated-clock tests and migration/default coverage, regenerate API artifacts for changed defaults, run targeted local tests and full PR CI.

## Validation / operational boundaries

Python 3.12 virtualenv created locally. PostgreSQL server/client tools are absent; no production DB, system package, Docker/service, secrets, backup-share or boot operations. Database-backed validation will run in CI. No backup copies created.

## Implemented

- `app/catalog.py`: durable `content_observed_at`, preserved on unchanged scans, reset for content changes; paused automatic uploads are not claimable and cannot starve explicit manual work. Historical migration fixtures retain pre-0054 write support.
- `app/storage.py`: independent ten-second auto-upload loop over eligible catalog observations; no filesystem walk or cloud call for admission. Watcher edits reset stability and cached fingerprints, including preserved size/mtime. Enrolled roots/Source Volumes are checked before work and before committing watcher/admission transactions. Full scans remain reconciliation and enqueue independently of lifecycle/preflight failures; existing health/error reporting is retained.
- Automatic Jobs retain origin, deduplication, retry budgets and existing conflict/sole-copy/lease fences. Execution checks pause, source metadata and stability again, including after cloud preflight/local hashing and before the first upload. Linked versions can finish verification while paused. Terminal failures/cancellations are not silently re-admitted for the same observation.
- `app/services/operation_policies.py`: auto-upload defaults on for new Vaults; fixed noise exclusions cannot be overridden by additional include rules. Existing explicit manual operations and additional filters are preserved.
- Migration `0054_watcher_auto_upload`: new nullable stability timestamp; policy database default becomes true. Existing rows are untouched; missing policy rows for existing Vaults are materialized as false, preserving their old implicit pause. No S3 calls, cloud deletion, or Archive Version trust/content changes.
- `app/main.py` starts the independent loop and documents the new request default. OpenAPI/TypeScript artifacts regenerated.
- Vault settings now expose the auto-upload switch; English/Italian/demo text and README explain exclusions, stability, pauses, rollout and safety.
- No policy-tag reconciliation, #424 states, #425 notification redesign, #426 cleanup changes, or later roadmap work.

## Validation results

Implementation commit `2703c5e5` passed **all 28 PR checks**:

- CI: https://github.com/PaoloDelCasale/frostvault/actions/runs/41616139064
- Repository security: https://github.com/PaoloDelCasale/frostvault/actions/runs/41616139164
- All twelve PostgreSQL Python shards, PostgreSQL migration/concurrency suite, MinIO integrity, frontend generation/typecheck/lint/Vitest (Node 22/24), frontend/image builds, production-image PostgreSQL backup, desktop/mobile Playwright, dependency reviews, CodeQL, gitleaks and image security checks passed.
- All 18 watcher/timer tests and the populated migration contract passed in CI. They prove five-minute simulated-clock copy-to-exact-VersionId verification without a full scan; lifecycle and validation failure independence; edit resets (including preserved size/mtime); pause/manual fairness; fixed noise; restart/reconciliation; offline/replaced roots and transactional rollback; source changes around preflight; terminal retry-budget preservation; and sole-cloud-copy/conflict safety.
- Earlier CI failures were read with `gh run view --log-failed` (completed runs) / completed job logs. Fixed batch SQL row comparison, historical-schema fixture writes, demo i18n parity, root enrollment in the automatic conflict fixture (with stronger setup assertions), and simulated-clock state isolation. No tests were removed, skipped or weakened.
- Local targeted Python: 10 tests passed (glob/stability/retention helpers, idle-timer error recovery, scan cleanup scheduling).
- Local targeted frontend: `npx vitest run tests/vault-access-policy.test.tsx tests/shared-i18n-parity.test.ts` — 6 tests passed.
- Python compile checks and `git diff --check` passed. No complete local Python/PostgreSQL/MinIO/Playwright/frontend/image builds were run.
- The final report-only commit will be pushed and its checks verified before marking the PR ready. No merge is performed by this implementation agent.

## Deployment / risks / handoff

- PR: https://github.com/PaoloDelCasale/frostvault/pull/445 (`Closes #423`). Branch: `feat/auto-upload-watcher-queue`.
- Deployment requires the ordinary application restart/redeploy plus migration 0054; none was performed on the homelab.
- Existing paused/default-manual Vaults stay paused; the owner must explicitly enable them. New Vaults auto-upload by default. Local cleanup defaults are unchanged.
- The one-hour target is bandwidth/provider-dependent, not a transfer-time SLA. Watcher-disabled/missed events are recovered by full scans.
- Fixed noise remains cataloged but is never automatically uploaded; explicit manual uploads remain possible. Existing pending cloud-only holds remain fail-closed until reconciled/resolved.
- Cloud-sync/preflight failures remain observable through existing health/errors/Jobs; redesigned notifications and protection states are deliberately deferred.
- No production operations, credentials, backup shares, boot files, raw production DB directories, or backup copies were touched.
- Raw CI logs are ignored temporary artifacts in this same session folder, not backup copies.
- Stop here after parent review/merge of #423. Do not start #424 or any later issue.
