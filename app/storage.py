from __future__ import annotations

import asyncio
import configparser
import hashlib
import json
import os
import re
import stat
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import boto3
from boto3.s3.transfer import TransferConfig
from watchfiles import Change, awatch

from .config import Settings, is_placeholder, settings
from .catalog import ArchiveCatalog
from .database import db
from .services import source_layout
from .services import vault_relocation
from .i18n import DEFAULT_LOCALE, format_message_params, translate
from .system_settings import effective_settings
from .services.rclone_runtime import (
    decode_object_relative_path,
    encode_object_relative_path,
    vault_rclone_config,
)
from .services.vault_crypto import safe_error_message
from .services.vault_recovery import secrets_for_vault
from .services.s3_preflight import check_bucket_readiness, preflight_failure_message
from .services.catalog_audit import audit_vault_catalog
from .services.lifecycle_policies import (
    load_policy_assignments,
    resolve_effective_policy_id,
    sync_lifecycle_rules_for_bucket,
)
from .services.policy_reconciliation import reconcile_pending_policy_tags
from .services.s3_object_tags import apply_version_policy_tag, read_version_policy_tag
from .services import health as health_service
from .services import metadata_backups as metadata_backup_service
from .services import metrics as metrics_service


_runtime_snapshot = None
_runtime_snapshot_source = None


def _runtime_settings(connection=None):
    global _runtime_snapshot, _runtime_snapshot_source
    if not isinstance(settings, Settings):
        return effective_settings(None, settings_obj=settings)
    if connection is not None:
        _runtime_snapshot = effective_settings(connection, settings_obj=settings)
        _runtime_snapshot_source = settings
        return _runtime_snapshot
    if _runtime_snapshot is not None and _runtime_snapshot_source is settings:
        return _runtime_snapshot
    return effective_settings(None, settings_obj=settings)
from .services import worker_errors as worker_error_store
from .services import notifications as notification_service
from .services import cloud_deletion as cloud_deletion_service
from .services import audit_events as audit_event_store
from .services.restore_estimates import (
    estimate_restore,
    is_high_impact_restore,
)
from .services.job_scheduler import select_fair_jobs
from .services.operation_policies import (
    effective_bandwidth_kibps,
    get_policy,
    policy_allows_transfer_now,
    queue_auto_local_cleanups,
    queue_auto_uploads,
    rclone_bwlimit_arg,
)


runtime_status: dict[int, dict[str, Any]] = {}
scan_locks: dict[int, threading.Lock] = {}
status_lock = threading.Lock()
operation_process_lock = threading.Lock()


def scan_lock_for_vault(vault_id: int) -> threading.Lock:
    """Return the process-wide scan/relocation lock for one Vault."""
    with status_lock:
        return scan_locks.setdefault(int(vault_id), threading.Lock())
active_operation_processes: dict[int, subprocess.Popen[str]] = {}
cancelled_jobs: set[int] = set()

RESTORE_TEMPORARY_RE = re.compile(
    r"\..+\.restore-[0-9a-f]{32}\.tmp(?:\..+\.partial)?"
)
CLEANUP_TEMPORARY_RE = re.compile(r"\..+\.cleanup-[0-9a-f]{32}\.tmp")
VERIFY_TEMPORARY_RE = re.compile(r"\..+\.verify-[0-9a-f]{32}\.tmp")

UPLOAD_RETRY_BASE_SECONDS = 2
UPLOAD_RETRY_CAP_SECONDS = 300
UPLOAD_RETRY_MAX_ATTEMPTS = 8

_PERMANENT_UPLOAD_FAILURE_MARKERS = (
    "digest does not match",
    "did not create the verification copy",
    "accessdenied",
    "invalidaccesskeyid",
    "rclone configuration not found",
    "without an s3 versionid",
    "bucket versioning is required",
    "not authorized",
    "access denied",
    "forbidden",
    "signaturedoesnotmatch",
)
_TRANSIENT_UPLOAD_FAILURE_MARKERS = (
    "slowdown",
    "service unavailable",
    "requesttimeout",
    "connection reset",
    "temporary failure",
    "timeout",
    "throttl",
    "503",
    "500",
    "internal error",
    "econnreset",
    "unavailable",
)


def classify_upload_failure(message: str) -> str:
    """Classify an upload/verify error for retry policy.

    Returns ``source_changed`` when the Local Copy mutated during transfer so
    the Job can be rescheduled after the Vault stability window, ``transient``
    for retryable transport faults, or ``permanent`` otherwise.
    """
    lowered = (message or "").lower()
    if "changed since fingerprinting" in lowered:
        return "source_changed"
    if any(marker in lowered for marker in _PERMANENT_UPLOAD_FAILURE_MARKERS):
        return "permanent"
    if any(marker in lowered for marker in _TRANSIENT_UPLOAD_FAILURE_MARKERS):
        return "transient"
    return "permanent"


def upload_retry_delay_seconds(attempt: int) -> int:
    """Exponential backoff delay for the next upload retry attempt."""
    if attempt < 1:
        attempt = 1
    delay = UPLOAD_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
    return min(delay, UPLOAD_RETRY_CAP_SECONDS)


class OperationCancelled(RuntimeError):
    """Raised when a queued or active operation is deliberately interrupted."""


def cancel_jobs(job_ids: list[int]) -> None:
    """Mark jobs for cancellation and terminate their active Rclone processes."""
    processes: list[subprocess.Popen[str]] = []
    with operation_process_lock:
        cancelled_jobs.update(job_ids)
        processes = [
            active_operation_processes[job_id]
            for job_id in job_ids
            if job_id in active_operation_processes
        ]
    for process in processes:
        if process.poll() is None:
            process.terminate()


def job_cancelled(job_id: int) -> bool:
    with operation_process_lock:
        return job_id in cancelled_jobs


def ensure_job_active(job_id: int, message: str) -> None:
    if job_cancelled(job_id):
        raise OperationCancelled(message)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def job_bwlimit(job: dict[str, Any] | None) -> str | None:
    """Return the rclone --bwlimit value attached during fair scheduling."""
    if not job:
        return None
    value = job.get("bwlimit")
    return str(value) if value else None


def job_bandwidth_bytes_per_sec(job: dict[str, Any] | None) -> int | None:
    """Parse the job bwlimit (rclone ``Nk`` form) into bytes/sec for boto3."""
    raw = job_bwlimit(job)
    if not raw:
        return None
    text = raw.strip().lower()
    if text.endswith("k"):
        text = text[:-1]
    try:
        kibps = int(text)
    except ValueError:
        return None
    if kibps <= 0:
        return None
    return kibps * 1024


def rclone_download_perf_args() -> list[str]:
    """Rclone flags that parallelize single-file downloads above the cutoff."""
    runtime = _runtime_settings()
    streams = int(runtime.rclone_multi_thread_streams or 8)
    cutoff = int(runtime.rclone_multi_thread_cutoff_mib or 64)
    return [
        f"--multi-thread-streams={max(1, streams)}",
        f"--multi-thread-cutoff={max(1, cutoff)}M",
    ]


def s3_download_transfer_config(job: dict[str, Any] | None = None) -> TransferConfig:
    """TransferManager config for plain-vault Archive Version downloads."""
    runtime = _runtime_settings()
    threshold_mib = int(runtime.s3_download_multipart_threshold_mib or 8)
    chunk_mib = int(runtime.s3_download_multipart_chunksize_mib or 8)
    concurrency = int(runtime.s3_download_max_concurrency or 10)
    return TransferConfig(
        multipart_threshold=max(5, threshold_mib) * 1024 * 1024,
        multipart_chunksize=max(5, chunk_mib) * 1024 * 1024,
        max_concurrency=max(1, min(32, concurrency)),
        max_bandwidth=job_bandwidth_bytes_per_sec(job),
        use_threads=True,
    )


class _ThrottledByteProgress:
    """Batch transferred-byte updates so progress does not thrash the database."""

    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        self.transferred = 0
        self._lock = threading.Lock()
        self._last_report = 0.0
        interval_ms = int(_runtime_settings().job_progress_min_interval_ms or 500)
        self._min_interval = max(0.05, interval_ms / 1000.0)

    def add(self, amount: int) -> None:
        if amount <= 0:
            return
        should_report = False
        with self._lock:
            self.transferred += amount
            now = time.monotonic()
            if now - self._last_report >= self._min_interval:
                self._last_report = now
                should_report = True
                value = self.transferred
        if should_report:
            set_job_progress(self.job_id, value)

    def flush(self) -> None:
        with self._lock:
            value = self.transferred
            self._last_report = time.monotonic()
        set_job_progress(self.job_id, value)


def safe_relative_path(value: str) -> PurePosixPath:
    candidate = PurePosixPath(value.replace("\\", "/"))
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise ValueError("Invalid path")
    return candidate


def safe_local_path(root_value: str, logical_path: str) -> Path:
    """Resolve a vault-relative path without following a final symbolic link."""
    root = Path(root_value).resolve()
    relative = safe_relative_path(logical_path)
    candidate = root.joinpath(*relative.parts)
    parent = candidate.parent.resolve()
    if parent != root and root not in parent.parents:
        raise ValueError("Path is outside the allowed folder")
    if candidate.is_symlink():
        raise ValueError("Symbolic links are not allowed")
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("Path is outside the allowed folder")
    return resolved


def safe_local_entry_path(root_value: str, logical_path: str) -> Path:
    """Return an on-disk entry without following a final symlink."""
    root = Path(root_value).resolve()
    relative = safe_relative_path(logical_path)
    candidate = root.joinpath(*relative.parts)
    parent = candidate.parent.resolve()
    if parent != root and root not in parent.parents:
        raise ValueError("Path is outside the allowed folder")
    if candidate.is_symlink():
        raise ValueError("Symbolic links are not allowed")
    return candidate


def s3_client():
    access_key = os.getenv("AWS_ACCESS_KEY_ID", "")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    if is_placeholder(
        access_key,
        "REPLACE_ME",
        "REPLACE-WITH-ACCESS-KEY",
    ) or is_placeholder(
        secret_key,
        "REPLACE_ME",
        "REPLACE-WITH-SECRET-KEY",
    ):
        raise RuntimeError("AWS credentials are not configured in the .env file")
    return boto3.client("s3", region_name=settings.aws_region)


def validate_cloud_vault(vault: dict[str, Any]) -> None:
    bucket = vault.get("s3_bucket")
    if not bucket or is_placeholder(
        bucket,
        "BUCKET-NAME",
        "REPLACE-WITH-BUCKET-NAME",
    ):
        raise RuntimeError("The S3 bucket name is not configured")
    result = check_bucket_readiness(
        bucket,
        region=settings.aws_region,
        client=s3_client(),
    )
    if not result.ok:
        raise RuntimeError(
            "S3 bucket preflight failed: " + preflight_failure_message(result)
        )


def rclone_remote_is_crypt(remote_name: str) -> bool:
    """Return whether a configured Rclone remote encrypts file contents."""
    config_path = Path(settings.rclone_config)
    if not config_path.is_file():
        raise RuntimeError(f"Rclone configuration not found: {config_path}")
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(config_path, encoding="utf-8")
    except configparser.Error as exc:
        raise RuntimeError(f"Invalid Rclone configuration: {exc}") from exc
    section = remote_name.strip().rstrip(":")
    if not section or not parser.has_section(section):
        raise RuntimeError(f"Rclone remote is not configured: {remote_name}")
    remote_type = parser.get(section, "type", fallback="").strip().lower()
    if not remote_type:
        raise RuntimeError(f"Rclone remote type is not configured: {remote_name}")
    return remote_type == "crypt"


def vault_encrypts_content(vault: dict[str, Any]) -> bool:
    """Whether uploads for ``vault`` go through content encryption."""
    mode = vault.get("encryption_mode")
    if mode == "crypt":
        return True
    if mode == "plain":
        return False
    return rclone_remote_is_crypt(vault["rclone_remote"])


def vault_encrypts_names(vault: dict[str, Any]) -> bool:
    """Per-vault crypt remotes always encrypt file and directory names."""
    return vault.get("encryption_mode") == "crypt"


def object_key_to_path(
    key: str,
    prefix_value: str,
    is_crypt: bool,
    *,
    encrypted_names: bool = False,
    runtime: Any | None = None,
) -> str | None:
    prefix = f"{prefix_value.strip('/')}/" if prefix_value.strip("/") else ""
    if prefix and not key.startswith(prefix):
        return None
    relative = key[len(prefix):]
    if encrypted_names:
        if not relative or relative.endswith("/") or runtime is None:
            return None
        try:
            relative = decode_object_relative_path(runtime, relative)
        except RuntimeError:
            return None
    elif is_crypt:
        if not relative.endswith(".bin"):
            return None
        relative = relative[:-4]
    if not relative or relative.endswith("/"):
        return None
    try:
        return safe_relative_path(relative).as_posix()
    except ValueError:
        return None


def expected_cloud_key(
    logical_path: str,
    prefix_value: str,
    is_crypt: bool,
    *,
    encrypted_names: bool = False,
    runtime: Any | None = None,
) -> str:
    relative = safe_relative_path(logical_path).as_posix()
    if encrypted_names:
        if runtime is None:
            raise RuntimeError("Filename-encrypted keys require a runtime Rclone config")
        relative = encode_object_relative_path(runtime, relative)
    elif is_crypt:
        relative += ".bin"
    prefix = prefix_value.strip("/")
    return f"{prefix}/{relative}" if prefix else relative


def plain_rclone_destination(
    remote_name: str,
    s3_prefix: str,
    logical_path: str,
) -> str:
    """Build a bucket-rooted plain Rclone object spec including the vault prefix."""
    remote = remote_name.strip().rstrip(":")
    if not remote:
        raise ValueError("Rclone remote name is required")
    key = expected_cloud_key(logical_path, s3_prefix, is_crypt=False)
    return f"{remote}:{key}"


