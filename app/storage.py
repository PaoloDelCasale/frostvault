from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import random
import re
import stat
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, NamedTuple

import boto3
import psycopg
from boto3.s3.transfer import TransferConfig
from .services.s3_clients import s3_client as _cached_s3_client
from .services.s3_upload import upload_file
from .services.upload_snapshot import (
    UploadSnapshot, prepare_upload_snapshot, validated_s3_proof, UploadProofMismatch,
    unsupported_checksum_api,
)
from watchfiles import Change, awatch

from .config import is_placeholder, settings, settings_snapshot
from .catalog import ArchiveCatalog
from .database import db
from .services import audit_failures, source_layout
from .services.upload_pipeline import UploadJournalHeld
from .services import local_observation
from .services.restore_ghosts import retire_absent_restore_ghosts
from .services import vault_relocation
from .services import watch_plan
from .services.catalog_event_hub import publish_committed_event
from .services.catalog_events import record_catalog_revision
from .i18n import DEFAULT_LOCALE, format_message_params, parse_message_params, translate
from .services.s3_preflight import check_bucket_readiness, failure_message_key, preflight_failure_message
from .services import cloud_preflight_gate
from .services.catalog_audit import audit_vault_catalog
from .services.directory_aggregates import (
    invalidate_for_vault_file,
    mark_path_dirty,
    process_directory_aggregate_maintenance,
    unit_dirty_marks,
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
    exception_chain, is_retryable_transaction_error, owned_connection,
    retry_database_unit,
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
from .services.fs_preflight import PinnedTree, TreeIdentityChanged, inspect_entry, removal_is_unproven
from .services.problems import display_path, report_problem, resolve_stale_path_problems
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
from .services.vaults import VAULT_ROW_SQL
from .services import vault_moves
from .services import protection_stop as protection_stop_service
from .services.shutdown import (
    WORKER_DRAIN_SECONDS, OperationCancelled, WorkerStopping, shutdown_state,
)
from .services import audit_events as audit_event_store
from .services.restore_estimates import estimate_restore
from .services.job_admission import JOB_LEASE_HELD_STATUSES, TERMINAL_JOB_STATUSES
from .services.job_scheduler import job_priority, select_fair_jobs, turn_is_aged
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
_operation_completion_notify: Callable[[], None] | None = None
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
# A foreground checkpoint extends the lease at most this often and otherwise
# only reads it. Far below the keeper's lease/3 cadence, so a claim that passes
# checkpoints is never closer to expiry than one that only has its keeper.
CLAIM_CHECKPOINT_RENEW_SECONDS = 15.0
EXPIRED_LEASE_RECONCILE_BATCH = 100
# How often the backlog gauges and the stall detector re-read a deep queue.
BACKLOG_OBSERVE_SECONDS = 10.0
# How often the background loop folds dirty directories into their aggregates
# (#622). While the queue is deep the loop spins every 100 ms: flushing on every
# cycle recomputed each ancestor directory for almost every finished file. A
# batch per interval recomputes it once for all the files finished meanwhile.
# Freshness: a listing that finds dirty directories reports ``stale`` with the
# previous projection and schedules its own refresh at once (ADR-0038).
DIRECTORY_AGGREGATE_BATCH_SECONDS = 2.0
JOB_TERMINAL_STATUSES = frozenset(TERMINAL_JOB_STATUSES)
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


@retry_database_unit
def _claim_is_live(job_id: int, claim_token: str) -> bool:
    with owned_connection(db()) as connection:
        return ArchiveCatalog(connection).job_claim_is_live(
            job_id=int(job_id), claim_token=claim_token, now=now_iso(),
        )


def _checkpoint_claim(job_id: int, claim_token: str) -> bool:
    """Durable ownership checkpoint of the foreground worker's own claim.

    Every checkpoint still asks PostgreSQL whether the claim is live, so
    cancellation, takeover and expiry are observed exactly as before. Only the
    lease *extension* is amortized: a small upload passes ~20 checkpoints in a
    few seconds, and extending a five-minute lease each time cost one WAL-logged
    non-HOT ``jobs`` update and a synchronous commit per checkpoint.
    """
    renewed = getattr(_worker_claim, "renewed", None)
    current = time.monotonic()
    if (
        renewed is not None and renewed[0] == (int(job_id), claim_token)
        and current - renewed[1] < CLAIM_CHECKPOINT_RENEW_SECONDS
    ):
        return _claim_is_live(job_id, claim_token)
    if _renew_claim_if_owned(job_id, claim_token) is None:
        return False
    _worker_claim.renewed = ((int(job_id), claim_token), current)
    return True


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
    if claim_token and not _checkpoint_claim(job_id, claim_token):
        _mark_claim_lost(job_id)
        raise JobLeaseLost(message)


def ensure_job_claim_owned_in_transaction(
    connection: Any,
    job: dict[str, Any],
    message: str,
) -> None:
    """Fence a catalog/audit transaction behind the currently owned lease.

    The conditional renewal locks the Job row, then its lease, until the
    surrounding transaction commits (the claim's lock order).  A cancellation/takeover that won first therefore
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
        lock_job=True,
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
        self._report_lock = threading.Lock()
        self._committed: int | None = None
        # The first report waits one interval too: a small transfer then costs
        # no progress write at all before its final value.
        self._last_report = time.monotonic()
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
        if should_report:
            self._report()

    def _report(self) -> None:
        # Serialize SDK callbacks and flush so an older value cannot commit after
        # a newer one. Dedup only a positively committed CAS, never an attempted
        # write (including a commit whose outcome is unknown).
        with self._report_lock:
            with self._lock:
                value = self.transferred
                if value == self._committed:
                    return
            if set_job_progress(self.job_id, value, claim_token=self.claim_token) is True:
                self._committed = value

    def flush(self) -> None:
        with self._lock:
            self._last_report = time.monotonic()
        self._report()


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
    return _cached_s3_client(region=settings.aws_region, preflight=preflight,
                             non_idempotent=non_idempotent, factory=boto3.client)


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
        key = failure_message_key(result)
        # Keyed failures store the English catalog text, so the readiness gate
        # can localize it for each viewer (cloud_preflight_gate.remediation_key).
        raise cloud_preflight_gate.ReadinessFailure(
            translate(key, locale="en") if key
            else "S3 bucket preflight failed: " + preflight_failure_message(result)
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
    # Names that cannot be catalogued (not valid UTF-8). They are reported as
    # their own problem and skipped; they do not make the walk incomplete, as no
    # catalog row can exist at or below such a path.
    unsupported_names: list[str] = []
    reported_unsupported: set[str] = set()
    # What this walk could not observe, for the watcher and the problem closure.
    unreadable_all: set[str] = set()
    unreadable_directories: set[str] = set()
    unsupported_all: set[str] = set()
    unsupported_directories: set[str] = set()

    def _persisted_scan(vault_row: dict[str, Any]) -> bool:
        return "root_identity" in vault_row or "root_identity_version" in vault_row

    def flush() -> None:
        nonlocal count
        if not pending and not unreadable_paths and not unsupported_names:
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
                    f"SELECT {_JOURNAL_COLUMNS_SQL} FROM files WHERE path=%s AND vault_id=%s",
                    (relative, vault_id),
                ).fetchone()
        for relative in unreadable_paths:
            report_problem(connection, event="file_unreadable", vault_id=vault_id, path=relative)
        unreadable_paths.clear()
        for relative in unsupported_names:
            report_problem(connection, event="file_name_unsupported", vault_id=vault_id, path=relative)
        unsupported_names.clear()
        catalog = ArchiveCatalog(connection)
        # Issue #242: one Vault row-lock touch, one bulk identity lookup, and
        # one bulk upsert replace the per-file round-trips that pinned Postgres
        # CPU on large Vaults.  The batch is semantically identical to the
        # serialized per-file path (same upsert columns, digest preservation,
        # dirty ancestors).
        # Directory replacement can preserve size/mtime. A watcher subtree
        # invalidates real changes, not independently verified file echoes.
        catalog.observe_local_copies_batch(
            vault_id=vault_id,
            entries=pending,
            seen_at=scan_id,
            invalidate_paths=invalidate_paths,
        )
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
        relative = entry.relative_to(root).as_posix()
        unreadable_paths.append(relative)
        unreadable_all.add(relative)

    def failed_directory(entry: Path, error: OSError) -> None:
        failed(entry, error)
        unreadable_directories.add(entry.relative_to(root).as_posix())

    unsupported_seen = False

    def unsupported(entry: Path, is_directory: bool) -> None:
        # A name that cannot be catalogued cannot be told apart from the old
        # name it may have replaced (a rename, or a client with another
        # charset): the rows under it must not become "missing". Like an
        # unreadable path, it makes this observation incomplete (#494).
        nonlocal unsupported_seen
        unsupported_seen = True
        if scan_journal is not None:
            scan_journal["incomplete"] = True
        relative = entry.relative_to(root).as_posix()
        reported_unsupported.add(display_path(relative))
        unsupported_names.append(relative)
        unsupported_all.add(relative)
        if is_directory:
            unsupported_directories.add(relative)

    with PinnedTree(root, subtree=subtree) as tree:
        if _persisted_scan(vault) and vault_relocation.opened_root_identity(tree.fd) != vault.get("root_identity"):
            raise _ScanRootMismatch("Vault root changed before traversal")
        for entry, pinned_entry in tree.entries(on_error=failed_directory, on_unsupported_name=unsupported):
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
        found = watch_plan.UnobservedPaths(
            unreadable=frozenset(unreadable_all),
            unsupported=frozenset(unsupported_all),
            directories=frozenset(unreadable_directories | unsupported_directories),
        )
        if subtree is None:
            watch_plan.record_full_scan(vault_id, found)
        else:
            watch_plan.record_subtree_scan(
                vault_id, subtree.relative_to(root).as_posix(), found,
            )
        if scan_journal is not None:
            journal_paths = scan_journal.setdefault("unreadable_paths", set())
            journal_paths.update(display_path(path) for path in unreadable_all)
            scan_journal.setdefault("unreadable_directories", set()).update(
                display_path(path) for path in unreadable_directories
            )
        if subtree is None and not unreadable:
            # A complete walk of the whole root is the proof that every earlier
            # unsupported name is gone; a partial or incomplete walk proves nothing.
            resolve_stale_path_problems(
                connection, event="file_name_unsupported", vault_id=vault_id,
                keep_paths=reported_unsupported,
            )
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
    incomplete = unreadable or unsupported_seen
    if finalize_missing and not incomplete and safe_scan_result and (
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
        if not incomplete and safe_scan_result and alias and source_layout.requires_full_local_scan(alias)
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
            "SELECT lifecycle FROM vaults WHERE id=%s", (vault["id"],),
        ).fetchone()
    if state and state["lifecycle"] == "reconnect_required":
        raise RuntimeError("Source watcher suspended for Vault relocation")
    if state and state["lifecycle"] != "active":
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
                # Even a same-size write with a restored mtime restarts the
                # window. Persist the event, not an in-memory debounce timer.
                catalog.observe_local_copy(
                    vault_id=vault["id"],
                    path=relative,
                    file_type=file_type,
                    size=entry_stat.st_size,
                    mtime_ns=entry_stat.st_mtime_ns,
                    seen_at=event_id,
                    observed_at=event_id,
                    invalidate_content=True,
                )
                changed += 1
                continue

            if entry.exists():
                # Directory metadata changes do not alter catalogued files.
                continue
            if removal_is_unproven(root, entry.parent.resolve() / entry.name):
                # Possibly renamed to a name that cannot be catalogued: not a
                # removal until a scan says so (and that scan is incomplete
                # while the name exists, so nothing becomes missing).
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


def scan_cloud(
    vault: dict[str, Any], scan_id: str, *, reconnect: bool = False,
    report: dict[str, int] | None = None,
) -> int:
    """Observe one complete cloud listing against the exact catalog versions.

    The listing is observation, never ownership or authority (ADR-0035):

    - only a version the catalog already knows by its exact object key and
      VersionId is marked listed (available), in bounded write batches;
    - a known version the listing did not show is marked missing only after a
      complete, interpretable listing, and only if this scan generation is
      still current;
    - an unknown version, an unknown object and every delete marker are
      counted in ``report`` and nothing else: never recorded as a version,
      attributed to a file by its path, promoted, or deleted.

    Provider listings stay outside write transactions. Returns the number of
    known versions the listing showed.
    """
    from .services.vaults import vault_prefix

    validate_cloud_vault(vault)
    client = s3_client()
    paginator = client.get_paginator("list_object_versions")
    prefix = vault_prefix(vault["uuid"])
    kwargs: dict[str, Any] = {"Bucket": vault["s3_bucket"], "Prefix": prefix}
    listed: list[tuple[str, str, str]] = []
    latest: dict[str, dict[str, Any]] = {}
    markers = 0
    invalid_keys = False
    for page in paginator.paginate(**kwargs):
        for item in page.get("Versions", []):
            object_key = str(item["Key"])
            if object_key_to_path(object_key, prefix) is None:
                # Directory marker objects contain no file content. Any other
                # unrecognised key prevents the missing transition.
                if not object_key.endswith("/"):
                    invalid_keys = True
                continue
            listed.append((
                object_key, str(item["VersionId"]),
                str(item.get("StorageClass") or "STANDARD"),
            ))
            if reconnect:
                _note_latest_listed_version(latest, item)
        markers += len(page.get("DeleteMarkers", []))
        if reconnect:
            for marker in page.get("DeleteMarkers", []):
                _note_latest_listed_version(latest, {**marker, "_delete_marker": True})
    if reconnect and invalid_keys:
        raise RuntimeError("Cloud listing is ambiguous; reconnect remains suspended")

    count = 0
    for offset in range(0, len(listed), CLOUD_SCAN_WRITE_BATCH_SIZE):
        if not _cloud_scan_still_current(vault, scan_id):
            return count
        batch = listed[offset : offset + CLOUD_SCAN_WRITE_BATCH_SIZE]
        with db() as connection:
            # The listed storage class of an exact known version is
            # observation of that version, like its availability.
            known = connection.execute(
                """
                UPDATE archive_versions av
                SET storage_class = item.storage_class
                FROM unnest(%s::text[], %s::text[], %s::text[])
                     AS item(object_key, version_id, storage_class),
                     files f
                WHERE av.object_key = item.object_key
                  AND av.provider_version_id = item.version_id
                  AND f.id = av.file_id AND f.vault_id = %s
                RETURNING av.id
                """,
                (
                    [entry[0] for entry in batch], [entry[1] for entry in batch],
                    [entry[2] for entry in batch], int(vault["id"]),
                ),
            ).fetchall()
            ArchiveCatalog(connection).mark_archive_versions_listed(
                [str(row["id"]) for row in known], scan_id=scan_id,
            )
        count += len(known)
    if report is not None:
        report.update(
            listed_versions=len(listed),
            known_versions=count,
            unknown_versions=len(listed) - count,
            delete_markers=markers,
            unrecognized_keys=int(invalid_keys),
        )
    # An unrecognised key or a truncated listing is not evidence that
    # prior catalog rows disappeared. Fail closed by retaining availability.
    if not invalid_keys and _cloud_scan_still_current(vault, scan_id):
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
        _reconcile_restored_cloud(vault, scan_id, latest, prefix=prefix)
        _verify_reconnected_cloud(vault, scan_id)
    return count


def _note_latest_listed_version(latest: dict[str, dict[str, Any]], item: dict[str, Any]) -> None:
    """Keep the newest listed entry per key (``IsLatest``, else LastModified)."""
    key = str(item["Key"])
    seen = latest.get(key)
    if item.get("IsLatest") is True:
        latest[key] = item
        return
    if seen is not None and seen.get("IsLatest") is True:
        return
    if seen is None or (
        item.get("LastModified") is not None
        and (seen.get("LastModified") is None or item["LastModified"] > seen["LastModified"])
    ):
        latest[key] = item


def _reconcile_restored_cloud(
    vault: dict[str, Any], scan_id: str, latest: dict[str, dict[str, Any]], *, prefix: str,
) -> int:
    """First Reconnect after a manual metadata restore: catch up the cloud.

    The restored catalog predates the backup; a catalogued file may have been
    replaced in the cloud afterwards (its old Current possibly already
    cleaned up). Only for a Vault queued by ``record_metadata_restore``, and
    only for a key of a file the catalog already knows, the newest listed
    version that the catalog does not know becomes the file's replacement in
    progress and then Current after its exact VersionId has been read and
    hashed (read-back proof). Nothing is deleted, no unknown path is imported,
    and a cold or unreadable version leaves the reconnect suspended.
    """
    from .services import recovery_kit

    vault_id = int(vault["id"])
    with db() as connection:
        if not recovery_kit.restore_reconciliation_pending(connection, vault_id):
            return 0
        rows = connection.execute(
            """
            SELECT f.id, f.path, f.replacing_version_id
            FROM files f WHERE f.vault_id=%s AND f.cloud_since IS NOT NULL
            """,
            (vault_id,),
        ).fetchall()
        known = {
            (str(row["object_key"]), str(row["provider_version_id"]))
            for row in connection.execute(
                """
                SELECT av.object_key, av.provider_version_id
                FROM archive_versions av JOIN files f ON f.id = av.file_id
                WHERE f.vault_id=%s AND av.provider_version_id IS NOT NULL
                """,
                (vault_id,),
            ).fetchall()
        }
    files = {str(row["path"]): row for row in rows}
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for object_key, item in latest.items():
        if item.get("_delete_marker"):
            continue
        path = object_key_to_path(object_key, prefix)
        row = files.get(path) if path is not None else None
        if row is None or (object_key, str(item["VersionId"])) in known:
            continue
        candidates.append((row, item))
    client = s3_client() if candidates else None
    reconciled = 0
    for row, item in candidates:
        if not _cloud_scan_still_current(vault, scan_id):
            raise RuntimeError("Reconnect scan superseded")
        storage_class = str(item.get("StorageClass") or "STANDARD")
        if storage_class in {"GLACIER", "DEEP_ARCHIVE"}:
            # Reading needs a cloud restore first; Current stays unlocated
            # and the reconnect suspended, never elected without a proof.
            continue
        file_id = str(row["id"])
        version_id = str(item["VersionId"])
        size = int(item.get("Size") or 0)
        with db() as connection:
            catalog = ArchiveCatalog(connection)
            if row["replacing_version_id"] is not None:
                catalog.abandon_replacement(
                    file_id, archive_version_id=str(row["replacing_version_id"]),
                )
            archive_version_id = catalog.begin_replacement(
                file_id, object_key=str(item["Key"]), provider_version_id=version_id,
                size=size, storage_class=storage_class,
            )
            catalog.record_verification_evidence(
                archive_version_id, method="read_back", fallback=None, egress_bytes=size,
            )
        digest = hashlib.sha256()
        read = 0
        response = client.get_object(
            Bucket=vault["s3_bucket"], Key=str(item["Key"]), VersionId=version_id,
        )
        if str(response.get("VersionId") or version_id) != version_id:
            raise RuntimeError("S3 returned another version; reconnect remains suspended")
        body = response["Body"]
        try:
            for chunk in iter(lambda: body.read(1024 * 1024), b""):
                digest.update(chunk)
                read += len(chunk)
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        with db() as connection:
            catalog = ArchiveCatalog(connection)
            if read != size:
                catalog.abandon_replacement(
                    file_id, archive_version_id=archive_version_id,
                    mismatch=True, checked_at=now_iso(),
                )
                raise RuntimeError("Cloud version size differs from its listing")
            catalog.promote_replacement(
                file_id, archive_version_id=archive_version_id,
                plaintext_sha256=digest.digest(), verified_at=now_iso(),
            )
            catalog.mark_archive_versions_listed([archive_version_id], scan_id=scan_id)
            recovery_kit.remember_cloud_update(connection, file_id)
        reconciled += 1
    return reconciled


def _verify_reconnected_cloud(vault: dict[str, Any], scan_id: str) -> None:
    """A reconnect resumes only when every Current is located, never elected.

    Current is an explicit, verified pointer: nothing is re-elected from the
    listing order or the Path History, and nothing is deleted. A Current the
    complete listing did not locate leaves the reconnect suspended until the
    Owner resolves it.
    """
    if not _cloud_scan_still_current(vault, scan_id):
        raise RuntimeError("Reconnect scan superseded")
    with db() as connection:
        unlocated = connection.execute(
            """
            SELECT count(*) AS total
            FROM files f JOIN archive_versions cur ON cur.id = f.current_version_id
            WHERE f.vault_id=%s AND cur.availability <> 'available'
            """,
            (int(vault["id"]),),
        ).fetchone()["total"]
    if unlocated:
        raise RuntimeError("Current cloud copies are missing; reconnect remains suspended")


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
            f"{VAULT_ROW_SQL} WHERE v.id=%s", (int(vault["id"]),)
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
        SELECT source_root, lifecycle, root_identity_version, root_identity
        FROM vaults WHERE id=%s
        """,
        (int(vault["id"]),),
    ).fetchone()
    if not isinstance(current, dict) or current.get("source_root") != vault.get("source_root"):
        raise _ScanRootMismatch("Vault root changed during local scan")
    if current.get("lifecycle", "active") not in {"active", "reconnect_required"}:
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
        return ArchiveCatalog(connection).local_copies_to_fingerprint(vault_id)


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
                scan_journal.setdefault("unreadable_paths", set()).add(display_path(row["path"]))
            with db() as connection:
                report_problem(
                    connection, event="file_unreadable", vault_id=int(vault["id"]),
                    path=row["path"],
                )
            continue
        except (ValueError, RuntimeError):
            if scan_journal is not None:
                scan_journal["incomplete"] = True
                # Not attributable to a path: no problem may be closed on this walk.
                scan_journal["unattributed"] = True
            continue
        updates.append(
            {
                "vault_file_id": row["vault_file_id"],
                "path": row["path"],
                "generation": row["generation"],
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
            catalog = ArchiveCatalog(connection)
            for update in batch:
                # A watcher may have replaced the file after hashing, or the
                # file changed while it was hashed: the generation and stat
                # compare-and-swap leave the digest unpublished; the next
                # observation hashes it again.
                if (update["size"], update["mtime_ns"]) != (
                    update["old_size"], update["old_mtime_ns"],
                ):
                    continue
                catalog.publish_local_fingerprint(
                    str(update["vault_file_id"]),
                    generation=int(update["generation"]),
                    plaintext_sha256=update["digest"],
                    size=update["size"],
                    mtime_ns=update["mtime_ns"],
                )
            if persisted:
                _current_verified_scan_row(connection, vault)


# The Local Copy columns a scan journal saves before observing a path, so a
# scan that loses its proof can restore them (#593).
_JOURNAL_COLUMNS_SQL = """
    id, local_presence, local_file_type, local_size, local_mtime_ns,
    local_sha256, matched_version_id, held_version_id, local_seen_at,
    local_observed_at, local_content_observed_at, local_recovered_at
"""


def _scan_timestamp(value: Any) -> datetime:
    """A scan id is its start instant; compare it as a timestamptz."""
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


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
                    # A file this scan created: removed only while nothing
                    # attached to it (no cloud content, no Job, no history).
                    deleted = connection.execute(
                        """
                        DELETE FROM files f
                        WHERE f.vault_id=%s AND f.path=%s AND f.local_seen_at=%s
                          AND f.cloud_since IS NULL
                          AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.file_id=f.id)
                          AND NOT EXISTS (
                              SELECT 1 FROM file_path_history h WHERE h.file_id=f.id
                          )
                        """,
                        (vault_id, path, _scan_timestamp(scan_id)),
                    ).rowcount
                    if deleted:
                        mark_path_dirty(connection, vault_id, path)
                    continue
                # Restore the previous observation unless a newer one won; the
                # restore is itself a new observation (generation + 1), so a
                # hash taken on the compensated one can never publish.
                connection.execute(
                    """
                    UPDATE files
                    SET generation=generation + 1,
                        local_presence=%s, local_file_type=%s, local_size=%s,
                        local_mtime_ns=%s, local_sha256=%s, matched_version_id=%s,
                        held_version_id=%s, local_seen_at=%s, local_observed_at=%s,
                        local_content_observed_at=%s, local_recovered_at=%s
                    WHERE id=%s AND vault_id=%s AND local_seen_at=%s
                    """,
                    (
                        snapshot["local_presence"],
                        snapshot["local_file_type"],
                        snapshot["local_size"],
                        snapshot["local_mtime_ns"],
                        snapshot["local_sha256"],
                        snapshot["matched_version_id"],
                        snapshot["held_version_id"],
                        snapshot["local_seen_at"],
                        snapshot["local_observed_at"],
                        snapshot["local_content_observed_at"],
                        snapshot["local_recovered_at"],
                        snapshot["id"],
                        vault_id,
                        _scan_timestamp(scan_id),
                    ),
                )
                mark_path_dirty(connection, vault_id, path)
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
        if not scan_journal.get("unattributed"):
            # Each unreadable path has its own entry: one that this walk and the
            # hashing no longer found unreadable is closed by itself, whatever
            # else is still unreadable. A path below a folder this walk could not
            # enter stays open: its state is unknown, not repaired.
            with db() as connection:
                resolve_stale_path_problems(
                    connection, event="file_unreadable", vault_id=int(vault["id"]),
                    keep_paths=scan_journal.get("unreadable_paths", ()),
                    keep_prefixes=scan_journal.get("unreadable_directories", ()),
                )

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
                recognize_renames=complete and vault.get("lifecycle") != "reconnect_required",
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
                    "SELECT lifecycle FROM vaults WHERE id=%s",
                    (int(vault["id"]),),
                ).fetchone()["lifecycle"]
                == "reconnect_required"
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


# (component, vault) pairs whose internal-error problem is known to be closed
# since the last failure. The loops that call ``_note_worker_ok`` run every few
# seconds; this keeps a healthy worker from writing to the inbox on each pass.
_workers_known_ok: set[tuple[str, int | None]] = set()
_workers_known_ok_lock = threading.Lock()


def _report_worker_error(component: str, exc: BaseException, vault_id: int | None = None) -> None:
    with _workers_known_ok_lock:
        _workers_known_ok.discard((component, vault_id))
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


def _resolve_worker_error(component: str, vault_id: int | None = None) -> bool:
    """Close the internal-error problem for a worker that is healthy again.

    Best effort, like the report: a failing resolve must never stop the worker.
    """
    try:
        with db() as connection:
            report_problem(
                connection, event="internal_error", vault_id=vault_id,
                component=component, active=False,
            )
        return True
    except Exception as resolve_error:
        worker_error_store.log_report_failure(component, resolve_error, resolving=True)
        return False


def _note_worker_ok(component: str, vault_id: int | None = None) -> None:
    """A worker finished a pass: close its internal-error problem, once.

    The first pass after a start or after a failure closes any open problem of
    that worker, so an error that was fixed no longer waits for the Owner (#496).
    """
    key = (component, vault_id)
    with _workers_known_ok_lock:
        if key in _workers_known_ok:
            return
    if _resolve_worker_error(component, vault_id):
        with _workers_known_ok_lock:
            _workers_known_ok.add(key)


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
    problem_params: Mapping[str, Any] | None = None,
) -> None:
    """Persist one scan/audit outcome and surface failures without diagnostic UI."""
    from .services.component_health import record_component_health

    stamp = now_iso()
    try:
        with db() as connection:
            if component == "audit":
                # Even behind a blocked gate: each Vault it leaves unconfirmed
                # says why, until its own audit succeeds (#620).
                audit_failures.set_audit_problem(
                    connection, vault_id=int(vault_id), active=not ok,
                    params=dict(problem_params or {}),
                )
            elif component in {"source", "cloud", "policy"} and (ok or notify_problem):
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
            vault.get("lifecycle") != "reconnect_required"
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
        reconnect = vault.get("lifecycle") == "reconnect_required"
        cloud_report: dict[str, int] = {}
        if reconnect:
            try:
                # Cloud first, with NO cleanup: stale local metadata must not
                # become upload/release authority before S3 has been recovered.
                result["cloud"] = scan_cloud(vault, scan_id, reconnect=True, report=cloud_report)
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
                registered_before_scan = watch_plan.scan_coverage_fence(vault_id)
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
                # A repaired directory can be walked before the observer that
                # excluded it has been replaced. Keep source unconfirmed until
                # registration AND the ensuing catch-up walk have completed.
                covered = watch_plan.source_confirmation_ready(vault_id, registered_before_scan)
                confirmed = complete and covered
                _record_scan_component_health(
                    vault_id, "source", ok=confirmed, verified=confirmed,
                    error=(None if confirmed else
                           "Source watcher registration pending" if complete else
                           "Source scan incomplete: unreadable paths or unsupported names"),
                    notify_problem=confirmed,
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
                    result["cloud"] = scan_cloud(vault, scan_id, report=cloud_report)
            except Exception as exc:
                status["last_error"] = f"Cloud scan: {exc}"
                result["cloud"] = -1
                _record_scan_component_health(
                    vault_id, "cloud", ok=False, error=status["last_error"],
                    notify_problem=not isinstance(exc, cloud_preflight_gate.CloudPreflightBlocked),
                )
            else:
                # Unknown versions and markers are reported, never attributed.
                _record_scan_component_health(
                    vault_id, "cloud", ok=True, verified=True, extra=cloud_report or None,
                )
            try:
                validate_cloud_vault(vault)
                with db() as connection:
                    result["lifecycle_rules"] = sync_lifecycle_rules_for_bucket(
                        connection,
                        s3_client(),
                        bucket=vault["s3_bucket"],
                    )
                    local_access = source_layout.vault_local_access(vault["source_root"])
                    if local_access.local_operations_allowed:
                        result["auto_local_cleanups"] = queue_auto_local_cleanups(
                            connection,
                            vault_id=int(vault["id"]),
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
                    local_access = source_layout.vault_local_access(vault["source_root"])
                    if local_access.local_operations_allowed:
                        result["auto_uploads"] = queue_auto_uploads(
                            connection, vault_id=vault_id,
                            source_root=str(vault["source_root"]),
                        )
                if "auto_uploads" in result:
                    _note_worker_ok("auto_upload_queue", vault_id)
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
    if hash_missing:
        for row in catalog.local_copies_to_fingerprint(int(vault["id"])):
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
            if (stat_result.st_size, stat_result.st_mtime_ns) != (row["size"], row["mtime_ns"]):
                # Changed while hashed: the next observation hashes it again.
                continue
            if catalog.publish_local_fingerprint(
                row["vault_file_id"], generation=row["generation"],
                plaintext_sha256=digest, size=row["size"], mtime_ns=row["mtime_ns"],
            ):
                summary["hashed"] += 1

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
            _connection, vault, root=root,
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
    remediation_reason: str | None = None,
) -> bool:
    """Persist a state transition without letting a cancelled/lost claim win.

    Worker calls inherit their token from ``_job_claim_context``.  A status
    change then becomes a compare-and-swap: a concurrent cancellation or a new
    claimant makes this a harmless no-op instead of resurrecting the Job.
    Direct administrative/test callers without a claim retain the historic
    update behavior.
    """
    if _claim_is_lost(job_id):
        return False
    with _defer_job_counters():
        with owned_connection(db()) as connection:
            return _set_job_in_transaction(
                connection, job_id, status, message,
                message_key=message_key, message_params=message_params,
                remediation_suspension=remediation_suspension,
                remediation_reason=remediation_reason,
            )


def _set_job_in_transaction(
    connection: Any, job_id: int, status: str, message: str = "", *,
    message_key: str | None = None,
    message_params: dict[str, Any] | None = None,
    remediation_suspension: str | None = None,
    remediation_reason: str | None = None,
    transferred_bytes: int | None = None,
    fenced_unit: bool = False,
) -> bool:
    """DB-only transition; caller owns commit/retry and deferred counters.

    This is the same transition used by set_job, not a second completion path.
    Terminal/retry transitions still lock the Vault exclusively; never invoke
    from an audited SHARE callback. Final upload progress can share its CAS.

    ``fenced_unit``: the caller is an exclusive claim-fenced upload unit
    (``_upload_catalog_unit``), which already holds the Vault row NO KEY UPDATE
    and renewed this lease under the Job row lock in the same transaction. The
    Vault lock and a held status's second renewal are then not asked again; a
    terminal status still ends the lease by its own CAS.
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
    vault_lock = "NO KEY UPDATE"
    current = None
    if (held and claim_token
            and remediation_suspension is None and remediation_reason is None):
        # A held step reads its subject before the Vault lock, whose mode it
        # decides; its previous status feeds no counter.
        current = _transition_subject(connection, job_id)
    # An unbound held step moves no Upload Remediation episode (see below).
    unbound_held = bool(current and current["unbound"])
    if unbound_held:
        # ADR-0031: a held step changes no admission predicate. Keep Vault-first
        # order before locking Jobs or append-only aggregate dirty FK marks.
        vault_lock = "KEY SHARE"
    if not fenced_unit:
        connection.execute(
            f"SELECT id FROM vaults WHERE id=(SELECT vault_id FROM jobs WHERE id=%s) FOR {vault_lock}",
            (job_id,),
        )
    if current is None:
        current = _transition_subject(connection, job_id)
    if current is not None:
        previous_status, action = current["status"], current["action"]
    catalog = ArchiveCatalog(connection)
    updated = catalog.transition_job(
        job_id=job_id,
        status=status,
        message=message,
        message_key=message_key,
        message_params=format_message_params(params) if message_key else None,
        now=timestamp,
        claim_token=claim_token,
        claim_expires_at=_claim_expiry_at(timestamp) if held else None,
        transferred_bytes=transferred_bytes,
        lease_renewed=fenced_unit,
    )
    if updated and action == "upload":
        from .services import upload_remediation
        if not unbound_held:
            # Bindings are written only with the Job's creation or completion:
            # an unbound held step has no episode to move (ADR-0035, #592).
            upload_remediation.transition(
                connection, job_id, status, timestamp,
                suspension=remediation_suspension, closing_reason=remediation_reason,
                bound=not current["unbound"], job=catalog.transitioned_job,
            )
        # Same row as _dirty_job_protection would read again.
        mark_path_dirty(connection, int(current["vault_id"]), current["path"])
        if remediation_reason == "file_removed" and current is not None:
            ArchiveCatalog(connection).mark_local_path_missing(
                vault_id=int(current["vault_id"]), path=current["path"], observed_at=timestamp,
            )
    if updated and status in {"completed", "failed"}:
        if action == "upload" and status == "completed":
            # A completed upload notifies no one (see
            # enqueue_job_terminal_notification): only leave the outbox.
            notification_service.mark_terminal_notification_done(connection, job_id=job_id)
        else:
            notification_service.enqueue_job_terminal_notification_best_effort(
                connection, job_id=job_id)
        if previous_status not in JOB_TERMINAL_STATUSES:
            _record_committed_job_counter("completed" if status == "completed" else "failed", action)
    return updated


def _set_job_message_in_transaction(connection: Any, job_id: int, message_key: str) -> None:
    """Change only the visible message of a claimed, running Job.

    No status, lease, protection or remediation change: the caller's
    claim-fenced unit already holds the Job row. A Job that moved on keeps its
    own message (the CAS matches only the status the run is in).
    """
    connection.execute(
        """UPDATE jobs SET message=%s, message_key=%s, message_params=NULL, updated_at=%s
           WHERE id=%s AND status IN ('uploading', 'verifying')""",
        (translate(message_key, locale=DEFAULT_LOCALE), message_key, now_iso(), job_id),
    )


def _transition_subject(connection: Any, job_id: int) -> dict[str, Any] | None:
    """The Job a transition moves, and whether it is unbound, in one read.

    ``unbound``: a held-status step of ``job_id`` writes nothing admission
    reads. An upload bound to an Upload Remediation episode also moves that
    episode (``upload_remediation.transition``); it keeps the exclusive Vault
    lock. Bindings are written only with the Job's creation or completion,
    never while a claimant holds it, so this unlocked read cannot go stale.
    """
    return connection.execute(
        """
        SELECT j.status, j.action, j.vault_id, f.path,
               NOT (
                   j.action='upload'
                   AND EXISTS (SELECT 1 FROM upload_remediation_jobs b WHERE b.job_id=j.id)
               ) AS unbound
        FROM jobs j JOIN files f ON f.id = j.file_id WHERE j.id=%s
        """,
        (job_id,),
    ).fetchone()


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
        # Stays exclusive (ADR-0031, #535): releasing the lease returns the
        # Job to the unstarted set ('retrying') that cancel-pending and the
        # scheduler select, and moves a bound remediation episode; sharing
        # was not proven safe, and retries are rare.
        connection.execute("SELECT id FROM vaults WHERE id=(SELECT vault_id FROM jobs WHERE id=%s) FOR NO KEY UPDATE", (job_id,))
        current = connection.execute(
            "SELECT action FROM jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
        if current is not None:
            action = current["action"]
        updated = ArchiveCatalog(connection).transition_job(
            job_id=job_id,
            status="retrying",
            message=message,
            message_key=message_key,
            message_params=format_message_params(params) if message_key else None,
            now=timestamp,
            claim_token=claim_token,
            run_after=retry_after,
            retry_count=retry_count,
        )
        if updated and action == "upload":
            from .services import upload_remediation
            upload_remediation.transition(connection, job_id, "retrying", timestamp, suspension=remediation_suspension)
            _dirty_job_protection(connection, job_id)
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
        return ArchiveCatalog(connection).record_job_progress(
            job_id=job_id,
            transferred_bytes=transferred_bytes,
            now=timestamp,
            claim_token=claim_token,
            claim_expires_at=_claim_expiry_at(timestamp),
        )


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
            "SELECT * FROM vaults WHERE lifecycle='active'"
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
        """SELECT j.vault_id, f.path FROM jobs j JOIN files f ON f.id = j.file_id
           WHERE j.id=%s AND j.action='upload'""", (job_id,),
    ).fetchone()
    if row:
        mark_path_dirty(connection, int(row["vault_id"]), row["path"])


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
    updated = ArchiveCatalog(connection).settle_stale_job(
        job_id=job_id,
        status=status,
        message=message,
        now=timestamp,
        progress="reset" if reset_progress else "complete" if complete_progress else None,
    )
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
            # The Job's own failure keeps the block as open Remediation work.
            ArchiveCatalog(connection).block_reconciliation(job["file_id"], reason=reason, at=now_iso())
    persist()
    # Notifications use Owner locks separately from attempt/receipt publication.
    # Retry the complete owned notification unit too, never the provider work.
    @retry_database_unit
    def notify() -> None:
        with owned_connection(db()) as connection:
            notification_service.set_problem(connection, key=f"upload_reconciliation:{job['file_id']}", event="upload_reconciliation", vault_id=job["vault_id"], active=True, email=False, params={"path": job["path"], "reason": reason, "remedy": provenance.REMEDIES[reason]})
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

    # Only the exact proof is adopted: the destination's bytes must hash to
    # the digest of the Job's Archive Version, and that version must still be
    # the file's verified Current. Nothing is downloaded or copied here.
    try:
        exact = _recovery_target(target)
    except RuntimeError:
        return transition(
            "failed",
            "Recovery destination preserved after restart; its Archive Version is no longer Current",
        )
    expected_digest = str(exact["version_sha256"]).lower()
    archive_version_id = str(exact["archive_version_id"])
    if exact["local_presence"] == "present":
        return transition(
            "failed",
            "Recovery destination preserved after restart; a newer Local Copy observation exists",
        )
    try:
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

    # The observation, digest and recovered_at are one compare-and-swap on the
    # generation read in this transaction, and the terminal Job is claimed in
    # the same savepoint: a live worker that renewed the lease, or a newer
    # observation, wins and nothing is committed for this Job.
    connection.execute("SAVEPOINT recover_adoption")
    published = ArchiveCatalog(connection).publish_recovered_local_copy(
        str(exact["file_id"]),
        generation=int(exact["local_generation"]),
        archive_version_id=archive_version_id,
        plaintext_sha256=expected_digest,
        size=recovered_stat.st_size,
        mtime_ns=recovered_stat.st_mtime_ns,
        recovered_at=timestamp,
    )
    if published is None:
        connection.execute("ROLLBACK TO SAVEPOINT recover_adoption")
        return transition(
            "failed",
            "Recovery destination preserved after restart; a newer Local Copy observation exists",
        )
    outcome = transition(
        "completed",
        "Recovered Local Copy verified and adopted after restart",
        complete=True,
    )
    if outcome != "completed":
        connection.execute("ROLLBACK TO SAVEPOINT recover_adoption")
        return outcome
    connection.execute("RELEASE SAVEPOINT recover_adoption")
    return outcome


def _reconcile_storage_class_job(
    connection: Any,
    job: dict[str, Any],
    *,
    timestamp: str,
) -> str:
    """An interrupted class change or Vault move simply resumes.

    Its copy attempt is independent of the Job (#595): the next claim
    reconciles a started copy from S3 by its journaled receipt or token and
    never sends it again.
    """
    return "requeued" if _reconcile_job_transition(
        connection, job_id=int(job["id"]), status="queued",
        message="Storage class change interrupted; its copy attempt will be reconciled",
        timestamp=timestamp,
    ) else "skipped"


def _reconcile_cloud_purge_group(
    connection: Any,
    lead: dict[str, Any],
    *,
    timestamp: str,
) -> dict[str, int]:
    """Settle an interrupted cloud-purge group by exact listing, then requeue it.

    A frozen grant whose exact version is absent was deleted by the crashed
    worker: it is recorded and its Job completes. A grant still present is
    revoked; its Job is requeued and the next claim revalidates the consent
    from the live catalog (an expired or changed consent is cancelled there).
    Nothing is deleted here.
    """
    group_id = lead.get("group_id")
    if not group_id:
        raise RuntimeError("Cloud purge is missing its group identity")
    group_rows = connection.execute(
        """
        SELECT j.*, EXISTS (
            SELECT 1 FROM job_leases l
            WHERE l.job_id = j.id AND l.expires_at > %s::timestamptz
        ) AS lease_live
        FROM jobs j
        WHERE j.vault_id=%s AND j.group_id=%s AND j.action='cloud-purge'
        ORDER BY j.requested_at ASC, j.id ASC
        FOR UPDATE OF j
        """,
        (timestamp, lead["vault_id"], group_id),
    ).fetchall()
    active_rows = [
        row for row in group_rows if row["status"] not in JOB_TERMINAL_STATUSES
    ]
    result = {"completed": 0, "requeued": 0, "failed": 0}
    # Reconciliation must never split a group around another worker's live
    # claim.  A partially published prior reconciliation may leave some rows
    # queued; those are harmless as long as every active member is unleased.
    if (
        not active_rows
        or any(row["lease_live"] for row in active_rows)
        or any(row["status"] not in {"cleaning", "queued"} for row in active_rows)
    ):
        return result
    jobs = [row for row in active_rows if row["status"] == "cleaning"]
    if not jobs:
        return result
    granted = connection.execute(
        """
        SELECT b.file_id, av.object_key, av.provider_version_id
        FROM cleanup_batches b
        JOIN cleanup_candidates c ON c.batch_id = b.id
        JOIN archive_versions av ON av.id = c.archive_version_id
        WHERE b.purge_group_id = %s AND c.deleted_at IS NULL
        """,
        (group_id,),
    ).fetchall()
    by_file = {str(row["file_id"]): row for row in granted}
    absent: list[dict[str, Any]] = []
    client = s3_client() if by_file else None
    for job in jobs:
        grant = by_file.get(str(job["file_id"]))
        if grant is None:
            continue
        versions, _markers = _object_version_entries(
            client, bucket=str(lead["s3_bucket"]), object_key=str(grant["object_key"]),
        )
        if str(grant["provider_version_id"]) not in versions:
            absent.append(job)
    absent_ids = {int(job["id"]) for job in absent}
    if absent:
        for job in absent:
            connection.execute("DELETE FROM job_leases WHERE job_id=%s", (int(job["id"]),))
        cloud_deletion_service.record_deleted(
            connection, jobs=absent, at=timestamp, actor="system",
        )
        result["completed"] += len(absent)
    remaining = [job for job in jobs if int(job["id"]) not in absent_ids]
    cloud_deletion_service.revoke_grants(
        connection, group_id=str(group_id),
        file_ids=[str(job["file_id"]) for job in remaining],
    )
    for job in remaining:
        if _reconcile_job_transition(
            connection,
            job_id=int(job["id"]),
            status="queued",
            message="Cloud deletion interrupted before its exact version was deleted; safely resumed",
            timestamp=timestamp,
        ):
            result["requeued"] += 1
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
                   MIN(l.expires_at) AS oldest_expiry,
                   MIN(j.updated_at) AS oldest_update
            FROM jobs j
            LEFT JOIN job_leases l ON l.job_id = j.id
            WHERE {_INTERRUPTED_JOB_SQL}
              AND (l.job_id IS NULL OR l.expires_at <= %s::timestamptz)
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
    """Count unresolved durable failures, without rewriting historical Jobs."""
    from .services.upload_failure_problems import CLOSED_UPLOAD_FAILURE_SQL
    now = datetime.now(timezone.utc)
    with db() as connection:
        row = connection.execute(
            f"""
            SELECT COUNT(*) AS total, MIN(original.updated_at) AS oldest_update
            FROM jobs original
            WHERE original.status='failed'
              AND NOT {CLOSED_UPLOAD_FAILURE_SQL}
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
            SELECT j.*, f.path, v.source_root, t.bucket AS s3_bucket
            FROM jobs j
            JOIN files f ON f.id = j.file_id
            JOIN vaults v ON v.id=j.vault_id
            JOIN vault_targets t ON t.id = v.target_id
            WHERE {_INTERRUPTED_JOB_SQL}
              AND NOT EXISTS (
                  SELECT 1 FROM job_leases l
                  WHERE l.job_id = j.id AND l.expires_at > %s::timestamptz
              )
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
                        SELECT j.id FROM jobs j
                        WHERE j.vault_id=%s AND j.group_id=%s AND j.action='cloud-purge'
                          AND j.status='cleaning'
                          AND NOT EXISTS (
                              SELECT 1 FROM job_leases l
                              WHERE l.job_id = j.id AND l.expires_at > %s::timestamptz
                          )
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
                                    job["file_id"], observed_at=timestamp
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
    floor_reported: bool = False,
) -> Callable[[int, int | None], None]:
    total_bytes = int(job.get("total_bytes") or 0)
    # Upload verification is a second read phase of the same Job. Its stream
    # counters must never reset the already-reported upload progress to zero.
    floor = max(0, int(minimum_bytes))
    total_bytes = max(total_bytes, floor)
    interval_ms = int(_runtime_settings().job_progress_min_interval_ms or 500)
    min_interval = max(0.05, interval_ms / 1000.0)
    # ``floor_reported``: the caller already recorded ``floor`` durably, so
    # reporting it again would be one more transaction with no new progress.
    state = {"last_at": 0.0, "last_value": floor if floor_reported else floor - 1}
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
    ensure_job_active(job["id"], "Upload source claim lost")
    with db() as connection:
        vault = connection.execute("SELECT * FROM vaults WHERE id=%s", (job["vault_id"],)).fetchone()
    _upload_source_root_check(job, vault)


def _upload_source_root_check(job: dict[str, Any], vault: dict[str, Any] | None) -> None:
    """Filesystem half of the Source Volume/root check, against a fresh Vault row.

    ``vault`` must come from the DB unit just committed (the claim-fenced
    upload unit's locked row): this never reads a cached row.
    """
    from .services.upload_provenance import UploadBlocked
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
    # The upload Module hands over the Vault row its send-started unit locked
    # and committed just before: the root is still checked on the filesystem
    # here, right before the PUT, without reading the same row again.
    vault = job.get("_upload_source_vault")
    if vault is None:
        _require_upload_source_identity(job)
    else:
        _upload_source_root_check(job, vault)
    check_active = job.get("_upload_check_active") or (lambda: ensure_job_active(job["id"], "Upload stopped"))
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
        check_active=check_active,
        on_progress=reporter.add,
        max_bandwidth=int(limit) * 1024 if limit else None,
        snapshot=job.get("_upload_snapshot"),
        metadata=job.get("_upload_metadata"),
        on_acknowledged=job.get("_on_upload_acknowledged"),
        on_multipart=job.get("_on_upload_multipart"),
    )
    if not job.get("_upload_progress_in_catalog"):
        # Otherwise the caller's next fenced unit records the final bytes.
        reporter.flush()
    return response


