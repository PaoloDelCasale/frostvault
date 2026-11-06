# #461 — preflight queue and explicit upload retry

Work in progress; dedicated draft PR, no merge/deployment authorized.

Baseline: 0a65c09f (#458/#462 integrated). Native #461 parent: none; blocked by #458 (closed). Read full #461/#458/#459/#464/#410/#423/#424/#425/#430, CONTEXT and ADR-0016/0017/0020. Project fields unavailable: token lacks read:project.

Plan: durable singleton readiness cache (migration 0062), PostgreSQL transaction advisory single-flight, read-only preflight probes, capped 5/10/20/40/60 second failure backoff. Queue suspension never consumes Job retries. Explicit per-Vault/global retry reuses stable automatic upload admission and fresh Jobs, preserving failure history. Whole-Vault upload uses existing catalog whole_vault seam; only empty upload directory path is allowed. API/types and UI updated together.

No local PostgreSQL/MinIO/full suite/build; targeted pure Python and Vitest locally. Full verification and migration/concurrency tests delegated to PR CI, not silently skipped. No production data/services/credentials touched.

Evidence and final limitations will be completed after CI.
