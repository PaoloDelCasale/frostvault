# #426 — per-Vault local release

Base: 5ef6e7f2d63a6dcd8482f8eb6f2ebebba5239d12.

Scope: one nullable `local_release_after_days` setting: null = off, 90/180/365/730 days = 3 months/6 months/1 year/2 years. UI preselects 180 when enabling. All files, independent of upload globs. No change to #427/#428/#430.

Safety: retain verified/available/digest-matching exact-version and stable rename-claim checks; require confirmed Source Volume and cloud observations; recheck current automatic policy and retention at execution, including explicit recovery and content-observation timestamps. Manual release remains an explicit confirmed UI action, independent of automation.

Migration: conservatively turn every existing automatic policy off because the old installation-wide environment gate cannot be inferred from database state, and old filtered policies have a different scope. Retire pending legacy free-space jobs rather than silently executing them after removal of that gate. Never modify file content, mtime or cloud evidence in migration. Owner must explicitly re-enable the new policy.

Validation: targeted unit/frontend tests only locally; complete PostgreSQL/S3/frontend/build/e2e/image via draft PR CI. No local DB or production actions. Read #426, native relationships, #410/#409/#422/#424/#425/#427/#430, map #360 and decisions #361/#363/#365/#366; ADRs 0012/0015/0016. Project fields unavailable with current GitHub token (no read:project).