def _stream_plaintext_for_verification(
    job: dict[str, Any], *, target: dict[str, Any], source_size: int,
    check_active: Callable[[], None] | None = None,
    floor_reported: bool = False,
) -> tuple[str, int]:
    """Hash only the exact VersionId returned by the upload, never current key.

    ``check_active`` replaces the per-chunk durable checkpoint with the
    caller's (the upload Module's amortized one); a read-back is no effect.
    """
    version_id = target.get("provider_version_id")
    key = target.get("object_key")
    if not version_id or not key:
        raise RuntimeError("An exact Archive Version is required for verification")
    if check_active is None:
        check_active = lambda: ensure_job_active(job["id"], "Upload stopped")
    check_active()
    response = s3_client().get_object(Bucket=job["s3_bucket"], Key=key, VersionId=version_id)
    body = response["Body"]
    digest = hashlib.sha256()
    size = 0
    progress = job_progress_callback(
        job, minimum_bytes=source_size, floor_reported=floor_reported)
    try:
        if str(response.get("VersionId") or "") != str(version_id):
            raise RuntimeError("S3 returned a different verification VersionId")
        while chunk := body.read(1024 * 1024):
            check_active()
            digest.update(chunk)
            size += len(chunk)
            progress(size)
    finally:
        body.close()
    check_active()
    return digest.hexdigest(), size


