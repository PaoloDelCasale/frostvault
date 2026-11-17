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
General page (`GET /api/recovery-kit/download`). Recent
Owner reauthentication is mandatory. The attachment is secret-bearing and
`no-store`; the action is audited without its contents. It contains:

- `ARCHIVE_MASTER_KEY`, used only to decrypt metadata backup artifacts;
- the [System Bucket](#system-bucket) (`system_bucket`) and its `region`;
- self-contained manual PostgreSQL restore and S3 emergency instructions.

The kit is offered only once a metadata backup run has succeeded with its
off-host copy in the current System Bucket (configured or created), so it never
names a bucket that holds no backup. Until then the Recovery Kit panel says so:
keep the previous kit, which points to the earlier backups in the bucket it
names.

It contains **no catalog, Vault/file list, AWS credentials, database credentials,
OIDC secrets or per-Vault recovery files**. Plaintext Vault bytes and readable
names are stored in S3; crypt/rclone are not recovery dependencies.

Store the whole kit in an independently hosted password manager (not one on
this Unraid), optionally also printed. Recover AWS access through the Owner's
AWS account; use MFA and bucket-scoped IAM. Compare the visible **Kit Fingerprint**
with the stored kit, then make the **one installation-wide custody acknowledgment**.
A master-key change or another System Bucket (name or region) changes the
fingerprint and invalidates that acknowledgment. This does not rotate/re-encrypt older backup artifacts:
retain earlier keys securely while those artifacts are needed.

`GET /api/recovery-kit` exposes only public fingerprint/custody, the System
Bucket's name and region, and durable metadata-restore warning state. `POST /api/recovery-kit/custody` accepts the
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
`system/backups/` in the [System Bucket](#system-bucket). Ciphertext SHA-256
sidecars accompany them. Local rotation
keeps `METADATA_BACKUP_RETENTION` artifacts (default 14), deleting older local
artifacts/sidecars only after success. `metadata_backup_runs` records local-only,
off-host and failed outcomes; `s3_key` and `s3_bucket` prove attempted off-host
delivery, not just the presence of a local file (`s3_bucket` is empty for runs
made before the System Bucket). Failed runs surface Owner notifications and
internal-error problems/metrics.

- `METADATA_BACKUP_BUCKET` (optional): see [System Bucket](#system-bucket).
- `METADATA_BACKUP_INTERVAL_SECONDS=86400` (0 disables scheduled backups).
- `METADATA_BACKUP_VERIFY_INTERVAL_SECONDS=604800`: weekly verification restores
  into a separate temporary database, checks Owner/Vault/schema counts, then
  drops only that temporary database. Without CREATEDB it reports
  `verification_mode=pg_restore_list`, not a proven full restore. No production
  catalog or `vaults/` S3 objects are modified.
- The weekly protection summary includes the last verified backup date.
- Authenticated API: `GET /api/general/metadata-backups`,
  `POST /api/general/metadata-backups/run` (CSRF),
  `GET /api/general/metadata-backups/download/{run_id}` (ciphertext only).

## System Bucket

New metadata backups go to the installation's **System Bucket**, separate from
every bucket holding Vault content ([ADR-0049](adr/0049-one-bucket-per-vault.md)).
A shared bucket of an installation from before Vault Buckets no longer receives them.

- **`METADATA_BACKUP_BUCKET` set:** FrostVault uses that bucket and never changes
  it. Before every backup it verifies that the bucket is in `AWS_DEFAULT_REGION`
  and has all four Block Public Access settings, versioning Enabled and default
  encryption SSE-S3 (`AES256`). Otherwise the backup fails with a message naming
  each missing setting (`METADATA_BACKUP_BUCKET refused: …`), recorded in the run
  list and notified to the Owner; nothing is uploaded.
- **Unset:** FrostVault creates `frostvault-system-<8 random [a-z0-9]>` once, in
  `AWS_DEFAULT_REGION`, with the same configuration, Object Ownership
  `BucketOwnerEnforced` (ACLs off), a lifecycle with the seven-day abort of
  incomplete multipart uploads, the tag `frostvault:role=system` and **no Object
  Lock** (backup retention must be able to delete). The name is saved in the
  catalog (`system_bucket`) before the bucket is created: an interrupted creation
  resumes on the same name (`BucketAlreadyOwnedByYou` counts as created), and a
  name S3 answers with `BucketAlreadyExists` is replaced by a new random one;
  after three taken names in a row the attempt fails. Any other error, including
  `AccessDenied`, is reported and never changes the name. The background loop
  creates it soon after start (retrying every 15 minutes and opening a Problem
  while it fails), and every backup verifies it again: the baseline above,
  Object Ownership `BucketOwnerEnforced`, FrostVault's abort rule unchanged and
  no Object Lock. **Lifecycle rules and tags the Owner adds are allowed** (for
  example a noncurrent-version expiry); a missing or changed check is refused,
  not repaired, and a deleted bucket is not recreated. Later changes of
  `AWS_DEFAULT_REGION` do not move it.
- **Neither `METADATA_BACKUP_BUCKET`, AWS credentials nor any Vault**
  (placeholders and an empty catalog, as in local development): backups stay
  local-only and there is no Recovery Kit. Any Vault in the catalog (#735; its
  bucket, Vault Bucket or shared, holds content the catalog names) alone also
  requires the off-host copy: missing credentials or IAM permissions then fail
  each backup (recorded and notified) instead of silently keeping it local-only.
  No Vault's bucket ever becomes the System Bucket.
  Credentials count when `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` are real
  values, or `AWS_PROFILE`, `AWS_WEB_IDENTITY_TOKEN_FILE` or a container
  credentials variable is set; an instance role alone is not probed: set
  `AWS_PROFILE` or `METADATA_BACKUP_BUCKET` in that case.

The General page names the System Bucket next to the metadata backups and in the
Recovery Kit panel. Setting `METADATA_BACKUP_BUCKET` later switches new backups
to it; a bucket FrostVault created stays in AWS with its backups.

### IAM permissions

Attach [`frostvault-buckets-policy.json`](../infra/terraform/archive-bucket/iam/frostvault-buckets-policy.json)
to the application identity. It is scoped to `arn:aws:s3:::frostvault-*` and
grants `s3:CreateBucket`, `s3:ListBucket`, `s3:GetBucketLocation`, the get/put
pairs for Block Public Access, versioning, encryption, lifecycle, tagging,
Object Ownership (`s3:Get/PutBucketOwnershipControls`) and Object Lock
(`s3:Get/PutBucketObjectLockConfiguration`, used only for a Vault Bucket's
Governance Retention Lock, never on the System Bucket), and the object
operations of Vault Buckets (#735; for metadata backups `s3:PutObject` and
`s3:GetObject`), listed in
[aws-s3-bucket.md](aws-s3-bucket.md#iam-for-frostvault-created-buckets). It
explicitly denies `s3:BypassGovernanceRetention`: FrostVault never has it. It
never grants `s3:DeleteBucket`. Ownership of a new bucket is decided by the `CreateBucket`
error codes alone, so no `sts:GetCallerIdentity` or `ExpectedBucketOwner` is
involved. An operator-supplied `METADATA_BACKUP_BUCKET` outside `frostvault-*`
needs `s3:GetBucketLocation`, `s3:GetBucketVersioning`,
`s3:GetBucketPublicAccessBlock`, `s3:GetEncryptionConfiguration`, `s3:PutObject`
and `s3:GetObject` on that bucket instead; FrostVault never configures it.

### Upgrading to the System Bucket

- Attach the IAM policy above before upgrading, or set `METADATA_BACKUP_BUCKET`.
  With any Vault in the catalog, backups keep requiring the off-host copy: without
  the policy each backup fails with `S3 refused s3:CreateBucket (AccessDenied)`
  (the local copy stays on disk) rather than silently becoming local-only.
- Migration `0008_system_bucket` adds the `system_bucket` row and the nullable
  `metadata_backup_runs.s3_bucket`; it makes no S3 calls and invents nothing for
  earlier runs.
- Backups made before the upgrade stay under `system/backups/` in the shared
  Vault bucket (`VAULT_S3_BUCKET` before #735), where the previous Recovery Kit points: nothing is moved,
  copied or deleted. Keep them, with the previous kit, as long as you need them.
- The Recovery Kit now names the System Bucket (format
  `frostvault-recovery-kit-v2`), so its **fingerprint changes**. The new kit is
  offered only after the first metadata backup succeeds in the System Bucket;
  until then keep the previous kit. Then download the new kit, store it outside
  the server and confirm custody again.

### Schema revision

A metadata backup restores into the release that made it or into a later one
([ADR-0035](adr/0035-database-rebuilt-from-scratch.md),
[ADR-0041](adr/0041-linear-migrations-after-the-frozen-baseline.md)).
Verification reads the dump's `alembic_version` before restoring anything and
accepts only a revision of the linear chain that starts at `0001_baseline`
(v0.6.0 and later); a restored catalog of an earlier revision is upgraded in
place with the chain's own migrations (step 5). A v0.5.x backup, or a revision
this release does not know (made by a newer release), is refused. Nothing
outside the chain is ever stamped, upgraded, reset or adopted.

## Main route: manual PostgreSQL 16 restore

Use the matching FrostVault release and PostgreSQL 16 client/server tools. The
image includes `pg_dump`, `pg_restore`, `createdb`, `dropdb`, `psql`. Keep AWS and
libpq credentials in protected profiles/environment files or `.pgpass`, never
literal process arguments, logs, reports or shell history. Use `umask 077` for
the private scratch directory. The downloaded kit includes these steps offline.

1. **Stop FrostVault.** Preserve the original database until recovery is proven.
2. Retrieve the independently custodied kit and obtain AWS/database access.
   Download the chosen artifact and `.sha256` sidecar from `system/backups/` in
   the kit's System Bucket (`aws s3 ls` / `aws s3 cp`, not `sync`); backups made
   before the System Bucket are in the shared Vault bucket named by the previous
   kit. On a new server, restore before FrostVault's first start: the restored
   catalog names the System Bucket it created, so no second one is made. Set
   `METADATA_BACKUP_BUCKET` again only if the original installation had it. Export `ARTIFACT` with its local
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

4. Validate metadata counts and revision: the revision must be the one of the
   release you run or an earlier one of v0.6+ (upgraded in step 5); FrostVault
   refuses any other catalog (recover a newer one with the release that made
   it; there is no stamp). Point `PGDATABASE` at the restored database and **unset
   `DATABASE_URL`**, or securely replace that DSN with the restored database's
   DSN: a configured `DATABASE_URL` takes precedence over `PG*` for FrostVault
   commands. Verify the target before any application command. Reapply
   non-secret deployment settings as needed.
5. **Before starting the app**, upgrade the restored catalog to this release's
   schema revision (a no-op when it is already there; an empty database or a
   catalog outside the chain is refused) and register the actual backup date
   from the saved manifest against that restored database:

   ```bash
   python - <<'PY'
   import json
   from pathlib import Path
   from app.config import settings
   from app.database import db
   from app.schema_guard import upgrade_schema
   from app.services.recovery_kit import record_metadata_restore
   upgrade_schema(settings.database_dsn)
   with db() as connection:
       record_metadata_restore(connection, json.loads(Path('restore-manifest.json').read_text())['created_at'])
   PY
   ```

   This also invalidates restored local/mount observations and cancels stale
   nonterminal Jobs without filesystem/S3 operations: request wanted work again
   after reconnect. Unfinished deletion targets are retired too, so cancelled
   consent cannot block later protection; already deleted evidence is retained.
   Every active Vault stays suspended until explicitly reconnected, even if an
   old inode happens to match. Stopped folders stay
   stopped; cancelled deletion requests never resume protection. Retained cloud
   content from stopped folders is recovered with the manual S3 route below.
   Recoveries that were waiting for a Deep Archive or Glacier restore are
   cancelled too: request them again with «Recupera» (Recover). The restore
   already requested from AWS goes on, and while its temporary copy lasts the
   new Recovery downloads it without a new restore charge.
   Do not start workers
   against the restored catalog before this step.
6. Configure AWS, OIDC environment and `BOOTSTRAP_ADMIN_*` securely. Start the
   app and sign in. The current Emergency Password is reapplied at startup and
   accepted only from loopback/allowed LAN; the restored OIDC Identity remains.
7. **Reconnect Folder for each Vault.** Select any supported direct Source
   Volume and relative directory (empty for that volume's root). This explicitly
   accepts the mount for this Vault only, not its siblings. Allocation/containment
   and Job/scan concurrency gates remain mandatory. No files are moved.
   A Vault created from the UI lives in its **own managed folder**
   `/sources/managed/<Vault uuid>`: put that folder (same uuid, same content)
   back under the new `/sources/managed` before starting the app, then choose
   **This Vault's own managed folder** as the destination
   (`{"volume_alias":"managed","relative_path":"<Vault uuid>"}` in the API).
   Nothing reconnects automatically. FrostVault never creates the folder: if it
   is missing, not a directory or a symbolic link, reconnect is refused with
   *Managed folder missing*. Another Vault's managed folder, any other path
   under `managed`, and moving a custom Source Volume Vault into `managed` are
   refused. A managed Vault may still reconnect to a custom Source Volume.
8. Reconnect reconciles S3 **without cloud deletions**, then rehashes the complete
   local tree. Matching files become **Protected**, absent files **Cloud-only**,
   different content **Cloud-only Conflict** with upload held back. An empty
   folder is valid. Full scan/verification failure keeps local work suspended,
   across restart; retry the scan or explicitly reconnect another valid folder.
   Unknown newer cloud content is read by exact VersionId before becoming
   verified evidence, including files uploaded after the backup, which are
   adopted without uploading them again. A version replaced after the backup,
   and already cleaned up from S3, is closed as superseded, not reported
   missing. GLACIER/DEEP_ARCHIVE content needs the manual S3 restore request
   below before reconnect can finish: until then reconnect stays suspended.
   No size/mtime-only shortcut.

The recovery panel keeps a durable warning: **“Restored backup dated <date>:
<N> files updated from the cloud”**, including new files absent from the backup.
The count is distinct Vault Files, not every scan or every historical VersionId;
reading notifications or restarting does not erase it. No routine per-file
reconciliation notifications are added. Settings changed after the backup remain
lost and must be reapplied. Retire scratch plaintext dumps under your retention
policy only after validation.

## Emergency route: files only

With no readable metadata backup, use any authorized S3 tool. Original bytes and
names are stored as plaintext objects; the master key is not needed for them.
Vault content is not in the System Bucket: list the account's buckets
(`aws s3 ls`) and set non-secret `BUCKET` and `REGION` to the bucket holding the
Vault.

- A **Vault Bucket** (every Vault created since #735) is
  `frostvault-<vault name>-<8 random>` and holds one Vault at its **root**: the
  key of a file is its relative path, so `PREFIX=""`. Its tags name the Vault
  (`aws s3api get-bucket-tagging --bucket "$BUCKET"`: `frostvault:vault` is its
  UUID, `frostvault:name` its name); `frostvault-system-*` is the System Bucket.
- An **older Vault** keeps the shared bucket of its installation, with its files
  at `vaults/<uuid>/<relative-path>`: list `vaults/` and set `PREFIX` to
  `vaults/<uuid>/`.

Download to a new empty destination:

```bash
aws s3api get-bucket-tagging --bucket "$BUCKET" --region "$REGION"   # a Vault Bucket
aws s3 ls "s3://$BUCKET/vaults/" --region "$REGION"                  # a shared bucket
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

## Older formats

PostgreSQL is the only supported backend. Catalogs and metadata backups of
v0.5.x and earlier, including SQLite artifacts, are refused by v0.6: the
database was rebuilt from scratch and its old cloud namespaces are not imported
([ADR-0035](adr/0035-database-rebuilt-from-scratch.md)). Keep the previous
release and its artifacts to read them, and recover their files with the
emergency route above. Old metadata ciphertext still needs its original master
key.
