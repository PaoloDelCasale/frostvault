# #464 implementation plan

Base merged main: ac9caff54d1692f4d4244aadfec54e6eb0920097. Dedicated worktree t3-91cc5ba0. No subagents, merge, production services, local PostgreSQL/S3, backups or system changes.

Read #464 approved body/comments and native blocked-by #460 / blocking #459; #460 closed final merge comment, siblings #458/#461 closed and #459; CONTEXT and ADR0001/0016/0018/0020/0021/0023. No native parent or Project. Read final #460 handoff and authorized coordinator preparation/frontier.

Implementation seams:
- DB-only durable upload intent referencing immutable format1 UploadSnapshot, exact owner/provider target, prewrite inventory and separate opaque token. Send-started committed before mutation; receipt append independent of cancellation/lease, no publication authority. Reconcile before hash/PUT; zero/multiple/fake tokens or observable writers stay blocked.
- Shared exact-version replacement cleanup authority: independently proven app lineage or historical full exact GET duplicate of verified available keeper. Durable historical evidence separate from Current Snapshot ordering; discovery/catalog alone never authorize deletion. Complete inventory and per-delete identity/claim/source/lifecycle fences; verify postcondition before completion. Scan stops destructive shortcut and records actionable pending reconciliation, never launders unknowns.
- Request-specific non-idempotent SDK writes with total_max_attempts=1, preserving reads/preflight/parts.
- Explicit reconcile-only API/UI independent of ordinary upload eligibility; no automatic sweep or failure-history resolution (#459).
- Targeted SDK/provider tests locally; real worker/admission/claim + migration/fault coverage only disposable PR CI. No local DB configured is not skipped coverage evidence.

Draft PR early; push fixes and require all applicable checks success on exact final HEAD. Candidate remains NO MERGE. Full-diff self-review and final handoff for #459.
