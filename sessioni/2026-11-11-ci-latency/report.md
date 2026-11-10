# CI latency investigation

Session: `/mnt/cache/appdata/t3code-native/worktrees/frostvault/t3-764387f0-ed54c0aceb904337/sessioni/2026-11-11-ci-latency/`
PR: https://github.com/PaoloDelCasale/frostvault/pull/547

## Scope and safeguards

Read `AGENTS.md`, `CONTEXT.md`, `docs/ci.md`, ADR 0026 (queue/lock decisions), the incoming PR 546 ADR 0031 (pooled connections/shared held-job lock), `tests/test_ci_contracts.py`, `scripts/require_commit_checks.py`, and closed epic 410 including native parent/dependencies/children and its CI rules. No other worktree edited. No production service, live DB, credentials, system packages, deploy/release, raw database directory or backup touched. Only targeted non-DB Python modules run locally. Dependencies installed in this checkout's ignored `.venv`.

## Reproduction and history

Saved `gh run view <id> --json jobs,headSha,createdAt,updatedAt,conclusion` and `gh run view <id> --log` in this session directory. `measure.py` processes these saved inputs; `measurements.json` is its output. Raw logs/JSON remain ignored local artifacts rather than bloating Git history.

| CI run | SHA/context | PostgreSQL job | unittest | Nested 1,024-upload interval | Workflow wall time |
| --- | --- | ---: | ---: | ---: | ---: |
| 42064317690 | `3e0b4cd8`, before aggregate stress | 75 s | 43.311 s / 27 tests | absent | 163 s |
| 42141803100 | `0a65c09f`, stress introduction (#466) | 263 s | 230.277 s / 39 tests | 169.759 s | 296 s |
| 42236673923 | `c066c127`, later upload-provenance work | 541 s | 511.398 s / 42 tests | 438.108 s | 561 s |
| 42918719853 | `693b7c0a`, main through #544 | 662 s | 628.523 s / 46 tests | 549.860 s | 670 s |

Job timing comes from GitHub start/end times; unittest timing is the test runner's own elapsed measurement. The per-stress interval is between the previous test's completion timestamp and stress completion in the GitHub log, **including fixture/setup overhead**, not an instrumented method-only benchmark. Buffered diagnostic prints are not used to derive start time. Workflow wall time is `updatedAt - createdAt` for completed runs.

Main baseline reproduction: job 22:54:06–23:05:08 UTC; test step starts 22:54:34. Migrations finish around 22:55:42; nested stress finishes 23:04:52; remaining deterministic tests finish 23:05:04. Stress accounts for ~87.5% of unittest duration. This is not a frontend/browser/security bottleneck. These historical snapshots show both the introduction of the full stress and later growth in per-file production work, **not** a controlled attribution of each production change's cost.

## Hypotheses ranked before implementation

1. **Quantitative stress dominates the critical path — confirmed.** A bounded PR workload should shorten feedback substantially; preserve full acceptance as a blocking main/publication gate. Initial expectation (not a measurement): reducing 1,024 to 64 uploads removes most of the ~550 s stress cost, but does not reduce migration/deterministic/setup costs. Incoming #546 also adds a 2,048-file burst, so both quantities need the same profile without deleting either case or changing safety assertions.
2. **Repeated setup / twelve shards consume runner capacity — real cost, not the measured critical path.** On baseline, shard unittest durations range 16.280–73.908 s; the last shard completes 22:56:36, well before PostgreSQL's 23:05:08. Some runner queueing is visible, but no timing evidence justifies choosing another shard count. Keep 12. New coverage-once contract runs the actual selector for every shard, checks no module is omitted/duplicated, and ensures the dedicated migration class selection remains exhaustive.
3. **Duplicate builds/frontend/security work — not the measured bottleneck.** Most finish in roughly two minutes. Removing checks risks losing independent production image, Node24, E2E, security or S3 evidence without improving the dominant wall clock. Leave unchanged.

## Design

- `tests.stress_profile.stress_file_count(full_count)`: `pr` gives 64; `full` retains the full count. Explicit local tests default full. Empty/unknown env values raise, never skip/fallback. Each of 16 workers still processes four nested paths; barrier, concurrent maintenance and semantic assertions remain.
- Historical migration/concurrency check name unchanged. Explicit workflow profile is bounded only for pull requests; main executes full.
- Main-only `Full PostgreSQL concurrency stress` job is a lightweight **attestation**, not a second stress execution: needs the existing concurrency job, requires both `success` and the `full` output written only after unittest succeeds. It fails on upstream skip/failure/missing profile. PR attestation is intentionally skipped and is **not** a PR branch-protection check.
- Publisher retains every existing required check and adds this attestation on the gated exact SHA. Also verify `head_sha` in returned API evidence. Tags/manual promotion of a commit without successful full-main evidence fail closed, including old pre-change commits; no historical bypass.
- No new publication or release trigger. Full main stress still costs time; reduced PR workload trades long-tail load coverage on PRs for fast feedback, with full-load evidence required before any image can be published.

## Regression/validation evidence

Recorded red tests in `profile-red.log`, `contracts-red.log` and `additive-check-red.log` before profile/gate implementation. Then 50 targeted tests pass:

```sh
.venv/bin/python -m unittest \
  tests.test_stress_profile tests.test_ci_contracts tests.test_require_commit_checks -v
```

Coverage includes defaults/full/invalid profiles, all 16 nonempty batches, workflow event/profile/output/attestation wiring, executing the attestation shell for full/bounded/missing/failed/skipped/cancelled scenarios, complete shard selection exactly once, mandatory publish check, skipped/cancelled/failed/missing checks, wrong-SHA evidence, and exact publisher gate command without custom-check overrides or conditional/error bypass. `git diff --check` passes. No full local suite/build/browser/S3/DB run.

## Observed after / final verification

First PR CI run **42926609582**, exact HEAD `88ac70523fc937a8bfaafcb647da192186c24b90`, succeeded. All applicable CI/security checks passed; the main-only attestation was intentionally skipped. Production tree before the CI change equals baseline main (`git diff 693b7c0a..f5295199` is empty), so this is a useful before/after comparison without #546 pooling mixed in, though hosted-runner variability remains.

| Measurement | Baseline | First bounded PR | Observed reduction |
| --- | ---: | ---: | ---: |
| PostgreSQL job | 662 s | 121 s | 541 s / 81.7% |
| PostgreSQL unittest | 628.523 s / 46 tests | 90.825 s / same 46 tests | 537.698 s / 85.5% |
| Nested stress interval incl. fixture | 549.860 s / 1,024 uploads | 23.638 s / 64 uploads | 526.222 s / 95.7% |
| Entire CI workflow | 670 s | 149 s | 521 s / 77.8% |

Log confirms 16 workers, 64 completed uploads, 173 maintenance passes, exact full-rebuild parity. Last shard/setup/runner queueing is now relevant to remaining wall time; no arbitrary shard-count adjustment is justified by this single run. This first measured improvement does **not** include #546's second stress or pooled implementation.

Final verification still pending incorporation of eventual PR #546 merge and exact final-HEAD CI. Full stress on main has not been run by this session: it is deliberately not triggered via release/publication.
