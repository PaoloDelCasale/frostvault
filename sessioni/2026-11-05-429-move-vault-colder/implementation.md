# #429 — implementation record

Base: origin/main 4304bb9e3bb7222ec06c362059f4c51eb35eb5ab (PR #453).
Scope: Vault-wide colder-only move on its Storage Plan, estimates/warnings, exact-version verified replacement, resumable background Jobs. No merge/issue closure/epic update.

Read: AGENTS.md; docs/agents/issue-tracker.md and domain.md; CONTEXT.md; ADR-0016/0019/0020; issues #429, #410, #360, #366 and siblings #418/#420/#421/#422/#426/#427/#428/#430. Native parent #410, blockedBy #422 CLOSED, blocking #430 OPEN. Project fields cannot be queried with the existing token (read:project unavailable); no token changes attempted.

Findings: existing internal class-copy worker preserves mutable Archive Version identity and retained source, and automatically warms/restores; it cannot directly implement #429. Reuse its exact-version S3/checksum/multipart helpers, scheduler and per-file Job fencing, with a durable move ledger for the new Vault-wide path. Preserve legacy reconciliation compatibility while adding immutable destination publication and copy provenance for safe restart. No expiration or per-file public API.

Verification: targeted local tests only; PostgreSQL/MinIO/full suites/builds in PR CI. No local database, production services or credentials.
