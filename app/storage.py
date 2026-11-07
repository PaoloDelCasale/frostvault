from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import random
import stat
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable

import boto3
import psycopg
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from .services.s3_upload import upload_file
from .services.upload_snapshot import (
    UploadSnapshot, prepare_upload_snapshot, validated_s3_proof, UploadProofMismatch,
    unsupported_checksum_api,
)
from watchfiles import Change, awatch

from .config import is_placeholder, settings, settings_snapshot
from .owner import owner_user_id
from .catalog import ArchiveCatalog
from .database import db
from .services import source_layout
from .services import local_observation
from .services.restore_ghosts import retire_absent_restore_ghosts
from .services import vault_relocation
from .services.catalog_event_hub import publish_committed_event
from .services.catalog_events import record_catalog_revision
from .i18n import DEFAULT_LOCALE, format_message_params, parse_message_params, translate
from .services.s3_preflight import check_bucket_readiness, preflight_failure_message
from .services import cloud_preflight_gate
from .services.catalog_audit import audit_vault_catalog
from .services.directory_aggregates import (
    invalidate_for_vault_file,
    mark_path_dirty,
    process_directory_aggregate_maintenance,
)
from .services.lifecycle_policies import sync_lifecycle_rules_for_bucket
from .services import health as health_service
from .services import metadata_backups as metadata_backup_service
from .services import metrics as metrics_service
# Helpers extracted from this module.  Names storage itself no longer calls
# stay importable here because tests and ``app.main`` reach them through
# ``app.storage`` (``patch("app.storage.<name>")`` and direct imports).
from .services.s3_copy import (
    S3_SINGLE_COPY_MAX_BYTES,
    _object_version_entries,
    _delete_s3_version,
    _verification_version_head,
    _head_object_with_checksum,
    _full_object_sha256_checksum,
    _sha256_s3_version,
    _copy_storage_class_version,
    _verify_storage_class_destination,
    restore_header_state,
    storage_class_requires_restore,
)
from .services.vault_paths import (
    InvalidLogicalPath,
    RESTORE_TEMPORARY_RE,
    CLEANUP_TEMPORARY_RE,
    VERIFY_TEMPORARY_RE,
    safe_relative_path,
    safe_local_path,
    safe_local_entry_path,
    object_key_to_path,
    expected_cloud_key,
    is_restore_temporary_name,
)
from .services.database_retry import (
    is_retryable_transaction_error, owned_connection, retry_database_unit,
)
from .services.upload_retry import (
    UPLOAD_RETRY_MAX_ATTEMPTS,
    classify_upload_failure,
    upload_retry_delay_seconds,
)
from .services.runtime_status import (
    runtime_status,
    scan_locks,
    status_lock,
    scan_lock_for_vault,
    snapshot_runtime_status_for_stats,
)
from .services.fs_preflight import PinnedTree, TreeIdentityChanged, inspect_entry
from .services.problems import report_problem
from .services.local_files import (
    hash_stable_regular_file,
    restore_claimed_local_copy,
    _local_stat_unchanged,
    _require_linked_upload_digest,
)


def _runtime_settings():
    return settings_snapshot(settings)
from .services import worker_errors as worker_error_store
from .services import notifications as notification_service
from .services import cloud_deletion as cloud_deletion_service
from .services import vault_moves
from .services import protection_stop as protection_stop_service
from .services.shutdown import (
    WORKER_DRAIN_SECONDS, OperationCancelled, WorkerStopping, shutdown_state,
)
from .services import audit_events as audit_event_store
from .services.cost_estimates import list_prices
from .services.restore_estimates import (
    estimate_restore,
    is_high_impact_restore,
)
from .services.job_scheduler import select_fair_jobs
from .services.operation_policies import (
    get_policy,
    queue_auto_local_cleanups,
    local_cleanup_is_due,
    local_release_is_confirmed,
    queue_auto_uploads,
    auto_upload_path_is_included,
    file_is_stable,
)


# Short-lived journals compensate committed observation batches if a pinned root
# or scan generation fails before the final catalog transaction. They are
# process-local; a crash remains conservative because no missing transition is
# committed until the scan completes.
_active_scan_journals: dict[tuple[int, str], dict[str, Any]] = {}
operation_process_lock = threading.Lock()


cancelled_jobs: set[int] = set()
# Long transfers stay on this pool so the worker loop can heartbeat, deliver
# notifications, and start other Jobs without waiting for the whole batch.
NOTIFICATION_MAX_LATENCY_SECONDS = 5.0
_operation_executor: ThreadPoolExecutor | None = None
_operation_futures: dict[int, Future[Any]] = {}
_operation_lock = threading.Lock()
_maintenance_executor: ThreadPoolExecutor | None = None
_maintenance_futures: dict[str, Future[Any]] = {}
_maintenance_lock = threading.Lock()

# Local scans deliberately keep filesystem work outside write transactions.
# This is small enough to give other PostgreSQL writers frequent admission while
# avoiding one transaction per catalogued entry.
LOCAL_SCAN_WRITE_BATCH_SIZE = 250

# Cloud scans keep provider I/O outside write transactions and commit catalog
# merges in bounded batches so other writers (lease renewal,
# notifications) are not blocked for the full listing.
CLOUD_SCAN_WRITE_BATCH_SIZE = 250
# S3 DeleteObjects accepts at most 1,000 exact targets per request.
CLOUD_DELETE_BATCH_SIZE = 1000


# A claim is intentionally durable rather than process-local.  Five minutes is
# long enough for ordinary provider calls while status/progress checkpoints renew
# it during transfers; a dead process becomes recoverable once the lease expires,
# without waiting for another restart.
JOB_CLAIM_LEASE_SECONDS = 5 * 60
EXPIRED_LEASE_RECONCILE_BATCH = 100
JOB_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
JOB_LEASE_HELD_STATUSES = frozenset(
    {"downloading", "uploading", "verifying", "cleaning"}
)
_JOB_COUNTER_ACTIONS = frozenset(
    {
        "upload",
        "recover",
        "free-space",
        "rename",
        "cloud-purge",
        "storage-class",
    }
)
_pending_job_counters: contextvars.ContextVar[list[tuple[str, str]] | None] = (
    contextvars.ContextVar("pending_job_counters", default=None)
)
_INTERRUPTED_JOB_SQL = """
(
        (j.action='recover' AND j.status IN ('downloading', 'verifying'))
     OR (j.action='upload' AND j.status IN ('uploading', 'verifying'))
     OR (j.action='rename' AND j.status IN ('uploading', 'verifying', 'cleaning'))
     OR (j.action='free-space' AND j.status='cleaning')
     OR (j.action='storage-class' AND j.status='uploading')
     OR (j.action='cloud-purge' AND j.status='cleaning')
)
"""
_worker_claim = threading.local()


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


@retry_database_unit
def _renew_claim_if_owned(job_id: int, claim_token: str) -> dict[str, Any] | None:
    timestamp = now_iso()
    with owned_connection(db()) as connection:
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
            if shutdown_state.requested:
                return
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
    """Signal SDK transfers and verification streams to stop at checkpoints."""
    with operation_process_lock:
        cancelled_jobs.update(job_ids)


def job_cancelled(job_id: int) -> bool:
    with operation_process_lock:
        return job_id in cancelled_jobs


def ensure_job_active(job_id: int, message: str) -> None:
    """Stop work when local cancellation, DB cancellation, or lease loss wins.

    ``cancel_jobs`` is a fast same-process signal.  The durable claim check is
    also required because another application process may cancel or reclaim the
    row while this worker is inside a long-running operation.
    """
    shutdown_state.checkpoint()
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
    shutdown_state.checkpoint()
    job_id = int(job["id"])
    if job_cancelled(job_id):
        raise OperationCancelled(message)
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


