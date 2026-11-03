# Metadata backups and disaster recovery

There are two manual routes, with no first-start wizard or in-app catalog
reconstruction:

1. **Metadata backup + Recovery Kit:** restore the Owner, Vaults, catalog,
   Cloud-only files, settings and Job history, then Reconnect Folder for
   every Vault. S3 supplies newer file content; settings changed since the backup
   must be reapplied manually.
2. **No usable metadata backup:** recover original files directly with an S3
   tool. This does not rebuild a FrostVault installation or file history.

## Keep one Recovery Kit outside the server

Download the installation-wide **Recovery Kit** from the recovery panel in the
current administration/General area (`GET /api/recovery-kit/download`). Recent
Owner reauthentication is mandatory. The attachment is secret-bearing and
`no-store`; the action is audited without its contents. It contains:

- `ARCHIVE_MASTER_KEY`, used only to decrypt metadata backup artifacts;
- bucket and region;
- self-contained manual PostgreSQL restore and S3 emergency instructions.

It contains **no catalog, Vault/file list, AWS credentials, database credentials,
OIDC secrets or per-Vault recovery files**. Plaintext Vault bytes and readable
names are stored in S3; crypt/rclone are not recovery dependencies.

Store the whole kit in an independently hosted password manager (not one on
this Unraid), optionally also printed. Recover AWS access through the Owner's
AWS account; use MFA and bucket-scoped IAM. Compare the visible **Kit Fingerprint**
with the stored kit, then make the **one installation-wide custody acknowledgment**.
A master-key or bucket/region change changes the fingerprint and invalidates
that acknowledgment. This does not rotate/re-encrypt older backup artifacts:
retain earlier keys securely while those artifacts are needed.

`GET /api/recovery-kit` exposes only public fingerprint/custody and durable
metadata-restore warning state. `POST /api/recovery-kit/custody` accepts the
current fingerprint with Session + CSRF; stale fingerprints fail with 409.
Custody never authorizes cloud deletion or suppresses the warning below.

## Automatic metadata backups

Every daily backup is a Fernet-encrypted archive containing a consistent
PostgreSQL 16 custom dump (`pg_dump --format=custom --no-owner --no-acl`), an
allow-listed non-secret configuration snapshot and a manifest with timestamps
and content digests. The artifact excludes `ARCHIVE_MASTER_KEY`, OIDC/bootstrap
secrets and database credentials/DSNs, including arbitrary caller snapshot keys.
Sessions, catalog and pending workflows are in the encrypted database dump.

Copies live in `METADATA_BACKUP_DIR` (default `/data/backups`) and off-host under
`system/backups/`. Ciphertext SHA-256 sidecars accompany them. Local rotation
keeps `METADATA_BACKUP_RETENTION` artifacts (default 14), deleting older local
artifacts/sidecars only after success. `metadata_backup_runs` records local-only,
off-host and failed outcomes; `s3_key` proves attempted off-host delivery, not
just the presence of a local file. Failed runs surface Owner notifications and
internal-error problems/metrics.

- `METADATA_BACKUP_INTERVAL_SECONDS=86400` (0 disables scheduled backups).
- `METADATA_BACKUP_VERIFY_INTERVAL_SECONDS=604800`: weekly verification restores
  into a separate temporary database, checks Owner/Vault/schema counts, then
  drops only that temporary database. Without CREATEDB it reports
  `verification_mode=pg_restore_list`, not a proven full restore. No production
  catalog or `vaults/` S3 objects are modified.
- The weekly protection summary includes the last verified backup date.
- Authenticated API: `GET /api/admin/metadata-backups`,
  `POST /api/admin/metadata-backups/run` (CSRF),
  `GET /api/admin/metadata-backups/download/{run_id}` (ciphertext only).

### Upgrade gate

Use `python -m app.backup_upgrade` (or `--skip-upgrade` for backup only). Failure
blocks Alembic. Automatic startup (`AUTO_MIGRATE=1`) first proves that the
recorded schema is a known ancestor of the single local expected head. Unknown,
ahead, divergent or multiple revisions stop before any backup or upgrade.
Fresh databases still validate the migration graph.

