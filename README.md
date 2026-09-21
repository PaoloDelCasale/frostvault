# FrostVault

FrostVault is a self-hosted web app that catalogs local files, archives
versioned copies in Amazon S3, verifies the upload, frees local space only
after that check, and recovers exact versions. It supports multiple users,
isolated vaults, OIDC, encrypted Rclone remotes, and SQLite or PostgreSQL.

The UI works like a file manager: folders, breadcrumbs, and search across the
selected vault.

This is not a backup product. Keep the database, `ARCHIVE_MASTER_KEY`, and
per-vault recovery exports in separate secure locations.

## Requirements

- Docker Engine with Compose v2 (recommended), or Python 3.12+ for native runs
- A versioned S3 bucket and a least-privilege IAM principal
- Rclone matching the version pinned in the `Dockerfile` when running outside the image
- PostgreSQL 16 for production; SQLite for development and small single-node installs
- An HTTPS reverse proxy for any network-accessible deployment

## Quick start

The published image is `ghcr.io/paolodelcasale/frostvault:latest`. A standard
deploy pulls it; you only need a local `Dockerfile` build if you are changing
the image.

### Configure

```bash
cp .env.local.example .env                 # local SQLite
# cp .env.example .env                     # PostgreSQL production
cp config/rclone.local.conf.example config/rclone.conf
```

Replace every `REPLACE…` value in `.env`. At minimum set `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, and `S3_BUCKET`. While placeholders remain, FrostVault
blocks AWS calls instead of using other credentials on the host.

Use dedicated IAM credentials for a test bucket/prefix. Never use account-root
keys, and never commit `.env` or `config/rclone.conf`.

Point `config/rclone.conf` at the same bucket and generate an obscured Rclone
password:

```bash
docker run --rm rclone/rclone:$(sed -n 's/^FROM rclone\/rclone:\([^ ]*\) AS rclone$/\1/p' Dockerfile) obscure 'ENCRYPTION-PASSWORD'
```

### Compose identity and fresh host data directory

The image bakes in the Unraid default `archive` (`99:100`) and never creates an account at runtime.
Compose starts the process as numeric `user: "${PUID:-99}:${PGID:-100}"`.

Before the first `docker compose up`, create `./data` as that identity. Docker
would otherwise create a missing bind source as root, and the non-root process
could not write the database:

```bash
# Same values as .env. Unraid defaults shown. Omit sudo if you are already root.
PUID=99
PGID=100
mkdir -p ./data
sudo chown "${PUID}:${PGID}" ./data
sudo chmod 0750 ./data
```

This is a one-time fresh-directory preflight, not a recursive chown of an
existing catalog. Prepare `SOURCES_ROOT` under its own access policy.
Full Linux, Unraid, and Docker Desktop notes:
[docs/filesystem-permissions.md](docs/filesystem-permissions.md).

### Run

```bash
docker compose pull
docker compose up -d
docker compose logs -f frostvault
```

The panel listens on `127.0.0.1:8080`. Schema migrations run on start
(`AUTO_MIGRATE=1`). Set `AUTO_MIGRATE=0` and run
`docker compose run --rm frostvault python -m app.backup_upgrade` first when
you want a manual upgrade gate.

On first boot the app creates `BOOTSTRAP_ADMIN_*` and, if configured, the first
vault. After the first successful sign-in, remove `BOOTSTRAP_ADMIN_USERNAME`
and `BOOTSTRAP_ADMIN_PASSWORD` from `.env`.

Behind Traefik, use `compose.traefik.yaml` so the app is not published on host
ports. Set `COOKIE_SECURE=true`, `ALLOWED_HOSTS`, and `TRUSTED_PROXIES`.
See [docs/traefik.md](docs/traefik.md).

### First smoke test

Use a tiny test folder, not a terabyte:

1. Wait for the catalog (filesystem watcher, or `POST /api/scan`).
2. Upload one file and confirm a distinct S3 version exists.
3. Confirm the catalog keeps it unverified until plaintext read-back finishes.
4. Confirm **Free local space** and **Recover** stay unavailable until then.

Keep `rclone.conf` and the original encryption password in at least two secure
places.

## Users and vaults

- Sign-in is configured OIDC, or network-gated **Local Sign-in** when the user
  has a local password. Loopback is always allowed; extra client networks go in
  `BREAK_GLASS_ALLOWED_CIDRS` (empty means loopback-only, never allow-all). An
  administrator using that path when OIDC is down is **Break-glass Login**.
- Each vault has its own local folder, S3 prefix, and Rclone remote. Users see
  only vaults assigned to them.
- `owner` manages sharing and policy and may free local space.
- `operator` can upload and recover files.
- `viewer` is read-only.
- Every vault keeps exactly one primary owner. Administrators are not implicit
  members of other users' vaults.
- Creating a vault requires an immutable **Cloud History Policy**:
  **Archive History** keeps every verified version recoverable; **Current
  Snapshot** keeps only the last verified upload. Bootstrap vaults stay Archive
  History. The shared S3 bucket is versioned either way.
- New vaults can use an empty managed root or adopt a directory from a Source
  Area assigned to the user. Source Areas authorize creation only; vault
  membership remains the data-access boundary.
- The Administration hub manages users, identities, invites, vaults, Source
  Areas, runtime defaults, and OIDC.

Queued uploads, recoveries, and local cleanup can be stopped from the UI.
`OPERATION_CONCURRENCY` caps parallel work (default `4`, max `16`).

## Local cleanup safety

The **Free local space** button appears only when the Local Copy fingerprint
matches an available, verified Archive Version, for owners, and when
`ALLOW_LOCAL_DELETE=true`. Cleanup deletes local copies only. It never deletes
the S3 object.

Recovery downloads the exact archived S3 `VersionId`, verifies the plaintext
SHA-256, then atomically replaces the local file. Glacier / Deep Archive
versions are restored first (Bulk by default).

**Hide in cloud** creates a reversible Delete Marker. **Purge from cloud
permanently** deletes selected Archive Versions after an owner-confirmed delay.

## Production

1. Create PostgreSQL 16:

   ```sql
   CREATE USER frostvault WITH PASSWORD 'A-LONG-PASSWORD';
   CREATE DATABASE frostvault OWNER frostvault;
   ```

2. Copy `.env.example` to `.env` and set paths, bucket, credentials, and the
   bootstrap administrator. Run the [data preflight](#compose-identity-and-fresh-host-data-directory)
   with the same `PUID`/`PGID`.

3. Mount each Source Volume as a sibling under `/sources/<alias>`. `managed` is
   reserved for empty vault roots. Nested mounts are unsupported:

   ```text
   /srv/frostvault/sources/          # host path bound to /sources
   ├── managed/                      # created by FrostVault
   ├── photos/                       # -> /sources/photos
   └── documents/                    # -> /sources/documents
   ```

4. Create `config/rclone.conf` from `config/rclone.conf.example`. In the
   administration panel, **Rclone remote** is `frostvault-crypt` (encrypted) or
   `frostvault-plain` (unencrypted). Never share an S3 prefix between both
   modes.

5. Before activating managed OIDC, keep at least one administrator with a local
   password and a non-empty `BREAK_GLASS_ALLOWED_CIDRS`.

IAM baseline: [`infra/terraform/archive-bucket/`](infra/terraform/archive-bucket/)
and [docs/aws-s3-bucket.md](docs/aws-s3-bucket.md). Metadata backups:
[docs/metadata-backups.md](docs/metadata-backups.md).

## Configuration

Start from `.env.local.example` (SQLite) or `.env.example` (PostgreSQL). Every
value containing `REPLACE` is a placeholder.

| Area | Variables |
| --- | --- |
| Database | `DB_BACKEND`, `SQLITE_PATH`, `PGHOST`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` |
| Auth | `BOOTSTRAP_ADMIN_*`, `OIDC_*`, `BREAK_GLASS_ALLOWED_CIDRS` |
| Network | `APP_PORT`, `COOKIE_SECURE`, `ALLOWED_HOSTS`, `TRUSTED_PROXIES` |
| Storage | `S3_BUCKET`, `VAULT_S3_BUCKET`, `VAULT_RCLONE_*`, `RCLONE_CONFIG` |
| Keys / backup | `ARCHIVE_MASTER_KEY`, `METADATA_BACKUP_*` |
| Operations | `OPERATION_CONCURRENCY`, `ALLOW_LOCAL_DELETE`, `RESTORE_*` |