def configured_rclone_destination(job: dict[str, Any], logical_path: str) -> str:
    """Object spec for a preconfigured Rclone remote (not a runtime crypt remote)."""
    if vault_encrypts_content(job):
        return f"{job['rclone_remote']}:{logical_path}"
    return plain_rclone_destination(
        job["rclone_remote"], job["s3_prefix"], logical_path
    )


def is_restore_temporary_name(name: str) -> bool:
    return (
        RESTORE_TEMPORARY_RE.fullmatch(name) is not None
        or CLEANUP_TEMPORARY_RE.fullmatch(name) is not None
        or VERIFY_TEMPORARY_RE.fullmatch(name) is not None
    )


def _record_scan_finding(
    vault_id: int, *, path: str, code: str, message: str
) -> None:
    with status_lock:
        status = runtime_status.setdefault(
            vault_id,
            {"scanning": False, "last_scan": None, "last_error": None},
        )
        filesystem = status.setdefault(
            "filesystem",
            {"ok": False, "uid": None, "gid": None, "checks": [], "findings": []},
        )
        findings = list(filesystem.get("findings") or [])
        findings.append({"path": path, "code": code, "message": message})
        filesystem["findings"] = findings
        filesystem["ok"] = False


def _scan_tree(
    connection: Any,
    vault: dict[str, Any],
    scan_id: str,
    *,
    root: Path,
    allow_chunk_commits: bool,
) -> tuple[int, str | None]:
    """Catalog one root, leaving transaction ownership to the caller."""
    vault_id = int(vault["id"])
    count = 0
    catalog = ArchiveCatalog(connection)
    for entry in root.rglob("*"):
        try:
            if entry.is_symlink():
                file_type = "symlink"
                entry_stat = entry.lstat()
            elif entry.is_file():
                file_type = "regular"
                entry_stat = entry.stat()
            elif entry.exists() and not entry.is_dir():
                file_type = "other"
                entry_stat = entry.lstat()
            else:
                continue
        except OSError as exc:
            try:
                relative = entry.relative_to(root).as_posix()
            except ValueError:
                relative = entry.name
            _record_scan_finding(
                vault_id,
                path=relative,
                code="fs.unreadable_file",
                message=f"File is unreadable: {relative} ({exc})",
            )
            continue
        if is_restore_temporary_name(entry.name):
            continue
        relative = entry.relative_to(root).as_posix()
        catalog.observe_local_copy(
            vault_id=vault_id,
            path=relative,
            file_type=file_type,
            size=entry_stat.st_size,
            mtime_ns=entry_stat.st_mtime_ns,
            seen_at=scan_id,
            observed_at=now_iso(),
        )
        count += 1
        if allow_chunk_commits and count % 1000 == 0:
            connection.commit()
    access = source_layout.vault_local_access(vault["source_root"])
    alias = access.volume_alias
    safe_scan_result = access.local_operations_allowed or access.volume_health == "scan_required"
    if safe_scan_result and (
        alias is None or source_layout.should_emit_local_copy_removals(alias)
    ):
        catalog.mark_unseen_local_copies_missing(
            vault_id=vault_id,
            seen_at=scan_id,
            observed_at=now_iso(),
        )
    completed_alias = (
        alias
        if safe_scan_result and alias and source_layout.requires_full_local_scan(alias)
        else None
    )
    return count, completed_alias


def scan_tree(
    vault: dict[str, Any],
    scan_id: str,
    *,
    _connection: Any | None = None,
    _root: Path | None = None,
) -> int:
    access = source_layout.vault_local_access(vault["source_root"])
    if not access.local_operations_allowed and access.volume_health != "scan_required":
        raise RuntimeError(
            f"Local scan blocked by Source Volume health: {access.volume_health}"
        )
    root = _root or Path(vault["source_root"]).resolve()
    if not root.exists():
        raise RuntimeError(f"Folder is not available in the container: {root}")
    if _connection is not None:
        count, _completed_alias = _scan_tree(
            _connection, vault, scan_id, root=root, allow_chunk_commits=False
        )
        return count
    with db() as connection:
        count, completed_alias = _scan_tree(
            connection, vault, scan_id, root=root, allow_chunk_commits=True
        )
    if completed_alias:
        source_layout.note_full_local_scan_completed(completed_alias)
    return count


def apply_filesystem_changes(
    vault: dict[str, Any], changes: set[tuple[Change, str]]
) -> int:
    """Serialize watcher updates with full scans for the same vault."""
    vault_id = int(vault["id"])
    with status_lock:
        lock = scan_locks.setdefault(vault_id, threading.Lock())
    with lock:
        return _apply_filesystem_changes(vault, changes)


def _apply_filesystem_changes(
    vault: dict[str, Any], changes: set[tuple[Change, str]]
) -> int:
    """Apply filesystem watcher events directly to the local catalog."""
    if vault_relocation.local_work_suspended(vault):
        raise RuntimeError("Source watcher suspended for Vault relocation")
    with db() as connection:
        state = connection.execute(
            "SELECT relocation_state FROM vaults WHERE id=%s", (vault["id"],)
        ).fetchone()
    if state and state["relocation_state"] != "ready":
        raise RuntimeError("Source watcher suspended for Vault relocation")
    access = source_layout.vault_local_access(vault["source_root"])
    if not access.local_operations_allowed:
        raise RuntimeError(
            f"Source watcher blocked by Source Volume health: {access.volume_health}"
        )
    root = Path(vault["source_root"]).resolve()
    if not root.is_dir():
        raise RuntimeError(f"Folder is not available in the container: {root}")
    changed = 0
    event_id = now_iso()
    with db() as connection:
        catalog = ArchiveCatalog(connection)
        for _, path_value in sorted(changes, key=lambda item: item[1]):
            entry = Path(path_value)
            try:
                relative = entry.relative_to(root).as_posix()
            except ValueError:
                continue
            if not relative or is_restore_temporary_name(entry.name):
                continue

            try:
                if entry.is_symlink():
                    file_type = "symlink"
                elif entry.is_file():
                    file_type = "regular"
                elif entry.exists() and not entry.is_dir():
                    file_type = "other"
                else:
                    file_type = None
            except OSError as exc:
                _record_scan_finding(
                    int(vault["id"]),
                    path=relative,
                    code="fs.unreadable_file",
                    message=f"File is unreadable: {relative} ({exc})",
                )
                continue
            if file_type is not None:
                try:
                    entry_stat = entry.stat() if file_type == "regular" else entry.lstat()
                except OSError as exc:
                    _record_scan_finding(
                        int(vault["id"]),
                        path=relative,
                        code="fs.unreadable_file",
                        message=f"File is unreadable: {relative} ({exc})",
                    )
                    continue
                catalog.observe_local_copy(
                    vault_id=vault["id"],
                    path=relative,
                    file_type=file_type,
                    size=entry_stat.st_size,
                    mtime_ns=entry_stat.st_mtime_ns,
                    seen_at=event_id,
                    observed_at=now_iso(),
                )
                changed += 1
                continue

            if entry.exists():
                # Directory metadata changes do not alter catalogued files.
                continue
            access = source_layout.vault_local_access(vault["source_root"])
            alias = access.volume_alias
            if access.local_operations_allowed and (
                alias is None or source_layout.should_emit_local_copy_removals(alias)
            ):
                catalog.mark_local_path_missing(
                    vault_id=vault["id"],
                    path=relative,
                    observed_at=now_iso(),
                )
            changed += 1
    return changed


def scan_cloud(vault: dict[str, Any], scan_id: str) -> int:
    validate_cloud_vault(vault)
    encrypts_names = vault_encrypts_names(vault)
    is_crypt = vault_encrypts_content(vault)
    client = s3_client()
    paginator = client.get_paginator("list_object_versions")
    prefix = vault["s3_prefix"].strip("/")
    kwargs: dict[str, Any] = {"Bucket": vault["s3_bucket"]}
    if prefix:
        kwargs["Prefix"] = f"{prefix}/"
    count = 0
    versions: list[tuple[str, str, dict[str, Any]]] = []
    markers: list[tuple[str, str, dict[str, Any]]] = []

    def _collect(runtime: Any | None = None) -> None:
        for page in paginator.paginate(**kwargs):
            for item in page.get("Versions", []):
                logical_path = object_key_to_path(
                    item["Key"],
                    prefix,
                    is_crypt,
                    encrypted_names=encrypts_names,
                    runtime=runtime,
                )
                if logical_path is not None:
                    versions.append((logical_path, item["VersionId"], item))
            for item in page.get("DeleteMarkers", []):
                logical_path = object_key_to_path(
                    item["Key"],
                    prefix,
                    is_crypt,
                    encrypted_names=encrypts_names,
                    runtime=runtime,
                )
                if logical_path is not None:
                    markers.append((logical_path, item["VersionId"], item))

    if encrypts_names:
        with vault_rclone_config(vault) as runtime:
            _collect(runtime)
    else:
        _collect()

    def remote_timestamp(item: dict[str, Any]) -> str:
        value = item.get("LastModified")
        return value.isoformat() if hasattr(value, "isoformat") else str(value or scan_id)

    versions.sort(
        key=lambda entry: (
            entry[0],
            remote_timestamp(entry[2]),
            entry[2]["Key"],
            entry[1],
        )
    )
    markers.sort(
        key=lambda entry: (
            entry[0],
            remote_timestamp(entry[2]),
            entry[2]["Key"],
            entry[1],
        )
    )
    with db() as connection:
        catalog = ArchiveCatalog(connection)
        assignments = load_policy_assignments(connection, vault["id"])
        from .services.lifecycle_pins import is_path_pinned

        for logical_path, version_id, item in versions:
            if is_path_pinned(connection, vault["id"], logical_path):
                desired_policy_id = None
            else:
                desired_policy_id = resolve_effective_policy_id(
                    logical_path, assignments
                )
            applied_policy_id = None
            try:
                applied_policy_id = read_version_policy_tag(
                    client,
                    bucket=vault["s3_bucket"],
                    key=item["Key"],
                    version_id=version_id,
                )
            except Exception:
                applied_policy_id = None
            catalog.record_archive_version(
                vault_id=vault["id"],
                path=logical_path,
                object_key=item["Key"],
                provider_version_id=version_id,
                size=item.get("Size"),
                storage_class=item.get("StorageClass", "STANDARD"),
                etag=item.get("ETag", "").strip('"'),
                uploaded_at=remote_timestamp(item),
                observed_at=now_iso(),
                scan_id=scan_id,
                desired_policy_id=desired_policy_id,
                applied_policy_id=applied_policy_id,
            )
            count += 1
        for logical_path, version_id, item in markers:
            catalog.record_delete_marker(
                vault_id=vault["id"],
                path=logical_path,
                object_key=item["Key"],
                provider_version_id=version_id,
                created_at=remote_timestamp(item),
                observed_at=now_iso(),
            )
            count += 1
        catalog.mark_unseen_archive_versions_missing(
            vault_id=vault["id"],
            scan_id=scan_id,
            scan_started_at=scan_id,
        )
    return count


def _current_scan_vault(vault: dict[str, Any]) -> dict[str, Any]:
    """Reload a Vault immediately before local work starts.

    Scheduled scans receive rows in a batch, so the row supplied by a caller
    can be stale after an administrator relocates the Vault. The database row
    is authoritative once the per-Vault scan lock has been acquired. The
    fallback keeps lightweight storage unit-test seams, which provide no real
    Vault row, working as before.
    """
    with db() as connection:
        current = connection.execute(
            "SELECT * FROM vaults WHERE id=%s", (int(vault["id"]),)
        ).fetchone()
    if isinstance(current, dict) and "source_root" in current:
        return current
    return vault


def _validate_enrolled_scan_root(vault: dict[str, Any]) -> None:
    """Reject a scan unless an enrolled root still has its original identity."""
    # A few direct scan callers intentionally use a minimal in-memory Vault
    # object rather than a persisted row. Real rows always have these columns.
    if "root_identity" not in vault and "root_identity_version" not in vault:
        return
    if (
        vault.get("root_identity_version") != vault_relocation.ROOT_IDENTITY_VERSION
        or not vault.get("root_identity")
    ):
        raise RuntimeError("Vault root identity is ambiguous; local scan blocked")
    try:
        observed = vault_relocation.root_identity(vault["source_root"])
    except vault_relocation.VaultRelocationError as exc:
        raise RuntimeError(f"Vault root identity is unavailable: {exc}") from exc
    if observed != vault["root_identity"]:
        raise RuntimeError("Vault root identity mismatch; local scan blocked")


class _ScanRootMismatch(RuntimeError):
    pass


@contextmanager
def _pinned_verified_scan_root(vault: dict[str, Any]):
    """Pin the enrolled directory so pathname replacement cannot redirect I/O.

    Linux has no atomic "check every ancestor and keep using this pathname"
    primitive. We therefore reject symlink components, open the directory with
    ``O_NOFOLLOW``, verify the opened inode, and perform scan reads through its
    ``/proc/self/fd`` handle. If procfs is unavailable, scanning fails closed.
    """
    try:
        _validate_enrolled_scan_root(vault)
    except Exception as exc:
        raise _ScanRootMismatch(str(exc)) from exc
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        fd = os.open(str(vault["source_root"]), flags)
    except OSError as exc:
        raise _ScanRootMismatch(f"Vault root could not be pinned: {exc}") from exc
    try:
        if vault_relocation.opened_root_identity(fd) != vault.get("root_identity"):
            raise _ScanRootMismatch("Vault root changed while it was being pinned")
        pinned = Path(f"/proc/self/fd/{fd}")
        try:
            pinned_stat = pinned.stat()
        except OSError as exc:
            raise _ScanRootMismatch("Pinned Vault root is unavailable through procfs") from exc
        opened_stat = os.fstat(fd)
        if (pinned_stat.st_dev, pinned_stat.st_ino) != (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ):
            raise _ScanRootMismatch("Pinned Vault root identity is inconsistent")
        yield pinned
    finally:
        os.close(fd)


