# Metadata backups and disaster recovery

Encrypted metadata backups protect the application database once it holds
the Owner's Identity, Vaults, Archive Versions, policies, Sessions, audit events
and pending workflows. Losing that database makes normal in-app recovery and
authorization unsafe. Vault contents and filenames are plaintext in S3; metadata
backups remain encrypted.

This document covers scheduled and pre-upgrade backups, restore procedures for
PostgreSQL 16, restore verification, and off-server custody of the
application master key. There is no per-Vault recovery export or custody gate.

## What is backed up

Each backup artifact is a Fernet-encrypted archive (sealed with
`ARCHIVE_MASTER_KEY`) that contains:

- a consistent PostgreSQL custom-format dump (`pg_dump --format=custom --no-owner --no-acl`)
- a reconstructable configuration snapshot
- a manifest with reason, backend, timestamps, and content digests

The artifact **never contains** `ARCHIVE_MASTER_KEY`, bootstrap passwords, or
OIDC client secrets or database DSNs/credentials. A SHA-256 digest of the ciphertext is stored alongside the
file and in `metadata_backup_runs`.

The configuration snapshot contains allow-listed, non-secret deployment values
from the environment/defaults. There are no database overrides or mutable
settings API. Secret and unknown keys are structurally excluded, including
caller-supplied snapshot fields. Before upgrading through migration `0048`,
move any required former overrides into the deployment environment.

## Where artifacts live

| Location | Purpose |
| --- | --- |
| `METADATA_BACKUP_DIR` (default `/data/backups`) | Rotated local copies on a dedicated volume |
| `system/backups/` in the installation S3 bucket | Off-host encrypted copies |

Retention is controlled by `METADATA_BACKUP_RETENTION` (default 14). Older local
`.bak.enc` files and their `.sha256` sidecars are deleted after each successful
run.

`metadata_backup_runs` makes the delivery outcome inspectable without storing
credentials: an off-host success has `status=succeeded` (or later `verified`)
and a non-empty `s3_key`; a local-only artifact success has the same successful
status, a `local_path`, and no `s3_key`; a blocked attempt is recorded as
`status=failed` with a typed non-secret `pre_upgrade_blocked:*` reason. Automatic
startup commits that failure record in a dedicated transaction only after the
attempted backup transaction rolls back, so the evidence survives while no
unrelated migration work can commit. When local-only development has no usable
`ARCHIVE_MASTER_KEY`, no artifact can exist, so automatic migration reports the
explicit allowed local-only state in startup output rather than creating a
misleading successful backup record.

## Scheduling and manual controls

- Background worker: interval `METADATA_BACKUP_INTERVAL_SECONDS` (default 24h).
  Set to `0` to disable scheduled runs.
- Restore verification: interval
  `METADATA_BACKUP_VERIFY_INTERVAL_SECONDS` (default 7d). Verification restores
  into an isolated temporary database / scratch directory and never mutates the
  live database or production `vaults/` S3 keys.
- Owner API (authenticated Session; manual runs also require CSRF):
  - `GET /api/admin/metadata-backups` — status and recent runs
  - `POST /api/admin/metadata-backups/run` — manual backup
  - `GET /api/admin/metadata-backups/download/{run_id}` — download ciphertext

Failed backups record `metadata_backup_runs.status=failed`, enqueue
`metadata_backup_failed` notifications for the Owner, and surface generic
internal-error problems / backup metrics.

## Pre-upgrade gate

Application-managed schema upgrades must not run without the safety copy their
deployment promises:

```bash
# Creates a pre_upgrade backup (local + S3 when configured), then alembic upgrade.
python -m app.backup_upgrade

# Backup only (still fails closed when the backup cannot be created):
python -m app.backup_upgrade --skip-upgrade
```

A failed backup exits non-zero and **does not** run Alembic. Prefer this wrapper
over a bare `alembic upgrade head` in production release procedures.

### Automatic startup policy

With `AUTO_MIGRATE=1` (default), an existing database passes two gates before
any backup or Alembic mutation:

1. FrostVault loads the local Alembic graph, requires exactly one head matching
   `HEAD_SCHEMA_REVISION`, and proves the recorded database revision is a known
   ancestor of that head. A database ahead of the image, an unknown revision,
   a divergent migration graph, or multiple database revision rows stops
   startup before a backup is attempted.
2. A non-empty, non-placeholder `VAULT_S3_BUCKET` means off-host metadata
   storage is configured. The pre-upgrade artifact must be encrypted, uploaded,
   and read back with its ciphertext digest and `.sha256` sidecar verified.
   Object-store initialization, missing/invalid `ARCHIVE_MASTER_KEY`, upload,
   or verification failures all block Alembic. FrostVault reports only a safe
   failure category; it never prints provider exception text or credentials.

An empty or documented placeholder bucket is the explicit **local-only
development** mode. If it has a usable `ARCHIVE_MASTER_KEY`, startup makes and
records a local encrypted artifact before upgrading. If it has no usable key,
startup reports an allowed local-only outcome and may upgrade without an
artifact; this keeps first boot and local development intentional without ever
silently weakening a configured off-host deployment.

Fresh/unversioned databases have no existing state to back up, but FrostVault
still validates that the local Alembic graph has one expected head before it
runs `alembic upgrade head`. Set `AUTO_MIGRATE=0` to keep upgrades fully manual.
It does not make an ahead, unknown, or divergent database compatible: deploy
the image containing its revision, or restore a compatible metadata backup,
before a deliberate manual recovery.

## Restore flow — PostgreSQL

Use PostgreSQL 16 server and client tools. Connections use libpq's `PG*`
environment (`PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` / `.pgpass`)
or optional `DATABASE_URL`; keep credentials out of command arguments and logs.
The image includes `pg_dump`, `pg_restore`, `createdb`, `dropdb`, and `psql`.

1. Stop the application and retain the original database until recovery is proven.
2. Retrieve `ARCHIVE_MASTER_KEY` and database access from independent custody.
3. Choose an artifact from `/data/backups` or `system/backups/`. In a private
   scratch directory, validate the ciphertext digest and decrypt it:

   ```bash
   umask 077
   python - <<'PY'
   import hashlib
   from pathlib import Path
   from app.services.metadata_backups import decrypt_backup_file
   artifact = Path("/data/backups/ARTIFACT.bak.enc")
   expected = Path(str(artifact) + ".sha256").read_text().strip()
   assert hashlib.sha256(artifact.read_bytes()).hexdigest() == expected
   payload = decrypt_backup_file(artifact)
   assert payload["manifest"]["backend"] == "postgresql"
   assert payload["manifest"]["schema_format"] == "postgresql-custom"
   assert hashlib.sha256(payload["database"]).hexdigest() == payload["manifest"]["database_sha256"]
   Path("restore.dump").write_bytes(payload["database"])
   PY
   pg_restore --list restore.dump > /dev/null
   ```

4. Restore into a **new empty isolated database**, never into the live catalog.
   Supply the target host/role securely through `PG*` / `.pgpass`; the names here
   are examples, not permission to overwrite an existing database:

   ```bash
   createdb frostvault_restored
   pg_restore --exit-on-error --no-owner --no-acl -d frostvault_restored restore.dump
   psql -d frostvault_restored -At -c 'SELECT version_num FROM alembic_version'
   ```

5. Validate metadata counts and the schema revision against the intended release.
   For a supported older PostgreSQL revision, point the app at that restored
   database and use `python -m app.backup_upgrade`; it enforces the pre-upgrade
   gate. Unknown/ahead revisions require the matching release, not forced stamps.
6. Only after validation, deliberately configure the app to use the restored
   database and start it. Confirm the Owner can sign in: OIDC uses `OIDC_*` and
   the restored Identity binding; `BOOTSTRAP_ADMIN_*` re-applies the Emergency
   Password. Retire scratch plaintext dumps securely under your own retention policy.