def _record_verification_failure(reason: str) -> None:
    """Record only bounded, non-sensitive verification failure categories."""
    try:
        metrics_service.inc("verification_failures_total", reason=reason)
    except Exception:
        # Metrics are observability only and must never change Job durability.
        pass


UPLOAD_READY = "ready"
UPLOAD_SOURCE_REMOVED = "removed"
UPLOAD_NOT_READY = "wait"


class UploadSourceRemoved(RuntimeError):
    """The Local Copy of an upload no longer exists, so there is nothing to send.

    Raised only where no cloud write could have started for the Job. It closes
    the Job as a removed file instead of a failure (issue #499, ADR-0026).
    """


def _upload_owes_cloud_work(connection: Any, target: dict[str, Any] | None, job_id: int) -> bool:
    """A linked version, a replacement in progress or a started send must finish."""
    from .services import upload_provenance as provenance
    if target and (target.get("archive_version_id") or target.get("replacing_version_id")):
        return True
    attempt = provenance.load_for_job(connection, job_id)
    if attempt is not None and attempt.send_started:
        return True
    return bool(target) and provenance.outstanding(connection, str(target["file_id"])) is not None


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


@retry_database_unit
def _automatic_upload_state(job: dict[str, Any]) -> str:
    """Recheck suspension and stability when a queued automatic upload starts.

    Queue time is not authority to upload later edits. Linked versions must
    finish verification/reconciliation even if automation was subsequently paused.

    Returns ``UPLOAD_READY``, ``UPLOAD_SOURCE_REMOVED`` (the enrolled root is
    healthy and the file is gone: the Job can be closed, issues #493/#499) or
    ``UPLOAD_NOT_READY`` (paused, unstable, unreadable or changed: keep waiting).
    """
    with owned_connection(db()) as connection:
        local = ArchiveCatalog(connection).get_job_target(job["id"])
        if _upload_owes_cloud_work(connection, local, job["id"]):
            return UPLOAD_READY
        policy = get_policy(connection, int(job["vault_id"]))
        vault = connection.execute("SELECT * FROM vaults WHERE id=%s", (job["vault_id"],)).fetchone()
        if (not vault or vault["lifecycle"] != "active" or vault["paused"]
                or not policy.auto_upload):
            return UPLOAD_NOT_READY
        if vault["source_root"] != job["source_root"]:
            return UPLOAD_NOT_READY
        if not source_layout.vault_local_access(vault["source_root"]).local_operations_allowed:
            return UPLOAD_NOT_READY
        if not auto_upload_path_is_included(job["path"], policy) or not local:
            return UPLOAD_NOT_READY
        try:
            # A disappeared or unreadable root is not a removed file.
            _validate_enrolled_scan_root(vault)
        except (OSError, ValueError, RuntimeError):
            return UPLOAD_NOT_READY
        try:
            # Files can disappear, lose access or be replaced after admission.
            # Suspend only this candidate; no DB work belongs in this catch.
            source = safe_local_path(job["source_root"], job["path"])
            stat_result = source.stat()
        except (FileNotFoundError, NotADirectoryError):
            # An unmount racing the stat also looks like ENOENT: only a root that
            # is still the enrolled one afterwards proves the file itself is gone.
            if not _enrolled_root_still_healthy(vault):
                return UPLOAD_NOT_READY
            return UPLOAD_SOURCE_REMOVED
        except (OSError, ValueError, RuntimeError):
            return UPLOAD_NOT_READY
        if not stat.S_ISREG(stat_result.st_mode):
            return UPLOAD_NOT_READY
        if (stat_result.st_size, stat_result.st_mtime_ns) != (
            local["local_size"], local["local_mtime_ns"]
        ):
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=int(job["vault_id"]), path=job["path"], file_type="regular",
                size=stat_result.st_size, mtime_ns=stat_result.st_mtime_ns, observed_at=now_iso(),
            )
            return UPLOAD_NOT_READY
        stable = file_is_stable(
            mtime_ns=stat_result.st_mtime_ns, now=datetime.fromisoformat(now_iso()),
            stability_seconds=policy.stability_seconds,
            content_observed_at=_iso(local["content_observed_at"]),
        )
        return UPLOAD_READY if stable else UPLOAD_NOT_READY