def _current_verified_scan_row(connection: Any, vault: dict[str, Any]) -> dict[str, Any]:
    current = connection.execute(
        """
        SELECT source_root, relocation_state, root_identity_version, root_identity
        FROM vaults WHERE id=%s
        """,
        (int(vault["id"]),),
    ).fetchone()
    if not isinstance(current, dict) or current.get("source_root") != vault.get("source_root"):
        raise _ScanRootMismatch("Vault root changed during local scan")
    try:
        _validate_enrolled_scan_root(current)
    except Exception as exc:
        raise _ScanRootMismatch(str(exc)) from exc
    return current


def _verified_local_scan(
    vault: dict[str, Any], scan_id: str
) -> tuple[int, dict[str, int], str | None]:
    """Commit catalog, rename/jobs, and recovery state only after final identity proof."""
    # Minimal in-memory Vaults are a longstanding unit-test seam. Persisted
    # rows always contain identity columns and always use the atomic path.
    if "root_identity" not in vault and "root_identity_version" not in vault:
        return scan_tree(vault, scan_id), apply_auto_renames(vault), None

    access = source_layout.vault_local_access(vault["source_root"])
    if not access.local_operations_allowed and access.volume_health != "scan_required":
        raise RuntimeError(
            f"Local scan blocked by Source Volume health: {access.volume_health}"
        )
    with _pinned_verified_scan_root(vault) as pinned_root:
        with db() as connection:
            _current_verified_scan_row(connection, vault)
            count = scan_tree(
                vault,
                scan_id,
                _connection=connection,
                _root=pinned_root,
            )
            alias = access.volume_alias
            completed_alias = (
                alias
                if alias and source_layout.requires_full_local_scan(alias)
                else None
            )
            _current_verified_scan_row(connection, vault)
            rename_summary = apply_auto_renames(
                vault, _connection=connection, _root=pinned_root
            )
            current = _current_verified_scan_row(connection, vault)
            if current.get("relocation_state") == "scan_required":
                vault_relocation.complete_relocation_scan(
                    connection, int(vault["id"]), release_runtime=False
                )
        # Runtime gates are delayed until the DB transaction committed.
        if current.get("relocation_state") == "scan_required":
            vault_relocation.release_relocation_scan_runtime(int(vault["id"]))
        if completed_alias:
            source_layout.note_full_local_scan_completed(completed_alias)
        return count, rename_summary, completed_alias


def scan_vault(vault: dict[str, Any]) -> dict[str, int]:
    vault_id = int(vault["id"])
    lock = scan_lock_for_vault(vault_id)
    if not lock.acquire(blocking=False):
        return {"already_running": 1}

    with status_lock:
        status = runtime_status.setdefault(
            vault_id, {"scanning": False, "last_scan": None, "last_error": None}
        )
    try:
        # The relocation route holds this same lock while it publishes the
        # recovery state. A direct caller can still observe the short runtime
        # handoff gate, so do not turn that into a scan while the DB row is
        # still ready. A persisted scan_required row is intentionally allowed:
        # that scan is the recovery operation which must clear the suspension.
        vault = _current_scan_vault(vault)
        if (
            str(vault.get("relocation_state") or "ready") == "ready"
            and vault_relocation.local_work_suspended(vault)
        ):
            return {"suspended": 1}
        try:
            _validate_enrolled_scan_root(vault)
        except Exception as exc:
            with status_lock:
                status["last_error"] = f"Source scan blocked: {exc}"
            return {"local": -1, "root_identity_mismatch": 1}

        with status_lock:
            status.update(scanning=True, last_error=None)

        scan_id = now_iso()
        result: dict[str, int] = {}
        local_scan_identity_valid = True
        # Reset scan findings; live preflight still runs via /api/stats.
        with status_lock:
            status["filesystem"] = {
                "ok": True,
                "uid": None,
                "gid": None,
                "checks": [],
                "findings": [],
            }
        access = source_layout.vault_local_access(vault["source_root"])
        if access.local_operations_allowed or access.volume_health == "scan_required":
            try:
                count, rename_summary, _completed_alias = _verified_local_scan(
                    vault, scan_id
                )
                result["local"] = count
                result.update(
                    {f"rename_{key}": value for key, value in rename_summary.items()}
                )
            except _ScanRootMismatch as exc:
                local_scan_identity_valid = False
                status["last_error"] = f"Source scan blocked: {exc}"
                result["local"] = -1
                result["root_identity_mismatch"] = 1
            except Exception as exc:
                status["last_error"] = f"Source scan: {exc}"
                result["local"] = -1
        else:
            result["local_skipped"] = 1
            result["local"] = 0
        if local_scan_identity_valid:
            try:
                result["cloud"] = scan_cloud(vault, scan_id)
            except Exception as exc:
                status["last_error"] = f"Cloud scan: {exc}"
                result["cloud"] = -1
            try:
                validate_cloud_vault(vault)
                with db() as connection:
                    result["policy_tags"] = reconcile_pending_policy_tags(
                        connection,
                        vault,
                        s3_client(),
                    )
                    result["lifecycle_rules"] = sync_lifecycle_rules_for_bucket(
                        connection,
                        s3_client(),
                        bucket=vault["s3_bucket"],
                    )
                    owner = connection.execute(
                        """
                        SELECT user_id FROM vault_members
                        WHERE vault_id=%s AND role='owner'
                        LIMIT 1
                        """,
                        (vault["id"],),
                    ).fetchone()
                    local_access = source_layout.vault_local_access(vault["source_root"])
                    if owner and local_access.local_operations_allowed:
                        result["auto_uploads"] = queue_auto_uploads(
                            connection,
                            vault_id=int(vault["id"]),
                            source_root=str(vault["source_root"]),
                            requested_by=int(owner["user_id"]),
                        )
                        result["auto_local_cleanups"] = queue_auto_local_cleanups(
                            connection,
                            vault_id=int(vault["id"]),
                            requested_by=int(owner["user_id"]),
                            local_delete_enabled=_runtime_settings(
                                connection
                            ).allow_local_delete,
                        )
            except Exception as exc:
                status["last_error"] = f"Policy tag reconciliation: {exc}"
                result["policy_tags"] = -1
                result["lifecycle_rules"] = -1
        status["last_scan"] = now_iso()
        return result
    finally:
        with status_lock:
            status["scanning"] = False
        lock.release()


def _apply_auto_renames(
    connection: Any,
    vault: dict[str, Any],
    *,
    root: Path,
    requested_by: int | None = None,
) -> dict[str, int]:
    """Apply rename analysis inside the caller-owned transaction."""
    from .audit import audit_log

    summary = {"hashed": 0, "confirmed": 0, "queued": 0, "ambiguous": 0}
    confirmed_paths: list[str] = []
    catalog = ArchiveCatalog(connection)
    if requested_by is None:
        owner = connection.execute(
            """
            SELECT user_id FROM vault_members
            WHERE vault_id=%s AND role='owner'
            LIMIT 1
            """,
            (vault["id"],),
        ).fetchone()
        requested_by = int(owner["user_id"]) if owner else None
    present_rows = connection.execute(
            """
            SELECT vf.id AS vault_file_id, fp.path, lc.size, lc.mtime_ns
            FROM vault_files vf
            JOIN file_paths fp
              ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
            JOIN local_copies lc ON lc.vault_file_id=vf.id
            WHERE vf.vault_id=%s
              AND vf.status='active'
              AND lc.presence='present'
              AND lc.file_type='regular'
              AND lc.plaintext_sha256 IS NULL
            ORDER BY lower(fp.path)
            """,
            (vault["id"],),
    ).fetchall()
    for row in present_rows:
            local_path = safe_local_path(str(root), row["path"])
            if not local_path.is_file():
                continue
            try:
                digest, stat_result = hash_stable_regular_file(local_path)
            except (OSError, RuntimeError):
                continue
            catalog.set_local_fingerprint(
                vault_id=vault["id"],
                path=row["path"],
                plaintext_sha256=digest,
                matched_archive_version_id=None,
            )
            summary["hashed"] += 1
            if (
                row["size"] != stat_result.st_size
                or row["mtime_ns"] != stat_result.st_mtime_ns
            ):
                catalog.observe_local_copy(
                    vault_id=vault["id"],
                    path=row["path"],
                    file_type="regular",
                    size=stat_result.st_size,
                    mtime_ns=stat_result.st_mtime_ns,
                    observed_at=now_iso(),
                )
                catalog.set_local_fingerprint(
                    vault_id=vault["id"],
                    path=row["path"],
                    plaintext_sha256=digest,
                    matched_archive_version_id=None,
                )

    candidates = catalog.list_rename_candidates(vault_id=vault["id"])
    changed_at = now_iso()
    for candidate in candidates:
            if candidate["decision"] == "ambiguous":
                summary["ambiguous"] += 1
                continue
            if candidate["decision"] != "auto":
                continue
            catalog.confirm_file_rename(
                vault_file_id=candidate["missing_vault_file_id"],
                new_path=candidate["new_path"],
                changed_at=changed_at,
            )
            audit_log(
                "vault_file_renamed",
                connection=connection,
                vault_id=vault["id"],
                vault_file_id=candidate["missing_vault_file_id"],
                old_path=candidate["missing_path"],
                new_path=candidate["new_path"],
                decision="auto",
            )
            confirmed_paths.append(candidate["new_path"])
            summary["confirmed"] += 1

    for path in confirmed_paths:
        job_ids, _total, eligible = catalog.queue_jobs(
                vault_id=vault["id"],
                path=path,
                action="rename",
                requested_by=requested_by,
                requested_at=now_iso(),
                group_id=uuid.uuid4().hex,
                is_directory=False,
            )
        if job_ids:
            summary["queued"] += len(job_ids)
        elif eligible:
            continue
    return summary


def apply_auto_renames(
    vault: dict[str, Any],
    *,
    requested_by: int | None = None,
    _connection: Any | None = None,
    _root: Path | None = None,
) -> dict[str, int]:
    """Hash and auto-confirm renames as one database transaction."""
    root = _root or Path(vault["source_root"]).resolve()
    if _connection is not None:
        return _apply_auto_renames(
            _connection, vault, root=root, requested_by=requested_by
        )
    with db() as connection:
        return _apply_auto_renames(
            connection, vault, root=root, requested_by=requested_by
        )


def run_rclone(
    *args: str | Callable[[int, int | None], None],
    job_id: int | None = None,
    config_path: str | None = None,
    bwlimit: str | None = None,
) -> None:
    progress_callback = args[-1] if args and callable(args[-1]) else None
    command_args = args[:-1] if progress_callback else args
    command = [
        "rclone",
        "--config",
        config_path or settings.rclone_config,
    ]
    if bwlimit:
        command.extend(["--bwlimit", bwlimit])
    command.extend(command_args)
    if progress_callback is None:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=None)
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "Rclone error").strip()
            raise RuntimeError(message[-1500:])
        return

    command.extend(["--stats=500ms", "--use-json-log", "--stats-log-level=NOTICE"])
    if job_id is not None and job_cancelled(job_id):
        raise OperationCancelled("Operation stopped")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    if job_id is not None:
        with operation_process_lock:
            active_operation_processes[job_id] = process
            should_terminate = job_id in cancelled_jobs
        if should_terminate and process.poll() is None:
            process.terminate()
    output_tail: list[str] = []
    try:
        assert process.stdout is not None
        for line in process.stdout:
            clean_line = line.strip().lstrip("\r")
            if not clean_line:
                continue
            output_tail.append(clean_line)
            output_tail = output_tail[-30:]
            progress = parse_rclone_progress(clean_line)
            if progress:
                progress_callback(*progress)
        return_code = process.wait()
    finally:
        if job_id is not None:
            with operation_process_lock:
                active_operation_processes.pop(job_id, None)
    if job_id is not None and job_cancelled(job_id):
        raise OperationCancelled("Operation stopped")
    if return_code != 0:
        raise RuntimeError("\n".join(output_tail)[-1500:] or "Rclone error")


def parse_rclone_progress(line: str) -> tuple[int, int | None] | None:
    """Extract byte counters from Rclone's structured stats log line."""
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    stats = record.get("stats", record)
    if not isinstance(stats, dict) or "bytes" not in stats:
        return None
    total = stats.get("totalBytes")
    return int(stats.get("bytes") or 0), int(total) if total is not None else None


