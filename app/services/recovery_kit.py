"""Installation Recovery Kit: no catalog reads, no AWS credentials, no logging.

The secret-bearing payload exists only in the authenticated no-store download.
Status and custody use a public, domain-separated fingerprint instead. The kit
names the System Bucket and its region (#734): a new System Bucket, or a new
METADATA_BACKUP_BUCKET, changes the fingerprint and asks for custody again.
The kit is offered only once a metadata backup run has succeeded in the
current System Bucket, so it never names a bucket that holds no backup.
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet

from ..config import settings
from ..schema_guard import require_current_schema
from . import system_bucket


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RecoveryKitUnavailable(ValueError):
    pass


KIT_FORMAT = "frostvault-recovery-kit-v2"


def _ready(bucket: system_bucket.SystemBucket | None) -> system_bucket.SystemBucket:
    if bucket is None or not bucket.ready or not bucket.name or not bucket.region:
        raise RecoveryKitUnavailable(
            "Recovery Kit is available once a metadata backup has succeeded in the "
            "System Bucket (METADATA_BACKUP_BUCKET, or the one FrostVault creates with "
            "AWS credentials). Until then keep the previous kit: it points to the "
            "earlier backups, in the bucket it names")
    return bucket


def kit_fingerprint(config: Any = None, *,
                    bucket: system_bucket.SystemBucket | None = None) -> str:
    """Fingerprint of the master key and the System Bucket's name and region."""
    config = config or settings
    try:
        raw = str(config.archive_master_key).strip().encode("ascii")
        Fernet(raw)
        key = base64.urlsafe_b64decode(raw)
    except (ValueError, TypeError, UnicodeError):
        raise RecoveryKitUnavailable("Recovery Kit requires a valid ARCHIVE_MASTER_KEY") from None
    bucket = _ready(bucket)
    identity = json.dumps([bucket.name, bucket.region], separators=(",", ":")).encode()
    return hashlib.sha256(KIT_FORMAT.encode() + b"\0" + key + identity).hexdigest()[:24]


