# Epic #410 — final integrated acceptance and deployment handoff

## Verdict

**can-close-epic: NO at this handoff.** The functional #366 Q18 criteria below
pass, but the exact-main overall check rollup is still FAILURE: image publication
cannot read GitHub check runs (HTTP 500). One infrastructure-only retry also
failed. One integrated protection-blocking defect was found and fixed below;
no failing test was accepted and no gate was relaxed. Coordinator sign-off/merge and disposition remain
required; **#410 stays OPEN**, with its checklist unchanged by this session.
This is not authorization to deploy, nor a request to start a fifth session.

## Fixed scope and direct evidence

- Audited merged main: **`92a06736e1eab6536b448da8b47e8375f1bee827`**.
- Reviewed [PR #455](https://github.com/PaoloDelCasale/frostvault/pull/455) HEAD:
  `c433e8c50d3ee786ecbc45d5bec10a71999a2730`, **28/28 COMPLETED/SUCCESS**,
  queried directly, not inferred from issue comments.
- Both Git trees: **`ecbae36ff4097bd6105897d6cf845b7f49db7743`**;
  `git diff --exit-code <main-sha> <reviewed-head>` succeeds. The clean prepared
  checkout equalled fetched `origin/main`. Dedicated branch
  `docs/epic-410-verification` adds this audit, a minimal restore-consent fix with
  two regressions and operator-doc corrections/restore clarification. The fix is **not yet on
  main**; final audit-PR CI validates the resulting candidate separately.
- [Main CI](https://github.com/PaoloDelCasale/frostvault/actions/runs/42061029518)
  and [strict main security](https://github.com/PaoloDelCasale/frostvault/actions/runs/42061029458):
  completed SUCCESS. All **27 CI/security check contexts** succeeded on this SHA.
  PR #455's count is 28 because it also has the additional CodeQL result context.
- [Main publication](https://github.com/PaoloDelCasale/frostvault/actions/runs/42061029635),
  attempts 1 and 2: `Require commit checks` failed on the GitHub REST check-runs
  request, **before** build/push; downstream publication SKIPPED. GraphQL exposes
  the green CI/security results, but this audit does not substitute those for the
  publisher's required gate or claim an image was published. No further retries,
  dispatches, release-script changes or deploys were made.
- [Audit PR #456](https://github.com/PaoloDelCasale/frostvault/pull/456): draft,
  opened early. Its final exact-HEAD check count/SHA are attested in the PR after
  the last commit, avoiding a self-referential commit/check claim in this file.
- `evidence.json` records timestamped identity, main contexts, native issue state,
  selected executed regressions, local test counts and inventory. Raw public
  source snapshots/logs are ignored scratch in this directory, not backup copies.

## #366 Q18 gates

| Gate | Finding and personally checked evidence |
| --- | --- |
| Sole-copy safety | **PASS within the documented locking/provider boundary.** Inspected actual scan/upload/rename/recovery/release/deletion/move code and regression assertions, not only ticket status. Matrix below. |
| Remove code/API/text/docs/tests together | **PASS after documentation corrections.** Inspected current OpenAPI, Settings, both locale catalogs, SPA routes, build dependencies and removal contracts. No active removed option found; intentional fences listed below. |
| Green PostgreSQL and S3 CI | **PASS for application CI/security; operational publication BLOCKED.** Main's 12 PostgreSQL shards, migration/concurrency, S3-compatible job, frontend, browser fixtures, image-backup and security jobs succeeded. Main overall remains red for the external API failure described above. Final audit PR must independently have every applicable check SUCCESS at its exact final HEAD. |
| Indicative halving | **Informational, not an acceptance condition.** Same-method counts below; no count-driven refactoring. |

## Integrated source/test matrix

All paths refer to the audited [merged tree](https://github.com/PaoloDelCasale/frostvault/tree/92a06736e1eab6536b448da8b47e8375f1bee827).

| Area | Source inspected and concrete evidence |
| --- | --- |
| #408 stale catalog | `app/storage.py:scan_cloud`, `_purge_current_snapshot_scan_extras`: complete listing, newest unambiguous verified keeper physically present, no active Job; discovery does not grant trust. `test_snapshot_scan_stale_catalog` covers sole newer copy, absent/older/mismatched keeper, equal timestamps and failed re-listing. The stale-keeper test executed successfully on main. |
| #409 recovered-file clock | `process_recover` and interrupted-recovery reconciliation persist explicit `recovered_at`; `local_cleanup_is_due` takes the latest verification/content observation/mtime/recovery. `test_recovery_verification.test_recovered_file_with_original_mtime_is_not_freed_again` executed successfully: old mtime preserved, complete new period. |
| #411 Cloud-only Conflict | `app/catalog.py:observe_local_copy`, `catalog_incidents`, upload admission and `process_upload` retain the sole-copy hold. `test_cloud_only_conflict` asserts zero deletion/upload for differing content, no upload for identical bytes, rename-away preservation, explicit replacement then verification-before-purge. |
| SDK / Current Snapshot | `s3_upload.py`, `process_upload`, `_stream_plaintext_for_verification`, `upload_retry.py`, `s3_copy.py`: response VersionId, CRC32 FULL_OBJECT transport proof, exact-version SHA-256 readback against durable upload digest; linked retry verifies rather than reuploads; later local edits are not falsely certified. Shared bandwidth, throttled progress, botocore standard retry and eight-attempt Job classification remain. Managed multipart cancellation explicitly aborts incomplete uploads and never deletes completed content; lifecycle reaps unfinished parts at seven days. `test_s3_upload`, `test_upload_verification`, `test_upload_retry_policy`, `test_current_snapshot` and dedicated S3 roundtrips cover success/failure boundaries. |
| Rename / SMB absence | `purge_current_snapshot_superseded`, rename verification and automatic matching preserve stable identity; a failed/ambiguous rename cannot authorize old-key deletion. `test_essential_file_actions` asserts verified new key before old-key cleanup, ambiguity as a new file, missing keeper/unknown content/listing failures retaining copies. Local absence is not cloud-delete consent. |
| Five-state health and release | `protection_states.py` distinguishes pending/protected/problem/unconfirmed/cloud_only; failed source/cloud/audit observability overlays unconfirmed without erasing prior evidence. Labels are not deletion authority. `operation_policies.py` and `process_free_space` independently require trusted source/root, current verified+available digest match, exact-VersionId HEAD/size, claimed-file rehash and final policy/claim recheck. Default OFF, only 90/180/365/730, no upload filters on release; manual release still requires Protected evidence, even for cold copies. `test_protection_states` and `test_local_release` exercise these gates. |
| Watcher / notifications | `queue_vault_auto_uploads`, independent upload loop and `scan_vault` separate durable local admission from cloud/lifecycle errors; the worker still preflights. Defaults/exclusions follow #423's explicit fixed-exclusion contract. `protection_notifications.py`, fixed notification routing and tests cover 2h stall/24h ceiling, deduplication, source/cloud blockage, conflict/missing copy, recovery outcomes and weekly summary. SMTP delivery is not a live-service claim. |
| Reconnect / kit / old backup | `vault_relocation.py`, reconnect branch of `scan_vault`, `_verify_reconnected_cloud`, `recovery_kit.py`: per-Vault explicit mount acceptance, cloud-first **no cleanup**, full local rehash, unknown cloud exact-version read, incomplete/cold unreadable recovery stays suspended. Restore registration invalidates stale local authority and nonterminal Jobs before workers. `test_vault_reconnect` executes the stale A/new B scenario with Protected/Cloud-only/Conflict and **zero provider deletes**. Kit is catalog/credential-independent except its intended metadata key; one fingerprint-bound custody; no-store download requires reauthentication. |
| Seven-day deletion / Stop Protection | `cloud_deletion.py`, `protection_stop.py`, `process_cloud_purge`: server-computed fixed delay, exact-name/reason and HTTP step-up, immutable captured VersionIds, physical absence through trusted root, group cancellation on reappearance, worker rechecks/leases. Stop never removes local files; delete rejects server-present cloud-backed files. Stop+delete retains root allocation; cancellation retains cloud, never resumes protection. `test_cloud_deletion`, safety, HTTP and lease suites cover pre-deadline cancellation, corrupted deadlines, exact deletion, reappearance/mount failure, partial batches and restart. |
| Colder-only movement interplay | `vault_moves.py` and `s3_copy.py`: targets only on saved plan and strictly colder, exact cold-source restore observed (never requested implicitly), durable opaque token/provenance, new immutable digest-verified destination before deleting **only captured source**. Release verification clock and conflict hold move to the new representation. Deletion excludes busy moves; moves exclude pending deletion; Stop/reconnect reject active Jobs. `test_vault_moves` executes crash windows, cancellation, lost identity, cloud race, listing failure, source preservation, **conflict hold surviving movement**, and admission interplay. |
| Essential actual UI flows | Read `App.tsx`, `FileBrowser`, `FileDetails`, `RecoverSelection`, General, settings, Move/Stop dialogs and `api/vault.ts`. Immutable explicit `vault_id` survives CSRF/reauth retry; keys include scope; changing folders remounts selection/dialogs. Missing/stopped explicit Vault is 404, never Session fallback. Global duplicate paths retain identity. Details exposes deliberate conflict replacement/deletion; recovery estimates cold descendants with Bulk default and states return/retention. General exposes kit/custody/backup/reconnect/email/spend/create. `test_essential_interface[_postgres]`, `vault.test.ts` and Playwright `02-flows.spec.ts` exercise real API/browser fixture flows at desktop/mobile sizes. |

## Removal audit and deliberate retained fences

Checked runtime/SPA/OpenAPI/env/it+en for retired multiuser/Invite/Source Area,
mutable OIDC/settings, quotas/windows/price books, webhook/push/preferences,
PWA/offline, filesystem/admin dashboards, Hide/manual rename/history, history
policy/picker, Vault crypt, rclone, folder lifecycle pins/expiration, SQLite,
legacy release/decommission and per-file class-change surfaces. Removal tests
include `test_single_owner_http`, `test_essential_administration`,
`test_essential_file_actions`, `test_postgres_configuration`, migration contracts
and browser retirement tests. API/Settings/locale-prefix survey passed (61 current
HTTP paths); targeted tracked-source/docs searches corroborate it.

Not dead options: historical linear migrations/superseded ADRs; internal immutable
Archive Version/Path History/Delete Marker identities; terminal legacy Job origins,
`decommission_state` occupancy and `relocation_state` reconnect fencing; old `/sw.js`
**unregister-only** script and one-way cache removal; sensitive-key redaction;
encrypted **metadata** backups; internal class-copy worker and scoped S3 test-cleanup
tooling. No production rclone binary/Go build or offline registration remains.

**Documentation fixes:** `docs/ci.md` incorrectly advised full local runs and
PostgreSQL skips; it now follows #410's targeted-local/PR-CI policy. README's stale
“username and password required” sentence now states OIDC or LAN Emergency Password.
The metadata-restore guide and embedded kit instructions explain retirement of
unfinished deletion targets while retaining completed deletion evidence.

### Integrated defect found and fixed in this audit

Restoring a backup with delayed deletion cancelled its Jobs but left item-level
`cloud_deletion_items.status='pending'`. `queue_auto_uploads` independently excludes
those items: after reconnect and a later local edit, protection could remain
silently starved. The original source scan/reconnect tests mocked automatic
admission and did not expose this cross-feature interaction.

`record_metadata_restore` now retires pending/failed targets for noncompleted
cloud-purge Jobs **before** cancelling Jobs. Deleted evidence, completed workflows
and cloud bytes are retained; this is metadata-only revocation, not new deletion
consent. A DB-free regression went red on the missing mutation, then the ten-case
kit module passed. A PostgreSQL regression in `test_cloud_deletion` covers partial
prior deletion, restore cancellation, matching reconnect observations, later edit
and actual automatic enqueue, plus retained current cloud evidence. Its execution
belongs to final PR CI. No API/schema change or OpenAPI/TS regeneration is needed.

## Comparable inventory (nonbinding)

Reproduce with `.venv/bin/python sessioni/2026-11-05-410-epic-verification/inventory.py`.
Baseline `aaebe1a817b1f9c57fc2f423f5681806027b9c8f` is main immediately before
#408's merge, not a selectively chosen earlier feature baseline. Literal lines,
including comments/blanks; tracked `app`/`tests` Python; frontend `src` TS/TSX/CSS/JSON,
excluding generated OpenAPI/types. HTTP operations counted by the same AST decorator
method on both commits; not unique paths or a line-grep estimate.

| Measure | Before | Final main |
| --- | ---: | ---: |
| app root Python | 19,588 | 14,488 |
| app/services Python | 19,198 | 11,640 |
| frontend src (non-generated) | 28,873 | 8,841 |
| Product total, above three groups | 67,659 | 34,969 (**−48.3%**) |
| tests Python | 48,965 | 35,016 |
| HTTP method/path operations | 130 | 66 (**−49.2%**) |
| Distinct decorated HTTP paths | 117 | 61 |

These are comparable measurements, not exactly #366's approximate frontend
count, code-only LOC, complexity metrics or a proof that every possible option
is absent. The indicative goal does not justify removing safety tests.

## Future authorized upgrade / deploy handoff

1. **Do not deploy from this audit.** First restore exact-commit publication/check
   API health and obtain the coordinator's explicit disposition. Never bypass
   `Require commit checks` or promote an older image as this audited tree.
2. Follow `README.md`, `docs/metadata-backups.md`, `docs/aws-s3-bucket.md`, ADRs
   0016–0022 and the [prior epic upgrade notes](https://github.com/PaoloDelCasale/frostvault/issues/410#issuecomment-6603583071).
   Review existing revision/data with the matching old release before upgrade;
   migrations are not permission to repair live data blindly.
3. PostgreSQL **16** only, matching clients and linear migration head
   `0060_vault_class_moves`; no automatic SQLite conversion. Recover legacy crypt
   Vaults with the old release/material before 0051 (fails before DDL); no ciphertext
   reinterpretation. Preserve independently custodied old metadata keys/artifacts.
4. 0044–0049 remove multiuser/managed configuration, channels/offline/admin/file
   actions. Move necessary old `system_settings`/SMTP/OIDC values to protected
   deployment environment before removal. Vaults are UI-created; Source Volume
   identity and non-overlap remain. 0050 retains only Current Snapshot. 0052 removes
   rclone alias/config/binary; remove retired rclone env/bind mount. 0053 requires
   deliberate resolution of conflicting legacy folder plans/pins, then confirm
   successful lifecycle sync (external expiration is preserved, operator-owned).
5. 0054 preserves deliberately suspended Vaults; 0055/0056 add state/problem data
   and retire routine notification history. **0057: first reconcile interrupted
   `cleaning` Jobs and complete legacy local-removal decommissions with the old
   release.** All old release rules become OFF and nonterminal release Jobs are
   cancelled. Remove `ALLOW_LOCAL_DELETE` and old cleanup fields; explicitly
   enable the new fixed-period rule only after review.
6. 0058 adds per-Vault reconnect acceptance, kit custody and restore warning. For
   metadata restore: application stopped, new isolated restored database, correct
   DSN (unset/replace overriding `DATABASE_URL`), supported migration, **register
   manifest before any worker**, then explicit reconnect for each Vault. Unknown
   newer cold copies may require external S3 Restore before reconciliation can
   finish. Stopped folders stay stopped; fallback S3 recovery is files-only.
7. **0059:** finish all unfinished legacy decommissions and reconcile interrupted
   destructive cloud Jobs before migration; unstarted old requests are cancelled,
   not inherited consent. Remove `CLOUD_PURGE_DELAY_SECONDS` and old cloud-enable
   configuration. Seven days cannot be shortened. Migration makes no provider or
   filesystem changes; persisted Stop Protection prevents downgrade.
8. **0060:** metadata-only move ledger, no content movement during migration.
   Complete/cancel and explicitly reconcile retained extra copies before downgrade;
   do not discard provenance of a pending/failed move. Representation-only moves
   preserve release clocks/conflict holds; provider object age restarts.
9. Bucket versioning plus all four BPA flags and appropriate IAM (including
   `GetBucketPublicAccessBlock`, multipart/abort, exact-version reads/copies/deletes,
   lifecycle and explicit restore where needed) are prerequisites, not changes made
   here. Verify the seven-day incomplete-upload abort and transition-only application
   rules; inspect external expiration separately. Keep external SMB/S3 writers
   quiescent around deliberate irreversible deletion/movement: application locks
   cannot atomically lock external writers or undo completed provider calls.
10. **Ship API and SPA together.** Configure canonical `APP_PUBLIC_URL` for absolute
    SMTP links; never derive it from request Host. Without it email still works but
    has no absolute application link. Keep Host/proxy/CSRF/LAN Emergency Password
    boundaries. Optional SMTP/OIDC connectivity and real AWS behavior require a
    separately authorized deployment validation, not credentials/data access here.

## Verification commands, coverage and limits

- Read `AGENTS.md`, domain/issue-tracker docs, CONTEXT and applicable ADRs; fetched
  #360–366, full #410/comments and all 22 children/comments. GraphQL native parents,
  children and dependencies agree: **22 CLOSED, 22 checked, no open dependency**;
  roadmap #360 and six decisions CLOSED. No issue/checklist mutation.
- `git fetch origin main`, identity/tree comparisons, `git diff --check`, AST
  inventory, tracked `rg` surveys; `gh issue view`, `gh api graphql`, `gh pr checks`,
  `gh run view --log`/`--log-failed`. One retry of the publisher's infrastructure
  failure, not an application test rerun. No flaky pass, admin merge or bypass.
- Created `.venv` with Python 3.12 and installed `requirements.txt`. Only targeted
  unittest selections: `test_s3_upload`, `test_upload_retry_policy`,
  `test_cloud_deletion_safety`, `test_vault_move_safety`, `test_essential_interface`
  (**51**); projection/release-policy/kit/delay/lifecycle/preflight/PostgreSQL-config
  modules/classes (**53**); `LocalReleaseWorkerTests` (**10**): **114 initial passes**.
  After the integrated fix, `test_recovery_kit` adds **10 green executions** (nine
  repeated cases plus the new regression): **124 successful executions, 0 skips**.
  The new regression's expected pre-fix failure is recorded separately, not accepted.
- Main CI logs independently trace ten critical regressions in `evidence.json`.
  Dedicated S3 job ran the SDK upload/recovery and copy/multipart proofs: **moto**
  under the historical MinIO check name, **not real MinIO/AWS**, not real >5 GiB.
  Production multipart-copy seams are exercised with small payloads/lowered test
  cutoffs. Playwright uses disposable fixtures, not live SMTP/OIDC/AWS.
- Ordinary Python shards intentionally skip S3 cases (executed in dedicated S3 job)
  and five pre-existing Docker/Compose smoke cases. Production-image backup and
  scanner jobs run separately; this is not proof of those skipped smoke scenarios.
  No local full suite, DB/S3 start, Playwright, frontend/image build or services.
- Security checks succeeded under existing policy, not a zero-vulnerability claim:
  snapshot still has [development-only low Dependabot alert #35](https://github.com/PaoloDelCasale/frostvault/security/dependabot/35).
  No work on Dependabot PR451/452, security #405 or unrelated issues.
- No real data, credentials, services, Compose, `/boot`, backups, migrations,
  restarts or deploys touched. No backup copies. Reports/scratch stayed in this
  session directory; no subagents or provider/model/settings changes.

**Coordinator next:** review this draft, verify the final-head check attestation,
merge only when acceptable, and explicitly resolve the main publication blocker
before declaring final epic completion/deployment readiness. Then STOP as requested.