A configured non-placeholder bucket requires an encrypted off-host pre-upgrade
artifact with verified readback of its ciphertext digest and sidecar. Missing
keys, cloud failures and verification errors block migration; output is limited
to safe failure categories. Empty/documented-placeholder buckets are explicit
local-only development: with a usable key there is a local encrypted artifact;
without one startup reports that intentional outcome rather than pretending a
backup exists. `AUTO_MIGRATE=0` permits manual control, not stale-schema serving.

## Main route: manual PostgreSQL 16 restore

Use the matching FrostVault release and PostgreSQL 16 client/server tools. The
image includes `pg_dump`, `pg_restore`, `createdb`, `dropdb`, `psql`. Keep AWS and
libpq credentials in protected profiles/environment files or `.pgpass`, never
literal process arguments, logs, reports or shell history. Use `umask 077` for
the private scratch directory. The downloaded kit includes these steps offline.

1. **Stop FrostVault.** Preserve the original database until recovery is proven.
2. Retrieve the independently custodied kit and obtain AWS/database access.
   Download the chosen artifact and `.sha256` sidecar from `system/backups/`
   (`aws s3 ls` / `aws s3 cp`, not `sync`). Export `ARTIFACT` with its local
   non-secret path. Supply the key with a hidden prompt, not a literal command:

   ```bash
   read -rs -p 'Master key: ' ARCHIVE_MASTER_KEY
   export ARCHIVE_MASTER_KEY
   printf '\n'
   python - <<'PY'
   import hashlib, json, os
   from pathlib import Path
   from app.services.metadata_backups import decrypt_backup_file
   artifact = Path(os.environ['ARTIFACT'])
   assert hashlib.sha256(artifact.read_bytes()).hexdigest() == Path(str(artifact)+'.sha256').read_text().strip()
   payload = decrypt_backup_file(artifact)
   assert payload['manifest']['backend'] == 'postgresql'
   assert hashlib.sha256(payload['database']).hexdigest() == payload['manifest']['database_sha256']
   Path('restore.dump').write_bytes(payload['database'])
   Path('restore-manifest.json').write_text(json.dumps(payload['manifest']))
   PY
   ```

3. Restore into a **new empty isolated database**, never the live catalog.
   Supply connection credentials through libpq `PG*` / `.pgpass`:

   ```bash
   createdb frostvault_restored
   pg_restore --exit-on-error --no-owner --no-acl -d frostvault_restored restore.dump
   psql -d frostvault_restored -At -c 'SELECT version_num FROM alembic_version'
   ```

4. Validate metadata counts and revision against the matching release. Point
   `PGDATABASE` at the restored database and **unset `DATABASE_URL`**, or securely
   replace that DSN with the restored database's DSN: a configured `DATABASE_URL`
   takes precedence over `PG*` for FrostVault commands. Verify the target before
   any application command. For a supported older revision use
   `python -m app.backup_upgrade`; unknown/ahead revisions require the matching
   release, never forced stamps. Reapply non-secret deployment settings as needed.
5. **Before starting the app**, register the actual backup date from the saved
   manifest against that restored database (after migration to 0058 or newer):

   ```bash
   python - <<'PY'
   import json
   from pathlib import Path
   from app.database import db
   from app.services.recovery_kit import record_metadata_restore
   with db() as connection:
       record_metadata_restore(connection, json.loads(Path('restore-manifest.json').read_text())['created_at'])
   PY
   ```

   This also invalidates restored local/mount observations and cancels stale
   nonterminal Jobs without filesystem/S3 operations: request wanted work again
   after reconnect. Every active Vault stays suspended until explicitly
   reconnected, even if an old inode happens to match. Retained decommission
   state remains quiesced and must be reviewed deliberately. Do not start workers
   against the restored catalog before this step.
6. Configure AWS, OIDC environment and `BOOTSTRAP_ADMIN_*` securely. Start the
   app and sign in. The current Emergency Password is reapplied at startup and
   accepted only from loopback/allowed LAN; the restored OIDC Identity remains.
