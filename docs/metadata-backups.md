# Metadata backups and disaster recovery

Encrypted metadata backups protect the application database once it holds
identities, vaults, version history, policies, sessions, audit events, and
encrypted vault keys. Losing that database makes normal recovery and
authorization unsafe.

This document covers scheduled and pre-upgrade backups, restore procedures for
SQLite and PostgreSQL, restore verification, and separate custody of the
application master key versus encrypted-vault recovery exports.

## What is backed up

Each backup artifact is a Fernet-encrypted archive (sealed with
`ARCHIVE_MASTER_KEY`) that contains:

- a consistent database dump (SQLite file image or PostgreSQL `pg_dump`
  custom format)
- a reconstructable configuration snapshot
- a manifest with reason, backend, timestamps, and content digests

The artifact **never contains** `ARCHIVE_MASTER_KEY`, bootstrap passwords, or
OIDC client secrets. A SHA-256 digest of the ciphertext is stored alongside the
file and in `metadata_backup_runs`.

The configuration snapshot uses the effective system-settings resolver, so it
includes committed non-secret database overrides and their environment or
built-in fallbacks. Secret and unknown keys are structurally excluded.

## Where artifacts live

| Location | Purpose |
| --- | --- |
| `METADATA_BACKUP_DIR` (default `/data/backups`) | Rotated local copies on a dedicated volume |
| `system/backups/` in the installation S3 bucket | Off-host encrypted copies |

Retention is controlled by `METADATA_BACKUP_RETENTION` (default 14). Older local
`.bak.enc` files and their `.sha256` sidecars are deleted after each successful
run.

## Scheduling and manual controls

- Background worker: interval `METADATA_BACKUP_INTERVAL_SECONDS` (default 24h).
  Set to `0` to disable scheduled runs.
- Restore verification: interval
  `METADATA_BACKUP_VERIFY_INTERVAL_SECONDS` (default 7d). Verification restores
  into an isolated temporary database / scratch directory and never mutates the
  live database or production `vaults/` S3 keys.
- Admin API (global administrators, reauthentication required for runs):
  - `GET /api/admin/metadata-backups` — status and recent runs
  - `POST /api/admin/metadata-backups/run` — manual backup
  - `GET /api/admin/metadata-backups/download/{run_id}` — download ciphertext

Failed backups record `metadata_backup_runs.status=failed`, enqueue
`metadata_backup_failed` notifications for administrators, and emit worker
errors / metrics.

## Pre-upgrade gate

Application-managed schema upgrades must not run without a successful backup
when backup secrets are configured:

```bash
# Creates a pre_upgrade backup (local + S3 when configured), then alembic upgrade.
python -m app.backup_upgrade

# Backup only (still fails closed when the backup cannot be created):
python -m app.backup_upgrade --skip-upgrade
```

A failed backup exits non-zero and **does not** run Alembic. Prefer this wrapper
over a bare `alembic upgrade head` in production release procedures.

With `AUTO_MIGRATE=1` (default), container and native app starts call the same
gate automatically for databases that are behind `HEAD_SCHEMA_REVISION`. Fresh /
unversioned databases skip the backup and run `alembic upgrade head` only. Set
`AUTO_MIGRATE=0` to keep upgrades fully manual.

## Restore flow — SQLite

1. Stop the application.
2. Keep `ARCHIVE_MASTER_KEY` available from offline custody (not from the backup).
3. Choose an artifact from `/data/backups` or download from `system/backups/`.
4. Verify the ciphertext digest against the `.sha256` sidecar.
5. Decrypt and unpack:

   ```bash
   python - <<'PY'
   from pathlib import Path
   from app.services.metadata_backups import decrypt_backup_file
   payload = decrypt_backup_file(Path("/data/backups/ARTIFACT.bak.enc"))
   Path("/data/frostvault.db.restored").write_bytes(payload["database"])
   print(payload["manifest"])
   print("config keys:", sorted(payload["config"]))
   PY
   ```

6. Replace the live SQLite file with the restored image only after validation.
7. Confirm `alembic_version` matches the application release, or run
   `python -m app.backup_upgrade` / `alembic upgrade head` as appropriate.
8. Start the application and confirm administrators can sign in.

## Restore flow — PostgreSQL

1. Stop the application.
2. Keep `ARCHIVE_MASTER_KEY` and PostgreSQL credentials from offline custody.
3. Decrypt the artifact as above; write `payload["database"]` to a dump file.
4. Restore into an empty target database (never into a live production DB while
   the app is writing):

   ```bash
   pg_restore --clean --if-exists --no-owner --no-acl -d frostvault restore.dump
   ```

5. Start the application against the restored database after schema validation.

## Master key and recovery-export custody

Keep these materials in **separate** offline locations:

| Material | Role | Must not be stored with |
| --- | --- | --- |
| `ARCHIVE_MASTER_KEY` | Seals per-vault crypt secrets and metadata backup artifacts | Database backups, recovery exports, S3 copies of either |
| Encrypted vault recovery exports | Standalone rclone crypt credentials for one vault | Application master key, live DB dumps |
| Metadata backup artifacts | Users, vaults, versions, policies, sessions, audit | Application master key in cleartext |

A clean installation can restore users, vaults, Archive Versions metadata,
policies, sessions/revocations, audit, and pending workflows from a metadata
backup **plus** the separately custodied master key. Vault content recovery from
S3 still requires the per-vault recovery material when using crypt vaults.

## Configuration reference

```bash
METADATA_BACKUP_DIR=/data/backups
METADATA_BACKUP_RETENTION=14
METADATA_BACKUP_INTERVAL_SECONDS=86400
METADATA_BACKUP_VERIFY_INTERVAL_SECONDS=604800
METADATA_BACKUP_S3_PREFIX=system/backups/
ARCHIVE_MASTER_KEY=  # custodied offline; never written into backup payloads
```
