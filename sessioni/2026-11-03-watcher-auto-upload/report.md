# Issue #423 — watcher-driven automatic protection

## Scope and base

- Branch: `feat/auto-upload-watcher-queue`.
- Base verified after fetching origin/main: `fefc282448fa795307e4e9075ddf41d1ab8be9f0` (#444).
- Only #423 is authorized; no subagents, merge, or implementation of #424+.
- Read AGENTS.md, CONTEXT.md, issue-tracker instructions, #423/#410, native dependency #422 and downstream #430, siblings #424–#427, decisions #361/#366, ADRs 0001/0012/0014/0016.
- Native relationships confirmed via GraphQL. Project metadata is unavailable with the current token (`read:project` missing); no credential changes attempted.

## Implementation plan

- Persist the start of observed content stability, retaining it across unchanged full scans and restart; reset it for edits.
- Periodically queue stable catalog observations independently of cloud validation/lifecycle, using existing Jobs, conflict eligibility, upload preflight, exact-VersionId verification, source-change and lease fences.
- Continue full scans as reconciliation, never as the prerequisite for watcher uploads.
- Default new Vaults to auto-upload; preserve existing intentional/off-by-default pauses during migration.
- Fixed automatic noise exclusions cannot be overridden by include globs; do not restore removed policy tags.
- Preserve cloud-sync health/error reporting without implementing the notification or state redesigns.
- Add deterministic simulated-clock tests and migration/default coverage, regenerate API artifacts for changed defaults, run targeted local tests and full PR CI.

## Validation / operational boundaries

Python 3.12 virtualenv created locally. PostgreSQL server/client tools are absent; no production DB, system package, Docker/service, secrets, backup-share or boot operations. Database-backed validation will run in CI. No backup copies created.

Implementation and final validation results will be recorded here before completion.
