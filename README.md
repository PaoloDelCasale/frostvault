# FrostVault

FrostVault is a self-hosted web application for cataloging files, archiving
versioned copies in Amazon S3 storage classes, verifying uploaded content, safely
freeing local space, and recovering exact versions. It supports multiple users,
isolated vaults, OIDC, encrypted Rclone remotes, and SQLite or PostgreSQL.

The archive works like a file manager: it shows top-level folders, lets users
open them, and provides breadcrumbs for navigating back to parent folders.
Search remains global across every file in the selected vault.

## Users and vaults

- Users sign in with configured OIDC or the network-gated **Local Sign-in**
  path when they have a local password. An administrator's use of Local Sign-in
  to recover access while OIDC is unavailable is **Break-glass Login**.
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
- New vaults can use an empty managed root or adopt an existing directory from
  an exclusive Source Area assigned to the user. Source Areas authorize vault
  creation only; vault membership remains the sole data-access boundary.
- The Administration hub manages users, external identities, one-time invites,
  vaults, Source Areas, safe runtime defaults, deployment settings, and OIDC.
  Administrators can promote or demote users, unlink an external identity, and
  revoke a pending invite before it is redeemed. The application refuses any
  change that would leave no active administrator, or leave a user with neither
  a password nor a linked identity.
- Every vault must retain exactly one primary owner.

Administrators do not automatically enter other users' vaults. They must be
explicitly assigned like any other user before they can browse files.

### Local Sign-in and Break-glass Login

Local Sign-in is available to any active User with a configured local password,
not only to administrators. It is always network-gated: loopback is allowed and
additional client networks must be listed in `BREAK_GLASS_ALLOWED_CIDRS`. An
empty value means loopback-only (`127.0.0.0/8` and `::1`); there is no implicit
allow-all value. A successful Local Sign-in creates a Session with only the
User's existing global role and Vault assignments.

Break-glass Login describes an administrator using that same Local Sign-in path
for recovery when OIDC is unavailable. Keep at least one active administrator
with a local password and a non-empty `BREAK_GLASS_ALLOWED_CIDRS` configuration
before activating a managed OIDC configuration; activation refuses to proceed
without that recovery path. The empty setting still permits loopback Local
Sign-in, but is not enough to activate OIDC. Passwordless Users cannot use
Local Sign-in, and administrator-only Reauthentication and authorization checks
remain unchanged.

## Archive lifecycle and cloud deletion

- Owners and operators can run a one-shot storage-class change for a file,
  directory, or whole vault. Moving data out of Glacier or Deep Archive performs
  the required restore before copying it to the selected class.
- The storage-class picker shows storage and retrieval rates, expected restore
  time, and early-deletion warnings. Rates are estimates; AWS billing remains
  authoritative.
- A lifecycle pin suspends automatic storage-class transitions for a path until
  it is unpinned. Automatic lifecycle policies may move data only to deeper
  classes; they never warm it.
- **Hide in cloud** creates a reversible Delete Marker and retains noncurrent
  Archive Versions. **Purge from cloud permanently** deletes every selected
  Archive Version and Delete Marker after an owner-confirmed, cancellable delay.
  A recently reauthenticated owner can skip the remaining delay with
  **Delete now**.
- Cloud actions work on files and folders. Permanent folder and vault purges
  batch S3 deletions while preserving per-item failures for safe retries.
- Archive statistics, file-state badges, and Job progress refresh automatically
  while background operations are active.

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
- Rclone 1.75.0 or a compatible release when running outside the image
- PostgreSQL 16 for production; SQLite is supported for development and small
  single-node installations
- An HTTPS reverse proxy for any network-accessible deployment

FrostVault never replaces independent backups. Keep the database, the
`ARCHIVE_MASTER_KEY`, and per-vault recovery exports in separate secure
locations.

## Setup

### Compose identity and fresh host data directory

The published image bakes in the default Unraid account `archive` (`99:100`).
It does **not** create users or groups when it starts. Both Compose manifests
instead launch the process directly as the numeric
`user: "${PUID:-99}:${PGID:-100}"` identity, so non-default numeric overrides
need no matching account in the image. This preserves the read-only root
filesystem, `cap_drop: [ALL]`, and `no-new-privileges` contract.

Before the first Compose start, create the repository's `./data` bind source on
the host with the identity selected in `.env`. A missing bind source may be
created by Docker as root, which prevents the non-root service from creating
its database and migration/backup files:

