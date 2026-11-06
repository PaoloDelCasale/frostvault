# #464 contract — coordinator takeover review candidate, NOT MERGED

PR: https://github.com/PaoloDelCasale/frostvault/pull/470
Branch: feat/464-upload-provenance
Worktree: /mnt/cache/appdata/t3code-native/worktrees/frostvault/t3-91cc5ba0
Base: ac9caff54d1692f4d4244aadfec54e6eb0920097 (#460 merged)

The Owner requested unblocking after the implementation agent ran over two hours. Coordinator cancelled its T3 task, verified interrupted/no active run, preserved every committed/uncommitted edit and took over this checkout. No replacement implementation agent. This document is a candidate contract: independent review, exact-final-HEAD CI and coordinator merge are still required.

## Reusable seams for #459

- `upload_provenance.load_for_job(connection, job_id)` / `outstanding(connection, file_id)` load immutable pre-write intent. `bind_for_resume` attaches an admitted Job to it, rejecting deliberate cancellation. Do not hash Local Copy B as expected evidence for already-sent A.
- Intent stores exact target envelope, original Job, distinct opaque metadata token, immutable #460 UploadSnapshot/mode, pre-send inventory, send_started and optional exact AV. `append_receipt` is allowlisted/idempotent DB-only replay, independent of Job claim/cancellation; receipt is fact, not publication/verification/deletion authority. Incompatible receipts block.
- `_resume_materialized_upload` verifies bound scope, exact receipt/unique token discovery and original snapshot before `_publish_upload_receipt`. Zero token after send_started remains ambiguity, never repeat PUT. New writes use one-shot non-idempotent SDK clients; reads and same-MPU parts retain retry behavior.
- `process_upload` recognizes `jobs.upload_reconcile_only`. Such a Job must obtain a durable exact linked target via provenance before proceeding; no linked target means block before the hash/PUT branch. Linked retries keep #460 evidence, exact HEAD contradiction checks, original mode, A/B distinction and completed-after-cleanup.
- `upload_provenance.describe` exposes pending/can_resume/reason/remedy for present intents, existing cleanup blocks and failed linked unverified legacy uploads. `schedule_resume` admits an explicit manual reconcile-only Job under Vault lock/active-file constraints. `POST /api/upload/reconcile` is separate from ordinary upload eligibility (including protected-file409). Original failed Jobs are NOT resolved here.
- Legacy `_legacy_materialized` requires exact same file/path/AV and independent originating failed Job digest (and matching snapshots if present); cancelled targets excluded. No synthesized intent/token. S3 baseline absent requires #460 exact plaintext read-back. #459 must separately discover historical verified+cleanup/completion obligations that have no new intent/block, preserving proof and identity instead of guessing latest.

## Common replacement cleanup

`storage.purge_current_snapshot_superseded` delegates upload/rename to `snapshot_cleanup.cleanup`. Cloud scan never deletes discovered extras directly: it leaves a durable block/remedy for claimed reconciliation. Mere catalog membership, origin, token, size, ETag or timestamps do not confer cleanup authority.

- `pin_inventory` commits exact key/version/marker inventory before provider writes, reuses unfinished inventories on the same materialized target, and records completed_at after postcondition. Unbound pending sends block competing mutations; a completed old inventory is not reused as a new upload baseline.
- `_keeper` pins independently verified available current AV/digest and namespace, rejects contradictory baselines or newer independently owned attempts. `_has_lineage` checks new prewrite scope+receipt+expected proof; legacy app ownership needs originating Job expected digest; copy-ledger lineage stays separate. A stale A cannot purge materialized newer B.
- Unknown historical candidate requires complete exact GET identity/length/SHA256 equal to trusted keeper. Different/unreadable/token-ambiguous content remains. Proofs live separately, so adoption cannot become Current Snapshot or alter local clocks/matches.
- Every delete consumes exact durable authority under current claim/path/Vault/source/cancellation fences and complete stable inventory. Keeper never targeted; new external content/markers are not added to consent. Observable external writer races block, no claim of DB/S3 atomicity. Existing Delete Markers are retained by replacement; delayed deletion consent is unchanged.
- Exact committed proof and absence reconcile delete-to-DB gaps without repeated GET/delete. Missing catalog observations without prior authority remain missing, not falsely purged. Settle intent/clear operation block only after full cleanup postcondition. verified alone is never completed.
- Move now rejects unsettled sends but retains its own verification/consent/copy ledger. Recovery, Reconnect and other verification promises are unchanged.

## Schema

Metadata-only Alembic0064_upload_provenance follows0063. Adds jobs.upload_reconcile_only and tables upload_intents, upload_intent_jobs, upload_receipts, snapshot_cleanup_inventories (including completed_at), snapshot_cleanup_proofs, snapshot_reconciliation_blocks. No I/O or inferred trust during migration. Downgrade refuses unresolved sends, undeleted proof authority, unfinished materialized cleanup inventory or unresolved operation blocks before DDL. Do not discard cancelled ambiguous receipt facts. #459 should follow final merged0064 with a single Alembic head, using these facts rather than introducing a competing proof/journal.

## Verification and limits at takeover

51 targeted nonDB provenance/SHA256/SDK tests passed locally; syntax and diff-check passed. No local PostgreSQL16 configured: all worker/migration/database verification is disposable PR CI, not skipped local evidence. Coordinator corrected red fixtures: terminal explicit reconciliation exhausts retry budget before a transient error; missing rename projection consistently remains missing; materialized legacy test independently seeds actual provider bytes and asserts real Job outcome before read counters.

Candidate includes worker fault tests for raw ack/lease/cancellation/COMMIT/restart, token ambiguity, duplicate-only historical authority, scan-to-catalog laundering, keeper preservation, exact API admission and migration refusal. Final check counts/HEAD are recorded on PR, not assumed here. No real AWS/>5GiB coverage claimed. No production DB/S3/Docker/deploy/restart/IAM/backup/secrets or /boot operations. #459 automatic <=60s DB discovery, target-scoped cancellation/backoff episodes, failed/remediation history linkage and notification resolution remain explicitly unimplemented here.