def s3_download_transfer_config() -> TransferConfig:
    """TransferManager config for plain-vault Archive Version downloads."""
    runtime = _runtime_settings()
    threshold_mib = int(runtime.s3_download_multipart_threshold_mib or 8)
    chunk_mib = int(runtime.s3_download_multipart_chunksize_mib or 8)
    concurrency = int(runtime.s3_download_max_concurrency or 10)
    return TransferConfig(
        multipart_threshold=max(5, threshold_mib) * 1024 * 1024,
        multipart_chunksize=max(5, chunk_mib) * 1024 * 1024,
        max_concurrency=max(1, min(32, concurrency)),
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
        if amount == 0:
            return
        should_report = False
        with self._lock:
            # s3transfer emits negative deltas when a retried part rewinds.
            self.transferred = max(0, self.transferred + amount)
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


def s3_client(*, preflight: bool = False, non_idempotent: bool = False):
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
    config = (Config(connect_timeout=2, read_timeout=3,
                     retries={"mode": "standard", "total_max_attempts": 1})
              if preflight else Config(retries={"mode": "standard", "total_max_attempts": 1})
              if non_idempotent else Config(retries={"mode": "standard", "max_attempts": 4}))
    return boto3.client("s3", region_name=settings.aws_region, config=config)


def validate_cloud_vault(vault: dict[str, Any], *, notify: bool = True) -> None:
    # The installation gate owns one warning, including for cleanup callers.
    # Provider failures here only suspend work; no transfer has started yet.
    try:
        state = cloud_preflight_gate.check(db, lambda: _validate_cloud_vault(vault))
    except psycopg.Error as exc:
        # Even an aborted gate-publication transaction is before the first
        # transfer. Suspend, do not replay a cloud call as a DB-unit retry.
        raise cloud_preflight_gate.CloudPreflightBlocked("S3 readiness status is temporarily unavailable; Job remains queued") from exc
    if state["status"] != "ready":
        raise cloud_preflight_gate.CloudPreflightBlocked(state.get("remediation") or "S3 readiness check pending")


def _validate_cloud_vault(vault: dict[str, Any]) -> None:
    bucket = vault.get("s3_bucket")
    if not bucket or is_placeholder(
        bucket,
        "BUCKET-NAME",
        "REPLACE-WITH-BUCKET-NAME",
    ):
        raise cloud_preflight_gate.ReadinessFailure("The S3 bucket name is not configured. Set VAULT_S3_BUCKET to the archive bucket")
    result = check_bucket_readiness(
        bucket,
        region=settings.aws_region,
        client=s3_client(preflight=True),
    )
    if not result.ok:
        raise cloud_preflight_gate.ReadinessFailure(
            "S3 bucket preflight failed: " + preflight_failure_message(result)
        )


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
    subtree: Path | None = None,
    invalidate_content: bool = False,
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
    invalidate_paths: list[str] = []
    unreadable_paths: list[str] = []

    def _persisted_scan(vault_row: dict[str, Any]) -> bool:
        return "root_identity" in vault_row or "root_identity_version" in vault_row

    def flush() -> None:
        nonlocal count
        if not pending and not unreadable_paths:
            return
        tree.validate_active()
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
                           lc.last_seen_at, lc.observed_at, lc.content_observed_at
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
        for relative in unreadable_paths:
            report_problem(connection, event="file_unreadable", vault_id=vault_id, path=relative)
        unreadable_paths.clear()
        catalog = ArchiveCatalog(connection)
        # Issue #242: one Vault row-lock touch, one bulk identity lookup, and
        # one bulk upsert replace the per-file round-trips that pinned Postgres
        # CPU on large Vaults.  The batch is semantically identical to the
        # serialized per-file path (same upsert columns, digest preservation,
        # dirty ancestors).
        catalog.observe_local_copies_batch(
            vault_id=vault_id,
            entries=pending,
            seen_at=scan_id,
        )
        if invalidate_paths:
            # Directory replacement can preserve size/mtime. A watcher subtree
            # invalidates real changes, not independently verified file echoes.
            connection.execute("""
                UPDATE local_copies SET content_observed_at=%s,
                    plaintext_sha256=NULL, matched_archive_version_id=NULL
                WHERE vault_file_id IN (
                    SELECT vf.id FROM vault_files vf JOIN file_paths fp
                      ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
                    WHERE vf.vault_id=%s AND fp.path=ANY(%s)
                )
            """, (scan_id, vault_id, invalidate_paths))
        count += len(pending)
        pending.clear()
        invalidate_paths.clear()
        tree.validate_active()
        if _persisted_scan(vault):
            _current_verified_scan_row(connection, vault)
        if allow_chunk_commits:
            connection.commit()

    unreadable = False

    def failed(entry: Path, error: OSError) -> None:
        nonlocal unreadable
        unreadable = True
        if scan_journal is not None:
            scan_journal["incomplete"] = True
        # Defer DB writes until flush so a later echoed child can be hashed
        # outside a write transaction, even after an unreadable sibling.
        unreadable_paths.append(entry.relative_to(root).as_posix())

    with PinnedTree(root, subtree=subtree) as tree:
        if _persisted_scan(vault) and vault_relocation.opened_root_identity(tree.fd) != vault.get("root_identity"):
            raise _ScanRootMismatch("Vault root changed before traversal")
        for entry, pinned_entry in tree.entries(on_error=failed):
            if is_restore_temporary_name(entry.name):
                continue
            try:
                inspected = inspect_entry(pinned_entry)
                if inspected is None:
                    continue
                file_type, entry_stat = inspected
            except OSError as exc:
                failed(entry, exc)
                continue
            relative = entry.relative_to(root).as_posix()
            if invalidate_content and not local_observation.consume(vault_id, relative, pinned_entry):
                # Use the pinned parent while it is open. Independent hashing
                # must precede journal/catalog/problem writes for this batch.
                invalidate_paths.append(relative)
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
        tree.validate()
        flush()
        tree.validate()
    if unreadable:
        # Keep the per-path problems durable even if a later identity check
        # rejects the scan. Healthy observations may still be protected, but an
        # incomplete walk cannot authorize missing transitions or recovery.
        connection.commit()

    if _persisted_scan(vault):
        _current_verified_scan_row(connection, vault)
    access = source_layout.vault_local_access(vault["source_root"])
    alias = access.volume_alias
    safe_scan_result = access.local_operations_allowed or access.volume_health == "scan_required"
    if finalize_missing and not unreadable and safe_scan_result and (
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
        if not unreadable and safe_scan_result and alias and source_layout.requires_full_local_scan(alias)
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
        retire_absent_restore_ghosts(_connection, int(vault["id"]), root, now_iso())
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
        retire_absent_restore_ghosts(connection, int(vault["id"]), root, now_iso())
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
        journal: dict[str, Any] = {}
        event_id = now_iso()
        try:
            return _apply_filesystem_changes(vault, changes, event_id=event_id, journal=journal)
        except Exception:
            _rollback_local_scan_journal(vault_id, event_id, journal)
            raise


def _apply_filesystem_changes(
    vault: dict[str, Any], changes: set[tuple[Change, str]],
    *, event_id: str | None = None, journal: dict[str, Any] | None = None,
) -> int:
    """Apply filesystem watcher events directly to the local catalog."""
    if vault_relocation.local_work_suspended(vault):
        raise RuntimeError("Source watcher suspended for Vault relocation")
    if protection_stop_service.local_work_suspended(vault):
        raise RuntimeError("Source watcher suspended after protection stopped")
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
        raise RuntimeError("Source watcher suspended after protection stopped")
    access = source_layout.vault_local_access(vault["source_root"])
    if not access.local_operations_allowed:
        raise RuntimeError(
            f"Source watcher blocked by Source Volume health: {access.volume_health}"
        )
    _validate_enrolled_scan_root(vault)
    root = Path(vault["source_root"]).resolve()
    if not root.is_dir():
        raise RuntimeError(f"Folder is not available in the container: {root}")
    changed = 0
    event_id = event_id or now_iso()
    published: dict[str, Any] | None = None
    subtrees: list[Path] = []
    added_paths = {Path(value) for change, value in changes if change == Change.added}
    paths: list[Path] = []
    for value in {value for _, value in changes}:
        entry = Path(value)
        try:
            relative = entry.relative_to(root).as_posix()
        except ValueError:
            continue
        if relative == "." or ".." in entry.parts or is_restore_temporary_name(entry.name):
            continue
        parent = entry.parent.resolve()
        if parent == root or root in parent.parents:
            # Echo validation can hash a large recovered/salvaged copy. Keep
            # that work outside every catalog write transaction.
            if not local_observation.consume(int(vault["id"]), relative, entry):
                paths.append(entry)
    # Directory transactions commit in bounded batches. Process them before
    # the atomic single-file/deletion batch, never committing a partial direct
    # observation as an accidental side effect of a later subtree flush.
    paths.sort(key=lambda entry: (not (entry.is_dir() and not entry.is_symlink()), str(entry)))
    with db() as connection:
        catalog = ArchiveCatalog(connection)
        # A rename emits one directory event, not an event for each child.
        # Coalesce overlapping prefixes and stream bounded scan-write batches;
        # never finalize missing copies outside the affected subtree.
        for entry in paths:
            try:
                relative = entry.relative_to(root).as_posix()
            except ValueError:
                continue
            if relative == "." or not relative or is_restore_temporary_name(entry.name):
                continue
            if any(parent == entry or parent in entry.parents for parent in subtrees):
                if entry.exists() or entry.is_symlink():
                    continue
                # A walk cannot emit a deleted child. Keep explicit missing
                # events even when their parent was reconciled in this batch.
            # Native events are lexical paths. Do not follow a substituted
            # parent symlink outside the enrolled root.
            parent = entry.parent.resolve()
            if parent != root and root not in parent.parents:
                continue
            if entry.is_dir() and not entry.is_symlink():
                if entry not in added_paths:
                    # Metadata-only directory changes (also emitted by polling
                    # on child writes) are not content events for every child.
                    continue
                changed += _scan_tree(
                    connection, vault, event_id, root=root, subtree=entry,
                    allow_chunk_commits=True, finalize_missing=False,
                    scan_journal=journal, invalidate_content=True,
                )[0]
                subtrees.append(entry)
                continue

            try:
                inspected = inspect_entry(entry)
            except FileNotFoundError:
                inspected = None
            except OSError:
                report_problem(
                    connection, event="file_unreadable", vault_id=int(vault["id"]),
                    path=relative,
                )
                continue
            if inspected is not None:
                file_type, entry_stat = inspected
                file_id = catalog.observe_local_copy(
                    vault_id=vault["id"],
                    path=relative,
                    file_type=file_type,
                    size=entry_stat.st_size,
                    mtime_ns=entry_stat.st_mtime_ns,
                    seen_at=event_id,
                    observed_at=event_id,
                )
                # Even a same-size write with a restored mtime restarts the
                # window. Persist the event, not an in-memory debounce timer.
                connection.execute(
                    """UPDATE local_copies SET content_observed_at=%s,
                           plaintext_sha256=NULL, matched_archive_version_id=NULL
                       WHERE vault_file_id=%s""",
                    (event_id, file_id),
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
        if "root_identity" in vault:
            _current_verified_scan_row(connection, vault)
        if not source_layout.vault_local_access(vault["source_root"]).local_operations_allowed:
            raise _ScanRootMismatch("Source Volume changed during watcher observation")
        if changed:
            # One revision per watcher batch — never one event per path.
            published = record_catalog_revision(
                connection,
                vault_id=int(vault["id"]),
                reason="filesystem_watch",
                extra_payload={"changed_paths": int(changed)},
                created_at=event_id,
            )
    if published is not None:
        publish_committed_event(published)
    return changed


def _cloud_scan_still_current(vault: dict[str, Any], scan_id: str) -> bool:
    """Skip missing-marks when a newer scan generation has already started."""
    vault_id = int(vault["id"])
    with status_lock:
        status = runtime_status.get(vault_id)
    if status is None:
        return True
    return status.get("scan_id") == scan_id


def _remote_listing_timestamp(item: dict[str, Any], scan_id: str) -> str:
    value = item.get("LastModified")
    return value.isoformat() if hasattr(value, "isoformat") else str(value or scan_id)


def scan_cloud(vault: dict[str, Any], scan_id: str, *, reconnect: bool = False) -> int:
    """Merge one complete cloud listing into the catalog in bounded writes.

    Provider listings stay outside write transactions.
    Catalog merges commit in ``CLOUD_SCAN_WRITE_BATCH_SIZE`` chunks so other
    writers can renew leases and persist notifications.
    Unseen versions are marked missing only after a complete, interpretable
    listing, and only if this scan generation is still current.
    """
    validate_cloud_vault(vault)
    client = s3_client()
    paginator = client.get_paginator("list_object_versions")
    prefix = vault["s3_prefix"].strip("/")
    kwargs: dict[str, Any] = {"Bucket": vault["s3_bucket"]}
    if prefix:
        kwargs["Prefix"] = f"{prefix}/"
    versions: list[tuple[str, str, str, Any, str, str, str]] = []
    markers: list[tuple[str, str, str, str]] = []
    invalid_keys = False
    listing_complete = False

    def convert_page(items: list[dict[str, Any]], kind: str) -> None:
        nonlocal invalid_keys
        for item in items:
            object_key = item["Key"]
            logical_path = object_key_to_path(object_key, prefix)
            if logical_path is None:
                # Directory marker objects contain no file content. Any other
                # unrecognised key prevents destructive reconciliation.
                if not object_key.endswith("/"):
                    invalid_keys = True
                continue
            version_id = item["VersionId"]
            timestamp = _remote_listing_timestamp(item, scan_id)
            if kind == "version":
                versions.append(
                    (
                        logical_path,
                        version_id,
                        object_key,
                        item.get("Size"),
                        item.get("StorageClass", "STANDARD"),
                        str(item.get("ETag", "")).strip('"'),
                        timestamp,
                    )
                )
            else:
                markers.append((logical_path, version_id, object_key, timestamp))

    try:
        page_iter = paginator.paginate(**kwargs)
        for page in page_iter:
            convert_page(page.get("Versions", []), "version")
            convert_page(page.get("DeleteMarkers", []), "marker")
        listing_complete = True
    except Exception:
        listing_complete = False
        raise

    versions.sort(key=lambda entry: (entry[0], entry[6], entry[2], entry[1]))
    markers.sort(key=lambda entry: (entry[0], entry[3], entry[2], entry[1]))
    grouped: dict[str, list[tuple[str, str, str, Any, str, str, str]]] = {}
    for entry in versions:
        grouped.setdefault(entry[0], []).append(entry)

    with db() as connection:
        # Decide cleanup authority before discovery changes internal version
        # numbers. Record all listed copies chronologically, without granting
        # trust. Legacy/unverified/mismatched or ambiguous keepers fail closed.
        catalog = ArchiveCatalog(connection)
        verified_keepers: dict[str, dict[str, Any]] = {}
        stale_keeper_paths: set[str] = set()
        for path, entries in grouped.items():
            known = catalog.list_versions(vault["id"], path)
            keeper = next((row for row in known if row["availability"] not in {"missing", "purged"}), None)
            if keeper is None:
                continue
            identity = (keeper["object_key"], keeper["provider_version_id"])
            if identity not in {(entry[2], entry[1]) for entry in entries}:
                stale_keeper_paths.add(path)
                continue
            keeper = next((row for row in known if row["recoverable"] and
                           (row["object_key"], row["provider_version_id"]) in {(entry[2], entry[1]) for entry in entries}), None)
            if not invalid_keys and keeper:
                # Discovery is observation, not election/trust. Every extra
                # requires the same claimed exact-version cleanup authority.
                verified_keepers[path] = keeper

    if reconnect and (invalid_keys or any(
        len(entries) > 1 and entries[-1][6] == entries[-2][6]
        for entries in grouped.values()
    )):
        raise RuntimeError("Cloud listing is ambiguous; reconnect remains suspended")
    count = 0
    snapshot_extras: list[tuple[str, str, str, str]] = []
    for offset in range(0, len(versions), CLOUD_SCAN_WRITE_BATCH_SIZE):
        if not _cloud_scan_still_current(vault, scan_id):
            return count
        batch = versions[offset : offset + CLOUD_SCAN_WRITE_BATCH_SIZE]
        with db() as connection:
            catalog = ArchiveCatalog(connection)
            for (
                logical_path,
                version_id,
                object_key,
                size,
                storage_class,
                etag,
                timestamp,
            ) in batch:
                keeper = verified_keepers.get(logical_path)
                if not reconnect and keeper and (object_key, str(version_id)) != (
                    keeper["object_key"], keeper["provider_version_id"],
                ):
                    snapshot_extras.append((
                        logical_path, str(version_id), object_key, str(keeper["id"]),
                    ))
                # A failed cleanup still leaves every discovered copy catalogued
                # as present, rather than producing a false missing-copy signal.
                previously_known = connection.execute(
                    "SELECT id FROM archive_versions WHERE vault_id=%s AND object_key=%s "
                    "AND provider_version_id=%s",
                    (vault["id"], object_key, version_id),
                ).fetchone()
                kept_archive_version_id = catalog.record_archive_version(
                    vault_id=vault["id"],
                    path=logical_path,
                    object_key=object_key,
                    provider_version_id=version_id,
                    size=size,
                    storage_class=storage_class,
                    etag=etag,
                    uploaded_at=timestamp,
                    observed_at=now_iso(),
                    scan_id=scan_id,
                )
                if previously_known is None and (logical_path, version_id, object_key) == grouped[logical_path][-1][:3]:
                    from .services.recovery_kit import remember_cloud_update
                    file_row = connection.execute(
                        "SELECT vault_file_id FROM archive_versions WHERE id=%s",
                        (kept_archive_version_id,),
                    ).fetchone()
                    remember_cloud_update(connection, file_row["vault_file_id"])
                if logical_path in stale_keeper_paths and not invalid_keys:
                    catalog.mark_unlisted_snapshot_versions_superseded(
                        kept_archive_version_id=str(kept_archive_version_id),
                        scan_id=scan_id,
                        scan_started_at=scan_id,
                    )
                count += 1
    if snapshot_extras:
        _purge_current_snapshot_scan_extras(vault, snapshot_extras, client)
    for offset in range(0, len(markers), CLOUD_SCAN_WRITE_BATCH_SIZE):
        if not _cloud_scan_still_current(vault, scan_id):
            return count
        batch = markers[offset : offset + CLOUD_SCAN_WRITE_BATCH_SIZE]
        with db() as connection:
            catalog = ArchiveCatalog(connection)
            for logical_path, version_id, object_key, timestamp in batch:
                catalog.record_delete_marker(
                    vault_id=vault["id"],
                    path=logical_path,
                    object_key=object_key,
                    provider_version_id=version_id,
                    created_at=timestamp,
                    observed_at=now_iso(),
                )
                count += 1
    # An unrecognised key or a truncated listing is not evidence that
    # prior catalog rows disappeared. Fail closed by retaining availability.
    if (
        listing_complete
        and not invalid_keys
        and _cloud_scan_still_current(vault, scan_id)
    ):
        with db() as connection:
            newly_missing = ArchiveCatalog(
                connection
            ).mark_unseen_archive_versions_missing(
                vault_id=vault["id"],
                scan_id=scan_id,
                scan_started_at=scan_id,
            )
            from .services.catalog_incidents import remember_missing_archives

            remember_missing_archives(
                connection,
                newly_missing,
                reason="cloud_scan_unseen",
                observed_at=scan_id,
            )
    if reconnect:
        _verify_reconnected_cloud(vault, grouped, client, scan_id)
    return count


def _verify_reconnected_cloud(
    vault: dict[str, Any], grouped: dict[str, list[tuple]], client: Any, scan_id: str,
) -> None:
    """Recover content evidence for the newest exact S3 versions, never delete.

    Known verified evidence is retained. Unknown current content is read fully
    (including size/VersionId checks); cold unreadable objects leave the entire
    reconnect suspended until an operator makes them readable. No stale digest
    can authorize an upload or local release in that interval.
    """
    for entries in grouped.values():
        path, provider_id, key, size, _storage_class, _etag, _timestamp = entries[-1]
        with db() as connection:
            row = connection.execute(
                "SELECT * FROM archive_versions WHERE vault_id=%s AND object_key=%s "
                "AND provider_version_id=%s", (vault["id"], key, provider_id),
            ).fetchone()
        if row["integrity"] == "mismatch" or (row.get("size") is not None and row["size"] != size):
            raise RuntimeError("Cloud content mismatch; reconnect remains suspended")
        digest = row.get("plaintext_sha256")
        if row["integrity"] != "verified" or not digest:
            response = client.get_object(Bucket=vault["s3_bucket"], Key=key, VersionId=provider_id)
            body = response["Body"]
            hasher, length = hashlib.sha256(), 0
            try:
                if str(response.get("VersionId")) != str(provider_id):
                    raise RuntimeError("Cloud verification returned a different VersionId")
                while chunk := body.read(1024 * 1024):
                    hasher.update(chunk)
                    length += len(chunk)
            finally:
                body.close()
            if length != size:
                raise RuntimeError("Cloud verification size mismatch")
            digest = hasher.hexdigest()
            if row.get("plaintext_sha256") and row["plaintext_sha256"] != digest:
                raise RuntimeError("Cloud verification digest mismatch")
        if not _cloud_scan_still_current(vault, scan_id):
            raise RuntimeError("Reconnect scan superseded")
        with db() as connection:
            catalog = ArchiveCatalog(connection)
            catalog.mark_version_verified(row["id"], plaintext_sha256=digest, verified_at=now_iso())
            # Listing chronology, not catalog allocation order, chooses current.
            connection.execute(
                """UPDATE archive_versions SET version_number=(
                    SELECT MAX(av.version_number)+1 FROM archive_versions av
                    WHERE av.vault_file_id=%s)
                WHERE id=%s AND version_number < (
                    SELECT MAX(av.version_number) FROM archive_versions av WHERE av.vault_file_id=%s)
                """, (row["vault_file_id"], row["id"], row["vault_file_id"]),
            )


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


class _ScanRootUnreadable(RuntimeError):
    """The enrolled root could not be observed; distinct from an observed mismatch."""


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
        # Only an I/O failure is "unreadable"; symlinks and non-directories are
        # observed replacements and stay a hard identity failure.
        failure = _ScanRootUnreadable if exc.reason == "inaccessible" else RuntimeError
        raise failure(f"Vault root identity is unavailable: {exc}") from exc
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
        access = source_layout.vault_local_access(current["source_root"])
        if not access.local_operations_allowed and access.volume_health != "scan_required":
            raise RuntimeError("Source Volume identity/health changed during scan")
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
    vault: dict[str, Any], root: Path, *, scan_journal: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Hash Local Copies without holding a database write transaction."""
    updates: list[dict[str, Any]] = []
    for row in _unfingerprinted_local_rows(int(vault["id"])):
        try:
            local_path = safe_local_path(str(root), row["path"])
            if not local_path.is_file():
                continue
            digest, stat_result = hash_stable_regular_file(local_path)
        except OSError:
            if scan_journal is not None:
                scan_journal["incomplete"] = True
            with db() as connection:
                report_problem(
                    connection, event="file_unreadable", vault_id=int(vault["id"]),
                    path=row["path"],
                )
            continue
        except (ValueError, RuntimeError):
            if scan_journal is not None:
                scan_journal["incomplete"] = True
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
            vault_id = int(vault["id"])
            for update in batch:
                # A watcher may have replaced the file after hashing. The
                # predicate prevents a stale digest from overwriting it.
                result = connection.execute(
                    """
                    UPDATE local_copies
                    SET content_observed_at=CASE
                            WHEN (size, mtime_ns) IS DISTINCT FROM (%s::bigint, %s::bigint)
                            THEN %s ELSE content_observed_at END,
                        size=%s,
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
                        update["observed_at"],
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
                if getattr(result, "rowcount", 0):
                    invalidate_for_vault_file(
                        connection,
                        vault_id,
                        str(update["vault_file_id"]),
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
                    mark_path_dirty(connection, vault_id, path)
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
                    invalidate_for_vault_file(connection, vault_id, str(file_id))
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
                        observed_at=%s,
                        content_observed_at=%s
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
                        snapshot["content_observed_at"],
                        file_id,
                        scan_id,
                    ),
                )
                invalidate_for_vault_file(connection, vault_id, str(file_id))
    except Exception:
        # Compensation is best effort. The scan is still reported as blocked,
        # and no missing transition or relocation completion can commit.
        return


def _verified_local_scan(
    vault: dict[str, Any], scan_id: str
) -> tuple[int, dict[str, int], str | None, bool]:
    """Walk/hash outside writes, then publish bounded catalog stages.

    Local observations are committed in short batches so ordinary PostgreSQL
    writers are not held behind directory traversal or hashing.  Missing
    transitions, rename decisions, and relocation recovery remain in one final
    transaction guarded by the enrolled root and scan generation.
    """
    # Minimal in-memory Vaults are a longstanding unit-test seam. Persisted
    # rows always contain identity columns and always use the staged path.
    if "root_identity" not in vault and "root_identity_version" not in vault:
        scan_journal: dict[str, Any] = {}
        count = scan_tree(vault, scan_id, _scan_journal=scan_journal)
        complete = not scan_journal.get("incomplete", False)
        renames = apply_auto_renames(vault, _recognize_renames=complete)
        return count, renames, None, complete

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
        # commit inside _scan_tree and release their PostgreSQL row locks.
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
        updates = _collect_local_fingerprint_updates(vault, pinned_root, scan_journal=scan_journal)
        _apply_local_fingerprint_updates(vault, scan_id, updates)

        complete = not scan_journal.get("incomplete", False)
        alias = access.volume_alias
        completed_alias = (
            alias
            if complete and alias and source_layout.requires_full_local_scan(alias)
            else None
        )
        with db() as connection:
            _current_verified_scan_row(connection, vault)
            if not _scan_generation_is_current(int(vault["id"]), scan_id):
                raise _ScanRootMismatch("Local scan was superseded")
            rename_summary = _apply_auto_renames(
                connection, vault, root=pinned_root, hash_missing=False,
                recognize_renames=complete and vault.get("relocation_state") != "scan_required",
            )
            rename_summary["hashed"] = len(updates)
            _current_verified_scan_row(connection, vault)
            final_access = source_layout.vault_local_access(vault["source_root"])
            safe_scan_result = (
                final_access.local_operations_allowed
                or final_access.volume_health == "scan_required"
            )
            if complete and safe_scan_result and (
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
            was_relocation_scan = was_relocation_scan and complete
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
        return count, rename_summary, completed_alias, complete


def _report_worker_error(component: str, exc: BaseException, vault_id: int | None = None) -> None:
    # The notification tells the Owner to check the server log: write the
    # exception there first, independently of whether the database is usable.
    worker_error_store.log_worker_error(component, exc, vault_id)
    try:
        with db() as connection:
            worker_error_store.record_worker_error(
                connection, component=component, exc=exc, vault_id=vault_id, log=False,
            )
    except Exception as report_error:
        worker_error_store.log_report_failure(component, report_error)


def _resolve_worker_error(component: str, vault_id: int | None = None) -> None:
    """Close the internal-error problem for a worker that is healthy again.

    Best effort, like the report: a failing resolve must never stop the worker.
    """
    try:
        with db() as connection:
            report_problem(
                connection, event="internal_error", vault_id=vault_id,
                component=component, active=False,
            )
    except Exception as resolve_error:
        worker_error_store.log_report_failure(component, resolve_error, resolving=True)


# A watcher batch is a complete DB unit plus read-only filesystem inspection.
# A PostgreSQL deadlock/serialization victim rolled back everything, the
# scan journal is compensated by apply_filesystem_changes, and the pending
# change set is idempotent, so replaying the same batch is safe.
WATCHER_APPLY_MAX_ATTEMPTS = 5


def _apply_watcher_changes(vault: dict[str, Any], changes: set[tuple[Change, str]]) -> int:
    """Apply one watcher batch, replaying it when PostgreSQL chose it as victim."""
    for attempt in range(WATCHER_APPLY_MAX_ATTEMPTS):
        try:
            return apply_filesystem_changes(vault, changes)
        except Exception as error:
            if (not is_retryable_transaction_error(error)
                    or attempt + 1 == WATCHER_APPLY_MAX_ATTEMPTS):
                raise
            time.sleep(random.uniform(0.05, min(1.0, 0.1 * 2 ** attempt)))
    raise AssertionError("unreachable")


def _record_scan_component_health(
    vault_id: int,
    component: str,
    *,
    ok: bool,
    error: str | None = None,
    verified: bool = False,
    extra: Mapping[str, Any] | None = None,
    notify_problem: bool = True,
) -> None:
    """Persist one scan/audit outcome and surface failures without diagnostic UI."""
    from .services.component_health import record_component_health

    stamp = now_iso()
    try:
        with db() as connection:
            if component in {"source", "cloud", "policy"} and (ok or notify_problem):
                notification_service.set_problem(
                    connection, key=f"blocked:{vault_id}:{component}",
                    event="protection_blocked", vault_id=int(vault_id), active=not ok,
                    params={"component": component},
                )
            elif not ok and notify_problem:
                report_problem(connection, vault_id=int(vault_id),
                               event="internal_error", component=component)
            if ok:
                report_problem(connection, vault_id=int(vault_id), event="internal_error",
                               component=component, active=False)
            record_component_health(
                connection,
                vault_id=int(vault_id),
                component=component,
                ok=ok,
                error=error,
                verified=verified,
                extra=extra,
                at=stamp,
            )
    except Exception:
        pass
    with status_lock:
        status = runtime_status.setdefault(
            int(vault_id),
            {"scanning": False, "last_scan": None, "last_error": None},
        )
        status[f"last_error_{component}"] = None if ok else error
        if ok:
            status[f"last_success_{component}"] = stamp
            if verified:
                status["last_verified_at"] = stamp
        if component == "audit" and extra is not None and "healthy" in extra:
            status["last_audit_healthy"] = bool(extra.get("healthy"))


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
        if protection_stop_service.local_work_suspended(vault):
            return {"suspended": 1}
        if (
            str(vault.get("relocation_state") or "ready") == "ready"
            and vault_relocation.local_work_suspended(vault)
        ):
            return {"suspended": 1}
        try:
            _validate_enrolled_scan_root(vault)
        except Exception as exc:
            message = f"Source scan blocked: {exc}"
            with status_lock:
                status["last_error"] = message
            _record_scan_component_health(
                vault_id, "source", ok=False, error=message
            )
            return {"local": -1, "root_identity_mismatch": 1}

        scan_id = now_iso()
        with status_lock:
            status.update(scanning=True, last_error=None, scan_id=scan_id)
        result: dict[str, int] = {}
        reconnect = vault.get("relocation_state") == "scan_required"
        if reconnect:
            try:
                # Cloud first, with NO cleanup: stale local metadata must not
                # become upload/release authority before S3 has been recovered.
                result["cloud"] = scan_cloud(vault, scan_id, reconnect=True)
                with db() as connection:
                    _current_verified_scan_row(connection, vault)
                    vault_relocation.prepare_reconnect_local_scan(connection, vault_id)
            except Exception as exc:
                status["last_error"] = "Cloud reconciliation failed; reconnect remains suspended"
                _record_scan_component_health(vault_id, "cloud", ok=False, error=status["last_error"],
                                              notify_problem=not isinstance(exc, cloud_preflight_gate.CloudPreflightBlocked))
                return {"cloud": -1, "reconnect_suspended": 1}
        local_scan_identity_valid = True
        access = source_layout.vault_local_access(vault["source_root"])
        if access.local_operations_allowed or access.volume_health == "scan_required":
            try:
                count, rename_summary, _completed_alias, complete = _verified_local_scan(
                    vault, scan_id
                )
                result["local"] = count
                if not complete:
                    result["local_incomplete"] = 1
                result.update(
                    {f"rename_{key}": value for key, value in rename_summary.items()}
                )
            except (_ScanRootMismatch, TreeIdentityChanged) as exc:
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
                _record_scan_component_health(
                    vault_id, "source", ok=False, error=status["last_error"]
                )
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
                _record_scan_component_health(
                    vault_id, "source", ok=False, error=status["last_error"],
                )
            else:
                _record_scan_component_health(
                    vault_id, "source", ok=complete, verified=complete,
                    error=None if complete else "Source scan incomplete: unreadable paths",
                    notify_problem=complete,
                )
        else:
            result["local_skipped"] = 1
            result["local"] = 0
            _record_scan_component_health(
                vault_id, "source", ok=False,
                error="Protection blocked: Source Volume unavailable",
            )
        if local_scan_identity_valid:
            try:
                if not reconnect:
                    result["cloud"] = scan_cloud(vault, scan_id)
            except Exception as exc:
                status["last_error"] = f"Cloud scan: {exc}"
                result["cloud"] = -1
                _record_scan_component_health(
                    vault_id, "cloud", ok=False, error=status["last_error"],
                    notify_problem=not isinstance(exc, cloud_preflight_gate.CloudPreflightBlocked),
                )
            else:
                _record_scan_component_health(
                    vault_id, "cloud", ok=True, verified=True
                )
            try:
                validate_cloud_vault(vault)
                with db() as connection:
                    result["lifecycle_rules"] = sync_lifecycle_rules_for_bucket(
                        connection,
                        s3_client(),
                        bucket=vault["s3_bucket"],
                    )
                    owner_id = owner_user_id(connection)
                    local_access = source_layout.vault_local_access(vault["source_root"])
                    if owner_id is not None and local_access.local_operations_allowed:
                        result["auto_local_cleanups"] = queue_auto_local_cleanups(
                            connection,
                            vault_id=int(vault["id"]),
                            requested_by=owner_id,
                        )
            except Exception as exc:
                status["last_error"] = f"Storage plan synchronization: {exc}"
                result["lifecycle_rules"] = -1
                _record_scan_component_health(
                    vault_id, "policy", ok=False, error=status["last_error"],
                    notify_problem=not isinstance(exc, cloud_preflight_gate.CloudPreflightBlocked),
                )
            else:
                _record_scan_component_health(
                    vault_id, "policy", ok=True
                )
            # Admission is local-only. Failed cloud validation or lifecycle
            # synchronization must not suppress the durable upload queue.
            # process_upload still requires successful preflight before writes.
            try:
                with db() as connection:
                    owner_id = owner_user_id(connection)
                    local_access = source_layout.vault_local_access(vault["source_root"])
                    if owner_id is not None and local_access.local_operations_allowed:
                        result["auto_uploads"] = queue_auto_uploads(
                            connection, vault_id=vault_id,
                            source_root=str(vault["source_root"]), requested_by=owner_id,
                        )
            except Exception as exc:
                status["last_error"] = status.get("last_error") or f"Automatic upload queue: {exc}"
                _report_worker_error("auto_upload_queue", exc, vault_id)
        status["last_scan"] = now_iso()
        # Full scans are the low-frequency reconciliation net. Publish one
        # catalog revision so open browsers converge without idle polling.
        if not result.get("already_running") and not result.get("suspended"):
            try:
                with db() as connection:
                    published = record_catalog_revision(
                        connection,
                        vault_id=vault_id,
                        reason="scan",
                        extra_payload={
                            "local": int(result.get("local") or 0),
                            "cloud": int(result.get("cloud") or 0),
                        },
                    )
                publish_committed_event(published)
                result["catalog_revision"] = int(published["revision"])
            except Exception as exc:
                # Catalog mutation already committed; surface publication failure
                # without discarding the scan result.
                status["last_error"] = (
                    f"Catalog revision publication: {exc}"
                    if not status.get("last_error")
                    else status["last_error"]
                )
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
    recognize_renames: bool = True,
) -> dict[str, int]:
    """Apply rename analysis inside the caller-owned transaction.

    ``hash_missing`` is false for the normal full-scan path: all filesystem
    hashing has already completed outside this transaction and its digests were
    published through compare-and-swap batches.
    """
    summary = {"hashed": 0, "confirmed": 0, "queued": 0}
    confirmed_paths: list[str] = []
    catalog = ArchiveCatalog(connection)
    from .services.catalog_incidents import settle_cloud_only_copies
    if requested_by is None:
        requested_by = owner_user_id(connection)
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
            except OSError:
                recognize_renames = False
                report_problem(
                    connection, event="file_unreadable", vault_id=int(vault["id"]),
                    path=row["path"],
                )
                continue
            except (ValueError, RuntimeError):
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

    # Hashed content that reappeared at a cloud-only path is decided before
    # renames and uploads: identical → Local Copy, different → held back.
    settle_cloud_only_copies(connection, vault_id=int(vault["id"]))
    if not recognize_renames:
        # An unreadable subtree prevents proving a unique rename, but must
        # never bypass the hold/notification for a cloud-only conflict.
        return summary
    candidates = catalog.list_rename_candidates(vault_id=vault["id"])
    changed_at = now_iso()
    for candidate in candidates:
        catalog.confirm_file_rename(
            vault_file_id=candidate["missing_vault_file_id"],
            new_path=candidate["new_path"],
            changed_at=changed_at,
            vault_id=vault["id"],
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
    _recognize_renames: bool = True,
) -> dict[str, int]:
    """Hash outside writes, then confirm renames atomically."""
    root = _root or Path(vault["source_root"]).resolve()
    if _connection is not None:
        # Preserve the caller-owned transaction seam used by direct catalog
        # callers and the relocation rollback tests.
        return _apply_auto_renames(
            _connection, vault, root=root, requested_by=requested_by,
            recognize_renames=_recognize_renames,
        )
    scan_id = now_iso()
    hash_status: dict[str, Any] = {}
    updates = _collect_local_fingerprint_updates(vault, root, scan_journal=hash_status)
    _recognize_renames = _recognize_renames and not hash_status.get("incomplete", False)
    _apply_local_fingerprint_updates(vault, scan_id, updates)
    with db() as connection:
        summary = _apply_auto_renames(
            connection,
            vault,
            root=root,
            requested_by=requested_by,
            hash_missing=False,
            recognize_renames=_recognize_renames,
        )
    summary["hashed"] = len(updates)
    return summary


def _claim_write_succeeded(result: Any) -> bool:
    """Normalize real cursors and lightweight unit-test recording seams."""
    rowcount = getattr(result, "rowcount", None)
    return rowcount is None or rowcount != 0


def _job_counter_action(action: str | None) -> str:
    value = str(action or "other")
    return value if value in _JOB_COUNTER_ACTIONS else "other"


def _inc_job_counter(kind: str, action: str | None) -> None:
    names = {
        "completed": "jobs_completed_total",
        "failed": "jobs_failed_total",
        "retries": "jobs_retries_total",
    }
    try:
        metrics_service.inc(names[kind], action=_job_counter_action(action))
    except Exception:
        pass


def _record_committed_job_counter(kind: str, action: str | None) -> None:
    """Count a committed Job outcome, or defer until the outer transaction."""
    pending = _pending_job_counters.get()
    if pending is not None:
        pending.append((kind, _job_counter_action(action)))
        return
    _inc_job_counter(kind, action)


@contextmanager
def _defer_job_counters() -> Iterator[None]:
    """Hold Job counters until the surrounding DB transaction commits."""
    items: list[tuple[str, str]] = []
    token = _pending_job_counters.set(items)
    try:
        yield
    except BaseException:
        raise
    else:
        for kind, action in items:
            _inc_job_counter(kind, action)
    finally:
        _pending_job_counters.reset(token)


@retry_database_unit
def set_job(
    job_id: int,
    status: str,
    message: str = "",
    *,
    message_key: str | None = None,
    message_params: dict[str, Any] | None = None,
    remediation_suspension: str | None = None,
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
    previous_status = None
    action = None
    with owned_connection(db()) as connection:
        # Non-key changes serialize with admission without blocking notification
        # FK KEY SHARE locks (Owner episode -> Vault vs Vault -> Owner).
        connection.execute("SELECT id FROM vaults WHERE id=(SELECT vault_id FROM jobs WHERE id=%s) FOR NO KEY UPDATE", (job_id,))
        current = connection.execute(
            "SELECT status, action FROM jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
        if current is not None:
            previous_status = current["status"]
            action = current["action"]
        notify_sql = ""
        if status in {"completed", "failed"}:
            notify_sql = ", terminal_notification_status='pending'"
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
                    {lease_set}{notify_sql}
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
                f"""
                UPDATE jobs
                SET status=%s,
                    message=%s,
                    message_key=%s,
                    message_params=%s,
                    updated_at=%s{notify_sql}
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
        if updated and action == "upload":
            from .services import upload_remediation
            upload_remediation.transition(connection, job_id, status, timestamp, suspension=remediation_suspension)
            _dirty_job_protection(connection, job_id)
        if updated and status in {"completed", "failed"}:
            notification_service.enqueue_job_terminal_notification_best_effort(
                connection, job_id=job_id
            )
    if (
        updated
        and status in {"completed", "failed"}
        and previous_status not in JOB_TERMINAL_STATUSES
    ):
        _record_committed_job_counter(
            "completed" if status == "completed" else "failed",
            action,
        )
    return updated


@retry_database_unit
def schedule_upload_retry(
    job_id: int,
    *,
    message: str = "",
    message_key: str | None = None,
    message_params: dict[str, Any] | None = None,
    retry_count: int,
    delay_seconds: int | None = None,
    remediation_suspension: str | None = None,
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
    action = None
    with owned_connection(db()) as connection:
        connection.execute("SELECT id FROM vaults WHERE id=(SELECT vault_id FROM jobs WHERE id=%s) FOR NO KEY UPDATE", (job_id,))
        current = connection.execute(
            "SELECT action FROM jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
        if current is not None:
            action = current["action"]
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
        if _claim_write_succeeded(result) and action == "upload":
            from .services import upload_remediation
            upload_remediation.transition(connection, job_id, "retrying", timestamp, suspension=remediation_suspension)
            _dirty_job_protection(connection, job_id)
    updated = _claim_write_succeeded(result)
    if updated and not remediation_suspension:
        # A wait for the Source Volume is not a retry of the transfer.
        _record_committed_job_counter("retries", action)
    return updated


@retry_database_unit
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
    with owned_connection(db()) as connection:
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
            SELECT * FROM vaults
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
            _validate_enrolled_scan_root(vault)
        except RuntimeError:
            # Never repair metadata using the absence of paths in a replaced
            # root. The existing residue cleanup remains separately scoped.
            pass
        else:
            with db() as connection:
                retire_absent_restore_ghosts(connection, int(vault["id"]), root, now_iso())
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


def _dirty_job_protection(connection: Any, job_id: int) -> None:
    row = connection.execute(
        "SELECT vault_id, path FROM jobs WHERE id=%s AND action='upload'", (job_id,),
    ).fetchone()
    if row:
        mark_path_dirty(connection, int(row["vault_id"]), row["path"])


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
    notify_sql = ""
    if status in {"completed", "failed"}:
        notify_sql = ", terminal_notification_status='pending'"
    result = connection.execute(
        f"""
        UPDATE jobs SET status='{status}'{progress_sql}{notify_sql},
            message=%s, updated_at=%s,
            claim_token=NULL, claimed_at=NULL, claim_expires_at=NULL
        WHERE {_reconcile_claim_is_stale_sql()} AND id=%s
          AND status NOT IN ('completed', 'failed', 'cancelled')
        """,
        (message, timestamp, timestamp, job_id),
    )
    updated = _claim_write_succeeded(result)
    if updated:
        _dirty_job_protection(connection, job_id)
    if updated and status in {"completed", "failed"}:
        row = connection.execute(
            "SELECT action FROM jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
        _record_committed_job_counter(
            status, row["action"] if row is not None else None
        )
    return updated


def _record_upload_reconciliation_block(job: dict[str, Any], reason: str) -> None:
    from .services import upload_provenance as provenance
    if reason in provenance.WAIT_REASONS:
        return  # Self-resolving: the Job waits queued, no durable block or notification.
    if reason in provenance.TERMINAL_JOB_REASONS:
        with db() as connection:
            row = connection.execute("SELECT status FROM jobs WHERE id=%s", (job["id"],)).fetchone()
        if row and row["status"] in JOB_TERMINAL_STATUSES:
            return  # The Job is already over: nothing about the file is blocked.
    @retry_database_unit
    def persist() -> None:
        with owned_connection(db()) as connection:
            connection.execute("INSERT INTO snapshot_reconciliation_blocks(vault_file_id,reason,updated_at) VALUES (%s,%s,%s) ON CONFLICT (vault_file_id) DO UPDATE SET reason=excluded.reason,updated_at=excluded.updated_at", (job["vault_file_id"], reason, now_iso()))
            connection.execute("UPDATE upload_intents SET blocked_reason=%s WHERE vault_file_id=%s AND NOT settled", (reason, job["vault_file_id"]))
            from .services.upload_remediation_discovery import capture
            capture(connection, now_iso(), file_id=job["vault_file_id"])
    persist()
    # Notifications use Owner locks separately from intent/receipt publication.
    # Retry the complete owned notification unit too, never the provider work.
    @retry_database_unit
    def notify() -> None:
        with owned_connection(db()) as connection:
            notification_service.set_problem(connection, key=f"upload_reconciliation:{job['vault_file_id']}", event="upload_reconciliation", vault_id=job["vault_id"], active=True, email=False, params={"path": job["path"], "reason": reason, "remedy": provenance.REMEDIES[reason]})
    notify()


def _keep_wait_state(job: dict[str, Any]) -> dict[str, Any]:
    """set_job keyword arguments that keep a Source Volume wait (its start, attempt).

    ``set_job`` rewrites ``message_key``/``message_params``; a Job that is waiting for
    its volume and is touched for another reason (readiness gate) must not lose them.
    """
    if job.get("message_key") != SOURCE_VOLUME_WAIT_KEY:
        return {}
    return {"message_key": SOURCE_VOLUME_WAIT_KEY, "message_params": parse_message_params(job.get("message_params"))}


def _fail_upload_on_permission_refusal(job: dict[str, Any], exc: BaseException) -> bool:
    """A refused S3 action for one file, while the bucket itself stays ready (#492).

    The readiness probe never exercises HeadObject/GetObject/PutObject, so sending
    such a 403 to the gate would reopen it at once and re-claim the Job forever. A
    fresh probe decides: it fails -> installation-wide, the caller uses the gate;
    it passes -> this Job fails with a named, actionable reason and the gate is not
    touched. Reads leave a durable block (an upload or its proof is left to
    reconcile); a refused write stored nothing and is journaled as a no-write
    outcome, so the file is simply retried by the ordinary flow once fixed.
    """
    if job["action"] != "upload":
        return False
    from .services import upload_provenance as provenance
    try:
        reason = provenance.permission_denied_reason(exc, job["s3_bucket"])
    except Exception:
        return False
    if reason is None:
        return False
    if reason == "write_permission_denied" and getattr(exc, "upload_abort_refused", False):
        # The write was refused AND so was the cleanup abort: nothing was committed,
        # but the incomplete parts are still stored. Say so (no durable block: the
        # intent journaled the no-write outcome, the bucket lifecycle reaps the parts).
        reason = "abort_permission_denied"
    if reason not in {"write_permission_denied", "abort_permission_denied"}:
        _record_upload_reconciliation_block(job, reason)
    set_job(job["id"], "failed", str(provenance.UploadBlocked(reason)))
    return True


def purge_current_snapshot_superseded(
    job: dict[str, Any], *, keep_version_id: str, client: Any,
) -> int:
    """Upload and rename share exact durable per-VersionId authority."""
    import sys
    from .services import snapshot_cleanup, upload_provenance
    try:
        inventory_job_id = snapshot_cleanup.cleanup(sys.modules[__name__], job, keep_version_id, client)
    except upload_provenance.UploadBlocked as exc:
        _record_upload_reconciliation_block(job, exc.reason)
        raise
    except (OperationCancelled, SourceVolumeUnavailable):
        raise
    except Exception:
        _record_upload_reconciliation_block(job, "cleanup_unavailable")
        raise
    with db() as connection:
        notification_service.set_problem(connection, key=f"upload_reconciliation:{job['vault_file_id']}", event="upload_reconciliation", vault_id=job["vault_id"], active=False, email=False)
    return inventory_job_id


def _purge_current_snapshot_scan_extras(
    vault: dict[str, Any], extras: list[tuple[str, str, str, str]], client: Any,
) -> None:
    """Discovery has no claim or delete authority. Offer claimed reconciliation.

    The worker's common gate can prove historical duplicates by exact GET, but
    scans never delete extras by newest/catalog/origin. #459 later admits work.
    """
    for path, _version, _key, keeper_id in extras:
        with db() as connection:
            row = connection.execute("SELECT vault_file_id FROM archive_versions WHERE id=%s", (keeper_id,)).fetchone()
        if row:
            _record_upload_reconciliation_block({"vault_id": vault["id"], "vault_file_id": row["vault_file_id"], "path": path}, "proof_unavailable")


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
        recovered_at=timestamp,
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
    if vault_moves.load(connection, int(job["id"])):
        return "requeued" if _reconcile_job_transition(
            connection, job_id=int(job["id"]), status="queued",
            message="Vault move interrupted; exact-version ledger will resume",
            timestamp=timestamp,
        ) else "skipped"
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


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def observe_stuck_lease_jobs() -> tuple[int, float]:
    """Count intermediate Jobs whose lease is already expired or missing.

    In-flight work with a live lease is excluded so this gauge stays distinct
    from both ``queue_depth`` and ordinary concurrency. Age is seconds since
    the oldest expired lease (or last update when no expiry was stored).
    """
    timestamp = now_iso()
    now = datetime.now(timezone.utc)
    with db() as connection:
        row = connection.execute(
            f"""
            SELECT COUNT(*) AS total,
                   MIN(j.claim_expires_at) AS oldest_expiry,
                   MIN(j.updated_at) AS oldest_update
            FROM jobs j
            WHERE {_INTERRUPTED_JOB_SQL}
              AND (j.claim_token IS NULL OR j.claim_expires_at IS NULL
                   OR j.claim_expires_at <= %s)
            """,
            (timestamp,),
        ).fetchone()
    count = int(row["total"] or 0) if row is not None else 0
    if count <= 0:
        return 0, 0.0
    stamp = _parse_iso_datetime(
        (row or {}).get("oldest_expiry") or (row or {}).get("oldest_update")
    )
    if stamp is None:
        return count, 0.0
    return count, max(0.0, (now - stamp).total_seconds())


def observe_failed_job_backlog() -> tuple[int, float]:
    """Count durable failed Jobs so scrapers see pre-restart failures."""
    now = datetime.now(timezone.utc)
    with db() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS total, MIN(updated_at) AS oldest_update
            FROM jobs
            WHERE status='failed'
            """
        ).fetchone()
    count = int(row["total"] or 0) if row is not None else 0
    if count <= 0:
        return 0, 0.0
    stamp = _parse_iso_datetime((row or {}).get("oldest_update"))
    if stamp is None:
        return count, 0.0
    return count, max(0.0, (now - stamp).total_seconds())


def reconcile_interrupted_jobs() -> dict[str, int]:
    """Reconcile expired or legacy durable worker states."""
    with _defer_job_counters():
        return _reconcile_interrupted_jobs_impl()


def _reconcile_interrupted_jobs_impl() -> dict[str, int]:
    """Reconcile expired or legacy durable worker states.

    A live lease is authoritative even when another process is sweeping.  The
    explicit action/state matrix covers every operation state which can be
    persisted mid-I/O; waiting states (retrying, pending approval/delay, and
    Glacier restoring) remain scheduler-owned and are not reset.  Each call
    is bounded so a large backlog of expired leases cannot monopolize a poll.
    """
    summary = {"completed": 0, "requeued": 0, "failed": 0}
    local_actions = {"recover", "upload", "rename", "free-space"}
    seen_purge_groups: set[tuple[int, str]] = set()
    with db() as connection:
        timestamp = now_iso()
        jobs = connection.execute(
            f"""
            SELECT j.*, v.source_root, v.s3_bucket
            FROM jobs j
            JOIN vaults v ON v.id=j.vault_id
            WHERE {_INTERRUPTED_JOB_SQL}
              AND (j.claim_token IS NULL OR j.claim_expires_at IS NULL
                   OR j.claim_expires_at <= %s)
            ORDER BY j.requested_at ASC, j.id ASC
            LIMIT %s
            """,
            (timestamp, EXPIRED_LEASE_RECONCILE_BATCH),
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
        # Verification reports absolute read counters.
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


def remove_local_copies(vault: dict[str, Any], logical_path: str) -> list[Path]:
    """Delete the source copy after the caller has verified the cloud copy."""
    target = safe_local_entry_path(vault["source_root"], logical_path)
    if not target.exists():
        return []
    if not target.is_file():
        raise RuntimeError(f"The local path is not a file: {target}")
    target.unlink()
    return [target]


class SourceVolumeUnavailable(RuntimeError):
    """The Source Volume cannot be read right now; this is not an identity change."""


def _require_upload_source_identity(job: dict[str, Any]) -> None:
    """Source Volume/root I/O is outside replayable DB transactions.

    ``identity_changed`` is reserved for a root that was observed and differs
    from the enrolled one. An unreadable/unavailable volume waits (#486).
    """
    from .services.upload_provenance import UploadBlocked
    ensure_job_active(job["id"], "Upload source claim lost")
    with db() as connection:
        vault = connection.execute("SELECT * FROM vaults WHERE id=%s", (job["vault_id"],)).fetchone()
    if not vault or vault["source_root"] != job["source_root"]:
        raise UploadBlocked("identity_changed")
    if not source_layout.vault_local_access(vault["source_root"]).local_operations_allowed:
        raise SourceVolumeUnavailable("Source Volume is unavailable")
    try:
        _validate_enrolled_scan_root(vault)
    except _ScanRootUnreadable as exc:
        raise SourceVolumeUnavailable(str(exc)) from exc
    except Exception as exc:
        raise UploadBlocked("identity_changed") from exc


def upload_s3_file(job: dict[str, Any], *, source: Path, key: str) -> dict[str, Any]:
    _require_upload_source_identity(job)
    reporter = _ThrottledByteProgress(job)
    limit = _runtime_settings().bandwidth_limit_kibps
    from .services.upload_provenance import provider_identity, UploadBlocked
    read_client, write_client = s3_client(), s3_client(non_idempotent=True)
    identity = provider_identity(read_client)
    if provider_identity(write_client) != identity or job.get("_upload_provider", identity) != identity:
        raise UploadBlocked("identity_changed")
    response = upload_file(
        read_client, source=source, bucket=job["s3_bucket"], key=key,
        write_client=write_client,
        check_active=lambda: ensure_job_active(job["id"], "Upload stopped"),
        on_progress=reporter.add,
        max_bandwidth=int(limit) * 1024 if limit else None,
        snapshot=job.get("_upload_snapshot"),
        metadata=job.get("_upload_metadata"),
        on_acknowledged=job.get("_on_upload_acknowledged"),
        on_multipart=job.get("_on_upload_multipart"),
    )
    reporter.flush()
    return response


def _stream_plaintext_for_verification(
    job: dict[str, Any], *, target: dict[str, Any], source_size: int,
) -> tuple[str, int]:
    """Hash only the exact VersionId returned by the upload, never current key."""
    version_id = target.get("provider_version_id")
    key = target.get("object_key")
    if not version_id or not key:
        raise RuntimeError("An exact Archive Version is required for verification")
    ensure_job_active(job["id"], "Upload stopped")
    response = s3_client().get_object(Bucket=job["s3_bucket"], Key=key, VersionId=version_id)
    body = response["Body"]
    digest = hashlib.sha256()
    size = 0
    progress = job_progress_callback(job, minimum_bytes=source_size)
    try:
        if str(response.get("VersionId") or "") != str(version_id):
            raise RuntimeError("S3 returned a different verification VersionId")
        while chunk := body.read(1024 * 1024):
            ensure_job_active(job["id"], "Upload stopped")
            digest.update(chunk)
            size += len(chunk)
            progress(size)
    finally:
        body.close()
    ensure_job_active(job["id"], "Upload stopped")
    return digest.hexdigest(), size


def _record_verification_failure(reason: str) -> None:
    """Record only bounded, non-sensitive verification failure categories."""
    try:
        metrics_service.inc("verification_failures_total", reason=reason)
    except Exception:
        # Metrics are observability only and must never change Job durability.
        pass


def _inspect_local_copy_for_upload_snapshot(
    job: dict[str, Any],
    *,
    source: Path,
    upload_digest: str,
    local_digest: str | None,
    local_stat: os.stat_result | None,
) -> tuple[str | None, os.stat_result | None, bool]:
    """Inspect the current Local Copy without holding a catalog transaction.

    Returns ``(digest, stat, matches_snapshot_a)``. A missing or unstable file
    yields ``(None, None, False)`` so snapshot A can still complete.
    """
    if local_digest == upload_digest and local_stat is not None:
        if _local_stat_unchanged(source, local_stat):
            return local_digest, local_stat, True
    if (
        local_digest
        and local_digest != upload_digest
        and local_stat is not None
        and _local_stat_unchanged(source, local_stat)
    ):
        return local_digest, local_stat, False
    try:
        current = source.stat(follow_symlinks=False)
    except OSError:
        return None, None, False
    if not stat.S_ISREG(current.st_mode):
        return None, None, False
    expected_size = job.get("total_bytes")
    if expected_size is not None and int(current.st_size) != int(expected_size):
        return None, current, False
    try:
        digest, hashed_stat = hash_stable_regular_file(source)
    except OperationCancelled:
        raise
    except Exception:
        return None, current, False
    return digest, hashed_stat, digest == upload_digest


def _complete_verified_upload(
    job: dict[str, Any],
    *,
    source: Path,
    version_id: str,
    upload_digest: str,
    mark_verified: bool,
    local_digest: str | None = None,
    local_stat: os.stat_result | None = None,
) -> None:
    """Persist Integrity for snapshot A without claiming a later Local Copy."""
    _require_upload_source_identity(job)
    with db() as connection:
        local_before = connection.execute("SELECT size,mtime_ns,plaintext_sha256,matched_archive_version_id,observed_at,content_observed_at FROM local_copies WHERE vault_file_id=%s", (job["vault_file_id"],)).fetchone()
    observed_digest, observed_stat, matches_snapshot = (
        _inspect_local_copy_for_upload_snapshot(
            job,
            source=source,
            upload_digest=upload_digest,
            local_digest=local_digest,
            local_stat=local_stat,
        )
    )
    # Filesystem stability is observed outside the DB replay. A newer catalog
    # observation can still win; the DB CAS below never overwrites that generation.
    local_observation_stable = observed_stat is not None and _local_stat_unchanged(source, observed_stat)
    verified_at = now_iso()

    @retry_database_unit
    def persist_verification() -> None:
        with owned_connection(db()) as connection:
            from .services import upload_provenance as provenance
            provenance.target(connection, job, key=expected_cloud_key(job["path"], job["s3_prefix"]), provider="db-fence")
            ensure_job_claim_owned_in_transaction(connection, job, "Upload claim was lost")
            catalog = ArchiveCatalog(connection)
            if mark_verified:
                catalog.mark_version_verified(
                    version_id, plaintext_sha256=upload_digest, verified_at=verified_at,
                )
            local_current = connection.execute("SELECT size,mtime_ns,plaintext_sha256,matched_archive_version_id,observed_at,content_observed_at FROM local_copies WHERE vault_file_id=%s FOR UPDATE", (job["vault_file_id"],)).fetchone()
            publish_local = local_observation_stable and local_current == local_before
            if publish_local and observed_stat is not None:
                catalog.observe_local_copy(
                    vault_id=job["vault_id"], path=job["path"], file_type="regular",
                    size=observed_stat.st_size, mtime_ns=observed_stat.st_mtime_ns,
                    observed_at=verified_at,
                )
            if publish_local and observed_digest is not None:
                catalog.set_local_fingerprint(
                    vault_id=job["vault_id"], path=job["path"],
                    plaintext_sha256=observed_digest,
                    matched_archive_version_id=version_id if matches_snapshot else None,
                )
                if matches_snapshot:
                    connection.execute("DELETE FROM protection_pending WHERE vault_file_id=%s",
                                       (job["vault_file_id"],))

    persist_verification()
    set_job(job["id"], "uploading", message_key="job.purging_previous_snapshot")
    ensure_job_active(job["id"], "Upload claim was lost")
    inventory_job_id = purge_current_snapshot_superseded(
        job,
        keep_version_id=str(version_id),
        client=s3_client(),
    )
    from .services import upload_remediation
    _upload_catalog_unit(job, lambda catalog: upload_remediation.record_cleanup(
        catalog.connection, job["id"], inventory_job_id,
    ))
    progress_bytes = int(job.get("total_bytes") or 0)
    if local_stat is not None and progress_bytes <= 0:
        progress_bytes = int(local_stat.st_size)
    set_job_progress(job["id"], progress_bytes)
    set_job(job["id"], "completed", message_key="job.upload_verified")


@retry_database_unit
def _automatic_upload_ready(job: dict[str, Any]) -> bool:
    """Recheck suspension and stability when a queued automatic upload starts.

    Queue time is not authority to upload later edits. Linked versions must
    finish verification/reconciliation even if automation was subsequently paused.
    """
    with owned_connection(db()) as connection:
        target = ArchiveCatalog(connection).get_job_target(job["id"])
        if target and target.get("archive_version_id"):
            return True
        from .services import upload_provenance as provenance
        admitted = provenance.load_for_job(connection, job["id"])
        if admitted and admitted["send_started"]:
            return True
        policy = get_policy(connection, int(job["vault_id"]))
        vault = connection.execute("SELECT * FROM vaults WHERE id=%s", (job["vault_id"],)).fetchone()
        if not vault or not vault["enabled"] or not policy.auto_upload:
            return False
        if vault["source_root"] != job["source_root"]:
            return False
        if not source_layout.vault_local_access(vault["source_root"]).local_operations_allowed:
            return False
        if not auto_upload_path_is_included(job["path"], policy) or not target:
            return False
        try:
            # Files can disappear, lose access or be replaced after admission.
            # Suspend only this candidate; no DB work belongs in this catch.
            _validate_enrolled_scan_root(vault)
            source = safe_local_path(job["source_root"], job["path"])
            stat_result = source.stat()
        except (OSError, ValueError, RuntimeError):
            return False
        if not stat.S_ISREG(stat_result.st_mode):
            return False
        if (stat_result.st_size, stat_result.st_mtime_ns) != (
            target["local_size"], target["local_mtime_ns"]
        ):
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=int(job["vault_id"]), path=job["path"], file_type="regular",
                size=stat_result.st_size, mtime_ns=stat_result.st_mtime_ns, observed_at=now_iso(),
            )
            return False
        return file_is_stable(
            mtime_ns=stat_result.st_mtime_ns, now=datetime.fromisoformat(now_iso()),
            stability_seconds=policy.stability_seconds,
            content_observed_at=target["content_observed_at"],
        )


@retry_database_unit
def _upload_catalog_unit(
    job: dict[str, Any], mutation: Callable[[ArchiveCatalog], Any],
) -> Any:
    """One owned, replayable transaction; callbacks must contain DB work only."""
    from .services import upload_provenance as provenance
    with owned_connection(db()) as connection:
        # Same Vault-first lock order as admission/cleanup; this is DB-only.
        provenance.target(connection, job, key=expected_cloud_key(job["path"], job["s3_prefix"]), provider="db-fence")
        ensure_job_claim_owned_in_transaction(connection, job, "Upload claim was lost")
        return mutation(ArchiveCatalog(connection))


def _publish_upload_receipt(job: dict[str, Any], intent: dict[str, Any], response: dict[str, Any]) -> str:
    from .services import upload_provenance as provenance
    _require_upload_source_identity(job)
    pinned = json.loads(intent["target_json"])
    snapshot = UploadSnapshot.from_json(intent["snapshot_json"])
    provider_id = response.get("VersionId")
    if not provider_id or provider_id == "null" or provider_id in json.loads(intent["inventory_json"])["versions"]:
        raise provenance.UploadBlocked("ambiguous_receipt")
    stamp = now_iso()
    def publish(catalog: ArchiveCatalog) -> str:
        provenance.require_target(catalog.connection, job, pinned)
        catalog.connection.execute("SELECT id FROM vault_files WHERE id=%s FOR UPDATE", (job["vault_file_id"],))
        recorded = catalog.record_archive_version(
            vault_id=job["vault_id"], vault_file_id=job["vault_file_id"], path=job["path"],
            object_key=pinned["key"], provider_version_id=provider_id, size=snapshot.size,
            storage_class=response.get("StorageClass", "STANDARD"),
            etag=str(response.get("ETag") or "").strip('"'),
            uploaded_at=intent["created_at"], observed_at=stamp, scan_id=stamp, origin="upload")
        catalog.set_version_upload_snapshot(recorded, snapshot_json=intent["snapshot_json"], requested=intent["requested"])
        catalog.set_upload_snapshot(job["id"], snapshot_json=intent["snapshot_json"])
        catalog.link_job_version(job["id"], recorded)
        catalog.connection.execute("UPDATE upload_intents SET archive_version_id=%s WHERE id=%s AND (archive_version_id IS NULL OR archive_version_id=%s)", (recorded, intent["id"], recorded))
        # The version exists (e.g. Complete reached S3 but its response was lost): the
        # multipart upload is no longer an obligation of this intent (#491).
        provenance.close_multiparts(catalog.connection, intent["id"], stamp)
        # A scan may have observed this exact write before its receipt was
        # published. Only this owner/intent-fenced publication promotes its
        # observation ordering; generic discovery never grants current trust.
        catalog.connection.execute("""UPDATE archive_versions av SET
            version_number=(SELECT COALESCE(MAX(v.version_number),0)+1
                FROM archive_versions v WHERE v.vault_file_id=av.vault_file_id),
            origin='upload', uploaded_at=%s
            WHERE av.id=%s AND av.vault_file_id=%s AND av.origin='discovered'
              AND EXISTS (SELECT 1 FROM upload_intents i WHERE i.id=%s
                  AND i.archive_version_id=av.id AND i.vault_file_id=av.vault_file_id
                  AND i.send_started AND i.snapshot_json=av.upload_snapshot_json)""",
            (intent["created_at"], recorded, job["vault_file_id"], intent["id"]))
        return recorded
    return _upload_catalog_unit(job, publish)


def _upload_protection_paused(job: dict[str, Any]) -> bool:
    from .services.operation_policies import get_policy, auto_upload_path_is_included
    with db() as connection:
        policy = get_policy(connection, int(job["vault_id"]))
    return not policy.auto_upload or not auto_upload_path_is_included(job["path"], policy)


def _rearm_after_proven_absence(job: dict[str, Any], intent: dict[str, Any]) -> None:
    """Claim-fenced DB-only unit; no provider I/O. Never reached with a receipt."""
    from .services import upload_provenance as provenance, upload_remediation
    def close(catalog: ArchiveCatalog) -> None:
        connection = catalog.connection
        stamp = now_iso()
        provenance.record_no_write(connection, job, intent, provenance.ABSENT_OPERATION, stamp)
        provenance.close_multiparts(connection, intent["id"], stamp)
        connection.execute("UPDATE upload_intents SET blocked_reason=NULL WHERE id=%s", (intent["id"],))
        connection.execute("DELETE FROM snapshot_reconciliation_blocks WHERE vault_file_id=%s", (job["vault_file_id"],))
        upload_remediation.release_for_rearm(connection, job, intent["id"], stamp)
    _upload_catalog_unit(job, close)
    # The remediation attempt is now this file's ordinary upload; the exact
    # intent, token and frozen snapshot are reused, so no new authority is minted.
    job["upload_reconcile_only"] = False
    @retry_database_unit
    def clear_problem() -> None:
        with owned_connection(db()) as connection:
            notification_service.set_problem(connection, key=f"upload_reconciliation:{job['vault_file_id']}", event="upload_reconciliation", vault_id=job["vault_id"], active=False, email=False)
    clear_problem()


def _resume_materialized_upload(job: dict[str, Any], intent: dict[str, Any], client: Any) -> None:
    from .services import upload_provenance as provenance
    pinned = json.loads(intent["target_json"])
    try:
        _upload_catalog_unit(job, lambda catalog: provenance.require_target(catalog.connection, job, pinned))
        with db() as connection:
            linked = ArchiveCatalog(connection).get_job_target(job["id"])
            raw = provenance.receipt(connection, intent["id"])
        # A crash after publication but before proof evaluation must not turn
        # a durable contradictory receipt into a mere identity check on retry.
        # This also applies to already-verified cleanup-only continuations.
        if raw is not None:
            try:
                validated_s3_proof(raw, version_id=raw["VersionId"],
                    snapshot=UploadSnapshot.from_json(intent["snapshot_json"]),
                    source="upload_response", completion=True)
            except UploadProofMismatch as exc:
                raise provenance.UploadBlocked("proof_mismatch") from exc
        if intent["archive_version_id"]:
            with db() as connection:
                exact = connection.execute("SELECT * FROM archive_versions WHERE id=%s AND vault_file_id=%s AND vault_id=%s", (intent["archive_version_id"], job["vault_file_id"], job["vault_id"])).fetchone()
            if (not exact or not raw or exact["object_key"] != pinned["key"]
                    or exact["provider_version_id"] != raw["VersionId"]
                    or exact["upload_snapshot_json"] != intent["snapshot_json"]
                    or (linked and linked["archive_version_id"] and str(linked["archive_version_id"]) != str(intent["archive_version_id"]))):
                raise provenance.UploadBlocked("ambiguous_receipt")
            if not linked or not linked["archive_version_id"]:
                _upload_catalog_unit(job, lambda catalog: catalog.link_job_version(job["id"], intent["archive_version_id"]))
            # VERSION baseline remains immutable, including cleanup-only retries.
            return
        try:
            with db() as connection:
                owned = provenance.open_multiparts(connection, intent["id"])
            response = provenance.reconcile(
                client, intent, raw, lambda: ensure_job_active(job["id"], "Upload reconciliation stopped"),
                owned_multiparts=owned,
                on_multipart_aborted=lambda upload_id: provenance.close_multipart(db, intent["id"], upload_id, now_iso()))
        except provenance.ProvenAbsent:
            # Complete S3 reads prove the journaled send never landed (#484):
            # close the intent as a definitive no-write outcome and let this
            # Job send again from its immutable snapshot, no manual unblock.
            # A reconcile-only attempt (remediation / explicit reconcile) never
            # starts an upload on a path whose automatic protection is paused:
            # stay blocked until it is resumed. An ordinary upload (a manual one
            # included) is the user's own explicit send and is never gated here.
            if job.get("upload_reconcile_only") and _upload_protection_paused(job):
                raise provenance.UploadBlocked("protection_paused") from None
            _rearm_after_proven_absence(job, intent)
            return
        provenance.append_receipt(db, intent["id"], response, now_iso())
        snapshot = UploadSnapshot.from_json(intent["snapshot_json"])
        try:
            if raw is not None:
                validated_s3_proof(raw, version_id=response["VersionId"], snapshot=snapshot, source="upload_response", completion=True)
            validated_s3_proof(response, version_id=response["VersionId"], snapshot=snapshot, source="exact_head")
        except UploadProofMismatch as exc:
            raise provenance.UploadBlocked("proof_mismatch") from exc
        _publish_upload_receipt(job, intent, response)
    except provenance.UploadBlocked as exc:
        _record_upload_reconciliation_block(job, exc.reason)
        raise


def process_upload(job: dict[str, Any]) -> None:
    ensure_job_active(job["id"], "Upload stopped")
    if job.get("origin") == "automatic" and not _automatic_upload_ready(job):
        set_job(job["id"], "queued", "Automatic upload waits for enabled protection and stable content")
        return
    validate_cloud_vault(job)
    _require_upload_source_identity(job)
    # The enrolled root was read again: an outage problem raised by a waiting Job ends.
    _clear_source_volume_problem(job)
    source = safe_local_path(job["source_root"], job["path"])
    local_present = source.is_file()
    from .services import upload_provenance as provenance
    def load_intent(catalog: ArchiveCatalog) -> dict[str, Any] | None:
        intent = provenance.load_for_job(catalog.connection, job["id"])
        if intent is None:
            intent = provenance.outstanding(catalog.connection, job["vault_file_id"])
            if intent:
                provenance.bind_for_resume(catalog.connection, job["id"], intent)
        return intent
    intent = _upload_catalog_unit(job, load_intent)
    import sys
    from .services import snapshot_cleanup
    if intent and intent["send_started"]:
        client = s3_client()
        snapshot_cleanup.pin_inventory(sys.modules[__name__], job, client)
        _resume_materialized_upload(job, intent, client)
    linked_target = _upload_catalog_unit(job, lambda catalog: catalog.get_job_target(job["id"]))
    linked_version_id = linked_target.get("archive_version_id") if linked_target else None
    if job.get("upload_reconcile_only") and not linked_version_id:
        raise provenance.UploadBlocked("ambiguous_send")
    if not local_present and not linked_version_id:
        raise RuntimeError("The file is no longer available in the source folder")
    if not linked_version_id and linked_target and linked_target.get(
        "cloud_only_version_id"
    ):
        # A Job queued before the path became cloud-only must not replace
        # the only cloud copy; the owner decides (#411, #363 D7).
        raise RuntimeError(
            "Upload held back: the path holds different content than a file "
            "kept only in the cloud"
        )
    verification_started = False
    verification_failure_reason: str | None = None
    local_plaintext_sha256: str | None = None
    source_stat: os.stat_result | None = None
    snapshot: UploadSnapshot | None = None
    requested_mode = str(intent["requested"] if intent else (linked_target or {}).get("job_upload_verification_requested") or "read_back")

    def version_head() -> dict[str, Any]:
        head = _verification_version_head(client,job,target)
        token = (head.get("Metadata") or {}).get(provenance.TOKEN_KEY)
        if token:
            with db() as connection:
                known = connection.execute("SELECT * FROM upload_intents WHERE archive_version_id=%s AND vault_file_id=%s AND token=%s", (target["archive_version_id"],job["vault_file_id"],token)).fetchone()
            if not known:
                raise provenance.UploadBlocked("ambiguous_receipt")
            scope = json.loads(known["target_json"])
            if scope["provider"] != provenance.provider_identity(client) or scope["bucket"] != job["s3_bucket"] or scope["key"] != target["object_key"]:
                raise provenance.UploadBlocked("identity_changed")
        return head

    def evaluate_provider_proof(proof: dict[str, Any], *, source: str,
                                completion: bool = False) -> dict[str, str] | None:
        # Every evidence origin, including the post-read HEAD, obeys the same
        # contradiction policy and records one verification-failure metric.
        nonlocal verification_started, verification_failure_reason
        assert snapshot is not None
        verification_started = True
        try:
            return validated_s3_proof(
                proof, version_id=target["provider_version_id"],
                snapshot=snapshot, source=source, completion=completion,
            )
        except UploadProofMismatch:
            verification_failure_reason = "mismatch"
            _upload_catalog_unit(job, lambda catalog: catalog.mark_version_mismatch(
                version_id, plaintext_sha256=upload_digest, checked_at=now_iso(),
            ))
            raise

    try:
        if not linked_version_id:
            if not local_present:
                raise RuntimeError(
                    "The file is no longer available in the source folder"
                )
            set_job(job["id"], "uploading", message_key="job.hashing_local_file")
            snapshot, source_stat = prepare_upload_snapshot(
                source, lambda: ensure_job_active(job["id"], "Upload claim was lost"),
            )
            local_plaintext_sha256 = snapshot.plaintext_sha256
            old_snapshot = (linked_target or {}).get("job_upload_snapshot_json")
            if old_snapshot is not None and old_snapshot != snapshot.as_json():
                raise RuntimeError("Local file changed since fingerprinting")
            ensure_job_active(job["id"], "Upload claim was lost")
            # Preflight and hashing can take time. A watcher edit in between
            # must earn a fresh stability window before the first cloud write.
            if job.get("origin") == "automatic" and not _automatic_upload_ready(job):
                set_job(job["id"], "queued", "Automatic upload waits for stable content")
                return

        version_id: str
        target: dict[str, Any]
        head: dict[str, Any]
        client = s3_client()
        snapshot_cleanup.pin_inventory(sys.modules[__name__], job, client)
        if linked_version_id:
            # A retry after catalog linkage is verification-only. Uploading
            # again here would create a second provider VersionId and split the
            # durable Archive Version history. The persisted upload digest
            # identifies snapshot A; a later Local Copy is archived separately.
            target = dict(linked_target)
            version_id = str(linked_version_id)
            integrity = str(target.get("integrity") or "unverified")
            upload_digest = _require_linked_upload_digest(target)
            if integrity == "mismatch":
                raise RuntimeError("The linked Archive Version already mismatches the Local Copy")
            if target.get("availability") not in {None, "available"}:
                raise RuntimeError("The linked Archive Version is no longer available")
            if not target.get("object_key") or not target.get("provider_version_id"):
                raise RuntimeError("The linked Archive Version cannot be verified safely")
            if target.get("upload_snapshot_json") is not None:
                snapshot = UploadSnapshot.from_json(target["upload_snapshot_json"])
                requested_mode = target.get("upload_verification_requested") or "read_back"
                if snapshot.plaintext_sha256 != upload_digest:
                    raise RuntimeError("Upload Snapshot digest differs from durable upload digest")
            head = version_head()
            if integrity == "verified":
                if snapshot is not None:
                    evaluate_provider_proof(head, source="ordinary_head")
                _complete_verified_upload(
                    job,
                    source=source,
                    version_id=version_id,
                    upload_digest=upload_digest,
                    mark_verified=False,
                )
                return
        else:
            if local_plaintext_sha256 is None:
                raise RuntimeError(
                    "The file is no longer available in the source folder"
                )
            upload_digest = local_plaintext_sha256
            # Keep the upload snapshot on the durable Job before contacting the
            # provider.  Local Copy fingerprints are mutable scan observations
            # and cannot identify a retry after the source changes.
            reuse_provider = provenance.provider_identity(client)
            def prepare_digest(catalog: ArchiveCatalog) -> dict[str, Any] | None:
                assert snapshot is not None
                catalog.set_upload_snapshot(job["id"], snapshot_json=snapshot.as_json())
                vault_file_id = job.get("vault_file_id") or (linked_target or {}).get("vault_file_id")
                return catalog.find_reusable_upload_version(
                    vault_file_id=str(vault_file_id), plaintext_sha256=upload_digest,
                    bucket=job["s3_bucket"], provider=reuse_provider,
                ) if vault_file_id else None

            reusable = _upload_catalog_unit(job, prepare_digest)
            job["upload_plaintext_sha256"] = upload_digest
            reusable_integrity = (
                str(reusable.get("integrity") or "") if reusable is not None else ""
            )
            if reusable is not None and reusable_integrity == "verified":
                version_id = str(reusable["id"])
                _upload_catalog_unit(job, lambda catalog: catalog.link_job_version(job["id"], version_id))
                target = _upload_catalog_unit(job, lambda catalog: catalog.get_job_target(job["id"]))
                # A cleanup-only reuse retains the VERSION's original baseline,
                # never today's newly hashed multipart layout or verification mode.
                if target.get("upload_snapshot_json") is not None:
                    snapshot = UploadSnapshot.from_json(target["upload_snapshot_json"])
                    if snapshot.plaintext_sha256 != upload_digest:
                        raise RuntimeError("Upload Snapshot digest differs from durable upload digest")
                    evaluate_provider_proof(
                        version_head(), source="ordinary_head",
                    )
                _complete_verified_upload(
                    job,
                    source=source,
                    version_id=version_id,
                    upload_digest=upload_digest,
                    mark_verified=False,
                    local_digest=local_plaintext_sha256,
                    local_stat=source_stat,
                )
                return
            if reusable is not None and reusable_integrity == "unverified":
                if not reusable.get("object_key") or not reusable.get(
                    "provider_version_id"
                ):
                    raise RuntimeError(
                        "The linked Archive Version cannot be verified safely"
                    )
                version_id = str(reusable["id"])
                _upload_catalog_unit(job, lambda catalog: catalog.link_job_version(job["id"], version_id))
                target = {
                    "archive_version_id": version_id,
                    "object_key": reusable["object_key"],
                    "provider_version_id": reusable["provider_version_id"],
                    "cloud_size": reusable["cloud_size"],
                    "upload_plaintext_sha256": upload_digest,
                    "integrity": "unverified",
                    "availability": reusable.get("availability") or "available",
                    "upload_snapshot_json": reusable.get("upload_snapshot_json"),
                    "upload_verification_requested": reusable.get("upload_verification_requested"),
                }
                # Reusing a prior intent never substitutes today's Local Copy
                # or policy for its historical expected evidence.
                snapshot = UploadSnapshot.from_json(target["upload_snapshot_json"]) if target.get("upload_snapshot_json") else None
                requested_mode = target.get("upload_verification_requested") or "read_back"
                head = version_head()
            else:
                set_job(job["id"], "uploading", message_key="job.plain_upload")
                key = expected_cloud_key(job["path"], job["s3_prefix"])
                assert snapshot is not None
                provider = provenance.provider_identity(client)
                with db() as connection:
                    pinned = provenance.target(connection, job, key=key, provider=provider)
                before = provenance.inventory(client, job["s3_bucket"], key)
                if before != provenance.inventory(client, job["s3_bucket"], key):
                    raise provenance.UploadBlocked("inventory_changed")
                intent = _upload_catalog_unit(job, lambda catalog: provenance.prepare(
                    catalog.connection, job, pinned=pinned, snapshot=snapshot,
                    requested=requested_mode, before=before, timestamp=now_iso()))
                if intent["snapshot_json"] != snapshot.as_json() or intent["inventory_json"] != provenance.canonical(before):
                    raise provenance.UploadBlocked("identity_changed")
                # This transaction commits BEFORE PutObject/CreateMultipartUpload.
                _upload_catalog_unit(job, lambda catalog: provenance.mark_send_started(catalog.connection, job, intent))

                def acknowledge(response: dict[str, Any]) -> None:
                    provenance.append_receipt(db, intent["id"], response, now_iso())

                def journal_multipart(event: str, upload_id: str) -> None:
                    # "created" commits before s3transfer sees the UploadId (#491).
                    if event == "created":
                        provenance.journal_multipart(db, intent["id"], upload_id, now_iso())
                    else:
                        provenance.close_multipart(db, intent["id"], upload_id, now_iso())

                transfer_job = {**job, "_upload_snapshot": snapshot, "_upload_provider": provider,
                                "_upload_metadata": {provenance.TOKEN_KEY: intent["token"]},
                                "_on_upload_acknowledged": acknowledge,
                                "_on_upload_multipart": journal_multipart}
                try:
                    response = upload_s3_file(transfer_job, source=source, key=key)
                except BaseException as exc:
                    # Shutdown joins late acknowledgments, even after cancellation.
                    # Retry only appending the retained fact, never the provider send.
                    raw = getattr(exc, "upload_response", None)
                    if raw is not None:
                        acknowledge(raw)
                    no_write = getattr(exc,'upload_no_write_operation',None)
                    if raw is None and no_write and cloud_preflight_gate.is_readiness_error(exc):
                        # Definitive refusal is journaled before rearming send.
                        # All ambiguous/lost-response outcomes stay reconcile-only.
                        _upload_catalog_unit(job,lambda catalog: provenance.record_no_write(
                            catalog.connection,job,intent,no_write,now_iso()))
                    raise
                acknowledge(response)
                version_id = _publish_upload_receipt(job, intent, response)
                target = {
                    "archive_version_id": version_id, "object_key": key,
                    "provider_version_id": response["VersionId"], "cloud_size": snapshot.size,
                    "upload_plaintext_sha256": upload_digest,
                    "upload_snapshot_json": snapshot.as_json(),
                    "upload_verification_requested": requested_mode,
                    "integrity": "unverified", "availability": "available",
                }
                # Present contradictory completion evidence blocks even when
                # read_back was requested; absence alone is not a mismatch.
                target["_provider_proof"] = evaluate_provider_proof(
                    response, source="upload_response", completion=True,
                )
                head = version_head()

        if snapshot is not None:
            # A normal HEAD may also contain checksum fields. Any present
            # contradictory evidence blocks, even when full Read-back was
            # requested or completion already supplied a valid proof.
            evaluate_provider_proof(head, source="ordinary_head")
        ensure_job_active(job["id"], "Upload stopped")
        # Snapshot A is identified by the persisted upload digest. A later
        # Local Copy must not abort verification or be treated as a mismatch
        # of the already-linked Archive Version.
        set_job(job["id"], "verifying", message_key="job.verifying_cloud_copy")
        ensure_job_active(job["id"], "Upload claim was lost")
        verification_started = True
        source_size = (
            source_stat.st_size
            if source_stat is not None
            else int(job.get("total_bytes") or target.get("cloud_size") or 0)
        )
        fallback_reason = None
        checksum_proven = False
        provider_proof = target.get("_provider_proof")
        if requested_mode == "s3_checksum" and snapshot is not None:
            checksum_proven = provider_proof is not None
            if not checksum_proven:
                try:
                    proof = client.head_object(Bucket=job["s3_bucket"], Key=target["object_key"],
                                               VersionId=target["provider_version_id"], ChecksumMode="ENABLED")
                    provider_proof = evaluate_provider_proof(proof, source="exact_head")
                    checksum_proven = provider_proof is not None
                    if not checksum_proven:
                        fallback_reason = "checksum_missing"
                except Exception as exc:
                    if not unsupported_checksum_api(exc):
                        raise
                    fallback_reason = "checksum_api_unsupported"
        elif snapshot is None:
            fallback_reason = "legacy_expected_missing"
        method = "s3_checksum" if checksum_proven else "read_back"
        _upload_catalog_unit(job, lambda catalog: catalog.set_version_verification_method(
            version_id, method=method, fallback_reason=fallback_reason, egress_bytes=0 if checksum_proven else None,
            provider_proof=provider_proof if checksum_proven else None,
        ))
        if checksum_proven:
            _complete_verified_upload(
                job, source=source, version_id=version_id, upload_digest=upload_digest,
                mark_verified=True, local_digest=local_plaintext_sha256, local_stat=source_stat,
            )
            return
        if fallback_reason:
            set_job(job["id"], "verifying", message_key="job.verifying_cloud_fallback")
        remote_digest, remote_size = _stream_plaintext_for_verification(
            job, target=target, source_size=source_size,
        )
        _upload_catalog_unit(job, lambda catalog: catalog.set_version_verification_method(
            version_id, method="read_back", fallback_reason=fallback_reason, egress_bytes=remote_size,
        ))
        final_head = version_head()
        if snapshot is not None:
            evaluate_provider_proof(final_head, source="ordinary_head")
        expected_stream_size = target.get("cloud_size")
        if expected_stream_size is not None and remote_size != int(expected_stream_size):
            verification_failure_reason = "truncated"
            raise RuntimeError(
                "Cloud verification stream length did not match the Archive Version"
            )
        if remote_digest != upload_digest:
            verification_failure_reason = "mismatch"
            ensure_job_active(job["id"], "Upload claim was lost")
            _upload_catalog_unit(job, lambda catalog: catalog.mark_version_mismatch(
                version_id, plaintext_sha256=upload_digest, checked_at=now_iso(),
            ))
            _record_upload_reconciliation_block(job, "proof_mismatch")
            raise RuntimeError("Cloud copy digest does not match local file")
        _complete_verified_upload(
            job,
            source=source,
            version_id=version_id,
            upload_digest=upload_digest,
            mark_verified=True,
            local_digest=local_plaintext_sha256,
            local_stat=source_stat,
        )
    except OperationCancelled:
        raise
    except provenance.UploadBlocked as exc:
        _record_upload_reconciliation_block(job, exc.reason)
        raise
    except UploadProofMismatch:
        _record_upload_reconciliation_block(job, "proof_mismatch")
        _record_verification_failure("mismatch")
        raise
    except Exception:
        if verification_started:
            _record_verification_failure(verification_failure_reason or "stream")
        raise


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


def _head_exact_archive_version(
    client: Any,
    *,
    bucket: str,
    object_key: str,
    provider_version_id: str | None,
    operation: str,
) -> dict[str, Any]:
    """Prove one recorded S3 VersionId still exists and is not a delete marker."""
    expected_version = str(provider_version_id or "")
    if not object_key or not expected_version:
        raise RuntimeError(
            f"{operation} aborted: destination has no exact S3 VersionId"
        )
    try:
        head = client.head_object(
            Bucket=bucket,
            Key=object_key,
            VersionId=expected_version,
        )
    except Exception as exc:
        raise RuntimeError(
            f"{operation} aborted: destination VersionId {expected_version!r} "
            "is not available"
        ) from exc
    if head.get("DeleteMarker"):
        raise RuntimeError(
            f"{operation} aborted: destination VersionId {expected_version!r} "
            "is a delete marker"
        )
    observed = head.get("VersionId")
    if not observed or str(observed) != expected_version:
        raise RuntimeError(
            f"{operation} aborted: destination VersionId {observed!r} does not "
            f"match recorded Archive Version {expected_version!r}"
        )
    return head


def _iter_reusable_rename_destinations(
    rows: list[Any],
    expected_digest: str | None,
):
    expected = str(expected_digest or "")
    verified_matches: list[Any] = []
    unverified: list[Any] = []
    for row in rows:
        availability = row["availability"] or "available"
        integrity = row["integrity"] or "unverified"
        if availability != "available" or not row["provider_version_id"]:
            continue
        if integrity == "verified":
            if expected and str(row["plaintext_sha256"] or "") == expected:
                verified_matches.append(row)
        elif integrity == "unverified":
            unverified.append(row)
    yield from verified_matches
    yield from unverified


def _select_live_rename_destination(
    client: Any,
    *,
    bucket: str,
    rows: list[Any],
    expected_digest: str | None,
) -> Any | None:
    """Return a live destination version that may be reused for rename.

    Missing, purged, or different-digest verified versions never authorize
    skipping copy or hiding the old key. An unverified available version can
    resume verification of its exact VersionId.
    """
    for row in _iter_reusable_rename_destinations(rows, expected_digest):
        try:
            _head_exact_archive_version(
                client,
                bucket=bucket,
                object_key=str(row["object_key"]),
                provider_version_id=str(row["provider_version_id"]),
                operation="Rename",
            )
        except RuntimeError:
            continue
        return row
    return None


def _verify_rename_destination_bytes(
    job: dict[str, Any],
    *,
    version_id: str,
    object_key: str,
    provider_version_id: str,
    expected_digest: str,
    source: Path,
) -> None:
    """Hash the exact recorded destination VersionId before the old key is hidden."""
    set_job(job["id"], "verifying", message_key="job.rename_verifying_new_key")
    ensure_job_active(job["id"], "Rename claim was lost")
    temporary = source.with_name(
        f".{source.name}.verify-{uuid.uuid4().hex}.tmp"
    )
    try:
        download_exact_version_plaintext(
            job,
            object_key=object_key,
            provider_version_id=provider_version_id,
            temporary=temporary,
        )
        remote_digest, _ = hash_stable_regular_file(temporary)
    finally:
        temporary.unlink(missing_ok=True)
    if remote_digest != expected_digest:
        ensure_job_active(job["id"], "Rename claim was lost")
        with db() as connection:
            ensure_job_claim_owned_in_transaction(
                connection,
                job,
                "Rename claim was lost",
            )
            ArchiveCatalog(connection).mark_version_mismatch(
                version_id,
                plaintext_sha256=expected_digest,
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
            plaintext_sha256=expected_digest,
            verified_at=verified_at,
        )
        catalog.set_local_fingerprint(
            vault_id=job["vault_id"],
            path=job["path"],
            plaintext_sha256=expected_digest,
            matched_archive_version_id=version_id,
        )


def process_rename(job: dict[str, Any]) -> None:
    """Copy verified content to the new key, then hide the previous key."""
    ensure_job_active(job["id"], "Rename stopped")
    validate_cloud_vault(job)
    source = safe_local_path(job["source_root"], job["path"])
    if not source.is_file():
        raise RuntimeError("The file is no longer available in the source folder")
    with db() as connection:
        previous = connection.execute(
            """
            SELECT object_key, provider_version_id, plaintext_sha256, integrity
            FROM archive_versions
            WHERE id=%s
            """,
            (job["archive_version_id"],),
        ).fetchone()
    if previous is None:
        raise RuntimeError("Rename target Archive Version is missing")
    if previous["integrity"] != "verified" or not previous["object_key"]:
        raise RuntimeError("Rename requires a verified Archive Version at the old key")
    old_key = previous["object_key"]
    try:
        new_key = expected_cloud_key(job["path"], job["s3_prefix"])
        if new_key == old_key:
            set_job(job["id"], "completed", message_key="job.rename_key_matches")
            return

        client = s3_client()
        import sys
        from .services import snapshot_cleanup
        snapshot_cleanup.pin_inventory(sys.modules[__name__], job, client)
        with db() as connection:
            destination_rows = connection.execute(
                """
                SELECT id, plaintext_sha256, integrity, availability,
                       provider_version_id, size, object_key
                FROM archive_versions
                WHERE vault_file_id=%s
                  AND object_key=%s
                ORDER BY version_number DESC
                """,
                (job["vault_file_id"], new_key),
            ).fetchall()

        live_destination = _select_live_rename_destination(
            client,
            bucket=job["s3_bucket"],
            rows=destination_rows,
            expected_digest=previous.get("plaintext_sha256"),
        )
        version_id: str | None = None
        destination_version_id: str | None = None
        source_stat = source.stat(follow_symlinks=False)
        plaintext_sha256 = previous.get("plaintext_sha256")
        if plaintext_sha256:
            # The admitted rename pins independently verified old content, not
            # scan membership/origin. Persist that lineage for replacement cleanup.
            _upload_catalog_unit(job, lambda catalog: catalog.set_upload_plaintext_digest(job["id"], plaintext_sha256=plaintext_sha256))

        if live_destination is not None:
            version_id = str(live_destination["id"])
            destination_version_id = str(live_destination["provider_version_id"])
            if live_destination["integrity"] == "verified":
                plaintext_sha256 = live_destination["plaintext_sha256"]
            else:
                set_job(job["id"], "uploading", message_key="job.hashing_local_file")
                plaintext_sha256, source_stat = hash_stable_regular_file(source)
                if (
                    previous["plaintext_sha256"]
                    and previous["plaintext_sha256"] != plaintext_sha256
                ):
                    raise RuntimeError(
                        "Local file digest no longer matches the archived version"
                    )
                _verify_rename_destination_bytes(
                    job,
                    version_id=version_id,
                    object_key=new_key,
                    provider_version_id=destination_version_id,
                    expected_digest=str(plaintext_sha256),
                    source=source,
                )
        else:
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
            response = upload_s3_file(job, source=source, key=new_key)
            ensure_job_active(job["id"], "Rename stopped")
            provider_version_id = response.get("VersionId")
            head = client.head_object(Bucket=job["s3_bucket"], Key=new_key,
                                      VersionId=provider_version_id)
            if not provider_version_id:
                raise RuntimeError(
                    "Rename stored without an S3 VersionId; bucket Versioning is required"
                )
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
                    vault_file_id=job["vault_file_id"],
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
                )
            destination_version_id = str(provider_version_id)
            ensure_job_active(job["id"], "Rename stopped")
            after = source.stat(follow_symlinks=False)
            if (
                after.st_size != source_stat.st_size
                or after.st_mtime_ns != source_stat.st_mtime_ns
                or after.st_dev != source_stat.st_dev
                or after.st_ino != source_stat.st_ino
            ):
                raise RuntimeError("Local file changed since fingerprinting")
            _verify_rename_destination_bytes(
                job,
                version_id=str(version_id),
                object_key=new_key,
                provider_version_id=destination_version_id,
                expected_digest=str(plaintext_sha256),
                source=source,
            )

        assert version_id is not None and destination_version_id is not None
        ensure_job_active(job["id"], "Rename stopped")
        _head_exact_archive_version(
            client,
            bucket=job["s3_bucket"],
            object_key=new_key,
            provider_version_id=destination_version_id,
            operation="Rename",
        )
        set_job(
            job["id"], "cleaning", message_key="job.purging_previous_snapshot",
        )
        ensure_job_active(job["id"], "Rename claim was lost")
        purge_current_snapshot_superseded(
            job,
            keep_version_id=str(version_id),
            client=client,
        )
        with db() as connection:
            ensure_job_claim_owned_in_transaction(
                connection, job, "Rename claim was lost",
            )
            ArchiveCatalog(connection).link_job_version(job["id"], version_id)
        set_job_progress(
            job["id"],
            int(job.get("total_bytes") or getattr(source_stat, "st_size", 0) or 0),
        )
        set_job(job["id"], "completed", message_key="job.rename_verified")
    except OperationCancelled:
        raise
    except Exception:
        raise


def record_automatic_cleanup_outcome(
    job: dict[str, Any],
    *,
    event: str,
    outcome: str,
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


def _checked_local_release_target(
    job: dict[str, Any], *, connection: Any = None
) -> dict[str, Any]:
    """Reauthorize queued automatic work; manual requests never imply automation."""
    if connection is None:
        with db() as connection:
            return _checked_local_release_target(job, connection=connection)
    target = ArchiveCatalog(connection).get_job_target(job["id"])
    if not local_release_is_confirmed(
        connection, int(job["vault_id"])
    ):
        raise RuntimeError("Cleanup blocked: local or cloud protection is not confirmed")
    if target and target["job_origin"] == "automatic":
        policy = get_policy(connection, int(job["vault_id"]))
        if policy.local_release_after_days is None or not target["verified_at"] or not local_cleanup_is_due(
            verified_at=target["verified_at"], retention_days=policy.local_release_after_days,
            now=datetime.now(timezone.utc), local_mtime_ns=target["local_mtime_ns"],
            recovered_at=target["local_recovered_at"], content_observed_at=target["content_observed_at"],
        ):
            raise OperationCancelled("Automatic local release is disabled or no longer due")
    if (
        not target
        or target["local_presence"] != "present"
        or target["local_file_type"] != "regular"
        or target["integrity"] != "verified"
        or target["availability"] != "available"
        or target["matched_archive_version_id"] != target["archive_version_id"]
        or not target["local_sha256"]
        or target["local_sha256"] != target["version_sha256"]
        or target["cloud_only_version_id"] is not None
        or target["archive_version_id"] != target["latest_archive_version_id"]
        or not target["object_key"]
        or not target["provider_version_id"]
    ):
        raise RuntimeError("The file no longer has verifiable local and cloud copies")
    return target


def process_free_space(job: dict[str, Any]) -> None:
    ensure_job_active(job["id"], "Freeing local space stopped")
    validate_cloud_vault(job, notify=False)
    # The claim's temporary absence must never become a watcher/full-scan
    # observation. Hashing holds no DB write transaction, only the same
    # per-Vault filesystem lock used by scans and watcher batches.
    with scan_lock_for_vault(int(job["vault_id"])):
        _process_free_space(job)


def _repair_failed_local_release(
    job: dict[str, Any], target: dict[str, Any], local_path: Path, *, replacement: bool = False,
) -> None:
    """Publish salvaged/replaced bytes immediately without following symlinks."""
    # A Job id is not a Vault id. Never fall back to the unenrolled Job row
    # when the actual Vault disappeared; its root/source fences are mandatory.
    with db() as connection:
        vault = connection.execute(
            "SELECT * FROM vaults WHERE id=%s", (int(job["vault_id"]),),
        ).fetchone()
    if not isinstance(vault, dict) or "source_root" not in vault:
        raise _ScanRootMismatch("Vault is unavailable during local release repair")
    if vault["source_root"] != job["source_root"]:
        raise _ScanRootMismatch("Vault root changed during local release repair")
    _validate_enrolled_scan_root(vault)
    if not source_layout.vault_local_access(vault["source_root"]).local_operations_allowed:
        raise _ScanRootMismatch("Source Volume changed during local release repair")
    inspected = inspect_entry(local_path)
    file_type, info = inspected if inspected else ("other", local_path.lstat())
    digest = None
    if file_type == "regular":
        digest, info = hash_stable_regular_file(local_path)
    with db() as connection:
        if "root_identity" in vault:
            _current_verified_scan_row(connection, vault)
        catalog = ArchiveCatalog(connection)
        if replacement:
            file_id = target["vault_file_id"]
            catalog.observe_replaced_local_copy(
                file_id, file_type=file_type, size=info.st_size,
                mtime_ns=info.st_mtime_ns, observed_at=now_iso(),
            )
        else:
            file_id = catalog.observe_local_copy(
                vault_id=job["vault_id"], path=job["path"], file_type=file_type,
                size=info.st_size, mtime_ns=info.st_mtime_ns, observed_at=now_iso(),
            )
        if digest != target["local_sha256"]:
            connection.execute("UPDATE local_copies SET content_observed_at=%s WHERE vault_file_id=%s", (now_iso(), file_id))
        if digest:
            catalog.set_local_fingerprint(
                vault_id=job["vault_id"], path=job["path"], plaintext_sha256=digest,
                matched_archive_version_id=(target["archive_version_id"] if digest == target["version_sha256"] else None),
            )
        if "root_identity" in vault:
            _current_verified_scan_row(connection, vault)
    if digest:
        local_observation.remember(job["vault_id"], job["path"], info, digest)


def _process_free_space(job: dict[str, Any]) -> None:
    ensure_job_active(job["id"], "Freeing local space stopped")
    target = _checked_local_release_target(job)
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
        head.get("DeleteMarker")
        or not head.get("VersionId")
        or str(head["VersionId"]) != str(target["provider_version_id"])
        or head.get("ContentLength") is None
        or int(head["ContentLength"]) != int(target["cloud_size"])
    ):
        raise RuntimeError("Cleanup blocked: exact S3 version or size no longer matches")
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
        with db() as connection:
            # Same lock order as admission: Vault, then Job. A concurrent policy
            # disable/extension or cancellation must win before this last check,
            # or wait until the already-authorized unlink has finished.
            connection.execute("UPDATE vaults SET name=name WHERE id=%s", (job["vault_id"],))
            ensure_job_claim_owned_in_transaction(connection, job, "Freeing local space claim was lost")
            fresh = _checked_local_release_target(job, connection=connection)
            if (
                fresh["archive_version_id"], fresh["object_key"], fresh["provider_version_id"],
                fresh["cloud_size"], fresh["version_sha256"], fresh["local_size"], fresh["local_mtime_ns"],
            ) != (
                target["archive_version_id"], target["object_key"], target["provider_version_id"],
                claimed_stat.st_size, local_digest, claimed_stat.st_size, claimed_stat.st_mtime_ns,
            ):
                raise RuntimeError("Cleanup blocked: protection changed while claiming the file")
            claimed_path.unlink()
    except Exception as exc:
        if not restore_claimed_local_copy(claimed_path, local_path):
            try:
                _repair_failed_local_release(job, target, local_path, replacement=True)
            finally:
                raise RuntimeError(
                    f"Cleanup stopped; original content was preserved at {claimed_path}"
                ) from exc
        _repair_failed_local_release(job, target, local_path)
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
        )


def process_storage_class(job: dict[str, Any]) -> None:
    with db() as connection:
        move = vault_moves.load(connection, int(job["id"]))
    if move:
        vault_moves.process(job, dict(move))
        return
    check_active = partial(ensure_job_active, int(job["id"]))
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
            check_active=check_active,
        )
    ensure_job_active(job["id"], "Storage class claim was lost")
    new_version_id = _copy_storage_class_version(
        client,
        bucket=job["s3_bucket"],
        object_key=target["object_key"],
        source_version_id=str(target["provider_version_id"]),
        target_class=target_class,
        source_size=source_size,
        source_head=head,
        check_active=check_active,
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
        check_active=check_active,
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

    Uses boto3 TransferManager (parallel range GETs for large objects).
    """
    if not provider_version_id:
        raise RuntimeError("Exact S3 VersionId is required for recovery")
    ensure_job_active(job["id"], "Recovery stopped")
    temporary.parent.mkdir(parents=True, exist_ok=True)
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
            Config=s3_download_transfer_config(),
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
        runtime = _runtime_settings()
        tier = job.get("restore_tier") or runtime.restore_tier or "Bulk"
        days = int(job.get("restore_days") or runtime.restore_days or 3)
        estimate = estimate_restore(
            size_bytes=size_bytes,
            storage_class=str(storage_class),
            tier=str(tier),
            days=days,
            pricing=list_prices(runtime.aws_region).restore_rates,
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
    message = "Downloading"
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
        with scan_lock_for_vault(int(job["vault_id"])):
            # Transfers/hash stay outside the filesystem critical section.
            # Publish the atomic rename and its verified observation together
            # so a watcher cannot unprotect our own just-recovered copy.
            ensure_job_active(job["id"], "Recovery claim was lost")
            if not _local_stat_unchanged(temporary, recovered_stat):
                raise RuntimeError("Recovered plaintext changed before publication")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(destination):
                raise RuntimeError(
                    "A local copy already exists at the recovery destination"
                )
            temporary.replace(destination)
            published_stat = destination.lstat()
            with db() as connection:
                ensure_job_claim_owned_in_transaction(connection, job, "Recovery claim was lost")
                catalog = ArchiveCatalog(connection)
                recovered_at = now_iso()
                catalog.observe_local_copy(
                    vault_id=job["vault_id"], path=job["path"], file_type="regular",
                    size=recovered_stat.st_size, mtime_ns=recovered_stat.st_mtime_ns,
                    observed_at=recovered_at, recovered_at=recovered_at,
                )
                catalog.set_local_fingerprint(
                    vault_id=job["vault_id"], path=job["path"],
                    plaintext_sha256=expected_digest, matched_archive_version_id=archive_version_id,
                )
                if target.get("restore_state") or storage_class_requires_restore(head_class):
                    catalog.update_restore_state(
                        archive_version_id,
                        state="available" if storage_class_requires_restore(head_class) else None,
                        expiry=restore_expiry, checked_at=now_iso(), storage_class=head_class,
                    )
            local_observation.remember(job["vault_id"], job["path"], published_stat, expected_digest)
    finally:
        temporary.unlink(missing_ok=True)
    set_job_progress(job["id"], int(job.get("total_bytes") or recovered_stat.st_size))
    set_job(job["id"], "completed", f"Recovered to {destination}")


def process_cloud_purge(job: dict[str, Any]) -> None:
    """Permanently delete a scheduler-claimed cloud-purge group exactly once."""
    ensure_job_active(job["id"], "Cloud purge stopped")
    claim_token = job.get("claim_token")
    if not claim_token:
        # The scheduler is the only supported entry point for destructive work.
        # Retaining this no-op protects direct legacy callers from bypassing the
        # durable group acquisition primitive.
        return
    claimed_at = now_iso()
    with db() as connection:
        if not cloud_deletion_service.validate_execution(
            connection, vault_id=int(job["vault_id"]), group_id=str(job.get("group_id") or ""),
            at=claimed_at,
        ):
            return
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
    validate_cloud_vault(job)
    client = s3_client()
    failures = 0
    for offset in range(0, len(items), CLOUD_DELETE_BATCH_SIZE):
        batch = items[offset : offset + CLOUD_DELETE_BATCH_SIZE]
        ensure_job_active(job["id"], "Cloud purge stopped")
        stamp = now_iso()
        # Keep all group members leased while a batch is in flight.  If a
        # cancellation or takeover won, do not turn its durable state back into
        # an item result after the provider call.
        with db() as connection:
            if not cloud_deletion_service.validate_execution(
                connection, vault_id=int(job["vault_id"]), group_id=str(group_id), at=stamp,
            ):
                return
            # Validation may traverse a large selection while holding Job locks.
            # Never revive an expired lease using the pre-validation timestamp.
            stamp = now_iso()
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


@retry_database_unit
def _load_claimed_job_status(job: dict[str, Any]) -> dict[str, Any] | None:
    with owned_connection(db()) as connection:
        claim_token = job.get("claim_token")
        if claim_token:
            return ArchiveCatalog(connection).renew_job_claim(
                job_id=int(job["id"]), claim_token=str(claim_token),
                now=now_iso(), claim_expires_at=_claim_expiry_at(),
            )
        # Direct unit/service callers retain the historic unclaimed seam;
        # production scheduling always supplies a token.
        return connection.execute(
            "SELECT status FROM jobs WHERE id=%s", (job["id"],),
        ).fetchone()


@retry_database_unit
def _release_preflight_purge_claim(job: dict[str, Any]) -> None:
    # The group was leased as cleaning, but preflight precedes DeleteObjects.
    # Release the complete group; keep exact deletion consent and its deadline.
    with owned_connection(db()) as connection:
        connection.execute(
            "UPDATE jobs SET status='queued',claim_token=NULL,claimed_at=NULL,claim_expires_at=NULL "
            "WHERE vault_id=%s AND group_id=%s AND action='cloud-purge' AND status='cleaning' "
            "AND claim_token=%s AND claim_expires_at>%s",
            (job["vault_id"], job["group_id"], job["claim_token"], now_iso()),
        )


# A Source Volume that stays unreadable is polled with backoff (not every cycle) and,
# past this age, surfaced to the Owner with the reason ``source_volume_unavailable``.
SOURCE_VOLUME_NOTIFY_AFTER_SECONDS = 10 * 60
SOURCE_VOLUME_WAIT_KEY = "job.source_volume_waiting"
SOURCE_VOLUME_PROBLEM = "source_volume_unavailable"


def _source_volume_problem_key(vault_id: int) -> str:
    return f"{SOURCE_VOLUME_PROBLEM}:{vault_id}"


def _wait_for_source_volume(job: dict[str, Any]) -> None:
    """Park a Job whose Source Volume cannot be read; never fail it (#486, #492).

    An upload backs off exponentially (status ``retrying`` with ``retry_after``,
    the failure retry budget untouched, the wait start and attempt kept in the
    Job's own message parameters so it survives restarts) and, once the outage is
    older than the threshold, opens one in-app problem per Vault with the reason
    ``source_volume_unavailable``. Other actions keep the plain queued wait: their
    states cannot be rescheduled without changing their lifecycle.
    """
    if not job.get("claim_token"):
        return
    if job["action"] != "upload":
        set_job(job["id"], "queued", "Source Volume is unavailable; Job remains queued",
                remediation_suspension=SOURCE_VOLUME_PROBLEM)
        return
    now = datetime.now(timezone.utc)
    since, attempt = now, 0
    if job.get("message_key") == SOURCE_VOLUME_WAIT_KEY:
        params = parse_message_params(job.get("message_params"))
        try:
            parsed = datetime.fromisoformat(str(params.get("since")))
            since = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            attempt = max(0, int(params.get("attempt") or 0))
        except (TypeError, ValueError):
            since, attempt = now, 0
    attempt += 1
    delay = upload_retry_delay_seconds(attempt)
    parked = schedule_upload_retry(
        job["id"], message_key=SOURCE_VOLUME_WAIT_KEY,
        message_params={"since": since.isoformat(), "attempt": attempt, "seconds": delay},
        retry_count=int(job.get("retry_count") or 0), delay_seconds=delay,
        remediation_suspension=SOURCE_VOLUME_PROBLEM,
    )
    if parked and (now - since).total_seconds() >= SOURCE_VOLUME_NOTIFY_AFTER_SECONDS:
        from .services import upload_provenance as provenance
        @retry_database_unit
        def notify() -> None:
            with owned_connection(db()) as connection:
                notification_service.set_problem(
                    connection, key=_source_volume_problem_key(int(job["vault_id"])),
                    event=SOURCE_VOLUME_PROBLEM, vault_id=int(job["vault_id"]), active=True, email=False,
                    params={"vault": job.get("vault_name") or "", "reason": SOURCE_VOLUME_PROBLEM,
                            "remedy": provenance.REMEDIES[SOURCE_VOLUME_PROBLEM]},
                )
        notify()


def _clear_source_volume_problem(job: dict[str, Any]) -> None:
    """The volume was read again: resolve the Vault's open problem, if any."""
    key = _source_volume_problem_key(int(job["vault_id"]))
    with db() as connection:
        opened = connection.execute(
            "SELECT 1 FROM notifications WHERE dedupe_key=%s AND problem_status='open' LIMIT 1", (key,),
        ).fetchone()
    if not opened:
        return
    @retry_database_unit
    def resolve() -> None:
        with owned_connection(db()) as connection:
            notification_service.set_problem(connection, key=key, event=SOURCE_VOLUME_PROBLEM,
                                             vault_id=int(job["vault_id"]), active=False, email=False)
    resolve()


def _source_volume_readable(vault: dict[str, Any]) -> bool:
    """The same criterion the upload uses to decide that it must keep waiting.

    ``vault_local_access`` must allow local operations AND the enrolled root must be
    observable (``_validate_enrolled_scan_root`` raising ``_ScanRootUnreadable`` is an
    I/O failure of the root: ENOENT/EIO/ESTALE on an otherwise healthy mount). Any
    other validator failure is an observed identity mismatch: the upload fails
    instead of waiting, so nothing is waiting any more.
    """
    try:
        if not source_layout.vault_local_access(vault["source_root"]).local_operations_allowed:
            return False
        _validate_enrolled_scan_root(vault)
    except _ScanRootUnreadable:
        return False
    except Exception:
        return True
    return True


def sweep_source_volume_problems() -> int:
    """Resolve ``source_volume_unavailable`` problems nothing keeps alive (#492).

    The problem is opened by a waiting upload and normally closed by the next upload
    that reads the root. It must not outlive its cause when that Job is cancelled or
    closed, or when the root is readable again and the Vault has no other upload to
    run: each scheduler cycle resolves a Vault's open problem when no upload of it
    still waits for the volume, or when its enrolled root can be read again.
    """
    with db() as connection:
        rows = connection.execute(
            "SELECT DISTINCT n.vault_id FROM notifications n WHERE n.event=%s AND n.problem_status='open' "
            "AND n.vault_id IS NOT NULL", (SOURCE_VOLUME_PROBLEM,)).fetchall()
        resolvable: list[int] = []
        for row in rows:
            vault = connection.execute("SELECT * FROM vaults WHERE id=%s", (row["vault_id"],)).fetchone()
            waiting = connection.execute(
                "SELECT 1 FROM jobs WHERE vault_id=%s AND action='upload' AND status IN ('queued','retrying') "
                "AND message_key=%s LIMIT 1", (row["vault_id"], SOURCE_VOLUME_WAIT_KEY)).fetchone()
            readable = False
            if vault and waiting:
                readable = _source_volume_readable(vault)
            if not vault or not waiting or readable:
                resolvable.append(int(row["vault_id"]))
    for vault_id in resolvable:
        @retry_database_unit
        def resolve() -> None:
            with owned_connection(db()) as connection:
                notification_service.set_problem(connection, key=_source_volume_problem_key(vault_id),
                                                 event=SOURCE_VOLUME_PROBLEM, vault_id=vault_id, active=False, email=False)
        resolve()
    return len(resolvable)


def _process_claimed_job(job: dict[str, Any]) -> bool:
    """Process one queue item after the scheduler has acquired its lease."""
    try:
        shutdown_state.checkpoint()
        runtime = _runtime_settings()
        current = _load_claimed_job_status(job)
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
                _wait_for_source_volume(job)
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
            "queued", "retrying",
            "restoring",
        }:
            process_storage_class(job)
        elif job["action"] == "cloud-purge" and job["status"] == "cleaning":
            process_cloud_purge(job)
        else:
            return False
        return True
    except cloud_preflight_gate.CloudPreflightBlocked:
        # Readiness is checked before any provider mutation in every handler.
        # Preserve retries/retry_after and release only the scheduler lease.
        if job["action"] == "cloud-purge" and job.get("claim_token"):
            _release_preflight_purge_claim(job)
        else:
            set_job(job["id"], job["status"], "S3 readiness blocked; Job remains queued", **_keep_wait_state(job))
        return False
    except SourceVolumeUnavailable:
        # A transient failure reading the enrolled root is not an identity
        # change (#486). Nothing was sent by this attempt; the journaled intent,
        # if any, reconciles on the next lease. Same wait as the pre-dispatch check.
        _wait_for_source_volume(job)
        return False
    except WorkerStopping:
        # Process shutdown is not an Owner cancellation or a failed upload.
        # Keep the durable lease, intent and any late receipts for recovery.
        return False
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
            )
    except Exception as exc:
        if _claim_is_lost(int(job["id"])):
            return False
        from .services import upload_provenance as _provenance
        if (job["action"] == "upload" and isinstance(exc, _provenance.UploadBlocked)
                and exc.reason in _provenance.WAIT_REASONS):
            # The Vault is suspended/relocating: wait queued and resume by itself
            # once it is reactivated, instead of failing into a block nobody can lift.
            if job.get("claim_token"):
                set_job(job["id"], "queued", "Vault is suspended; Job remains queued",
                        remediation_suspension=exc.reason)
            return False
        if cloud_preflight_gate.is_readiness_error(exc):
            if _fail_upload_on_permission_refusal(job, exc):
                return True
            # This happens outside the rolled-back provider/catalog unit. Do not
            # consume a transfer retry or expose provider auth error payloads.
            cloud_preflight_gate.invalidate(db)
            if job['action'] == 'cloud-purge' and job.get('claim_token'):
                _release_preflight_purge_claim(job)
            else:
                set_job(job['id'], job['status'], 'S3 readiness blocked; Job remains queued', **_keep_wait_state(job))
            return False
        message = str(exc)
        failure_kind = (
            classify_upload_failure(exc) if job["action"] == "upload" else ""
        )
        if job["action"] == "upload" and failure_kind == "source_changed":
            next_attempt = int(job.get("retry_count") or 0) + 1
            if next_attempt <= UPLOAD_RETRY_MAX_ATTEMPTS:
                with db() as connection:
                    policy = get_policy(connection, int(job["vault_id"]))
                return schedule_upload_retry(
                    job["id"],
                    message_key="job.retrying_source_changed",
                    message_params={"seconds": policy.stability_seconds},
                    retry_count=next_attempt,
                    delay_seconds=policy.stability_seconds,
                )
            return set_job(
                job["id"],
                "failed",
                message_key="job.source_changed_stalled",
                message_params={"attempts": UPLOAD_RETRY_MAX_ATTEMPTS},
            )
        # DB failures are retried at their owned transaction boundary above.
        # Exhaustion must fail closed, not restart S3 with a lost response (#464).
        if job["action"] == "upload" and failure_kind in {"transient", "database_connection"}:
            next_attempt = int(job.get("retry_count") or 0) + 1
            if next_attempt <= UPLOAD_RETRY_MAX_ATTEMPTS:
                return schedule_upload_retry(
                    job["id"],
                    message_key="job.retrying_transient",
                    message_params={"error": message},
                    retry_count=next_attempt,
                )
        if job["action"] == "storage-class":
            with db() as connection:
                move = vault_moves.load(connection, int(job["id"]))
            if move and not isinstance(exc, vault_moves.MoveBlocked) and classify_upload_failure(exc) == "transient":
                next_attempt = int(job.get("retry_count") or 0) + 1
                if next_attempt <= UPLOAD_RETRY_MAX_ATTEMPTS:
                    return schedule_upload_retry(
                        job["id"], message_key="job.retrying_transient",
                        message_params={"error": message}, retry_count=next_attempt,
                    )
        failed = set_job(job["id"], "failed", message)
        if failed and job["action"] == "free-space":
            record_automatic_cleanup_outcome(
                job,
                event="local_cleanup.failed",
                outcome="failure",
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


def _reap_operation_futures() -> None:
    with _operation_lock:
        finished = [
            job_id
            for job_id, future in _operation_futures.items()
            if future.done()
        ]
        for job_id in finished:
            _operation_futures.pop(job_id, None)


def _active_operation_count() -> int:
    _reap_operation_futures()
    with _operation_lock:
        return len(_operation_futures)


def _operation_pool(max_workers: int) -> ThreadPoolExecutor:
    global _operation_executor
    workers = max(1, int(max_workers))
    with _operation_lock:
        shutdown_state.checkpoint()
        if _operation_executor is None:
            _operation_executor = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="operation",
            )
        return _operation_executor


def _maintenance_pool_locked() -> ThreadPoolExecutor:
    """Return the maintenance pool; the caller must hold ``_maintenance_lock``."""
    global _maintenance_executor
    if _maintenance_executor is None:
        _maintenance_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="maintenance",
        )
    return _maintenance_executor


def _maintenance_pool() -> ThreadPoolExecutor:
    with _maintenance_lock:
        return _maintenance_pool_locked()


def _submit_maintenance(name: str, func: Callable[..., Any], *args: Any) -> None:
    # ``_maintenance_lock`` is not reentrant.  Calling ``_maintenance_pool()``
    # here would re-acquire it and freeze the event loop thread that runs
    # ``background_loop`` on the first scheduled scan, audit or backup.
    with _maintenance_lock:
        if shutdown_state.requested:
            return
        existing = _maintenance_futures.get(name)
        if existing is not None and not existing.done():
            return
        _maintenance_futures[name] = _maintenance_pool_locked().submit(func, *args)


def stop_background_executors() -> None:
    """Cancel queued work immediately, retaining running futures for draining."""
    with _operation_lock:
        if _operation_executor is not None:
            _operation_executor.shutdown(wait=False, cancel_futures=True)
    with _maintenance_lock:
        if _maintenance_executor is not None:
            _maintenance_executor.shutdown(wait=False, cancel_futures=True)


def _detach_background_executors() -> list[Future[Any]]:
    global _operation_executor, _maintenance_executor
    with _operation_lock:
        executor = _operation_executor
        _operation_executor = None
        futures = list(_operation_futures.values())
        _operation_futures.clear()
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
    with _maintenance_lock:
        maintenance = _maintenance_executor
        _maintenance_executor = None
        futures.extend(_maintenance_futures.values())
        _maintenance_futures.clear()
        if maintenance is not None:
            maintenance.shutdown(wait=False, cancel_futures=True)
    return futures


def shutdown_background_executors() -> None:
    """Release pools immediately; test/service callers retain this cleanup seam."""
    _detach_background_executors()


async def drain_background_executors(timeout: float = WORKER_DRAIN_SECONDS) -> bool:
    """Cancel queued work and await running workers without blocking HTTP's loop.

    Stop admission and signal cooperative checkpoints before detaching pools.
    A timeout is NOT success: the process watchdog also bounds default-executor
    and interpreter thread joins, which asyncio cancellation cannot interrupt.
    """
    shutdown_state.request()
    futures = [asyncio.wrap_future(future) for future in _detach_background_executors()
               if not future.cancelled()]
    if not futures:
        return True
    done, pending = await asyncio.wait(futures, timeout=timeout)
    for future in done:
        # Retrieve exceptions; worker error/state handling already owns them.
        if not future.cancelled():
            future.exception()
    for future in pending:
        future.cancel()
    return not pending


def process_jobs_once(*, wait: bool = True) -> int:
    """Claim and dispatch one fair, concurrency-bounded scheduler batch.

    Selecting candidates is intentionally separate from acquisition.  Every
    selected row is rechecked by a conditional UPDATE in ``ArchiveCatalog``;
    two processes may read the same queue but only one receives a claim token.
    Expired intermediate leases are reconciled first so a dead worker does not
    leave ``uploading``/``verifying``/``cleaning`` Jobs invisible to the scheduler.
    When ``wait`` is false the caller keeps polling, delivering notifications,
    and refreshing health while transfers continue on the operation pool.
    """
    if shutdown_state.requested:
        return 0
    reconcile_interrupted_jobs()
    try:
        sweep_source_volume_problems()
    except Exception:
        logging.getLogger(__name__).warning("Source Volume problem sweep failed; retried next cycle")
    try:
        failed_count, failed_age = observe_failed_job_backlog()
        metrics_service.set_gauge("jobs_failed_backlog", float(failed_count))
        metrics_service.set_gauge(
            "jobs_failed_oldest_age_seconds", failed_age
        )
    except Exception:
        pass
    now = now_iso()
    current = datetime.now(timezone.utc)
    with db() as connection:
        runtime = _runtime_settings()
        restore_due_before = _restore_due_before(runtime, current=current)
        # Over-fetch candidates so fair interleave can still fill the concurrency
        # budget when one Vault dominates the oldest requested_at values.
        batch_size = max(10, int(runtime.operation_concurrency) * 10)
        candidates = ArchiveCatalog(connection).list_claimable_jobs(
            now=now,
            restore_due_before=restore_due_before,
            limit=batch_size,
        )
        preflight = cloud_preflight_gate.snapshot(connection)
        preflight_wait = ((preflight['status'] == 'blocked' and
                           not cloud_preflight_gate.probe_due(preflight, current))
                          or (not wait and not cloud_preflight_gate.dispatch_ready(preflight, current)))
        eligible_candidates: list[dict[str, Any]] = []
        seen_purge_groups: set[tuple[int, str]] = set()
        for row in candidates:
            job = dict(row)
            vault_id = int(job["vault_id"])
            # Only handlers which call validate_cloud_vault are gated; this
            # does not stop local catalog scans, notifications or heartbeat.
            if preflight_wait and job["action"] in {"upload", "recover", "rename", "free-space", "storage-class", "cloud-purge"}:
                continue
            if vault_relocation.local_work_suspended({"id": vault_id}):
                continue
            if (
                job.get("origin") != "stop-protection"
                and protection_stop_service.local_work_suspended(job)
            ):
                continue
            if job["action"] == "cloud-purge":
                if not job.get("group_id"):
                    continue
                purge_group = (vault_id, str(job["group_id"]))
                if purge_group in seen_purge_groups:
                    continue
                seen_purge_groups.add(purge_group)
            eligible_candidates.append(job)

    # Preserve the automatic-upload stability/suspension fence before preflight,
    # even when an SMB edit has not reached the watcher/catalog yet.
    eligible_candidates = [job for job in eligible_candidates
        if job['action'] != 'upload' or job.get('origin') != 'automatic'
        or _automatic_upload_ready(job)]
    # The durable ready cache may belong to a previous deployment's credentials.
    # Probe outside the candidate transaction BEFORE acquiring any Job lease.
    # The live non-waiting scheduler never performs provider I/O: the dedicated
    # readiness loop proves startup/expiry while heartbeat/delivery keep moving.
    if eligible_candidates and wait:
        try:
            validate_cloud_vault(eligible_candidates[0])
        except cloud_preflight_gate.CloudPreflightBlocked:
            return 0

    concurrency = int(runtime.operation_concurrency)
    if wait:
        available_slots = concurrency
    else:
        available_slots = max(0, concurrency - _active_operation_count())
        if available_slots <= 0:
            return 0
    selected = select_fair_jobs(
        eligible_candidates,
        limit=available_slots,
    )
    claimed_jobs: list[dict[str, Any]] = []
    for job in selected:
        if shutdown_state.requested:
            break
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
        if wait:
            worker_count = min(concurrency, len(claimed_jobs))
            with ThreadPoolExecutor(
                max_workers=worker_count, thread_name_prefix="operation"
            ) as executor:
                list(executor.map(process_job, claimed_jobs))
        else:
            executor = _operation_pool(concurrency)
            with _operation_lock:
                for job in claimed_jobs:
                    job_id = int(job["id"])
                    if shutdown_state.requested:
                        break
                    if job_id in _operation_futures and not _operation_futures[job_id].done():
                        continue
                    _operation_futures[job_id] = executor.submit(process_job, job)
    return len(claimed_jobs)


def claimable_queue_depth() -> int:
    """Return the actual unleased runnable backlog for the ``queue_depth`` gauge."""
    timestamp = now_iso()
    with db() as connection:
        runtime = _runtime_settings()
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


AUTO_UPLOAD_POLL_SECONDS = 10


def queue_vault_auto_uploads(vault: dict[str, Any], *, now: datetime | None = None,
                             retry_failed_only: bool = False) -> int:
    """Drain stable watcher observations without a cloud call or full scan.

    The catalog is the durable pending set, including observations recovered by
    full-scan reconciliation. No in-memory timer is lost on restart.
    """
    lock = scan_lock_for_vault(int(vault["id"]))
    if not lock.acquire(blocking=False):
        return 0
    try:
        vault = _current_scan_vault(vault)
        if not vault.get("enabled", True) or vault_relocation.local_work_suspended(vault):
            return 0
        if protection_stop_service.local_work_suspended(vault):
            return 0
        access = source_layout.vault_local_access(vault["source_root"])
        if not access.local_operations_allowed:
            return 0
        # Open the enrolled root before using /proc/self/fd for candidate stats.
        # Replacement/absence rolls back admission without changing the catalog.
        pinned_root = (
            _pinned_verified_scan_root(vault)
            if "root_identity" in vault else nullcontext(Path(vault["source_root"]))
        )
        with pinned_root as root, db() as connection:
            from .services.job_admission import lock_vault
            try:
                lock_vault(connection, int(vault["id"]))
            except LookupError:
                return 0
            current = connection.execute("SELECT * FROM vaults WHERE id=%s", (vault["id"],)).fetchone()
            if (not current or not current["enabled"] or current["relocation_state"] != "ready"
                    or current["decommission_state"] != "active"
                    or current["source_root"] != vault["source_root"]):
                return 0
            owner_id = owner_user_id(connection)
            if owner_id is None:
                return 0
            queued = 0
            if retry_failed_only:
                from .services import upload_remediation
                queued += upload_remediation.retry_vault(connection,dict(current),owner_id,
                    (now or datetime.now(timezone.utc)).isoformat())
            queued += queue_auto_uploads(
                connection, vault_id=int(vault["id"]), source_root=str(root),
                requested_by=owner_id, now=now, retry_failed_only=retry_failed_only,
            )
            if "root_identity" in vault:
                _current_verified_scan_row(connection, vault)
            if not source_layout.vault_local_access(vault["source_root"]).local_operations_allowed:
                raise _ScanRootMismatch("Source Volume changed during automatic upload admission")
            return queued
    finally:
        lock.release()


def queue_all_auto_uploads_once() -> int:
    queued = 0
    for vault in _enabled_vaults():
        try:
            queued += queue_vault_auto_uploads(vault)
        except Exception as exc:
            _report_worker_error("auto_upload_queue", exc, int(vault["id"]))
    return queued


def reprobe_cloud_readiness_once() -> None:
    # Independent of scheduler capacity and maintenance scans. A blocked gate
    # must recover even if the Owner has since suspended all queued Vaults.
    with db() as connection:
        state = cloud_preflight_gate.snapshot(connection)
        now = datetime.now(timezone.utc)
        if cloud_preflight_gate.dispatch_ready(state, now):
            return
        if state['status'] == 'blocked' and not cloud_preflight_gate.probe_due(state, now):
            return
        vault = connection.execute("SELECT * FROM vaults ORDER BY id LIMIT 1").fetchone()
    if vault:
        try:
            validate_cloud_vault(dict(vault))
        except cloud_preflight_gate.CloudPreflightBlocked:
            pass


async def cloud_preflight_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(reprobe_cloud_readiness_once)
        except Exception as exc:
            _report_worker_error("cloud_preflight", exc)
        await asyncio.sleep(1)


async def upload_remediation_loop() -> None:
    # Dedicated DB-only executor: neither long transfers, mount probes nor the
    # singleton maintenance executor can delay discovery/admission ticks.
    from .services import upload_remediation
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="upload-remediation")
    try:
        while True:
            try:
                await loop.run_in_executor(executor, upload_remediation.sweep, db)
                await loop.run_in_executor(executor, upload_remediation.observe, db)
            except Exception as exc:
                await asyncio.to_thread(_report_worker_error, "upload_remediation", exc)
            await asyncio.sleep(upload_remediation.POLL_SECONDS)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


