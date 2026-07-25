# Continuous integration

This repository uses layered CI so pull requests stay deterministic while still
proving upload and recovery integrity against object storage.

## Pull request and push to `main`

Workflow: [`.github/workflows/migrations.yml`](../.github/workflows/migrations.yml)

| Job | What it proves | Credentials |
| --- | --- | --- |
| Unit and migration tests | Python `unittest` suite (SQLite + PostgreSQL migrations) and frontend `node --test` | None. Live AWS/MinIO env vars are intentionally unset so S3 integration cases skip. |
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

## Weekly / manual AWS proofs

Workflow: [`.github/workflows/aws-s3-integrity.yml`](../.github/workflows/aws-s3-integrity.yml)

- Triggers: `workflow_dispatch` and weekly cron (Mondays). **Not** on pull requests.
- Auth: GitHub OIDC → IAM role (no static AWS keys in GitHub secrets).
- Scope: objects under a dedicated prefix only. Provision the role with
  [`infra/terraform/github-oidc-ci/`](../infra/terraform/github-oidc-ci/).
- Required GitHub Actions variables (environment `aws-ci`):
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

## Temporary agent pipeline for epic #56

This infrastructure is deliberately disposable. It has a hard-coded allowlist
containing only epic #56 sub-issues #57–#72 and the self-removal issue #86; the
label alone cannot enrol any other issue.

Three workflows, all driven by
[`.github/scripts/agent_pipeline.py`](../.github/scripts/agent_pipeline.py):

| Workflow | Trigger | What it does |
| --- | --- | --- |
| [Agent pipeline unblock](../.github/workflows/agent-unblock.yml) | `issues: closed` | Labels `ready-for-agent` on every dependent whose blockers are now all closed — only if it carries `agent-pipeline` — and starts a cloud agent on each |
| [Agent pipeline dispatch](../.github/workflows/agent-dispatch.yml) | `workflow_dispatch` | Starts a cloud agent on one issue. Needed for the first issue of a chain |
| [Agent pipeline auto-merge](../.github/workflows/agent-automerge.yml) | Every 10 minutes, plus `workflow_dispatch` | Squash-merges open pull requests that pass every gate below |

Agents are started through the private webhook of the repo-backed
`FrostVault Epic 56 TDD Pipeline` Cursor Automation. The endpoint and Bearer value
live in `CURSOR_EPIC_56_WEBHOOK_URL` and `CURSOR_EPIC_56_WEBHOOK_KEY` Actions
secrets. The model (**Cursor Grok 4.5 High**, non-fast), environment and pull
request capability are fixed in Cursor, so GitHub never holds a personal Cursor
API key.

Auto-merge gates, all required: a same-repo `cursor/*` branch, not a draft, a body
that closes an issue, that issue carrying `agent-pipeline`, a mergeable state, and
**every** check run on the head commit finished and green. A commit with no checks
at all is never merged. `neutral` counts as passing, since `Cursor Bugbot` reports
findings that way.

This does **not** change how human or Dependabot pull requests are handled: a pull
request that does not close an `agent-pipeline` issue is left alone.

After #72 completes, #86 removes the workflows, script, tests and documentation.
The already-running merge process then closes epic #56 and deletes the dedicated
label. GitHub's historical issues and pull requests remain, but no active
automation or repository configuration remains.

A schedule is used rather than reacting to CI finishing because `workflow_run` and
`pull_request_target` are forbidden in this repository — both hand a privileged
token to a workflow chosen by pull-request activity. `tests/test_ci_contracts.py`
enforces that, and `tests/test_agent_pipeline.py` covers the gates.

## Local commands

```bash
# Unit suite (same as PR job without Postgres service — PG tests skip)
.venv/bin/python -m unittest discover -s tests -v
node --test tests/*.mjs

# MinIO integrity (requires a local MinIO on :9000 and rclone on PATH)
export AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin
export AWS_DEFAULT_REGION=us-east-1
export AWS_ENDPOINT_URL=http://127.0.0.1:9000
export TEST_S3_ENDPOINT=http://127.0.0.1:9000 TEST_S3_BUCKET=archive-ci
.venv/bin/python -m unittest tests.test_s3_integrity_integration -v
```
