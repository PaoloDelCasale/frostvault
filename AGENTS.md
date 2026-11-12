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

## Native / cloud development (PostgreSQL 16)

Python 3.12 FastAPI app ("FrostVault"). Create `.venv/`, install
`requirements.txt`, and use `.venv/bin/python`.

### Database rebuild (v0.6, epic #578)
The database is being rebuilt from scratch ([ADR-0035](docs/adr/0035-database-rebuilt-from-scratch.md)).
Until #598 closes, work for this epic lands on `main`; the app and full CI may
stay broken in between. Each PR keeps its own targeted tests green and leaves
the full gates to #598.
- The schema is the single Alembic revision `0001_baseline` (#601): add or change
  tables by editing it in place and recreate the local database. Migrations,
  compatibility code and fallbacks to the v0.5.x tables are out of scope.
- When the Owner recreates the database for real use, `0001_baseline` freezes
  and ordinary linear Alembic migrations resume.

### Tests
- Locally run only targeted modules: `.venv/bin/python -m unittest tests.test_<module> -v`.
  Full Python/migration/S3/frontend/build verification belongs to PR CI (#410).
  Open a draft PR early, push, inspect `gh run view <id> --log-failed`, and fix
  until every applicable check is green.
- Frontend locally: `cd frontend && npx vitest run <file>` for a targeted change.
  Full lint/Vitest, Playwright, frontend and image builds run in PR CI.
- Database tests require `TEST_POSTGRES_URL` pointing at a disposable PostgreSQL 16
  server with a CREATEDB role (see `.github/workflows/migrations.yml`). Fixtures
  create uniquely named databases from a migrated template; never use a production
  server. Missing configuration is an error, not skipped database coverage.
- S3-compatible integrity tests run only when `TEST_S3_ENDPOINT` is set (MinIO job in CI).
  Contributor-facing CI status: `docs/ci.md`.
- There is no Python linter configured; the frontend has ESLint.

### Running the app locally (no Docker)
PostgreSQL is the only backend, including native development and tests. Docker
is optional. With PostgreSQL 16 binaries already on PATH, run an isolated cluster
as your unprivileged OS user (not root):

```bash
# Use your own session folder; keep the server off public interfaces.
SESSION="$PWD/sessioni/$(date +%F)-postgres-dev"
mkdir -p "$SESSION"
# Trust is for this disposable, loopback-only cluster with no real data.
# Choose an unused port, never the port of an existing database service.
initdb -D "$SESSION/pgdata" -U frostvault --auth=trust
pg_ctl -D "$SESSION/pgdata" -l "$SESSION/postgres.log" \
  -o "-h 127.0.0.1 -p 55432 -k ''" -w start
export PGHOST=127.0.0.1 PGPORT=55432 PGUSER=frostvault PGDATABASE=frostvault
createdb frostvault
createdb frostvault_test
export TEST_POSTGRES_URL="postgresql+psycopg://frostvault@127.0.0.1:55432/frostvault_test"
# After targeted tests / native development:
# pg_ctl -D "$SESSION/pgdata" -m fast -w stop
```

Cloud agents without Docker need PostgreSQL 16 server and client tools
(`initdb`, `pg_ctl`, `pg_dump`, `pg_restore`, `createdb`, `dropdb`, `psql`). Use
preinstalled binaries or a user-owned installation; ask when unavailable rather
than using a live database or changing system packages. Root agents must run the
isolated process under an existing unprivileged account, with access limited to
that session directory. Do not start/restart a system PostgreSQL service.

- The app does NOT auto-load `.env` (`app/config.py` reads `os.getenv` at import), so export
  the vars yourself. A local `.env` (gitignored) is used for this; load it with
  `set -a && . ./.env && set +a`. If it is missing, recreate it from `.env.local.example`,
  and quote values containing spaces (e.g. `BOOTSTRAP_ADMIN_DISPLAY_NAME="Local Admin"`)
  or `source` will fail.
- `.env.local.example` uses container-style paths (`/data/...`, `/sources/...`). For a native run, keep `PG*` pointed at your isolated cluster and repoint filesystem paths under
  the session folder and create the dirs first, e.g. `METADATA_BACKUP_DIR=$SESSION/backups`
  (set `FROSTVAULT_TEST_SOURCES_ROOT` to relocate the fixed `/sources` layout for
  native/test seams). Vaults are created from the UI; empty Vaults
  use `/sources/managed/<uuid>`; custom volumes must be direct mounts under
  `/sources/<alias>` (nested mounts unsupported), and `BOOTSTRAP_ADMIN_PASSWORD` needs >= 12 chars.
- Schema on start: an empty database is initialized with `alembic upgrade head`;
  a nonempty database at another revision, or without one, is refused before
  any write (ADR-0035). After a change to `0001_baseline`, drop and recreate
  your local database (`dropdb frostvault && createdb frostvault`). Until #601
  lands, the v0.5.x chain (`AUTO_MIGRATE`, `app.backup_upgrade`) is still in
  the code; #601 removes it.
- Start: `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080`
  (serves `frontend/dist`; missing dist returns HTTP 503 with build instructions).
- Local break-glass admin login is configured through `BOOTSTRAP_ADMIN_*` in the
  Git-ignored `.env`; never commit real credentials.

### Frontend (React SPA)

The UI is a Vite + React SPA. uvicorn always serves `frontend/dist` for HTML
routes (hashed `/assets/*` → `Cache-Control: public, max-age=31536000, immutable`;
`index.html` → `no-store`). Missing `dist` returns HTTP 503 with build
instructions. Image builds produce `dist`; use Vite for ordinary local work.
Build locally only when explicitly testing production serving is requested.

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
path without Docker (when explicitly requested): build `frontend/dist`, then open
`http://127.0.0.1:8080`.

### Non-obvious gotchas
- AWS credentials are placeholders locally. Local file cataloging (scan + browse)
 works without them; cloud transfers, verification and scans require valid S3
 configuration. Missing credentials/bucket errors are expected with placeholders.
- With placeholder AWS creds a red UI toast `Policy log reconciliation: The S3 bucket name
 is not configured` can appear; it is the same expected placeholder behavior, not a failure.