async def auto_upload_loop() -> None:
    # Separate from scanning and cloud maintenance, including when a long scan
    # or lifecycle request occupies their worker. Catalog observations persist.
    while True:
        try:
            await asyncio.to_thread(queue_all_auto_uploads_once)
        except Exception as exc:
            await asyncio.to_thread(_report_worker_error, "auto_upload_queue", exc)
        await asyncio.sleep(AUTO_UPLOAD_POLL_SECONDS)


def _filesystem_watch_filter(_: Change, path: str) -> bool:
    return not is_restore_temporary_name(Path(path).name)


def _new_watcher_directories(root: Path, changes: set[tuple[Change, str]]) -> set[tuple[Change, str]]:
    directories = set()
    for change, value in changes:
        entry = Path(value)
        if change != Change.added or is_restore_temporary_name(entry.name):
            continue
        try:
            entry.relative_to(root)
            if entry.is_dir() and not entry.is_symlink():
                directories.add((Change.added, value))
        except (ValueError, OSError):
            continue
    return directories


async def _watch_vault_filesystem(vault: dict[str, Any]) -> None:
    # Some Source Volume backends do not reliably enroll new directories.
    # Install the successor BEFORE draining/closing the old recursive observer:
    # subtree catch-up alone cannot repair unrelated sibling events in a gap.
    pending_reconcile: set[tuple[Change, str]] = set()
    while True:
        if vault_relocation.local_work_suspended(vault):
            await asyncio.sleep(1)
            continue
        if protection_stop_service.local_work_suspended(vault):
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
        # Identity / mount gate first — never resolve() or walk the configured
        # tree while local operations are fail-closed (replacement, absent,
        # inaccessible, ambiguous, unsafe alias).
        access = await asyncio.to_thread(
            source_layout.vault_local_access, vault["source_root"]
        )
        if not access.local_operations_allowed:
            await asyncio.to_thread(
                _record_scan_component_health, int(vault["id"]), "source",
                ok=False, error="Protection blocked: Source Volume unavailable",
            )
            await asyncio.sleep(5)
            continue
        try:
            root = Path(vault["source_root"]).resolve()
            if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
                raise PermissionError("Vault root is inaccessible")
        except OSError:
            await asyncio.to_thread(
                _record_scan_component_health, int(vault["id"]), "source",
                ok=False, error="Protection blocked: Vault root inaccessible",
            )
            await asyncio.sleep(5)
            continue
        try:
            runtime = await asyncio.to_thread(_runtime_settings)
            def new_observer():
                return awatch(
                    root,
                    watch_filter=_filesystem_watch_filter,
                    debounce=runtime.filesystem_watch_debounce_ms,
                    force_polling=settings.filesystem_watch_force_polling,
                    poll_delay_ms=runtime.filesystem_watch_poll_ms,
                    recursive=True,
                    ignore_permission_denied=False,
                    # awatch is lazy: its first yield (including a timeout)
                    # is the registration barrier, for inotify AND polling.
                    yield_on_timeout=True,
                    rust_timeout=1000,
                )

            watcher = new_observer()
            successor = None
            rearm_required = False
            recovered = False
            try:
                while True:
                    native_changes = set() if rearm_required else await anext(watcher)
                    pending_reconcile |= native_changes
                    new_directories = await asyncio.to_thread(
                        _new_watcher_directories, root, native_changes,
                    ) if native_changes else set()
                    # PollWatcher recursively rediscovers topology every poll;
                    # it needs no re-registration. Keep its original snapshot
                    # alive: a timeout is NOT a completed polling-cycle fence,
                    # so replacing it could lose pre-successor sibling changes.
                    if (new_directories or rearm_required) and not settings.filesystem_watch_force_polling:
                        successor = new_observer()
                        # Old observer is still live while the successor takes
                        # its recursive snapshot / registers native watches.
                        first = await anext(successor)
                        pending_reconcile |= first
                        tail = await anext(watcher)
                        pending_reconcile |= tail
                        old, watcher = watcher, successor
                        successor = None
                        await old.aclose()
                        # Topology added DURING registration may need another
                        # handoff. No full-root walk, and at most two observers.
                        additions = await asyncio.to_thread(
                            _new_watcher_directories, root, first | tail,
                        )
                        rearm_required = bool(additions - new_directories)
                    if pending_reconcile and not rearm_required:
                        # Reconcile only once every added subtree is covered by
                        # a registered successor, including additions made in
                        # the preceding handoff itself.
                        await asyncio.to_thread(_apply_watcher_changes, vault, pending_reconcile)
                        pending_reconcile = set()
                    if not recovered and not pending_reconcile and not rearm_required:
                        # First fully healthy cycle of this run: registration
                        # barrier passed and every pending change applied.
                        await asyncio.to_thread(
                            _resolve_worker_error, "source_watcher", int(vault["id"]),
                        )
                        recovered = True
            finally:
                if successor is not None:
                    await successor.aclose()
                await watcher.aclose()
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
            if isinstance(exc, OSError):
                await asyncio.to_thread(
                    _record_scan_component_health, vault_id, "source",
                    ok=False, error="Protection blocked: Source watcher cannot read the root",
                )
            else:
                await asyncio.to_thread(_report_worker_error, "source_watcher", exc, vault_id)
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