Weekly verification uses a separate temporary database, checks users, Vaults and
`alembic_version`, and drops only that temporary database. Without CREATEDB
permission it reports `verification_mode=pg_restore_list`, not a proven full
restore. It never writes to the live catalog or mutates `vaults/` objects.

### PostgreSQL-only release (#422)

Production, native development and tests now use PostgreSQL only. `DB_BACKEND`
and `SQLITE_PATH` are removed. Existing PostgreSQL databases retain the historical
Alembic upgrade/downgrade graph (including populated storage-plan migration 0053);
this change requires no new schema revision and performs no S3 calls.

Legacy SQLite artifacts are not PostgreSQL dumps and cannot be restored by
`pg_restore` or this release's verification path. Keep those artifacts and the
previous application release if needed for recovery; this release neither deletes
nor converts them. Recover wanted legacy content with that release before moving
to a PostgreSQL installation. There is no automatic cross-backend data conversion.

## Master key custody and emergency file recovery

Keep `ARCHIVE_MASTER_KEY`, the bucket name and region, and recovery instructions
outside this server, preferably in an independently hosted password manager.
The key seals **metadata backup artifacts only**; it is not included in those
artifacts. AWS access is recovered through the Owner's AWS account. Enable MFA
and use an IAM policy restricted to the archive bucket. Do not rely on a password
manager or a copy of the key hosted only on the failed server.

A clean installation can restore the Owner, Vaults, Archive Version metadata,
policies, Sessions/revocations, audit, and pending workflows from a metadata
backup plus that key. The unified downloadable Recovery Kit and its one custody
confirmation are future work (#427), not a per-Vault export retained here.

If no usable metadata backup exists, any authorized S3 tool can list and download
original file bytes under `vaults/<uuid>/<relative-path>`, without FrostVault or
per-Vault secrets. For example, with AWS credentials supplied through your normal
AWS profile (never embed them in these commands):

```bash
aws s3 ls s3://BUCKET/vaults/VAULT-UUID/ --recursive --region REGION
aws s3 cp s3://BUCKET/vaults/VAULT-UUID/ ./recovered/ --recursive --region REGION
```

This is file-only emergency recovery, not an in-app catalog reconstruction.
GLACIER/DEEP_ARCHIVE content needs an S3 restore request and completion before
download. Use `list-object-versions` and the exact `VersionId` if an interrupted
replacement or historical Delete Marker requires inspection; do not delete any
versions while diagnosing. A Local Copy deletion never deletes cloud content.

## Plaintext-only upgrade (0051)

Migration `0051_plaintext_vaults` drops `encryption_mode`, per-Vault sealed secrets,
and custody columns only when **every** persisted Vault is plain and has no
sealed secrets. A crypt Vault, including a disabled Vault or tombstone, refuses
the entire migration before DDL. No row contents or secrets are printed. There
are no S3 calls, key conversions, object deletions, or automatic data migration.
The SDK transfer upgrade (0052) removes legacy transport aliases; it never
reinterprets encrypted data. Old ciphertext
would not become plaintext.

There is no compatibility promise for test Vaults (#362). Recover any wanted
legacy content with the previous release and its existing recovery material,
then create a fresh plaintext installation and re-upload deliberately. Retain
old metadata ciphertext and `ARCHIVE_MASTER_KEY` while they are needed for that
recovery. This release never deletes or rewrites those artifacts. Downgrade
recreates empty nullable secret/custody columns with plain mode; it cannot
recreate retired secrets or enable crypt as a new choice.

## Configuration reference

```bash
METADATA_BACKUP_DIR=/data/backups
METADATA_BACKUP_RETENTION=14
METADATA_BACKUP_INTERVAL_SECONDS=86400
METADATA_BACKUP_VERIFY_INTERVAL_SECONDS=604800
METADATA_BACKUP_S3_PREFIX=system/backups/
ARCHIVE_MASTER_KEY=  # custodied offline; never written into backup payloads
```
