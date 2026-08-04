## Agent skills

### Issue tracker

Issues live on GitHub Issues. See docs/agents/issue-tracker.md.
Before working on a referenced issue, read its parent, native dependencies, and
relevant sibling issues so the implementation does not contradict roadmap
decisions made elsewhere.
Creating an issue does not require adding it to a GitHub Project. When the
issue explicitly belongs to a Project, add it and set the applicable project
fields. Set native parent and dependency relationships whenever applicable.

### Triage labels

Uses default labels: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See docs/agents/triage-labels.md.

### Domain docs

Single-context: CONTEXT.md at the repo root; ADRs in docs/adr/. See docs/agents/domain.md.

## Cursor Cloud specific instructions

Python 3.12 FastAPI app ("FrostVault"). The startup update script creates
a virtualenv at `.venv/` and installs `requirements.txt` into it. Use `.venv/bin/python`.

### Tests
- Python: `.venv/bin/python -m unittest discover -s tests -v` (this is what CI runs).
- Frontend: `cd frontend && npm ci && npm run lint && npm run test` (Vitest).
  Playwright e2e: `cd frontend && npm run test:e2e` (requires `npm run build` and Chromium).
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
  `BOOTSTRAP_VAULT_SOURCE_ROOT` pointing at a directory under the fixed `/sources`
  layout (or set `FROSTVAULT_TEST_SOURCES_ROOT` for native/test seams). Empty Vaults
  use `/sources/managed/<uuid>`; custom volumes must be direct mounts under
  `/sources/<alias>` (nested mounts unsupported), and `BOOTSTRAP_ADMIN_PASSWORD` needs >= 12 chars.
- With `AUTO_MIGRATE=1` (default), schema upgrades run on app start (fresh →
  `alembic upgrade head`; existing behind HEAD → pre-upgrade backup then
  alembic). Set `AUTO_MIGRATE=0` for manual control, then:
  `.venv/bin/python -m app.backup_upgrade` (or `alembic upgrade head` for empty
  local DBs). The app still refuses to serve on a stale schema
  (`HEAD_SCHEMA_REVISION`).
- Build the SPA first: `cd frontend && npm ci && npm run build`.
- Start: `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080`
  (serves `frontend/dist`; missing dist returns HTTP 503 with build instructions).
- Local break-glass admin login is configured through `BOOTSTRAP_ADMIN_*` in the
  Git-ignored `.env`; never commit real credentials.

### Frontend (React SPA)

The UI is a Vite + React SPA. uvicorn always serves `frontend/dist` for HTML
routes (hashed `/assets/*` → `Cache-Control: public, max-age=31536000, immutable`;
`index.html` → `no-store`). Missing `dist` returns HTTP 503 with build
instructions. Image builds produce `dist`; locally run
`cd frontend && npm ci && npm run build` before pointing a browser at uvicorn.

**Local SPA development (Vite + uvicorn):**

```bash
# terminal 1 — API (Host validation: leave ALLOWED_HOSTS empty locally, or
# include 127.0.0.1; the Vite proxy uses changeOrigin so FastAPI sees Host
# 127.0.0.1:8080 — a misconfigured proxy yields "Host not allowed" on login)
set -a && . ./.env && set +a
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080

# terminal 2 — SPA with /api, /auth, /login proxied to uvicorn
cd frontend && npm ci && npm run dev
```

Open the Vite URL (default `http://127.0.0.1:5173`). For the production serving
path without Docker: build `frontend/dist`, then open `http://127.0.0.1:8080`.

### Non-obvious gotchas
- AWS/rclone are placeholders locally. Local file cataloging (scan + browse) works without
 them; only upload/recover/free-space actually call AWS. The background cloud scan logs a
 benign `Rclone configuration not found` error locally — this is expected, not a failure.
- With placeholder AWS creds a red UI toast `Policy log reconciliation: The S3 bucket name
 is not configured` can appear; it is the same expected placeholder behavior, not a failure.