def _watch_root_signature(vault: dict[str, Any]) -> tuple[Any, ...]:
    # Reconnect may accept a different inode/mount at the SAME pathname. A
    # pathname-only signature would leave a watcher attached to the old tree.
    return (vault["source_root"], vault.get("root_identity"),
            vault.get("reconnect_volume_fingerprint"))


async def filesystem_watch_loop() -> None:
    """Keep one recursive filesystem watcher aligned with each enabled vault."""
    watchers: dict[int, tuple[tuple[Any, ...], asyncio.Task[None]]] = {}
    try:
        while True:
            await asyncio.to_thread(source_layout.verify_mounts_once)
            vaults = await asyncio.to_thread(_enabled_vaults)
            desired = {int(vault["id"]): vault for vault in vaults}
            for vault_id, (signature, task) in list(watchers.items()):
                vault = desired.get(vault_id)
                if vault is None or _watch_root_signature(vault) != signature or task.done():
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
                    await asyncio.to_thread(
                        _record_scan_component_health, vault_id, "source",
                        ok=False, error="Protection blocked: Source Volume unavailable",
                    )
                    existing = watchers.get(vault_id)
                    if existing is not None:
                        existing[1].cancel()
                        watchers.pop(vault_id, None)
                    continue
                if vault_id not in watchers:
                    watchers[vault_id] = (
                        _watch_root_signature(vault),
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
            stamp = now_iso()
            with status_lock:
                status["last_audit"] = stamp
                status["last_audit_report"] = report
                status["last_audit_healthy"] = bool(report.get("healthy", 1))
                status["last_error"] = None
            _record_scan_component_health(
                vault_id,
                "audit",
                ok=True,
                verified=True,
                extra=report,
            )
        except Exception as exc:
            message = f"Catalog audit: {exc}"
            with status_lock:
                status["last_error"] = message
            _record_scan_component_health(
                vault_id, "audit", ok=False, error=message,
                notify_problem=not isinstance(exc, cloud_preflight_gate.CloudPreflightBlocked),
            )


def _deliver_notifications_once() -> None:
    """Best-effort monitoring and delivery, with independent transactions.

    Monitoring faults must not suppress already durable terminal/incident email;
    network delivery must not hold the Owner's episode-scheduling lock.
    """
    try:
        from .services.protection_notifications import maintain_notifications
        with db() as connection:
            maintain_notifications(connection)
            report_problem(connection, event="internal_error", component="notification_monitor", active=False)
    except Exception as exc:
        _report_worker_error("notification_monitor", exc)
    try:
        with db() as connection:
            notification_service.reconcile_pending_terminal_notifications(
                connection
            )
            try:
                metrics_service.set_gauge(
                    "jobs_notification_backlog",
                    float(
                        notification_service.pending_terminal_notification_count(
                            connection
                        )
                    ),
                )
            except Exception:
                pass
            stats = notification_service.deliver_pending_notifications(
                connection,
                smtp_client=notification_service.production_smtp_client(),
            )
            report_problem(connection, event="internal_error", component="notification_delivery", active=False)
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
            runtime = _runtime_settings()
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
                metadata_backup_service.notify_owner_of_backup_failure(
                    connection,
                    reason="verify",
                    error_message=str(exc),
                )
        except Exception:
            pass


MAINTENANCE_TASKS = ("scan", "audit", "backup", "backup_verify")


def _seconds_since(value: Any, *, now: datetime | None = None) -> float | None:
    parsed = _parse_iso_datetime(value)
    if parsed is None:
        return None
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current - parsed).total_seconds())


