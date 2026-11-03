"""Installation Recovery Kit: no catalog reads, no AWS credentials, no logging.

The secret-bearing payload exists only in the authenticated no-store download.
Status and custody use a public, domain-separated fingerprint instead.
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet

from ..config import settings


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RecoveryKitUnavailable(ValueError):
    pass


def kit_fingerprint(config: Any = None) -> str:
    config = config or settings
    try:
        raw = str(config.archive_master_key).encode("ascii")
        Fernet(raw)
        key = base64.urlsafe_b64decode(raw)
    except (ValueError, TypeError, UnicodeError):
        raise RecoveryKitUnavailable("Recovery Kit requires a valid ARCHIVE_MASTER_KEY") from None
    if not config.vault_s3_bucket or not config.aws_region:
        raise RecoveryKitUnavailable("Recovery Kit requires bucket and region configuration")
    identity = json.dumps([config.vault_s3_bucket, config.aws_region], separators=(",", ":")).encode()
    return hashlib.sha256(b"frostvault-recovery-kit-v1\0" + key + identity).hexdigest()[:24]


INSTRUCTIONS = """FrostVault — manual disaster recovery (PostgreSQL 16, plaintext files)

Keep this entire kit OFF this server, preferably in an independently hosted
password manager, and compare its fingerprint with FrostVault after changes.
The master key decrypts metadata backups only. AWS credentials are NOT included;
recover AWS access through your own account, with MFA and bucket-scoped IAM.
Never paste the kit/key/credentials into logs, reports, command arguments or chat.

MAIN ROUTE: restore metadata plus files
1. Stop FrostVault. Keep the old database unchanged. Use the matching application
   release and PostgreSQL 16 tools. Obtain AWS access via a private AWS profile.
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
   Validate counts and schema revision with the matching release. Point PGDATABASE
   at frostvault_restored and run python -m app.backup_upgrade if an upgrade is
   required. Never force-stamp an unknown/ahead revision. Preserve deployment
   settings independently; changes after the backup must be reapplied manually.
5. BEFORE starting FrostVault, register the backup date in that restored database:
   python - <<'PY'
import json
from pathlib import Path
from app.database import db
from app.services.recovery_kit import record_metadata_restore
with db() as connection:
    record_metadata_restore(connection, json.loads(Path('restore-manifest.json').read_text())['created_at'])
PY
6. Configure AWS, OIDC_* and BOOTSTRAP_ADMIN_* securely. The Emergency Password
   is accepted only from loopback/allowed LAN. Start FrostVault, then explicitly
   Reconnect folder for EACH Vault, selecting the new Source Volume and directory.
   S3 is reconciled first without deleting cloud copies; a mandatory full local
   scan rehashes everything. Matching files become Protected, absent files
   Cloud-only, different files Cloud-only Conflicts (no automatic replacement).
   Failed/incomplete scans stay suspended and retryable. Newly discovered cloud
   versions need exact-version content verification; cold objects may first need
   the restore request below. Review the durable backup-date/updated-files warning.
   No in-app cloud-only catalog reconstruction exists.

EMERGENCY ROUTE: files only, no usable metadata backup
The objects are original bytes with readable names under vaults/<uuid>/path.
The kit intentionally has NO Vault list, file list, catalog or per-Vault secrets.
Use any authorized S3 tool; first list prefixes, then set PREFIX to vaults/UUID/:
   aws s3 ls "s3://$BUCKET/vaults/" --region "$REGION"
   aws s3 ls "s3://$BUCKET/$PREFIX" --recursive --region "$REGION"
   aws s3 cp "s3://$BUCKET/$PREFIX" ./recovered/ --recursive --region "$REGION"
Use an empty destination. These commands do not remove anything from S3.
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


def build_recovery_kit(config: Any = None) -> bytes:
    config = config or settings
    fingerprint = kit_fingerprint(config)
    return json.dumps({
        "format": "frostvault-recovery-kit-v1",
        "fingerprint": fingerprint,
        "ARCHIVE_MASTER_KEY": config.archive_master_key,
        "bucket": config.vault_s3_bucket,
        "region": config.aws_region,
        "instructions": INSTRUCTIONS,
    }, ensure_ascii=False, indent=2).encode("utf-8")


def kit_status(connection: Any, config: Any = None) -> dict[str, Any]:
    try:
        fingerprint = kit_fingerprint(config)
    except RecoveryKitUnavailable:
        fingerprint = None
    custody = connection.execute("SELECT * FROM recovery_kit_custody WHERE id=1").fetchone()
    restore = connection.execute("SELECT * FROM metadata_restore_state WHERE id=1").fetchone()
    count = connection.execute("SELECT COUNT(*) AS n FROM metadata_restore_updates").fetchone()["n"]
    acknowledged = bool(fingerprint and custody and custody["fingerprint"] == fingerprint)
    return {"configured": fingerprint is not None, "fingerprint": fingerprint,
            "custody_acknowledged": acknowledged,
            "acknowledged_at": custody["acknowledged_at"] if acknowledged else None,
            "metadata_restore": {"backup_created_at": restore["backup_created_at"],
                                 "updated_files": int(count)} if restore else None}


def acknowledge_custody(connection: Any, fingerprint: str, config: Any = None) -> None:
    if fingerprint != kit_fingerprint(config):
        raise ValueError("Recovery Kit fingerprint changed; download the current kit")
    connection.execute("""
        INSERT INTO recovery_kit_custody(id, fingerprint, acknowledged_at) VALUES (1,%s,%s)
        ON CONFLICT(id) DO UPDATE SET fingerprint=excluded.fingerprint,
            acknowledged_at=excluded.acknowledged_at
        """, (fingerprint, now_iso()))


def record_metadata_restore(connection: Any, backup_created_at: str) -> None:
    """Manual restore seam; invoke only against the restored isolated database."""
    stamp = datetime.fromisoformat(backup_created_at)
    if stamp.tzinfo is None:
        raise ValueError("Backup date must include its timezone")
    connection.execute("DELETE FROM metadata_restore_updates")
    connection.execute("""
        INSERT INTO metadata_restore_state(id, backup_created_at, recorded_at) VALUES (1,%s,%s)
        ON CONFLICT(id) DO UPDATE SET backup_created_at=excluded.backup_created_at,
            recorded_at=excluded.recorded_at
        """, (stamp.astimezone(timezone.utc).isoformat(), now_iso()))


def remember_cloud_update(connection: Any, vault_file_id: str) -> None:
    connection.execute("""
        INSERT INTO metadata_restore_updates(vault_file_id, observed_at)
        SELECT %s,%s WHERE EXISTS (SELECT 1 FROM metadata_restore_state WHERE id=1)
        ON CONFLICT(vault_file_id) DO NOTHING
        """, (vault_file_id, now_iso()))
