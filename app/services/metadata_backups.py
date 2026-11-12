"""Encrypted metadata database backups and disaster recovery (issue #15).

Creates rotated, encrypted, checksummed backups of the application metadata
database plus a reconstructable configuration snapshot. The application master
key is used to seal artifacts but is never written into them.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from cryptography.fernet import Fernet, InvalidToken

from ..config import Settings, is_placeholder, settings


S3_BACKUP_PREFIX = "system/backups/"


class BackupError(RuntimeError):
    """Raised when a metadata backup or restore verification fails."""


class BackupConfigurationError(BackupError):
    """A deployment setting prevents a required metadata backup."""


class MissingMasterKeyError(BackupConfigurationError):
    """The deployment has not supplied a usable backup encryption key."""


class InvalidMasterKeyError(BackupConfigurationError):
    """The deployment supplied an invalid backup encryption key."""


class ObjectStoreUnavailableError(BackupConfigurationError):
    """A configured object store could not be initialized."""


class ObjectStoreUploadError(BackupError):
    """An encrypted artifact could not be uploaded to configured off-host storage."""


class ObjectStoreVerificationError(BackupError):
    """An uploaded encrypted artifact could not be read back and verified."""


class ObjectStore(Protocol):
    """System boundary for durable backup object storage (S3 or compatible)."""

    def put_bytes(self, key: str, body: bytes) -> None: ...

    def get_bytes(self, key: str) -> bytes: ...

    def list_keys(self, prefix: str) -> list[str]: ...

    def delete_key(self, key: str) -> None: ...


class Boto3ObjectStore:
    """S3 ObjectStore adapter scoped to one bucket."""

    def __init__(self, client: Any, bucket: str):
        self._client = client
        self._bucket = bucket

    def put_bytes(self, key: str, body: bytes) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=body)

    def get_bytes(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        return response["Body"].read()

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token = None
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": self._bucket,
                "Prefix": prefix,
            }
            if token:
                kwargs["ContinuationToken"] = token
            response = self._client.list_objects_v2(**kwargs)
            for item in response.get("Contents") or []:
                keys.append(item["Key"])
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
        return keys

    def delete_key(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)


def off_host_backup_configured(settings_obj: Settings | Any | None = None) -> bool:
    """Whether deployment configuration requires an off-host backup copy.

    Empty and documented placeholder buckets intentionally model local-only
    development. Any other bucket value is a production storage commitment,
    not an optional optimization.
    """
    cfg = settings_obj if settings_obj is not None else settings
    bucket = str(getattr(cfg, "vault_s3_bucket", "") or "").strip()
    return bool(bucket) and not is_placeholder(bucket, "example-bucket")


def backup_master_key_configured(master_key: str | None) -> bool:
    """Whether a non-placeholder encryption key was supplied at all.

    This deliberately does not validate the key. A non-empty malformed key is
    a configuration error when a backup is attempted, rather than silently
    opting out of a backup.
    """
    key = (master_key or "").strip()
    return bool(key) and not is_placeholder(key)


def default_object_store(
    *, settings_obj: Settings | Any | None = None
) -> ObjectStore | None:
    """Build the configured S3 object store, or ``None`` for local-only mode.

    A non-placeholder bucket is configured off-host storage. Initialization
    errors are typed and intentionally omit provider exception details, which
    can contain deployment identifiers or credential-adjacent data.
    """
    cfg = settings_obj if settings_obj is not None else settings
    bucket = str(getattr(cfg, "vault_s3_bucket", "") or "").strip()
    if not off_host_backup_configured(cfg):
        return None
    try:
        from ..storage import s3_client

        return Boto3ObjectStore(s3_client(), bucket)
    except Exception:  # noqa: BLE001 - normalize an external boundary
        raise ObjectStoreUnavailableError(
            "Configured metadata backup object store is unavailable"
        ) from None


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fernet(master_key: str) -> Fernet:
    key = (master_key or "").strip()
    if not backup_master_key_configured(key):
        raise MissingMasterKeyError("ARCHIVE_MASTER_KEY is not configured")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError):
        raise InvalidMasterKeyError(
            "ARCHIVE_MASTER_KEY must be a url-safe base64-encoded 32-byte key"
        ) from None


# Explicit allow-list: new settings and arbitrary caller-supplied snapshot keys
# are excluded until reviewed. Credentials never enter the manifest.
_SNAPSHOT_KEYS = frozenset("""
    session_cookie_name csrf_cookie_name
    session_idle_seconds session_absolute_seconds cookie_secure allowed_hosts
    trusted_proxies reauth_window_seconds break_glass_allowed_cidrs
    aws_region scan_interval audit_interval filesystem_watch_enabled
    filesystem_watch_force_polling filesystem_watch_debounce_ms filesystem_watch_poll_ms
    queue_poll_interval operation_concurrency
    bandwidth_limit_kibps upload_verification s3_download_max_concurrency s3_download_multipart_threshold_mib
    s3_download_multipart_chunksize_mib job_progress_min_interval_ms restore_poll_interval
    restore_days restore_tier restore_high_impact_gib restore_high_impact_eur
    restore_approval_hold_seconds oidc_enabled oidc_issuer oidc_client_id oidc_scopes
    oidc_login_ttl_seconds bootstrap_admin_username bootstrap_admin_display_name
    vault_s3_bucket
    metadata_backup_dir metadata_backup_retention metadata_backup_interval_seconds
    metadata_backup_s3_prefix metadata_backup_verify_interval_seconds frontend_dist_dir
    smtp_host smtp_port smtp_username smtp_from_address smtp_to_address smtp_use_tls
