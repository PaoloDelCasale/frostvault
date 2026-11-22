# Development

Native development, tests, frontend workflow, CI and localization.

## Python environment and tests

Create a virtual environment and install the pinned dependencies; the app
initializes an empty database on start (`alembic upgrade head` does the same):

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# PostgreSQL 16 must already be running (native setup: AGENTS.md).
# .env is not auto-loaded; keep credentials in this Git-ignored file.
set -a; . ./.env; set +a
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m app.server --host 127.0.0.1 --port 8080
```

On Windows PowerShell, use `.venv\Scripts\python.exe` and set environment
variables with `$env:NAME = "value"`.

## Frontend

The UI is a React SPA. The container image builds `frontend/dist` in a Node
stage; uvicorn always serves it for HTML routes (hashed assets are cached
immutably; `index.html` is `no-store`). For a native run, build first:
`cd frontend && npm ci && npm run build`.

For day-to-day SPA work, run uvicorn and the Vite dev server together. The Vite
proxy forwards `/api`, `/auth`, and `/login` to `http://127.0.0.1:8080` with
`changeOrigin: true` so the Host FastAPI sees is `127.0.0.1` (see ADR-0005).
Leave `ALLOWED_HOSTS` empty locally, or list `127.0.0.1`; a wrong Host yields
hard-to-diagnose login failures.

```bash
# terminal 1
set -a; . ./.env; set +a
.venv/bin/python -m app.server --host 127.0.0.1 --port 8080

# terminal 2
cd frontend && npm ci && npm run dev
```

Open the Vite URL (default `http://127.0.0.1:5173`). To exercise the production
serving path without Docker: `cd frontend && npm run build`, then start uvicorn
and open `http://127.0.0.1:8080`.

Open a draft PR early and use its CI for complete Python, PostgreSQL/migration,
S3, Playwright, frontend and image verification (#410). Locally run only targeted
modules, for example:

```bash
.venv/bin/python -m unittest tests.test_database -v
```

All database tests require `TEST_POSTGRES_URL` pointing at an **isolated**
PostgreSQL 16 server with a CREATEDB role. They create uniquely named databases
and clone a template created once by the real `0001_baseline` Alembic run.
Missing configuration fails rather
than skipping database regressions. S3-compatible cases additionally require
`TEST_S3_ENDPOINT`. See [docs/ci.md](ci.md).

## Local development with PostgreSQL

The local configuration uses:

- a dedicated PostgreSQL 16 catalog (never a production database);
- a test source at `local-data/sources/test`;
- a real AWS bucket restricted to the `development/test` prefix;
- credentials read only from the Git-ignored `.env` file.

Local setup:

1. Provision a disposable PostgreSQL 16 server and database. For native/cloud
   development without Docker, follow the isolated `initdb` / `pg_ctl` procedure
   in [AGENTS.md](../AGENTS.md). Use a user-owned cluster, not a system service.
2. Copy `.env.local.example` to `.env` if absent; configure `PGHOST`, `PGPORT`,
   `PGDATABASE`, `PGUSER` and database authentication. For cloud tests also set:

   ```text
   AWS_ACCESS_KEY_ID=...
   AWS_SECRET_ACCESS_KEY=...
   AWS_DEFAULT_REGION=...
   ```

3. Never send AWS keys in chat or commit the `.env` file.
4. Use dedicated IAM credentials restricted to the test bucket/prefix, without
   delete permissions when cloud deletion is not being tested. Never use account
   root credentials.
5. Run the [fresh `./data` preflight](deployment.md#compose-identity-and-fresh-host-data-directory),
   then pull the published image and start the application (an empty database
   is initialized on first start):

   ```bash
   docker compose pull
   docker compose up -d
   ```

   Compose uses `ghcr.io/paolodelcasale/frostvault:latest`. Developers who need
   to change the image can still build from the `Dockerfile` locally.
While `REPLACE...` placeholder values remain, the application blocks AWS calls
instead of trying other credentials available on the computer.

## Localization

The UI defaults to English and includes a complete Italian translation. You
can switch language from the archive header or the sign-in page; the preference
is stored in the `frostvault_locale` cookie. Contributor guidance for catalogs and
critical-key checks lives in `docs/translation-workflow.md`.

## Continuous integration

Pull-request CI is deterministic and does not need AWS credentials: unit tests,
PostgreSQL migrations, frontend JS tests, and S3-compatible integrity proofs
against a local versioned S3 server.
A separate optional manual workflow uses GitHub OIDC against a prefix-scoped IAM
role for real AWS digest checks. See [docs/ci.md](ci.md) for job status,
cleanup reruns, and security scanner severity gates.