def persisted_maintenance_ages(
    *, now: datetime | None = None
) -> dict[str, float | None]:
    """Return seconds since each maintenance task last ran, or None when never.

    The full scan is always due at startup: it is the reconciliation net for
    events missed while the process was down.  Audit, scheduled backup, and
    restore verification are read back from their durable records so a
    restart neither skips an overdue run nor repeats a recent one.
    """
    ages: dict[str, float | None] = {name: None for name in MAINTENANCE_TASKS}
    with db() as connection:
        vaults = connection.execute(
            """
            SELECT id FROM vaults
            WHERE enabled=TRUE AND decommission_state='active'
            """
        ).fetchall()
        audit_ages: list[float] = []
        for vault in vaults:
            row = connection.execute(
                """
                SELECT updated_at FROM vault_component_health
                WHERE vault_id=%s AND component='audit'
                """,
                (int(vault["id"]),),
            ).fetchone()
            age = _seconds_since(row["updated_at"], now=now) if row else None
            if age is None:
                audit_ages = []
                break
            audit_ages.append(age)
        if vaults and audit_ages:
            # The Vault audited longest ago decides when the next pass is due.
            ages["audit"] = max(audit_ages)
        backup = connection.execute(
            """
            SELECT MAX(created_at) AS at FROM metadata_backup_runs
            WHERE reason='scheduled'
            """
        ).fetchone()
        ages["backup"] = _seconds_since(backup["at"], now=now) if backup else None
        verify = connection.execute(
            "SELECT MAX(verified_at) AS at FROM metadata_backup_runs"
        ).fetchone()
        ages["backup_verify"] = (
            _seconds_since(verify["at"], now=now) if verify else None
        )
    return ages