def _automatic_upload_ready(job: dict[str, Any]) -> bool:
    """Boolean view of ``_automatic_upload_state`` for callers that only gate."""
    return _automatic_upload_state(job) == UPLOAD_READY


def _enrolled_root_still_healthy(vault: Mapping[str, Any]) -> bool:
    """True when the enrolled Source Volume is still reachable and unchanged."""
    try:
        if not source_layout.vault_local_access(vault["source_root"]).local_operations_allowed:
            return False
        _validate_enrolled_scan_root(vault)
    except (OSError, ValueError, RuntimeError):
        return False
    return True


@retry_database_unit
def _upload_source_confirmed_removed(job: dict[str, Any]) -> bool:
    """True only when no cloud write can be owed and the file is provably gone.

    The enrolled root must be healthy (an unmounted or replaced root is not a
    removed file), the path must raise ENOENT/ENOTDIR, and the Job must owe no
    cloud work: no linked version, no replacement in progress and no started
    send, which may already have reached S3 and stays with reconciliation
    (ADR-0025).
    """
    with owned_connection(db()) as connection:
        target = ArchiveCatalog(connection).get_job_target(job["id"])
        if _upload_owes_cloud_work(connection, target, job["id"]):
            return False
        vault = connection.execute("SELECT * FROM vaults WHERE id=%s", (job["vault_id"],)).fetchone()
    if not vault or vault["source_root"] != job.get("source_root", vault["source_root"]):
        return False
    if not source_layout.vault_local_access(vault["source_root"]).local_operations_allowed:
        return False
    try:
        _validate_enrolled_scan_root(vault)
        source = safe_local_path(vault["source_root"], job["path"])
        source.stat()
    except (FileNotFoundError, NotADirectoryError):
        return _enrolled_root_still_healthy(vault)
    except (OSError, ValueError, RuntimeError):
        return False
    return False


def _close_removed_upload(job: dict[str, Any]) -> bool:
    """Close an upload whose file is gone: a stopped outcome, not a failure.

    The Job ends ``cancelled`` with its own message, so no ``job_failed``
    notification is produced and a linked remediation episode ends as
    ``file_removed``. A file that reappears with different content is admitted
    again by the watcher (its content observation postdates this Job).
    """
    return set_job(
        job["id"], "cancelled", message_key="job.upload_file_removed",
        remediation_reason="file_removed",
    )


def _upload_key(job: dict[str, Any]) -> str:
    """The Vault namespace key of the Job's path: ``vaults/<uuid>/<path>``."""
    return expected_cloud_key(job["path"], f"vaults/{job['vault_uuid']}")


@retry_database_unit
def _upload_catalog_unit(
    job: dict[str, Any], mutation: Callable[[ArchiveCatalog], Any], *,
    shared: bool = False,
) -> Any:
    """Replay DB work only, never provider I/O.

    SHARE is an audited opt-in, not a property of all per-file writes. The full
    callback (including nested target checks) must never upgrade the Vault lock:
    no identity creation, aggregate/event/rebuild writers or set_job transitions.
    Materialization and publication deliberately stay exclusive.
    """
    from .services import upload_provenance as provenance
    from .services.upload_pipeline import catalog_capacity
    # Acquired before connection checkout/Vault locks, released after COMMIT
    # (or rollback), and outside the retry decorator's sleep between attempts.
    with catalog_capacity(job), _defer_job_counters():
        with owned_connection(db()) as connection, unit_dirty_marks(connection):
            # Same Vault -> Job/file order as admission/cleanup, in either mode.
            # The private capability exists only inside this DB unit and still
            # compares every pin; nested prepare/send/publication checks need
            # not re-query the same fenced target. It is revoked before commit.
            with provenance._validated_target(
                connection, job, key=_upload_key(job), provider="db-fence", shared=shared,
            ):
                ensure_job_claim_owned_in_transaction(connection, job, "Upload claim was lost")
                return mutation(ArchiveCatalog(connection))


def cleanup_superseded_upload(
    job: dict[str, Any], *, keeper_id: str | None, client: Any = None,
) -> None:
    """Exact cleanup, by VersionId, of what the verified keeper superseded (#591).

    Runs after verification and publication, before completion, with the
    published ``keeper_id``. With ``keeper_id=None`` (a Job about to send or to
    match the Current it did not produce) it first finishes an open cleanup of
    the file's Current, so a file never holds more than Current, one
    replacement in progress and versions with open cleanup.
    """
    import sys
    from .services import snapshot_cleanup
    snapshot_cleanup.cleanup(sys.modules[__name__], job, keeper_id, client)


def require_sole_keeper(job: dict[str, Any], keeper: dict[str, Any], *, client: Any,
                        bucket: str) -> None:
    """Postcondition of a published keeper that superseded nothing (#622)."""
    import sys
    from .services import snapshot_cleanup
    snapshot_cleanup.require_sole_keeper(sys.modules[__name__], job, keeper,
                                         client=client, bucket=bucket)


def _upload_protection_paused(job: dict[str, Any]) -> bool:
    from .services.operation_policies import get_policy, auto_upload_path_is_included
    with db() as connection:
        policy = get_policy(connection, int(job["vault_id"]))
        vault = connection.execute("SELECT paused FROM vaults WHERE id=%s", (job["vault_id"],)).fetchone()
    return (not vault or vault["paused"] or not policy.auto_upload
            or not auto_upload_path_is_included(job["path"], policy))


def _clear_upload_reconciliation_problem(job: dict[str, Any]) -> None:
    @retry_database_unit
    def clear_problem() -> None:
        with owned_connection(db()) as connection:
            notification_service.set_problem(connection, key=f"upload_reconciliation:{job['file_id']}", event="upload_reconciliation", vault_id=job["vault_id"], active=False, email=False)
    clear_problem()


def _close_stale_upload_snapshot(job: dict[str, Any]) -> None:
    """After a source change, let the next attempt fingerprint the current content.

    Snapshot A can never match a Local Copy that changed, so retrying against it
    only rereads the file until ``job.source_changed_stalled`` (#538). An unsent
    attempt is closed (never re-pinned) and the Job forgets A. A started send is
    left alone: the next attempt reconciles it first. Best effort: on any
    failure the retry is scheduled as before and the next source change tries
    again.
    """
    from .services import upload_provenance as provenance
    try:
        _upload_catalog_unit(job, lambda catalog: provenance.close_unsent_for_job(
            catalog.connection, job, now_iso()))
    except Exception:
        logging.getLogger(__name__).warning("Stale Upload Snapshot was not closed; the retry reuses it")


def process_upload(job: dict[str, Any]) -> None:
    """Compatibility hook: the UploadExecution Module owns the per-file protocol."""
    import sys
    from .services.upload_execution import UploadExecution
    UploadExecution(sys.modules[__name__]).run(job)


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


def _copy_worker() -> Any:
    """This module as the worker seam of the copy engine (patched in tests)."""
    return sys.modules[__name__]


_RENAME_CHAIN_LIMIT = 3


def _refresh_rename_path(job: dict[str, Any]) -> bool:
    """Follow a rename chain: the Job copies to the newest path, never to a stale one."""
    with db() as connection:
        row = connection.execute(
            "SELECT path FROM files WHERE id=%s AND vault_id=%s",
            (job["file_id"], job["vault_id"])).fetchone()
    if row is None or row["path"] == job["path"]:
        return False
    job["path"] = row["path"]
    return True


def _copy_blocked(job: dict[str, Any], exc: BaseException) -> None:
    from .services import upload_provenance as provenance
    if isinstance(exc, provenance.UploadBlocked) and exc.reason not in provenance.WAIT_REASONS:
        # Kept copies and attempts stay for reconciliation; the file says why.
        _record_upload_reconciliation_block(job, exc.reason)