def set_job(
    job_id: int,
    status: str,
    message: str = "",
    *,
    message_key: str | None = None,
    message_params: dict[str, Any] | None = None,
) -> None:
    params = message_params or {}
    if message_key and not message:
        message = translate(message_key, locale=DEFAULT_LOCALE, **params)
    with db() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET status=%s,
                message=%s,
                message_key=%s,
                message_params=%s,
                updated_at=%s
            WHERE id=%s
            """,
            (
                status,
                message,
                message_key,
                format_message_params(params) if message_key else None,
                now_iso(),
                job_id,
            ),
        )
        if status in {"completed", "failed"}:
            try:
                notification_service.enqueue_job_terminal_push(
                    connection, job_id=job_id
                )
            except Exception:
                # Push enqueue must never fail the Job status transition.
                pass


def schedule_upload_retry(
    job_id: int,
    *,
    message: str = "",
    message_key: str | None = None,
    message_params: dict[str, Any] | None = None,
    retry_count: int,
    delay_seconds: int | None = None,
) -> None:
    delay = (
        int(delay_seconds)
        if delay_seconds is not None
        else upload_retry_delay_seconds(retry_count)
    )
    # Bounded jitter keeps thundering herds apart without changing the base delay.
    jitter = delay * 0.2 * (uuid.uuid4().int % 1000) / 1000.0
    retry_after = (
        datetime.now(timezone.utc) + timedelta(seconds=delay + jitter)
    ).isoformat()
    params = message_params or {}
    if message_key and not message:
        message = translate(message_key, locale=DEFAULT_LOCALE, **params)
    with db() as connection:
        connection.execute(
            """
            UPDATE jobs
            SET status='retrying',
                message=%s,
                message_key=%s,
                message_params=%s,
                retry_count=%s,
                retry_after=%s,
                updated_at=%s
            WHERE id=%s
            """,
            (
                message,
                message_key,
                format_message_params(params) if message_key else None,
                retry_count,
                retry_after,
                now_iso(),
                job_id,
            ),
        )


def set_job_progress(job_id: int, transferred_bytes: int) -> None:
    with db() as connection:
        connection.execute(
            """
            UPDATE jobs SET transferred_bytes=%s, updated_at=%s WHERE id=%s
            """,
            (max(0, transferred_bytes), now_iso(), job_id),
        )


def _remove_abandoned_restore_files(target: Path) -> None:
    """Remove temporary recovery files left behind by a stopped process."""
    prefix = f".{target.name}.restore-"
    try:
        for entry in target.parent.iterdir():
            if entry.name.startswith(prefix):
                try:
                    entry.unlink(missing_ok=True)
                except OSError:
                    # A stale temporary file must not prevent the job from being retried.
                    pass
    except OSError:
        # The worker will create the parent directory when it retries the job.
        pass


def cleanup_abandoned_restore_files() -> int:
    """Remove application-owned recovery residues before workers are started."""
    with db() as connection:
        vaults = connection.execute(
            "SELECT source_root FROM vaults WHERE enabled=TRUE"
        ).fetchall()
    removed = 0
    for vault in vaults:
        access = source_layout.vault_local_access(vault["source_root"])
        if not access.local_operations_allowed:
            continue
        root = Path(vault["source_root"]).resolve()
        if not root.is_dir():
            continue
        try:
            entries = root.rglob("*")
            for entry in entries:
                # Do not delete free-space .cleanup-*.tmp salvage claims here.
                if (
                    RESTORE_TEMPORARY_RE.fullmatch(entry.name) is not None
                    or VERIFY_TEMPORARY_RE.fullmatch(entry.name) is not None
                ):
                    try:
                        entry.unlink(missing_ok=True)
                        removed += 1
                    except OSError:
                        pass
        except OSError:
            continue
    return removed


def reconcile_interrupted_jobs() -> dict[str, int]:
    """Reconcile non-durable operation states left behind by a restart."""
    summary = {"completed": 0, "requeued": 0, "failed": 0}
    with db() as connection:
        jobs = connection.execute(
            """
            SELECT j.*, v.source_root
            FROM jobs j
            JOIN vaults v ON v.id=j.vault_id
            WHERE (j.action='recover' AND j.status IN ('downloading', 'verifying'))
               OR (j.action='upload' AND j.status IN ('uploading', 'verifying'))
               OR (j.action='rename' AND j.status IN ('uploading', 'verifying', 'cleaning'))
               OR (j.action='free-space' AND j.status='cleaning')
            ORDER BY j.requested_at ASC
            """
        ).fetchall()

        for job in jobs:
            access = source_layout.vault_local_access(job["source_root"])
            if not access.local_operations_allowed:
                # Keep interrupted local work suspended without touching the
                # absent, inaccessible, or replaced tree.
                continue
            timestamp = now_iso()
            try:
                if job["action"] == "recover":
                    source_root = Path(job["source_root"])
                    if not source_root.is_dir():
                        raise RuntimeError("Source folder is unavailable")
                    target = safe_local_entry_path(job["source_root"], job["path"])
                    _remove_abandoned_restore_files(target)
                    # Never mark recoveries completed from size alone: digest
                    # verification must run again through process_recover.
                    if target.is_file() and not target.is_symlink():
                        try:
                            target.unlink()
                        except OSError:
                            pass
                    connection.execute(
                        """
                        UPDATE jobs SET status='queued', transferred_bytes=0,
                            message=%s, updated_at=%s
                        WHERE id=%s
                        """,
                        (
                            "Recovery interrupted by restart; digest verification will rerun",
                            timestamp,
                            job["id"],
                        ),
                    )
                    summary["requeued"] += 1
                    continue

                if job["action"] == "free-space":
                    source_root = Path(job["source_root"])
                    if not source_root.is_dir():
                        raise RuntimeError("Source folder is unavailable")
                    target = safe_local_entry_path(job["source_root"], job["path"])
                    if not os.path.lexists(target):
                        # Prefer restoring a surviving free-space claim over completing.
                        claim_restored = False
                        surviving_claims: list[Path] = []
                        for claim in sorted(
                            target.parent.glob(f".{target.name}.cleanup-*.tmp")
                        ):
                            if CLEANUP_TEMPORARY_RE.fullmatch(claim.name) is None:
                                continue
                            surviving_claims.append(claim)
                            if restore_claimed_local_copy(claim, target) and os.path.lexists(
                                target
                            ):
                                claim_restored = True
                                break
                        if claim_restored:
                            connection.execute(
                                """
                                UPDATE jobs SET status='queued', transferred_bytes=0,
                                    message=%s, updated_at=%s
                                WHERE id=%s
                                """,
                                (
                                    "Cleanup claim restored after restart; free-space resumed",
                                    timestamp,
                                    job["id"],
                                ),
                            )
                            summary["requeued"] += 1
                            continue
                        remaining_claims = [
                            claim for claim in surviving_claims if claim.exists()
                        ]
                        if remaining_claims:
                            # Never mark freed while a salvageable claim is still on disk.
                            preserved = remaining_claims[0]
                            connection.execute(
                                """
                                UPDATE jobs SET status='failed', message=%s, updated_at=%s
                                WHERE id=%s
                                """,
                                (
                                    "Cleanup claim could not be restored after restart; "
                                    f"original content was preserved at {preserved}",
                                    timestamp,
                                    job["id"],
                                ),
                            )
                            summary["failed"] += 1
                            continue
                        ArchiveCatalog(connection).mark_local_copy_missing(
                            job["vault_file_id"],
                            observed_at=timestamp,
                        )
                        connection.execute(
                            """
                            UPDATE jobs SET status='completed',
                                transferred_bytes=total_bytes, message=%s, updated_at=%s
                            WHERE id=%s
                            """,
                            (
                                "Local space freed (reconciled after restart)",
                                timestamp,
                                job["id"],
                            ),
                        )
                        summary["completed"] += 1
                        continue

                connection.execute(
                    """
                    UPDATE jobs SET status='queued', transferred_bytes=0,
                        message=%s, updated_at=%s
                    WHERE id=%s
                    """,
                    (
                        "Operation interrupted by restart; automatically resumed",
                        timestamp,
                        job["id"],
                    ),
                )
                summary["requeued"] += 1
            except Exception as exc:
                connection.execute(
                    """
                    UPDATE jobs SET status='failed', message=%s, updated_at=%s
                    WHERE id=%s
                    """,
                    (
                        f"Post-restart reconciliation failed: {exc}",
                        timestamp,
                        job["id"],
                    ),
                )
                summary["failed"] += 1
    return summary


def job_progress_callback(job: dict[str, Any]) -> Callable[[int, int | None], None]:
    total_bytes = int(job.get("total_bytes") or 0)
    interval_ms = int(_runtime_settings().job_progress_min_interval_ms or 500)
    min_interval = max(0.05, interval_ms / 1000.0)
    state = {"last_at": 0.0, "last_value": -1}
    lock = threading.Lock()

    def update(transferred_bytes: int, _: int | None = None) -> None:
        # Rclone reports absolute transferred counters.
        value = min(transferred_bytes, total_bytes) if total_bytes else transferred_bytes
        should_report = False
        with lock:
            if value == state["last_value"]:
                return
            now = time.monotonic()
            complete = bool(total_bytes) and value >= total_bytes
            if complete or now - state["last_at"] >= min_interval:
                state["last_at"] = now
                state["last_value"] = value
                should_report = True
        if should_report:
            set_job_progress(job["id"], value)

    return update


def download_with_rclone(job: dict[str, Any]) -> None:
    ensure_job_active(job["id"], "Recovery stopped")
    target = safe_local_entry_path(job["source_root"], job["path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}.tmp")
    is_crypt = vault_encrypts_content(job)
    if is_crypt:
        set_job(
            job["id"],
            "downloading",
            message_key="job.downloading_decrypting",
        )
    else:
        set_job(job["id"], "downloading", message_key="job.downloading")
    try:
        if vault_encrypts_names(job):
            with vault_rclone_config(job) as runtime:
                run_rclone(
                    "copyto",
                    f"{runtime.remote_name}:{job['path']}",
                    str(temporary),
                    *rclone_download_perf_args(),
                    job_progress_callback(job),
                    job_id=job["id"],
                    config_path=str(runtime.path),
                    bwlimit=job_bwlimit(job),
                )
        else:
            run_rclone(
                "copyto",
                configured_rclone_destination(job, job["path"]),
                str(temporary),
                *rclone_download_perf_args(),
                job_progress_callback(job),
                job_id=job["id"],
                bwlimit=job_bwlimit(job),
            )
        ensure_job_active(job["id"], "Recovery stopped")
        if not temporary.is_file():
            raise RuntimeError("Rclone did not create the recovered file")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    stat = target.stat()
    with db() as connection:
        ArchiveCatalog(connection).observe_local_copy(
            vault_id=job["vault_id"],
            path=job["path"],
            file_type="regular",
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            observed_at=now_iso(),
        )
    set_job_progress(job["id"], int(job.get("total_bytes") or stat.st_size))
    set_job(
        job["id"],
        "completed",
        message_key="job.recovered_to",
        message_params={"target": str(target)},
    )


def hash_stable_regular_file(path: Path) -> tuple[str, os.stat_result]:
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError("Local file is not a regular file")
    expected = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) != expected:
            raise RuntimeError("Local file changed since fingerprinting")
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(source.fileno())
    if (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) != expected:
        raise RuntimeError("Local file changed since fingerprinting")
    return digest.hexdigest(), after


def restore_claimed_local_copy(claimed: Path, target: Path) -> bool:
    try:
        claimed.lstat()
    except FileNotFoundError:
        return True
    try:
        os.link(claimed, target, follow_symlinks=False)
    except FileExistsError:
        return False
    except OSError:
        try:
            claimed.lstat()
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Cleanup claim disappeared while restoring local content"
            ) from exc
        return False
    claimed.unlink()
    return True


def remove_local_copies(vault: dict[str, Any], logical_path: str) -> list[Path]:
    """Delete the source copy after the caller has verified the cloud copy."""
    target = safe_local_entry_path(vault["source_root"], logical_path)
    if not target.exists():
        return []
    if not target.is_file():
        raise RuntimeError(f"The local path is not a file: {target}")
    target.unlink()
    return [target]


def _download_plaintext_for_verification(
    job: dict[str, Any],
    *,
    temporary: Path,
) -> None:
    """Read the uploaded object back through Rclone into ``temporary``."""
    if vault_encrypts_names(job):
        with vault_rclone_config(job) as runtime:
            run_rclone(
                "copyto",
                f"{runtime.remote_name}:{job['path']}",
                str(temporary),
                *rclone_download_perf_args(),
                job_progress_callback(job),
                job_id=job["id"],
                config_path=str(runtime.path),
                bwlimit=job_bwlimit(job),
            )
    else:
        run_rclone(
            "copyto",
            configured_rclone_destination(job, job["path"]),
            str(temporary),
            *rclone_download_perf_args(),
            job_progress_callback(job),
            job_id=job["id"],
            bwlimit=job_bwlimit(job),
        )
    ensure_job_active(job["id"], "Upload stopped")
    if not temporary.is_file():
        raise RuntimeError("Rclone did not create the verification copy")


def process_upload(job: dict[str, Any]) -> None:
    ensure_job_active(job["id"], "Upload stopped")
    validate_cloud_vault(job)
    source = safe_local_path(job["source_root"], job["path"])
    if not source.is_file():
        raise RuntimeError("The file is no longer available in the source folder")
    is_crypt = vault_encrypts_content(job)
    encrypts_names = vault_encrypts_names(job)
    secrets = secrets_for_vault(job) if encrypts_names else None
    with db() as connection:
        assignments = load_policy_assignments(connection, job["vault_id"])
    policy_id = resolve_effective_policy_id(job["path"], assignments)
    try:
        set_job(job["id"], "uploading", message_key="job.hashing_local_file")
        plaintext_sha256, source_stat = hash_stable_regular_file(source)
        upload_key = "job.encrypted_upload" if is_crypt else "job.plain_upload"
        set_job(job["id"], "uploading", message_key=upload_key)
        if encrypts_names:
            with vault_rclone_config(job) as runtime:
                run_rclone(
                    "copyto",
                    str(source),
                    f"{runtime.remote_name}:{job['path']}",
                    job_progress_callback(job),
                    job_id=job["id"],
                    config_path=str(runtime.path),
                    bwlimit=job_bwlimit(job),
                )
                ensure_job_active(job["id"], "Upload stopped")
                key = expected_cloud_key(
                    job["path"],
                    job["s3_prefix"],
                    is_crypt,
                    encrypted_names=True,
                    runtime=runtime,
                )
        else:
            run_rclone(
                "copyto",
                str(source),
                configured_rclone_destination(job, job["path"]),
                job_progress_callback(job),
                job_id=job["id"],
                bwlimit=job_bwlimit(job),
            )
            ensure_job_active(job["id"], "Upload stopped")
            key = expected_cloud_key(job["path"], job["s3_prefix"], is_crypt)
        head = s3_client().head_object(Bucket=job["s3_bucket"], Key=key)
        provider_version_id = head.get("VersionId")
        if not provider_version_id:
            raise RuntimeError(
                "Upload stored without an S3 VersionId; bucket Versioning is required"
            )
        applied_policy_id = None
        if policy_id:
            apply_version_policy_tag(
                s3_client(),
                bucket=job["s3_bucket"],
                key=key,
                version_id=provider_version_id,
                policy_id=policy_id,
            )
            applied_policy_id = policy_id
        timestamp = now_iso()
        with db() as connection:
            catalog = ArchiveCatalog(connection)
            version_id = catalog.record_archive_version(
                vault_id=job["vault_id"],
                path=job["path"],
                object_key=key,
                provider_version_id=provider_version_id,
                size=head.get("ContentLength"),
                storage_class=head.get("StorageClass", "STANDARD"),
                etag=head.get("ETag", "").strip('"'),
                uploaded_at=timestamp,
                observed_at=timestamp,
                scan_id=timestamp,
                origin="upload",
                desired_policy_id=policy_id,
                applied_policy_id=applied_policy_id,
            )
            catalog.link_job_version(job["id"], version_id)
        ensure_job_active(job["id"], "Upload stopped")
        after = source.stat(follow_symlinks=False)
        if (
            after.st_size != source_stat.st_size
            or after.st_mtime_ns != source_stat.st_mtime_ns
            or after.st_dev != source_stat.st_dev
            or after.st_ino != source_stat.st_ino
        ):
            raise RuntimeError("Local file changed since fingerprinting")
        set_job(job["id"], "verifying", message_key="job.verifying_cloud_copy")
        temporary = source.with_name(
            f".{source.name}.verify-{uuid.uuid4().hex}.tmp"
        )
        try:
            _download_plaintext_for_verification(job, temporary=temporary)
            remote_digest, _ = hash_stable_regular_file(temporary)
        finally:
            temporary.unlink(missing_ok=True)
        if remote_digest != plaintext_sha256:
            with db() as connection:
                ArchiveCatalog(connection).mark_version_mismatch(
                    version_id,
                    plaintext_sha256=plaintext_sha256,
                    checked_at=now_iso(),
                )
            raise RuntimeError("Cloud copy digest does not match local file")
        verified_at = now_iso()
        with db() as connection:
            catalog = ArchiveCatalog(connection)
            catalog.mark_version_verified(
                version_id,
                plaintext_sha256=plaintext_sha256,
                verified_at=verified_at,
            )
            catalog.set_local_fingerprint(
                vault_id=job["vault_id"],
                path=job["path"],
                plaintext_sha256=plaintext_sha256,
                matched_archive_version_id=version_id,
            )
        set_job_progress(job["id"], int(job.get("total_bytes") or source_stat.st_size))
        set_job(job["id"], "completed", message_key="job.upload_verified")
    except OperationCancelled:
        raise
    except Exception as exc:
        raise RuntimeError(safe_error_message(exc, secrets)) from exc


def ensure_scheduled_current_version(
    *,
    bucket: str,
    object_key: str,
    expected_version_id: str | None,
    operation: str,
) -> None:
    """Abort when the live current VersionId differs from the scheduled Archive Version.

    Unversioned delete_object is still required to create an S3 Delete Marker;
    this Head/compare detects concurrent uploads before that hide (REQ-004).
    """
    if not expected_version_id:
        raise RuntimeError(
            f"{operation} aborted: scheduled Archive Version has no provider VersionId"
        )
    head = s3_client().head_object(Bucket=bucket, Key=object_key)
    current_version = head.get("VersionId")
    if not current_version or current_version != expected_version_id:
        raise RuntimeError(
            f"{operation} aborted: current VersionId "
            f"{current_version!r} does not match scheduled Archive Version "
            f"{expected_version_id!r}"
        )


def process_rename(job: dict[str, Any]) -> None:
    """Copy verified content to the new key, then hide the previous key."""
    ensure_job_active(job["id"], "Rename stopped")
    validate_cloud_vault(job)
    source = safe_local_path(job["source_root"], job["path"])
    if not source.is_file():
        raise RuntimeError("The file is no longer available in the source folder")
    is_crypt = vault_encrypts_content(job)
    encrypts_names = vault_encrypts_names(job)
    secrets = secrets_for_vault(job) if encrypts_names else None
    with db() as connection:
        previous = connection.execute(
            """
            SELECT object_key, provider_version_id, plaintext_sha256, integrity
            FROM archive_versions
            WHERE id=%s
            """,
            (job["archive_version_id"],),
        ).fetchone()
        assignments = load_policy_assignments(connection, job["vault_id"])
    if previous is None:
        raise RuntimeError("Rename target Archive Version is missing")
    if previous["integrity"] != "verified" or not previous["object_key"]:
        raise RuntimeError("Rename requires a verified Archive Version at the old key")
    old_key = previous["object_key"]
    policy_id = resolve_effective_policy_id(job["path"], assignments)
    try:
        if encrypts_names:
            with vault_rclone_config(job) as runtime:
                new_key = expected_cloud_key(
                    job["path"],
                    job["s3_prefix"],
                    is_crypt,
                    encrypted_names=True,
                    runtime=runtime,
                )
        else:
            new_key = expected_cloud_key(job["path"], job["s3_prefix"], is_crypt)
        if new_key == old_key:
            set_job(job["id"], "completed", message_key="job.rename_key_matches")
            return

        with db() as connection:
            existing_new = connection.execute(
                """
                SELECT id, plaintext_sha256
                FROM archive_versions
                WHERE vault_file_id=%s
                  AND object_key=%s
                  AND integrity='verified'
                ORDER BY version_number DESC
                LIMIT 1
                """,
                (job["vault_file_id"], new_key),
            ).fetchone()
            existing_marker = connection.execute(
                """
                SELECT id FROM delete_markers
                WHERE vault_file_id=%s AND object_key=%s
                LIMIT 1
                """,
                (job["vault_file_id"], old_key),
            ).fetchone()

        if existing_marker is not None and existing_new is not None:
            catalog_link = existing_new["id"]
            with db() as connection:
                ArchiveCatalog(connection).link_job_version(job["id"], catalog_link)
            set_job(job["id"], "completed", message_key="job.rename_already_completed")
            return

        version_id: str | None = existing_new["id"] if existing_new else None
        source_stat = source.stat(follow_symlinks=False)
        plaintext_sha256 = (
            existing_new["plaintext_sha256"]
            if existing_new and existing_new["plaintext_sha256"]
            else None
        )

        if version_id is None:
            set_job(job["id"], "uploading", message_key="job.hashing_local_file")
            plaintext_sha256, source_stat = hash_stable_regular_file(source)
            if (
                previous["plaintext_sha256"]
                and previous["plaintext_sha256"] != plaintext_sha256
            ):
                raise RuntimeError(
                    "Local file digest no longer matches the archived version"
                )
            set_job(job["id"], "uploading", message_key="job.rename_copying")
            if encrypts_names:
                with vault_rclone_config(job) as runtime:
                    run_rclone(
                        "copyto",
                        str(source),
                        f"{runtime.remote_name}:{job['path']}",
                        job_progress_callback(job),
                        job_id=job["id"],
                        config_path=str(runtime.path),
                        bwlimit=job_bwlimit(job),
                    )
                    ensure_job_active(job["id"], "Rename stopped")
            else:
                run_rclone(
                    "copyto",
                    str(source),
                    configured_rclone_destination(job, job["path"]),
                    job_progress_callback(job),
                    job_id=job["id"],
                    bwlimit=job_bwlimit(job),
                )
                ensure_job_active(job["id"], "Rename stopped")
            head = s3_client().head_object(Bucket=job["s3_bucket"], Key=new_key)
            provider_version_id = head.get("VersionId")
            if not provider_version_id:
                raise RuntimeError(
                    "Rename stored without an S3 VersionId; bucket Versioning is required"
                )
            applied_policy_id = None
            if policy_id:
                apply_version_policy_tag(
                    s3_client(),
                    bucket=job["s3_bucket"],
                    key=new_key,
                    version_id=provider_version_id,
                    policy_id=policy_id,
                )
                applied_policy_id = policy_id
            timestamp = now_iso()
            with db() as connection:
                catalog = ArchiveCatalog(connection)
                version_id = catalog.record_archive_version(
                    vault_id=job["vault_id"],
                    path=job["path"],
                    object_key=new_key,
                    provider_version_id=provider_version_id,
                    size=head.get("ContentLength"),
                    storage_class=head.get("StorageClass", "STANDARD"),
                    etag=head.get("ETag", "").strip('"'),
                    uploaded_at=timestamp,
                    observed_at=timestamp,
                    scan_id=timestamp,
                    origin="upload",
                    desired_policy_id=policy_id,
                    applied_policy_id=applied_policy_id,
                )
            ensure_job_active(job["id"], "Rename stopped")
            after = source.stat(follow_symlinks=False)
            if (
                after.st_size != source_stat.st_size
                or after.st_mtime_ns != source_stat.st_mtime_ns
                or after.st_dev != source_stat.st_dev
                or after.st_ino != source_stat.st_ino
            ):
                raise RuntimeError("Local file changed since fingerprinting")
            set_job(job["id"], "verifying", message_key="job.rename_verifying_new_key")
            temporary = source.with_name(
                f".{source.name}.verify-{uuid.uuid4().hex}.tmp"
            )
            try:
                _download_plaintext_for_verification(job, temporary=temporary)
                remote_digest, _ = hash_stable_regular_file(temporary)
            finally:
                temporary.unlink(missing_ok=True)
            if remote_digest != plaintext_sha256:
                with db() as connection:
                    ArchiveCatalog(connection).mark_version_mismatch(
                        version_id,
                        plaintext_sha256=plaintext_sha256,
                        checked_at=now_iso(),
                    )
                raise RuntimeError("Cloud copy digest does not match local file")
            verified_at = now_iso()
            with db() as connection:
                catalog = ArchiveCatalog(connection)
                catalog.mark_version_verified(
                    version_id,
                    plaintext_sha256=plaintext_sha256,
                    verified_at=verified_at,
                )
                catalog.set_local_fingerprint(
                    vault_id=job["vault_id"],
                    path=job["path"],
                    plaintext_sha256=plaintext_sha256,
                    matched_archive_version_id=version_id,
                )

        assert version_id is not None
        ensure_job_active(job["id"], "Rename stopped")
        set_job(job["id"], "cleaning", message_key="job.rename_hiding_previous")
        ensure_scheduled_current_version(
            bucket=job["s3_bucket"],
            object_key=old_key,
            expected_version_id=previous.get("provider_version_id"),
            operation="Rename hide",
        )
        delete_result = s3_client().delete_object(
            Bucket=job["s3_bucket"],
            Key=old_key,
        )
        marker_version = delete_result.get("VersionId")
        if not marker_version:
            raise RuntimeError(
                "S3 did not return a delete marker VersionId for the previous key"
            )
        marker_at = now_iso()
        with db() as connection:
            catalog = ArchiveCatalog(connection)
            catalog.record_delete_marker(
                vault_id=job["vault_id"],
                path=job["path"],
                object_key=old_key,
                provider_version_id=marker_version,
                created_at=marker_at,
                observed_at=marker_at,
            )
            catalog.link_job_version(job["id"], version_id)
        set_job_progress(
            job["id"],
            int(job.get("total_bytes") or getattr(source_stat, "st_size", 0) or 0),
        )
        set_job(job["id"], "completed", message_key="job.rename_verified")
    except OperationCancelled:
        raise
    except Exception as exc:
        raise RuntimeError(safe_error_message(exc, secrets)) from exc


def record_automatic_cleanup_outcome(
    job: dict[str, Any],
    *,
    event: str,
    outcome: str,
    title: str,
    body: str,
) -> None:
    if job.get("origin") != "automatic":
        return
    try:
        with db() as connection:
            audit_event_store.record_audit_event(
                connection,
                event=event,
                actor_user_id=job.get("requested_by"),
                vault_id=job["vault_id"],
                job_id=job["id"],
                outcome=outcome,
                path=job["path"],
                archive_version_id=job.get("archive_version_id"),
            )
            notification_service.enqueue_notification(
                connection,
                user_id=int(job["requested_by"]),
                event=event,
                title=title,
                body=body,
                vault_id=job["vault_id"],
                job_id=job["id"],
            )
    except Exception as exc:
        try:
            with db() as connection:
                worker_error_store.record_worker_error(
                    connection,
                    component="local_cleanup_observability",
                    exc=exc,
                    vault_id=job.get("vault_id"),
                    job_id=job.get("id"),
                    event=event,
                )
        except Exception:
            pass


def process_free_space(job: dict[str, Any]) -> None:
    ensure_job_active(job["id"], "Freeing local space stopped")
    if not _runtime_settings().allow_local_delete:
        raise RuntimeError("Freeing local space is disabled")
    validate_cloud_vault(job)
    with db() as connection:
        target = ArchiveCatalog(connection).get_job_target(job["id"])
    if (
        not target
        or target["local_presence"] != "present"
        or target["local_file_type"] != "regular"
        or target["integrity"] != "verified"
        or target["availability"] != "available"
        or target["matched_archive_version_id"] != target["archive_version_id"]
        or not target["local_sha256"]
        or target["local_sha256"] != target["version_sha256"]
    ):
        raise RuntimeError("The file no longer has verifiable local and cloud copies")

    set_job(job["id"], "cleaning", message_key="job.verifying_cloud_copy")
    try:
        head = s3_client().head_object(
            Bucket=job["s3_bucket"],
            Key=target["object_key"],
            VersionId=target["provider_version_id"],
        )
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(
            "Cleanup blocked: unable to verify the S3 copy"
        ) from exc
    if (
        head.get("ContentLength") is not None
        and int(head["ContentLength"]) != int(target["cloud_size"])
    ):
        raise RuntimeError("Cleanup blocked: S3 object size no longer matches")
    ensure_job_active(job["id"], "Freeing local space stopped")
    local_path = safe_local_entry_path(job["source_root"], job["path"])
    claimed_path = local_path.with_name(
        f".{local_path.name}.cleanup-{uuid.uuid4().hex}.tmp"
    )
    try:
        local_path.rename(claimed_path)
    except FileNotFoundError as exc:
        raise RuntimeError("The local copy is no longer available") from exc
    try:
        local_digest, claimed_stat = hash_stable_regular_file(claimed_path)
        if (
            claimed_stat.st_size != target["local_size"]
            or claimed_stat.st_mtime_ns != target["local_mtime_ns"]
            or local_digest != target["version_sha256"]
        ):
            raise RuntimeError("Local file changed since fingerprinting")
        ensure_job_active(job["id"], "Freeing local space stopped")
        claimed_path.unlink()
    except Exception as exc:
        if not restore_claimed_local_copy(claimed_path, local_path):
            raise RuntimeError(
                f"Cleanup stopped; original content was preserved at {claimed_path}"
            ) from exc
        raise

    try:
        replacement_stat = local_path.lstat()
    except FileNotFoundError:
        replacement_stat = None
    with db() as connection:
        catalog = ArchiveCatalog(connection)
        if replacement_stat is None:
            catalog.mark_local_copy_missing(
                target["vault_file_id"],
                observed_at=now_iso(),
            )
        else:
            if stat.S_ISREG(replacement_stat.st_mode):
                file_type = "regular"
            elif stat.S_ISLNK(replacement_stat.st_mode):
                file_type = "symlink"
            else:
                file_type = "other"
            catalog.observe_replaced_local_copy(
                target["vault_file_id"],
                file_type=file_type,
                size=replacement_stat.st_size,
                mtime_ns=replacement_stat.st_mtime_ns,
                observed_at=now_iso(),
            )
    set_job_progress(job["id"], int(job.get("total_bytes") or 0))
    set_job(job["id"], "completed", message_key="job.local_space_freed")
    record_automatic_cleanup_outcome(
        job,
        event="local_cleanup.completed",
        outcome="success",
        title="Automatic local cleanup completed",
        body=f"The Local Copy of {job['path']} was removed; recovery remains available.",
    )


def process_storage_class(job: dict[str, Any]) -> None:
    """Restore if needed, then copy an Archive Version onto a new storage class.

    Warming from GLACIER / DEEP_ARCHIVE chains RestoreObject inside this Job so
    operators are not forced through Recover (which requires an absent Local Copy).
    """
    ensure_job_active(job["id"], "Storage class change stopped")
    validate_cloud_vault(job)
    target_class = (job.get("target_storage_class") or "").upper()
    if not target_class:
        raise RuntimeError("Target storage class is missing")
    with db() as connection:
        target = ArchiveCatalog(connection).get_job_target(job["id"])
    if (
        not target
        or not target.get("archive_version_id")
        or not target.get("object_key")
        or not target.get("provider_version_id")
        or target.get("availability") != "available"
    ):
        raise RuntimeError("No available Archive Version for storage class change")

    client = s3_client()
    head = client.head_object(
        Bucket=job["s3_bucket"],
        Key=target["object_key"],
        VersionId=target["provider_version_id"],
    )
    # AWS omits StorageClass on STANDARD; never prefer a stale colder catalog class.
    head_class = (head.get("StorageClass") or "STANDARD").upper()
    if storage_class_requires_restore(head_class):
        restore_state, restore_expiry = restore_header_state(head.get("Restore"))
        if restore_state != "available":
            tier = (
                job.get("restore_tier")
                or _runtime_settings().restore_tier
                or "Bulk"
            )
            days = int(
                job.get("restore_days")
                or _runtime_settings().restore_days
                or 3
            )
            estimate = estimate_restore(
                size_bytes=int(target.get("cloud_size") or job.get("total_bytes") or 0),
                storage_class=str(head_class),
                tier=str(tier),
                days=days,
            )
            if job["status"] != "restoring" and restore_state == "not_requested":
                client.restore_object(
                    Bucket=job["s3_bucket"],
                    Key=target["object_key"],
                    VersionId=target["provider_version_id"],
                    RestoreRequest={
                        "Days": estimate.days,
                        "GlacierJobParameters": {"Tier": estimate.tier},
                    },
                )
                restore_state = "restoring"
                restore_expiry = None
            checked_at = now_iso()
            with db() as connection:
                ArchiveCatalog(connection).update_restore_state(
                    target["archive_version_id"],
                    state=restore_state,
                    expiry=restore_expiry,
                    checked_at=checked_at,
                    storage_class=head_class,
                )
            set_job(
                job["id"],
                "restoring",
                message_key="job.storage_class_restoring",
                message_params={"storage_class": head_class},
            )
            return

    if head_class == target_class:
        set_job(
            job["id"],
            "completed",
            message_key="job.storage_class_skipped_same",
            message_params={"storage_class": target_class},
        )
        return

    set_job(
        job["id"],
        "uploading",
        message_key="job.storage_class_changing",
        message_params={"storage_class": target_class},
    )
    ensure_job_active(job["id"], "Storage class change stopped")
    copy_source = {
        "Bucket": job["s3_bucket"],
        "Key": target["object_key"],
        "VersionId": target["provider_version_id"],
    }
    copy_result = client.copy_object(
        Bucket=job["s3_bucket"],
        Key=target["object_key"],
        CopySource=copy_source,
        StorageClass=target_class,
        MetadataDirective="COPY",
        TaggingDirective="COPY",
    )
    new_version_id = copy_result.get("VersionId")
    if not new_version_id:
        # Unversioned buckets keep the same logical object; fall back to head.
        new_head = client.head_object(
            Bucket=job["s3_bucket"],
            Key=target["object_key"],
        )
        new_version_id = new_head.get("VersionId") or target["provider_version_id"]
        etag = (new_head.get("ETag") or "").strip('"') or None
        recorded_class = (new_head.get("StorageClass") or target_class).upper()
    else:
        etag = None
        copy_etag = (copy_result.get("CopyObjectResult") or {}).get("ETag")
        if copy_etag:
            etag = str(copy_etag).strip('"')
        recorded_class = target_class

    ensure_job_active(job["id"], "Storage class change stopped")
    timestamp = now_iso()
    with db() as connection:
        ArchiveCatalog(connection).update_version_storage_placement(
            target["archive_version_id"],
            provider_version_id=str(new_version_id),
            storage_class=recorded_class,
            etag=etag,
            observed_at=timestamp,
        )

    # Drop the previous provider version when copy created a new VersionId so the
    # catalog keeps one recoverable identity without orphaning billed versions.
    if str(new_version_id) != str(target["provider_version_id"]):
        try:
            client.delete_object(
                Bucket=job["s3_bucket"],
                Key=target["object_key"],
                VersionId=target["provider_version_id"],
            )
        except Exception:
            # Catalog already points at the new version; orphan cleanup is best-effort.
            pass

    set_job(
        job["id"],
        "completed",
        message_key="job.storage_class_completed",
        message_params={"storage_class": recorded_class},
    )


def restore_header_state(value: str | None) -> tuple[str, str | None]:
    if not value:
        return "not_requested", None
    if 'ongoing-request="true"' in value:
        return "restoring", None
    match = re.search(r'expiry-date="([^"]+)"', value)
    return "available", match.group(1) if match else None


ARCHIVE_RESTORE_REQUIRED = frozenset({"GLACIER", "DEEP_ARCHIVE"})


def storage_class_requires_restore(storage_class: str | None) -> bool:
    return (storage_class or "").upper() in ARCHIVE_RESTORE_REQUIRED


def download_exact_version_plaintext(
    job: dict[str, Any],
    *,
    object_key: str,
    provider_version_id: str,
    temporary: Path,
) -> None:
    """Download one exact Archive Version as plaintext into ``temporary``.

    Always pins the S3 ``VersionId``. Path-only current-object downloads are
    forbidden because they can return a different object version.

    Plain vaults use boto3 TransferManager (parallel range GETs for large
    objects). Crypt vaults keep Rclone so content/name decryption stays on the
    crypt remote, with multi-thread download flags for large objects.
    """
    if not provider_version_id:
        raise RuntimeError("Exact S3 VersionId is required for recovery")
    ensure_job_active(job["id"], "Recovery stopped")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    if vault_encrypts_names(job) or vault_encrypts_content(job):
        version_flag = f"--s3-version-id={provider_version_id}"
        if vault_encrypts_names(job):
            with vault_rclone_config(job) as runtime:
                logical_path = object_key_to_path(
                    object_key,
                    job.get("s3_prefix") or "",
                    is_crypt=True,
                    encrypted_names=True,
                    runtime=runtime,
                ) or job["path"]
                run_rclone(
                    "copyto",
                    f"{runtime.remote_name}:{logical_path}",
                    str(temporary),
                    version_flag,
                    *rclone_download_perf_args(),
                    job_progress_callback(job),
                    job_id=job["id"],
                    config_path=str(runtime.path),
                    bwlimit=job_bwlimit(job),
                )
        else:
            logical_path = object_key_to_path(
                object_key,
                job.get("s3_prefix") or "",
                is_crypt=vault_encrypts_content(job),
            ) or job["path"]
            run_rclone(
                "copyto",
                configured_rclone_destination(job, logical_path),
                str(temporary),
                version_flag,
                *rclone_download_perf_args(),
                job_progress_callback(job),
                job_id=job["id"],
                bwlimit=job_bwlimit(job),
            )
        ensure_job_active(job["id"], "Recovery stopped")
        if not temporary.is_file():
            raise RuntimeError("Rclone did not create the recovered file")
        return

    client = s3_client()
    reporter = _ThrottledByteProgress(job["id"])

    def on_bytes(amount: int) -> None:
        ensure_job_active(job["id"], "Recovery stopped")
        reporter.add(amount)

    try:
        # TransferManager issues parallel ranged GetObject calls above the
        # multipart threshold while still pinning VersionId via ExtraArgs.
        client.download_file(
            Bucket=job["s3_bucket"],
            Key=object_key,
            Filename=str(temporary),
            ExtraArgs={"VersionId": provider_version_id},
            Callback=on_bytes,
            Config=s3_download_transfer_config(job),
        )
        reporter.flush()
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    ensure_job_active(job["id"], "Recovery stopped")
    if not temporary.is_file():
        raise RuntimeError("S3 download did not create the recovered file")


def process_recover(job: dict[str, Any]) -> None:
    ensure_job_active(job["id"], "Recovery stopped")
    validate_cloud_vault(job)
    with db() as connection:
        target = ArchiveCatalog(connection).get_job_target(job["id"])
    if (
        not target
        or target["integrity"] != "verified"
        or target["availability"] != "available"
        or not target["provider_version_id"]
        or not target["object_key"]
        or not target["version_sha256"]
        or not target["archive_version_id"]
    ):
        raise RuntimeError("The file is not available in the cloud")
    if target["local_presence"] == "present":
        raise RuntimeError("A local copy is already present")

    expected_digest = str(target["version_sha256"]).lower()
    object_key = str(target["object_key"])
    provider_version_id = str(target["provider_version_id"])
    archive_version_id = str(target["archive_version_id"])
    storage_class = target.get("storage_class")
    size_bytes = int(target.get("cloud_size") or job.get("total_bytes") or 0)
    restore_expiry = target.get("restore_expiry")

    if (
        storage_class_requires_restore(storage_class)
        and target.get("restore_state") != "available"
        and job.get("approved_at") is None
    ):
        with db() as connection:
            from .services.cost_estimates import get_active_price_book

            runtime = _runtime_settings(connection)
            book = get_active_price_book(connection)
        tier = job.get("restore_tier") or runtime.restore_tier or "Bulk"
        days = int(job.get("restore_days") or runtime.restore_days or 3)
        estimate = estimate_restore(
            size_bytes=size_bytes,
            storage_class=str(storage_class),
            tier=str(tier),
            days=days,
            pricing=book.restore_rates,
        )
        if is_high_impact_restore(
            size_bytes=size_bytes,
            estimated_cost_eur=estimate.estimated_cost_eur,
            size_threshold_gib=runtime.restore_high_impact_gib,
            cost_threshold_eur=runtime.restore_high_impact_eur,
        ):
            hold_seconds = int(runtime.restore_approval_hold_seconds)
            pending_until = (
                datetime.now(timezone.utc) + timedelta(seconds=hold_seconds)
            ).isoformat()
            with db() as connection:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status='pending_approval',
                        message=%s,
                        pending_until=%s,
                        restore_tier=%s,
                        restore_days=%s,
                        estimated_cost_eur=%s,
                        estimated_hours=%s,
                        updated_at=%s
                    WHERE id=%s
                    """,
                    (
                        "High-impact Glacier restore held for primary-owner approval; "
                        "RestoreObject cannot be cancelled after AWS accepts it",
                        pending_until,
                        estimate.tier,
                        estimate.days,
                        estimate.estimated_cost_eur,
                        estimate.estimated_hours,
                        now_iso(),
                        job["id"],
                    ),
                )
            return

    head = s3_client().head_object(
        Bucket=job["s3_bucket"],
        Key=object_key,
        VersionId=provider_version_id,
    )
    head_class = head.get("StorageClass") or storage_class
    restore_state, restore_expiry = restore_header_state(head.get("Restore"))
    if storage_class_requires_restore(head_class) and restore_state != "available":
        tier = (
            job.get("restore_tier")
            or _runtime_settings().restore_tier
            or "Bulk"
        )
        days = int(
            job.get("restore_days")
            or _runtime_settings().restore_days
            or 3
        )
        estimate = estimate_restore(
            size_bytes=size_bytes,
            storage_class=str(head_class),
            tier=str(tier),
            days=days,
        )
        if job["status"] != "restoring" and restore_state == "not_requested":
            s3_client().restore_object(
                Bucket=job["s3_bucket"],
                Key=object_key,
                VersionId=provider_version_id,
                RestoreRequest={
                    "Days": days,
                    "GlacierJobParameters": {"Tier": estimate.tier},
                },
            )
            restore_state = "restoring"
            restore_expiry = None
        checked_at = now_iso()
        with db() as connection:
            ArchiveCatalog(connection).update_restore_state(
                archive_version_id,
                state=restore_state,
                expiry=restore_expiry,
                checked_at=checked_at,
                storage_class=head_class,
            )
        if restore_state != "available":
            set_job(
                job["id"],
                "restoring",
                "Waiting for Glacier restore; RestoreObject cannot be cancelled after AWS accepts it",
            )
            return

    destination = safe_local_entry_path(job["source_root"], job["path"])
    temporary = destination.with_name(
        f".{destination.name}.restore-{uuid.uuid4().hex}.tmp"
    )
    is_crypt = vault_encrypts_content(job)
    message = "Downloading and decrypting" if is_crypt else "Downloading"
    set_job(job["id"], "downloading", message)
    try:
        download_exact_version_plaintext(
            job,
            object_key=object_key,
            provider_version_id=provider_version_id,
            temporary=temporary,
        )
        ensure_job_active(job["id"], "Recovery stopped")
        set_job(job["id"], "verifying", "Verifying recovered plaintext")
        recovered_digest, recovered_stat = hash_stable_regular_file(temporary)
        if recovered_digest != expected_digest:
            raise RuntimeError(
                "Recovered plaintext digest does not match the Archive Version"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(destination):
            raise RuntimeError(
                "A local copy already exists at the recovery destination"
            )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)

    with db() as connection:
        catalog = ArchiveCatalog(connection)
        catalog.observe_local_copy(
            vault_id=job["vault_id"],
            path=job["path"],
            file_type="regular",
            size=recovered_stat.st_size,
            mtime_ns=recovered_stat.st_mtime_ns,
            observed_at=now_iso(),
        )
        catalog.set_local_fingerprint(
            vault_id=job["vault_id"],
            path=job["path"],
            plaintext_sha256=expected_digest,
            matched_archive_version_id=archive_version_id,
        )
        if target.get("restore_state") or storage_class_requires_restore(head_class):
            catalog.update_restore_state(
                archive_version_id,
                state="available" if storage_class_requires_restore(head_class) else None,
                expiry=restore_expiry,
                checked_at=now_iso(),
                storage_class=head_class,
            )
    set_job_progress(job["id"], int(job.get("total_bytes") or recovered_stat.st_size))
    set_job(job["id"], "completed", f"Recovered to {destination}")


