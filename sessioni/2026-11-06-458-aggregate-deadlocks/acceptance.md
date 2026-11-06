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

## Evidence / outcome

Implementation in progress. Final test commands, run links, limitations and integration notes will be recorded here before handoff.

GitHub native relationships: #458 has no parent/blockers and blocks #461; #459/#464 have no native parent/dependencies. All bodies/comments read. #410 roadmap and ADR-0001/0016 inspected. GitHub token lacks `read:project`, so project fields cannot be inspected; no project/batch decisions changed.
