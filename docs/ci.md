# Continuous integration

This repository uses layered CI so pull requests stay deterministic while still
proving upload and recovery integrity against object storage.

## Pull request and push to `main`

Workflow: [`.github/workflows/migrations.yml`](../.github/workflows/migrations.yml)

| Job | What it proves | Credentials |
| --- | --- | --- |
| Unit and migration tests | Python `unittest` suite against SQLite, partitioned across twelve parallel shards by packing largest test modules first so slow files do not land together, then reported through one stable aggregate check. PostgreSQL-only cases skip here and run in the parallel job below. | None. Live AWS/MinIO env vars are intentionally unset so S3 integration cases skip. |
| PostgreSQL migration and concurrency tests | PostgreSQL-specific migration and shared rate-limit concurrency cases against `postgres:16`. | Ephemeral Postgres service only. |
| Frontend generate and typecheck | Regenerates the committed OpenAPI TypeScript artifacts from `frontend/openapi.json`, fails on generated-artifact drift, then runs the strict browser/Node/Vitest/Playwright TypeScript projects. | None. |
| Frontend lint and unit tests | ESLint (including `e2e/`) and Vitest on Node 22, in parallel with typecheck and the production build. | None. |
| Frontend unit tests (Node 24) | The same Vitest suite on Node 24, which `frontend/package.json` engines already allow. Guards Blob/Response/WebCrypto download checks that can pass on Node 22 and fail on jsdom+Node 24. | None. |
| Frontend production build | Vite production build only (`npm run build:ci`); TypeScript is already checked in the parallel typecheck job. | None. |
| Playwright e2e (mobile-375 / desktop-1280) | Chromium Playwright against uvicorn + SQLite with seeded fixtures, split into two parallel projects: 375×667 and 1280×800. | None. Placeholder AWS env only; no live cloud calls. Browsers cached under `~/.cache/ms-playwright`. Uses `E2E_PYTHON=python` (setup-python on PATH; no repo `.venv` in CI). Failure screenshots upload as `playwright-e2e-failures-<project>`; successful 375px shots as `playwright-e2e-375px`. |
| Production image PostgreSQL backup | Builds the production Docker image, checks `pg_dump`/`pg_restore`/`createdb`/`dropdb`/`psql`, then runs Alembic + `backup_upgrade --skip-upgrade` + isolated restore verification against `postgres:16` | Ephemeral Postgres service only. |
| S3-compatible integrity (MinIO) | Real Rclone + MinIO upload/recovery SHA-256 proofs (plain, crypt, empty, Unicode, multipart cutoff) plus prefix cleanup | Ephemeral MinIO from `quay.io/minio/minio` (`minioadmin`). No AWS account. Docker Hub `minio/minio` denies anonymous pulls. |

Failed MinIO cleanup writes `artifacts/s3-cleanup-report.json` and uploads it as a
workflow artifact. Rerun cleanup locally or in CI with:

