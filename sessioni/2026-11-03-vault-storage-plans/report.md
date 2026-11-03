# #421 — One storage plan per Vault

- Base: latest `origin/main`, `6f4dd5429e74a9de5b502debf6cda8a288577502` (#420).
- Branch: `feat/421-vault-storage-plans`.
- PR: https://github.com/PaoloDelCasale/frostvault/pull/443 (Closes #421).
- Scope ends at #421. No subagents, merge, #422/#423/#429/#430 implementation or production operations.

## Implementation

- One `vaults.storage_plan_json`, edited via GET/PUT `/api/vault/lifecycle`, in Advanced. Standard; economical IA30/GlacierIR90; recommended tiered IA30/GlacierIR90/DeepArchive365; early Deep Archive at day 0 (next AWS lifecycle evaluation); custom ordered current-version transition chain.
- Italian/English timing, cost, small-object, minimum-billing, asynchronous transition and already-cold-content explanations. One Zone-IA single-zone caveat. Custom client/server validation uses the AWS waterfall and source-class residence, not absolute Glacier90/DeepArchive180 from Standard.
- One exact Vault-prefix lifecycle rule; preserve existing legacy namespaces, external rules, and #420 seven-day incomplete-multipart abort. Reject empty/overlapping namespaces before writing. Standard removes only application transition rules, never content and never warms cold objects.
- Removed folder assignments, pins/table/aggregate fields, policy UUID/tag reconciliation, public per-file/folder/whole-Vault manual class-change routes and UI, expired/noncurrent profile support and corresponding contracts/options/i18n/docs/tests/demo/capture scripts. No pending object retagging work remains. Existing cloud tags are inert and left untouched.
- Internal verified class-copy Job/catalog/worker/restart/abort mechanisms remain for existing Jobs and future #429; no new Vault-wide movement action is exposed.
- OpenAPI and TypeScript regenerated through repository scripts. Source/copy trust, exact-VersionId verification, sole-copy keeper checks and cloud-only conflict logic remain unchanged.

## Migration / deploy

`0053_vault_storage_plans` follows `0052_s3_sdk`; metadata only, no S3 calls, object moves or cloud deletion. Catalog identities/content, Integrity/Availability, Local Copies, Path History, Jobs, Sessions and notifications are preserved by populated contracts on both database backends (PostgreSQL only in CI).

Safe mapping: no default → Standard; identical effective folder policies collapse into that single plan; pins on Standard are redundant. Retired expiration and noncurrent actions are discarded by the explicit #418/#421 roadmap decision, never translated into deletion. Heterogeneous plans, pins conflicting with transitions, bad/missing/cross-Vault policy references, invalid chains, and unsafe namespaces fail before DDL. Resolve ambiguity deliberately in the previous release before retrying; no cloud content is changed by a refused upgrade. Unassigned policies are retired metadata only. Downgrade recreates an equivalent single default, not retired historical pin/folder/expiration promises.

Existing application-owned bucket rules are replaced on the next successful lifecycle sync, not by migration. Confirm synchronization before using an upgraded bucket. External expiration rules remain operator-owned and need review; existing cold objects stay cold and can still incur minimum-duration/retrieval charges. IAM requirements remain the existing lifecycle read/write permissions. No new ports, secrets, service configuration or restart commands applied.

## Verification

Only targeted local runs, never full Python, PostgreSQL, S3/MinIO, Playwright, frontend build or image build:

- 135 focused Python regressions across catalog audit/incidents, file/directory aggregation, cleanup/scan, adoption/relocation/readability and internal class-copy worker: passed.
- Plan/rule/API/populated migration tests, including source residence boundaries, day-0 transitions, sole-cloud-copy preservation, sync retry reporting, removed routes/options and fail-closed ambiguity: passed. Additional targeted namespace/Standard-mapping cases included.
- 33 focused Vitest tests (plan UI/validation, file actions, operation dispatch and demo) passed; 19 focused plan/file-browser tests passed after CI fixture correction.
- Frontend typecheck, targeted ESLint, Python compile checks, API regeneration and `git diff --check`: passed.
- Initial CI logs read with `gh run view --log-failed`: corrected stale tag-I/O assertions to exercise listing-I/O concurrency, public-action overflow expectations and a PostgreSQL boolean fixture/downgrade type. No checks bypassed.
- Authoritative full validation: PR #443 checks (SQLite shards, populated PostgreSQL migrations/concurrency, S3-compatible integrity, Node 22/24, desktop/mobile Playwright, frontend/image/backup/security). Mark ready only after every check on final HEAD is green; parent orchestrator alone reviews/merges.

Primary AWS sources checked during implementation:
- https://docs.aws.amazon.com/AmazonS3/latest/userguide/lifecycle-transition-general-considerations.html (waterfall, source residence, 128 KB default, asynchronous timing).
- https://docs.aws.amazon.com/AmazonS3/latest/API/API_Transition.html (`Days` accepts 0 or a positive integer).

Local logs and primary-source scratch material are in this same session folder, ignored by Git. No backup copies created and no secrets, backup shares, `/boot`, raw production databases, containers or live services touched.
