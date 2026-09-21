# FrostVault

Self-hosted archive for files you still want on disk **and** in the cloud.

FrostVault catalogs local files, uploads versioned copies to Amazon S3,
verifies the plaintext, lets vault owners free local space only after that
check, and recovers exact versions later. Isolated vaults, OIDC or local
sign-in, encrypted Rclone remotes, SQLite or PostgreSQL.

It is not a backup product. Keep the database, `ARCHIVE_MASTER_KEY`, and
per-vault recovery exports in separate places.

## Features

- File-manager UI: folders, breadcrumbs, vault-wide search
- Versioned S3 archive, including Glacier and Deep Archive
- Upload is trusted only after plaintext read-back verification
- **Free local space** is owner-only, and only when a verified Archive Version
  is available (`ALLOW_LOCAL_DELETE`)
- Recover the exact S3 `VersionId`
- Isolated vaults with `owner` / `operator` / `viewer` roles, OIDC, and rclone encryption

## Run

You need Docker Compose v2, a **versioned** S3 bucket, and least-privilege IAM
credentials. PostgreSQL 16 is the production catalog; SQLite is fine for
development.

```bash
cp .env.local.example .env
cp config/rclone.local.conf.example config/rclone.conf
```

Set `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `S3_BUCKET`. Values
containing `REPLACE` block AWS calls instead of picking up other host
credentials. Do not commit `.env` or `rclone.conf`.

The image is `ghcr.io/paolodelcasale/frostvault:latest`. It never creates an account at runtime.
Compose runs as `PUID`/`PGID` (Unraid default `99:100`).
Create `./data` as that identity before the first `up`:

```bash
PUID=99
PGID=100
mkdir -p ./data
sudo chown "${PUID}:${PGID}" ./data
sudo chmod 0750 ./data
```

```bash
docker compose pull
docker compose up -d
```

Open http://127.0.0.1:8080. After the first sign-in, remove
`BOOTSTRAP_ADMIN_USERNAME` and `BOOTSTRAP_ADMIN_PASSWORD` from `.env`.

Mount source volumes as siblings under `/sources/<alias>` (`managed` is
reserved). Permissions, Traefik, IAM, and backups:

- [docs/filesystem-permissions.md](docs/filesystem-permissions.md)
- [docs/traefik.md](docs/traefik.md)
- [docs/aws-s3-bucket.md](docs/aws-s3-bucket.md)
- [docs/metadata-backups.md](docs/metadata-backups.md)

## Development

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cd frontend && npm ci && npm run build
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Tests: `.venv/bin/python -m unittest discover -s tests -v`. CI notes:
[docs/ci.md](docs/ci.md). Domain language: [CONTEXT.md](CONTEXT.md).

## License

Licensed under the [Apache License 2.0](LICENSE). Report vulnerabilities via
[SECURITY.md](SECURITY.md).