```bash
# Use the same values in .env. These are the Unraid defaults.
# Omit `sudo` when your shell is already root, as is typical on Unraid.
PUID=99
PGID=100
mkdir -p ./data
sudo chown "${PUID}:${PGID}" ./data
sudo chmod 0750 ./data
```

This is a one-time fresh-directory preflight, not a recursive ownership reset
of an existing catalog. It deliberately changes only `./data`: prepare
`SOURCES_ROOT` under its own access policy and do not use it as a workaround for
source permissions. Full Linux, Unraid, Docker Desktop, and invalid-identity
guidance is in [docs/filesystem-permissions.md](docs/filesystem-permissions.md).

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
   delete permissions when cloud deletion is not being tested. Never use account
   root credentials.
5. Copy `config/rclone.local.conf.example` to `config/rclone.conf`, enter the
   same bucket, and generate the obscured Rclone password.
6. Run the [fresh `./data` preflight](#compose-identity-and-fresh-host-data-directory),
   then pull the published image and start the application (schema migrations
   run automatically on start when `AUTO_MIGRATE=1`, the default):

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
2. Mount each Source Volume directly under the fixed `/sources` namespace.
   `managed` is reserved for server-provisioned empty Vault roots. Nested
   mounts are unsupported — every filesystem must be a sibling under
   `/sources/<alias>`:

   ```text
   /srv/frostvault/sources/          # host path bound to /sources
   ├── managed/                      # created by FrostVault (do not mount over)
   ├── photos/                       # direct host mount -> /sources/photos
   └── documents/                    # direct host mount -> /sources/documents
   ```

   Compose may still substitute the host path with `SOURCES_ROOT`; the
   application no longer accepts `VAULT_SOURCES_ROOT`.

   On first discovery FrostVault stores a markerless, opaque identity for each
   custom Source Volume. It accepts ordinary remounts of the same source but
   blocks scans, watchers, and all local operations if the alias is absent,
   inaccessible, ambiguous, unsupported, or backed by a different source. The
   catalog remains intact. There is no “accept replacement” action: restore the
   expected Compose mount. Bind, Docker Desktop, and NAS details and limitations
   are documented in [docs/filesystem-permissions.md](docs/filesystem-permissions.md).

   Administrators assign exclusive **Source Areas** under those volumes.
   When creating a Vault, Users can keep the default empty root
   (`/sources/managed/<uuid>`) or adopt an existing directory under one of
   their Source Areas in place. Adoption never moves or rewrites content;
   the server still mints the Vault UUID and `vaults/<uuid>/` S3 prefix, then
   starts an asynchronous local scan.

3. Copy `.env.example` to `.env` and configure paths, bucket, credentials, and
   the bootstrap administrator. Before the first Compose start, run the
   [fresh `./data` preflight](#compose-identity-and-fresh-host-data-directory)
   using the same `PUID`/`PGID` values. For a network deployment set `COOKIE_SECURE=true`
   and provide `ALLOWED_HOSTS` (the hostnames the panel answers to) and
   `TRUSTED_PROXIES` (CIDRs of your reverse proxies); the app refuses to start
   in production without them. Set `OIDC_ENABLED=true` and configure
   `OIDC_ISSUER`/`OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET` for the initial
   environment-defined single sign-on configuration. Set a separate
   `OIDC_SETTINGS_ENCRYPTION_KEY` before managing OIDC through the
   Administration hub.

4. Create `config/rclone.conf` from `config/rclone.conf.example`.
5. Generate a distinct encryption password for each vault:

   ```bash
   docker run --rm rclone/rclone:1.75.0 obscure 'ENCRYPTION-PASSWORD'
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

After completing the [fresh `./data` preflight](#compose-identity-and-fresh-host-data-directory):

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

The image bakes in default `99:100`; Compose runs directly as numeric
`PUID`/`PGID` and never creates an account at runtime. Permission the host
source and data directories for that identity before start; see
[docs/filesystem-permissions.md](docs/filesystem-permissions.md) for the
required `./data` preflight and valid-override behavior.

On first startup, the application creates the administrator defined by
`BOOTSTRAP_ADMIN_*` and, when configured, the first vault. Manage subsequent
users, identities, invites, vault assignments, Source Areas, defaults, and OIDC
from the **Administration** page. Bootstrap variables never overwrite existing
users. After the first successful sign-in, remove
`BOOTSTRAP_ADMIN_USERNAME` and `BOOTSTRAP_ADMIN_PASSWORD` from `.env`.

## Localization

The UI defaults to English and includes a complete Italian translation. Users
can switch language from the archive header or the sign-in page; the preference
is stored in the `frostvault_locale` cookie. Contributor guidance for catalogs and
critical-key checks lives in `docs/translation-workflow.md`.

## Continuous integration

Pull-request CI is deterministic and does not need AWS credentials: unit tests,
PostgreSQL migrations, frontend JS tests, and MinIO-backed S3 integrity proofs.
A separate optional manual workflow uses GitHub OIDC against a prefix-scoped IAM
role for real AWS digest checks. See [docs/ci.md](docs/ci.md) for job status,
cleanup reruns, and security scanner severity gates.

## Recommended first smoke test

Before indexing a terabyte of data:

1. use a test source folder containing two unimportant files;
2. wait for the local catalog to pick them up through the filesystem watcher
   (or trigger an administrative `POST /api/scan` if you need a full
   reconciliation immediately);
3. upload one file and confirm that a distinct S3 version exists in the bucket;
4. confirm the catalog keeps the version unverified until plaintext read-back
   verification completes;
5. confirm **Free local space** and **Recover** remain unavailable for an
   unverified version;
6. keep `rclone.conf` and the original password in at least two secure places.

The local catalog updates automatically when files under the configured source
are created, modified, renamed, or deleted. Native filesystem watching is the
primary mechanism; authenticated browsers receive a Vault-scoped catalog
revision signal and refresh the archive view without idle polling or a manual
**Refresh list** action. A low-frequency full scan (`SCAN_INTERVAL_SECONDS`,
default six hours) remains the reconciliation safety net for missed events,
startup, and mount-return correctness. Set
`FILESYSTEM_WATCH_FORCE_POLLING=true` with Docker Desktop on Windows. On a Linux
server, prefer the native watcher with the value `false`.
See [docs/catalog-events.md](docs/catalog-events.md) for the SSE journal cost
model and reconnect semantics.

## Indicative minimum AWS permissions

The application role/user must be able to list the bucket and, within the
configured prefix, read, upload, and request object restores. **Hide in cloud**
and permanent purge additionally require `s3:DeleteObject` and
`s3:DeleteObjectVersion`; omit them when vault cloud deletion remains disabled.

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
| Authentication | `BOOTSTRAP_ADMIN_*`, `OIDC_*` (including the dedicated `OIDC_SETTINGS_ENCRYPTION_KEY`), `BREAK_GLASS_ALLOWED_CIDRS` (network gate for Local Sign-in and administrator Break-glass Login) |
| Network | `APP_PORT`, `COOKIE_SECURE`, `ALLOWED_HOSTS`, `TRUSTED_PROXIES` |
| Frontend | `FRONTEND_DIST_DIR` (Vite build output; required for HTML routes) |
| Web Push | `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT` (optional; see below) |
| Storage | `S3_BUCKET`, `VAULT_S3_BUCKET`, `VAULT_RCLONE_*`, `RCLONE_CONFIG` |
| Encryption and backup | `ARCHIVE_MASTER_KEY`, `METADATA_BACKUP_*` |
| Operations | `OPERATION_CONCURRENCY`, `S3_DOWNLOAD_*`, `RCLONE_MULTI_THREAD_*`, `RESTORE_*`, `CLOUD_PURGE_DELAY_SECONDS`, `ALLOW_LOCAL_DELETE` |

Database settings and credentials, paths and mount roots, master/storage
credentials, proxy/host/cookie trust, bootstrap values, automatic migration,
and the frontend distribution path are deployment-only. They cannot be edited
at runtime because they define process and deployment trust boundaries.
The **Administration** page exposes a structurally redacted deployment inventory
and allows recently reauthenticated administrators to apply or reset bounded
runtime defaults. The equivalent API is `GET /api/admin/settings` plus
revision-checked `PATCH /api/admin/settings`; stale writes are rejected. See
[ADR-0009](docs/adr/0009-effective-system-configuration.md) for the exhaustive
classification and precedence model.

Managed OIDC configuration uses a staged workflow: save a draft, validate the
provider discovery document and JWKS, then activate the unchanged validated
draft. Administrators can also rotate the write-only client secret or disable
new OIDC sign-ins without deleting existing Sessions or Identity bindings. See
[ADR-0010](docs/adr/0010-secure-oidc-configuration-lifecycle.md).

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
- Prefer OIDC and short-lived AWS credentials. Restrict Local Sign-in (including
  administrator Break-glass Login) to loopback or explicitly trusted networks;
  an empty `BREAK_GLASS_ALLOWED_CIDRS` value is loopback-only, never allow-all.
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