INSTRUCTIONS = """FrostVault — manual disaster recovery (PostgreSQL 16, plaintext files)

Keep this entire kit OFF this server, preferably in an independently hosted
password manager, and compare its fingerprint with FrostVault after changes.
The master key decrypts metadata backups only. AWS credentials are NOT included;
recover AWS access through your own account, with MFA and bucket-scoped IAM.
Never paste the kit/key/credentials into logs, reports, command arguments or chat.

MAIN ROUTE: restore metadata plus files
BUCKET and REGION below are this kit's system_bucket and region: the System
Bucket that receives the metadata backups. Backups made before FrostVault had a
System Bucket stay under system/backups/ in the shared Vault bucket, which the
previous kit names: keep that kit while you need them.
1. Stop FrostVault. Keep the old database unchanged. Use the matching application
   release and PostgreSQL 16 tools. Obtain AWS access via a private AWS profile.
   On a new server, restore before the first start: the restored catalog names
   the System Bucket FrostVault created. Set METADATA_BACKUP_BUCKET again only
   if the original installation had it.
2. Download the chosen backup and .sha256 sidecar from system/backups/:
   aws s3 ls "s3://$BUCKET/system/backups/" --region "$REGION"
   aws s3 cp "s3://$BUCKET/system/backups/$ARTIFACT" ./ --region "$REGION"
   aws s3 cp "s3://$BUCKET/system/backups/$ARTIFACT.sha256" ./ --region "$REGION"
   Set BUCKET, REGION and ARTIFACT to the non-secret values in this kit/listing.
3. Set ARCHIVE_MASTER_KEY securely from this kit, through a protected environment
   file or a hidden prompt (never a literal shell argument):
   read -rs -p 'Master key: ' ARCHIVE_MASTER_KEY; export ARCHIVE_MASTER_KEY; printf '\\n'
   In a private scratch directory (umask 077), with ARTIFACT exported:
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
4. Configure libpq PG* / .pgpass securely for a NEW EMPTY isolated database, not
   production. Never put database credentials in arguments or output.
   createdb frostvault_restored
   pg_restore --exit-on-error --no-owner --no-acl -d frostvault_restored restore.dump
   Validate counts and the schema revision with the matching release:
   psql -d frostvault_restored -At -c 'SELECT version_num FROM alembic_version'
   FrostVault accepts the revision of this release or an earlier v0.6+ one
   (upgraded in step 5) and refuses any other: it never stamps or adopts a
   v0.5.x or unknown catalog. Point
   PGDATABASE at frostvault_restored and UNSET DATABASE_URL (or securely replace
   it with the restored DSN): FrostVault's DATABASE_URL takes precedence over
   PG*. Verify the target. Preserve deployment settings independently; changes
   after the backup must be reapplied manually.
5. BEFORE starting FrostVault, upgrade the restored catalog to this release's
   revision (a no-op when it is already there) and register the backup date:
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
   Registration invalidates active Vault local/mount observations and cancels
   stale nonterminal Jobs without touching local/S3 bytes. Pending Delayed
   Cloud Deletions are withdrawn; completed cleanup evidence is retained.
   Request wanted work again after reconnect; stopped folders remain stopped.
6. Configure AWS, OIDC_* and BOOTSTRAP_ADMIN_* securely. The Emergency Password
   is accepted only from loopback/allowed LAN. Start FrostVault, then explicitly
   Reconnect folder for EACH Vault, selecting the new Source Volume and directory.
   S3 is reconciled first without deleting cloud copies; a mandatory full local
   scan rehashes everything. Matching files become Protected, absent files
   Cloud-only, different files Cloud-only Conflicts (no automatic replacement).
   Failed/incomplete scans stay suspended and retryable. A newer cloud version
   of a catalogued file (uploaded after the backup) becomes Current only after
   its exact VersionId is read and hashed; a cold one first needs the restore
   request below, and the reconnect stays suspended until then. Objects at
   paths the restored catalog does not know are never imported or deleted.
   Review the durable backup-date/updated-files warning. No in-app cloud-only
   catalog reconstruction exists.

EMERGENCY ROUTE: files only, no usable metadata backup
The objects are original bytes with readable names: the relative path of each
file is its key. The kit intentionally has NO Vault list, file list, catalog or
per-Vault secrets. Vault content is NOT in the System Bucket. List your
account's buckets:
   aws s3 ls
A Vault created by a release with Vault Buckets has its own bucket, named
frostvault-<vault name>-<8 random characters>, with the files at the bucket
ROOT (no prefix). The tags identify each bucket: frostvault:vault (the Vault
UUID) and frostvault:name (its name) mark a Vault Bucket; the tag
frostvault:role=system marks the System Bucket FrostVault created, named
frostvault-system-<8 random characters>. It is never a Vault: no Vault Bucket
name starts with frostvault-system-.
   aws s3api get-bucket-tagging --bucket "$BUCKET"
   aws s3api get-bucket-location --bucket "$BUCKET"
For such a bucket set BUCKET to its name, REGION to its region and PREFIX to
the empty string:
   PREFIX=""
An older Vault keeps the shared bucket of its installation, with its files
under vaults/<uuid>/ (one folder per Vault). For it set BUCKET and REGION to
that bucket, list the folders, then set PREFIX to vaults/UUID/:
   aws s3 ls "s3://$BUCKET/vaults/" --region "$REGION"
Then, with any authorized S3 tool:
   aws s3 ls "s3://$BUCKET/$PREFIX" --recursive --region "$REGION"
   aws s3 cp "s3://$BUCKET/$PREFIX" ./recovered/ --recursive --region "$REGION"
Use an empty destination. These commands do not remove anything from S3.
AWS CLI does NOT apply FrostVault's original file modification time automatically.
New uploads store signed Unix nanoseconds in S3 metadata frostvault-mtime-ns.
After each download, read metadata from the SAME exact VersionId (not LastModified)
and apply it, for example after setting KEY and VERSION for ./recovered-file:
   MTIME_NS=$(aws s3api head-object --bucket "$BUCKET" --key "$KEY" --version-id "$VERSION" --region "$REGION" --query 'Metadata."frostvault-mtime-ns"' --output text)
   if [ "$MTIME_NS" != None ] && [ -n "$MTIME_NS" ]; then
     python3 -c 'import os,sys; p=sys.argv[1]; ns=int(sys.argv[2]); os.utime(p, ns=(os.stat(p).st_atime_ns,ns), follow_symlinks=False)' ./recovered-file "$MTIME_NS"
   fi
Older uploads may lack this metadata: their original modification time is unknown.
Since v0.8.0 the same metadata also records the original owner and mode
(frostvault-uid, frostvault-gid, frostvault-mode in octal). As root, apply them
the same way, mode first:
   META=$(aws s3api head-object --bucket "$BUCKET" --key "$KEY" --version-id "$VERSION" --region "$REGION" --query 'Metadata.["frostvault-uid","frostvault-gid","frostvault-mode"]' --output text)
   set -- $META
   if [ "$1" != None ] && [ -n "$3" ]; then chmod "$3" ./recovered-file && chown "$1:$2" ./recovered-file; fi
S3 LastModified is the upload/copy time, NOT the original file modification time.
For GLACIER/DEEP_ARCHIVE, set KEY to the exact key and VERSION to its VersionId:
   aws s3api list-object-versions --bucket "$BUCKET" --prefix "$PREFIX" --region "$REGION"
   aws s3api restore-object --bucket "$BUCKET" --key "$KEY" --version-id "$VERSION" --restore-request '{"Days":7,"GlacierJobParameters":{"Tier":"Bulk"}}' --region "$REGION"
   aws s3api head-object --bucket "$BUCKET" --key "$KEY" --version-id "$VERSION" --region "$REGION"
Wait until Restore reports ongoing-request=false, then download the exact version:
   aws s3api get-object --bucket "$BUCKET" --key "$KEY" --version-id "$VERSION" --region "$REGION" ./recovered-file
Inspect timestamps, current versions and Delete Markers if replacement was
interrupted. Preserve all versions while diagnosing; never delete a supposed
'extra' based on an old catalog. This recovers files, NOT catalog/settings/history.
Cold retrieval, requests and transfer may incur AWS charges.
"""


