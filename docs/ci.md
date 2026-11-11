# Continuous integration

This repository uses layered CI so pull requests stay deterministic while still
proving upload and recovery integrity against object storage.

## Python runtime alignment

Python **3.12** is the single supported/tested runtime line for the backend,
not a runtime matrix. The production `Dockerfile` uses the official
`python:3.12.<patch>-slim-trixie` image pinned to its multi-platform index digest.
All `setup-python` steps select `3.12`, including unit/migration/S3 tests,
Playwright's API/seed interpreter, dependency auditing, the CodeQL SARIF gate
and the exact-commit publication gate. OpenAPI response/artifact contracts run
in the Python suite; local exports use `.venv/bin/python` as documented below.
TypeScript artifact generation itself uses Node, not a second Python runtime.

The image keeps Debian trixie (the previous base's distribution), PGDG's explicit
`postgresql-client-16` and the existing OS upgrades/pip removal. PR CI checks the
built image's Python runtime and all five PostgreSQL 16 client utilities before
exercising encrypted metadata backup/restore. Image/security builds and
publication all use the same pinned `Dockerfile`; scan severity, publication
checks and the main-only exact-SHA full-stress requirement are unchanged.

The root Docker ecosystem in `.github/dependabot.yml` already maintains literal
`FROM python:<version>-slim-trixie@sha256:<digest>` references. Digest refreshes
and Python 3.12 patch updates remain ordinary reviewed dependency PRs. A move to
another Python runtime line must update CI and the alignment contracts together;
it must not silently ship through a base-tag update. `tests/test_ci_contracts.py`
checks the versioned digest pin and runtime alignment without registry access.

## Pull request and push to `main`

Workflow: [`.github/workflows/migrations.yml`](../.github/workflows/migrations.yml)

| Job | What it proves | Credentials |
| --- | --- | --- |
| Unit and migration tests | Python `unittest` suite against isolated `postgres:16` services, partitioned across twelve parallel shards by packing largest test modules first. The stable aggregate requires all shards and the dedicated migration/concurrency job below. Each fixture uses its own database, with a migrated HEAD template for fresh fixtures; populated upgrades/downgrades still run Alembic. No database regression skips without configuration. | None. Live AWS/MinIO env vars are intentionally unset so S3 integration cases skip. |
| PostgreSQL migration and concurrency tests | PostgreSQL-specific migration/concurrency cases against `postgres:16`, including #458: nested uploads through `process_job` on all 16 workers with concurrent aggregate maintenance and full-rollup parity (64 uploads on PRs; the full 1,024 on `main`), a real forced deadlock, commit-time aborts and exact-version/no-extra-PUT retry assertions on every PR, and #535: a small-upload burst (64 files on PRs; the full 2,048 on `main`) with the unchanged conservative files/second floor and bound on new PostgreSQL connections (pooled `db()`). Provider I/O is a thread-safe fake; actual SDK/cloud-compatible integrity remains in the S3 job below. | Ephemeral Postgres service only. |
| Upload execution baseline comparison | Separate #562 descriptive benchmark: the same external harness compares fixed v0.5.3 with the candidate on one runner/PG16 service, with three alternating repetitions per SHA and driver-probe profile. Real claims, Job execution, SDK orchestration and exact completed-after-cleanup assertions; in-memory S3 API responses, not AWS/HTTP throughput. Raw JSON, SQL-shape trace and summary artifact. No timing threshold and no change to existing stress/publication gates. | Ephemeral Postgres service only; synthetic SDK credentials, no network provider. |
| Frontend generate and typecheck | Regenerates the committed OpenAPI TypeScript artifacts from `frontend/openapi.json`, fails on generated-artifact drift, then runs the strict browser/Node/Vitest/Playwright TypeScript projects. | None. |
| Frontend lint and unit tests | ESLint (including `e2e/`) and Vitest on Node 22, in parallel with typecheck and the production build. | None. |
| Frontend unit tests (Node 24) | The same Vitest suite on Node 24, which `frontend/package.json` engines already allow. Guards Blob/Response/WebCrypto download checks that can pass on Node 22 and fail on jsdom+Node 24. | None. |
| Frontend production build | Vite production build only (`npm run build:ci`); TypeScript is already checked in the parallel typecheck job. | None. |
| Playwright e2e (mobile-375 / desktop-1280) | Chromium Playwright against uvicorn + isolated PostgreSQL 16 with seeded fixtures, split into two parallel projects: 375×667 and 1280×800. | None. Placeholder AWS env only; no live cloud calls. Browsers cached under `~/.cache/ms-playwright`. Uses `E2E_PYTHON=python` (setup-python on PATH; no repo `.venv` in CI). Failure screenshots upload as `playwright-e2e-failures-<project>`; successful 375px shots as `playwright-e2e-375px`. |
| Production image PostgreSQL backup | Builds the production Docker image, checks `pg_dump`/`pg_restore`/`createdb`/`dropdb`/`psql`, verifies the inherited exec-form readiness healthcheck and runs focused Host/HTTP/failure tests against the packaged probe as numeric UID/GID on a read-only rootfs (no DB/S3 needed for those tests), then runs Alembic + `backup_upgrade --skip-upgrade` + isolated restore verification against `postgres:16` | Ephemeral Postgres service only. |
| S3-compatible integrity (MinIO) | Real SDK upload/recovery SHA-256 proofs against a versioned S3-compatible endpoint (plaintext, empty, Unicode, multipart cutoff; all four bucket BPA flags required) plus prefix cleanup | Ephemeral PostgreSQL 16 and moto S3 server (`moto[server]` pinned, from PyPI) on `127.0.0.1:9000`. No AWS account. The job keeps its historical name because release gates require it; MinIO no longer publishes community images (Docker Hub `minio/minio` is gone and `quay.io/minio/minio` refuses anonymous pulls). |

### Upload stress profiles and publication safety

Only the **quantity** of the long upload bursts changes between profiles:

- `FROSTVAULT_STRESS_PROFILE=pr`: 64 nested uploads per stress case, four Jobs
  per each of the same 16 workers, with the same concurrent maintenance,
  completed/verified exact-version/no-extra-PUT assertions and rollup parity.
- `FROSTVAULT_STRESS_PROFILE=full`: the original full acceptance sizes (1,024
  nested uploads and the pooled-connection burst's 2,048 files). Explicit local
  test invocations default to `full`; an empty or
  unknown value raises an error, never a skip or a smaller fallback.

The historical **PostgreSQL migration and concurrency tests** check stays stable
for branch protection. Its workflow explicitly chooses `pr` only for
`pull_request`; pushes to `main` use `full`. All deterministic migration,
real-deadlock, transaction-abort, race and retry tests remain selected unchanged
on every PR. All twelve Python shards, both Node versions, E2E, image/backup,
S3 integrity and security checks remain intact. A contract test executes the
actual shard selector and proves that every ordinary module is selected exactly
once and the dedicated modules are not duplicated in the shards.

A lightweight **Full PostgreSQL concurrency stress** check runs **only on pushes
to `main`**, after the existing concurrency job. It requires both a successful
job and its `full` profile output (written only after the tests succeed). This
attests the completed full workload without repeating it on another runner.
It is intentionally skipped on PRs: **do not add it to PR branch protection**.
The publisher requires it in addition to every previous check on the **exact
commit being published/promoted**. Missing, skipped, pending, cancelled, failed
or wrong-SHA full-stress evidence cannot publish, including a manual dispatch
or a version tag using the updated gate. The updated gate also refuses a
pre-change historical commit without this new evidence; it has no legacy bypass.
The gate's optional `--check` arguments add requirements, never replace the
mandatory publication checks. Full stress is not merely a nightly or
optional test. No new release or deployment trigger is introduced.

The tradeoff is deliberate: PRs find deterministic concurrency failures quickly,
while high-volume/long-tail effects remain a blocking pre-publication check on
`main`. Main and image publication still pay the full stress cost. Measurements
and hypothesis ranking are recorded in the
[CI-latency session report](../sessioni/2026-11-11-ci-latency/report.md).

### Repeatable upload execution comparison (#562)

The **Upload execution baseline comparison** job is independent of the existing
stress checks, thresholds, aggregate and publication requirements. It must fail
on invalid prerequisites, extra PUTs, missing claims or wrong outcomes; it has
**no speed pass/fail threshold**. It first runs the new
`tests.upload_benchmark_postgres_contract` against disposable PG16 in both frozen
verification modes. Existing integrity tests and floors are not changed.

`scripts/upload_benchmark.py` runs **its own identical worker/probes** against
two clean checkouts; it does not copy candidate code into the baseline. The
baseline must resolve to v0.5.3
`628d065fe54a5c13364d5da7c58cb563d0bd80b7`. Each subprocess imports `app` from the
specified checkout, uses its real migrations and the unchanged
`tests/postgres.py` fixture (the harness refuses different fixture hashes), and
creates uniquely named disposable databases. A manifest records both exact
SHAs, harness/fixture/requirements digests, Python/platform and installed SDK/PG
driver versions, workload and actual run order.
On PRs the candidate is the checkout's tested merge SHA, just like ordinary CI.

Default CI workload: **32 new temporary files × 10 KiB**, one Vault, **16**
concurrent claimed Jobs, unlimited bandwidth, explicit frozen **s3_checksum** on
each Job. Every sample gets a fresh filesystem/database and 16 distinct warmup
files; seeding/migration/warmup are excluded and separately timed. The timer
covers the synchronized burst's real claim/load, hashing, SDK upload,
verification, durable receipt, publication, cleanup and all `process_job`
returns. Per-file claim→completed, claim-only and handler latencies are reported.
It does **not** time bulk admission/HTTP202 or scheduler candidate nomination.
Bucket readiness is a prevalidated fixture. Pause/cancel and aggregate
maintenance (including automatic aggregate scheduling) are explicitly **excluded
on both SHAs**; the unchanged concurrency stress covers those adversarial cases.
The real pool reset and PG16 `fsync`, `synchronous_commit`, `full_page_writes=on`
remain intact. No production service, dataset, credentials or S3 endpoint is used.

The provider intercepts **only** `BaseClient._make_api_call`. Production client
creation, upload facade, bounded-body checksum validation, SDK managers/threads,
progress/ack callbacks and the whole Job pipeline still run. The thread-safe
emulator consumes actual request bodies, validates supplied SHA256, preserves
metadata/exact VersionIds, supports paged listings and exact GET/HEAD, counts
API calls without inventing internal HEADs, and rejects unexpected operations
(including fresh-file deletes/multipart). **No HTTP serialization, network,
service retry latency or AWS throughput is represented.** This is not the old
high-level synchronous upload stub. Real SDK-to-endpoint/AWS integrity evidence
remains in the existing separate S3 jobs; this comparison does not claim an
endpoint/HTTP profile or substitute for those checks.

There are **three repetitions per SHA for each of two driver-probe profiles**
(12 samples): traced and plain; baseline/candidate order is AB, BA, AB, and
trace/plain order also alternates. The plain runs disable only SQL-driver
instrumentation, keeping provider counting and the 50 ms lock observer the
same. Median elapsed traced/plain minus one reports noisy probe overhead; it is
not subtracted from results. Reports include throughput/latency, standard
deviation/CV, per-file work, bounded parameter-free normalized SQL shapes,
actual transaction start/finish state transitions plus BEGIN/COMMIT/ROLLBACK
command attempts (a redundant BEGIN is not a second transaction), receipt INSERTs, renewal/progress
UPDATE attempts, lock clauses/wait samples and S3 operations. SQL/driver timing
is inclusive (implicit BEGIN can overlap cursor timing), not additive CPU time;
transactions are not physical fsyncs. Server-trigger SQL is not a separate client
statement. Sampling can miss short lock waits. No metric labels contain file,
Job, VersionId or token identities; SQL parameter values are never exported.

Every sample checks completed **after cleanup and intent settlement**, one
durable receipt per intent, independently expected exact bytes/digest/VersionId,
verified/available catalog state and matched Local Copy, frozen requested/effective
mode without fallback, and exactly one PUT per completed file. A checksum sample
must not fall back to GET; read_back must perform an exact GET. All validation is
outside timing. Failures write only exception type and frame names/line numbers,
never DB/provider exception text. The artifact `upload-execution-comparison`
(retained seven days) includes `manifest.json`, the raw samples, `summary.json`
and `summary.md` (also appended to the job summary). Artifact size is bounded by
workload limits and a maximum 512 SQL shapes per sample. A failing run may contain
only manifest/partial samples; never treat it as a valid comparison.

To reproduce, prepare two clean checkouts and a disposable loopback PG16 service
with CREATEDB, export `TEST_POSTGRES_URL` without displaying credentials, then
use the **same Python 3.12 environment** and candidate harness for both:

```bash
# Paths are examples; output must be a fresh directory in this session.
.venv/bin/python -m unittest tests.test_upload_benchmark -v  # no DB/network
.venv/bin/python -m unittest tests.upload_benchmark_postgres_contract -v  # disposable PG16 required
.venv/bin/python scripts/upload_benchmark.py \
  --baseline /path/to/clean-v0.5.3 --candidate /path/to/clean-candidate \
  --output /path/to/sessioni/<session>/upload-comparison \
  --disposable-postgres \
  --mode s3_checksum --files 32 --concurrency 16 --warmup 16 --repetitions 3
```

`--mode read_back` creates a **separate** comparison, not a mixed-mode ratio.
Arguments are bounded (3–5 repetitions, 1–16 workers, at most 128 measured and
32 warmup files). The tool refuses non-loopback DB addresses, wrong PG major,
weakened durability, dirty compared trees, wrong baseline or missing configuration.
The explicit disposable acknowledgment is still essential: loopback alone cannot
prove that an operator chose a test server. Do not run these DB commands on the
homelab's live PostgreSQL; local environments without PG16 should use PR CI.
Never compare these fake-provider numbers directly with production observations
from #535/#551 or call a static call-count estimate a runtime result.

Failed S3-compatible cleanup writes `artifacts/s3-cleanup-report.json` and uploads it as a
workflow artifact. Rerun cleanup locally or in CI with:

```bash
export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_ENDPOINT_URL=http://127.0.0.1:9000
python -m app.services.s3_prefix_cleanup_cli \
  --bucket archive-ci \
  --prefix ci-runs \
  --report-path artifacts/s3-cleanup-report.json
```

## Optional manual AWS proofs

Workflow: [`.github/workflows/aws-s3-integrity.yml`](../.github/workflows/aws-s3-integrity.yml)

- Trigger: manual `workflow_dispatch` only. **Not** on a schedule or pull requests.
- Auth: GitHub OIDC → IAM role (no static AWS keys in GitHub secrets).
- Scope: objects under a dedicated prefix only. Provision the role with
  [`infra/terraform/github-oidc-ci/`](../infra/terraform/github-oidc-ci/).
- Required repository-level GitHub Actions variables:
  - `AWS_CI_ROLE_ARN`
  - `AWS_CI_TEST_BUCKET`
  - `AWS_CI_TEST_PREFIX` (default `ci/github`)
  - `AWS_CI_REGION` (optional, default `eu-south-1`)

Cleanup always runs and uploads `aws-s3-cleanup-report.json`. Rerun the workflow
or the CLI above against the same bucket/prefix if leftovers remain.

## Container image (GHCR)

Workflow: [`.github/workflows/publish-image.yml`](../.github/workflows/publish-image.yml)

- Triggers: push to `main`, version tags `v*`, and `workflow_dispatch`.
  `workflow_run` and `pull_request_target` are forbidden.
- A `require-ci` job gates publish on the **exact commit SHA** (or the git tag
  `vX.Y.Z` when promoting). It waits for GitHub check runs from [CI](../.github/workflows/migrations.yml):
  PostgreSQL unit/migration aggregate, PostgreSQL concurrency and its main-only
  **Full PostgreSQL concurrency stress** attestation, MinIO S3-compatible integrity, frontend
  generate/typecheck/lint/unit/Node 24/build, Playwright (375 and 1280), and
  the production-image PostgreSQL backup. Missing, pending, cancelled, skipped,
  or failed checks block the image. The script is
  [`scripts/require_commit_checks.py`](../scripts/require_commit_checks.py).
- Publishes `ghcr.io/paolodelcasale/frostvault`: every build gets a short
  `sha-` tag from the gated SHA; version tags also get the full semver,
  major/minor, and `latest` tags. Ordinary `main` pushes do not move `latest`,
  so it remains the newest published release.
- A manual dispatch may set `promote_tag` to an existing full semantic version
  such as `0.3.0`. The workflow resolves `v0.3.0` to a commit, requires that
  commit's checks, then promotes that exact manifest to `latest` without
  rebuilding it and fails if the resulting digests differ.
- Live AWS S3 proofs stay **manual** via [Optional manual AWS proofs](#optional-manual-aws-proofs).
  They are not a publish gate; MinIO covers S3-compatible integrity in CI.
- Compose files pull this image; operators do not need a local image build for a
  standard deploy.

## Repository security scanners

Workflow: [`.github/workflows/security.yml`](../.github/workflows/security.yml)

| Scanner | Gate | Exceptions |
| --- | --- | --- |
| Dependency review via pip-audit | **Pull requests** run in baseline mode ([`scripts/pip_audit_gate.py`](../scripts/pip_audit_gate.py)): the base branch's `requirements.txt` is audited too and only vulnerabilities the PR introduces fail. **`main` and the daily schedule** are strict: any known vulnerability in `requirements.txt` fails. | Temporarily add `--ignore-vuln` IDs with a linked issue URL and review date. Native `actions/dependency-review-action` needs Dependency graph + GitHub Advanced Security on private repos; pip-audit is the portable substitute for this Python stack. |
| Dependency review via npm audit | Same two modes as pip-audit (`--baseline-full` on pull requests, strict on `main` and the schedule). Strict mode fails on any known vulnerability in the `frontend/` lockfile, including transitive copies and `dev` toolchain packages. A second `npm audit --omit=dev` report classifies **runtime** vs **build** exposure. JSON reports upload as the `npm-audit` artifact. The production image only copies `frontend/dist`, so Trivy on the final image does not replace this gate. | Add a GHSA/CVE to [`.github/npm-audit-exceptions.json`](../.github/npm-audit-exceptions.json) with `reason`, GitHub issue URL, `exposure` (`runtime` or `build`), and `review_by`. Expired, unused, or build-classified runtime findings fail the job. Do not use `npm audit fix --force`. |
| CodeQL | SARIF artifact + job fails on error/high/critical findings; upload to GitHub Code Scanning is disabled during the private bootstrap and enabled automatically when public | Fix or dismiss the finding in a follow-up PR; document false positives in the PR. |
| Gitleaks (CLI) | Any finding fails the job | Rotate the secret, purge history if needed, then add a documented allow rule only for false positives. |
| SBOM + Trivy image scan | **CRITICAL** and **HIGH** fail (`ignore-unfixed: true`) | Add CVE lines to [`.trivyignore`](../.trivyignore) with advisory URL + review date. |

The 6 September 2026 production audit (`56ee35d`) reported three `dev:true` advisories. `npm audit --omit=dev` was clean: they were not reachable in the FastAPI/SPA runtime and were not treated as S3 data compromise. Targeted `package.json` overrides (not `npm audit fix --force`) raised every lockfile copy:

| Package | Introduced by | Was | Now | Exposure |
| --- | --- | --- | --- | --- |
| brace-expansion | `eslint-plugin-react` → minimatch 3; `typescript-eslint`; `shadcn` → ts-morph | 1.1.16, 2.1.2, 5.0.8 | 1.1.18, 2.1.4, 5.0.9 | build |
| nanoid | `shadcn` → postcss | 3.3.16 | 3.3.18 | build |
| qs | `shadcn` → MCP SDK → express/body-parser | 6.15.3 | 6.16.0 | build |

### Issue #405: retire the deepmerge/minimatch exceptions

The full and runtime-only audits are clean after the targeted #405 cleanup;
`.github/npm-audit-exceptions.json` has no remaining exceptions. This does not
weaken either the strict or baseline gate:

- Remove only the installed **shadcn CLI**, not the vendored UI components or
  design system. Its exact `4.22.1` stylesheet is imported locally from
  `frontend/src/vendor/shadcn/tailwind.css`, with npm integrity, upstream commit,
  SHA-256 and the original MIT notice documented alongside it. An identical
  public license ships at `/licenses/shadcn.txt`; the production-build job
  compares the emitted notice to the source license. This removes
  deepmerge (no patched upstream release) and ts-morph from the dependency graph.
  [ADR 0008](adr/0008-tailwind-shadcn-design-system.md) and the
  [vendoring guide](../frontend/src/vendor/shadcn/README.md) describe updates and
  explicit, disposable CLI use for contributors.
- Override **only `minimatch@10`** to `^11.0.4`. The current advisory affects
  `>=7 <11.0.1`; safe minimatch 3/5 consumers keep their existing callable API
  and versions. Do not apply a blanket minimatch override. The remaining 10.x
  consumer is typescript-eslint's estree parser; its actual CommonJS named
  `minimatch(filePath, pattern, { dot: true })` seam is compatible with 11.x.
  Both support this project's Node range (`^20.19.0 || >=22.12.0`). This is an
  explicitly tested cross-major exception, not a claim that every 10.x consumer
  is compatible. Revisit/remove it when typescript-eslint adopts a patched
  release natively.
- Remove unused CLI-only overrides, retain patched floors for every remaining
  copy, and leave unrelated dependencies unchanged. Structural tests do not
  require removed packages to reappear just to test their floors.

Deterministic regressions in `tests/test_ci_contracts.py` cover the installed
lockfile graph, scoped override and retired advisory IDs. The targeted
`frontend/tests/npm-toolchain.test.ts` exercises the **real parser project
service** allowDefaultProject path (dotfiles, braces, nested paths and negative
matches), consumer-resolved CommonJS APIs, exact CSS/license hashes, provenance
and application import wiring. Tests never fetch packages. Full build/browser
verification remains in PR CI.

To reproduce the strict audit proof (no `--baseline-full`), save reports in your
session folder and run:

```bash
cd frontend
npm ci
npm audit --json > ../sessioni/<session>/audit-full.json
npm audit --omit=dev --json > ../sessioni/<session>/audit-runtime.json
node scripts/npm-audit-gate.mjs \
  --full ../sessioni/<session>/audit-full.json \
  --prod ../sessioni/<session>/audit-runtime.json \
  --exceptions ../.github/npm-audit-exceptions.json
npx vitest run tests/npm-toolchain.test.ts
```

### Why pull requests only fail on new findings

Advisories are published independently of our changes. With a strict gate, a new
advisory turned `main` and every open Dependabot PR red at once, and a required
`Dependency review (pip-audit)` result computed against an old base kept
auto-merge blocked until someone refreshed each branch. The audits therefore run
in two modes:

- **Pull requests (baseline mode):** the same audit runs on the base branch's
  lockfile/requirements. Findings the base already has are listed as
  pre-existing and never fail the PR; only findings the change introduces fail.
  Unused or expired npm exceptions are not a PR's concern either.
- **`main` and the daily schedule (strict):** every unexcepted finding fails the
  job, which is how drift stays visible. [`scripts/ci_audit_issue.sh`](../scripts/ci_audit_issue.sh)
  keeps one open issue per audit titled `Security audit failing on main: <audit>`
  (created or refreshed on failure, closed when the audit passes again), so the
  drift is triaged on its own instead of on unrelated PRs.

A strict failure therefore never blocks a merge by itself. Fix it with a targeted
lockfile/override bump, or a dated exception, in a dedicated PR. Contract tests
pin override floors (not exact versions) so a newer patched release does not
break them.

Dependabot (`.github/dependabot.yml`) opens weekly update PRs for pip, Actions,
Docker, and npm in `/frontend`. Minor and patch bumps in each ecosystem are
grouped into one PR (`open-pull-requests-limit` is 3 so a grouped PR can sit
beside at most two majors). [Dependabot maintenance](../.github/workflows/dependabot-maintenance.yml)
queues minor, patch, and security updates (including advisories that omit
`update-type`) for squash auto-merge; branch protection and all required checks
still gate each merge. Major updates remain manual and ungrouped. The workflow
also squash-merges any eligible PR that is already `CLEAN`, since `--auto` is
refused for a PR that is mergeable. The branch protection on `main` does not
require up-to-date branches, so auto-merge fires as soon as a PR's own checks
pass and nothing has to rebase the others after each merge. The workflow does
not ask Dependabot to rebase: a `@dependabot rebase` comment posted with
`GITHUB_TOKEN` is refused ("only users with push access can use that command"),
and a `GITHUB_TOKEN` `update-branch` merge would start CI as
`github-actions[bot]`. When a PR's checks failed on an old base (for example
after a fix to a red gate on `main`), comment `@dependabot rebase` yourself to
rerun them. A 15-minute cron is only a backstop: GitHub often delays
high-frequency schedules. CodeQL `init` and `analyze` updates are grouped
because those steps must use exactly the same version.

The privileged maintenance workflow runs only from the trusted default branch
and never checks out dependency-PR code. `workflow_run` and
`pull_request_target` remain forbidden because both can cross a privileged
boundary based on pull-request activity. `tests/test_ci_contracts.py` enforces
these constraints.

## Local commands

Follow [AGENTS.md](../AGENTS.md) and [epic #410](https://github.com/PaoloDelCasale/frostvault/issues/410):
run only targeted modules locally, open a draft PR early, and obtain full
Python/migration/S3/frontend/Playwright/build evidence from PR CI. Do not repeat
the full workflows locally. Missing `TEST_POSTGRES_URL` is an error for database
tests, **not skipped PostgreSQL coverage**. Database development, when explicitly
needed, requires a disposable PostgreSQL 16 instance as documented in AGENTS.md;
never point tests at a production service.

```bash
# From the repository root: use the project's Python 3.12 virtual environment.
.venv/bin/python -m pip install -r requirements.txt
# Choose only the module(s) relevant to the change; this example needs no DB/S3.
.venv/bin/python -m unittest tests.test_s3_upload -v

# When the API changes, export without starting a server, then regenerate types.
.venv/bin/python scripts/export_openapi.py frontend/openapi.json
cd frontend
npm ci
npm run generate:api
git diff -- openapi.json src/api/openapi.generated.ts src/api/types.ts
# A targeted frontend example, not the full Vitest suite:
npx vitest run src/api/vault.test.ts
```

Push and inspect `gh pr checks <pr>`; diagnose failed jobs with
`gh run view <run-id> --log-failed`. Require SUCCESS for every applicable check
on the exact final HEAD, not an earlier commit, a skipped job or a local subset.
The S3 job's historical MinIO name refers to moto, not live AWS/Glacier proof.
The essential shell has no demo fetch shim or fake archive screenshot mode:
PR CI exercises disposable browser fixtures at 375px and 1280px. Authentication-only
captures remain in `screenshot-auth-pages.mjs`.