def process_cloud_archive(job: dict[str, Any]) -> None:
    """Hide the current key with a Delete Marker; never delete noncurrent versions."""
    ensure_job_active(job["id"], "Cloud archival stopped")
    if not job.get("cloud_deletion_enabled"):
        raise RuntimeError("Cloud deletion is disabled for this vault")
    validate_cloud_vault(job)
    with db() as connection:
        target = ArchiveCatalog(connection).get_job_target(job["id"])
        version = connection.execute(
            """
            SELECT object_key, provider_version_id
            FROM archive_versions
            WHERE id=%s
            """,
            (job.get("archive_version_id"),),
        ).fetchone()
    if not target or not version:
        raise RuntimeError("Archive Version target is no longer available")
    object_key = version["object_key"]
    set_job(job["id"], "cleaning", message_key="job.cloud_archive_creating_marker")
    ensure_job_active(job["id"], "Cloud archival stopped")
    ensure_scheduled_current_version(
        bucket=job["s3_bucket"],
        object_key=object_key,
        expected_version_id=version.get("provider_version_id"),
        operation="Cloud archival",
    )
    delete_result = s3_client().delete_object(
        Bucket=job["s3_bucket"],
        Key=object_key,
    )
    marker_version = delete_result.get("VersionId")
    if not marker_version:
        raise RuntimeError("S3 did not return a Delete Marker VersionId")
    stamp = now_iso()
    with db() as connection:
        catalog = ArchiveCatalog(connection)
        catalog.record_delete_marker(
            vault_id=job["vault_id"],
            path=job["path"],
            object_key=object_key,
            provider_version_id=marker_version,
            created_at=stamp,
            observed_at=stamp,
        )
        cloud_deletion_service.record_archive_completed(
            connection,
            job_id=job["id"],
            vault_id=job["vault_id"],
            path=job["path"],
            marker_version_id=marker_version,
            actor_user_id=job.get("requested_by"),
            updated_at=stamp,
        )
    set_job(
        job["id"],
        "completed",
        message_key="job.cloud_archive_completed",
    )