def current_bucket(connection: Any = None, config: Any = None) -> system_bucket.SystemBucket | None:
    """The current System Bucket, ready or not; reads the saved row, never S3."""
    return system_bucket.location(config or settings, connection=connection)


def has_backup_in(connection: Any, bucket: system_bucket.SystemBucket) -> bool:
    """Whether a metadata backup run has succeeded with its copy in ``bucket``."""
    row = connection.execute(
        "SELECT 1 FROM metadata_backup_runs WHERE s3_bucket=%s "
        "AND status IN ('succeeded', 'verified') LIMIT 1", (bucket.name,)).fetchone()
    return row is not None


def kit_bucket(connection: Any, config: Any = None) -> system_bucket.SystemBucket | None:
    """The System Bucket the kit names: ready and holding a succeeded backup."""
    bucket = current_bucket(connection, config)
    if bucket is None or not bucket.ready or not has_backup_in(connection, bucket):
        return None
    return bucket


def build_recovery_kit(config: Any = None, *,
                       bucket: system_bucket.SystemBucket | None = None) -> bytes:
    config = config or settings
    fingerprint = kit_fingerprint(config, bucket=bucket)
    bucket = _ready(bucket)
    return json.dumps({
        "format": KIT_FORMAT,
        "fingerprint": fingerprint,
        "ARCHIVE_MASTER_KEY": config.archive_master_key.strip(),
        "system_bucket": bucket.name,
        "region": bucket.region,
        "instructions": INSTRUCTIONS,
    }, ensure_ascii=False, indent=2).encode("utf-8")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def kit_status(connection: Any, config: Any = None) -> dict[str, Any]:
    bucket = current_bucket(connection, config)
    named = kit_bucket(connection, config)
    try:
        fingerprint = kit_fingerprint(config, bucket=named)
    except RecoveryKitUnavailable:
        fingerprint = None
    custody = connection.execute("SELECT * FROM recovery_kit_custody WHERE id=1").fetchone()
    restore = connection.execute("SELECT * FROM metadata_restore WHERE id=1").fetchone()
    count = connection.execute("SELECT COUNT(*) AS n FROM metadata_restore_updates").fetchone()["n"]
    acknowledged = bool(fingerprint and custody and custody["fingerprint"] == fingerprint)
    return {"configured": fingerprint is not None, "fingerprint": fingerprint,
            "system_bucket": bucket.public() if bucket is not None else None,
            # A System Bucket exists but no backup has succeeded in it yet.
            "awaiting_backup": bucket is not None and named is None,
            "custody_acknowledged": acknowledged,
            "acknowledged_at": _iso(custody["acknowledged_at"]) if acknowledged else None,
            "metadata_restore": {"backup_created_at": _iso(restore["backup_created_at"]),
                                 "updated_files": int(count)} if restore else None}


