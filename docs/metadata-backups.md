# Metadata backups and disaster recovery

Encrypted metadata backups protect the application database once it holds
the Owner's Identity, Vaults, Archive Versions, policies, Sessions, audit events
and pending workflows. Losing that database makes normal in-app recovery and
authorization unsafe. Vault contents and filenames are plaintext in S3; metadata
backups remain encrypted.

This document covers scheduled and pre-upgrade backups, restore procedures for
SQLite and PostgreSQL, restore verification, and off-server custody of the
application master key. There is no per-Vault recovery export or custody gate.

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
8. Start the application and confirm the Owner can sign in. OIDC comes from
   the `OIDC_*` environment and the restored Identity binding; the
   `BOOTSTRAP_ADMIN_*` Emergency Password is re-applied at start.

## Restore flow — PostgreSQL

1. Stop the application.
2. Keep `ARCHIVE_MASTER_KEY` and PostgreSQL credentials from offline custody.
3. Decrypt the artifact as above; write `payload["database"]` to a dump file.
4. Restore into an empty target database (never into a live production DB while
   the app is writing):

   ```bash
   pg_restore --clean --if-exists --no-owner --no-acl -d frostvault restore.dump
   ```

5. Start the application against the restored database after schema validation
   and confirm the Owner can sign in.

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
Legacy shared crypt remotes are also refused at runtime, even for an old plain
row. Do not just change a legacy remote to a plaintext alias: old ciphertext
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