```bash
export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_ENDPOINT_URL=http://127.0.0.1:9000
python -m app.services.s3_prefix_cleanup_cli \
  --bucket archive-ci \
  --prefix ci-runs \
  --prefix ci-crypt \
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
  SQLite aggregate, PostgreSQL 16, MinIO S3-compatible integrity, frontend
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
| Dependency review via pip-audit | Fails on any known vulnerability in `requirements.txt` | Temporarily add `--ignore-vuln` IDs with a linked issue URL and review date. Native `actions/dependency-review-action` needs Dependency graph + GitHub Advanced Security on private repos; pip-audit is the portable substitute for this Python stack. |
| Dependency review via npm audit | Fails on any known vulnerability in the `frontend/` lockfile, including transitive copies and `dev` toolchain packages. A second `npm audit --omit=dev` report classifies **runtime** vs **build** exposure. JSON reports upload as the `npm-audit` artifact. The production image only copies `frontend/dist`, so Trivy on the final image does not replace this gate. | Add a GHSA/CVE to [`.github/npm-audit-exceptions.json`](../.github/npm-audit-exceptions.json) with `reason`, GitHub issue URL, `exposure` (`runtime` or `build`), and `review_by`. Expired, unused, or build-classified runtime findings fail the job. Do not use `npm audit fix --force`. |
| CodeQL | SARIF artifact + job fails on error/high/critical findings; upload to GitHub Code Scanning is disabled during the private bootstrap and enabled automatically when public | Fix or dismiss the finding in a follow-up PR; document false positives in the PR. |
| Gitleaks (CLI) | Any finding fails the job | Rotate the secret, purge history if needed, then add a documented allow rule only for false positives. |
| SBOM + Trivy image scan | **CRITICAL** and **HIGH** fail (`ignore-unfixed: true`) | Add CVE lines to [`.trivyignore`](../.trivyignore) with advisory URL + review date. |

The 6 September 2026 production audit (`56ee35d`) reported three `dev:true` advisories. `npm audit --omit=dev` was clean: they were not reachable in the FastAPI/SPA runtime and were not treated as S3 data compromise. Targeted `package.json` overrides (not `npm audit fix --force`) raised every lockfile copy:

| Package | Introduced by | Was | Now | Exposure |
| --- | --- | --- | --- | --- |
| brace-expansion | `eslint-plugin-react` → minimatch 3; `vite-plugin-pwa` → workbox-build → filelist/glob; `typescript-eslint`; `shadcn` → ts-morph | 1.1.16, 2.1.2, 5.0.8 | 1.1.18, 2.1.4, 5.0.9 | build |
| nanoid | `shadcn` → postcss | 3.3.16 | 3.3.18 | build |
| qs | `shadcn` → MCP SDK → express/body-parser | 6.15.3 | 6.16.0 | build |

Dependabot (`.github/dependabot.yml`) opens weekly update PRs for pip, Actions,
Docker, and npm in `/frontend`. Minor and patch bumps in each ecosystem are
grouped into one PR (`open-pull-requests-limit` is 3 so a grouped PR can sit
beside at most two majors). [Dependabot maintenance](../.github/workflows/dependabot-maintenance.yml)
queues minor, patch, and security updates (including advisories that omit
`update-type`) for squash auto-merge; branch protection and all required checks
still gate each merge. Major updates remain manual and ungrouped. The workflow refreshes
open Dependabot PRs against `main`, squash-merges any that are already `CLEAN`,
and comments `@dependabot rebase` on remaining behind heads. A GITHUB_TOKEN
`update-branch` merge would start CI as `github-actions[bot]`, which sits in
`action_required` until a human approves it; Dependabot's own rebase push is
trusted and starts checks immediately. A 15-minute cron is only a backstop:
GitHub often delays high-frequency schedules. CodeQL `init` and `analyze`
updates are grouped
because those steps must use exactly the same version.

The privileged maintenance workflow runs only from the trusted default branch
and never checks out dependency-PR code. `workflow_run` and
`pull_request_target` remain forbidden because both can cross a privileged
boundary based on pull-request activity. `tests/test_ci_contracts.py` enforces
these constraints.

## Local commands

```bash
# Unit suite (same as PR job without Postgres service — PG tests skip)
.venv/bin/python -m unittest discover -s tests -v

# PostgreSQL-only suite (requires TEST_POSTGRES_URL)
.venv/bin/python -m unittest \
  tests.test_lookup_rate_limit.PostgreSQLSharedLookupRateLimitTests \
  tests.test_migrations_postgresql.PostgreSQLMigrationTests -v

# Export OpenAPI without a running server (from the repository root)
.venv/bin/python scripts/export_openapi.py frontend/openapi.json

# Frontend SPA, generated API artifacts, and strict environment typechecks (from frontend/)
npm ci
npm run generate:api
git diff --exit-code -- openapi.json src/api/openapi.generated.ts src/api/types.ts
npm run typecheck
npm run lint
npm run test
npm run build
# npm audit gate (same reports the security workflow uploads as artifacts)
mkdir -p ../artifacts
npm audit --json > ../artifacts/npm-audit.json || true
npm audit --omit=dev --json > ../artifacts/npm-audit-prod.json || true
node scripts/npm-audit-gate.mjs \
  --full ../artifacts/npm-audit.json \
  --prod ../artifacts/npm-audit-prod.json \
  --exceptions ../.github/npm-audit-exceptions.json
# Generated artifacts are committed; CI fails if the schema or TypeScript drifts.
# `npm run lint` includes e2e/; `npm run typecheck` covers browser, Node, Vitest, and Playwright projects.

# Playwright e2e (requires a built frontend/dist and Chromium)
npx playwright install chromium
npm run test:e2e

# Capture-only archive screenshots (375px; demo seams require explicit opt-in)
VITE_ALLOW_DEMO=1 npm run build
node scripts/capture-file-browser-screenshots.mjs
node scripts/capture-file-operations-screenshots.mjs
node scripts/capture-pwa-offline-screenshot.mjs
node scripts/capture-storage-class-screenshots.mjs

# Other 375px capture scripts use mocked/seeded API routes and need no demo flag:
# capture-vault-access-375.mjs, screenshot-admin.mjs, screenshot-auth-pages.mjs

# MinIO integrity (requires a local MinIO on :9000 and rclone on PATH)
export AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin
export AWS_DEFAULT_REGION=us-east-1
export AWS_ENDPOINT_URL=http://127.0.0.1:9000
export TEST_S3_ENDPOINT=http://127.0.0.1:9000 TEST_S3_BUCKET=archive-ci
.venv/bin/python -m unittest tests.test_s3_integrity_integration -v
```
