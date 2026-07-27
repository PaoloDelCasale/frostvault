# FrostVault

FrostVault is a self-hosted web application for cataloging files, archiving
versioned copies in Amazon S3 storage classes, verifying uploaded content, safely
freeing local space, and recovering exact versions. It supports multiple users,
isolated vaults, OIDC, encrypted Rclone remotes, and SQLite or PostgreSQL.

The archive works like a file manager: it shows top-level folders, lets users
open them, and provides breadcrumbs for navigating back to parent folders.
Search remains global across every file in the selected vault.

## Users and vaults

- Users sign in with configured OIDC, or with the restricted break-glass
  administrator password when that path is enabled for the client network.
- Each vault has its own local folder, S3 prefix, and Rclone remote.
- Every query and operation includes the vault identifier.
- Users can see only vaults assigned to them.
- The primary `owner` manages sharing and policy and may free local space;
  `operator` can upload and recover files; `viewer` is read-only.
- Queued or active uploads, recoveries, and local cleanup operations can be
  stopped with the **Stop** button shown next to their progress.
- Operations run in parallel. `OPERATION_CONCURRENCY` controls the number of
  simultaneous processes (default: `4`, maximum: `16`). The previous
  `UPLOAD_CONCURRENCY` variable remains supported as a fallback.
- Local cleanup runs in the background and reports progress for each file or
  folder.
- Authenticated users can create their own vaults; administrators retain global
  access with reauthentication for sensitive actions.
- Every vault must retain exactly one primary owner.

Administrators do not automatically enter other users' vaults. They must be
explicitly assigned like any other user before they can browse files.

## Local cleanup safety

- The **Free local space** button appears only when the Local Copy fingerprint
  matches an available, verified Archive Version, for owners, and when
  `ALLOW_LOCAL_DELETE=true`.
- Before deleting the server copy, the application checks the exact S3
  `VersionId`, atomically claims the local file, and rehashes it; object
  existence at the same key or a stale local fingerprint is not sufficient.
- Cleanup deletes local copies only. It never deletes the S3 object.
- Recovery downloads the exact archived S3 `VersionId`, verifies the plaintext
  SHA-256 digest, then atomically replaces the local file. Glacier/Deep Archive
  versions request a restore first (Bulk by default); high-impact restores are
  held for one-hour primary-owner reauthentication before any `RestoreObject`
  call. An accepted S3 restore request cannot be cancelled. Large plain recovers
  use parallel S3 range downloads (`S3_DOWNLOAD_*`); crypt recovers use rclone
  multi-thread streams (`RCLONE_MULTI_THREAD_*`).
- The panel requires a username and password.

## Requirements

- Docker Engine with Docker Compose v2 for the recommended deployment, or
  Python 3.12+ for native development
- An S3 bucket with versioning enabled and a least-privilege IAM principal
- Rclone 1.74.4 or a compatible release when running outside the image
- PostgreSQL 16 for production; SQLite is supported for development and small
  single-node installations
- An HTTPS reverse proxy for any network-accessible deployment

FrostVault never replaces independent backups. Keep the database, the
`ARCHIVE_MASTER_KEY`, and per-vault recovery exports in separate secure
locations.

## Setup

### Local development with SQLite

The local configuration uses:

- a SQLite catalog at `data/frostvault.db`;
- a test source at `local-data/sources/test`;
- a real AWS bucket restricted to the `development/test` prefix;
- credentials read only from the Git-ignored `.env` file.

Local setup:

1. Copy `.env.local.example` to `.env` if the file does not already exist.
2. Edit only these values in your local `.env`:

   ```text
   AWS_ACCESS_KEY_ID=...
   AWS_SECRET_ACCESS_KEY=...
   S3_BUCKET=...
   ```

3. Never send AWS keys in chat or commit the `.env` file.
4. Use dedicated IAM credentials restricted to the test bucket/prefix, without
   `s3:DeleteObject`. Never use account root credentials.
5. Copy `config/rclone.local.conf.example` to `config/rclone.conf`, enter the
   same bucket, and generate the obscured Rclone password.
6. Pull the published image and start the application (schema migrations run
   automatically on start when `AUTO_MIGRATE=1`, the default):

   ```bash
   docker compose pull
   docker compose up -d
   ```

   Compose uses `ghcr.io/paolodelcasale/frostvault:latest`. Developers who need
   to change the image can still build from the `Dockerfile` locally.
   Set `AUTO_MIGRATE=0` to keep upgrades manual (then run
   `docker compose run --rm frostvault python -m app.backup_upgrade` before
   `up`).
