from __future__ import annotations

import asyncio
import base64
import configparser
import hashlib
import json
import os
import re
import selectors
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
from urllib.parse import urlencode

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
    decode_object_relative_paths,
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
from .services import vault_decommission as vault_decommission_service
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
# Short-lived journals compensate committed observation batches if a pinned root
# or scan generation fails before the final catalog transaction. They are
# process-local; a crash remains conservative because no missing transition is
# committed until the scan completes.
_active_scan_journals: dict[tuple[int, str], dict[str, Any]] = {}
scan_locks: dict[int, threading.Lock] = {}
status_lock = threading.Lock()
operation_process_lock = threading.Lock()


def scan_lock_for_vault(vault_id: int) -> threading.Lock:
    """Return the process-wide scan/relocation lock for one Vault."""
    with status_lock:
        return scan_locks.setdefault(int(vault_id), threading.Lock())
active_operation_processes: dict[int, subprocess.Popen[Any]] = {}
cancelled_jobs: set[int] = set()

# Streaming verification never materializes the remote plaintext.  Keep every
# read bounded even when Rclone emits a very large object or a noisy diagnostic
# stream.  The selector loop drains both pipes so a provider error cannot
# deadlock a child that is still producing stdout.
RCLONE_STREAM_CHUNK_BYTES = 1024 * 1024
RCLONE_STDERR_TAIL_BYTES = 16 * 1024
RCLONE_STDERR_LINE_BYTES = 64 * 1024
RCLONE_PROCESS_TERMINATION_GRACE_SECONDS = 1.0

RESTORE_TEMPORARY_RE = re.compile(
    r"\..+\.restore-[0-9a-f]{32}\.tmp(?:\..+\.partial)?"
)
CLEANUP_TEMPORARY_RE = re.compile(r"\..+\.cleanup-[0-9a-f]{32}\.tmp")
VERIFY_TEMPORARY_RE = re.compile(r"\..+\.verify-[0-9a-f]{32}\.tmp")

UPLOAD_RETRY_BASE_SECONDS = 2
UPLOAD_RETRY_CAP_SECONDS = 300
UPLOAD_RETRY_MAX_ATTEMPTS = 8

# Local scans deliberately keep filesystem work outside write transactions.
# This is small enough to give other SQLite writers frequent admission while
# avoiding one transaction per catalogued entry.
LOCAL_SCAN_WRITE_BATCH_SIZE = 250

# S3 CopyObject is limited to objects up to 5 GiB.  Multipart copy keeps the
# source VersionId on every UploadPartCopy request and uses a deliberately
# conservative part size so even the largest supported object stays below the
# provider's 10,000-part limit.
S3_SINGLE_COPY_MAX_BYTES = 5 * 1024**3
S3_MULTIPART_COPY_MIN_PART_BYTES = 5 * 1024**2
S3_MULTIPART_COPY_PART_BYTES = 128 * 1024**2
S3_MULTIPART_COPY_MAX_PARTS = 10_000
S3_COPY_CHECKSUM_ALGORITHM = "SHA256"
S3_COPY_CHECKSUM_TYPE = "FULL_OBJECT"
S3_OBJECT_HASH_CHUNK_BYTES = 1024 * 1024

# A claim is intentionally durable rather than process-local.  Five minutes is
# long enough for ordinary provider calls while status/progress checkpoints renew
# it during transfers; a dead process becomes recoverable on the next restart.
JOB_CLAIM_LEASE_SECONDS = 5 * 60
JOB_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
JOB_LEASE_HELD_STATUSES = frozenset(
    {"downloading", "uploading", "verifying", "cleaning"}
)
_worker_claim = threading.local()

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
    "connection refused",
    "connection aborted",
    "network is unreachable",
    "no route to host",
    "broken pipe",
    "unexpected eof",
    "temporary failure",
    "timeout",
    "throttl",
    "503",
    "500",
    "internal error",
    "econnreset",
    "unavailable",
    "verification stream length",
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


class InvalidLogicalPath(ValueError):
    """A caller supplied a path that cannot be a Vault-relative logical path.

    This remains a ``ValueError`` subclass for direct-call compatibility, but
    the API maps this dedicated domain exception rather than catching every
    ``ValueError`` raised by application code.
    """

    message_key = "api.invalid_path"

    def __init__(self) -> None:
        super().__init__("Invalid path")


class OperationCancelled(RuntimeError):
    """Raised when a queued or active operation is deliberately interrupted."""


class JobLeaseLost(OperationCancelled):
    """The worker no longer owns the durable claim required to publish work."""


def _claim_expiry_at(timestamp: str | None = None) -> str:
    """Build an ISO deadline using the same UTC representation as Job rows."""
    base = datetime.fromisoformat(timestamp or now_iso())
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base + timedelta(seconds=JOB_CLAIM_LEASE_SECONDS)).isoformat()


def _claim_token_for(job_id: int) -> str | None:
    active = getattr(_worker_claim, "active", None)
    if not active:
        return None
    active_id, token = active
    return str(token) if int(active_id) == int(job_id) else None


def _claim_lost_event_for(job_id: int) -> threading.Event | None:
    active = getattr(_worker_claim, "active", None)
    lost = getattr(_worker_claim, "lost_event", None)
    if not active or not isinstance(lost, threading.Event):
        return None
    return lost if int(active[0]) == int(job_id) else None


def _claim_is_lost(job_id: int) -> bool:
    event = _claim_lost_event_for(job_id)
    return bool(event and event.is_set())


def _mark_claim_lost(job_id: int) -> None:
    event = _claim_lost_event_for(job_id)
    if event is not None:
        event.set()


@contextmanager
def _job_claim_context(
    job: dict[str, Any],
    *,
    lost_event: threading.Event | None = None,
):
    """Make a scheduler-acquired claim and its loss signal available to helpers."""
    token = job.get("claim_token")
    if not token:
        yield
        return
    previous_active = getattr(_worker_claim, "active", None)
    previous_lost = getattr(_worker_claim, "lost_event", None)
    _worker_claim.active = (int(job["id"]), str(token))
    _worker_claim.lost_event = lost_event or threading.Event()
    try:
        yield
    finally:
        if previous_active is None:
            for attribute in ("active", "lost_event"):
                try:
                    delattr(_worker_claim, attribute)
                except AttributeError:
                    pass
        else:
            _worker_claim.active = previous_active
            _worker_claim.lost_event = previous_lost


def _renew_claim_if_owned(job_id: int, claim_token: str) -> dict[str, Any] | None:
    timestamp = now_iso()
    with db() as connection:
        return ArchiveCatalog(connection).renew_job_claim(
            job_id=int(job_id),
            claim_token=claim_token,
            now=timestamp,
            claim_expires_at=_claim_expiry_at(timestamp),
        )


class _ClaimLeaseHeartbeat:
    """Renew a claimed Job while CPU/provider work has no progress callback."""

    def __init__(self, job: dict[str, Any]) -> None:
        self.job_id = int(job["id"])
        self.claim_token = str(job["claim_token"]) if job.get("claim_token") else None
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_ClaimLeaseHeartbeat":
        if self.claim_token:
            self._thread = threading.Thread(
                target=self._run,
                name=f"job-lease-{self.job_id}",
                daemon=True,
            )
            self._thread.start()
        return self

    def _run(self) -> None:
        # Use a fraction of the lease rather than a fixed setting so tests and
        # future policy changes cannot silently make a live transfer reclaimable.
        interval = max(0.1, JOB_CLAIM_LEASE_SECONDS / 3)
        while not self._stop.wait(interval):
            try:
                if _renew_claim_if_owned(self.job_id, str(self.claim_token)) is None:
                    self.lost.set()
                    return
            except Exception:
                # The foreground worker observes this shared signal at its next
                # checkpoint and fences all post-provider catalog/audit writes.
                self.lost.set()
                return

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)


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
    """Stop work when local cancellation, DB cancellation, or lease loss wins.

    ``cancel_jobs`` is a fast same-process signal.  The durable claim check is
    also required because another application process may cancel or reclaim the
    row while this worker is inside a long-running operation.
    """
    if job_cancelled(job_id):
        raise OperationCancelled(message)
    if _claim_is_lost(job_id):
        raise JobLeaseLost(message)
    claim_token = _claim_token_for(job_id)
    if claim_token and _renew_claim_if_owned(job_id, claim_token) is None:
        _mark_claim_lost(job_id)
        raise JobLeaseLost(message)


def ensure_job_claim_owned_in_transaction(
    connection: Any,
    job: dict[str, Any],
    message: str,
) -> None:
    """Fence a catalog/audit transaction behind the currently owned lease.

    The conditional renewal holds the Job row lock until the surrounding
    transaction commits.  A cancellation/takeover that won first therefore
    prevents every post-provider catalog or audit write in that transaction.
    """
    job_id = int(job["id"])
    if _claim_is_lost(job_id):
        raise JobLeaseLost(message)
    claim_token = job.get("claim_token")
    if not claim_token:
        return
    timestamp = now_iso()
    current = ArchiveCatalog(connection).renew_job_claim(
        job_id=job_id,
        claim_token=str(claim_token),
        now=timestamp,
        claim_expires_at=_claim_expiry_at(timestamp),
    )
    if current is None:
        _mark_claim_lost(job_id)
        raise JobLeaseLost(message)


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

    def __init__(self, job: dict[str, Any]) -> None:
        self.job_id = int(job["id"])
        # Boto3 invokes callbacks on its own threads, outside the worker-local
        # claim context.  Carry the token explicitly so those callbacks cannot
        # refresh or mutate a cancelled/reclaimed Job.
        self.claim_token = str(job["claim_token"]) if job.get("claim_token") else None
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
            set_job_progress(
                self.job_id,
                value,
                claim_token=self.claim_token,
            )

    def flush(self) -> None:
        with self._lock:
            value = self.transferred
            self._last_report = time.monotonic()
        set_job_progress(
            self.job_id,
            value,
            claim_token=self.claim_token,
        )


def safe_relative_path(value: str) -> PurePosixPath:
    """Normalize one non-empty, traversal-safe Vault-relative logical path."""
    if not isinstance(value, str) or "\x00" in value:
        raise InvalidLogicalPath()
    normalized = value.replace("\\", "/")
    # PurePosixPath intentionally does not treat a Windows drive prefix as an
    # absolute path. Reject it explicitly because callers may submit paths
    # produced on another platform.
    if re.match(r"^[A-Za-z]:/", normalized):
        raise InvalidLogicalPath()
    candidate = PurePosixPath(normalized)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise InvalidLogicalPath()
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


_UNSET_DECODED_PATH = object()


def _cloud_relative_key(key: str, prefix_value: str) -> str | None:
    prefix = f"{prefix_value.strip('/')}/" if prefix_value.strip('/') else ""
    if prefix and not key.startswith(prefix):
        return None
    relative = key[len(prefix):]
    if not relative or relative.endswith('/'):
        return None
    return relative


