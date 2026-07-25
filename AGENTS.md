## Agent skills

### Issue tracker

Issues live on GitHub Issues. See docs/agents/issue-tracker.md.
Before working on a referenced issue, read its parent, native dependencies, and
relevant sibling issues so the implementation does not contradict roadmap
decisions made elsewhere.
When creating an issue, also add it to the repository's GitHub Project and set
its native parent, dependencies, phase, priority, effort, and initial status.

### Triage labels

Uses default labels: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See docs/agents/triage-labels.md.

### Domain docs

Single-context: CONTEXT.md at the repo root; ADRs in docs/adr/. See docs/agents/domain.md.

## Automated agent pipeline

Some issues are worked by cloud agents without a human in the loop. The loop is:

1. An issue carries `agent-pipeline` (it belongs to an automated epic) and gets
   `ready-for-agent` once nothing blocks it.
2. A Cursor Automation triggers on that label and starts a cloud agent, which
   opens a pull request whose body says `Closes #N`.
3. `.github/workflows/agent-automerge.yml` squash-merges that pull request once
   every check run for its head commit is finished and green. Merging closes the
   issue.
4. `.github/workflows/agent-unblock.yml` reacts to the closure: for every issue
   the closed one was blocking, it adds `ready-for-agent` if all of that issue's
   blockers are now closed — which starts the next agent.

Consequences worth knowing:

- **Only `agent-pipeline` issues are touched.** Both workflows check that label,
  so an ordinary issue that merely depends on a pipeline issue stays untouched.
- **Auto-merge means CI is the review.** Nothing else gates a merge, which is why
  pipeline issues carry explicit seams and acceptance criteria in their body.
- **Only `cursor/*` branches are merged**, and only when the pull request closes
  an `agent-pipeline` issue. Human pull requests are never merged automatically.
- The `Cursor Bugbot` check reports findings as `neutral`, not `failure`, so
  requiring it would not block a merge on findings. Do not rely on it as a gate.

To stop the pipeline: remove `ready-for-agent` from the open issues, or remove
`agent-pipeline` from the ones that should wait. Disabling the Cursor Automation
stops new agents but does not stop merges; disabling
`.github/workflows/agent-automerge.yml` stops merges.

Issue dependencies are only writable over REST
(`POST /repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by` with an integer
`issue_id`). GraphQL exposes `blockedBy` and `blocking` read-only; `addSubIssue`
is the only relevant mutation, and it covers parents, not dependencies.

## Cursor Cloud specific instructions

Python 3.12 FastAPI app ("FrostVault"). The startup update script creates
a virtualenv at `.venv/` and installs `requirements.txt` into it. Use `.venv/bin/python`.

### Tests
- Python: `.venv/bin/python -m unittest discover -s tests -v` (this is what CI runs).
- Frontend JS: `node --test tests/*.mjs` (Node's built-in runner, no npm install needed).
- The `*_postgresql` migration tests are skipped unless `TEST_POSTGRES_URL` points at a
  Postgres 16 server (see `.github/workflows/migrations.yml`); everything else runs on SQLite.
- S3-compatible integrity tests run only when `TEST_S3_ENDPOINT` is set (MinIO job in CI).
  Contributor-facing CI status: `docs/ci.md`.
- There is no configured linter (no ruff/flake8/eslint config in the repo).

### Running the app locally (no Docker)
Docker is not installed; run natively with the SQLite backend instead of `docker compose`.
- The app does NOT auto-load `.env` (`app/config.py` reads `os.getenv` at import), so export
 the vars yourself. A local `.env` (gitignored) is used for this; load it with
 `set -a && . ./.env && set +a`. If it is missing, recreate it from `.env.local.example`,
 and quote values containing spaces (e.g. `BOOTSTRAP_ADMIN_DISPLAY_NAME="Local Admin"`,
 `BOOTSTRAP_VAULT_NAME="Test Archive"`) or `source` will fail.
- `.env.local.example` uses container-style paths (`/data/...`, `/sources/...`,
 `/config/rclone/rclone.conf`). For a native run, repoint them at real writable paths under
 the repo and create the dirs first, e.g. `SQLITE_PATH=/workspace/data/frostvault.db`,
 `BOOTSTRAP_VAULT_SOURCE_ROOT=/workspace/local-data/sources/test` (the vault source folder
 must exist for scan/browse to work), and `BOOTSTRAP_ADMIN_PASSWORD` needs >= 12 chars.
- You MUST run migrations before starting: `.venv/bin/python -m alembic upgrade head`.
  The app refuses to start on a stale/unversioned schema (see `HEAD_SCHEMA_REVISION`).
- Start: `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080`.
- Local break-glass admin login is configured through `BOOTSTRAP_ADMIN_*` in the
  Git-ignored `.env`; never commit real credentials.

### Non-obvious gotchas
- AWS/rclone are placeholders locally. Local file cataloging (scan + browse) works without
 them; only upload/recover/free-space actually call AWS. The background cloud scan logs a
 benign `Rclone configuration not found` error locally — this is expected, not a failure.
- With placeholder AWS creds a red UI toast `Policy log reconciliation: The S3 bucket name
 is not configured` can appear; it is the same expected placeholder behavior, not a failure.