While `REPLACE...` placeholder values remain, the application blocks AWS calls
instead of trying other credentials available on the computer.

### Production with PostgreSQL

1. Create the PostgreSQL database and user shown below.
2. Prepare one subfolder for each vault, for example:

   ```text
   /srv/frostvault/sources/photos
   /srv/frostvault/sources/documents
   ```

3. Copy `.env.example` to `.env` and configure paths, bucket, credentials, and
   the bootstrap administrator. For a network deployment set `COOKIE_SECURE=true`
   and provide `ALLOWED_HOSTS` (the hostnames the panel answers to) and
   `TRUSTED_PROXIES` (CIDRs of your reverse proxies); the app refuses to start
   in production without them. Set `OIDC_ENABLED=true` and configure
   `OIDC_ISSUER`/`OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET` for the initial
   environment-defined single sign-on configuration. Set a separate
   `OIDC_SETTINGS_ENCRYPTION_KEY` before managing OIDC through the admin API.

4. Create `config/rclone.conf` from `config/rclone.conf.example`.
5. Generate a distinct encryption password for each vault:

   ```bash
   docker run --rm rclone/rclone:1.74.4 obscure 'ENCRYPTION-PASSWORD'
   ```

6. Put the result in the relevant remote's `password` field in `rclone.conf`.
7. Ensure the bucket, prefix, region, and remote name match the vault settings
   in the administration panel.

Example database creation, run as a PostgreSQL administrator:

```sql
CREATE USER frostvault WITH PASSWORD 'A-LONG-PASSWORD';
CREATE DATABASE frostvault OWNER frostvault;
```

Schema changes are Alembic migrations. On container/app start, `AUTO_MIGRATE=1`
(the default) brings the database to the expected revision: fresh databases run
`alembic upgrade head`; existing databases that are behind take an encrypted
pre-upgrade metadata backup first (see
[docs/metadata-backups.md](docs/metadata-backups.md)), then upgrade. A failed
backup with real backup configuration blocks startup. Set `AUTO_MIGRATE=0` for
CI or fully manual release procedures, then run
`docker compose run --rm frostvault python -m app.backup_upgrade` before
starting a new application release. The application still validates the current
revision after any automatic upgrade and refuses to serve on a stale schema.

The first Alembic revision adopts only a database that exactly matches the
current multi-user release; unknown or historical single-user schemas stop
without modification. Existing cloud catalog rows migrate as unverified and
cannot authorize local cleanup until a later read-back verification resolves
their exact S3 version.

Downgrade is supported only while the target legacy schema can represent all
stored data. Alembic refuses a downgrade that would discard multiple Archive
Versions, Path History, Delete Markers, digests, or verified state; restore a
database backup instead.

If PostgreSQL runs directly on the server or exposes port `5432`, keep
`PGHOST=host.docker.internal`. Otherwise, connect both containers to the same
Docker network and set `PGHOST` to the PostgreSQL service name.

With an Rclone remote using `type = crypt` and `filename_encryption = off`, file
and folder names remain readable in S3 while file contents are encrypted. Rclone
adds `.bin` to object names, and the panel removes that suffix when displaying
them. With an unencrypted remote such as `type = alias`, the panel uses the
original name and does not add or remove `.bin`.

To archive without client-side encryption, point a shared alias at the bucket.
FrostVault appends each vault's `s3_prefix` (`vaults/<uuid>/`) to every plain
object path, so one remote can serve many self-service vaults:

```ini
[frostvault-plain]
type = alias
remote = frostvault-s3:example-bucket
```

In the administration panel, **Rclone remote** must be `frostvault-crypt` for
encrypted mode or `frostvault-plain` for unencrypted mode. Never use the same S3 prefix for both
modes at the same time.

## Run

```bash
docker compose pull
docker compose up -d
docker compose logs -f frostvault
```

Schema migrations run automatically on start (`AUTO_MIGRATE=1` by default). Set
`AUTO_MIGRATE=0` and run `python -m app.backup_upgrade` yourself when you want
an explicit pre-upgrade gate outside the container entrypoint.

The service listens on `127.0.0.1:8080` by default. Use an HTTPS reverse proxy or
Tailscale to make it reachable. Set `COOKIE_SECURE=true` when using HTTPS.

