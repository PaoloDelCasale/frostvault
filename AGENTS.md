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
  the vars yourself. A local `.env` (gitignored) already exists for this; load it with
  `set -a && . ./.env && set +a`. If you recreate it, quote values containing spaces
  (e.g. `BOOTSTRAP_VAULT_NAME="Test Archive"`) or `source` will fail.
- You MUST run migrations before starting: `.venv/bin/python -m alembic upgrade head`.
  The app refuses to start on a stale/unversioned schema (see `HEAD_SCHEMA_REVISION`).
- Start: `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080`.
- Local break-glass admin login is configured through `BOOTSTRAP_ADMIN_*` in the
  Git-ignored `.env`; never commit real credentials.

### Non-obvious gotchas
- AWS/rclone are placeholders locally. Local file cataloging (scan + browse) works without
  them; only upload/recover/free-space actually call AWS. The background cloud scan logs a
  benign `Rclone configuration not found` error locally — this is expected, not a failure.
