# FrostVault

FrostVault catalogs local files, archives versioned copies in Amazon S3,
verifies the upload, frees local space only after that check, and recovers
exact versions. Multi-user isolated vaults, OIDC, encrypted Rclone remotes,
SQLite or PostgreSQL.

The UI is a file manager: folders, breadcrumbs, search inside the selected
vault. It does not replace independent backups — keep the database,
`ARCHIVE_MASTER_KEY`, and per-vault recovery exports separately.

## Requirements

- Docker Engine with Compose v2, or Python 3.12+
- A versioned S3 bucket and a least-privilege IAM principal
- Rclone matching the pin in the `Dockerfile` when running outside the image
- PostgreSQL 16 in production; SQLite for development and small single-node installs

## Quick start

Image: `ghcr.io/paolodelcasale/frostvault:latest`.

```bash
cp .env.local.example .env
cp config/rclone.local.conf.example config/rclone.conf
```

Replace `REPLACE…` values. Set `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
and `S3_BUCKET`. Placeholders block AWS calls instead of using other host
credentials. Do not commit `.env` or `rclone.conf`.

### Compose identity and fresh host data directory

The image never creates an account at runtime. Compose runs as
`user: "${PUID:-99}:${PGID:-100}"` (Unraid default `99:100`). Create `./data`
as that identity before the first `up` — Docker would otherwise create it as
root:

```bash
PUID=99
PGID=100
mkdir -p ./data
sudo chown "${PUID}:${PGID}" ./data
sudo chmod 0750 ./data
```

See [docs/filesystem-permissions.md](docs/filesystem-permissions.md).

```bash
docker compose pull
docker compose up -d
```

Open `http://127.0.0.1:8080`. Migrations run on start (`AUTO_MIGRATE=1`).
After the first sign-in, remove `BOOTSTRAP_ADMIN_USERNAME` and
`BOOTSTRAP_ADMIN_PASSWORD` from `.env`.

## Users and vaults

- Sign-in is OIDC, or network-gated **Local Sign-in** (local password;
  loopback plus `BREAK_GLASS_ALLOWED_CIDRS`). Empty CIDRs means loopback-only.
  An administrator using that path when OIDC is down is **Break-glass Login**.
- Each vault has its own folder, S3 prefix, and Rclone remote. Users see only
  assigned vaults. Every vault has exactly one primary owner.
- `owner` manages sharing and policy and may free local space.
- `operator` can upload and recover files.
- `viewer` is read-only.
- Vault creation picks an immutable **Cloud History Policy**: **Archive
  History** keeps every verified version; **Current Snapshot** keeps only the
  last verified upload. Bootstrap vaults stay Archive History.
- New vaults use an empty managed root or adopt a directory from a Source Area.
  Source Areas authorize creation only.
- Administration manages users, identities, invites, vaults, Source Areas,
  runtime defaults, and OIDC. Administrators are not implicit vault members.

## Local cleanup safety

The **Free local space** button appears only when the Local Copy fingerprint
matches an available, verified Archive Version, for owners, and when
`ALLOW_LOCAL_DELETE=true`. Cleanup deletes local copies only. It never deletes
the S3 object. Recovery downloads the exact S3 `VersionId` and verifies the
plaintext SHA-256 before replacing the local file.

## Production

PostgreSQL 16, `.env.example`, and the [data preflight](#compose-identity-and-fresh-host-data-directory).
Mount each Source Volume as a sibling under `/sources/<alias>` (`managed` is
reserved; nested mounts are unsupported):

```text
/srv/frostvault/sources/
├── managed/
├── photos/
└── documents/
```

Rclone remotes: `frostvault-crypt` or `frostvault-plain` — never both on the
same S3 prefix. Before activating OIDC, keep one administrator with a local
password and a non-empty `BREAK_GLASS_ALLOWED_CIDRS`.

IAM: [docs/aws-s3-bucket.md](docs/aws-s3-bucket.md). Optional Traefik:
[docs/traefik.md](docs/traefik.md).

## Configuration

`.env.local.example` (SQLite) or `.env.example` (PostgreSQL). Values containing
`REPLACE` are placeholders.

| Area | Variables |
| --- | --- |
| Database | `DB_BACKEND`, `SQLITE_PATH`, `PGHOST`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` |
| Auth | `BOOTSTRAP_ADMIN_*`, `OIDC_*`, `BREAK_GLASS_ALLOWED_CIDRS` |
| Network | `APP_PORT`, `COOKIE_SECURE`, `ALLOWED_HOSTS`, `TRUSTED_PROXIES` |
| Storage | `S3_BUCKET`, `VAULT_S3_BUCKET`, `VAULT_RCLONE_*`, `RCLONE_CONFIG` |
| Keys / backup | `ARCHIVE_MASTER_KEY`, `METADATA_BACKUP_*` |
| Operations | `OPERATION_CONCURRENCY`, `ALLOW_LOCAL_DELETE`, `RESTORE_*` |

Database, paths, credentials, proxy trust, and bootstrap are deployment-only.
Runtime defaults are edited from Administration after reauthentication.

UI language: English default, complete Italian translation.

## Development

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cd frontend && npm ci && npm run build && cd ..
set -a && . ./.env && set +a
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

SPA work: uvicorn on `:8080` and `cd frontend && npm run dev`. Leave
`ALLOWED_HOSTS` empty locally.

```bash
.venv/bin/python -m unittest discover -s tests -v
cd frontend && npm ci && npm run lint && npm run test
```

CI: [docs/ci.md](docs/ci.md).

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
| Security reports | [SECURITY.md](SECURITY.md) |

## License

Licensed under the [Apache License 2.0](LICENSE).
