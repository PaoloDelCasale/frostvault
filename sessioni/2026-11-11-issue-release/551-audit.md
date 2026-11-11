# #551 — shared upload catalog lock audit

PR: https://github.com/PaoloDelCasale/frostvault/pull/556
Branch: `perf/upload-provenance-shared-lock` (base `87b946a4`).

## Lock contract

Exclusive `FOR NO KEY UPDATE` remains the default. Only explicit `shared=True`
uses `FOR SHARE`. This is intentionally **not KEY SHARE**: SHARE conflicts with
non-key Vault lifecycle/identity writes and admission's `UPDATE vaults SET
name=name`, preserving the validity of target and incompatible-job checks until
commit. Both modes lock Vault before renewing/locking the claimed Job, then
file/version/intent rows. Nested `require_target`, `prepare`, `mark_send_started`
pass the same mode; no opted-in callback upgrades it. Pause is not a target
refusal: already leased Jobs must finish (ADR-0033).

## Audited shared calls and complete DB callees

| Call site in `app/storage.py` | Callback/callees and effects |
| --- | --- |
| `process_upload.load_intent_and_target` | `load_for_job`, `outstanding`: SELECT intents; `bind_for_resume`: read original/cancelled Jobs, INSERT binding only; `get_job_target`: SELECT Job/Local Copy/exact version. Coalesces two consecutive DB units on the ordinary path. |
| Post-reconciliation target refresh | `get_job_target` SELECT only; necessary after reconciliation potentially linked/published a version. |
| `_resume_materialized_upload` target check | `require_target -> target -> upload_remediation.require_job` (and `proven_absent` for bound episodes): reads only; nested target stays SHARE. Receipt/proof/provider reconciliation remains outside this transaction. |
| `_resume_materialized_upload` linkage | `link_job_version`: UPDATE this Job's `archive_version_id`, no status/aggregate/event mutation. |
| `process_upload.prepare_digest` | `set_upload_snapshot`: validate frozen JSON, lock/check/update own Job; `find_reusable_upload_version -> snapshot_cleanup._has_lineage -> receipt / validated_s3_proof`: DB reads plus pure integrity checks, no S3 calls. |
| Verified reuse `link_reused_version` | `link_job_version` + `get_job_target` in one DB-only transaction. |
| Unverified reuse linkage | `link_job_version` only; exact version proof remains separate. |
| Pre-send target pin | Direct `target(..., shared=True)`; read-only fence, committed before provider inventory reads. Its revalidation remains inside prepare and send-started. |
| Pre-write intent `prepare` | Nested `require_target` retains SHARE; read/reuse existing intent; refresh unsent token/scope/snapshot or INSERT intent/binding; `_settle_unstarted -> receipt / close_multiparts` reads receipts and updates only unsent intent/multipart rows; `outstanding` SELECT. |
| `mark_send_started` | Nested `require_target` retains SHARE; read receipt; UPDATE intent, `upload_remediation_discovery.capture` checks schema and INSERTs facts. This transaction commits BEFORE PutObject/CreateMultipartUpload. |
| Both verification metadata units | `set_version_verification_method`: UPDATE exact unverified Archive Version's method/fallback/egress/provider-proof fields. No Integrity/availability/current-keeper election or aggregate writes. |
| `process_rename` admitted digest | `set_upload_plaintext_digest`: validate digest and UPDATE own Job's lineage digest only. |

Every `_upload_catalog_unit` also executes `target -> require_job` (DB reads)
and `ensure_job_claim_owned_in_transaction -> renew_job_claim` (conditional
UPDATE of this Job's expiry only). No callback calls `set_job`, creates file
identity, records catalog events/revisions, requests aggregate rebuilds, or
writes derived aggregate/status rows. Foreign-key locks do not upgrade SHARE.

## Deliberately exclusive / unchanged

- `_publish_upload_receipt`: file lock, archive version publication/snapshot,
  Job linkage, intent linkage, multipart closure and version ordering.
- `_complete_verified_upload.persist_verification`: version Integrity plus
  Local Copy observation/fingerprint; `observe_local_copy -> _get_or_create_file
  -> _vault_lock_touch` needs exclusive Vault ownership.
- Mismatch publication, cleanup/remediation, proven-absence rearm, stale-snapshot
  closure and all other unaudited callbacks remain exclusive.
- Terminal/retry/queued Job transitions, aggregate maintenance/rebuild, admission,
  lifecycle and cancellation keep their existing locks and durable invalidations.
- Raw provider receipts remain facts, not publication authority; no DB retry
  wraps provider/filesystem I/O. No extra PUT after a post-provider DB retry.
- No changes to #550 bulk admission, #552 scheduler or #549/#554 notifications.
  The only rename change is its already-admitted DB digest catalog unit.

## Verification

- Local: Python 3.12 `.venv`; `tests.test_upload_provenance` (21 tests), syntax
  compilation of changed Python modules and `git diff --check`.
- No PG16 server binaries available locally; no packages/system DB/services
  changed. Real DB tests run against ephemeral PG16 in PR CI.
- Added real SQL-recording of full shared callbacks, including preparation
  INSERT/refresh/settle, read-back/checksum metadata, exact materialized resume,
  verified/unverified reuse and admitted digest. Recorder rejects stronger Vault
  locks, Vault writes, derived aggregate/status/events/revisions writes and
  nested `set_job` calls.
- Real PG overlap barrier on one Vault; conflicting lifecycle/admission/exclusive
  writer fences; pre-provider commit-time 40P01/40001 failures; statement aborts
  in shared prepare/send/snapshot/verification metadata with exact version and
  one-PUT assertions; pause/cancel safety with leased provider-bound work.
- Existing 2048-file/16-worker throughput proof now includes bounded (<10 s)
  pause/cancel/resume writers and a queued cancellation sentinel. Counts/floors
  (0.5 files/s, <=200 connections), aggregate maintenance, exact provider PUT/read
  assertions and profiles/gates remain unchanged. PR profile remains 64; full
  2048-file stress remains the exact-main publication gate, coordinator-owned.

## Integration concerns

1. Rebase/cherry-pick only this branch's commits. Preserve any coordinator changes
   to the shared `app/storage.py` made for other issues.
2. A future callback opted into SHARE must audit **all** callees again; do not
   add publication/Local Copy observation/catalog events/rebuilds/status changes.
3. MultiXact pressure and real 30k-file throughput/lock-wait distribution remain
   deployment observations; this work neither touches production nor claims a
   production speedup from fake-provider CI timings.
4. Project fields could not be queried with the existing token (`read:project`
   absent); #551 has no native parent, sub-issues, blocked-by or blocking edges.
5. Coordinator alone merges/releases. No migration/restart is required by these
   source changes themselves; no deploy/release/tag or production action done.

Exact final SHA/check evidence is recorded in the PR after the last push, avoiding
an endless documentation-only SHA change after validation.