def process_cloud_purge(job: dict[str, Any]) -> None:
    """Permanently delete every selected Archive Version and Delete Marker."""
    ensure_job_active(job["id"], "Cloud purge stopped")
    if job["status"] == "pending_delay":
        pending_until = job.get("pending_until")
        if not pending_until:
            raise RuntimeError("Permanent purge is missing its delay deadline")
        if datetime.fromisoformat(pending_until) > datetime.now(timezone.utc):
            return

    if not job.get("cloud_deletion_enabled"):
        raise RuntimeError("Cloud deletion is disabled for this vault")
    validate_cloud_vault(job)
    claimed_at = now_iso()
    with db() as connection:
        purge_jobs, items = cloud_deletion_service.claim_purge_group(
            connection,
            lead_job_id=job["id"],
            claimed_at=claimed_at,
            message=translate("job.cloud_purge_deleting", locale=DEFAULT_LOCALE),
            message_key="job.cloud_purge_deleting",
        )
    if not purge_jobs:
        return
    client = s3_client()
    failures = 0
    for offset in range(0, len(items), 1000):
        batch = items[offset : offset + 1000]
        ensure_job_active(job["id"], "Cloud purge stopped")
        stamp = now_iso()
        deleted_ids: list[int] = []
        failed_items: list[tuple[int, str]] = []
        try:
            response = client.delete_objects(
                Bucket=job["s3_bucket"],
                Delete={
                    "Objects": [
                        {
                            "Key": item["object_key"],
                            "VersionId": item["provider_version_id"],
                        }
                        for item in batch
                    ],
                    "Quiet": True,
                },
            )
            errors: dict[tuple[str, str], str] = {}
            for error in response.get("Errors") or []:
                key = error.get("Key")
                version_id = error.get("VersionId")
                if not key or not version_id:
                    raise RuntimeError("S3 returned an unidentifiable delete error")
                code = error.get("Code") or "DeleteError"
                message = error.get("Message") or "S3 rejected the deletion"
                errors[(str(key), str(version_id))] = f"{code}: {message}"
            for item in batch:
                item_id = int(item["id"])
                error_message = errors.get(
                    (str(item["object_key"]), str(item["provider_version_id"]))
                )
                if error_message is None:
                    deleted_ids.append(item_id)
                else:
                    failed_items.append((item_id, error_message))
        except Exception as exc:
            failed_items = [(int(item["id"]), str(exc)) for item in batch]
        failures += len(failed_items)
        with db() as connection:
            cloud_deletion_service.mark_items_deleted(
                connection,
                item_ids=deleted_ids,
                updated_at=stamp,
            )
            cloud_deletion_service.mark_items_failed(
                connection,
                failures=failed_items,
                updated_at=stamp,
            )
    stamp = now_iso()
    with db() as connection:
        for purge_job in purge_jobs:
            cloud_deletion_service.finalize_purge_job(
                connection,
                job_id=int(purge_job["id"]),
                vault_file_id=purge_job["vault_file_id"],
                actor_user_id=purge_job.get("requested_by"),
                updated_at=stamp,
            )
    if failures:
        # finalize_purge_job already persisted failed status; avoid double-write
        # through process_job's exception handler.
        return