Database, paths, credentials, proxy trust, bootstrap, and `AUTO_MIGRATE` are
deployment-only. Bounded runtime defaults are edited from Administration after
reauthentication. Optional Web Push (`VAPID_*`) degrades cleanly when unset;
it needs HTTPS outside localhost.

The UI defaults to English and ships a complete Italian translation.

## Development

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cd frontend && npm ci && npm run build && cd ..
set -a && . ./.env && set +a
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Day-to-day SPA work: uvicorn on `:8080` plus `cd frontend && npm run dev`.
Leave `ALLOWED_HOSTS` empty locally (or list `127.0.0.1`). The Vite proxy uses
`changeOrigin`, so a wrong Host shows up as a login failure.

```bash
.venv/bin/python -m unittest discover -s tests -v
cd frontend && npm ci && npm run lint && npm run test
```

Contributor CI (no AWS credentials required): [docs/ci.md](docs/ci.md).

## Security

- Expose FrostVault only through HTTPS. Configure trusted hosts and proxies.
- Prefer OIDC and short-lived AWS credentials. Restrict Local Sign-in to
  loopback or explicit CIDRs.
- Grant only the documented S3 actions and prefixes.
- Report vulnerabilities according to [SECURITY.md](SECURITY.md).

## Docs

| Topic | Doc |
| --- | --- |
| Permissions / PUID | [docs/filesystem-permissions.md](docs/filesystem-permissions.md) |
| Traefik | [docs/traefik.md](docs/traefik.md) |
| S3 bucket | [docs/aws-s3-bucket.md](docs/aws-s3-bucket.md) |
| Metadata backups | [docs/metadata-backups.md](docs/metadata-backups.md) |
| Catalog events | [docs/catalog-events.md](docs/catalog-events.md) |
| Translations | [docs/translation-workflow.md](docs/translation-workflow.md) |
| Domain language | [CONTEXT.md](CONTEXT.md) |

## License

Licensed under the [Apache License 2.0](LICENSE).