Compose pulls `ghcr.io/paolodelcasale/frostvault:latest` from the GitHub
Container Registry (a standard deploy does not build the image locally).
For production behind Traefik, use `compose.traefik.yaml` so the app is not
published on host ports. See [docs/traefik.md](docs/traefik.md).

The container runs as `PUID`/`PGID` (default `99:100`). Permission the host
source and data directories for that identity; see
[docs/filesystem-permissions.md](docs/filesystem-permissions.md).

On first startup, the application creates the administrator defined by
`BOOTSTRAP_ADMIN_*` and, when configured, the first vault. Manage subsequent
user passwords and vault assignments from the **Administration** page.
Bootstrap variables never overwrite existing users. After the first successful
sign-in, remove `BOOTSTRAP_ADMIN_USERNAME` and `BOOTSTRAP_ADMIN_PASSWORD` from
`.env`.

## Localization

The UI defaults to English and includes a complete Italian translation. Users
can switch language from the archive header or the sign-in page; the preference
is stored in the `frostvault_locale` cookie. Contributor guidance for catalogs and
critical-key checks lives in `docs/translation-workflow.md`.

## Continuous integration

Pull-request CI is deterministic and does not need AWS credentials: unit tests,
PostgreSQL migrations, frontend JS tests, and MinIO-backed S3 integrity proofs.
A separate weekly/manual workflow uses GitHub OIDC against a prefix-scoped IAM
role for real AWS digest checks. See [docs/ci.md](docs/ci.md) for job status,
cleanup reruns, and security scanner severity gates.

## Recommended first smoke test

Before indexing a terabyte of data:

1. use a test source folder containing two unimportant files;
2. select **Refresh list**;
3. upload one file and confirm that a distinct S3 version exists in the bucket;
4. confirm the catalog keeps the version unverified until plaintext read-back
   verification completes;
5. confirm **Free local space** and **Recover** remain unavailable for an
   unverified version;
6. keep `rclone.conf` and the original password in at least two secure places.

The local catalog updates automatically when files under the configured source
are created, modified, renamed, or deleted. **Refresh list** remains available
as a forced synchronization of the filesystem and S3 content. Set
`FILESYSTEM_WATCH_FORCE_POLLING=true` with Docker Desktop on Windows. On a Linux
server, prefer the native watcher with the value `false`.

## Indicative minimum AWS permissions

The application role/user must be able to list the bucket and, within the
configured prefix, read, upload, and request object restores. Do not grant
`s3:DeleteObject`.

Use the Terraform baseline in
[`infra/terraform/archive-bucket/`](infra/terraform/archive-bucket/) or apply
equivalent versioning, encryption, public-access blocking, ownership, and
least-privilege controls. See [docs/aws-s3-bucket.md](docs/aws-s3-bucket.md) for
the full model.

## Configuration

Start from `.env.local.example` for local SQLite use or `.env.example` for a
PostgreSQL deployment. Every value containing `REPLACE` is a placeholder and
must be replaced before the corresponding integration is used. Important groups:

