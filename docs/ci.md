# Continuous integration

This repository uses layered CI so pull requests stay deterministic while still
proving upload and recovery integrity against object storage.

## Pull request and push to `main`

Workflow: [`.github/workflows/migrations.yml`](../.github/workflows/migrations.yml)

| Job | What it proves | Credentials |
| --- | --- | --- |
| Unit and migration tests | Python `unittest` suite against SQLite, partitioned deterministically by test module across four parallel shards and reported through one stable aggregate check. PostgreSQL-only cases skip here and run in the parallel job below. | None. Live AWS/MinIO env vars are intentionally unset so S3 integration cases skip. |
| PostgreSQL migration and concurrency tests | PostgreSQL-specific migration and shared rate-limit concurrency cases against `postgres:16`. | Ephemeral Postgres service only. |
| Frontend lint, unit tests, and build | ESLint, Vitest, TypeScript and the production Vite/PWA build. Runs in parallel with both Python jobs. | None. |
| Playwright e2e (375px + desktop) | Chromium Playwright against uvicorn + SQLite with seeded fixtures: eleven archive/admin flows plus touch/a11y checks at 375×667 and 1280×800 | None. Placeholder AWS env only; no live cloud calls. Browsers cached under `~/.cache/ms-playwright`. Uses `E2E_PYTHON=python` (setup-python on PATH; no repo `.venv` in CI). Failure screenshots upload as `playwright-e2e-failures`; successful 375px shots as `playwright-e2e-375px`. |
| Production image PostgreSQL backup | Builds the production Docker image, checks `pg_dump`/`pg_restore`/`createdb`/`dropdb`/`psql`, then runs Alembic + `backup_upgrade --skip-upgrade` + isolated restore verification against `postgres:16` | Ephemeral Postgres service only. |
| S3-compatible integrity (MinIO) | Real Rclone + MinIO upload/recovery SHA-256 proofs (plain, crypt, empty, Unicode, multipart cutoff) plus prefix cleanup | Ephemeral MinIO only (`minioadmin`). No AWS account. |

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
- Publishes `ghcr.io/paolodelcasale/frostvault` (`latest` on `main`, semver on
  tags, plus a short `sha-` tag).
- Compose files pull this image; operators do not need a local image build for a
  standard deploy.

## Repository security scanners

Workflow: [`.github/workflows/security.yml`](../.github/workflows/security.yml)

| Scanner | Gate | Exceptions |
| --- | --- | --- |
| Dependency review via pip-audit | Fails on any known vulnerability in `requirements.txt` | Temporarily add `--ignore-vuln` IDs with a linked issue URL and review date. Native `actions/dependency-review-action` needs Dependency graph + GitHub Advanced Security on private repos; pip-audit is the portable substitute for this Python stack. |
| CodeQL | SARIF artifact + job fails on error/high/critical findings; upload to GitHub Code Scanning is disabled during the private bootstrap and enabled automatically when public | Fix or dismiss the finding in a follow-up PR; document false positives in the PR. |
| Gitleaks (CLI) | Any finding fails the job | Rotate the secret, purge history if needed, then add a documented allow rule only for false positives. |
| SBOM + Trivy image scan | **CRITICAL** and **HIGH** fail (`ignore-unfixed: true`) | Add CVE lines to [`.trivyignore`](../.trivyignore) with advisory URL + review date. |

Dependabot (`.github/dependabot.yml`) opens weekly update PRs for pip, Actions, and
Docker. **Auto-merge is disabled**; humans review and merge.

`workflow_run` and `pull_request_target` remain forbidden: both hand a privileged
token to a workflow chosen by pull-request activity.
`tests/test_ci_contracts.py` enforces that.

## Local commands

```bash
# Unit suite (same as PR job without Postgres service — PG tests skip)
.venv/bin/python -m unittest discover -s tests -v

# PostgreSQL-only suite (requires TEST_POSTGRES_URL)
.venv/bin/python -m unittest \
  tests.test_lookup_rate_limit.PostgreSQLSharedLookupRateLimitTests \
  tests.test_migrations_postgresql.PostgreSQLMigrationTests -v

# Frontend SPA (from frontend/)
npm ci && npm run lint && npm run test && npm run build

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