def process_rename(job: dict[str, Any]) -> None:
    """Copy the exact verified Current to the key of the file's new path (#595).

    The copy names its source by key and VersionId, is attributed by its own
    token, becomes Current only once verified, and the previous key's version
    is removed only afterwards by exact cleanup. The Local Copy is matched to
    it only through a fresh digest of the real local bytes.
    """
    from .services import copy_provenance
    from .services.upload_provenance import UploadBlocked
    ensure_job_active(job["id"], "Rename stopped")
    validate_cloud_vault(job)
    try:
        for _link in range(_RENAME_CHAIN_LIMIT):
            try:
                keeper = copy_provenance.run(_copy_worker(), job, purpose="rename")
                break
            except UploadBlocked as exc:
                if exc.reason != "identity_changed" or not _refresh_rename_path(job):
                    raise
        else:
            raise UploadBlocked("identity_changed")
    except copy_provenance.CopyNeedsRestore as exc:
        # A cold Current is never warmed implicitly by a rename.
        raise RuntimeError(
            f"The current Archive Version is in {exc.storage_class}: restore it "
            "explicitly, then retry the rename") from exc
    except Exception as exc:
        _copy_blocked(job, exc)
        raise
    _clear_upload_reconciliation_problem(job)
    set_job(job["id"], "completed",
            message_key="job.rename_verified" if keeper else "job.rename_key_matches")


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
                actor="system" if job.get("origin") == "automatic" else "owner",
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
        or target["archive_version_id"] != target["current_version_id"]
        or target["replacing_version_id"] is not None
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
    file_id = str(target["vault_file_id"])
    with db() as connection:
        if "root_identity" in vault:
            _current_verified_scan_row(connection, vault)
        catalog = ArchiveCatalog(connection)
        observed_at = now_iso()
        if replacement:
            # Content that replaced the claimed original: Current may be the
            # only copy now, so it is held unless it proves identical.
            catalog.observe_replaced_local_copy(
                file_id, file_type=file_type, size=info.st_size,
                mtime_ns=info.st_mtime_ns, observed_at=observed_at,
            )
        else:
            # New bytes (a different digest) restart the content window and
            # never inherit the released copy's fingerprint or match.
            catalog.observe_local_copy(
                vault_id=job["vault_id"], path=job["path"], file_type=file_type,
                size=info.st_size, mtime_ns=info.st_mtime_ns, observed_at=observed_at,
                invalidate_content=digest != target["local_sha256"],
            )
        if digest:
            observed = connection.execute(
                "SELECT generation FROM files WHERE id=%s", (file_id,),
            ).fetchone()
            catalog.publish_local_fingerprint(
                file_id, generation=int(observed["generation"]), plaintext_sha256=digest,
                size=info.st_size, mtime_ns=info.st_mtime_ns, match_current=True,
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


def _request_copy_restore(job: dict[str, Any], exc: Any) -> None:
    """A manual class change of a cold Current restores it once, then waits."""
    from .services import upload_provenance as provenance
    with db() as connection:
        current = connection.execute(
            """SELECT av.id, av.object_key, av.provider_version_id, av.size
               FROM files f JOIN archive_versions av ON av.id = f.current_version_id
               WHERE f.id=%s""", (job["file_id"],)).fetchone()
    if current is None:
        raise provenance.UploadBlocked("keeper_missing")
    state, expiry = exc.restore_state, exc.expiry
    if job["status"] != "restoring" and state == "not_requested":
        estimate = estimate_restore(
            size_bytes=int(current["size"] or 0), storage_class=exc.storage_class,
            tier=str(job.get("restore_tier") or _runtime_settings().restore_tier or "Bulk"),
            days=int(job.get("restore_days") or _runtime_settings().restore_days or 3))
        ensure_job_active(job["id"], "Storage class change stopped")
        s3_client().restore_object(
            Bucket=job["s3_bucket"], Key=current["object_key"],
            VersionId=current["provider_version_id"],
            RestoreRequest={"Days": estimate.days,
                            "GlacierJobParameters": {"Tier": estimate.tier}})
        state, expiry = "restoring", None
    with db() as connection:
        ensure_job_claim_owned_in_transaction(connection, job, "Storage class claim was lost")
        ArchiveCatalog(connection).update_restore_state(
            str(current["id"]), state=state, expiry=expiry, checked_at=now_iso(),
            storage_class=exc.storage_class)
    set_job(job["id"], "restoring", message_key="job.storage_class_restoring",
            message_params={"storage_class": exc.storage_class})


def process_storage_class(job: dict[str, Any]) -> None:
    """Copy the exact verified Current in place to another class (#595).

    A manual change may warm a cold Current through one restore of the exact
    VersionId; a Vault move (``colder_only``) never warms and never restores.
    The superseded version is removed by exact cleanup once the copy is Current.
    """
    from .services import copy_provenance
    ensure_job_active(job["id"], "Storage class change stopped")
    validate_cloud_vault(job)
    with db() as connection:
        change = connection.execute(
            "SELECT target_storage_class, colder_only FROM job_storage_class_changes WHERE job_id=%s",
            (job["id"],)).fetchone()
        if change is not None:
            restore = connection.execute(
                "SELECT tier, days FROM job_restores WHERE job_id=%s", (job["id"],)).fetchone()
            if restore is not None:
                job.setdefault("restore_tier", restore["tier"])
                job.setdefault("restore_days", restore["days"])
    if change is None or not change["target_storage_class"]:
        raise RuntimeError("Target storage class is missing")
    target_class = str(change["target_storage_class"]).upper()
    colder_only = bool(change["colder_only"])
    try:
        keeper = copy_provenance.run(
            _copy_worker(), job, purpose="storage-class", storage_class=target_class,
            check_source=partial(vault_moves.check_source, target_class) if colder_only else None)
    except copy_provenance.CopyNeedsRestore as exc:
        if colder_only:
            raise vault_moves.MoveBlocked(
                f"Vault move never restores: the Archive Version is in {exc.storage_class}") from exc
        _request_copy_restore(job, exc)
        return
    except Exception as exc:
        _copy_blocked(job, exc)
        raise
    _clear_upload_reconciliation_problem(job)
    set_job(
        job["id"], "completed",
        message_key="job.storage_class_completed" if keeper else "job.storage_class_skipped_same",
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


def _recovery_target(target: dict[str, Any] | None) -> dict[str, Any]:
    """The exact Archive Version a recovery may read: the file's verified Current."""
    if (
        not target
        or not target["archive_version_id"]
        or str(target["archive_version_id"]) != str(target.get("current_version_id") or "")
        or target["integrity"] != "verified"
        or target["availability"] != "available"
        or not target["provider_version_id"]
        or not target["object_key"]
        or not target["version_sha256"]
    ):
        raise RuntimeError("The file is not available in the cloud")
    return target


def _publish_without_replacing(temporary: Path, destination: Path) -> None:
    """Move the verified plaintext to the final path, never over an entry there.

    A hard link fails atomically when anything (a file, a directory, a
    symlink) appeared at the destination since the last check. Only where
    links are unsupported does the publication fall back to a checked rename.
    """
    try:
        os.link(temporary, destination, follow_symlinks=False)
    except FileExistsError as exc:
        raise RuntimeError("A local copy already exists at the recovery destination") from exc
    except OSError:
        if os.path.lexists(destination):
            raise RuntimeError("A local copy already exists at the recovery destination")
        temporary.replace(destination)
        return
    temporary.unlink()


def process_recover(job: dict[str, Any]) -> None:
    ensure_job_active(job["id"], "Recovery stopped")
    validate_cloud_vault(job)
    with db() as connection:
        target = _recovery_target(ArchiveCatalog(connection).get_job_target(job["id"]))
    if target["local_presence"] == "present":
        raise RuntimeError("A local copy is already present")

    from .services.upload_snapshot import recovery_mtime_ns

    expected_digest = str(target["version_sha256"]).lower()
    object_key = str(target["object_key"])
    provider_version_id = str(target["provider_version_id"])
    archive_version_id = str(target["archive_version_id"])
    storage_class = target.get("storage_class")
    size_bytes = int(target.get("cloud_size") or job.get("total_bytes") or 0)

    # Restore Approval is decided once for the whole request when it is
    # queued (#533). The worker never re-evaluates the thresholds, so a restore
    # already requested from AWS cannot be held again after settings change
    # (#534). The provider's restore header of the exact VersionId decides
    # whether RestoreObject is still needed: never requested twice (#594).
    head = s3_client().head_object(
        Bucket=job["s3_bucket"],
        Key=object_key,
        VersionId=provider_version_id,
    )
    if str(head.get("VersionId") or provider_version_id) != provider_version_id or head.get("DeleteMarker"):
        raise RuntimeError("The provider returned a different Archive Version")
    original_mtime_ns = recovery_mtime_ns(target.get("upload_snapshot_json"), head.get("Metadata") or {})
    head_class = head.get("StorageClass") or storage_class
    restore_state, restore_expiry = restore_header_state(head.get("Restore"))
    needs_restore = storage_class_requires_restore(head_class)
    if needs_restore and restore_state != "available":
        tier = target.get("restore_tier") or _runtime_settings().restore_tier or "Bulk"
        days = int(target.get("restore_days") or _runtime_settings().restore_days or 3)
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
                state=restore_state if restore_state != "not_requested" else None,
                expiry=restore_expiry,
                checked_at=checked_at,
                storage_class=head_class,
            )
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
    set_job(job["id"], "downloading", "Downloading")
    try:
        download_exact_version_plaintext(
            job,
            object_key=object_key,
            provider_version_id=provider_version_id,
            temporary=temporary,
        )
        ensure_job_active(job["id"], "Recovery stopped")
        set_job(job["id"], "verifying", "Verifying recovered plaintext")
        if original_mtime_ns is not None:
            # Apply before hashing so the publication guard and catalog observe
            # the final fingerprint, while recovered_at owns local retention.
            os.utime(temporary, ns=(temporary.stat().st_atime_ns, original_mtime_ns), follow_symlinks=False)
        recovered_digest, recovered_stat = hash_stable_regular_file(temporary)
        if recovered_digest != expected_digest:
            raise RuntimeError(
                "Recovered plaintext digest does not match the Archive Version"
            )
        with scan_lock_for_vault(int(job["vault_id"])):
            # Transfers/hash stay outside the filesystem critical section. The
            # observation token is read before the final path appears, and the
            # observation, digest and recovered_at are published with one
            # compare-and-swap on it: a newer observation is never overwritten.
            ensure_job_active(job["id"], "Recovery claim was lost")
            if not _local_stat_unchanged(temporary, recovered_stat):
                raise RuntimeError("Recovered plaintext changed before publication")
            with db() as connection:
                ensure_job_claim_owned_in_transaction(connection, job, "Recovery claim was lost")
                live = _recovery_target(ArchiveCatalog(connection).get_job_target(job["id"]))
            if (str(live["archive_version_id"]), live["version_sha256"]) != (
                archive_version_id, expected_digest,
            ) or live["local_presence"] == "present":
                raise RuntimeError("The recovery target changed before publication")
            generation = int(live["local_generation"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            _publish_without_replacing(temporary, destination)
            published_stat = destination.lstat()
            recovered_at = now_iso()
            with db() as connection:
                ensure_job_claim_owned_in_transaction(connection, job, "Recovery claim was lost")
                catalog = ArchiveCatalog(connection)
                published = catalog.publish_recovered_local_copy(
                    str(target["file_id"]),
                    generation=generation,
                    archive_version_id=archive_version_id,
                    plaintext_sha256=expected_digest,
                    size=recovered_stat.st_size,
                    mtime_ns=recovered_stat.st_mtime_ns,
                    recovered_at=recovered_at,
                )
                if published is None:
                    raise RuntimeError(
                        "Recovered Local Copy was preserved; a newer observation of the path won"
                    )
                if needs_restore or target.get("restore_state"):
                    catalog.update_restore_state(
                        archive_version_id,
                        state="available" if needs_restore else None,
                        expiry=restore_expiry if needs_restore else None,
                        checked_at=recovered_at,
                        storage_class=head_class,
                    )
            local_observation.remember(job["vault_id"], job["path"], published_stat, expected_digest)
    finally:
        temporary.unlink(missing_ok=True)
    set_job_progress(job["id"], int(job.get("total_bytes") or recovered_stat.st_size))
    set_job(job["id"], "completed", f"Recovered to {destination}")


class _PurgeUnsettled(JobLeaseLost):
    """A DELETE may have run but its outcome is not recorded: restart settles it."""


def process_cloud_purge(job: dict[str, Any]) -> None:
    """Delete a scheduler-claimed Delayed Cloud Deletion group exactly once.

    Per chunk: revalidate the consent and freeze the grants (committed before
    any DELETE); then, in one transaction holding the Vault lock, the renewed
    group claim and a fresh local-absence proof, send one DeleteObjects by
    exact VersionId; then record each result in a separate DB unit. A DELETE
    is never replayed by a DB retry; an unrecorded outcome stays held for the
    restart reconciliation, which settles it by exact listing.
    """
    from .services import upload_provenance as _provenance

    ensure_job_active(job["id"], "Cloud purge stopped")
    claim_token = job.get("claim_token")
    if not claim_token:
        # The scheduler is the only supported entry point for destructive work.
        return
    group_id = str(job.get("group_id") or "")
    if not group_id:
        raise RuntimeError("Permanent purge is missing its group identity")
    vault_id = int(job["vault_id"])
    validate_cloud_vault(job)
    client = s3_client()
    provider = _provenance.provider_identity(client)
    with db() as connection:
        if not cloud_deletion_service.validate_execution(
            connection, vault_id=vault_id, group_id=group_id, at=now_iso(),
        ):
            return
        purge_jobs, items = ArchiveCatalog(connection).load_claimed_purge_group(
            lead_job_id=int(job["id"]), claim_token=str(claim_token), now=now_iso(),
        )
    if not purge_jobs:
        return
    # The lead carries the worker's claim checkpoints: settle it in the last
    # batch, so an ended lead never fences the members still to be deleted.
    items = sorted(items, key=lambda item: int(item["id"]) == int(job["id"]))
    expected_job_ids = [int(purge_job["id"]) for purge_job in purge_jobs]
    actor = "owner"

    def renew(connection: Any) -> None:
        stamp = now_iso()
        if _claim_is_lost(int(job["id"])) or not ArchiveCatalog(connection).renew_purge_group_claim(
            vault_id=vault_id, group_id=group_id, claim_token=str(claim_token),
            expected_job_ids=expected_job_ids, now=stamp,
            claim_expires_at=_claim_expiry_at(stamp),
        ):
            _mark_claim_lost(int(job["id"]))
            raise JobLeaseLost("Cloud purge claim was lost")

    for offset in range(0, len(items), CLOUD_DELETE_BATCH_SIZE):
        batch = items[offset : offset + CLOUD_DELETE_BATCH_SIZE]
        ensure_job_active(job["id"], "Cloud purge stopped")
        with db() as connection:
            if not cloud_deletion_service.validate_execution(
                connection, vault_id=vault_id, group_id=group_id, at=now_iso(),
            ):
                return
            renew(connection)
            vault = cloud_deletion_service.lock_vault(connection, vault_id)
            cloud_deletion_service.freeze_grants(
                connection, vault=vault, group_id=group_id, jobs=batch, provider=provider,
            )
        response: dict[str, Any] | None = None
        ambiguous: Exception | None = None
        sent = False
        try:
            with owned_connection(db()) as connection:
                if not cloud_deletion_service.validate_execution(
                    connection, vault_id=vault_id, group_id=group_id, at=now_iso(),
                ):
                    return
                renew(connection)
                if job_cancelled(int(job["id"])):
                    raise OperationCancelled("Cloud purge stopped")
                sent = True
                try:
                    response = client.delete_objects(
                        Bucket=job["s3_bucket"],
                        Delete={
                            "Objects": [
                                {"Key": item["object_key"], "VersionId": item["provider_version_id"]}
                                for item in batch
                            ],
                            "Quiet": True,
                        },
                    )
                except Exception as exc:  # the outcome is unknown: settle by listing
                    ambiguous = exc
        except psycopg.Error:
            if not sent:
                raise
        if _claim_is_lost(int(job["id"])):
            _mark_claim_lost(int(job["id"]))
            raise _PurgeUnsettled("Cloud purge claim was lost")
        deleted: list[dict[str, Any]] = []
        failed: list[tuple[dict[str, Any], str]] = []
        try:
            if ambiguous is not None or response is None:
                for item in batch:
                    versions, _markers = _object_version_entries(
                        client, bucket=str(job["s3_bucket"]), object_key=str(item["object_key"]),
                    )
                    if str(item["provider_version_id"]) in versions:
                        failed.append((item, str(ambiguous or "S3 did not answer")))
                    else:
                        deleted.append(item)
            else:
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
                    message = errors.get((str(item["object_key"]), str(item["provider_version_id"])))
                    if message is None:
                        deleted.append(item)
                    else:
                        failed.append((item, message))
            stamp = now_iso()
            with owned_connection(db()) as connection:
                renew(connection)
                cloud_deletion_service.record_deleted(
                    connection, jobs=deleted, at=stamp, actor=actor,
                )
                cloud_deletion_service.record_failed(
                    connection, failures=failed, at=stamp, actor=actor,
                )
            # Ended members dropped their lease; the claim covers the rest.
            ended = {int(item["id"]) for item in deleted} | {int(item["id"]) for item, _ in failed}
            expected_job_ids[:] = [i for i in expected_job_ids if i not in ended]
        except JobLeaseLost:
            raise _PurgeUnsettled("Cloud purge claim was lost")
        except Exception as exc:
            # Never fail a Job whose exact DELETE may have run without its
            # record: keep it held; the restart reconciliation lists and settles.
            raise _PurgeUnsettled(f"Cloud deletion outcome not recorded: {exc}") from exc


@retry_database_unit
def _load_claimed_job_status(job: dict[str, Any]) -> dict[str, Any] | None:
    claim_token = job.get("claim_token")
    if claim_token:
        with owned_connection(db()) as connection:
            renewed = ArchiveCatalog(connection).renew_job_claim(
                job_id=int(job["id"]), claim_token=str(claim_token),
                now=now_iso(), claim_expires_at=_claim_expiry_at(),
            )
        if renewed is not None and _claim_token_for(int(job["id"])) == str(claim_token):
            # This is the run's first extension: the next checkpoints read the
            # lease and extend it again only after the usual interval.
            _worker_claim.renewed = ((int(job["id"]), str(claim_token)), time.monotonic())
        return renewed
    with owned_connection(db()) as connection:
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
        ArchiveCatalog(connection).release_purge_group_claim(
            vault_id=int(job["vault_id"]), group_id=str(job["group_id"]),
            claim_token=str(job["claim_token"]), now=now_iso(),
        )


def _is_source_removal_error(exc: BaseException) -> bool:
    """ENOENT-class errors and ``UploadSourceRemoved``, anywhere in the chain."""
    for error in exception_chain(exc):
        if isinstance(error, (UploadSourceRemoved, FileNotFoundError, NotADirectoryError)):
            return True
    return False


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
    with db() as connection:
        opened = _source_volume_problem_open(connection, job)
    if opened:
        _resolve_source_volume_problem(job)


def _source_volume_problem_open(connection: Any, job: dict[str, Any]) -> bool:
    key = _source_volume_problem_key(int(job["vault_id"]))
    return connection.execute(
        "SELECT 1 FROM notifications WHERE dedupe_key=%s AND problem_status='open' LIMIT 1", (key,),
    ).fetchone() is not None


def _resolve_source_volume_problem(job: dict[str, Any]) -> None:
    key = _source_volume_problem_key(int(job["vault_id"]))

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
            "rename": {"queued", "retrying"},
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
            last_check = _parse_iso_datetime(job["updated_at"])
            age = (
                (datetime.now(timezone.utc) - last_check).total_seconds()
                if last_check is not None else float("inf")
            )
            if age < runtime.restore_poll_interval:
                return False
        if job["action"] == "upload" and job["status"] in {"queued", "retrying"}:
            process_upload(job)
        elif job["action"] == "rename" and job["status"] in {"queued", "retrying"}:
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
    except UploadJournalHeld:
        # A dependency hold is not a failed transfer. Give back the claim and
        # retry later without exhausting this (possibly unrelated) file's budget.
        schedule_upload_retry(
            job["id"], message_key="job.upload_journal_waiting",
            retry_count=int(job.get("retry_count") or 0), delay_seconds=10,
            remediation_suspension="upload_journal_unavailable",
        )
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
        if (job["action"] in _provenance.TARGET_ACTIONS and isinstance(exc, _provenance.UploadBlocked)
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
        if job["action"] == "upload" and _is_source_removal_error(exc):
            # A file that vanished before any cloud write is not a failed upload
            # (#499). Anything that could have reached S3 is never closed here.
            try:
                removed = _upload_source_confirmed_removed(job)
            except Exception:
                removed = False
            if removed and _close_removed_upload(job):
                return True
        message = str(exc)
        failure_kind = (
            classify_upload_failure(exc) if job["action"] == "upload" else ""
        )
        if job["action"] == "upload" and failure_kind == "source_changed":
            next_attempt = int(job.get("retry_count") or 0) + 1
            if next_attempt <= UPLOAD_RETRY_MAX_ATTEMPTS:
                # A started send keeps its snapshot: the retry reconciles it first
                # (ADR-0025/0029). Only an unsent intent is closed here (#538).
                _close_stale_upload_snapshot(job)
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
        if (job["action"] in {"rename", "storage-class"}
                and not isinstance(exc, (_provenance.UploadBlocked, vault_moves.MoveBlocked))
                and classify_upload_failure(exc) in {"transient", "database_connection"}):
            # The copy engine reconciles a started copy from S3 on the retry;
            # it never sends it again.
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


def _operation_completed(_future: Future[Any]) -> None:
    # A callback may run synchronously inside add_done_callback while the
    # operation lock is held. Notify only: never acquire that lock or do DB I/O.
    notify = _operation_completion_notify
    if notify is not None:
        notify()


@contextmanager
def _operation_completion_event(loop: asyncio.AbstractEventLoop):
    global _operation_completion_notify
    wakeup = asyncio.Event()

    def notify() -> None:
        try:
            loop.call_soon_threadsafe(wakeup.set)
        except RuntimeError:
            # A request can finish after the loop's cancellation/closure. The
            # durable Job and ordinary scheduler poll remain authoritative.
            pass

    _operation_completion_notify = notify
    try:
        yield wakeup
    finally:
        if _operation_completion_notify is notify:
            _operation_completion_notify = None


async def _refill_until_deadline(wakeup: asyncio.Event, delay: float) -> None:
    """Coalesce completions without postponing heartbeat/maintenance deadlines."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(wakeup.wait(), timeout=remaining)
        except TimeoutError:
            return
        wakeup.clear()
        # Use the same candidate/claim path. Never claim directly from a private
        # upload queue: pause, fair turns and all operation tiers still apply.
        await asyncio.to_thread(process_jobs_once, wait=False)


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
    _shutdown_watcher_scan_executor()
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


# Automatic uploads that cannot run now (paused, unstable, unreadable, changed)
# are left out of the candidate query for a while, with a growing delay, so a
# permanently unrunnable Job (excluded path, not a regular file, EACCES) is
# rechecked ever more rarely and never keeps hiding the Jobs queued behind it.
# In memory on purpose: a restart simply rechecks them; the durable Job is untouched.
UPLOAD_DEFER_SECONDS = 15.0
UPLOAD_DEFER_MAX_SECONDS = 120.0
_DEFER_FORGET_SECONDS = 900.0
_DEFER_LIMIT = 50000
# Candidate-scan rounds per scheduler tick: each round reads the next window of
# every Vault after setting aside the Jobs found not runnable in the previous one.
MAX_SCAN_ROUNDS = 20
_deferred_jobs: dict[tuple[str, int], tuple[float, int]] = {}
_deferred_lock = threading.Lock()


def _database_scope(connection: Any) -> str:
    """Identify the database a Job id belongs to (ids restart per database)."""
    return str(getattr(getattr(connection, "info", None), "dbname", ""))


def _defer_job(scope: str, job_id: int, *, seconds: float | None = None) -> None:
    with _deferred_lock:
        _, attempts = _deferred_jobs.get((scope, job_id), (0.0, 0))
        delay = (
            seconds if seconds is not None
            else min(UPLOAD_DEFER_SECONDS * (2 ** min(attempts, 10)), UPLOAD_DEFER_MAX_SECONDS)
        )
        if len(_deferred_jobs) >= _DEFER_LIMIT and (scope, job_id) not in _deferred_jobs:
            oldest = sorted(_deferred_jobs, key=lambda key: _deferred_jobs[key][0])
            for stale in oldest[: _DEFER_LIMIT // 10]:
                _deferred_jobs.pop(stale, None)
        _deferred_jobs[(scope, job_id)] = (time.monotonic() + delay, attempts + 1)


def _clear_deferral(scope: str, job_id: int) -> None:
    with _deferred_lock:
        _deferred_jobs.pop((scope, job_id), None)


def _deferred_job_ids(scope: str) -> list[int]:
    """Job ids currently set aside; long-expired entries (and their backoff) are forgotten."""
    current = time.monotonic()
    with _deferred_lock:
        for key in [k for k, (until, _) in _deferred_jobs.items() if until + _DEFER_FORGET_SECONDS <= current]:
            _deferred_jobs.pop(key, None)
        return [job_id for (key_scope, job_id), (until, _) in _deferred_jobs.items()
                if key_scope == scope and until > current]


# A queue with runnable work, nothing running and no claim for this long is
# stalled: report it instead of looking healthy while it does not move (#493).
QUEUE_STALL_SECONDS = 300.0


class _QueueProgress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_progress = time.monotonic()

    def note_progress(self) -> None:
        with self._lock:
            self._last_progress = time.monotonic()

    def evaluate(self, queued: int, active: int, *, now: float | None = None) -> tuple[bool, float]:
        """Return ``(stalled, seconds_without_progress)`` for the current queue."""
        current = time.monotonic() if now is None else now
        with self._lock:
            if queued <= 0 or active > 0:
                # Nothing waiting, or work is running: that is progress.
                self._last_progress = current
                return False, 0.0
            idle = max(0.0, current - self._last_progress)
        return idle >= QUEUE_STALL_SECONDS, idle


_queue_progress = _QueueProgress()
# The Owner-facing problem is not resolved by a single claim: it stays open
# until the queue has moved without a stall for this long, and its text is
# refreshed while it lasts. A queue that alternates between a claim and five
# idle minutes therefore raises one problem, not one unread alert per cycle.
QUEUE_STALL_RECOVERY_SECONDS = 600.0
QUEUE_STALL_REFRESH_SECONDS = 60.0
_queue_problem_open: bool | None = None
_queue_last_stalled_at = 0.0
_queue_problem_updated_at = 0.0


def observe_queue_stall(queued: int, *, now: float | None = None) -> bool:
    """Surface a queue that has runnable work but does not advance.

    ``queued`` counts only Jobs the worker could actually start: Jobs it has set
    aside as not runnable (paused, unstable, unreadable) are not work waiting to
    move, so a Vault of permanently unrunnable files is not a stalled queue.
    """
    global _queue_problem_open, _queue_last_stalled_at, _queue_problem_updated_at
    current = time.monotonic() if now is None else now
    stalled, idle = _queue_progress.evaluate(queued, _active_operation_count(), now=current)
    metrics_service.set_gauge("queue_stalled", 1.0 if stalled else 0.0)
    metrics_service.set_gauge("queue_stalled_seconds", idle if stalled else 0.0)
    health_service.set_queue_status(stalled=stalled, queued=queued, idle_seconds=idle if stalled else 0.0)
    if stalled:
        _queue_last_stalled_at = current
    open_problem = _queue_problem_open
    if stalled and (not open_problem or current - _queue_problem_updated_at >= QUEUE_STALL_REFRESH_SECONDS):
        with db() as connection:
            notification_service.set_problem(
                connection, key="queue_stalled", event="queue_stalled", active=True,
                params={"count": queued, "minutes": int(idle // 60)}, email=False,
            )
        _queue_problem_open, _queue_problem_updated_at = True, current
    elif not stalled and (
        open_problem is None
        or (open_problem and current - _queue_last_stalled_at >= QUEUE_STALL_RECOVERY_SECONDS)
    ):
        with db() as connection:
            notification_service.set_problem(
                connection, key="queue_stalled", event="queue_stalled", active=False, email=False,
            )
        _queue_problem_open = False
    return stalled


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
    from .services.recovery_approval import sweep_expired
    with db() as connection:
        sweep_expired(connection, now=datetime.now(timezone.utc).isoformat())
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
    runtime = _runtime_settings()
    concurrency = int(runtime.operation_concurrency)
    # Busy workers still observe one head per Vault: otherwise an idle Vault's
    # starvation clock would not start until a long transfer finally freed a slot.
    # No backlog readiness checks or claims are done without capacity.
    if wait:
        available_slots = concurrency
    else:
        available_slots = max(0, concurrency - _active_operation_count())
    now = now_iso()
    current = datetime.now(timezone.utc)
    try:
        scheduler_clock: datetime | None = datetime.fromisoformat(now)
    except ValueError:
        scheduler_clock = None
    # The window is cut per Vault and tier, after excluding candidates this
    # worker recently found not runnable, so neither a burst in one Vault nor a
    # stuck head of the queue can hide other Vaults' work (#493, #500).
    window = max(10, concurrency * 2) if available_slots else 1
    ready_candidates: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    seen_purge_groups: set[tuple[int, str]] = set()
    scope = ""
    checked_ready: set[int] = set()
    nominated_vaults: set[int] = set()
    for _scan_round in range(MAX_SCAN_ROUNDS):
        with db() as connection:
            scope = _database_scope(connection)
            catalog = ArchiveCatalog(connection)
            candidates = [
                row for row in catalog.list_claimable_jobs(
                    now=now,
                    restore_due_before=_restore_due_before(runtime, current=current),
                    limit=max(10, concurrency * 10) * 8,
                    per_vault_limit=window,
                    exclude_job_ids=_deferred_job_ids(scope),
                    unserved_only=not available_slots,
                )
                if int(row["id"]) not in seen_ids
            ]
            if not candidates:
                # This includes a wholly deferred local queue: leaving its old
                # clock open would incorrectly bank the backoff after restart.
                catalog.observe_scheduler_turns(now=now, runnable_vault_ids=[])
                break
            seen_ids.update(int(row["id"]) for row in candidates)
            if _scan_round == 0:
                active_by_vault = catalog.active_job_counts(now=now)
                preflight = cloud_preflight_gate.snapshot(connection)
                preflight_wait = ((preflight['status'] == 'blocked' and
                                   not cloud_preflight_gate.probe_due(preflight, current))
                                  or (not wait and not cloud_preflight_gate.dispatch_ready(preflight, current)))
            eligible_candidates: list[dict[str, Any]] = []
            for row in candidates:
                job = dict(row)
                vault_id = int(job["vault_id"])
                # Only handlers which call validate_cloud_vault are gated; this
                # does not stop local catalog scans, notifications or heartbeat.
                if preflight_wait and job["action"] in {"upload", "recover", "rename", "free-space", "storage-class", "cloud-purge"}:
                    continue
                if vault_relocation.local_work_suspended({"id": vault_id}):
                    continue
                if job["action"] == "cloud-purge":
                    if not job.get("group_id"):
                        continue
                    purge_group = (vault_id, str(job["group_id"]))
                    if purge_group in seen_purge_groups:
                        continue
                    seen_purge_groups.add(purge_group)
                eligible_candidates.append(job)

        # Observe one runnable head per Vault, not every backlog element. Local
        # fences must pass BEFORE banking starvation credit. Additional Jobs of
        # that Vault are still checked lazily only if capacity needs them.
        runnable_vaults: set[int] = set()
        unready_ids: set[int] = set()
        deferred_now = 0
        for job in sorted(eligible_candidates, key=lambda row: (
            job_priority(row), str(row.get('requested_at') or ''), int(row['id']),
        )):
            vault_id = int(job['vault_id'])
            if vault_id in runnable_vaults:
                continue
            if job['action'] == 'upload' and job.get('origin') == 'automatic':
                if _automatic_upload_state(job) == UPLOAD_NOT_READY:
                    _defer_job(scope, int(job['id']))
                    unready_ids.add(int(job['id']))
                    deferred_now += 1
                    continue
                checked_ready.add(int(job['id']))
                _clear_deferral(scope, int(job['id']))
            runnable_vaults.add(vault_id)
        with db() as connection:
            turns = ArchiveCatalog(connection).observe_scheduler_turns(
                now=now, runnable_vault_ids=sorted(runnable_vaults),
            )
        eligible_candidates = [job for job in eligible_candidates if int(job['id']) not in unready_ids]
        for job in eligible_candidates:
            job.update(turns.get(int(job['vault_id']), {}))
            if int(job['vault_id']) in nominated_vaults:
                job['scheduler_wait_seconds'] = 0
        ordered = select_fair_jobs(
            eligible_candidates, limit=len(eligible_candidates),
            active_by_vault=active_by_vault, now=scheduler_clock,
        )
        for job in ordered:
            if len(ready_candidates) >= available_slots or shutdown_state.requested:
                break
            if (job['action'] == 'upload' and job.get('origin') == 'automatic'
                    and int(job['id']) not in checked_ready):
                # The stability/suspension fence precedes preflight, even when an
                # SMB edit has not reached the watcher/catalog yet. A removed file
                # is still dispatched: its worker closes the Job (#493, #499).
                if _automatic_upload_state(job) == UPLOAD_NOT_READY:
                    _defer_job(scope, int(job["id"]))
                    deferred_now += 1
                    continue
                _clear_deferral(scope, int(job["id"]))
            # Credit belongs to ONE nomination. Later backlog Jobs may fill
            # otherwise available slots, but cannot reuse an aged turn's tier.
            if int(job['vault_id']) in nominated_vaults:
                job['scheduler_wait_seconds'] = 0
            nominated_vaults.add(int(job['vault_id']))
            ready_candidates.append(job)
        if len(ready_candidates) >= available_slots or shutdown_state.requested or not deferred_now:
            break
    # The durable ready cache may belong to a previous deployment's credentials.
    # Probe outside the candidate transaction BEFORE acquiring any Job lease.
    # The live non-waiting scheduler never performs provider I/O: the dedicated
    # readiness loop proves startup/expiry while heartbeat/delivery keep moving.
    if ready_candidates and wait:
        try:
            validate_cloud_vault(ready_candidates[0])
        except cloud_preflight_gate.CloudPreflightBlocked:
            return 0

    selected = ready_candidates
    claimed_jobs: list[dict[str, Any]] = []
    for job in selected:
        if shutdown_state.requested:
            break
        claimed_at = now_iso()
        claim_token = str(uuid.uuid4())
        claim_expires_at = _claim_expiry_at(claimed_at)
        generation = job.get('scheduler_generation') if turn_is_aged(job) else None
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
                    scheduler_generation=generation,
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
                    scheduler_generation=generation,
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
                    future = executor.submit(process_job, job)
                    _operation_futures[job_id] = future
                    future.add_done_callback(_operation_completed)
    if claimed_jobs:
        _queue_progress.note_progress()
    return len(claimed_jobs)


def claimable_queue_depth(*, exclude_deferred: bool = False) -> int:
    """Return the actual unleased runnable backlog for the ``queue_depth`` gauge.

    ``exclude_deferred`` leaves out Jobs the worker set aside as not runnable
    right now: the stall detector counts only work that should be moving.
    """
    timestamp = now_iso()
    with db() as connection:
        runtime = _runtime_settings()
        return ArchiveCatalog(connection).claimable_queue_depth(
            now=timestamp,
            restore_due_before=_restore_due_before(runtime),
            exclude_job_ids=(
                _deferred_job_ids(_database_scope(connection)) if exclude_deferred else ()
            ),
        )


def scan_all_vaults() -> None:
    with db() as connection:
        vaults = connection.execute(
            f"{VAULT_ROW_SQL} WHERE v.lifecycle='active' ORDER BY v.id"
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
        if vault.get("lifecycle", "active") != "active" or vault_relocation.local_work_suspended(vault):
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
            current = connection.execute(
                f"{VAULT_ROW_SQL} WHERE v.id=%s", (vault["id"],),
            ).fetchone()
            if (not current or current["lifecycle"] != "active"
                    or current["source_root"] != vault["source_root"]):
                return 0
            queued = 0
            if retry_failed_only:
                from .services import upload_remediation
                queued += upload_remediation.retry_vault(connection,dict(current),None,
                    (now or datetime.now(timezone.utc)).isoformat())
            queued += queue_auto_uploads(
                connection, vault_id=int(vault["id"]), source_root=str(root),
                now=now, retry_failed_only=retry_failed_only,
            )
            if "root_identity" in vault:
                _current_verified_scan_row(connection, vault)
            if not source_layout.vault_local_access(vault["source_root"]).local_operations_allowed:
                raise _ScanRootMismatch("Source Volume changed during automatic upload admission")
        # Admission completed: whatever failed before is over (#496). Done after
        # the Vault transaction ends so the inbox write takes no Owner lock under it.
        _note_worker_ok("auto_upload_queue", int(vault["id"]))
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


def scan_vaults_with_cloud_error() -> None:
    """Scan the Vaults whose last cloud observation failed.

    A failed listing leaves the cloud component in error until a scan lists S3
    again; nothing else clears it. Once S3 is reachable again that scan is due.
    """
    with db() as connection:
        vaults = connection.execute(
            f"""
            {VAULT_ROW_SQL}
            JOIN vault_component_health h
              ON h.vault_id=v.id AND h.component='cloud' AND h.last_error IS NOT NULL
            WHERE v.lifecycle='active'
            ORDER BY v.id
            """
        ).fetchall()
    for vault in vaults:
        scan_vault(vault)


def recover_vaults_after_cloud_ready() -> None:
    scan_vaults_with_cloud_error()
    # A failed audit also stays failed until an audit lists S3 again (#620).
    audit_vaults_with_error()


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
        was_blocked = state['status'] == 'blocked'
        # The bucket belongs to the Vault Target (#593): only the projection
        # carries ``s3_bucket``, a bare Vault row would block the gate for good.
        vault = connection.execute(f"{VAULT_ROW_SQL} ORDER BY v.id LIMIT 1").fetchone()
    if vault:
        try:
            validate_cloud_vault(dict(vault))
        except cloud_preflight_gate.CloudPreflightBlocked:
            return
        if was_blocked:
            # S3 answers again (a corrected key, bucket or permission). Without a
            # restart nothing else would list it again before the next periodic
            # scan, and every Vault that failed on S3 would stay unconfirmed.
            _submit_maintenance("cloud_recovery_scan", recover_vaults_after_cloud_ready)


async def cloud_preflight_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(reprobe_cloud_readiness_once)
            await asyncio.to_thread(_note_worker_ok, "cloud_preflight")
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


async def history_retention_loop() -> None:
    # Retention of closed history (#596): short keyset batches on a dedicated
    # DB-only executor; it deletes database rows only.
    from .services import history_retention
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="history-retention")
    try:
        while True:
            try:
                await loop.run_in_executor(executor, history_retention.collect, db)
                await loop.run_in_executor(executor, _note_worker_ok, "history_retention")
            except Exception as exc:
                await asyncio.to_thread(_report_worker_error, "history_retention", exc)
            await asyncio.sleep(history_retention.POLL_SECONDS)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


async def bulk_upload_admission_loop() -> None:
    # Durable state lives in PostgreSQL, not this executor. One tick advances
    # bounded transactions independently of transfer slots/cloud readiness.
    from .services import bulk_upload_admission
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bulk-upload-admission")
    try:
        while True:
            try:
                await loop.run_in_executor(executor, bulk_upload_admission.tick, db)
                await loop.run_in_executor(executor, _note_worker_ok, "bulk_upload_admission")
            except Exception as exc:
                await asyncio.to_thread(_report_worker_error, "bulk_upload_admission", exc)
            await asyncio.sleep(bulk_upload_admission.POLL_SECONDS)
    finally:
        # Do not let a committing batch outlive shutdown/pool closure.
        await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)


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


# A watcher that stopped is restarted after a growing pause. A run counts as
# healthy again, and resets the pause, once it stayed up this long NOT counting
# the time it spent in scans: a long scan must not hide a watcher that dies at
# once.
WATCHER_RESTART_BASE_SECONDS = 5
WATCHER_RESTART_MAX_SECONDS = 300
WATCHER_STABLE_SECONDS = 60
# Whatever stops a watcher (a failure, a mount that comes back) loses events, so
# the Vault is scanned again. Two such scans are never closer than this, so a
# watcher that stops in a burst cannot turn into a burst of full scans.
WATCHER_GAP_RESCAN_MIN_SECONDS = 300
# While a scan names paths it could not observe, only those paths are looked at
# this often; the full scan runs again as soon as one of them changed.
WATCHER_RECHECK_SECONDS = 60
# A scan that failed or could not reach S3 is repeated after this pause.
WATCHER_SCAN_FAILED_SECONDS = 900
WATCHER_SCAN_RETRY_SECONDS = 60
WATCHER_SCAN_BUSY_SECONDS = 10
# A watch batch that fails this many times in a row is dropped: the scan that
# follows every restart covers what it contained, and a batch that can never be
# applied must not keep the whole Vault from being observed.
WATCHER_BATCH_MAX_FAILURES = 3


def _flattened_exceptions(exc: BaseException) -> Iterator[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from _flattened_exceptions(inner)
    else:
        yield exc


def _is_undecodable_path_error(exc: BaseException) -> bool:
    """watchfiles raises this when an event or a directory is not UTF-8.

    The native observer cannot represent such a path, so it stops. That is a
    property of the filename, not an internal failure of the watcher.
    """
    return any("Unable to decode path" in str(inner) for inner in _flattened_exceptions(exc))


_DENIED_WATCH_PATH = re.compile(r"IO error for operation on (.+?): Permission denied")


def _watcher_denied_directories(exc: BaseException, root: Path) -> set[str]:
    """Folders a native or polling observer could not enroll, relative to root.

    Native registration raises PermissionError with notify's path list; polling
    reports an IO error naming one path. Keep the discovery even if permissions
    have already been repaired: a later readable walk cannot prove coverage.
    """
    found: set[str] = set()
    for inner in _flattened_exceptions(exc):
        paths = [match.group(1) for match in _DENIED_WATCH_PATH.finditer(str(inner))]
        if isinstance(inner, PermissionError):
            # watchfiles 1.3: PermissionError has neither errno nor filename;
            # notify appends `about ["/path"]` to its message instead.
            _message, separator, encoded = str(inner).partition(" about ")
            if separator:
                try:
                    native_paths = json.loads(encoded)
                except (ValueError, TypeError):
                    native_paths = []
                if isinstance(native_paths, list):
                    paths.extend(value for value in native_paths if isinstance(value, str))
        for value in paths:
            path = Path(value)
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if relative != "." and path.is_dir():
                found.add(relative)
    return found


def _watcher_restart_delay(streak: int) -> float:
    return float(min(WATCHER_RESTART_MAX_SECONDS, WATCHER_RESTART_BASE_SECONDS * 2 ** max(0, streak - 1)))


class _WatcherScanOutcome(NamedTuple):
    # Seconds until the Vault must be scanned again; None when reconciled.
    retry_after: float | None = None
    # The scan was complete except for paths it named: re-check only those.
    recheck: bool = False


_watcher_scan_pool: ThreadPoolExecutor | None = None
_watcher_scan_pool_lock = threading.Lock()


def _watcher_scan_executor() -> ThreadPoolExecutor:
    """One thread for every paired scan: Vaults take turns, as in scan_all_vaults.

    A dedicated pool keeps the long scans off the default executor, which the
    watchers themselves and every request share.
    """
    global _watcher_scan_pool
    with _watcher_scan_pool_lock:
        if _watcher_scan_pool is None:
            _watcher_scan_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="watcher-scan")
        return _watcher_scan_pool


def _shutdown_watcher_scan_executor() -> None:
    global _watcher_scan_pool
    with _watcher_scan_pool_lock:
        pool, _watcher_scan_pool = _watcher_scan_pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


async def _reconcile_vault_after_watcher(vault: dict[str, Any]) -> _WatcherScanOutcome:
    """Run the full scan that a freshly registered watcher is paired with.

    Registration happens first, so anything that arrives during the scan is
    seen by the watcher and anything older is seen by the scan.
    """
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(_watcher_scan_executor(), scan_vault, vault)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        worker_error_store.log_worker_error("source_watcher_scan", exc, int(vault["id"]))
        return _WatcherScanOutcome(float(WATCHER_SCAN_RETRY_SECONDS))
    if result.get("already_running") or result.get("suspended"):
        return _WatcherScanOutcome(float(WATCHER_SCAN_BUSY_SECONDS))
    if result.get("local_skipped"):
        return _WatcherScanOutcome(float(WATCHER_SCAN_RETRY_SECONDS))
    if int(result.get("local", 0)) < 0:
        return _WatcherScanOutcome(float(WATCHER_SCAN_FAILED_SECONDS))
    if result.get("local_incomplete"):
        return _WatcherScanOutcome(recheck=True)
    if int(result.get("cloud", 0)) < 0:
        # The Vault was walked, but S3 could not be reached: it is not reconciled.
        return _WatcherScanOutcome(float(WATCHER_SCAN_FAILED_SECONDS))
    return _WatcherScanOutcome()


async def _watch_vault_filesystem(vault: dict[str, Any]) -> None:
    try:
        await _run_vault_filesystem_watch(vault)
    finally:
        watch_plan.forget_registration(int(vault["id"]), owner=asyncio.current_task())


async def _run_vault_filesystem_watch(vault: dict[str, Any]) -> None:
    # Some Source Volume backends do not reliably enroll new directories.
    # Install the successor BEFORE draining/closing the old recursive observer:
    # subtree catch-up alone cannot repair unrelated sibling events in a gap.
    pending_reconcile: set[tuple[Change, str]] = set()
    vault_id = int(vault["id"])
    loop = asyncio.get_running_loop()
    # A new watcher task starts from what a scan finds, not from a past root.
    watch_plan.forget(vault_id)
    # Every watcher run is paired with a scan once it is registered and healthy
    # (issues #496, #497): what happened before the watcher existed, or while it
    # was down, is invisible to it. None means no full scan is due.
    next_scan_at: float | None = 0.0
    # Due time of the cheap re-check of the paths the last scan could not observe.
    recheck_at: float | None = None
    failure_streak = 0
    last_scan_end = float("-inf")
    batch_failures = 0
    # What the registered observer was built around; a change re-registers it.
    registered_gaps = watch_plan.UnobservedPaths()
    registered_debt: dict[str, int] = {}

    def schedule_gap_scan() -> None:
        nonlocal next_scan_at
        next_scan_at = max(loop.time(), last_scan_end + WATCHER_GAP_RESCAN_MIN_SECONDS)

    async def reconcile() -> None:
        nonlocal next_scan_at, recheck_at, last_scan_end, scan_seconds
        began = loop.time()
        outcome = await _reconcile_vault_after_watcher(vault)
        last_scan_end = loop.time()
        scan_seconds += last_scan_end - began
        next_scan_at = None if outcome.retry_after is None else last_scan_end + outcome.retry_after
        recheck_at = last_scan_end + WATCHER_RECHECK_SECONDS if outcome.recheck else None

    scan_seconds = 0.0
    while True:
        if vault_relocation.local_work_suspended(vault):
            await asyncio.sleep(1)
            continue
        if protection_stop_service.local_work_suspended(vault):
            await asyncio.sleep(1)
            continue
        with db() as connection:
            current = connection.execute(
                "SELECT lifecycle FROM vaults WHERE id=%s", (vault["id"],),
            ).fetchone()
        if current and current["lifecycle"] != "active":
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
                _record_scan_component_health, vault_id, "source",
                ok=False, error="Protection blocked: Source Volume unavailable",
            )
            schedule_gap_scan()
            await asyncio.sleep(5)
            continue
        try:
            root = Path(vault["source_root"]).resolve()
            if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
                raise PermissionError("Vault root is inaccessible")
        except OSError:
            await asyncio.to_thread(
                _record_scan_component_health, vault_id, "source",
                ok=False, error="Protection blocked: Vault root inaccessible",
            )
            schedule_gap_scan()
            await asyncio.sleep(5)
            continue
        recovered = False
        applying = False
        started_at = loop.time()
        scan_seconds = 0.0
        try:
            runtime = await asyncio.to_thread(_runtime_settings)

            def new_observer():
                nonlocal registered_gaps, registered_debt
                gaps, registered_debt = watch_plan.registration_plan(vault_id)
                registered_gaps = gaps
                options = dict(
                    watch_filter=_filesystem_watch_filter,
                    debounce=runtime.filesystem_watch_debounce_ms,
                    force_polling=settings.filesystem_watch_force_polling,
                    poll_delay_ms=runtime.filesystem_watch_poll_ms,
                    # Native EACCES MUST surface (#565): ignoring it can yield
                    # a successful barrier with a new directory silently left
                    # unwatched. A later chmod/chown raises no topology event.
                    # Record the named gap and restart around it instead. Keep
                    # polling's existing snapshot/permission behavior unchanged.
                    ignore_permission_denied=settings.filesystem_watch_force_polling,
                    # awatch is lazy: its first yield (including a timeout)
                    # is the registration barrier, for inotify AND polling.
                    yield_on_timeout=True,
                    rust_timeout=1000,
                )
                if not gaps.directories:
                    return awatch(root, recursive=True, **options)
                flat, recursive = watch_plan.watch_directories(root, gaps)
                observers = []
                if flat:
                    observers.append(awatch(*flat, recursive=False, **options))
                if recursive:
                    observers.append(awatch(*recursive, recursive=True, **options))
                return watch_plan.MergedWatch(observers)

            watcher = new_observer()
            successor = None
            rearm_required = False
            try:
                while True:
                    native_changes = set() if rearm_required else await anext(watcher)
                    if not rearm_required:
                        watch_plan.record_registration(
                            vault_id, registered_gaps, owner=asyncio.current_task(), debt=registered_debt,
                        )
                    pending_reconcile |= native_changes
                    new_directories = await asyncio.to_thread(
                        _new_watcher_directories, root, native_changes,
                    ) if native_changes else set()
                    # PollWatcher recursively rediscovers topology every poll;
                    # it needs no re-registration. Keep its original snapshot
                    # alive: a timeout is NOT a completed polling-cycle fence,
                    # so replacing it could lose pre-successor sibling changes.
                    if (
                        (new_directories and not settings.filesystem_watch_force_polling)
                        or rearm_required
                    ):
                        old_gaps = registered_gaps
                        successor = new_observer()
                        # Old observer is still live while the successor takes
                        # its recursive snapshot / registers native watches.
                        first = await anext(successor)
                        settled = watch_plan.record_registration(
                            vault_id, registered_gaps, owner=asyncio.current_task(), debt=registered_debt,
                        )
                        if (old_gaps.directories | settled) - registered_gaps.directories:
                            # Coverage expanded: files may predate the successor's
                            # snapshot. Pair this handoff with a scan, just like
                            # a healthy watcher start (ADR 0027), before success.
                            next_scan_at = loop.time()
                        pending_reconcile |= first
                        tail = await anext(watcher)
                        pending_reconcile |= tail
                        old, watcher = watcher, successor
                        successor = None
                        await old.aclose()
                        # Every directory addition DURING registration needs
                        # another handoff, even at a pathname in the original
                        # batch: that directory may have been replaced after
                        # the successor registered its old inode. A duplicate
                        # notification costs one extra handoff; deduplicating by
                        # pathname can silently leave the new inode unwatched.
                        # No full-root walk, and at most two observers.
                        additions = await asyncio.to_thread(
                            _new_watcher_directories, root, first | tail,
                        )
                        rearm_required = bool(additions)
                    if pending_reconcile and not rearm_required:
                        # Reconcile only once every added subtree is covered by
                        # a registered successor, including additions made in
                        # the preceding handoff itself.
                        applying = True
                        await asyncio.to_thread(_apply_watcher_changes, vault, pending_reconcile)
                        applying = False
                        batch_failures = 0
                        pending_reconcile = set()
                    if not recovered and not pending_reconcile and not rearm_required:
                        # First fully healthy cycle of this run: registration
                        # barrier passed and every pending change applied.
                        await asyncio.to_thread(
                            _resolve_worker_error, "source_watcher", vault_id,
                        )
                        recovered = True
                    if recheck_at is None and watch_plan.unobserved(vault_id):
                        # A batch or a failed observer found a gap outside a
                        # full scan: its repair raises no event either.
                        recheck_at = loop.time() + WATCHER_RECHECK_SECONDS
                    if (
                        recovered and not pending_reconcile and not rearm_required
                        and next_scan_at is not None and loop.time() >= next_scan_at
                    ):
                        recheck_at = None
                        await reconcile()
                    elif (
                        recovered and not pending_reconcile and not rearm_required
                        and recheck_at is not None and loop.time() >= recheck_at
                    ):
                        known = watch_plan.unobserved(vault_id)
                        repaired = not await asyncio.to_thread(
                            watch_plan.still_unobserved, root, known,
                        )
                        if repaired:
                            await reconcile()
                        else:
                            recheck_at = loop.time() + WATCHER_RECHECK_SECONDS
                    if (
                        recovered and not rearm_required
                        and (watch_plan.unobserved(vault_id).directories != registered_gaps.directories
                             or watch_plan.coverage_debt(vault_id))
                    ):
                        # A permission was repaired, a name renamed, or a folder
                        # turned out unreadable: register the tree again.
                        rearm_required = True
            finally:
                if successor is not None:
                    await successor.aclose()
                await watcher.aclose()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            now = loop.time()
            # Quick repeated failures grow the pause. Time spent scanning is not
            # time the watcher stayed up.
            uptime = now - started_at - scan_seconds
            failure_streak = 1 if uptime >= WATCHER_STABLE_SECONDS else failure_streak + 1
            schedule_gap_scan()
            if applying and pending_reconcile:
                batch_failures += 1
                if batch_failures >= WATCHER_BATCH_MAX_FAILURES:
                    worker_error_store.log_worker_error("source_watcher_batch_dropped", exc, vault_id)
                    pending_reconcile = set()
                    batch_failures = 0
            denied = await asyncio.to_thread(_watcher_denied_directories, exc, root)
            if denied:
                watch_plan.note_unreadable_directories(vault_id, denied)
                # Registration can fail before any batch/scan sees the denial,
                # and the folder can already be readable by the next walk.
                # Publish its path now, independently of the retry scan.
                with db() as connection:
                    for relative in sorted(denied):
                        report_problem(
                            connection, event="file_unreadable", vault_id=vault_id, path=relative,
                        )
            if _is_undecodable_path_error(exc):
                # A filename that is not UTF-8 stopped the native observer and the
                # events around it are lost: not an internal error. The Vault is
                # not confirmed until a scan has looked again; that scan names the
                # path, and the restart registers the tree around a directory
                # with such a name (#494).
                worker_error_store.log_worker_error("source_watcher", exc, vault_id)
                await asyncio.to_thread(
                    _record_scan_component_health, vault_id, "source",
                    ok=False, notify_problem=False,
                    error="Source watcher stopped on a file name that is not valid UTF-8",
                )
            elif denied:
                # Native registration or the polling backend could not enter
                # a folder: a path problem, not an internal error. The restart
                # registers around it and the cheap retry repairs coverage.
                worker_error_store.log_worker_error("source_watcher", exc, vault_id)
                await asyncio.to_thread(
                    _record_scan_component_health, vault_id, "source",
                    ok=False, notify_problem=False,
                    error="Source watcher stopped on a folder it may not read",
                )
            else:
                with status_lock:
                    status = runtime_status.setdefault(
                        vault_id,
                        {"scanning": False, "last_scan": None, "last_error": None},
                    )
                    status["last_error"] = f"Source watcher: {exc}"
                if isinstance(exc, OSError):
                    await asyncio.to_thread(
                        _record_scan_component_health, vault_id, "source",
                        ok=False, error="Protection blocked: Source watcher stopped on an unreadable path",
                    )
                else:
                    await asyncio.to_thread(_report_worker_error, "source_watcher", exc, vault_id)
            if not recovered and not denied and next_scan_at is not None and loop.time() >= next_scan_at:
                # Unknown registration failures still need a scan to name the
                # gap. A named permission gap already has a problem and plan:
                # wait for registration around it before scanning. Otherwise a
                # repair before this walk could clear source health without any
                # registered observer, and consume the start's paired scan.
                await reconcile()
            await asyncio.sleep(_watcher_restart_delay(failure_streak))


def _enabled_vaults() -> list[dict[str, Any]]:
    with db() as connection:
        return connection.execute(
            f"{VAULT_ROW_SQL} WHERE v.lifecycle='active' ORDER BY v.id"
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


# Failed audits are retried on their own backoff, in monotonic seconds (#620).
# A failure from before a restart needs no retry: it makes the full audit due
# at startup (``persisted_maintenance_ages``).
_audit_retry_lock = threading.Lock()
_audit_retry: dict[str, float] = {"failures": 0, "due": float("inf")}


def _note_audit_outcome(failed: bool) -> None:
    with _audit_retry_lock:
        if failed:
            _audit_retry["failures"] += 1
            _audit_retry["due"] = time.monotonic() + audit_failures.retry_seconds(
                int(_audit_retry["failures"]))
        else:
            _audit_retry.update(failures=0, due=float("inf"))


def audit_retry_due(now: float | None = None) -> bool:
    with _audit_retry_lock:
        return (time.monotonic() if now is None else now) >= _audit_retry["due"]


def _active_vaults_for_audit(*, failed_only: bool) -> list[dict[str, Any]]:
    failed = (
        "JOIN vault_component_health h ON h.vault_id=v.id AND h.component='audit' "
        "AND h.last_error IS NOT NULL" if failed_only else ""
    )
    with db() as connection:
        return [dict(row) for row in connection.execute(
            f"{VAULT_ROW_SQL} {failed} WHERE v.lifecycle='active' ORDER BY v.id"
        ).fetchall()]


def audit_all_vaults() -> None:
    _audit_vaults(_active_vaults_for_audit(failed_only=False))


def audit_vaults_with_error() -> None:
    """Re-audit the Vaults whose last audit failed; nothing else clears it."""
    _audit_vaults(_active_vaults_for_audit(failed_only=True))


def _audit_vaults(vaults: list[dict[str, Any]]) -> None:
    failed = False
    for vault in vaults:
        failed = not _audit_vault(vault) or failed
    _note_audit_outcome(failed)


def _audit_vault(vault: dict[str, Any]) -> bool:
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
        return True
    except Exception as exc:
        message = f"Catalog audit: {exc}"
        with status_lock:
            status["last_error"] = message
        _record_scan_component_health(
            vault_id, "audit", ok=False, error=message,
            problem_params=audit_failures.problem_params(exc),
        )
        return False


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
            SELECT id FROM vaults WHERE lifecycle='active'
            """
        ).fetchall()
        audit_ages: list[float] = []
        for vault in vaults:
            # From the last success: a failed audit has not run, and a Vault
            # it left unconfirmed is audited again at startup (#620).
            row = connection.execute(
                """
                SELECT last_success_at, last_error FROM vault_component_health
                WHERE vault_id=%s AND component='audit'
                """,
                (int(vault["id"]),),
            ).fetchone()
            age = (
                _seconds_since(row["last_success_at"], now=now)
                if row and not row["last_error"] else None
            )
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
    with _operation_completion_event(loop) as wakeup:
        await _background_loop(loop, wakeup)


def _record_background_loop_error(exc: Exception) -> None:
    # Never include paths/provider messages in logs. The existing bounded error
    # store and counter own diagnostics for ordinary and fast-refill cycles.
    try:
        with db() as connection:
            worker_error_store.record_worker_error(
                connection, component="background_loop", exc=exc,
            )
        metrics_service.inc("worker_errors_total")
    except Exception:
        pass
    metrics_service.set_gauge("worker_up", 0)


async def _background_loop(loop: asyncio.AbstractEventLoop, wakeup: asyncio.Event) -> None:
    try:
        ages = await asyncio.to_thread(persisted_maintenance_ages)
    except Exception:
        ages = {}
    marks = initial_maintenance_marks(loop.time(), ages)
    # A new loop starts from the persisted ages, not from an earlier retry.
    _note_audit_outcome(False)
    last_scan = marks["scan"]
    last_audit = marks["audit"]
    last_backup = marks["backup"]
    last_backup_verify = marks["backup_verify"]
    backlog_observed_at: float | None = None
    backlog_runnable = 0
    last_aggregates: float | None = None
    try:
        while True:
            # Clear before dispatch, not before waiting: a completion arriving
            # during the normal cycle must still trigger a prompt refill.
            wakeup.clear()
            runnable_count = 0
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
                # /api/files request path (issue #229), one batch per interval
                # rather than one per cycle (#622).
                if (last_aggregates is None
                        or loop.time() - last_aggregates >= DIRECTORY_AGGREGATE_BATCH_SECONDS):
                    last_aggregates = loop.time()
                    await asyncio.to_thread(process_directory_aggregate_maintenance)
                await asyncio.to_thread(protection_stop_service.reconcile_all)
                # Backlog is measured independently from the selected batch: an
                # active lease is no longer claimable, while every remaining due
                # queued row is visible even when concurrency is small. Stuck
                # intermediate Jobs with expired leases are a separate series.
                # Each of these counts reads the whole backlog. While the queue
                # is deep the loop spins every 100 ms, so counting on every
                # cycle costs more the more work is waiting. A deep queue is
                # sampled; a short one is still counted exactly every cycle.
                current = loop.time()
                if (
                    backlog_observed_at is None
                    or backlog_runnable < max(10, runtime.operation_concurrency * 10)
                    or current - backlog_observed_at >= BACKLOG_OBSERVE_SECONDS
                ):
                    queued_count = await asyncio.to_thread(claimable_queue_depth)
                    metrics_service.set_gauge("queue_depth", float(queued_count))
                    runnable_count = await asyncio.to_thread(
                        claimable_queue_depth, exclude_deferred=True
                    )
                    await asyncio.to_thread(observe_queue_stall, runnable_count)
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
                    backlog_observed_at, backlog_runnable = current, runnable_count
                else:
                    runnable_count = backlog_runnable
                current = loop.time()
                if current - last_scan >= runtime.scan_interval:
                    _submit_maintenance("scan", scan_all_vaults)
                    last_scan = current
                if current - last_audit >= runtime.audit_interval:
                    _submit_maintenance("audit", audit_all_vaults)
                    last_audit = current
                elif audit_retry_due():
                    _submit_maintenance("audit_retry", audit_vaults_with_error)
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
                await asyncio.to_thread(_record_background_loop_error, exc)
            batch_size = max(10, runtime.operation_concurrency * 10)
            # Poll fast only for work the worker can start now: Jobs set aside as
            # not runnable must not keep the loop spinning every 100 ms (#493).
            delay = (
                0.1
                if runnable_count >= batch_size
                else min(
                    NOTIFICATION_MAX_LATENCY_SECONDS,
                    max(0.1, float(runtime.queue_poll_interval or 2)),
                )
            )
            try:
                await _refill_until_deadline(wakeup, delay)
            except Exception as exc:
                await asyncio.to_thread(_record_background_loop_error, exc)
    except asyncio.CancelledError:
        # Lifespan owns pool draining after all producers have been cancelled.
        # Detaching here would lose the running futures before they can drain.
        raise