def initial_maintenance_marks(
    current: float, ages: Mapping[str, float | None]
) -> dict[str, float]:
    """Translate persisted ages into loop-clock marks for ``background_loop``.

    ``loop.time()`` is a monotonic clock whose zero is arbitrary (seconds
    since boot on Linux), so a literal ``0.0`` mark would tie every schedule
    to host uptime.  A task that never ran gets ``-inf`` and is due at once.
    """
    marks: dict[str, float] = {}
    for name in MAINTENANCE_TASKS:
        age = ages.get(name)
        marks[name] = float("-inf") if age is None else float(current) - float(age)
    return marks


async def background_loop() -> None:
    await asyncio.sleep(5)
    loop = asyncio.get_running_loop()
    try:
        ages = await asyncio.to_thread(persisted_maintenance_ages)
    except Exception:
        ages = {}
    marks = initial_maintenance_marks(loop.time(), ages)
    last_scan = marks["scan"]
    last_audit = marks["audit"]
    last_backup = marks["backup"]
    last_backup_verify = marks["backup_verify"]
    try:
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
                await asyncio.to_thread(process_jobs_once, wait=False)
                await asyncio.to_thread(_deliver_notifications_once)
                # Converge durable directory aggregate dirty/rebuild work off the
                # /api/files request path (issue #229).
                await asyncio.to_thread(process_directory_aggregate_maintenance)
                await asyncio.to_thread(protection_stop_service.reconcile_all)
                # Backlog is measured independently from the selected batch: an
                # active lease is no longer claimable, while every remaining due
                # queued row is visible even when concurrency is small. Stuck
                # intermediate Jobs with expired leases are a separate series.
                queued_count = await asyncio.to_thread(claimable_queue_depth)
                metrics_service.set_gauge("queue_depth", float(queued_count))
                stuck_count, stuck_age = await asyncio.to_thread(
                    observe_stuck_lease_jobs
                )
                metrics_service.set_gauge("jobs_stuck", float(stuck_count))
                metrics_service.set_gauge(
                    "jobs_stuck_oldest_age_seconds", stuck_age
                )
                failed_count, failed_age = await asyncio.to_thread(
                    observe_failed_job_backlog
                )
                metrics_service.set_gauge(
                    "jobs_failed_backlog", float(failed_count)
                )
                metrics_service.set_gauge(
                    "jobs_failed_oldest_age_seconds", failed_age
                )
                current = loop.time()
                if current - last_scan >= runtime.scan_interval:
                    _submit_maintenance("scan", scan_all_vaults)
                    last_scan = current
                if current - last_audit >= runtime.audit_interval:
                    _submit_maintenance("audit", audit_all_vaults)
                    last_audit = current
                if (
                    runtime.metadata_backup_interval_seconds > 0
                    and current - last_backup
                    >= runtime.metadata_backup_interval_seconds
                ):
                    _submit_maintenance(
                        "backup", _run_scheduled_metadata_backup_once
                    )
                    last_backup = current
                if (
                    runtime.metadata_backup_verify_interval_seconds > 0
                    and current - last_backup_verify
                    >= runtime.metadata_backup_verify_interval_seconds
                ):
                    _submit_maintenance(
                        "backup_verify", _verify_latest_metadata_backup_once
                    )
                    last_backup_verify = current
                await asyncio.to_thread(_deliver_notifications_once)
            except Exception as exc:
                # Surface a generic internal-error problem instead of failing silently.
                try:
                    with db() as connection:
                        worker_error_store.record_worker_error(
                            connection,
                            component="background_loop",
                            exc=exc,
                        )
                    metrics_service.inc("worker_errors_total")
                except Exception:
                    # Last-resort: never let error accounting crash the loop.
                    pass
                metrics_service.set_gauge("worker_up", 0)
            batch_size = max(10, runtime.operation_concurrency * 10)
            delay = (
                0.1
                if queued_count >= batch_size
                else min(
                    NOTIFICATION_MAX_LATENCY_SECONDS,
                    max(0.1, float(runtime.queue_poll_interval or 2)),
                )
            )
            await asyncio.sleep(delay)
    except asyncio.CancelledError:
        # Lifespan owns pool draining after all producers have been cancelled.
        # Detaching here would lose the running futures before they can drain.
        raise