| Area | Variables |
| --- | --- |
| Database | `DB_BACKEND`, `SQLITE_PATH`, `PGHOST`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` |
| Authentication | `BOOTSTRAP_ADMIN_*`, `OIDC_*` (including the dedicated `OIDC_SETTINGS_ENCRYPTION_KEY`), `BREAK_GLASS_ALLOWED_CIDRS` |
| Network | `APP_PORT`, `COOKIE_SECURE`, `ALLOWED_HOSTS`, `TRUSTED_PROXIES` |
| Frontend | `FRONTEND_DIST_DIR` (Vite build output; required for HTML routes) |
| Web Push | `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT` (optional; see below) |
| Storage | `S3_BUCKET`, `VAULT_S3_BUCKET`, `VAULT_RCLONE_*`, `RCLONE_CONFIG` |
| Encryption and backup | `ARCHIVE_MASTER_KEY`, `METADATA_BACKUP_*` |
| Operations | `OPERATION_CONCURRENCY`, `S3_DOWNLOAD_*`, `RCLONE_MULTI_THREAD_*`, `RESTORE_*`, `ALLOW_LOCAL_DELETE` |

Database settings and credentials, paths and mount roots, master/storage
credentials, proxy/host/cookie trust, bootstrap values, automatic migration,
and the frontend distribution path are deployment-only. They cannot be edited
at runtime because they define process and deployment trust boundaries.
Administrators can inspect the effective, structurally redacted inventory at
`GET /api/admin/settings`. Recently reauthenticated administrators can atomically
apply bounded runtime overrides, or remove them to restore environment/default
precedence, with `PATCH /api/admin/settings`. Mutations use the `revision`
returned by `GET` to reject stale writes. See
[ADR-0009](docs/adr/0009-effective-system-configuration.md) for the exhaustive
classification and precedence model.

### Progressive Web App and Web Push

The SPA is an installable PWA (`vite-plugin-pwa`): offline shell, cached last file
listing, and optional push notifications when a Job completes or fails.

**HTTPS precondition.** Web Push and installability require a secure origin.
Behind Traefik that is already true (see [docs/traefik.md](docs/traefik.md)).
Plain HTTP on a LAN will not deliver push; `localhost` / `127.0.0.1` remain
exempt for development. Document this as an operator precondition, not a bug.

**VAPID keys.** Generate a key pair once and set the public/private values:

```bash
.venv/bin/python - <<'PY'
from py_vapid import Vapid
from cryptography.hazmat.primitives import serialization
import base64
v = Vapid()
v.generate_keys()
raw = v.public_key.public_bytes(
    encoding=serialization.Encoding.X962,
    format=serialization.PublicFormat.UncompressedPoint,
)
print(base64.urlsafe_b64encode(raw).decode().rstrip("="))
print(v.private_pem().decode())
PY
```

- `VAPID_PUBLIC_KEY` — URL-safe base64 application server key
- `VAPID_PRIVATE_KEY` — matching private key
- `VAPID_SUBJECT` — contact URI, e.g. `mailto:ops@example.com`

When these are unset or still placeholders, the app works normally: the push
config endpoint reports `configured: false`, subscription POSTs are accepted
without error but not stored as active push, and no delivery attempts run —
the same degrade pattern as placeholder AWS credentials.

Revoking a Session deletes that device’s push subscription. Deliveries also
re-check live Session and Vault membership at send time so a removed member
never receives Job notifications for that Vault.

Do not commit `.env`, `config/rclone.conf`, recovery exports, database files, or
cloud credentials.

## Security

- Expose FrostVault only through HTTPS and configure trusted hosts and proxies.
- Prefer OIDC and short-lived AWS credentials. Restrict break-glass login to
  loopback or explicitly trusted networks.
- Grant only the documented S3 actions and prefixes. Do not grant account-root
  credentials or broad bucket deletion rights.
- Keep versioning enabled, test restores, and review audit events.
- Report vulnerabilities according to [SECURITY.md](SECURITY.md).

## Development and tests

Create a virtual environment, install the pinned dependencies, and run the
database migration before starting the app:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
DB_BACKEND=sqlite SQLITE_PATH=./data/frostvault.db \
  .venv/bin/python -m alembic upgrade head
DB_BACKEND=sqlite SQLITE_PATH=./data/frostvault.db \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

On Windows PowerShell, use `.venv\Scripts\python.exe` and set environment
variables with `$env:NAME = "value"`.

### Frontend

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
DB_BACKEND=sqlite SQLITE_PATH=./data/frostvault.db \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080

# terminal 2
cd frontend && npm ci && npm run dev
```

Open the Vite URL (default `http://127.0.0.1:5173`). To exercise the production
serving path without Docker: `cd frontend && npm run build`, then start uvicorn
and open `http://127.0.0.1:8080`.

Run the same portable suites used by pull-request CI:

```bash
.venv/bin/python -m unittest discover -s tests -v
cd frontend && npm ci && npm run lint && npm run test
```

PostgreSQL migration tests require `TEST_POSTGRES_URL`; S3-compatible integrity
tests require `TEST_S3_ENDPOINT` and Rclone. See [docs/ci.md](docs/ci.md).

## Known limits

- FrostVault is a self-hosted single-application deployment, not a managed
  multi-region service.
- AWS and Rclone operations depend on external service availability and may
  incur storage, retrieval, request, and transfer charges.
- Glacier restore requests cannot be cancelled after S3 accepts them.
- SQLite does not provide PostgreSQL's production concurrency and operational
  tooling.
- The application protects archive workflows but does not replace offline,
  independently tested disaster-recovery copies.

## License

Licensed under the [Apache License 2.0](LICENSE).
