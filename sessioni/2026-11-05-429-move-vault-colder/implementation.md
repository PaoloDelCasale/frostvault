# #429 — implementation record

Base: origin/main 4304bb9e3bb7222ec06c362059f4c51eb35eb5ab (PR #453).
Scope: Vault-wide colder-only move on its Storage Plan, estimates/warnings, exact-version verified replacement, resumable background Jobs. No merge/issue closure/epic update.

Read: AGENTS.md; docs/agents/issue-tracker.md and domain.md; CONTEXT.md; ADR-0001/0012/0016/0018/0019/0020; issues #429, #410, #360, #366 and siblings #418/#420/#421/#422/#426/#427/#428/#430. Native parent #410, blockedBy #422 CLOSED, blocking #430 OPEN. Project fields cannot be queried with the existing token (read:project unavailable); no token changes attempted.

Findings: existing internal class-copy worker preserves mutable Archive Version identity and retained source, and automatically warms/restores; it cannot directly implement #429. Reuse its exact-version S3/checksum/multipart helpers, scheduler and per-file Job fencing, with a durable move ledger for the new Vault-wide path. Preserve legacy reconciliation compatibility while adding immutable destination publication and copy provenance for safe restart. No expiration or per-file public API.

Verification: targeted local tests only; PostgreSQL/MinIO/full suites/builds in PR CI. No local database, production services or credentials.

## Implemented safety boundaries

- Saved Storage Plan targets only, strict colder-only supported transitions, verified exact Current Snapshot per file. Standard has no action; mixed classes and pending #428 deletions are reported as skipped.
- Preview/admission are bound to the displayed Vault ID; no path/directory/version selectors accepted. Fresh Vault lock and worker checks fence stopped/reconnecting Vaults, stale paths, changed source identity/trust and competing Jobs.
- CopyObject/multipart preserve exact source VersionId, tags/headers/custom metadata and add opaque copy provenance. Durable ledger precedes I/O; checksum verification precedes immutable destination publication; cleanup targets only the captured source after fresh keeper/listing/lease/cancellation checks.
- Interrupted response, publication and source-delete windows resume from exact ledger/provenance; source removal is idempotent. Unknown copies/markers/current writes and duplicate provenance are held, not guessed away. Cloud-only Conflict pins and local release clock survive representation changes.
- Multipart SHA256 uses AWS COMPOSITE, with an independent full-source SHA256/part-tree proof. CI exposed nonconforming compatible-provider composite metadata: exact destination GET/SHA256 substitutes only when the bytes independently match. Cold unreadable/mismatched destinations retain all copies; there is no automatic restore.
- Migration 0060 is additive metadata-only; complete/cancel moves and reconcile retained provider copies before downgrade. No deployment performed.

## Review focus

`app/services/vault_moves.py` owns admission, immutable replacement and copy ledger; `app/services/s3_copy.py` retains transport/checksum boundaries. Legacy internal class-copy Jobs keep their old private reconciliation seam; no per-file public API was reintroduced. Source-delete I/O holds Vault/Job row locks and rechecks immediately before the provider call; external S3 writers remain outside the application lock boundary.

Final verification evidence is recorded separately in this session folder so the final report/check snapshots do not change the tested source HEAD.