def object_key_to_path(
    key: str,
    prefix_value: str,
    is_crypt: bool,
    *,
    encrypted_names: bool = False,
    runtime: Any | None = None,
    decoded_relative_path: str | None | object = _UNSET_DECODED_PATH,
) -> str | None:
    relative = _cloud_relative_key(key, prefix_value)
    if relative is None:
        return None
    if encrypted_names:
        if runtime is None:
            return None
        if decoded_relative_path is _UNSET_DECODED_PATH:
            try:
                relative = decode_object_relative_path(runtime, relative)
            except RuntimeError:
                return None
        elif decoded_relative_path is None:
            # The batch decoder deliberately makes one bad key unknown rather
            # than allowing a partial/ambiguous plaintext path into the catalog.
            return None
        else:
            relative = str(decoded_relative_path)
    elif is_crypt:
        if not relative.endswith('.bin'):
            return None
        relative = relative[:-4]
    if not relative or relative.endswith('/'):
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
    finalize_missing: bool = True,
    scan_started_at: str | None = None,
    scan_journal: dict[str, Any] | None = None,
) -> tuple[int, str | None]:
    """Observe a root with short catalog-write transactions.

    Directory traversal and metadata reads happen before each bounded write
    batch.  The caller can defer the missing-copy transition until it has
    proved that the complete scan still owns the enrolled root.
    """
    vault_id = int(vault["id"])
    count = 0
    scan_started_at = scan_started_at or scan_id
    pending: list[tuple[str, str, int | None, int | None, str]] = []

    def _persisted_scan(vault_row: dict[str, Any]) -> bool:
        return "root_identity" in vault_row or "root_identity_version" in vault_row

    def flush() -> None:
        nonlocal count
        if not pending:
            return
        if _persisted_scan(vault):
            _current_verified_scan_row(connection, vault)
        if scan_journal is not None:
            entries = scan_journal.setdefault("entries", {})
            for relative, _file_type, _size, _mtime_ns, _observed_at in pending:
                if relative in entries:
                    continue
                entries[relative] = connection.execute(
                    """
                    SELECT vf.id AS vault_file_id,
                           vf.status AS vault_file_status,
                           vf.retired_at AS vault_file_retired_at,
                           lc.presence, lc.file_type, lc.size, lc.mtime_ns,
                           lc.plaintext_sha256, lc.matched_archive_version_id,
                           lc.last_seen_at, lc.observed_at
                    FROM vault_files vf
                    JOIN file_paths fp
                      ON fp.vault_file_id=vf.id
                     AND fp.vault_id=vf.vault_id
                     AND fp.path=%s
                     AND fp.valid_to IS NULL
                    LEFT JOIN local_copies lc ON lc.vault_file_id=vf.id
                    WHERE vf.vault_id=%s
                    """,
                    (relative, vault_id),
                ).fetchone()
        catalog = ArchiveCatalog(connection)
        for relative, file_type, size, mtime_ns, observed_at in pending:
            catalog.observe_local_copy(
                vault_id=vault_id,
                path=relative,
                file_type=file_type,
                size=size,
                mtime_ns=mtime_ns,
                seen_at=scan_id,
                observed_at=observed_at,
            )
        count += len(pending)
        pending.clear()
        if _persisted_scan(vault):
            _current_verified_scan_row(connection, vault)
        if allow_chunk_commits:
            connection.commit()

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
        pending.append(
            (
                relative,
                file_type,
                entry_stat.st_size,
                entry_stat.st_mtime_ns,
                now_iso(),
            )
        )
        if len(pending) >= LOCAL_SCAN_WRITE_BATCH_SIZE:
            flush()
    flush()

    if _persisted_scan(vault):
        _current_verified_scan_row(connection, vault)
    access = source_layout.vault_local_access(vault["source_root"])
    alias = access.volume_alias
    safe_scan_result = access.local_operations_allowed or access.volume_health == "scan_required"
    if finalize_missing and safe_scan_result and (
        alias is None or source_layout.should_emit_local_copy_removals(alias)
    ):
        ArchiveCatalog(connection).mark_unseen_local_copies_missing(
            vault_id=vault_id,
            seen_at=scan_id,
            observed_at=now_iso(),
            scan_started_at=scan_started_at,
        )
        if allow_chunk_commits:
            connection.commit()
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
    _finalize_missing: bool = True,
    _scan_journal: dict[str, Any] | None = None,
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
            _connection,
            vault,
            scan_id,
            root=root,
            allow_chunk_commits=True,
            finalize_missing=_finalize_missing,
            scan_started_at=scan_id,
            scan_journal=_scan_journal,
        )
        return count
    with db() as connection:
        count, completed_alias = _scan_tree(
            connection,
            vault,
            scan_id,
            root=root,
            allow_chunk_commits=True,
            finalize_missing=_finalize_missing,
            scan_started_at=scan_id,
            scan_journal=_scan_journal,
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
    if vault_decommission_service.local_work_suspended(vault):
        raise RuntimeError("Source watcher suspended for Vault decommission")
    with db() as connection:
        state = connection.execute(
            """
            SELECT relocation_state, decommission_state
            FROM vaults WHERE id=%s
            """,
            (vault["id"],),
        ).fetchone()
    if state and state["relocation_state"] != "ready":
        raise RuntimeError("Source watcher suspended for Vault relocation")
    if state and state["decommission_state"] != "active":
        raise RuntimeError("Source watcher suspended for Vault decommission")
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
    raw_versions: list[dict[str, Any]] = []
    raw_markers: list[dict[str, Any]] = []

    # Complete the provider listing before decoding. This lets one bounded
    # decoder batch reuse a ciphertext name across every historical Version and
    # Delete Marker, including entries split across paginator pages.
    for page in paginator.paginate(**kwargs):
        raw_versions.extend(page.get("Versions", []))
        raw_markers.extend(page.get("DeleteMarkers", []))

    decoded_paths: dict[str, str] = {}
    decode_failures: set[str] = set()
    runtime: Any | None = None
    if encrypts_names:
        with vault_rclone_config(vault) as runtime_config:
            runtime = runtime_config
            encrypted_relatives = [
                relative
                for item in [*raw_versions, *raw_markers]
                for relative in [_cloud_relative_key(item["Key"], prefix)]
                if relative is not None
            ]
            decoded_paths, decode_failures = decode_object_relative_paths(
                runtime_config,
                encrypted_relatives,
            )

    versions: list[tuple[str, str, dict[str, Any]]] = []
    markers: list[tuple[str, str, dict[str, Any]]] = []

    def _convert(
        items: list[dict[str, Any]],
        destination: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        for item in items:
            decode = decoded_paths.get(_cloud_relative_key(item["Key"], prefix))
            logical_path = object_key_to_path(
                item["Key"],
                prefix,
                is_crypt,
                encrypted_names=encrypts_names,
                runtime=runtime,
                decoded_relative_path=decode if encrypts_names else _UNSET_DECODED_PATH,
            )
            if logical_path is not None:
                destination.append((logical_path, item["VersionId"], item))

    _convert(raw_versions, versions)
    _convert(raw_markers, markers)

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
        # An unknown encrypted key is not evidence that its prior catalog rows
        # disappeared. Fail closed by retaining their last known availability.
        if not decode_failures:
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
        SELECT source_root, relocation_state, root_identity_version, root_identity,
               decommission_state
        FROM vaults WHERE id=%s
        """,
        (int(vault["id"]),),
    ).fetchone()
    if not isinstance(current, dict) or current.get("source_root") != vault.get("source_root"):
        raise _ScanRootMismatch("Vault root changed during local scan")
    if current.get("decommission_state", "active") != "active":
        raise _ScanRootMismatch("Vault was quiesced during local scan")
    try:
        _validate_enrolled_scan_root(current)
    except Exception as exc:
        raise _ScanRootMismatch(str(exc)) from exc
    return current


def _scan_generation_is_current(vault_id: int, scan_id: str) -> bool:
    """Reject a completion path superseded by a newer in-process scan."""
    with status_lock:
        status = runtime_status.get(int(vault_id))
        if not status or "scan_id" not in status:
            # Direct unit-test callers do not enter through scan_vault and have
            # no runtime generation to compare.
            return True
        return status.get("scan_id") == scan_id


def _unfingerprinted_local_rows(vault_id: int) -> list[dict[str, Any]]:
    with db() as connection:
        return connection.execute(
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
            (vault_id,),
        ).fetchall()


def _collect_local_fingerprint_updates(
    vault: dict[str, Any], root: Path
) -> list[dict[str, Any]]:
    """Hash Local Copies without holding a database write transaction."""
    updates: list[dict[str, Any]] = []
    for row in _unfingerprinted_local_rows(int(vault["id"])):
        try:
            local_path = safe_local_path(str(root), row["path"])
            if not local_path.is_file():
                continue
            digest, stat_result = hash_stable_regular_file(local_path)
        except (OSError, ValueError, RuntimeError):
            continue
        updates.append(
            {
                "vault_file_id": row["vault_file_id"],
                "path": row["path"],
                "old_size": row["size"],
                "old_mtime_ns": row["mtime_ns"],
                "size": stat_result.st_size,
                "mtime_ns": stat_result.st_mtime_ns,
                "digest": digest,
                "observed_at": now_iso(),
            }
        )
    return updates


def _apply_local_fingerprint_updates(
    vault: dict[str, Any],
    scan_id: str,
    updates: list[dict[str, Any]],
) -> None:
    """Publish hashed observations in bounded compare-and-swap batches."""
    persisted = "root_identity" in vault or "root_identity_version" in vault
    for offset in range(0, len(updates), LOCAL_SCAN_WRITE_BATCH_SIZE):
        batch = updates[offset : offset + LOCAL_SCAN_WRITE_BATCH_SIZE]
        with db() as connection:
            if persisted:
                _current_verified_scan_row(connection, vault)
                if not _scan_generation_is_current(int(vault["id"]), scan_id):
                    raise _ScanRootMismatch("Local scan was superseded")
            for update in batch:
                # A watcher may have replaced the file after hashing. The
                # predicate prevents a stale digest from overwriting it.
                connection.execute(
                    """
                    UPDATE local_copies
                    SET size=%s,
                        mtime_ns=%s,
                        plaintext_sha256=%s,
                        matched_archive_version_id=NULL,
                        last_seen_at=%s,
                        observed_at=%s
                    WHERE vault_file_id=%s
                      AND presence='present'
                      AND file_type='regular'
                      AND plaintext_sha256 IS NULL
                      AND (size=%s OR (size IS NULL AND %s IS NULL))
                      AND (mtime_ns=%s OR (mtime_ns IS NULL AND %s IS NULL))
                    """,
                    (
                        update["size"],
                        update["mtime_ns"],
                        update["digest"],
                        scan_id,
                        update["observed_at"],
                        update["vault_file_id"],
                        update["old_size"],
                        update["old_size"],
                        update["old_mtime_ns"],
                        update["old_mtime_ns"],
                    ),
                )
            if persisted:
                _current_verified_scan_row(connection, vault)


def _rollback_local_scan_journal(
    vault_id: int, scan_id: str, journal: dict[str, Any]
) -> None:
    """Compensate observations committed before a scan lost its proof.

    The compare-and-swap predicate leaves a newer watcher observation alone. A
    newly-created scan-only Vault File is removed only when no cloud Version or
    Job has attached to it in the meantime.
    """
    entries = journal.get("entries") or {}
    if not entries:
        return
    try:
        with db() as connection:
            for path, snapshot in entries.items():
                if snapshot is None:
                    current = connection.execute(
                        """
                        SELECT vf.id AS vault_file_id
                        FROM vault_files vf
                        JOIN file_paths fp
                          ON fp.vault_file_id=vf.id
                         AND fp.vault_id=vf.vault_id
                         AND fp.path=%s
                         AND fp.valid_to IS NULL
                        JOIN local_copies lc ON lc.vault_file_id=vf.id
                        WHERE vf.vault_id=%s
                          AND lc.last_seen_at=%s
                          AND NOT EXISTS (
                              SELECT 1 FROM archive_versions av
                              WHERE av.vault_file_id=vf.id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM jobs j WHERE j.vault_file_id=vf.id
                          )
                        """,
                        (path, vault_id, scan_id),
                    ).fetchone()
                    if current is None:
                        continue
                    file_id = current["vault_file_id"]
                    connection.execute(
                        "DELETE FROM local_copies WHERE vault_file_id=%s AND last_seen_at=%s",
                        (file_id, scan_id),
                    )
                    connection.execute(
                        """
                        DELETE FROM file_paths
                        WHERE vault_file_id=%s AND vault_id=%s
                          AND path=%s AND valid_to IS NULL
                        """,
                        (file_id, vault_id, path),
                    )
                    connection.execute(
                        "DELETE FROM vault_files WHERE id=%s AND vault_id=%s",
                        (file_id, vault_id),
                    )
                    continue

                file_id = snapshot["vault_file_id"]
                if snapshot.get("presence") is None:
                    connection.execute(
                        """
                        DELETE FROM local_copies
                        WHERE vault_file_id=%s AND last_seen_at=%s
                        """,
                        (file_id, scan_id),
                    )
                    continue
                connection.execute(
                    """
                    UPDATE local_copies
                    SET presence=%s,
                        file_type=%s,
                        size=%s,
                        mtime_ns=%s,
                        plaintext_sha256=%s,
                        matched_archive_version_id=%s,
                        last_seen_at=%s,
                        observed_at=%s
                    WHERE vault_file_id=%s AND last_seen_at=%s
                    """,
                    (
                        snapshot["presence"],
                        snapshot["file_type"],
                        snapshot["size"],
                        snapshot["mtime_ns"],
                        snapshot["plaintext_sha256"],
                        snapshot["matched_archive_version_id"],
                        snapshot["last_seen_at"],
                        snapshot["observed_at"],
                        file_id,
                        scan_id,
                    ),
                )
    except Exception:
        # Compensation is best effort. The scan is still reported as blocked,
        # and no missing transition or relocation completion can commit.
        return


def _verified_local_scan(
    vault: dict[str, Any], scan_id: str
) -> tuple[int, dict[str, int], str | None]:
    """Walk/hash outside writes, then publish bounded catalog stages.

    Local observations are committed in short batches so ordinary SQLite
    writers are not held behind directory traversal or hashing.  Missing
    transitions, rename decisions, and relocation recovery remain in one final
    transaction guarded by the enrolled root and scan generation.
    """
    # Minimal in-memory Vaults are a longstanding unit-test seam. Persisted
    # rows always contain identity columns and always use the staged path.
    if "root_identity" not in vault and "root_identity_version" not in vault:
        return scan_tree(vault, scan_id), apply_auto_renames(vault), None

    access = source_layout.vault_local_access(vault["source_root"])
    if not access.local_operations_allowed and access.volume_health != "scan_required":
        raise RuntimeError(
            f"Local scan blocked by Source Volume health: {access.volume_health}"
        )
    journal_key = (int(vault["id"]), scan_id)
    scan_journal: dict[str, Any] = {"entries": {}}
    _active_scan_journals[journal_key] = scan_journal
    with _pinned_verified_scan_root(vault) as pinned_root:
        # The walk itself uses short catalog batches. Keeping this connection
        # open preserves the old rollback seam when a test or an early failure
        # writes directly before the final identity check; normal batches
        # commit inside _scan_tree and release SQLite's write lock.
        with db() as connection:
            _current_verified_scan_row(connection, vault)
            if not _scan_generation_is_current(int(vault["id"]), scan_id):
                raise _ScanRootMismatch("Local scan was superseded")
            count = scan_tree(
                vault,
                scan_id,
                _connection=connection,
                _root=pinned_root,
                _finalize_missing=False,
                _scan_journal=scan_journal,
            )
            _current_verified_scan_row(connection, vault)

        # Hashing happens with no write transaction open. Publishing uses
        # compare-and-swap predicates so watcher replacements win safely.
        updates = _collect_local_fingerprint_updates(vault, pinned_root)
        _apply_local_fingerprint_updates(vault, scan_id, updates)

        alias = access.volume_alias
        completed_alias = (
            alias
            if alias and source_layout.requires_full_local_scan(alias)
            else None
        )
        with db() as connection:
            _current_verified_scan_row(connection, vault)
            if not _scan_generation_is_current(int(vault["id"]), scan_id):
                raise _ScanRootMismatch("Local scan was superseded")
            rename_summary = _apply_auto_renames(
                connection,
                vault,
                root=pinned_root,
                hash_missing=False,
            )
            rename_summary["hashed"] = len(updates)
            _current_verified_scan_row(connection, vault)
            final_access = source_layout.vault_local_access(vault["source_root"])
            safe_scan_result = (
                final_access.local_operations_allowed
                or final_access.volume_health == "scan_required"
            )
            if safe_scan_result and (
                final_access.volume_alias is None
                or source_layout.should_emit_local_copy_removals(
                    final_access.volume_alias
                )
            ):
                ArchiveCatalog(connection).mark_unseen_local_copies_missing(
                    vault_id=int(vault["id"]),
                    seen_at=scan_id,
                    observed_at=now_iso(),
                    scan_started_at=scan_id,
                )
            _current_verified_scan_row(connection, vault)
            was_relocation_scan = (
                connection.execute(
                    "SELECT relocation_state FROM vaults WHERE id=%s",
                    (int(vault["id"]),),
                ).fetchone()["relocation_state"]
                == "scan_required"
            )
            if was_relocation_scan:
                vault_relocation.complete_relocation_scan(
                    connection, int(vault["id"]), release_runtime=False
                )
            current = _current_verified_scan_row(connection, vault)
        # Runtime gates are delayed until the final transaction committed.
        if was_relocation_scan:
            vault_relocation.release_relocation_scan_runtime(int(vault["id"]))
        if completed_alias:
            source_layout.note_full_local_scan_completed(completed_alias)
        _active_scan_journals.pop(journal_key, None)
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
        if vault_decommission_service.local_work_suspended(vault):
            return {"suspended": 1}
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

        scan_id = now_iso()
        with status_lock:
            status.update(scanning=True, last_error=None, scan_id=scan_id)
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
                journal = _active_scan_journals.pop(
                    (int(vault["id"]), scan_id), None
                )
                if journal is not None:
                    _rollback_local_scan_journal(
                        int(vault["id"]), scan_id, journal
                    )
                local_scan_identity_valid = False
                status["last_error"] = f"Source scan blocked: {exc}"
                result["local"] = -1
                result["root_identity_mismatch"] = 1
            except Exception as exc:
                journal = _active_scan_journals.pop(
                    (int(vault["id"]), scan_id), None
                )
                if journal is not None:
                    _rollback_local_scan_journal(
                        int(vault["id"]), scan_id, journal
                    )
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
    hash_missing: bool = True,
) -> dict[str, int]:
    """Apply rename analysis inside the caller-owned transaction.

    ``hash_missing`` is false for the normal full-scan path: all filesystem
    hashing has already completed outside this transaction and its digests were
    published through compare-and-swap batches.
    """
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
    if hash_missing:
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
            try:
                local_path = safe_local_path(str(root), row["path"])
                if not local_path.is_file():
                    continue
                digest, stat_result = hash_stable_regular_file(local_path)
            except (OSError, ValueError, RuntimeError):
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
                vault_id=vault["id"],
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
    """Hash outside writes, then confirm renames atomically."""
    root = _root or Path(vault["source_root"]).resolve()
    if _connection is not None:
        # Preserve the caller-owned transaction seam used by direct catalog
        # callers and the relocation rollback tests.
        return _apply_auto_renames(
            _connection, vault, root=root, requested_by=requested_by
        )
    scan_id = now_iso()
    updates = _collect_local_fingerprint_updates(vault, root)
    _apply_local_fingerprint_updates(vault, scan_id, updates)
    with db() as connection:
        summary = _apply_auto_renames(
            connection,
            vault,
            root=root,
            requested_by=requested_by,
            hash_missing=False,
        )
    summary["hashed"] = len(updates)
    return summary


def _rclone_command(
    command_args: tuple[str, ...],
    *,
    config_path: str | None,
    bwlimit: str | None,
) -> list[str]:
    command = ["rclone", "--config", config_path or settings.rclone_config]
    if bwlimit:
        command.extend(["--bwlimit", bwlimit])
    command.extend(command_args)
    return command


def _sanitize_rclone_diagnostics(value: str) -> str:
    """Keep subprocess diagnostics useful without echoing config credentials."""
    redacted = re.sub(
        r"(?i)(password2?|secret[_-]?access[_-]?key|access[_-]?key[_-]?id|session[_-]?token|authorization)"
        r"\s*([:=])\s*[^\s,;]+",
        r"\1\2[REDACTED]",
        value,
    )
    return redacted[-1500:].strip() or "Rclone error"


def run_rclone(
    *args: str | Callable[[int, int | None], None],
    job_id: int | None = None,
    config_path: str | None = None,
    bwlimit: str | None = None,
) -> None:
    progress_callback = args[-1] if args and callable(args[-1]) else None
    command_args = args[:-1] if progress_callback else args
    command = _rclone_command(
        tuple(str(arg) for arg in command_args),
        config_path=config_path,
        bwlimit=bwlimit,
    )
    if progress_callback is None:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=None)
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "Rclone error").strip()
            raise RuntimeError(_sanitize_rclone_diagnostics(message))
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
                if active_operation_processes.get(job_id) is process:
                    active_operation_processes.pop(job_id, None)
    if job_id is not None and job_cancelled(job_id):
        raise OperationCancelled("Operation stopped")
    if return_code != 0:
        raise RuntimeError(
            _sanitize_rclone_diagnostics("\n".join(output_tail))
            if output_tail
            else "Rclone error"
        )


def run_rclone_stream(
    *args: str,
    on_chunk: Callable[[bytes], None] | None = None,
    progress_callback: Callable[[int, int | None], None] | None = None,
    job_id: int | None = None,
    config_path: str | None = None,
    bwlimit: str | None = None,
) -> int:
    """Run Rclone with binary stdout and consume it without a local artifact.

    Rclone's ``cat`` output is the plaintext boundary for both plain and Crypt
    remotes.  A selector drains stdout and stderr together, so a noisy provider
    cannot fill stderr while the verifier is reading content.  The child is
    terminated, escalated to SIGKILL when necessary, and reaped on every exit
    path.  ``on_chunk`` therefore receives bounded binary chunks only.
    """
    if on_chunk is None:
        raise ValueError("A binary stdout callback is required")
    if job_id is not None:
        ensure_job_active(job_id, "Operation stopped")

    command = _rclone_command(
        tuple(str(arg) for arg in args),
        config_path=config_path,
        bwlimit=bwlimit,
    )
    command.extend(["--stats=500ms", "--use-json-log", "--stats-log-level=NOTICE"])
    process: subprocess.Popen[Any] | None = None
    selector: selectors.BaseSelector | None = None
    stdout = None
    stderr = None
    stderr_tail = bytearray()
    stderr_pending = bytearray()
    bytes_read = 0
    reported_progress = -1
    abort_error: BaseException | None = None
    terminate_at: float | None = None
    last_claim_check = 0.0

    def request_abort(error: BaseException) -> None:
        nonlocal abort_error, terminate_at
        if abort_error is None:
            abort_error = error
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            if terminate_at is None:
                terminate_at = time.monotonic() + RCLONE_PROCESS_TERMINATION_GRACE_SECONDS

    def cancellation_error() -> BaseException | None:
        if job_id is None:
            return None
        if job_cancelled(job_id):
            return OperationCancelled("Operation stopped")
        if _claim_is_lost(job_id):
            return JobLeaseLost("Operation claim was lost")
        return None

    def report_progress(value: int, total: int | None) -> None:
        nonlocal reported_progress
        if progress_callback is None:
            return
        reported_progress = max(reported_progress, int(value))
        progress_callback(reported_progress, total)

    def consume_stderr_line(line: bytes) -> None:
        text = line.decode("utf-8", errors="replace").strip().lstrip("\r")
        if not text:
            return
        progress = parse_rclone_progress(text)
        if progress is not None:
            report_progress(*progress)

    def consume_stderr(data: bytes) -> None:
        if data:
            stderr_tail.extend(data)
            if len(stderr_tail) > RCLONE_STDERR_TAIL_BYTES:
                del stderr_tail[:-RCLONE_STDERR_TAIL_BYTES]
            stderr_pending.extend(data)
        while True:
            try:
                newline = stderr_pending.index(10)
            except ValueError:
                break
            line = bytes(stderr_pending[:newline])
            del stderr_pending[: newline + 1]
            consume_stderr_line(line)
        # A broken or malicious child must not make the line parser unbounded.
        while len(stderr_pending) > RCLONE_STDERR_LINE_BYTES:
            line = bytes(stderr_pending[:RCLONE_STDERR_LINE_BYTES])
            del stderr_pending[:RCLONE_STDERR_LINE_BYTES]
            consume_stderr_line(line)

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
        )
        if job_id is not None:
            with operation_process_lock:
                active_operation_processes[job_id] = process
                should_terminate = job_id in cancelled_jobs
            if should_terminate:
                request_abort(OperationCancelled("Operation stopped"))

        stdout = process.stdout
        stderr = process.stderr
        if stdout is None or stderr is None:
            raise RuntimeError("Rclone did not expose binary output pipes")
        selector = selectors.DefaultSelector()
        selector.register(stdout, selectors.EVENT_READ, "stdout")
        selector.register(stderr, selectors.EVENT_READ, "stderr")
        last_claim_check = time.monotonic()

        while selector.get_map():
            now = time.monotonic()
            if abort_error is None:
                current_error = cancellation_error()
                if current_error is not None:
                    request_abort(current_error)
                elif (
                    job_id is not None
                    and now - last_claim_check >= max(0.25, JOB_CLAIM_LEASE_SECONDS / 6)
                ):
                    try:
                        ensure_job_active(job_id, "Operation claim was lost")
                    except BaseException as exc:
                        request_abort(exc)
                    last_claim_check = now
            elif terminate_at is not None and now >= terminate_at and process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
                terminate_at = None

            events = selector.select(0.1)
            for key, _ in events:
                stream = key.fileobj
                try:
                    data = os.read(stream.fileno(), RCLONE_STREAM_CHUNK_BYTES)
                except BlockingIOError:
                    continue
                except OSError:
                    try:
                        selector.unregister(stream)
                    except Exception:
                        pass
                    request_abort(RuntimeError("Rclone stream read failed"))
                    continue
                if not data:
                    try:
                        selector.unregister(stream)
                    except Exception:
                        pass
                    continue
                if key.data == "stderr":
                    try:
                        consume_stderr(data)
                    except BaseException as exc:
                        request_abort(exc)
                    continue
                if abort_error is not None:
                    continue
                bytes_read += len(data)
                try:
                    on_chunk(data)
                    report_progress(bytes_read, None)
                except BaseException as exc:
                    request_abort(exc)

        if stderr_pending:
            consume_stderr_line(bytes(stderr_pending))
        if process.poll() is None:
            # A child with closed pipes is still not allowed to escape unreaped.
            request_abort(RuntimeError("Rclone stream ended before the process exited"))
            try:
                process.wait(timeout=RCLONE_PROCESS_TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
        else:
            process.wait()
        return_code = int(process.returncode or 0)
        if abort_error is None:
            current_error = cancellation_error()
            if current_error is not None:
                abort_error = current_error
        if abort_error is not None:
            raise abort_error
        if return_code != 0:
            message = _sanitize_rclone_diagnostics(
                stderr_tail.decode("utf-8", errors="replace")
            )
            raise RuntimeError(f"Rclone stream failed: {message}")
        return bytes_read
    except BaseException as exc:
        if abort_error is None:
            abort_error = exc
        raise
    finally:
        if selector is not None:
            try:
                selector.close()
            except Exception:
                pass
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=RCLONE_PROCESS_TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait()
                except Exception:
                    pass
        for stream in (stdout, stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if job_id is not None and process is not None:
            with operation_process_lock:
                if active_operation_processes.get(job_id) is process:
                    active_operation_processes.pop(job_id, None)


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


def _claim_write_succeeded(result: Any) -> bool:
    """Normalize real cursors and lightweight unit-test recording seams."""
    rowcount = getattr(result, "rowcount", None)
    return rowcount is None or rowcount != 0


def set_job(
    job_id: int,
    status: str,
    message: str = "",
    *,
    message_key: str | None = None,
    message_params: dict[str, Any] | None = None,
) -> bool:
    """Persist a state transition without letting a cancelled/lost claim win.

    Worker calls inherit their token from ``_job_claim_context``.  A status
    change then becomes a compare-and-swap: a concurrent cancellation or a new
    claimant makes this a harmless no-op instead of resurrecting the Job.
    Direct administrative/test callers without a claim retain the historic
    update behavior.
    """
    params = message_params or {}
    if message_key and not message:
        message = translate(message_key, locale=DEFAULT_LOCALE, **params)
    timestamp = now_iso()
    if _claim_is_lost(job_id):
        return False
    claim_token = _claim_token_for(job_id)
    held = status in JOB_LEASE_HELD_STATUSES
    with db() as connection:
        if claim_token:
            lease_set = (
                "claim_expires_at=%s"
                if held
                else "claim_token=NULL, claimed_at=NULL, claim_expires_at=NULL"
            )
            lease_params: list[Any] = (
                [_claim_expiry_at(timestamp)] if held else []
            )
            result = connection.execute(
                f"""
                UPDATE jobs
                SET status=%s,
                    message=%s,
                    message_key=%s,
                    message_params=%s,
                    updated_at=%s,
                    {lease_set}
                WHERE id=%s
                  AND claim_token=%s
                  AND claim_expires_at IS NOT NULL
                  AND claim_expires_at > %s
                  AND status NOT IN ('completed', 'failed', 'cancelled')
                """,
                (
                    status,
                    message,
                    message_key,
                    format_message_params(params) if message_key else None,
                    timestamp,
                    *lease_params,
                    job_id,
                    claim_token,
                    timestamp,
                ),
            )
        else:
            result = connection.execute(
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
                    timestamp,
                    job_id,
                ),
            )
        updated = _claim_write_succeeded(result)
        if updated and status in {"completed", "failed"}:
            notification_service.enqueue_job_terminal_notification_best_effort(
                connection, job_id=job_id
            )
    return updated


def schedule_upload_retry(
    job_id: int,
    *,
    message: str = "",
    message_key: str | None = None,
    message_params: dict[str, Any] | None = None,
    retry_count: int,
    delay_seconds: int | None = None,
) -> bool:
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
    timestamp = now_iso()
    if _claim_is_lost(job_id):
        return False
    claim_token = _claim_token_for(job_id)
    with db() as connection:
        if claim_token:
            result = connection.execute(
                """
                UPDATE jobs
                SET status='retrying',
                    message=%s,
                    message_key=%s,
                    message_params=%s,
                    retry_count=%s,
                    retry_after=%s,
                    updated_at=%s,
                    claim_token=NULL,
                    claimed_at=NULL,
                    claim_expires_at=NULL
                WHERE id=%s
                  AND claim_token=%s
                  AND claim_expires_at IS NOT NULL
                  AND claim_expires_at > %s
                  AND status NOT IN ('completed', 'failed', 'cancelled')
                """,
                (
                    message,
                    message_key,
                    format_message_params(params) if message_key else None,
                    retry_count,
                    retry_after,
                    timestamp,
                    job_id,
                    claim_token,
                    timestamp,
                ),
            )
        else:
            result = connection.execute(
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
                    timestamp,
                    job_id,
                ),
            )
    return _claim_write_succeeded(result)


def set_job_progress(
    job_id: int,
    transferred_bytes: int,
    *,
    claim_token: str | None = None,
) -> bool:
    timestamp = now_iso()
    if _claim_is_lost(job_id):
        return False
    claim_token = claim_token or _claim_token_for(job_id)
    with db() as connection:
        if claim_token:
            result = connection.execute(
                """
                UPDATE jobs
                SET transferred_bytes=%s,
                    updated_at=%s,
                    claim_expires_at=%s
                WHERE id=%s
                  AND claim_token=%s
                  AND claim_expires_at IS NOT NULL
                  AND claim_expires_at > %s
                  AND status NOT IN ('completed', 'failed', 'cancelled')
                """,
                (
                    max(0, transferred_bytes),
                    timestamp,
                    _claim_expiry_at(timestamp),
                    job_id,
                    claim_token,
                    timestamp,
                ),
            )
        else:
            result = connection.execute(
                """
                UPDATE jobs SET transferred_bytes=%s, updated_at=%s WHERE id=%s
                """,
                (max(0, transferred_bytes), timestamp, job_id),
            )
    return _claim_write_succeeded(result)


def _remove_abandoned_restore_files(target: Path) -> None:
    """Remove only application-owned recovery files left by a stopped worker."""
    try:
        for entry in target.parent.iterdir():
            if RESTORE_TEMPORARY_RE.fullmatch(entry.name) is None:
                continue
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
            """
            SELECT source_root FROM vaults
            WHERE enabled=TRUE AND decommission_state='active'
            """
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


def _reconcile_claim_is_stale_sql() -> str:
    """Predicate that lets restart repair legacy or expired claims, never live ones."""
    return "(claim_token IS NULL OR claim_expires_at IS NULL OR claim_expires_at <= %s)"


def _reconcile_job_transition(
    connection: Any,
    *,
    job_id: int,
    status: str,
    message: str,
    timestamp: str,
    reset_progress: bool = False,
    complete_progress: bool = False,
) -> bool:
    """Publish one restart decision only if no worker renewed the old lease."""
    progress_sql = ""
    if reset_progress:
        progress_sql = ", transferred_bytes=0"
    elif complete_progress:
        progress_sql = ", transferred_bytes=total_bytes"
    result = connection.execute(
        f"""
        UPDATE jobs SET status='{status}'{progress_sql},
            message=%s, updated_at=%s,
            claim_token=NULL, claimed_at=NULL, claim_expires_at=NULL
        WHERE {_reconcile_claim_is_stale_sql()} AND id=%s
          AND status NOT IN ('completed', 'failed', 'cancelled')
        """,
        (message, timestamp, timestamp, job_id),
    )
    return _claim_write_succeeded(result)


def _object_version_entries(
    client: Any,
    *,
    bucket: str,
    object_key: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Read exact-version postconditions without treating a retry as evidence.

    S3's list API is used rather than a current-key HEAD because a delete marker
    and a noncurrent Archive Version are both meaningful durable outcomes.
    """
    kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": object_key}
    versions: dict[str, dict[str, Any]] = {}
    markers: dict[str, dict[str, Any]] = {}
    while True:
        page = client.list_object_versions(**kwargs)
        for item in page.get("Versions") or []:
            if item.get("Key") == object_key and item.get("VersionId"):
                versions[str(item["VersionId"])] = item
        for item in page.get("DeleteMarkers") or []:
            if item.get("Key") == object_key and item.get("VersionId"):
                markers[str(item["VersionId"])] = item
        if not page.get("IsTruncated"):
            break
        next_key = page.get("NextKeyMarker")
        next_version = page.get("NextVersionIdMarker")
        if not next_key:
            raise RuntimeError("S3 version listing omitted its continuation marker")
        kwargs["KeyMarker"] = next_key
        if next_version:
            kwargs["VersionIdMarker"] = next_version
    return versions, markers


def _storage_head_matches_catalog(
    version: dict[str, Any],
    head: dict[str, Any],
    *,
    target_class: str,
) -> bool:
    """Require provider proof for a catalogued storage-class destination."""
    if not head.get("VersionId") or str(head["VersionId"]) != str(
        version.get("provider_version_id")
    ):
        return False
    if (head.get("StorageClass") or "STANDARD").upper() != target_class:
        return False
    if (
        version.get("size") is not None
        and head.get("ContentLength") is not None
        and int(head["ContentLength"]) != int(version["size"])
    ):
        return False
    expected_etag = str(version.get("etag") or "").strip('"')
    observed_etag = str(head.get("ETag") or "").strip('"')
    return not expected_etag or not observed_etag or expected_etag == observed_etag


def _fail_storage_class_reconciliation(
    connection: Any,
    job: dict[str, Any],
    *,
    timestamp: str,
    message: str,
) -> str:
    """Fail closed rather than issue a second storage-class copy."""
    if _reconcile_job_transition(
        connection,
        job_id=int(job["id"]),
        status="failed",
        message=message,
        timestamp=timestamp,
    ):
        notification_service.enqueue_job_terminal_notification_best_effort(
            connection, job_id=int(job["id"])
        )
        return "failed"
    return "skipped"


def _reconcile_recover_job(
    connection: Any,
    job: dict[str, Any],
    *,
    target_path: Path,
    target: dict[str, Any] | None,
    timestamp: str,
) -> str:
    """Reconcile a recovery destination without ever deleting its final path."""

    def transition(status: str, message: str, *, complete: bool = False) -> str:
        changed = _reconcile_job_transition(
            connection,
            job_id=int(job["id"]),
            status=status,
            message=message,
            timestamp=timestamp,
            reset_progress=status == "queued",
            complete_progress=complete,
        )
        if not changed:
            return "skipped"
        if status in {"completed", "failed"}:
            notification_service.enqueue_job_terminal_notification_best_effort(
                connection, job_id=int(job["id"])
            )
        return "requeued" if status == "queued" else status

    # An absent destination is the only state where retrying is safe.  Any
    # existing final entry, including a directory or symlink, is preserved.
    if not os.path.lexists(target_path):
        return transition(
            "queued",
            "Recovery interrupted by restart; destination is absent and recovery will retry",
        )

    if target_path.is_symlink() or not target_path.is_file():
        return transition(
            "failed",
            "Recovery destination conflict preserved after restart; the final path is not a regular file",
        )

    expected_digest = (target or {}).get("version_sha256") or job.get(
        "version_sha256"
    )
    archive_version_id = (target or {}).get("archive_version_id") or job.get(
        "archive_version_id"
    )
    if not expected_digest or not archive_version_id:
        return transition(
            "failed",
            "Recovery destination preserved after restart; its Archive Version digest could not be verified",
        )
    expected_digest = str(expected_digest).lower()
    if len(expected_digest) != 64:
        return transition(
            "failed",
            "Recovery destination preserved after restart; its Archive Version digest is invalid",
        )
    try:
        int(expected_digest, 16)
        recovered_digest, recovered_stat = hash_stable_regular_file(target_path)
    except (OSError, ValueError, RuntimeError) as exc:
        return transition(
            "failed",
            f"Recovery destination conflict preserved after restart; digest verification failed: {exc}",
        )

    if recovered_digest != expected_digest:
        return transition(
            "failed",
            "Recovery destination conflict preserved after restart; its digest does not match the Archive Version",
        )

    # Claim the terminal Job before publishing the Local Copy in this same
    # transaction.  A live worker that renews the lease wins the compare-and-
    # swap and this transaction commits no catalog observation.
    outcome = transition(
        "completed",
        "Recovered Local Copy verified and adopted after restart",
        complete=True,
    )
    if outcome != "completed":
        return outcome
    catalog = ArchiveCatalog(connection)
    catalog.observe_local_copy(
        vault_id=int(job["vault_id"]),
        path=str(job["path"]),
        file_type="regular",
        size=recovered_stat.st_size,
        mtime_ns=recovered_stat.st_mtime_ns,
        observed_at=timestamp,
        seen_at=timestamp,
    )
    catalog.set_local_fingerprint(
        vault_id=int(job["vault_id"]),
        path=str(job["path"]),
        plaintext_sha256=expected_digest,
        matched_archive_version_id=str(archive_version_id),
    )
    return outcome


def _reconcile_storage_class_job(
    connection: Any,
    job: dict[str, Any],
    *,
    timestamp: str,
) -> str:
    """Reconcile copy postconditions without ever issuing a second copy.

    ``CopyObject`` creates a new current S3 VersionId before the worker can
    persist it.  A current-key HEAD can reveal a possible destination but cannot
    prove provenance if that VersionId was never catalogued.  Only a catalogued
    exact VersionId that still HEADs as the requested class is safe to complete;
    every other interrupted outcome is held for explicit operator recovery.
    """
    target = (job.get("target_storage_class") or "").upper()
    version = connection.execute(
        """
        SELECT object_key, provider_version_id, storage_class, size, etag
        FROM archive_versions WHERE id=%s
        """,
        (job.get("archive_version_id"),),
    ).fetchone()
    if not target or not version or not version.get("object_key"):
        return _fail_storage_class_reconciliation(
            connection,
            job,
            timestamp=timestamp,
            message=(
                "Storage class change interrupted with no verifiable target; "
                "manual review is required and no automatic retry was scheduled"
            ),
        )

    try:
        client = s3_client()
        if (version.get("storage_class") or "").upper() == target:
            head = client.head_object(
                Bucket=job["s3_bucket"],
                Key=version["object_key"],
                VersionId=version["provider_version_id"],
            )
            if _storage_head_matches_catalog(version, head, target_class=target):
                if _reconcile_job_transition(
                    connection,
                    job_id=int(job["id"]),
                    status="completed",
                    message="Storage class change completed before restart",
                    timestamp=timestamp,
                    complete_progress=True,
                ):
                    notification_service.enqueue_job_terminal_notification_best_effort(
                        connection, job_id=int(job["id"])
                    )
                    return "completed"
                return "skipped"
            return _fail_storage_class_reconciliation(
                connection,
                job,
                timestamp=timestamp,
                message=(
                    "Catalogued storage-class destination no longer matches the "
                    "provider; manual review is required and no automatic retry "
                    "was scheduled"
                ),
            )

        # A noncatalogued current target may be the copied VersionId or a
        # concurrent write.  Record neither as ours and never repeat CopyObject.
        current = client.head_object(
            Bucket=job["s3_bucket"],
            Key=version["object_key"],
        )
        observed_version = current.get("VersionId")
        observed_class = (current.get("StorageClass") or "STANDARD").upper()
        detail = (
            "an unrecorded destination VersionId was observed"
            if observed_version and observed_class == target
            else "the provider could not prove the copy outcome"
        )
    except Exception:
        detail = "the provider postcondition could not be verified"
    return _fail_storage_class_reconciliation(
        connection,
        job,
        timestamp=timestamp,
        message=(
            "Storage class change interrupted: "
            f"{detail}; manual review is required and no automatic retry was scheduled"
        ),
    )


def _reconcile_cloud_archive_job(
    connection: Any,
    job: dict[str, Any],
    *,
    timestamp: str,
) -> str:
    """Complete a hidden key from catalog/provider proof; never create a second marker blindly."""
    version = connection.execute(
        """
        SELECT object_key FROM archive_versions
        WHERE id=%s
        """,
        (job.get("archive_version_id"),),
    ).fetchone()
    if not version or not version.get("object_key"):
        raise RuntimeError("Cloud archive target is missing")
    object_key = str(version["object_key"])
    marker = connection.execute(
        """
        SELECT provider_version_id
        FROM delete_markers
        WHERE vault_file_id=%s AND object_key=%s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (job["vault_file_id"], object_key),
    ).fetchone()
    if marker is None:
        _versions, markers = _object_version_entries(
            s3_client(),
            bucket=str(job["s3_bucket"]),
            object_key=object_key,
        )
        latest = next(
            (item for item in markers.values() if item.get("IsLatest")),
            None,
        )
        if latest is not None:
            modified = latest.get("LastModified")
            observed_at = modified.isoformat() if hasattr(modified, "isoformat") else timestamp
            ArchiveCatalog(connection).record_delete_marker(
                vault_id=int(job["vault_id"]),
                path=str(job["path"]),
                object_key=object_key,
                provider_version_id=str(latest["VersionId"]),
                created_at=observed_at,
                observed_at=timestamp,
            )
            marker = latest
    if marker is not None:
        if _reconcile_job_transition(
            connection,
            job_id=int(job["id"]),
            status="completed",
            message="Cloud archival completed before restart",
            timestamp=timestamp,
        ):
            notification_service.enqueue_job_terminal_notification_best_effort(
                connection, job_id=int(job["id"])
            )
            return "completed"
        return "skipped"
    # Listing proved that no Delete Marker was written, so retrying the
    # reversible operation is safe.  Provider uncertainty instead fails closed.
    return (
        "requeued"
        if _reconcile_job_transition(
            connection,
            job_id=int(job["id"]),
            status="queued",
            message="Cloud archival interrupted before its Delete Marker; safely resumed",
            timestamp=timestamp,
        )
        else "skipped"
    )


def _reconcile_cloud_purge_group(
    connection: Any,
    lead: dict[str, Any],
    *,
    timestamp: str,
) -> dict[str, int]:
    """Reconcile exact-version purge items before allowing a group to resume."""
    group_id = lead.get("group_id")
    if not group_id:
        raise RuntimeError("Cloud purge is missing its group identity")
    group_rows = connection.execute(
        """
        SELECT * FROM jobs
        WHERE vault_id=%s AND group_id=%s AND action='cloud-purge'
        ORDER BY requested_at ASC, id ASC
        """,
        (lead["vault_id"], group_id),
    ).fetchall()
    active_rows = [
        row for row in group_rows if row["status"] not in JOB_TERMINAL_STATUSES
    ]
    # Reconciliation must never split a group around another worker's live
    # claim.  A partially published prior reconciliation may leave some rows
    # queued; those are harmless as long as every active member is unleased.
    if (
        not active_rows
        or any(
            row.get("claim_token")
            and row.get("claim_expires_at")
            and str(row["claim_expires_at"]) > timestamp
            for row in active_rows
        )
        or any(row["status"] not in {"cleaning", "queued"} for row in active_rows)
    ):
        return {"completed": 0, "requeued": 0, "failed": 0}
    jobs = [row for row in active_rows if row["status"] == "cleaning"]
    if not jobs:
        return {"completed": 0, "requeued": 0, "failed": 0}
    job_ids = [int(job["id"]) for job in jobs]
    placeholders = ", ".join(["%s"] * len(job_ids))
    items = connection.execute(
        f"""
        SELECT * FROM cloud_deletion_items
        WHERE job_id IN ({placeholders})
          AND status IN ('pending', 'failed')
        ORDER BY id
        """,
        job_ids,
    ).fetchall()
    if any(item["status"] == "failed" for item in items):
        outcome = "failed"
        message = "Permanent purge had recorded failures before restart"
    else:
        remaining = list(items)
        cached: dict[str, tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]] = {}
        deleted_ids: list[int] = []
        for item in remaining:
            key = str(item["object_key"])
            if key not in cached:
                cached[key] = _object_version_entries(
                    s3_client(),
                    bucket=str(lead["s3_bucket"]),
                    object_key=key,
                )
            versions, markers = cached[key]
            source = versions if item["kind"] == "version" else markers
            if str(item["provider_version_id"]) not in source:
                deleted_ids.append(int(item["id"]))
        if deleted_ids:
            cloud_deletion_service.mark_items_deleted(
                connection,
                item_ids=deleted_ids,
                updated_at=timestamp,
            )
        still_pending = len(remaining) - len(deleted_ids)
        outcome = "requeued"
        message = (
            "Permanent purge deletions reconciled after restart; finalizing"
            if still_pending == 0
            else "Permanent purge interrupted before all exact versions were deleted; safely resumed"
        )
    result = {"completed": 0, "requeued": 0, "failed": 0}
    for job in jobs:
        if _reconcile_job_transition(
            connection,
            job_id=int(job["id"]),
            status="failed" if outcome == "failed" else "queued",
            message=message,
            timestamp=timestamp,
        ):
            if outcome == "failed":
                notification_service.enqueue_job_terminal_notification_best_effort(
                    connection, job_id=int(job["id"])
                )
            result[outcome] += 1
    return result


def reconcile_interrupted_jobs() -> dict[str, int]:
    """Reconcile only legacy/expired durable worker states after a restart.

    A live lease is authoritative even when another process is restarting.  The
    explicit action/state matrix below covers every operation state which can be
    persisted mid-I/O; waiting states (retrying, pending approval/delay, and
    Glacier restoring) remain scheduler-owned and are not reset.
    """
    summary = {"completed": 0, "requeued": 0, "failed": 0}
    local_actions = {"recover", "upload", "rename", "free-space"}
    seen_purge_groups: set[tuple[int, str]] = set()
    with db() as connection:
        timestamp = now_iso()
        jobs = connection.execute(
            """
            SELECT j.*, v.source_root, v.s3_bucket
            FROM jobs j
            JOIN vaults v ON v.id=j.vault_id
            WHERE (
                    (j.action='recover' AND j.status IN ('downloading', 'verifying'))
                 OR (j.action='upload' AND j.status IN ('uploading', 'verifying'))
                 OR (j.action='rename' AND j.status IN ('uploading', 'verifying', 'cleaning'))
                 OR (j.action='free-space' AND j.status='cleaning')
                 OR (j.action='storage-class' AND j.status='uploading')
                 OR (j.action='cloud-archive' AND j.status='cleaning')
                 OR (j.action='cloud-purge' AND j.status='cleaning')
            )
              AND (j.claim_token IS NULL OR j.claim_expires_at IS NULL
                   OR j.claim_expires_at <= %s)
            ORDER BY j.requested_at ASC, j.id ASC
            """,
            (timestamp,),
        ).fetchall()

        for job in jobs:
            # A cloud-purge group must be reconciled as one operation, not once
            # per member, otherwise one restart could split its exact-version
            # postconditions across competing retries.
            if job["action"] == "cloud-purge":
                group_key = (int(job["vault_id"]), str(job.get("group_id") or ""))
                if group_key in seen_purge_groups:
                    continue
                seen_purge_groups.add(group_key)
                try:
                    result = _reconcile_cloud_purge_group(
                        connection, job, timestamp=timestamp
                    )
                    for key, value in result.items():
                        summary[key] += value
                except Exception as exc:
                    # A provider postcondition we cannot prove is never retried
                    # blindly.  Mark every stale group member terminal instead.
                    group_rows = connection.execute(
                        """
                        SELECT id FROM jobs
                        WHERE vault_id=%s AND group_id=%s AND action='cloud-purge'
                          AND status='cleaning'
                          AND (claim_token IS NULL OR claim_expires_at IS NULL
                               OR claim_expires_at <= %s)
                        """,
                        (job["vault_id"], job.get("group_id"), timestamp),
                    ).fetchall()
                    for row in group_rows:
                        if _reconcile_job_transition(
                            connection,
                            job_id=int(row["id"]),
                            status="failed",
                            message=f"Post-restart reconciliation failed: {exc}",
                            timestamp=timestamp,
                        ):
                            notification_service.enqueue_job_terminal_notification_best_effort(
                                connection, job_id=int(row["id"])
                            )
                            summary["failed"] += 1
                continue

            if job["action"] in local_actions:
                access = source_layout.vault_local_access(job["source_root"])
                if not access.local_operations_allowed:
                    # Keep interrupted local work suspended without touching the
                    # absent, inaccessible, or replaced tree.
                    continue
            try:
                if job["action"] == "recover":
                    source_root = Path(job["source_root"])
                    if not source_root.is_dir():
                        raise RuntimeError("Source folder is unavailable")
                    target_path = safe_local_entry_path(
                        job["source_root"], job["path"]
                    )
                    _remove_abandoned_restore_files(target_path)
                    recovery_target = ArchiveCatalog(connection).get_job_target(
                        int(job["id"])
                    )
                    outcome = _reconcile_recover_job(
                        connection,
                        job,
                        target_path=target_path,
                        target=recovery_target,
                        timestamp=timestamp,
                    )
                elif job["action"] == "free-space":
                    source_root = Path(job["source_root"])
                    if not source_root.is_dir():
                        raise RuntimeError("Source folder is unavailable")
                    target = safe_local_entry_path(job["source_root"], job["path"])
                    if os.path.lexists(target):
                        outcome = (
                            "requeued"
                            if _reconcile_job_transition(
                                connection,
                                job_id=int(job["id"]),
                                status="queued",
                                message="Operation interrupted by restart; automatically resumed",
                                timestamp=timestamp,
                                reset_progress=True,
                            )
                            else "skipped"
                        )
                    else:
                        claim_restored = False
                        surviving_claims: list[Path] = []
                        for claim in sorted(target.parent.glob(f".{target.name}.cleanup-*.tmp")):
                            if CLEANUP_TEMPORARY_RE.fullmatch(claim.name) is None:
                                continue
                            surviving_claims.append(claim)
                            if restore_claimed_local_copy(claim, target) and os.path.lexists(target):
                                claim_restored = True
                                break
                        if claim_restored:
                            outcome = (
                                "requeued"
                                if _reconcile_job_transition(
                                    connection,
                                    job_id=int(job["id"]),
                                    status="queued",
                                    message="Cleanup claim restored after restart; free-space resumed",
                                    timestamp=timestamp,
                                    reset_progress=True,
                                )
                                else "skipped"
                            )
                        else:
                            remaining_claims = [claim for claim in surviving_claims if claim.exists()]
                            if remaining_claims:
                                message = (
                                    "Cleanup claim could not be restored after restart; "
                                    f"original content was preserved at {remaining_claims[0]}"
                                )
                                outcome = "failed"
                            else:
                                ArchiveCatalog(connection).mark_local_copy_missing(
                                    job["vault_file_id"], observed_at=timestamp
                                )
                                message = "Local space freed (reconciled after restart)"
                                outcome = "completed"
                            changed = _reconcile_job_transition(
                                connection,
                                job_id=int(job["id"]),
                                status=outcome,
                                message=message,
                                timestamp=timestamp,
                                complete_progress=outcome == "completed",
                            )
                            if changed and outcome in {"completed", "failed"}:
                                notification_service.enqueue_job_terminal_notification_best_effort(
                                    connection, job_id=int(job["id"])
                                )
                            if not changed:
                                outcome = "skipped"
                elif job["action"] == "storage-class":
                    outcome = _reconcile_storage_class_job(
                        connection, job, timestamp=timestamp
                    )
                elif job["action"] == "cloud-archive":
                    outcome = _reconcile_cloud_archive_job(
                        connection, job, timestamp=timestamp
                    )
                else:
                    outcome = (
                        "requeued"
                        if _reconcile_job_transition(
                            connection,
                            job_id=int(job["id"]),
                            status="queued",
                            message="Operation interrupted by restart; automatically resumed",
                            timestamp=timestamp,
                            reset_progress=True,
                        )
                        else "skipped"
                    )
                if outcome in summary:
                    summary[outcome] += 1
            except Exception as exc:
                if _reconcile_job_transition(
                    connection,
                    job_id=int(job["id"]),
                    status="failed",
                    message=f"Post-restart reconciliation failed: {exc}",
                    timestamp=timestamp,
                ):
                    notification_service.enqueue_job_terminal_notification_best_effort(
                        connection, job_id=int(job["id"])
                    )
                    summary["failed"] += 1
    return summary


def job_progress_callback(
    job: dict[str, Any],
    *,
    minimum_bytes: int = 0,
) -> Callable[[int, int | None], None]:
    total_bytes = int(job.get("total_bytes") or 0)
    # Upload verification is a second read phase of the same Job. Its stream
    # counters must never reset the already-reported upload progress to zero.
    floor = max(0, int(minimum_bytes))
    total_bytes = max(total_bytes, floor)
    interval_ms = int(_runtime_settings().job_progress_min_interval_ms or 500)
    min_interval = max(0.05, interval_ms / 1000.0)
    state = {"last_at": 0.0, "last_value": floor - 1}
    lock = threading.Lock()

    def update(transferred_bytes: int, _: int | None = None) -> None:
        # Rclone reports absolute transferred counters.
        value = max(floor, int(transferred_bytes))
        if total_bytes:
            value = min(value, total_bytes)
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
            set_job_progress(
                job["id"],
                value,
                claim_token=(
                    str(job["claim_token"])
                    if job.get("claim_token")
                    else None
                ),
            )

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
    """Materialize a rename verification copy (upload verification is streamed)."""
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


def _verification_version_head(
    client: Any,
    job: dict[str, Any],
    target: dict[str, Any],
) -> dict[str, Any]:
    """Reconfirm the exact provider VersionId around a streamed read."""
    object_key = str(target.get("object_key") or "")
    expected_version = str(target.get("provider_version_id") or "")
    if not object_key or not expected_version:
        raise RuntimeError("Upload verification target has no exact S3 VersionId")
    head = client.head_object(Bucket=job["s3_bucket"], Key=object_key)
    observed_version = head.get("VersionId")
    if not observed_version:
        raise RuntimeError("Upload verification found no S3 VersionId")
    if str(observed_version) != expected_version:
        raise RuntimeError("Archive Version changed during upload verification")
    expected_size = target.get("cloud_size")
    observed_size = head.get("ContentLength")
    if expected_size is not None and observed_size is not None:
        if int(observed_size) != int(expected_size):
            raise RuntimeError("Archive Version size changed during upload verification")
    return head


def _stream_plaintext_for_verification(
    job: dict[str, Any],
    *,
    target: dict[str, Any],
    source_size: int,
) -> tuple[str, int]:
    """Hash decrypted Rclone output incrementally without touching the Vault root."""
    digest = hashlib.sha256()
    bytes_read = 0

    def consume(chunk: bytes) -> None:
        nonlocal bytes_read
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise RuntimeError("Rclone returned a non-binary verification chunk")
        bytes_read += len(chunk)
        digest.update(chunk)

    progress = job_progress_callback(
        job,
        minimum_bytes=max(int(job.get("total_bytes") or 0), int(source_size)),
    )
    if vault_encrypts_names(job):
        with vault_rclone_config(job) as runtime:
            run_rclone_stream(
                "cat",
                f"{runtime.remote_name}:{job['path']}",
                *rclone_download_perf_args(),
                on_chunk=consume,
                progress_callback=progress,
                job_id=job["id"],
                config_path=str(runtime.path),
                bwlimit=job_bwlimit(job),
            )
    else:
        logical_path = object_key_to_path(
            str(target.get("object_key") or ""),
            job.get("s3_prefix") or "",
            is_crypt=vault_encrypts_content(job),
        ) or job["path"]
        run_rclone_stream(
            "cat",
            configured_rclone_destination(job, logical_path),
            *rclone_download_perf_args(),
            on_chunk=consume,
            progress_callback=progress,
            job_id=job["id"],
            bwlimit=job_bwlimit(job),
        )
    ensure_job_active(job["id"], "Upload stopped")
    return digest.hexdigest(), bytes_read


def _record_verification_failure(reason: str) -> None:
    """Record only bounded, non-sensitive verification failure categories."""
    try:
        metrics_service.inc("verification_failures_total", reason=reason)
    except Exception:
        # Metrics are observability only and must never change Job durability.
        pass


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
        linked_target = ArchiveCatalog(connection).get_job_target(job["id"])
    policy_id = resolve_effective_policy_id(job["path"], assignments)
    verification_started = False
    verification_failure_reason: str | None = None
    try:
        set_job(job["id"], "uploading", message_key="job.hashing_local_file")
        plaintext_sha256, source_stat = hash_stable_regular_file(source)
        ensure_job_active(job["id"], "Upload claim was lost")

        version_id: str
        target: dict[str, Any]
        head: dict[str, Any]
        client = s3_client()
        linked_version_id = linked_target.get("archive_version_id") if linked_target else None
        if linked_version_id:
            # A retry after catalog linkage is verification-only. Uploading
            # again here would create a second provider VersionId and split the
            # durable Archive Version history.
            target = dict(linked_target)
            version_id = str(linked_version_id)
            integrity = str(target.get("integrity") or "unverified")
            if integrity == "mismatch":
                raise RuntimeError("The linked Archive Version already mismatches the Local Copy")
            if target.get("availability") not in {None, "available"}:
                raise RuntimeError("The linked Archive Version is no longer available")
            if integrity == "verified":
                expected_digest = str(target.get("version_sha256") or "").lower()
                if expected_digest != plaintext_sha256:
                    raise RuntimeError(
                        "Local file digest no longer matches the verified Archive Version"
                    )
                _verification_version_head(client, job, target)
                ensure_job_active(job["id"], "Upload claim was lost")
                with db() as connection:
                    ensure_job_claim_owned_in_transaction(
                        connection,
                        job,
                        "Upload claim was lost",
                    )
                    ArchiveCatalog(connection).set_local_fingerprint(
                        vault_id=job["vault_id"],
                        path=job["path"],
                        plaintext_sha256=plaintext_sha256,
                        matched_archive_version_id=version_id,
                    )
                set_job_progress(
                    job["id"], int(job.get("total_bytes") or source_stat.st_size)
                )
                set_job(job["id"], "completed", message_key="job.upload_verified")
                return
            if not target.get("object_key") or not target.get("provider_version_id"):
                raise RuntimeError("The linked Archive Version cannot be verified safely")
            head = _verification_version_head(client, job, target)
        else:
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
            head = client.head_object(Bucket=job["s3_bucket"], Key=key)
            provider_version_id = head.get("VersionId")
            if not provider_version_id:
                raise RuntimeError(
                    "Upload stored without an S3 VersionId; bucket Versioning is required"
                )
            applied_policy_id = None
            if policy_id:
                apply_version_policy_tag(
                    client,
                    bucket=job["s3_bucket"],
                    key=key,
                    version_id=provider_version_id,
                    policy_id=policy_id,
                )
                applied_policy_id = policy_id
            ensure_job_active(job["id"], "Upload claim was lost")
            timestamp = now_iso()
            with db() as connection:
                ensure_job_claim_owned_in_transaction(
                    connection,
                    job,
                    "Upload claim was lost",
                )
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
            target = {
                "archive_version_id": version_id,
                "object_key": key,
                "provider_version_id": provider_version_id,
                "cloud_size": head.get("ContentLength"),
                "integrity": "unverified",
                "availability": "available",
            }

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
        ensure_job_active(job["id"], "Upload claim was lost")
        verification_started = True
        remote_digest, remote_size = _stream_plaintext_for_verification(
            job,
            target=target,
            source_size=source_stat.st_size,
        )
        _verification_version_head(client, job, target)
        # Crypt providers report ciphertext ContentLength, while the stream
        # is decrypted plaintext. Only a plain object can use the catalogued
        # provider length; encrypted modes must be compared with the stable
        # Local Copy size instead.
        expected_stream_size = (
            source_stat.st_size
            if is_crypt
            else target.get("cloud_size")
        )
        if expected_stream_size is not None and remote_size != int(expected_stream_size):
            verification_failure_reason = "truncated"
            raise RuntimeError(
                "Cloud verification stream length did not match the Archive Version"
            )
        after_verify = source.stat(follow_symlinks=False)
        if (
            after_verify.st_size != source_stat.st_size
            or after_verify.st_mtime_ns != source_stat.st_mtime_ns
            or after_verify.st_dev != source_stat.st_dev
            or after_verify.st_ino != source_stat.st_ino
        ):
            verification_failure_reason = "source_changed"
            raise RuntimeError("Local file changed since fingerprinting")
        if remote_digest != plaintext_sha256:
            verification_failure_reason = "mismatch"
            ensure_job_active(job["id"], "Upload claim was lost")
            with db() as connection:
                ensure_job_claim_owned_in_transaction(
                    connection,
                    job,
                    "Upload claim was lost",
                )
                ArchiveCatalog(connection).mark_version_mismatch(
                    version_id,
                    plaintext_sha256=plaintext_sha256,
                    checked_at=now_iso(),
                )
            raise RuntimeError("Cloud copy digest does not match local file")
        ensure_job_active(job["id"], "Upload claim was lost")
        verified_at = now_iso()
        with db() as connection:
            ensure_job_claim_owned_in_transaction(
                connection,
                job,
                "Upload claim was lost",
            )
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
        if verification_started:
            _record_verification_failure(verification_failure_reason or "stream")
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
                ensure_job_claim_owned_in_transaction(
                    connection,
                    job,
                    "Rename claim was lost",
                )
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
            ensure_job_active(job["id"], "Rename claim was lost")
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
            ensure_job_active(job["id"], "Rename claim was lost")
            timestamp = now_iso()
            with db() as connection:
                ensure_job_claim_owned_in_transaction(
                    connection,
                    job,
                    "Rename claim was lost",
                )
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
            ensure_job_active(job["id"], "Rename claim was lost")
            temporary = source.with_name(
                f".{source.name}.verify-{uuid.uuid4().hex}.tmp"
            )
            try:
                _download_plaintext_for_verification(job, temporary=temporary)
                remote_digest, _ = hash_stable_regular_file(temporary)
            finally:
                temporary.unlink(missing_ok=True)
            if remote_digest != plaintext_sha256:
                ensure_job_active(job["id"], "Rename claim was lost")
                with db() as connection:
                    ensure_job_claim_owned_in_transaction(
                        connection,
                        job,
                        "Rename claim was lost",
                    )
                    ArchiveCatalog(connection).mark_version_mismatch(
                        version_id,
                        plaintext_sha256=plaintext_sha256,
                        checked_at=now_iso(),
                    )
                raise RuntimeError("Cloud copy digest does not match local file")
            ensure_job_active(job["id"], "Rename claim was lost")
            verified_at = now_iso()
            with db() as connection:
                ensure_job_claim_owned_in_transaction(
                    connection,
                    job,
                    "Rename claim was lost",
                )
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
        ensure_job_active(job["id"], "Rename claim was lost")
        delete_result = s3_client().delete_object(
            Bucket=job["s3_bucket"],
            Key=old_key,
        )
        marker_version = delete_result.get("VersionId")
        if not marker_version:
            raise RuntimeError(
                "S3 did not return a delete marker VersionId for the previous key"
            )
        ensure_job_active(job["id"], "Rename claim was lost")
        marker_at = now_iso()
        with db() as connection:
            ensure_job_claim_owned_in_transaction(
                connection,
                job,
                "Rename claim was lost",
            )
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
    ensure_job_active(job["id"], "Freeing local space claim was lost")
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

    ensure_job_active(job["id"], "Freeing local space claim was lost")
    try:
        replacement_stat = local_path.lstat()
    except FileNotFoundError:
        replacement_stat = None
    with db() as connection:
        ensure_job_claim_owned_in_transaction(
            connection,
            job,
            "Freeing local space claim was lost",
        )
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
    completed = set_job(job["id"], "completed", message_key="job.local_space_freed")
    if completed:
        record_automatic_cleanup_outcome(
            job,
            event="local_cleanup.completed",
            outcome="success",
            title="Automatic local cleanup completed",
            body=f"The Local Copy of {job['path']} was removed; recovery remains available.",
        )


def _head_object_with_checksum(client: Any, **kwargs: Any) -> dict[str, Any]:
    """Read a version with provider checksum fields when supported.

    Some S3-compatible providers reject ``ChecksumMode``. Falling back to a
    normal HEAD keeps the operation portable; the caller then obtains an
    equivalent proof by hashing the exact VersionId instead of publishing on
    size/ETag metadata alone.
    """
    try:
        return client.head_object(**kwargs, ChecksumMode="ENABLED")
    except Exception:
        return client.head_object(**kwargs)


def _full_object_sha256_checksum(head: dict[str, Any]) -> str | None:
    checksum = head.get("ChecksumSHA256")
    if not checksum:
        return None
    checksum_type = str(head.get("ChecksumType") or "FULL_OBJECT").upper()
    if checksum_type != S3_COPY_CHECKSUM_TYPE:
        return None
    return str(checksum)


def _sha256_s3_version(
    client: Any,
    *,
    bucket: str,
    object_key: str,
    version_id: str,
    job_id: int | None = None,
) -> str:
    """Hash one exact provider VersionId and return its S3 checksum encoding."""
    response = client.get_object(
        Bucket=bucket,
        Key=object_key,
        VersionId=version_id,
    )
    body = response.get("Body") if isinstance(response, dict) else None
    if body is None:
        raise RuntimeError("S3 did not return a body for integrity verification")
    digest = hashlib.sha256()
    try:
        iter_chunks = getattr(body, "iter_chunks", None)
        if callable(iter_chunks):
            chunks = iter_chunks(chunk_size=S3_OBJECT_HASH_CHUNK_BYTES)
        else:
            read = getattr(body, "read", None)
            if not callable(read):
                raise RuntimeError("S3 returned an unreadable body")

            def read_chunks():
                while True:
                    chunk = read(S3_OBJECT_HASH_CHUNK_BYTES)
                    if not chunk:
                        break
                    yield chunk

            chunks = read_chunks()
        for chunk in chunks:
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise RuntimeError("S3 returned a non-binary body")
            digest.update(chunk)
            if job_id is not None:
                ensure_job_active(job_id, "Storage class change stopped")
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    return base64.b64encode(digest.digest()).decode("ascii")


def _source_object_tagging(
    client: Any,
    *,
    bucket: str,
    object_key: str,
    version_id: str,
) -> str | None:
    """Return source Version tags in CreateMultipartUpload's wire format."""
    response = client.get_object_tagging(
        Bucket=bucket,
        Key=object_key,
        VersionId=version_id,
    )
    if not isinstance(response, dict):
        raise RuntimeError("S3 returned an invalid object-tag response")
    tag_set = response.get("TagSet") or []
    if not isinstance(tag_set, (list, tuple)):
        raise RuntimeError("S3 returned an invalid object-tag set")
    tags: list[tuple[str, str]] = []
    for tag in tag_set:
        if not isinstance(tag, dict) or tag.get("Key") is None or tag.get("Value") is None:
            raise RuntimeError("S3 returned an invalid object tag")
        tags.append((str(tag["Key"]), str(tag["Value"])))
    return urlencode(tags) if tags else None


def _multipart_copy_storage_class(
    client: Any,
    *,
    job: dict[str, Any],
    bucket: str,
    object_key: str,
    source_version_id: str,
    target_class: str,
    size_bytes: int,
    source_head: dict[str, Any],
) -> str | None:
    """Copy one exact S3 Version with multipart UploadPartCopy operations."""
    if size_bytes <= S3_SINGLE_COPY_MAX_BYTES:
        raise ValueError("Multipart storage-class copy requires an oversized object")

    part_size = max(
        S3_MULTIPART_COPY_MIN_PART_BYTES,
        S3_MULTIPART_COPY_PART_BYTES,
        (size_bytes + S3_MULTIPART_COPY_MAX_PARTS - 1)
        // S3_MULTIPART_COPY_MAX_PARTS,
    )
    part_count = (size_bytes + part_size - 1) // part_size
    if part_count > S3_MULTIPART_COPY_MAX_PARTS:
        raise RuntimeError("S3 multipart copy would exceed the part limit")

    tagging = _source_object_tagging(
        client,
        bucket=bucket,
        object_key=object_key,
        version_id=source_version_id,
    )
    create_kwargs: dict[str, Any] = {
        "Bucket": bucket,
        "Key": object_key,
        "StorageClass": target_class,
        "ChecksumAlgorithm": S3_COPY_CHECKSUM_ALGORITHM,
        "ChecksumType": S3_COPY_CHECKSUM_TYPE,
    }
    if tagging:
        # UploadPartCopy does not inherit tags from its source Version. The
        # tag set must be supplied when the multipart upload is initiated.
        create_kwargs["Tagging"] = tagging
    # Multipart initiation does not have CopyObject's metadata directives.  Set
    # the source headers that S3 exposes so the new representation does not
    # unexpectedly lose content metadata while its bytes are copied.
    for field in (
        "CacheControl",
        "ContentDisposition",
        "ContentEncoding",
        "ContentLanguage",
        "ContentType",
        "Expires",
        "Metadata",
    ):
        if source_head.get(field) is not None:
            create_kwargs[field] = source_head[field]

    upload_id: str | None = None
    try:
        initiated = client.create_multipart_upload(**create_kwargs)
        upload_id = initiated.get("UploadId")
        if not upload_id:
            raise RuntimeError("S3 did not return a multipart UploadId")

        parts: list[dict[str, Any]] = []
        copy_source = {
            "Bucket": bucket,
            "Key": object_key,
            "VersionId": source_version_id,
        }
        for part_number in range(1, part_count + 1):
            ensure_job_active(job["id"], "Storage class change stopped")
            start = (part_number - 1) * part_size
            end = min(size_bytes, start + part_size) - 1
            result = client.upload_part_copy(
                Bucket=bucket,
                Key=object_key,
                UploadId=upload_id,
                PartNumber=part_number,
                CopySource=copy_source,
                CopySourceRange=f"bytes={start}-{end}",
            )
            copy_result = result.get("CopyPartResult") or {}
            etag = copy_result.get("ETag") or result.get("ETag")
            if not etag:
                raise RuntimeError(
                    f"S3 did not return an ETag for multipart part {part_number}"
                )
            part = {"PartNumber": part_number, "ETag": etag}
            for checksum_name in (
                "ChecksumCRC32",
                "ChecksumCRC32C",
                "ChecksumSHA1",
                "ChecksumSHA256",
                "ChecksumCRC64NVME",
            ):
                checksum = copy_result.get(checksum_name)
                if checksum:
                    part[checksum_name] = checksum
            parts.append(part)

        ensure_job_active(job["id"], "Storage class change stopped")
        completed = client.complete_multipart_upload(
            Bucket=bucket,
            Key=object_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
            ChecksumType=S3_COPY_CHECKSUM_TYPE,
        )
        # Completion makes the upload no longer abortable.  A missing VersionId
        # is handled by the destination read-back, not by guessing the source.
        upload_id = None
        return completed.get("VersionId")
    except BaseException as exc:
        if upload_id:
            try:
                client.abort_multipart_upload(
                    Bucket=bucket,
                    Key=object_key,
                    UploadId=upload_id,
                )
            except Exception as abort_exc:
                raise RuntimeError(
                    "Multipart storage-class copy failed and its upload could not be aborted"
                ) from abort_exc
        raise


def _copy_storage_class_version(
    client: Any,
    *,
    job: dict[str, Any],
    bucket: str,
    object_key: str,
    source_version_id: str,
    target_class: str,
    source_size: int,
    source_head: dict[str, Any],
) -> str | None:
    """Use CopyObject for small objects and exact multipart copy for large ones."""
    if source_size > S3_SINGLE_COPY_MAX_BYTES:
        return _multipart_copy_storage_class(
            client,
            job=job,
            bucket=bucket,
            object_key=object_key,
            source_version_id=source_version_id,
            target_class=target_class,
            size_bytes=source_size,
            source_head=source_head,
        )
    copy_result = client.copy_object(
        Bucket=bucket,
        Key=object_key,
        CopySource={
            "Bucket": bucket,
            "Key": object_key,
            "VersionId": source_version_id,
        },
        StorageClass=target_class,
        MetadataDirective="COPY",
        TaggingDirective="COPY",
        ChecksumAlgorithm=S3_COPY_CHECKSUM_ALGORITHM,
    )
    return copy_result.get("VersionId")


def _verify_storage_class_destination(
    client: Any,
    *,
    bucket: str,
    object_key: str,
    source_version_id: str,
    candidate_version_id: str | None,
    target_class: str,
    expected_size: int,
    expected_sha256_checksum: str | None = None,
    job_id: int | None = None,
) -> tuple[str, str | None]:
    """Read back the exact destination before publishing catalog state.

    Size and ETag are useful metadata checks but are not content proofs (in
    particular, multipart ETags are not object digests). The destination must
    expose the same full-object SHA-256 checksum as the source, or be streamed
    and hashed when the provider does not expose checksum metadata.
    """
    if not expected_sha256_checksum:
        raise RuntimeError("Storage class copy lacks a source integrity proof")
    kwargs: dict[str, Any] = {"Bucket": bucket, "Key": object_key}
    if candidate_version_id:
        kwargs["VersionId"] = candidate_version_id
    destination = _head_object_with_checksum(client, **kwargs)
    observed_version_id = destination.get("VersionId") or candidate_version_id
    if not observed_version_id:
        raise RuntimeError(
            "Storage class copy did not produce a verifiable S3 VersionId"
        )
    if candidate_version_id and str(observed_version_id) != str(candidate_version_id):
        raise RuntimeError(
            "Storage class copy read-back returned a different S3 VersionId"
        )
    if str(observed_version_id) == str(source_version_id):
        raise RuntimeError(
            "Storage class copy read-back still points at the source S3 VersionId"
        )
    if destination.get("DeleteMarker"):
        raise RuntimeError("Storage class copy read-back returned a Delete Marker")
    observed_class = (destination.get("StorageClass") or "STANDARD").upper()
    if observed_class != target_class:
        raise RuntimeError(
            "Storage class copy read-back returned the wrong storage class"
        )
    content_length = destination.get("ContentLength")
    if content_length is None or int(content_length) != int(expected_size):
        raise RuntimeError(
            "Storage class copy read-back returned the wrong object size"
        )
    destination_checksum = _full_object_sha256_checksum(destination)
    if destination_checksum is None:
        destination_checksum = _sha256_s3_version(
            client,
            bucket=bucket,
            object_key=object_key,
            version_id=str(observed_version_id),
            job_id=job_id,
        )
    if destination_checksum != expected_sha256_checksum:
        raise RuntimeError(
            "Storage class copy read-back failed its content-integrity check"
        )
    return str(observed_version_id), (
        str(destination.get("ETag")).strip('"')
        if destination.get("ETag")
        else None
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
    head = _head_object_with_checksum(
        client,
        Bucket=job["s3_bucket"],
        Key=target["object_key"],
        VersionId=target["provider_version_id"],
    )
    if head.get("DeleteMarker"):
        raise RuntimeError("The scheduled Archive Version is a Delete Marker")
    if head.get("VersionId") and str(head["VersionId"]) != str(
        target["provider_version_id"]
    ):
        raise RuntimeError("The provider returned a different source VersionId")
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
            ensure_job_active(job["id"], "Storage class claim was lost")
            checked_at = now_iso()
            with db() as connection:
                ensure_job_claim_owned_in_transaction(
                    connection,
                    job,
                    "Storage class claim was lost",
                )
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
    # The provider's exact-Version HEAD is authoritative for the copy limit;
    # catalog size may be stale after an external rewrite or prior scan.
    source_size = head.get("ContentLength")
    if source_size is None:
        source_size = target.get("cloud_size")
    if source_size is None:
        raise RuntimeError("S3 did not return the Archive Version size")
    try:
        source_size = int(source_size)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("S3 returned an invalid Archive Version size") from exc
    if source_size < 0:
        raise RuntimeError("S3 returned a negative Archive Version size")

    source_checksum = _full_object_sha256_checksum(head)
    if source_checksum is None:
        source_checksum = _sha256_s3_version(
            client,
            bucket=job["s3_bucket"],
            object_key=target["object_key"],
            version_id=str(target["provider_version_id"]),
            job_id=int(job["id"]),
        )
    ensure_job_active(job["id"], "Storage class claim was lost")
    new_version_id = _copy_storage_class_version(
        client,
        job=job,
        bucket=job["s3_bucket"],
        object_key=target["object_key"],
        source_version_id=str(target["provider_version_id"]),
        target_class=target_class,
        source_size=source_size,
        source_head=head,
    )
    ensure_job_active(job["id"], "Storage class change stopped")
    new_version_id, etag = _verify_storage_class_destination(
        client,
        bucket=job["s3_bucket"],
        object_key=target["object_key"],
        source_version_id=str(target["provider_version_id"]),
        candidate_version_id=(str(new_version_id) if new_version_id else None),
        target_class=target_class,
        expected_size=source_size,
        expected_sha256_checksum=source_checksum,
        job_id=int(job["id"]),
    )
    ensure_job_active(job["id"], "Storage class claim was lost")
    timestamp = now_iso()
    with db() as connection:
        ensure_job_claim_owned_in_transaction(
            connection,
            job,
            "Storage class claim was lost",
        )
        ArchiveCatalog(connection).publish_storage_class_copy(
            job_id=int(job["id"]),
            archive_version_id=str(target["archive_version_id"]),
            provider_version_id=new_version_id,
            storage_class=target_class,
            etag=etag,
            observed_at=timestamp,
        )

    # The prior exact VersionId is intentionally retained.  Only the dedicated
    # Cloud Purge workflow may permanently delete provider versions.
    set_job(
        job["id"],
        "completed",
        message_key="job.storage_class_completed",
        message_params={"storage_class": target_class},
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
    reporter = _ThrottledByteProgress(job)

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
                timestamp = now_iso()
                claim_token = _claim_token_for(int(job["id"]))
                predicate = "WHERE id=%s"
                claim_params: list[Any] = []
                if claim_token:
                    predicate += """
                      AND claim_token=%s
                      AND claim_expires_at IS NOT NULL
                      AND claim_expires_at > %s
                      AND status NOT IN ('completed', 'failed', 'cancelled')
                    """
                    claim_params = [claim_token, timestamp]
                connection.execute(
                    f"""
                    UPDATE jobs
                    SET status='pending_approval',
                        message=%s,
                        pending_until=%s,
                        restore_tier=%s,
                        restore_days=%s,
                        estimated_cost_eur=%s,
                        estimated_hours=%s,
                        updated_at=%s,
                        claim_token=NULL,
                        claimed_at=NULL,
                        claim_expires_at=NULL
                    {predicate}
                    """,
                    (
                        "High-impact Glacier restore held for primary-owner approval; "
                        "RestoreObject cannot be cancelled after AWS accepts it",
                        pending_until,
                        estimate.tier,
                        estimate.days,
                        estimate.estimated_cost_eur,
                        estimate.estimated_hours,
                        timestamp,
                        job["id"],
                        *claim_params,
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
        ensure_job_active(job["id"], "Recovery claim was lost")
        checked_at = now_iso()
        with db() as connection:
            ensure_job_claim_owned_in_transaction(
                connection,
                job,
                "Recovery claim was lost",
            )
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

    ensure_job_active(job["id"], "Recovery claim was lost")
    with db() as connection:
        ensure_job_claim_owned_in_transaction(
            connection,
            job,
            "Recovery claim was lost",
        )
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
    ensure_job_active(job["id"], "Cloud archival stopped")
    delete_result = s3_client().delete_object(
        Bucket=job["s3_bucket"],
        Key=object_key,
    )
    marker_version = delete_result.get("VersionId")
    if not marker_version:
        raise RuntimeError("S3 did not return a Delete Marker VersionId")
    # If ownership was lost while DeleteObject was stalled, leave provider
    # evidence for restart/takeover reconciliation; never publish it from the
    # stale worker.
    ensure_job_active(job["id"], "Cloud archival claim was lost")
    stamp = now_iso()
    with db() as connection:
        ensure_job_claim_owned_in_transaction(
            connection,
            job,
            "Cloud archival claim was lost",
        )
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
    """Permanently delete a scheduler-claimed cloud-purge group exactly once."""
    ensure_job_active(job["id"], "Cloud purge stopped")
    claim_token = job.get("claim_token")
    if not claim_token:
        # The scheduler is the only supported entry point for destructive work.
        # Retaining this no-op protects direct legacy callers from bypassing the
        # durable group acquisition primitive.
        return
    if not job.get("cloud_deletion_enabled"):
        raise RuntimeError("Cloud deletion is disabled for this vault")
    validate_cloud_vault(job)
    claimed_at = now_iso()
    with db() as connection:
        purge_jobs, items = ArchiveCatalog(connection).load_claimed_purge_group(
            lead_job_id=int(job["id"]),
            claim_token=str(claim_token),
            now=claimed_at,
        )
    if not purge_jobs:
        return
    expected_job_ids = [int(purge_job["id"]) for purge_job in purge_jobs]
    group_id = job.get("group_id")
    if not group_id:
        raise RuntimeError("Permanent purge is missing its group identity")
    client = s3_client()
    failures = 0
    for offset in range(0, len(items), 1000):
        batch = items[offset : offset + 1000]
        ensure_job_active(job["id"], "Cloud purge stopped")
        stamp = now_iso()
        # Keep all group members leased while a batch is in flight.  If a
        # cancellation or takeover won, do not turn its durable state back into
        # an item result after the provider call.
        with db() as connection:
            if not ArchiveCatalog(connection).renew_purge_group_claim(
                vault_id=int(job["vault_id"]),
                group_id=str(group_id),
                claim_token=str(claim_token),
                expected_job_ids=expected_job_ids,
                now=stamp,
                claim_expires_at=_claim_expiry_at(stamp),
            ):
                _mark_claim_lost(int(job["id"]))
                raise JobLeaseLost("Cloud purge claim was lost")
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
        except Exception as exc:
            response = None
            failed_items = [(int(item["id"]), str(exc)) for item in batch]

        # The lease may expire while DeleteObjects is blocked.  Capture the
        # provider-return time before inspecting its result, then honor the
        # heartbeat's shared loss signal before any durable result is derived.
        provider_returned_at = now_iso()
        if _claim_is_lost(int(job["id"])):
            _mark_claim_lost(int(job["id"]))
            raise JobLeaseLost("Cloud purge claim was lost")

        if response is not None:
            try:
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
        # Result parsing is local-only, but use a second timestamp immediately
        # before the conditional group renewal so an expired lease cannot be
        # revived with the pre-provider fence time.
        fence_at = now_iso()
        if _claim_is_lost(int(job["id"])):
            _mark_claim_lost(int(job["id"]))
            raise JobLeaseLost("Cloud purge claim was lost")
        with db() as connection:
            if not ArchiveCatalog(connection).renew_purge_group_claim(
                vault_id=int(job["vault_id"]),
                group_id=str(group_id),
                claim_token=str(claim_token),
                expected_job_ids=expected_job_ids,
                now=fence_at,
                claim_expires_at=_claim_expiry_at(fence_at),
            ):
                _mark_claim_lost(int(job["id"]))
                raise JobLeaseLost("Cloud purge claim was lost")
            if _claim_is_lost(int(job["id"])):
                _mark_claim_lost(int(job["id"]))
                raise JobLeaseLost("Cloud purge claim was lost")
            cloud_deletion_service.mark_items_deleted(
                connection,
                item_ids=deleted_ids,
                updated_at=provider_returned_at,
            )
            cloud_deletion_service.mark_items_failed(
                connection,
                failures=failed_items,
                updated_at=provider_returned_at,
            )
    stamp = now_iso()
    if _claim_is_lost(int(job["id"])):
        _mark_claim_lost(int(job["id"]))
        raise JobLeaseLost("Cloud purge claim was lost")
    with db() as connection:
        # This conditional UPDATE holds all Jobs' row locks through finalization,
        # so a cancellation either wins before this point or observes terminal
        # Jobs afterward; it can never be overwritten by finalize_purge_job.
        if not ArchiveCatalog(connection).renew_purge_group_claim(
            vault_id=int(job["vault_id"]),
            group_id=str(group_id),
            claim_token=str(claim_token),
            expected_job_ids=expected_job_ids,
            now=stamp,
            claim_expires_at=_claim_expiry_at(stamp),
        ):
            _mark_claim_lost(int(job["id"]))
            raise JobLeaseLost("Cloud purge claim was lost")
        if _claim_is_lost(int(job["id"])):
            _mark_claim_lost(int(job["id"]))
            raise JobLeaseLost("Cloud purge claim was lost")
        for purge_job in purge_jobs:
            cloud_deletion_service.finalize_purge_job(
                connection,
                job_id=int(purge_job["id"]),
                vault_file_id=purge_job["vault_file_id"],
                actor_user_id=purge_job.get("requested_by"),
                updated_at=stamp,
            )
        placeholders = ", ".join(["%s"] * len(expected_job_ids))
        connection.execute(
            f"""
            UPDATE jobs
            SET claim_token=NULL, claimed_at=NULL, claim_expires_at=NULL
            WHERE id IN ({placeholders})
              AND status IN ('completed', 'failed', 'cancelled')
            """,
            expected_job_ids,
        )
    if failures:
        # finalize_purge_job already persisted failed status; avoid double-write
        # through process_job's exception handler.
        return


def _process_claimed_job(job: dict[str, Any]) -> bool:
    """Process one queue item after the scheduler has acquired its lease."""
    try:
        with db() as connection:
            runtime = _runtime_settings(connection)
            claim_token = job.get("claim_token")
            if claim_token:
                current = ArchiveCatalog(connection).renew_job_claim(
                    job_id=int(job["id"]),
                    claim_token=str(claim_token),
                    now=now_iso(),
                    claim_expires_at=_claim_expiry_at(),
                )
            else:
                # Direct unit/service callers retain the historic unclaimed seam;
                # production scheduling always supplies a token.
                current = connection.execute(
                    "SELECT status FROM jobs WHERE id=%s",
                    (job["id"],),
                ).fetchone()
        if current is None:
            return False
        if current["status"] in JOB_TERMINAL_STATUSES:
            return False
        job["status"] = current["status"]
        if current["status"] in {"queued", "retrying", "restoring"}:
            # Drop stale in-memory cancel flags from prior process/test DBs that
            # reused this Job id; a live cancel after this point re-arms the set.
            with operation_process_lock:
                cancelled_jobs.discard(int(job["id"]))
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
                # Release the short scheduler lease while the Source Volume is
                # suspended.  Keeping it would make an unavailable local path
                # look actively executed until expiry on every poll.
                if job.get("claim_token"):
                    set_job(
                        job["id"],
                        "queued",
                        "Source Volume is unavailable; Job remains queued",
                    )
                # Restoring the expected mount is the only path back to local
                # execution; no filesystem action is attempted here.
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
        elif job["action"] == "cloud-purge" and job["status"] == "cleaning":
            process_cloud_purge(job)
        else:
            return False
        return True
    except JobLeaseLost:
        # A different worker/cancellation now owns the durable outcome.  Never
        # write a terminal status, audit, or notification from this stale worker.
        return False
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
        cancelled = set_job(
            job["id"],
            "cancelled",
            message_key=message_keys.get(job["action"], "job.operation_stopped"),
        )
        if cancelled and job["action"] == "free-space":
            record_automatic_cleanup_outcome(
                job,
                event="local_cleanup.cancelled",
                outcome="cancelled",
                title="Automatic local cleanup cancelled",
                body=f"The Local Copy cleanup for {job['path']} was cancelled.",
            )
    except Exception as exc:
        if _claim_is_lost(int(job["id"])):
            return False
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
            return schedule_upload_retry(
                job["id"],
                message_key="job.retrying_source_changed",
                message_params={"seconds": policy.stability_seconds},
                retry_count=next_attempt,
                delay_seconds=policy.stability_seconds,
            )
        if job["action"] == "upload" and failure_kind == "transient":
            next_attempt = int(job.get("retry_count") or 0) + 1
            if next_attempt <= UPLOAD_RETRY_MAX_ATTEMPTS:
                return schedule_upload_retry(
                    job["id"],
                    message_key="job.retrying_transient",
                    message_params={"error": message},
                    retry_count=next_attempt,
                )
        failed = set_job(job["id"], "failed", message)
        if failed and job["action"] == "free-space":
            record_automatic_cleanup_outcome(
                job,
                event="local_cleanup.failed",
                outcome="failure",
                title="Automatic local cleanup failed",
                body=f"The Local Copy cleanup for {job['path']} failed: {message}",
            )
    return True


def process_job(job: dict[str, Any]) -> bool:
    """Run a Job with its durable scheduler claim bound to nested helpers."""
    with _ClaimLeaseHeartbeat(job) as heartbeat:
        with _job_claim_context(job, lost_event=heartbeat.lost):
            return _process_claimed_job(job)


def _restore_due_before(runtime: Any, *, current: datetime | None = None) -> str:
    """Return the oldest ``updated_at`` that may be polled again."""
    try:
        interval = max(0, int(getattr(runtime, "restore_poll_interval", 900)))
    except (TypeError, ValueError):
        interval = 900
    reference = current or datetime.now(timezone.utc)
    return (reference - timedelta(seconds=interval)).isoformat()


def process_jobs_once() -> int:
    """Claim and dispatch one fair, concurrency-bounded scheduler batch.

    Selecting candidates is intentionally separate from acquisition.  Every
    selected row is rechecked by a conditional UPDATE in ``ArchiveCatalog``;
    two processes may read the same queue but only one receives a claim token.
    """
    now = now_iso()
    current = datetime.now(timezone.utc)
    with db() as connection:
        runtime = _runtime_settings(connection)
        restore_due_before = _restore_due_before(runtime, current=current)
        # Over-fetch candidates so fair interleave can still fill the concurrency
        # budget when one Vault dominates the oldest requested_at values.
        batch_size = max(10, int(runtime.operation_concurrency) * 10)
        candidates = ArchiveCatalog(connection).list_claimable_jobs(
            now=now,
            restore_due_before=restore_due_before,
            limit=batch_size,
        )
        policy_cache: dict[int, Any] = {}
        eligible_candidates: list[dict[str, Any]] = []
        seen_purge_groups: set[tuple[int, str]] = set()
        for row in candidates:
            job = dict(row)
            vault_id = int(job["vault_id"])
            if vault_relocation.local_work_suspended({"id": vault_id}):
                continue
            if (
                job.get("origin") != "decommission"
                and vault_decommission_service.local_work_suspended(job)
            ):
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
            if job["action"] == "cloud-purge":
                if not job.get("group_id"):
                    continue
                purge_group = (vault_id, str(job["group_id"]))
                if purge_group in seen_purge_groups:
                    continue
                seen_purge_groups.add(purge_group)
            eligible_candidates.append(job)

    selected = select_fair_jobs(
        eligible_candidates,
        limit=int(runtime.operation_concurrency),
    )
    claimed_jobs: list[dict[str, Any]] = []
    for job in selected:
        claimed_at = now_iso()
        claim_token = uuid.uuid4().hex
        claim_expires_at = _claim_expiry_at(claimed_at)
        with db() as connection:
            catalog = ArchiveCatalog(connection)
            if job["action"] == "cloud-purge":
                group = catalog.claim_purge_group(
                    lead_job_id=int(job["id"]),
                    claim_token=claim_token,
                    claimed_at=claimed_at,
                    claim_expires_at=claim_expires_at,
                    now=claimed_at,
                    message=translate(
                        "job.cloud_purge_deleting", locale=DEFAULT_LOCALE
                    ),
                    message_key="job.cloud_purge_deleting",
                )
                lead = next(
                    (row for row in group if int(row["id"]) == int(job["id"])),
                    None,
                )
                if lead is None:
                    continue
                job.update(lead)
            else:
                claimed = catalog.claim_job(
                    job_id=int(job["id"]),
                    claim_token=claim_token,
                    claimed_at=claimed_at,
                    claim_expires_at=claim_expires_at,
                    now=claimed_at,
                    restore_due_before=_restore_due_before(runtime),
                )
                if claimed is None:
                    continue
                job.update(claimed)
        claimed_jobs.append(job)

    if claimed_jobs:
        worker_count = min(int(runtime.operation_concurrency), len(claimed_jobs))
        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="operation"
        ) as executor:
            list(executor.map(process_job, claimed_jobs))
    return len(claimed_jobs)


def claimable_queue_depth() -> int:
    """Return the actual unleased runnable backlog for the ``queue_depth`` gauge."""
    timestamp = now_iso()
    with db() as connection:
        runtime = _runtime_settings(connection)
        return ArchiveCatalog(connection).claimable_queue_depth(
            now=timestamp,
            restore_due_before=_restore_due_before(runtime),
        )


def scan_all_vaults() -> None:
    with db() as connection:
        vaults = connection.execute(
            """
            SELECT * FROM vaults
            WHERE enabled=TRUE AND decommission_state='active'
            ORDER BY id
            """
        ).fetchall()
    for vault in vaults:
        scan_vault(vault)


def _filesystem_watch_filter(_: Change, path: str) -> bool:
    return not is_restore_temporary_name(Path(path).name)


async def _watch_vault_filesystem(vault: dict[str, Any]) -> None:
    while True:
        if vault_relocation.local_work_suspended(vault):
            await asyncio.sleep(1)
            continue
        if vault_decommission_service.local_work_suspended(vault):
            await asyncio.sleep(1)
            continue
        with db() as connection:
            current = connection.execute(
                """
                SELECT relocation_state, decommission_state
                FROM vaults WHERE id=%s
                """,
                (vault["id"],),
            ).fetchone()
        if current and current["relocation_state"] != "ready":
            await asyncio.sleep(1)
            continue
        if current and current["decommission_state"] != "active":
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
            """
            SELECT * FROM vaults
            WHERE enabled=TRUE AND decommission_state='active'
            ORDER BY id
            """
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
            """
            SELECT * FROM vaults
            WHERE enabled=TRUE AND decommission_state='active'
            ORDER BY id
            """
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
            await asyncio.to_thread(process_jobs_once)
            await asyncio.to_thread(
                vault_decommission_service.reconcile_all,
                local_delete_enabled=runtime.allow_local_delete,
                purge_delay_seconds=runtime.cloud_purge_delay_seconds,
            )
            # Backlog is measured independently from the selected batch: an
            # active lease is no longer claimable, while every remaining due
            # queued row is visible even when concurrency is small.
            queued_count = await asyncio.to_thread(claimable_queue_depth)
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
