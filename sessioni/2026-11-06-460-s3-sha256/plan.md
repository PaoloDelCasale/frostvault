# #460 — implementation candidate (no merge)

Base: 8413ffada15d2d5f73d558048da9f327d5939ff2.
Read #460, #464, #459, #458, #461, #230, #420, #416, #410 and ADRs 0009/0016/0018. Native #460 has no parent/blocker and blocks #464; #410 is contextual. Project fields unavailable with current read:project scopes; no token changes requested.

Plan: immutable independent upload snapshot proof (plaintext SHA256, size, SHA256 full/composite and part layout); bounded validated part buffers at the pinned managed-transfer facade, retaining shared bandwidth and cancellation. Freeze mode at Job admission; persist snapshot before write and Archive Version verification method/fallback, never backfill legacy trust. Capture raw upload identity and register before HEAD/proof failures. Legacy/shared operations retain full read-back. Expose optional Metadata/on_acknowledged hooks without implementing provenance/crash recovery (#464).

DB and populated migration tests only on disposable PostgreSQL16 PR CI; no local DB configured. Local targeted no-network SDK/helper/Vitest tests only. No production operations, backups, secrets, Docker or system-package installation.