""".split())


def build_config_snapshot(source: Settings | Any | None = None) -> dict[str, Any]:
    """Return deployment configuration, never database overrides or secrets."""
    cfg = source if source is not None else settings
    return {key: getattr(cfg, key) for key in sorted(_SNAPSHOT_KEYS) if hasattr(cfg, key)}


def _postgres_env(database_dsn: str | None = None) -> dict[str, str]:
    """Keep connection credentials in child environment, never process arguments."""
    from psycopg.conninfo import conninfo_to_dict

    env = os.environ.copy()
    values = conninfo_to_dict(
        database_dsn if database_dsn is not None else settings.database_dsn
    )
    for key, value in values.items():
        name = {
            "dbname": "PGDATABASE",
            "connect_timeout": "PGCONNECT_TIMEOUT",
            "application_name": "PGAPPNAME",
        }.get(key, "PG" + key.replace("_", "").upper())
        env[name] = value
    return env


def _dump_postgres_bytes(database_dsn: str | None = None) -> bytes:
    """Return a PostgreSQL custom-format dump using pg_dump and PG* env vars."""
    command = [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-acl",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            env=_postgres_env(database_dsn),
        )
    except FileNotFoundError:
        raise BackupError(
            "pg_dump is required for PostgreSQL metadata backups"
        ) from None
    if completed.returncode != 0:
        raise BackupError("pg_dump failed")
    if not completed.stdout:
        raise BackupError("pg_dump produced an empty backup")
    return completed.stdout


def _pack_payload(
    *,
    database_bytes: bytes,
    config: dict[str, Any],
    manifest: dict[str, Any],
) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, raw in (
            ("manifest.json", json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")),
            ("config.json", json.dumps(config, sort_keys=True, indent=2).encode("utf-8")),
            ("database.dump", database_bytes),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    return buffer.getvalue()


def unpack_backup_payload(plaintext: bytes) -> dict[str, Any]:
    """Unpack a decrypted backup payload into manifest/config/database parts."""
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:gz") as archive:
        members = {member.name: archive.extractfile(member) for member in archive.getmembers()}
        missing = {"manifest.json", "config.json", "database.dump"} - set(members)
        if missing:
            raise BackupError(f"Backup payload missing members: {sorted(missing)}")
        manifest = json.loads(members["manifest.json"].read().decode("utf-8"))
        config = json.loads(members["config.json"].read().decode("utf-8"))
        database = members["database.dump"].read()
    return {"manifest": manifest, "config": config, "database": database}


def create_metadata_backup(
    *,
    reason: str,
    backup_dir: str | Path,
    master_key: str | None = None,
    config_snapshot: dict[str, Any] | None = None,
    database_dsn: str | None = None,
) -> dict[str, Any]:
    """Create one encrypted metadata backup artifact on the local backup volume.

    Returns a public artifact descriptor (path, digest, backend, reason, created_at).
    """
    backend = "postgresql"
    key = master_key if master_key is not None else settings.archive_master_key
    fernet = _fernet(key)
    config = config_snapshot if config_snapshot is not None else build_config_snapshot()
    config = {
        key: value
        for key, value in config.items()
        if key in _SNAPSHOT_KEYS
    }

    database_bytes = _dump_postgres_bytes(database_dsn)
    schema_format = "postgresql-custom"

    created_at = now_iso()
    content_digest = hashlib.sha256(database_bytes).hexdigest()
    manifest = {
        "created_at": created_at,
        "reason": reason,
        "backend": backend,
        "schema_format": schema_format,
        "database_sha256": content_digest,
        "s3_prefix": S3_BACKUP_PREFIX,
    }
    plaintext = _pack_payload(
        database_bytes=database_bytes,
        config=config,
        manifest=manifest,
    )
    ciphertext = fernet.encrypt(plaintext)
    digest = hashlib.sha256(ciphertext).hexdigest()

    destination = Path(backup_dir)
    destination.mkdir(parents=True, exist_ok=True)
    stamp = created_at.replace("+00:00", "Z").replace(":", "")
    filename = f"{stamp}-{backend}-{reason}.bak.enc"
    artifact_path = destination / filename
    artifact_path.write_bytes(ciphertext)
    (destination / f"{filename}.sha256").write_text(f"{digest}\n", encoding="utf-8")

    return {
        "path": artifact_path,
        "digest_sha256": digest,
        "backend": backend,
        "reason": reason,
        "created_at": created_at,
        "size_bytes": len(ciphertext),
        "database_sha256": content_digest,
        "filename": filename,
    }


def decrypt_backup_file(path: str | Path, master_key: str | None = None) -> dict[str, Any]:
    key = master_key if master_key is not None else settings.archive_master_key
    ciphertext = Path(path).read_bytes()
    try:
        plaintext = _fernet(key).decrypt(ciphertext)
    except InvalidToken:
        raise BackupError("Unable to decrypt backup with the provided master key") from None
    return unpack_backup_payload(plaintext)


def list_local_backup_files(backup_dir: str | Path) -> list[Path]:
    """Return encrypted backup artifacts newest-first by filename."""
    directory = Path(backup_dir)
    if not directory.exists():
        return []
    return sorted(directory.glob("*.bak.enc"), key=lambda path: path.name, reverse=True)


def rotate_local_retention(backup_dir: str | Path, keep: int) -> list[Path]:
    """Delete oldest local backup artifacts beyond ``keep``; return removed paths."""
    if keep < 0:
        raise BackupError("Backup retention must be >= 0")
    artifacts = list_local_backup_files(backup_dir)
    removed: list[Path] = []
    for path in artifacts[keep:]:
        sidecar = Path(str(path) + ".sha256")
        path.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        removed.append(path)
    return removed


def _normalize_prefix(prefix: str) -> str:
    cleaned = (prefix or S3_BACKUP_PREFIX).strip().lstrip("/")
    if not cleaned.endswith("/"):
        cleaned += "/"
    if not cleaned.startswith("system/backups"):
        raise BackupError(
            "Metadata backups must use the dedicated system/backups/ prefix"
        )
    return cleaned


def store_backup_to_object_store(
    artifact: dict[str, Any],
    *,
    object_store: ObjectStore,
    prefix: str = S3_BACKUP_PREFIX,
) -> dict[str, Any]:
    """Upload and read-back verify an encrypted artifact in off-host storage."""
    normalized = _normalize_prefix(prefix)
    filename = artifact.get("filename") or Path(artifact["path"]).name
    key = f"{normalized}{filename}"
    sidecar_key = key + ".sha256"
    body = Path(artifact["path"]).read_bytes()
    digest = str(artifact["digest_sha256"])
    sidecar = f"{digest}\n".encode("utf-8")
    try:
        object_store.put_bytes(key, body)
        object_store.put_bytes(sidecar_key, sidecar)
    except Exception:  # noqa: BLE001 - external storage boundary
        raise ObjectStoreUploadError(
            "Configured metadata backup upload failed"
        ) from None

    try:
        uploaded = object_store.get_bytes(key)
        uploaded_sidecar = object_store.get_bytes(sidecar_key)
    except Exception:  # noqa: BLE001 - external storage boundary
        raise ObjectStoreVerificationError(
            "Configured metadata backup verification failed"
        ) from None
    if (
        hashlib.sha256(uploaded).hexdigest() != digest
        or uploaded_sidecar != sidecar
    ):
        raise ObjectStoreVerificationError(
            "Configured metadata backup verification failed"
        )
    return {
        "key": key,
        "digest_sha256": digest,
        "size_bytes": len(body),
    }


def verify_restore_isolated(
    artifact_path: str | Path,
    *,
    master_key: str | None = None,
    work_dir: str | Path,
    object_store: ObjectStore | None = None,
) -> dict[str, Any]:
    """Decrypt a backup into a temporary database and verify it opens cleanly.

    Never writes to the live database path and never mutates production vault
    object keys (anything under ``vaults/``).
    """
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    payload = decrypt_backup_file(artifact_path, master_key=master_key)
    backend = payload["manifest"].get("backend")
    if backend != "postgresql" or payload["manifest"].get("schema_format") != "postgresql-custom":
        raise BackupError("Only PostgreSQL custom-format backups are supported")

    vault_keys_before: list[str] = []
    if object_store is not None:
        vault_keys_before = object_store.list_keys("vaults/")

    result = _verify_postgres_dump_isolated(payload["database"], work)
    result["backend"] = backend

    if object_store is not None:
        vault_keys_after = object_store.list_keys("vaults/")
        if vault_keys_after != vault_keys_before:
            raise BackupError("Restore verification mutated production vault objects")

    return result


def _require_current_dump_revision(dump_path: Path, env: dict[str, str]) -> str:
    """Read the dump's Alembic revision without a database; refuse any other."""
    from ..schema_guard import SCHEMA_HEAD

    listed = subprocess.run(
        ["pg_restore", "--data-only", "--table=alembic_version", "--file=-", str(dump_path)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if listed.returncode != 0:
        raise BackupError("PostgreSQL dump failed verification")
    revisions: list[str] = []
    copying = False
    for line in listed.stdout.splitlines():
        if line.startswith("COPY ") and line.split()[1].rsplit(".", 1)[-1] == "alembic_version":
            copying = True
        elif copying and line == "\\.":
            copying = False
        elif copying:
            revisions.append(line)
    if revisions != [SCHEMA_HEAD]:
        found = ", ".join(revision[:64] for revision in revisions[:3]) or "no revision"
        raise BackupError(
            f"Metadata backup refused: its schema revision is {found}, this "
            f"release requires {SCHEMA_HEAD}. Backups of other schemas are "
            "never upgraded or adopted."
        )
    return SCHEMA_HEAD


def _verify_postgres_dump_isolated(database_bytes: bytes, work: Path) -> dict[str, Any]:
    """Validate a PostgreSQL custom dump without touching the live database.

    Prefers restoring into a temporary database. Falls back to ``pg_restore
    --list`` when creating a temporary database is not possible.
    """
    dump_path = work / "restore.dump"
    dump_path.write_bytes(database_bytes)
    env = _postgres_env()
    list_result = subprocess.run(
        ["pg_restore", "--list", str(dump_path)],
        check=False,
        capture_output=True,
        env=env,
    )
    if list_result.returncode != 0:
        raise BackupError("PostgreSQL dump failed verification")
    # Schema guard (ADR-0035): a backup of another schema is refused before
    # anything is restored, even into a scratch database. There is no upgrade.
    schema_revision = _require_current_dump_revision(dump_path, env)

    temp_db_name = f"metadata_backup_verify_{os.getpid()}"
    created = subprocess.run(
        ["createdb", temp_db_name],
        check=False,
        capture_output=True,
        env=env,
    )
    if created.returncode != 0:
        # Listing succeeded; treat as verified when a scratch DB cannot be created.
        return {
            "ok": True,
            "owner_count": None,
            "vault_count": None,
            "schema_revision": schema_revision,
            "temp_db": None,
            "verification_mode": "pg_restore_list",
        }

    try:
        restored = subprocess.run(
            ["pg_restore", "--dbname", temp_db_name, "--no-owner", "--no-acl", str(dump_path)],
            check=False,
            capture_output=True,
            env=env,
        )
        if restored.returncode not in {0, 1}:
            # pg_restore uses 1 for warnings; >1 is hard failure.
            raise BackupError("PostgreSQL isolated restore failed")
        counted = subprocess.run(
            [
                "psql",
                "-d",
                temp_db_name,
                "-At",
                "-c",
                "SELECT "
                "(SELECT COUNT(*) FROM owner), "
                "(SELECT COUNT(*) FROM vaults), "
                "(SELECT version_num FROM alembic_version LIMIT 1)",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        owner_count = vault_count = restored_revision = None
        if counted.returncode == 0 and counted.stdout.strip():
            parts = counted.stdout.strip().split("|")
            if len(parts) >= 3:
                owner_count = int(parts[0])
                vault_count = int(parts[1])
                restored_revision = parts[2] or None
        if owner_count is None or vault_count is None or restored_revision != schema_revision:
            raise BackupError(
                "PostgreSQL isolated restore did not prove schema tables"
            )
        return {
            "ok": True,
            "owner_count": owner_count,
            "vault_count": vault_count,
            "schema_revision": schema_revision,
            "temp_db": temp_db_name,
            "verification_mode": "temp_database",
        }
    finally:
        subprocess.run(["dropdb", "--if-exists", temp_db_name], check=False, capture_output=True, env=env)


def _row_run(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "finished_at": row.get("finished_at"),
        "reason": row["reason"],
        "backend": row["backend"],
        "status": row["status"],
        "digest_sha256": row.get("digest_sha256"),
        "database_sha256": row.get("database_sha256"),
        "local_path": row.get("local_path"),
        "s3_key": row.get("s3_key"),
        "size_bytes": row.get("size_bytes"),
        "error_message": row.get("error_message"),
        "verified_at": row.get("verified_at"),
    }


def record_backup_run(
    connection: Any,
    *,
    reason: str,
    backend: str,
    status: str,
    digest_sha256: str | None = None,
    database_sha256: str | None = None,
    local_path: str | None = None,
    s3_key: str | None = None,
    size_bytes: int | None = None,
    error_message: str | None = None,
    verified_at: str | None = None,
) -> dict[str, Any]:
    stamp = now_iso()
    finished = stamp if status in {"succeeded", "failed", "verified"} else None
    row = connection.execute(
        """
        INSERT INTO metadata_backup_runs(
            created_at, finished_at, reason, backend, status,
            digest_sha256, database_sha256, local_path, s3_key,
            size_bytes, error_message, verified_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            stamp,
            finished,
            reason,
            backend,
            status,
            digest_sha256,
            database_sha256,
            local_path,
            s3_key,
            size_bytes,
            (error_message or "")[:1000] or None,
            verified_at,
        ),
    ).fetchone()
    return _row_run(row)


def list_backup_artifacts(
    connection: Any, *, limit: int = 50
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT * FROM metadata_backup_runs
        ORDER BY id DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [_row_run(row) for row in rows]


def backup_status(connection: Any) -> dict[str, Any]:
    latest = connection.execute(
        """
        SELECT * FROM metadata_backup_runs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    succeeded = connection.execute(
        """
        SELECT COUNT(*) AS total FROM metadata_backup_runs
        WHERE status IN ('succeeded', 'verified')
        """
    ).fetchone()["total"]
    failed = connection.execute(
        """
        SELECT COUNT(*) AS total FROM metadata_backup_runs
        WHERE status='failed'
        """
    ).fetchone()["total"]
    if latest is None:
        return {
            "last_status": "never",
            "last_run": None,
            "succeeded_count": 0,
            "failed_count": 0,
        }
    run = _row_run(latest)
    return {
        "last_status": run["status"],
        "last_run": run,
        "succeeded_count": int(succeeded),
        "failed_count": int(failed),
    }


def notify_owner_of_backup_failure(
    connection: Any, *, reason: str, error_message: str
) -> None:
    from .notifications import enqueue_notification

    enqueue_notification(
        connection,
        event="metadata_backup_failed",
        title="Metadata backup failed",
        body=f"Reason={reason}. {error_message}",
        channels=("in_app",),
    )


def run_metadata_backup(
    connection: Any | None = None,
    *,
    reason: str,
    backup_dir: str | Path,
    master_key: str | None = None,
    database_dsn: str | None = None,
    config_snapshot: dict[str, Any] | None = None,
    object_store: ObjectStore | None = None,
    retention: int | None = None,
    s3_prefix: str = S3_BACKUP_PREFIX,
) -> dict[str, Any]:
    """Create, optionally upload, rotate, and record one metadata backup."""
    backend = "postgresql"
    keep = retention if retention is not None else settings.metadata_backup_retention
    if config_snapshot is None:
        config_snapshot = build_config_snapshot()
    try:
        artifact = create_metadata_backup(
            reason=reason,
            backup_dir=backup_dir,
            master_key=master_key,
            config_snapshot=config_snapshot,
            database_dsn=database_dsn,
        )
        s3_key = None
        if object_store is not None:
            stored = store_backup_to_object_store(
                artifact, object_store=object_store, prefix=s3_prefix
            )
            s3_key = stored["key"]
        rotate_local_retention(backup_dir, keep=keep)
        result = {
            "ok": True,
            "reason": reason,
            "path": str(artifact["path"]),
            "digest_sha256": artifact["digest_sha256"],
            "database_sha256": artifact["database_sha256"],
            "backend": backend,
            "s3_key": s3_key,
            "size_bytes": artifact["size_bytes"],
            "filename": artifact["filename"],
            "created_at": artifact["created_at"],
        }
        if connection is not None:
            record_backup_run(
                connection,
                reason=reason,
                backend=backend,
                status="succeeded",
                digest_sha256=artifact["digest_sha256"],
                database_sha256=artifact["database_sha256"],
                local_path=str(artifact["path"]),
                s3_key=s3_key,
                size_bytes=artifact["size_bytes"],
            )
        return result
    except Exception as exc:
        # Do not persist or notify arbitrary provider/process exception text:
        # those boundaries can include deployment identifiers or credentials.
        # Typed backup errors carry the deliberately safe operator message.
        if isinstance(
            exc,
            (
                BackupConfigurationError,
                ObjectStoreUploadError,
                ObjectStoreVerificationError,
            ),
        ):
            message = str(exc)
        else:
            message = "Metadata backup operation failed"
        if connection is not None:
            try:
                record_backup_run(
                    connection,
                    reason=reason,
                    backend=backend,
                    status="failed",
                    error_message=message,
                )
                notify_owner_of_backup_failure(
                    connection, reason=reason, error_message=message
                )
            except Exception:
                # Status/notify failures must not hide the original backup error.
                pass
        if isinstance(exc, BackupError):
            raise
        raise BackupError(message) from None


def open_backup_artifact(
    connection: Any, run_id: int, *, backup_dir: str | Path | None = None
) -> dict[str, Any]:
    """Resolve a recorded backup run to local file bytes for download."""
    row = connection.execute(
        "SELECT * FROM metadata_backup_runs WHERE id=%s",
        (run_id,),
    ).fetchone()
    if row is None:
        raise BackupError("Backup run not found")
    if row["status"] not in {"succeeded", "verified"}:
        raise BackupError("Backup artifact is not available for download")
    candidates: list[Path] = []
    if row.get("local_path"):
        candidates.append(Path(row["local_path"]))
    directory = Path(backup_dir or settings.metadata_backup_dir)
    if row.get("digest_sha256"):
        for path in directory.glob("*.bak.enc"):
            if hashlib.sha256(path.read_bytes()).hexdigest() == row["digest_sha256"]:
                candidates.append(path)
                break
    for path in candidates:
        if path.exists():
            body = path.read_bytes()
            digest = hashlib.sha256(body).hexdigest()
            if row.get("digest_sha256") and digest != row["digest_sha256"]:
                raise BackupError("Backup artifact digest mismatch")
            return {
                "filename": path.name,
                "body": body,
                "digest_sha256": digest,
                "run": _row_run(row),
            }
    raise BackupError("Backup artifact file is missing from the local backup volume")