def process_job(job: dict[str, Any]) -> bool:
    """Process one queue item and persist its terminal error state."""
    try:
        with db() as connection:
            runtime = _runtime_settings(connection)
            current = connection.execute(
                "SELECT status FROM jobs WHERE id=%s",
                (job["id"],),
            ).fetchone()
        if current is None:
            return False
        if current["status"] == "cancelled":
            return False
        if current["status"] in {"queued", "retrying", "restoring"}:
            # Drop stale in-memory cancel flags from prior process/test DBs that
            # reused this Job id; a live cancel after this point re-arms the set.
            with operation_process_lock:
                cancelled_jobs.discard(int(job["id"]))
            job["status"] = current["status"]
        local_statuses = {
            "upload": {"queued", "retrying"},
            "rename": {"queued"},
            "recover": {"queued", "retrying", "restoring"},
            "free-space": {"queued"},
        }
        allowed_statuses = local_statuses.get(job["action"])
        if allowed_statuses is not None and job["status"] not in allowed_statuses:
            return False
        if allowed_statuses is not None and "source_root" in job:
            access = source_layout.vault_local_access(job["source_root"])
            if not access.local_operations_allowed:
                # Leave the Job queued/suspended; restoring the expected mount
                # is the only path back to local execution.
                return False
        if job["status"] == "restoring":
            last_check = datetime.fromisoformat(job["updated_at"])
            age = (datetime.now(timezone.utc) - last_check).total_seconds()
            if age < runtime.restore_poll_interval:
                return False
        if job["action"] == "upload" and job["status"] in {"queued", "retrying"}:
            process_upload(job)
        elif job["action"] == "rename" and job["status"] == "queued":
            process_rename(job)
        elif job["action"] == "recover" and job["status"] in {
            "queued",
            "retrying",
            "restoring",
        }:
            process_recover(job)
        elif job["action"] == "free-space" and job["status"] == "queued":
            process_free_space(job)
        elif job["action"] == "storage-class" and job["status"] in {
            "queued",
            "restoring",
        }:
            process_storage_class(job)
        elif job["action"] == "cloud-archive" and job["status"] == "queued":
            process_cloud_archive(job)
        elif job["action"] == "cloud-purge" and job["status"] in {
            "queued",
            "pending_delay",
        }:
            process_cloud_purge(job)
        else:
            return False
        return True
    except OperationCancelled:
        message_keys = {
            "upload": "job.upload_stopped",
            "recover": "job.recovery_stopped",
            "free-space": "job.cleanup_stopped",
            "rename": "job.rename_stopped",
            "cloud-archive": "job.cloud_archive_stopped",
            "cloud-purge": "job.cloud_purge_stopped",
            "storage-class": "job.storage_class_stopped",
        }
        set_job(
            job["id"],
            "cancelled",
            message_key=message_keys.get(job["action"], "job.operation_stopped"),
        )
        if job["action"] == "free-space":
            record_automatic_cleanup_outcome(
                job,
                event="local_cleanup.cancelled",
                outcome="cancelled",
                title="Automatic local cleanup cancelled",
                body=f"The Local Copy cleanup for {job['path']} was cancelled.",
            )
    except Exception as exc:
        secrets = None
        if job.get("encryption_mode") == "crypt" and job.get(
            "crypt_password_ciphertext"
        ):
            try:
                secrets = secrets_for_vault(job)
            except Exception:
                secrets = None
        message = safe_error_message(exc, secrets)
        failure_kind = (
            classify_upload_failure(message) if job["action"] == "upload" else ""
        )
        if job["action"] == "upload" and failure_kind == "source_changed":
            next_attempt = int(job.get("retry_count") or 0) + 1
            with db() as connection:
                policy = get_policy(connection, int(job["vault_id"]))
            schedule_upload_retry(
                job["id"],
                message_key="job.retrying_source_changed",
                message_params={"seconds": policy.stability_seconds},
                retry_count=next_attempt,
                delay_seconds=policy.stability_seconds,
            )
            return True
        if job["action"] == "upload" and failure_kind == "transient":
            next_attempt = int(job.get("retry_count") or 0) + 1
            if next_attempt <= UPLOAD_RETRY_MAX_ATTEMPTS:
                schedule_upload_retry(
                    job["id"],
                    message_key="job.retrying_transient",
                    message_params={"error": message},
                    retry_count=next_attempt,
                )
                return True
        set_job(job["id"], "failed", message)
        if job["action"] == "free-space":
            record_automatic_cleanup_outcome(
                job,
                event="local_cleanup.failed",
                outcome="failure",
                title="Automatic local cleanup failed",
                body=f"The Local Copy cleanup for {job['path']} failed: {message}",
            )
    return True