def acknowledge_custody(connection: Any, fingerprint: str, config: Any = None) -> None:
    try:
        current = kit_fingerprint(config, bucket=kit_bucket(connection, config))
    except RecoveryKitUnavailable:
        current = None
    if fingerprint != current:
        raise ValueError("Recovery Kit fingerprint changed; download the current kit")
    connection.execute("""
        INSERT INTO recovery_kit_custody(id, fingerprint, acknowledged_at) VALUES (1,%s,%s)
        ON CONFLICT(id) DO UPDATE SET fingerprint=excluded.fingerprint,
            acknowledged_at=excluded.acknowledged_at
        """, (fingerprint, now_iso()))


RESTORE_CANCEL_MESSAGE = "Metadata restored; request this operation again after reconnect"


def record_metadata_restore(connection: Any, backup_created_at: str) -> None:
    """Manual restore seam; invoke only against the restored isolated database.

    The schema guard runs first: a restored catalog that is not at this
    release's revision is refused before any write (ADR-0035); one of an
    earlier v0.6+ release is upgraded beforehand with ``upgrade_schema``.

    The app must be stopped. Old Jobs and local inode/mtime evidence cannot
    acquire authority on a replacement server, even if pathnames still match:

    - every allocated, not stopping Vault enters ``reconnect_required`` with
      no accepted root identity, and is queued for the cloud reconciliation
      of its first Reconnect;
    - every nonterminal Job is cancelled. Cancelling a ``cloud-purge`` Job
      withdraws its deletion consent: no deletion survives the restore. A
      stopping Vault stays stopping; its purge must be requested again;
    - nothing touches local files or S3. Completed cleanup and deletion
      evidence (rows already removed with their proof) stays as it is.
    """
    stamp = datetime.fromisoformat(backup_created_at)
    if stamp.tzinfo is None:
        raise ValueError("Backup date must include its timezone")
    require_current_schema(connection)
    recorded = now_iso()
    connection.execute("""
        UPDATE vaults SET lifecycle='reconnect_required', root_identity=NULL,
            root_identity_version=NULL, reconnect_volume_fingerprint=NULL
        WHERE lifecycle IN ('active','reconnect_required')
        """)
    connection.execute("DELETE FROM metadata_restore_vaults")
    connection.execute("""
        INSERT INTO metadata_restore_vaults(vault_id)
        SELECT id FROM vaults WHERE lifecycle='reconnect_required'
        """)
    connection.execute("""
        UPDATE jobs SET status='cancelled', updated_at=%s, finished_at=%s,
            message=%s, message_key=NULL, message_params=NULL
        WHERE status NOT IN ('completed','failed','cancelled')
        """, (recorded, recorded, RESTORE_CANCEL_MESSAGE))
    connection.execute("DELETE FROM metadata_restore_updates")
    connection.execute("""
        INSERT INTO metadata_restore(id, backup_created_at, recorded_at) VALUES (1,%s,%s)
        ON CONFLICT(id) DO UPDATE SET backup_created_at=excluded.backup_created_at,
            recorded_at=excluded.recorded_at
        """, (stamp.astimezone(timezone.utc).isoformat(), recorded))


def restore_reconciliation_pending(connection: Any, vault_id: int) -> bool:
    """Whether this Vault's Reconnect still reconciles a metadata restore."""
    return connection.execute(
        "SELECT 1 FROM metadata_restore_vaults WHERE vault_id=%s", (int(vault_id),),
    ).fetchone() is not None


def finish_restore_reconciliation(connection: Any, vault_id: int) -> None:
    connection.execute(
        "DELETE FROM metadata_restore_vaults WHERE vault_id=%s", (int(vault_id),),
    )


def remember_cloud_update(connection: Any, file_id: str) -> None:
    """Count a Vault File found updated in the cloud after the restored backup."""
    connection.execute("""
        INSERT INTO metadata_restore_updates(file_id, observed_at)
        SELECT %s,%s WHERE EXISTS (SELECT 1 FROM metadata_restore WHERE id=1)
        ON CONFLICT(file_id) DO NOTHING
        """, (file_id, now_iso()))