7. **Reconnect Folder for each Vault.** Select any supported direct Source
   Volume and relative directory (empty for that volume's root). This explicitly
   accepts the mount for this Vault only, not its siblings. Allocation/containment
   and Job/scan concurrency gates remain mandatory. No files are moved.
8. Reconnect reconciles S3 **without cloud deletions**, then rehashes the complete
   local tree. Matching files become **Protected**, absent files **Cloud-only**,
   different content **Cloud-only Conflict** with upload held back. An empty
   folder is valid. Full scan/verification failure keeps local work suspended,
   across restart; retry the scan or explicitly reconnect another valid folder.
   Unknown newer cloud content is read by exact VersionId before becoming
   verified evidence. GLACIER/DEEP_ARCHIVE content may need the manual S3 restore
   request below before reconnect can finish. No size/mtime-only shortcut.

The recovery panel keeps a durable warning: **“Restored backup dated <date>:
<N> files updated from the cloud”**, including new files absent from the backup.
The count is distinct Vault Files, not every scan or every historical VersionId;
reading notifications or restarting does not erase it. No routine per-file
reconciliation notifications are added. Settings changed after the backup remain
lost and must be reapplied. Retire scratch plaintext dumps under your retention
policy only after validation.

## Emergency route: files only

With no readable metadata backup, use any authorized S3 tool. Original bytes and
names live at `vaults/<uuid>/<relative-path>`; the master key is not needed for
those plaintext files. Set non-secret `BUCKET`, `REGION`, then choose `PREFIX`
from the listing. Download to a new empty destination:

```bash
aws s3 ls "s3://$BUCKET/vaults/" --region "$REGION"
aws s3 ls "s3://$BUCKET/$PREFIX" --recursive --region "$REGION"
aws s3 cp "s3://$BUCKET/$PREFIX" ./recovered/ --recursive --region "$REGION"
```

For GLACIER/DEEP_ARCHIVE, set `KEY` and exact `VERSION` from the listing:

```bash
aws s3api list-object-versions --bucket "$BUCKET" --prefix "$PREFIX" --region "$REGION"
aws s3api restore-object --bucket "$BUCKET" --key "$KEY" --version-id "$VERSION" --restore-request '{"Days":7,"GlacierJobParameters":{"Tier":"Bulk"}}' --region "$REGION"
aws s3api head-object --bucket "$BUCKET" --key "$KEY" --version-id "$VERSION" --region "$REGION"
# Wait for Restore ongoing-request=false, then:
aws s3api get-object --bucket "$BUCKET" --key "$KEY" --version-id "$VERSION" --region "$REGION" ./recovered-file
```

Inspect current versions/Delete Markers when a replacement was interrupted;
**preserve every version while diagnosing**. No command above removes cloud
content. Listing is not permission to purge “extras” based on a stale catalog.
Cold retrieval, requests and transfer can incur AWS charges. This route recovers
files, not catalog/settings/history, and does not create Vaults inside FrostVault.

## Older formats and upgrades

PostgreSQL is the only supported backend (#422); `DB_BACKEND`/`SQLITE_PATH` are
removed. Legacy SQLite artifacts cannot be fed to `pg_restore` or this release's
verifier: retain the old release/artifacts for deliberate recovery, not automatic
conversion. Migration 0051 refuses all encrypted legacy Vaults before DDL and
never rewrites ciphertext or calls S3; recover wanted legacy content with the
previous release and its old material before creating plaintext Vaults. The SDK
upgrade (0052) does not reinterpret encrypted bytes. No compatibility guarantee
exists for test Vaults (#362). Old metadata ciphertext still needs its original
master key.

Migration **0058_reconnect_recovery_kit** adds only per-Vault mount acceptance,
installation custody and restore-warning state. It performs no filesystem/cloud
operations, inherits no custody acknowledgment and does not rewrite catalog
trust. Downgrade refuses accepted mount overrides because the previous release
cannot enforce them. Follow existing pre-upgrade backup safeguards.