def process_jobs_once() -> int:
    now = now_iso()
    current = datetime.now(timezone.utc)
    with db() as connection:
        runtime = _runtime_settings(connection)
        # Over-fetch candidates so fair interleave can still fill the concurrency
        # budget when one Vault dominates the oldest requested_at values.
        batch_size = max(10, runtime.operation_concurrency * 10)
        queued_candidates = connection.execute(
            f"""
            SELECT j.*, v.source_root, v.s3_bucket,
                   v.s3_prefix, v.rclone_remote, v.encryption_mode,
                   v.crypt_password_ciphertext, v.crypt_password2_ciphertext,
                   v.uuid AS vault_uuid, v.name AS vault_name,
                   v.cloud_deletion_enabled
            FROM jobs j
            JOIN vaults v ON v.id=j.vault_id
            WHERE v.enabled=TRUE
              AND v.relocation_state='ready'
              AND (
                    j.status='queued'
                 OR (
                        j.status='retrying'
                    AND (j.retry_after IS NULL OR j.retry_after <= %s)
                 )
                 OR (
                        j.action='cloud-purge'
                    AND j.status='pending_delay'
                    AND j.pending_until IS NOT NULL
                    AND j.pending_until <= %s
                 )
              )
            ORDER BY j.requested_at ASC
            LIMIT {batch_size}
            """,
            (now, now),
        ).fetchall()
        restoring_jobs = connection.execute(
            """
            SELECT j.*, v.source_root, v.s3_bucket,
                   v.s3_prefix, v.rclone_remote, v.encryption_mode,
                   v.crypt_password_ciphertext, v.crypt_password2_ciphertext,
                   v.uuid AS vault_uuid, v.name AS vault_name,
                   v.cloud_deletion_enabled
            FROM jobs j
            JOIN vaults v ON v.id=j.vault_id
            WHERE j.status='restoring' AND v.enabled=TRUE
              AND v.relocation_state='ready'
            ORDER BY j.updated_at ASC
            LIMIT 10
            """
        ).fetchall()
        policy_cache: dict[int, Any] = {}
        eligible_candidates: list[dict[str, Any]] = []
        seen_purge_groups: set[tuple[int, str]] = set()
        for row in queued_candidates:
            job = dict(row)
            vault_id = int(job["vault_id"])
            if vault_relocation.local_work_suspended({"id": vault_id}):
                continue
            if vault_id not in policy_cache:
                policy_cache[vault_id] = get_policy(connection, vault_id)
            policy = policy_cache[vault_id]
            if not policy_allows_transfer_now(policy, now=current):
                continue
            limit = effective_bandwidth_kibps(
                global_limit=runtime.bandwidth_limit_kibps,
                vault_limit=policy.bandwidth_limit_kibps,
            )
            job["bwlimit"] = rclone_bwlimit_arg(limit)
            if job["action"] == "cloud-purge" and job.get("group_id"):
                purge_group = (vault_id, str(job["group_id"]))
                if purge_group in seen_purge_groups:
                    continue
                seen_purge_groups.add(purge_group)
            eligible_candidates.append(job)
        restoring: list[dict[str, Any]] = []
        for row in restoring_jobs:
            job = dict(row)
            vault_id = int(job["vault_id"])
            if vault_id not in policy_cache:
                policy_cache[vault_id] = get_policy(connection, vault_id)
            policy = policy_cache[vault_id]
            limit = effective_bandwidth_kibps(
                global_limit=runtime.bandwidth_limit_kibps,
                vault_limit=policy.bandwidth_limit_kibps,
            )
            job["bwlimit"] = rclone_bwlimit_arg(limit)
            restoring.append(job)

    queued_jobs = select_fair_jobs(
        eligible_candidates,
        limit=runtime.operation_concurrency,
    )
    jobs = [*queued_jobs, *restoring]
    if jobs:
        worker_count = min(runtime.operation_concurrency, len(jobs))
        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="operation"
        ) as executor:
            list(executor.map(process_job, jobs))
    return len(queued_jobs)


def scan_all_vaults() -> None:
    with db() as connection:
        vaults = connection.execute("SELECT * FROM vaults WHERE enabled=TRUE ORDER BY id").fetchall()
    for vault in vaults:
        scan_vault(vault)


def _filesystem_watch_filter(_: Change, path: str) -> bool:
    return not is_restore_temporary_name(Path(path).name)


async def _watch_vault_filesystem(vault: dict[str, Any]) -> None:
    while True:
        if vault_relocation.local_work_suspended(vault):
            await asyncio.sleep(1)
            continue
        with db() as connection:
            current = connection.execute(
                "SELECT relocation_state FROM vaults WHERE id=%s", (vault["id"],)
            ).fetchone()
        if current and current["relocation_state"] != "ready":
            await asyncio.sleep(1)
            continue
        access = await asyncio.to_thread(
            source_layout.vault_local_access, vault["source_root"]
        )
        if not access.local_operations_allowed:
            await asyncio.sleep(5)
            continue
        root = Path(vault["source_root"]).resolve()
        if not root.is_dir():
            await asyncio.sleep(5)
            continue
        try:
            runtime = await asyncio.to_thread(_runtime_settings)
            async for changes in awatch(
                root,
                watch_filter=_filesystem_watch_filter,
                debounce=runtime.filesystem_watch_debounce_ms,
                force_polling=settings.filesystem_watch_force_polling,
                poll_delay_ms=runtime.filesystem_watch_poll_ms,
                recursive=True,
                ignore_permission_denied=True,
            ):
                await asyncio.to_thread(apply_filesystem_changes, vault, changes)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            vault_id = int(vault["id"])
            with status_lock:
                status = runtime_status.setdefault(
                    vault_id,
                    {"scanning": False, "last_scan": None, "last_error": None},
                )
                status["last_error"] = f"Source watcher: {exc}"
            await asyncio.sleep(5)


def _enabled_vaults() -> list[dict[str, Any]]:
    with db() as connection:
        return connection.execute(
            "SELECT * FROM vaults WHERE enabled=TRUE ORDER BY id"
        ).fetchall()


async def filesystem_watch_loop() -> None:
    """Keep one recursive filesystem watcher aligned with each enabled vault."""
    watchers: dict[int, tuple[str, asyncio.Task[None]]] = {}
    try:
        while True:
            await asyncio.to_thread(source_layout.verify_mounts_once)
            vaults = await asyncio.to_thread(_enabled_vaults)
            desired = {int(vault["id"]): vault for vault in vaults}
            for vault_id, (source_root, task) in list(watchers.items()):
                vault = desired.get(vault_id)
                if vault is None or vault["source_root"] != source_root or task.done():
                    task.cancel()
                    watchers.pop(vault_id)
            for vault_id, vault in desired.items():
                if vault_relocation.local_work_suspended(vault):
                    existing = watchers.get(vault_id)
                    if existing is not None:
                        existing[1].cancel()
                        watchers.pop(vault_id, None)
                    continue
                access = source_layout.vault_local_access(vault["source_root"])
                if not access.local_operations_allowed:
                    existing = watchers.get(vault_id)
                    if existing is not None:
                        existing[1].cancel()
                        watchers.pop(vault_id, None)
                    continue
                if vault_id not in watchers:
                    watchers[vault_id] = (
                        vault["source_root"],
                        asyncio.create_task(_watch_vault_filesystem(vault)),
                    )
            await asyncio.sleep(10)
    finally:
        tasks = [task for _, task in watchers.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def audit_all_vaults() -> None:
    with db() as connection:
        vaults = connection.execute(
            "SELECT * FROM vaults WHERE enabled=TRUE ORDER BY id"
        ).fetchall()
    for vault in vaults:
        vault_id = int(vault["id"])
        with status_lock:
            status = runtime_status.setdefault(
                vault_id,
                {
                    "scanning": False,
                    "last_scan": None,
                    "last_audit": None,
                    "last_error": None,
                },
            )
        try:
            validate_cloud_vault(vault)
            with db() as connection:
                report = audit_vault_catalog(connection, vault, s3_client())
            with status_lock:
                status["last_audit"] = now_iso()
                status["last_audit_report"] = report
                status["last_error"] = None
        except Exception as exc:
            with status_lock:
                status["last_error"] = f"Catalog audit: {exc}"


def _deliver_notifications_once() -> None:
    """Best-effort outbound notification delivery pass."""
    try:
        with db() as connection:
            push_client = None
            from .config import push_configured

            if push_configured():
                push_client = notification_service.PyWebPushClient()
            stats = notification_service.deliver_pending_notifications(
                connection,
                push_client=push_client,
            )
        for _ in range(int(stats.get("delivered", 0))):
            metrics_service.inc(
                "notification_deliveries_total",
                channel="outbound",
                result="delivered",
            )
        for _ in range(int(stats.get("failed", 0))):
            metrics_service.inc(
                "notification_deliveries_total",
                channel="outbound",
                result="failed",
            )
    except Exception as exc:
        try:
            with db() as connection:
                worker_error_store.record_worker_error(
                    connection,
                    component="notification_delivery",
                    exc=exc,
                )
        except Exception:
            pass


def _run_scheduled_metadata_backup_once() -> None:
    """Create a scheduled encrypted metadata backup when configured."""
    try:
        with db() as connection:
            runtime = _runtime_settings(connection)
            if runtime.metadata_backup_interval_seconds <= 0:
                return
            result = metadata_backup_service.run_metadata_backup(
                connection,
                reason="scheduled",
                backup_dir=settings.metadata_backup_dir,
                object_store=metadata_backup_service.default_object_store(),
                retention=runtime.metadata_backup_retention,
                s3_prefix=runtime.metadata_backup_s3_prefix,
            )
        metrics_service.inc("metadata_backups_total", result="succeeded")
        if result.get("path"):
            metrics_service.set_gauge("metadata_backup_last_success_unixtime", float(int(datetime.now(timezone.utc).timestamp())))
    except Exception as exc:
        metrics_service.inc("metadata_backups_total", result="failed")
        try:
            with db() as connection:
                worker_error_store.record_worker_error(
                    connection,
                    component="metadata_backup",
                    exc=exc,
                )
        except Exception:
            pass


def _verify_latest_metadata_backup_once() -> None:
    """Periodically restore-verify the newest succeeded backup run's bound artifact."""
    runtime = _runtime_settings()
    if runtime.metadata_backup_verify_interval_seconds <= 0:
        return
    backup_dir = Path(settings.metadata_backup_dir)
    work_dir = backup_dir / "verify-scratch"
    try:
        with db() as connection:
            latest = connection.execute(
                """
                SELECT id, local_path, digest_sha256 FROM metadata_backup_runs
                WHERE status='succeeded'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        if not latest:
            return
        artifact = None
        recorded_digest = latest.get("digest_sha256")
        if latest.get("local_path"):
            candidate = Path(str(latest["local_path"]))
            if candidate.is_file():
                if (
                    not recorded_digest
                    or hashlib.sha256(candidate.read_bytes()).hexdigest()
                    == recorded_digest
                ):
                    artifact = candidate
        if artifact is None and recorded_digest:
            for path in metadata_backup_service.list_local_backup_files(backup_dir):
                if hashlib.sha256(path.read_bytes()).hexdigest() == recorded_digest:
                    artifact = path
                    break
        if artifact is None:
            return
        result = metadata_backup_service.verify_restore_isolated(
            artifact,
            work_dir=work_dir,
            object_store=None,
        )
        if not result.get("ok"):
            raise metadata_backup_service.BackupError("Restore verification reported not ok")
        metrics_service.inc("metadata_backup_verifications_total", result="succeeded")
        with db() as connection:
            connection.execute(
                """
                UPDATE metadata_backup_runs
                SET status='verified', verified_at=%s, finished_at=%s
                WHERE id=%s
                """,
                (
                    metadata_backup_service.now_iso(),
                    metadata_backup_service.now_iso(),
                    latest["id"],
                ),
            )
    except Exception as exc:
        metrics_service.inc("metadata_backup_verifications_total", result="failed")
        try:
            with db() as connection:
                worker_error_store.record_worker_error(
                    connection,
                    component="metadata_backup_verify",
                    exc=exc,
                )
                metadata_backup_service.notify_admins_of_backup_failure(
                    connection,
                    reason="verify",
                    error_message=str(exc),
                )
        except Exception:
            pass


async def background_loop() -> None:
    await asyncio.sleep(5)
    last_scan = 0.0
    last_audit = 0.0
    last_backup = 0.0
    last_backup_verify = 0.0
    loop = asyncio.get_running_loop()
    while True:
        queued_count = 0
        runtime = settings
        health_service.mark_worker_heartbeat()
        metrics_service.set_gauge("worker_up", 1)
        try:
            runtime = await asyncio.to_thread(_runtime_settings)
            # Reconcile Source Volume identity even when filesystem watchers are
            # disabled, before workers or scheduled scans can touch local data.
            await asyncio.to_thread(source_layout.verify_mounts_once)
            queued_count = await asyncio.to_thread(process_jobs_once)
            metrics_service.set_gauge("queue_depth", float(queued_count))
            current = loop.time()
            if current - last_scan >= runtime.scan_interval:
                await asyncio.to_thread(scan_all_vaults)
                last_scan = current
            if current - last_audit >= runtime.audit_interval:
                await asyncio.to_thread(audit_all_vaults)
                last_audit = current
            if (
                runtime.metadata_backup_interval_seconds > 0
                and current - last_backup >= runtime.metadata_backup_interval_seconds
            ):
                await asyncio.to_thread(_run_scheduled_metadata_backup_once)
                last_backup = current
            if (
                runtime.metadata_backup_verify_interval_seconds > 0
                and current - last_backup_verify
                >= runtime.metadata_backup_verify_interval_seconds
            ):
                await asyncio.to_thread(_verify_latest_metadata_backup_once)
                last_backup_verify = current
            await asyncio.to_thread(_deliver_notifications_once)
        except Exception as exc:
            # Persist a classified error and alert seam instead of failing silently.
            try:
                with db() as connection:
                    worker_error_store.record_worker_error(
                        connection,
                        component="background_loop",
                        exc=exc,
                    )
                metrics_service.inc(
                    "worker_errors_total",
                    classification=worker_error_store.classify_exception(exc),
                )
            except Exception:
                # Last-resort: never let error accounting crash the loop.
                pass
            metrics_service.set_gauge("worker_up", 0)
        batch_size = max(10, runtime.operation_concurrency * 10)
        delay = 0.1 if queued_count >= batch_size else max(2, runtime.queue_poll_interval)
        await asyncio.sleep(delay)
