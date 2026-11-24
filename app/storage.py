from __future__ import annotations

import asyncio
import email.utils
import hashlib
import logging
import os
import random
import stat
import threading
import time
import uuid
from collections import Counter
from collections.abc import Iterator, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, InvalidStateError, ThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures
from contextlib import closing, contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable

import psycopg
from boto3.s3.transfer import TransferConfig
from .services import s3 as s3_service
from .services.s3_upload import upload_file
from .services.upload_snapshot import PartDigests, prepare_upload_snapshot, validated_s3_proof
from watchfiles import Change

# The native observer runs in a child process with its own GIL (ADR-0053).
from .services.watch_process import awatch

from .config import is_placeholder, settings
from .services.archive_versions import ArchiveVersions
from .services.file_renames import FileRenames
from .database import db
from .services import audit_failures, source_layout
from .services.upload_pipeline import UploadJournalHeld
from .services import local_observation
from .services.restore_ghosts import retire_absent_restore_ghosts
from .services import vault_relocation
from .services import watch_plan
from .services import source_devices
from .services import throughput
from .services import excluded_folders as excluded_folder_rules
from .services import write_access
from .services.catalog_event_hub import publish_committed_event
from .services.catalog_events import record_catalog_revision
from .i18n import parse_message_params, translate
from .services.s3_preflight import check_bucket_readiness, failure_message_key, preflight_failure_message
from .services import cloud_preflight_gate
from .services.catalog_audit import audit_vault_catalog
from .services.directory_aggregates import (
    lock_vaults_in_order,
    mark_path_dirty,
    process_directory_aggregate_maintenance,
    unit_dirty_marks,
)
from .services.lifecycle_policies import sync_lifecycle_rules_for_bucket
from .services import health as health_service
from .services import metadata_backups as metadata_backup_service
from .services import system_bucket as system_bucket_service
from .services import retention_lock as retention_lock_service
from .services import metrics as metrics_service
# ``prepare_upload_snapshot``, ``validated_s3_proof`` and
# ``_verification_version_head`` are called only by ``_WorkerAdapter``, the
# worker seam the Upload execution receives.
from .services.s3 import (
    object_version_entries as _object_version_entries,
    verification_version_head as _verification_version_head,
    restore_header_state,
    storage_class_requires_restore,
)
from .services.vault_paths import (
    RESTORE_TEMPORARY_RE,
    CLEANUP_TEMPORARY_RE,
    VERIFY_TEMPORARY_RE,
    safe_local_path,
    safe_local_entry_path,
    object_key_to_path,
    expected_cloud_key,
    object_prefix,
    vault_object_prefix,
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
    lock_holders,
    runtime_status,
    status_lock,
    scan_lock_for_vault,
)
from .services.fs_preflight import (
    PinnedTree, TreeIdentityChanged, inspect_entry, is_storable_name, removal_is_unproven,
)
from .services.problems import display_path, report_problem, resolve_stale_path_problems
from .services.local_files import (
    hash_stable_regular_file,
    restore_claimed_local_copy,
    _local_stat_unchanged,
)
from .services import worker_errors as worker_error_store
from .services import notifications as notification_service
from .services import cloud_deletion as cloud_deletion_service
from .services.vaults import VAULT_ROW_SQL
from .services import vault_moves
from .services import protection_stop as protection_stop_service
from .services import vault_deletion as vault_deletion_service
from .services import duplicate_cleanup as duplicate_cleanup_service
from .services import mirror_cleanup as mirror_cleanup_service
from .services import bucket_metrics as bucket_metrics_service
from .services import background_scheduler, filesystem_watcher, periodic_workers
from .services.shutdown import (
    WORKER_DRAIN_SECONDS, OperationCancelled, WorkerStopping, shutdown_state,
)
from .services import audit_events as audit_event_store
from .services.restore_estimates import estimate_restore
from .services import job_queue
from .services.job_admission import JobAdmission
from .services.local_copies import LocalCopies
from .services.job_queue import (
    TERMINAL_JOB_STATUSES, JobLeaseLost, JobQueue, job_cancelled, job_priority,
    select_fair_jobs,
)
from .services.operation_policies import (
    get_policy,
    queue_auto_local_cleanups,
    local_cleanup_is_due,
    SOURCE_SCAN_INCOMPLETE,
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


# Long transfers stay on this pool so the worker loop can heartbeat, deliver
# notifications, and start other Jobs without waiting for the whole batch.
NOTIFICATION_MAX_LATENCY_SECONDS = 5.0
_operation_executor: ThreadPoolExecutor | None = None
_operation_futures: dict[int, Future[Any]] = {}
_operation_lock = threading.Lock()
_operation_completion_notify: Callable[[], None] | None = None
_maintenance_executor: ThreadPoolExecutor | None = None
_maintenance_futures: dict[str, Future[Any]] = {}
_aggregate_executor: ThreadPoolExecutor | None = None
_aggregate_future: Future[Any] | None = None
_maintenance_lock = threading.Lock()

# Local scans deliberately keep filesystem work outside write transactions.
# This is small enough to give other PostgreSQL writers frequent admission while
# avoiding one transaction per catalogued entry.
LOCAL_SCAN_WRITE_BATCH_SIZE = 250
# Paths per catalog lookup of a listed folder's fingerprinted files (#696).
_PROBE_LOOKUP_CHUNK = 1000
# How often the traversal refreshes its runtime progress (#696).
_TRAVERSAL_PROGRESS_SECONDS = 1.0
# How many times one scan walks again a folder replaced during it (#686)
# before reporting it as changing, with its protection not confirmed.
SCAN_REPLACED_FOLDER_REWALKS = 3
_scan_logger = logging.getLogger(__name__)
# uvicorn's handler prints INFO in production; the traversal's time split goes
# there so its cost can be read from the container log (#707).
_traversal_logger = logging.getLogger("uvicorn.error")
# The watcher's own findings (#721), printed beside the traversal's.
_watcher_logger = logging.getLogger("uvicorn.error")

# Cloud scans keep provider I/O outside write transactions and commit catalog
# merges in bounded batches so other writers (lease renewal,
# notifications) are not blocked for the full listing.
CLOUD_SCAN_WRITE_BATCH_SIZE = 250
# S3 DeleteObjects accepts at most 1,000 exact targets per request.
CLOUD_DELETE_BATCH_SIZE = 1000


# How often the backlog gauges and the stall detector re-read a deep queue.
BACKLOG_OBSERVE_SECONDS = 10.0
# How often the background loop folds dirty directories into their aggregates
# (#622). While the queue is deep the loop spins every 100 ms: flushing on every
# cycle recomputed each ancestor directory for almost every finished file. A
# batch per interval recomputes it once for all the files finished meanwhile.
# Freshness: a listing that finds dirty directories reports ``stale`` with the
# previous projection and schedules its own refresh at once (ADR-0038).
DIRECTORY_AGGREGATE_BATCH_SECONDS = 2.0
# The Job queue as this process runs it (#794). Claim, lease, transitions and
# reconciliation live in ``services/job_queue``; the runner reads this Module's
# ``db`` and ``now_iso`` when it is called (a patched one is the one that runs).
job_runner = job_queue.QueueRunner(db=lambda: db(), now_iso=lambda: now_iso())
# The runner's verbs, as the worker code below asks them of it.
set_job = job_runner.set_job
set_job_progress = job_runner.set_job_progress
schedule_upload_retry = job_runner.schedule_upload_retry
ensure_job_active = job_runner.ensure_job_active
ensure_job_claim_owned_in_transaction = job_runner.ensure_job_claim_owned_in_transaction


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def s3_download_transfer_config() -> TransferConfig:
    """TransferManager config for plain-vault Archive Version downloads."""
    runtime = settings
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
        interval_ms = int(settings.job_progress_min_interval_ms or 500)
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


def s3_client(*, preflight: bool = False, non_idempotent: bool = False,
              region: str | None = None):
    """The shared client of the S3 module, under the name the worker adapter exposes."""
    return s3_service.s3_client(preflight=preflight, non_idempotent=non_idempotent, region=region)


def validate_cloud_vault(vault: dict[str, Any], *, notify: bool = True) -> None:
    # The installation gate owns one warning, including for cleanup callers.
    # Provider failures here only suspend work; no transfer has started yet.
    try:
        state = cloud_preflight_gate.check(db, lambda: _validate_cloud_vault(vault))
    except psycopg.Error as exc:
        # Even an aborted gate-publication transaction is before the first
        # transfer. Suspend, do not replay a cloud call as a DB-unit retry.
        raise cloud_preflight_gate.CloudPreflightBlocked("S3 readiness status is temporarily unavailable; Job remains queued") from exc
    if state.get("probe_in_progress"):
        raise cloud_preflight_gate.CloudPreflightBusy(state["remediation"])
    if state["status"] != "ready":
        raise cloud_preflight_gate.CloudPreflightBlocked(state.get("remediation") or "S3 readiness check pending")


# Vaults whose bucket must answer for cloud work to run: a provisioning Vault's
# bucket is still being created and a Tombstone's is left to Vault Deletion.
_LIVE_BUCKETS_SQL = """
    SELECT DISTINCT t.bucket, t.region FROM vaults v JOIN vault_targets t ON t.id = v.target_id
    WHERE v.lifecycle IN ('active', 'reconnect_required', 'stopping')
    ORDER BY t.bucket, t.region
"""


def _cloud_targets(vault: dict[str, Any]) -> list[tuple[Any, str]]:
    """The Vault's own bucket first, then every other bucket a live Vault uses.

    Readiness is installation-wide (one gate), and since #735 every new Vault
    has its own bucket: probing only the caller's bucket would let the gate
    flip between the Vaults' answers. A catalog that cannot be read leaves the
    caller's bucket alone; the gate's own publication then fails anyway.
    """
    targets: list[tuple[Any, str]] = [
        (vault.get("s3_bucket"), str(vault.get("s3_region") or settings.aws_region))]
    try:
        with db() as connection:
            rows = connection.execute(_LIVE_BUCKETS_SQL).fetchall()
    except Exception:
        rows = []
    for row in rows:
        target = (row["bucket"], str(row["region"]))
        if target not in targets:
            targets.append(target)
    return targets


def _validate_cloud_vault(vault: dict[str, Any]) -> None:
    for bucket, region in _cloud_targets(vault):
        _validate_cloud_bucket(bucket, region)


def _validate_cloud_bucket(bucket: Any, region: str) -> None:
    if not bucket or is_placeholder(
        bucket,
        "BUCKET-NAME",
        "REPLACE-WITH-BUCKET-NAME",
    ):
        raise cloud_preflight_gate.ReadinessFailure("A Vault has no S3 bucket in its Vault Target")
    result = check_bucket_readiness(
        bucket,
        region=region,
        client=s3_client(preflight=True, region=region),
    )
    if not result.ok:
        key = failure_message_key(result)
        # Keyed failures store the English catalog text, so the readiness gate
        # can localize it for each viewer (cloud_preflight_gate.remediation_key).
        # Every message names the bucket: since #735 each Vault has its own.
        raise cloud_preflight_gate.ReadinessFailure(
            translate(key, locale="en", bucket=bucket) if key
            else f"S3 bucket {bucket} preflight failed: " + preflight_failure_message(result)
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
    excluded_folders: excluded_folder_rules.ExcludedFolders | None = None,
    verification_follows: bool = False,
    expected_files: int | None = None,
) -> tuple[int, str | None]:
    """Observe a root with short catalog-write transactions.

    Directory traversal and metadata reads happen before each bounded write
    batch.  The caller can defer the missing-copy transition until it has
    proved that the complete scan still owns the enrolled root. Excluded
    Folders are not entered (ADR-0044); pass the rules the caller's missing
    transition will use.

    ``verification_follows`` is the full scan's promise that every present
    regular Local Copy left without a fingerprint is hashed after the walk
    (``_fingerprint_scanned_local_copies``), which reports an unreadable file
    by its path. The walk then skips the open/read probe for those files and
    keeps it for an already fingerprinted file whose size and mtime did not
    change, and it reports its progress in the runtime status (#696).
    """
    vault_id = int(vault["id"])
    if excluded_folders is None:
        excluded_folders = excluded_folder_rules.load(connection, vault_id)
    prune = excluded_folders.covers_directory if excluded_folders else None
    pruned: set[str] = set()
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
        # The Vault row the batch upsert takes anyway, taken here in the same
        # mode and at the same point of the lock order so its wait is timed
        # apart from the write (#707). The touch inside the upsert is then free.
        waited = time.monotonic()
        if pending:
            lock_vaults_in_order(connection, [vault_id])
        written = time.monotonic()
        local_copies = LocalCopies(connection)
        # Issue #242: one Vault row-lock touch, one bulk identity lookup, and
        # one bulk upsert replace the per-file round-trips that pinned Postgres
        # CPU on large Vaults.  The batch is semantically identical to the
        # serialized per-file path (same upsert columns, digest preservation,
        # dirty ancestors).
        # Directory replacement can preserve size/mtime. A watcher subtree
        # invalidates real changes, not independently verified file echoes.
        local_copies.observe_local_copies_batch(
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
        finished = time.monotonic()
        traversal["batches"] += 1
        traversal["lock_wait"] += written - waited
        traversal["write"] += finished - written

    unreadable = False
    tree: PinnedTree | None = None

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

    def pruned_folder(entry: Path) -> None:
        pruned.add(entry.relative_to(root).as_posix())

    # The size and mtime of the listed folder's fingerprinted Local Copies,
    # looked up in bulk when the folder is listed: a file not in it is hashed
    # by the verification, so its readability probe is skipped (#696).
    fingerprinted: dict[str, tuple[int | None, int | None]] = {}

    def listed(directory: Path, names: list[str]) -> None:
        prefix = directory.relative_to(root).as_posix()
        paths = [name if prefix == "." else f"{prefix}/{name}" for name in names]
        for start in range(0, len(paths), _PROBE_LOOKUP_CHUNK):
            for row in connection.execute(
                """
                SELECT path, local_size, local_mtime_ns FROM files
                WHERE vault_id=%s AND path = ANY(%s::text[])
                  AND local_presence='present' AND local_file_type='regular'
                  AND local_sha256 IS NOT NULL
                """,
                (vault_id, paths[start:start + _PROBE_LOOKUP_CHUNK]),
            ).fetchall():
                fingerprinted[row["path"]] = (row["local_size"], row["local_mtime_ns"])

    # Seconds spent waiting for the Vault row and writing batches (#707); the
    # rest of the elapsed time is the walk itself.
    traversal = {
        "seen": 0, "started": time.monotonic(), "reported": 0.0,
        "batches": 0, "lock_wait": 0.0, "write": 0.0, "probe": 0.0,
    }

    def report_traversal(*, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - traversal["reported"] < _TRAVERSAL_PROGRESS_SECONDS:
            return
        traversal["reported"] = now
        elapsed = now - traversal["started"]
        _scan_progress(
            vault_id, scan_id,
            traversal_files_seen=traversal["seen"],
            traversal_files_per_minute=(
                round(traversal["seen"] * 60 / elapsed, 1) if elapsed > 0 else None
            ),
            traversal_files_expected=expected_files,
            traversal_lock_wait_seconds=round(traversal["lock_wait"], 1),
            traversal_write_seconds=round(traversal["write"], 1),
            traversal_probe_seconds=round(traversal["probe"], 1),
        )

    def observe(walk: PinnedTree) -> None:
        nonlocal tree
        tree = walk
        for entry, pinned_entry, listed_stat in walk.entries(
            on_error=failed_directory, on_unsupported_name=unsupported,
            prune=prune, on_pruned=pruned_folder,
            on_listing=listed if verification_follows else None, with_stat=True,
        ):
            if is_restore_temporary_name(entry.name):
                continue
            relative = entry.relative_to(root).as_posix()
            known = fingerprinted.pop(relative, None)
            if listed_stat is None or not stat.S_ISDIR(listed_stat.st_mode):
                traversal["seen"] += 1
                if verification_follows:
                    report_traversal()
            # An already fingerprinted file whose size and mtime did not
            # change is only opened, never read (#709): the open still refuses
            # a file made unreadable after it was protected, and the walk does
            # not wait for a data seek on a disk the uploads keep busy.
            unchanged = listed_stat is not None and known == (
                listed_stat.st_size, listed_stat.st_mtime_ns,
            )
            probe_started = time.monotonic()
            try:
                inspected = inspect_entry(
                    pinned_entry, info=listed_stat,
                    probe=not verification_follows or listed_stat is None or unchanged,
                    read=not (verification_follows and unchanged),
                )
                if inspected is None:
                    continue
                file_type, entry_stat = inspected
            except OSError as exc:
                failed(entry, exc)
                continue
            finally:
                traversal["probe"] += time.monotonic() - probe_started
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
            if shutdown_state.requested:
                _stop_walk_for_shutdown()
        walk.validate()
        flush()
        walk.validate()
        fingerprinted.clear()
        if verification_follows:
            report_traversal(force=True)
            elapsed = time.monotonic() - traversal["started"]
            _traversal_logger.info(
                "Vault %s: traversal saw %d files in %.1f s (%.0f files/min); "
                "%d batches, Vault row wait %.1f s, batch writes %.1f s, walk %.1f s "
                "(file checks %.1f s)",
                vault_id, traversal["seen"], elapsed,
                traversal["seen"] * 60 / elapsed if elapsed > 0 else 0.0,
                traversal["batches"], traversal["lock_wait"], traversal["write"],
                max(0.0, elapsed - traversal["lock_wait"] - traversal["write"]),
                traversal["probe"],
            )

    def _stop_walk_for_shutdown() -> None:
        # Process shutdown (#696, ADR-0045): commit the batch in progress when
        # its folder identity still holds, then stop. What is committed stays
        # as after a crash: no compensation, no missing transition.
        try:
            flush()
        except Exception:
            _scan_logger.info("Vault %s: batch in progress discarded at shutdown", vault_id)
        raise WorkerStopping("Local scan stopped for process shutdown")

    unsettled: list[str] = []
    root_check = _scan_root_check(vault) if _persisted_scan(vault) else None
    with PinnedTree(root, subtree=subtree, root_check=root_check) as tree:
        if _persisted_scan(vault) and _opened_scan_root_identity(tree.fd, vault) != vault.get("root_identity"):
            raise _ScanRootMismatch("Vault root changed before traversal")
        observe(tree)
        # A folder replaced or removed during the walk invalidates only its own
        # subtree (#686): its observations are compensated and it is walked
        # again, a bounded number of times. The rest of the walk stands.
        replaced = tree.replaced_paths()
        rewalks = 0
        while replaced:
            if scan_journal is None:
                raise TreeIdentityChanged(
                    f"Visited directory changed during traversal: {display_path(replaced[0])}"
                )
            for relative in replaced:
                _scan_logger.warning(
                    "Vault %s: folder %s was replaced or removed during the scan; "
                    "its observations are discarded and it is walked again",
                    vault_id, display_path(relative),
                )
                count -= _compensate_scan_subtree(connection, vault_id, scan_id, scan_journal, relative)
            if allow_chunk_commits:
                connection.commit()
            if rewalks >= SCAN_REPLACED_FOLDER_REWALKS:
                unsettled = replaced
                break
            rewalks += 1
            again: list[str] = []
            for relative in replaced:
                try:
                    with PinnedTree(root, subtree=root / relative, missing_ok=True,
                                    root_check=root_check) as walk:
                        observe(walk)
                        again.extend(walk.replaced_paths())
                except TreeIdentityChanged:
                    # The folder itself changed again: this walk is discarded
                    # (and compensated) as a whole, never half committed.
                    pending.clear()
                    invalidate_paths.clear()
                    again.append(relative)
            replaced = again
        for relative in unsettled:
            _scan_logger.warning(
                "Vault %s: folder %s kept changing during the scan; its protection "
                "is not confirmed until a later scan observes it",
                vault_id, display_path(relative),
            )
            report_problem(connection, event="folder_changed_during_scan", vault_id=vault_id, path=relative)
        if unsettled:
            if scan_journal is not None:
                scan_journal["incomplete"] = True
            if allow_chunk_commits:
                connection.commit()
        # An Excluded Folder removed from disk stays out of the plan while its
        # rule holds: its disappearance is not a coverage gain to scan for.
        pruned |= {
            path for path in watch_plan.unobserved(vault_id).excluded
            if excluded_folders.covers_directory(path)
        }
        found = watch_plan.UnobservedPaths(
            unreadable=frozenset(unreadable_all),
            unsupported=frozenset(unsupported_all),
            directories=frozenset(unreadable_directories | unsupported_directories | pruned),
            excluded=frozenset(pruned),
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
        if subtree is None:
            # A folder that kept changing is closed by the next whole walk that
            # observed it; one below a folder this walk could not enter is unknown.
            resolve_stale_path_problems(
                connection, event="folder_changed_during_scan", vault_id=vault_id,
                keep_paths={display_path(path) for path in unsettled},
                keep_prefixes={display_path(path) for path in unreadable_directories},
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
    incomplete = unreadable or unsupported_seen or bool(unsettled)
    if finalize_missing and not incomplete and safe_scan_result and (
        alias is None or source_layout.should_emit_local_copy_removals(alias)
    ):
        LocalCopies(connection).mark_unseen_local_copies_missing(
            vault_id=vault_id,
            seen_at=scan_id,
            observed_at=now_iso(),
            scan_started_at=scan_started_at,
            excluded_folders=excluded_folders,
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
    _excluded_folders: excluded_folder_rules.ExcludedFolders | None = None,
    _verification_follows: bool = False,
    _expected_files: int | None = None,
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
            excluded_folders=_excluded_folders,
            verification_follows=_verification_follows,
            expected_files=_expected_files,
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
            excluded_folders=_excluded_folders,
        )
    if completed_alias:
        source_layout.note_full_local_scan_completed(completed_alias)
    return count


def _check_directory_level(
    connection: Any,
    vault_id: int,
    root: Path,
    directory: Path,
    excluded: excluded_folder_rules.ExcludedFolders,
    *,
    with_files: bool = True,
) -> tuple[list[tuple[str, Path]], list[tuple[str, Path]], set[str]]:
    """List one directory one level deep against the catalog (#721).

    A directory event does not say what changed inside it, and on some
    sources (an Unraid ``fuse.shfs`` share) a new folder arrives only as a
    ``modified`` of itself or of its parent. Returns:

    - the child files the catalog does not hold as present with this size and
      mtime (with ``with_files``);
    - the child folders with no present catalogued file under them, to walk
      like an added folder;
    - the child Excluded Folders, never entered (ADR-0044).

    One ``scandir`` and one catalog query; symlinks are never followed, and
    restore temporary and uncataloguable names are left to the walks.
    """
    prefix = directory.relative_to(root).as_posix()
    files: dict[str, tuple[Path, os.stat_result]] = {}
    folders: dict[str, Path] = {}
    excluded_found: set[str] = set()
    try:
        with os.scandir(directory) as listing:
            children = list(listing)
    except OSError:
        # An unreadable or vanished folder: the walks and scans report it.
        return [], [], excluded_found
    for child in children:
        name = child.name
        if is_restore_temporary_name(name) or not is_storable_name(name):
            continue
        relative = name if prefix == "." else f"{prefix}/{name}"
        try:
            if child.is_dir(follow_symlinks=False):
                if excluded.covers_directory(relative):
                    excluded_found.add(relative)
                else:
                    folders[relative] = directory / name
            elif with_files and not excluded.contains_path(relative):
                files[relative] = (directory / name, child.stat(follow_symlinks=False))
        except OSError:
            continue
    if not files and not folders:
        return [], [], excluded_found
    known_files: dict[str, dict[str, Any]] = {}
    known_folders: set[str] = set()
    for row in connection.execute(
        """
        SELECT 'file' AS kind, path, local_presence, local_size, local_mtime_ns
        FROM files WHERE vault_id=%s AND path = ANY(%s::text[])
        UNION ALL
        SELECT 'folder', d.path, NULL, NULL, NULL
        FROM unnest(%s::text[]) AS d(path)
        WHERE EXISTS (
            SELECT 1 FROM files f
            WHERE f.vault_id=%s AND f.local_presence='present'
              AND f.path > (d.path || '/') COLLATE "C"
              AND f.path < (d.path || '0') COLLATE "C"
        )
        """,
        (vault_id, list(files), list(folders), vault_id),
    ).fetchall():
        if row["kind"] == "folder":
            known_folders.add(row["path"])
        else:
            known_files[row["path"]] = row
    changed_files = []
    for relative, (path, info) in files.items():
        row = known_files.get(relative)
        if row is None or row["local_presence"] != "present" or (
            row["local_size"], row["local_mtime_ns"]
        ) != (info.st_size, info.st_mtime_ns):
            changed_files.append((relative, path))
    unknown_folders = [
        (relative, path) for relative, path in folders.items() if relative not in known_folders
    ]
    return changed_files, unknown_folders, excluded_found


def _check_watched_directory_levels(
    vault: dict[str, Any],
    root: Path,
    directories: list[Path],
    excluded: excluded_folder_rules.ExcludedFolders,
    *,
    skip: set[Path],
) -> tuple[list[tuple[str, Path]], list[Path], set[Path]]:
    """The one-level checks of a watcher batch's changed directories (#721).

    Returns the unknown child folders to walk (parents first), the unknown or
    changed child files to observe like a file event (none of ``skip``, the
    paths with an event of their own), and those of the files that are echoes
    of FrostVault's own restores, observed like the walk observes them. The
    listings and lookups run in one short read transaction; the echo check,
    which may hash, stays outside every transaction.
    """
    vault_id = int(vault["id"])
    folders: dict[Path, str] = {}
    files: list[tuple[str, Path]] = []
    queued = set(skip)
    noted: set[str] = set()
    if not directories:
        return [], [], set()
    with db() as connection:
        for directory in dict.fromkeys(directories):
            relative = directory.relative_to(root).as_posix()
            if directory != root:
                if excluded.covers_directory(relative):
                    continue
                parent = directory.parent.resolve()
                if parent != root and root not in parent.parents:
                    continue
            child_files, child_folders, child_excluded = _check_directory_level(
                connection, vault_id, root, directory, excluded,
            )
            noted |= child_excluded
            for child_relative, child in child_folders:
                folders.setdefault(child, child_relative)
            for child_relative, child in child_files:
                if child not in queued:
                    queued.add(child)
                    files.append((child_relative, child))
    if noted:
        # Already noted when the scan found them; a new one stops being watched.
        watch_plan.note_excluded_directories(vault_id, noted)
    echoes = {
        child for relative, child in files
        if local_observation.consume(vault_id, relative, child)
    }
    new_folders = sorted(((relative, path) for path, relative in folders.items()), key=lambda item: str(item[1]))
    return new_folders, [child for _relative, child in files], echoes


def apply_filesystem_changes(
    vault: dict[str, Any], changes: set[tuple[Change, str]], *, wait: bool = True,
) -> int | None:
    """Serialize watcher updates with full scans for the same vault.

    With ``wait=False`` a busy Vault returns None at once and nothing is
    applied: the watcher keeps the batch and keeps consuming events (#690).
    """
    vault_id = int(vault["id"])
    lock = scan_lock_for_vault(vault_id)
    if not lock.acquire(blocking=wait, operation="watcher_changes"):
        return None
    try:
        journal: dict[str, Any] = {}
        event_id = now_iso()
        try:
            return _apply_filesystem_changes(vault, changes, event_id=event_id, journal=journal)
        except Exception:
            _rollback_local_scan_journal(vault_id, event_id, journal)
            raise
    finally:
        lock.release()


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
        excluded = excluded_folder_rules.load(connection, int(vault["id"]))
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
    # Directories this batch lists one level deep (#721), the root included.
    level_directories: list[Path] = []
    for value in {value for _, value in changes}:
        entry = Path(value)
        try:
            relative = entry.relative_to(root).as_posix()
        except ValueError:
            continue
        if relative == "." and ".." not in entry.parts:
            level_directories.append(root)
            continue
        if relative == "." or ".." in entry.parts or is_restore_temporary_name(entry.name):
            continue
        if excluded.contains_path(relative):
            # Inside an Excluded Folder: not observed, its rows keep their
            # last known state (ADR-0044).
            continue
        parent = entry.parent.resolve()
        if parent == root or root in parent.parents:
            # Echo validation can hash a large recovered/salvaged copy. Keep
            # that work outside every catalog write transaction.
            if not local_observation.consume(int(vault["id"]), relative, entry):
                paths.append(entry)
    level_directories += [
        entry for entry in paths
        if entry.is_dir() and not entry.is_symlink()
        and not any(added == entry or added in entry.parents for added in added_paths)
    ]
    # Every directory event (added ones are walked whole) lists that directory
    # one level deep: on some sources a new folder arrives only as a modified
    # parent, or as a modified folder whose content raised no event (#721).
    new_folders, new_files, echoes = _check_watched_directory_levels(
        vault, root, level_directories, excluded,
        skip={Path(value) for _, value in changes} | set(paths),
    )
    paths += new_files
    # Directory transactions commit in bounded batches. Process them before
    # the atomic single-file/deletion batch, never committing a partial direct
    # observation as an accidental side effect of a later subtree flush.
    paths.sort(key=lambda entry: (not (entry.is_dir() and not entry.is_symlink()), str(entry)))
    with db() as connection:
        local_copies = LocalCopies(connection)
        # A rename emits one directory event, not an event for each child.
        # Coalesce overlapping prefixes and stream bounded scan-write batches;
        # never finalize missing copies outside the affected subtree.
        for relative, entry in new_folders:
            # A folder the catalog does not know, found by listing a changed
            # directory: walked like an added folder (#721).
            if any(parent == entry or parent in entry.parents for parent in subtrees):
                continue
            walked = _scan_tree(
                connection, vault, event_id, root=root, subtree=entry,
                allow_chunk_commits=True, finalize_missing=False,
                scan_journal=journal, invalidate_content=True,
                excluded_folders=excluded,
            )[0]
            subtrees.append(entry)
            changed += walked
            _watcher_logger.info(
                "Vault %s: new folder found: %s, %d files",
                vault["id"], display_path(relative), walked,
            )
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
                if excluded.covers_directory(relative):
                    # A new Excluded Folder: not entered, and no longer watched.
                    watch_plan.note_excluded_directories(int(vault["id"]), {relative})
                    continue
                if entry not in added_paths:
                    # Metadata-only directory changes (also emitted by polling
                    # on child writes) are not content events for every child:
                    # only the children its one-level listing found unknown
                    # were queued above (#721).
                    continue
                changed += _scan_tree(
                    connection, vault, event_id, root=root, subtree=entry,
                    allow_chunk_commits=True, finalize_missing=False,
                    scan_journal=journal, invalidate_content=True,
                    excluded_folders=excluded,
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
                local_copies.observe_local_copy(
                    vault_id=vault["id"],
                    path=relative,
                    file_type=file_type,
                    size=entry_stat.st_size,
                    mtime_ns=entry_stat.st_mtime_ns,
                    seen_at=event_id,
                    observed_at=event_id,
                    # A listed child that is an echo of our own restore keeps
                    # its fingerprint, as in a subtree walk (#721).
                    invalidate_content=entry not in echoes,
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
                local_copies.mark_local_path_missing(
                    vault_id=vault["id"],
                    path=relative,
                    observed_at=now_iso(),
                    excluded_folders=excluded,
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
      attributed to a file by its path, promoted, or deleted. The only
      exception is the first Reconnect after a metadata restore, which reads
      and adopts newer cloud content (``_reconcile_restored_cloud``) and, in
      a Vault with a Retention Lock, adopts the superseded locked versions
      as Retained Versions (#749).

    Provider listings stay outside write transactions. Returns the number of
    known versions the listing showed.
    """
    validate_cloud_vault(vault)
    client = s3_client()
    paginator = client.get_paginator("list_object_versions")
    # The target's prefix: empty in a Vault Bucket, vaults/<uuid>/ in a shared one.
    prefix = vault_object_prefix(vault)
    kwargs: dict[str, Any] = {"Bucket": vault["s3_bucket"], "Prefix": prefix}
    listed: list[tuple[str, str, str]] = []
    latest: dict[str, dict[str, Any]] = {}
    markers = 0
    invalid_keys = False
    # The whole listing, kept only to adopt orphaned Retained Versions after a
    # metadata restore of a Vault with a Retention Lock (#749).
    adopt = reconnect and bool(vault.get("s3_retention_lock_days"))
    entries: list[dict[str, Any]] = []
    marker_keys: set[str] = set()
    for page in paginator.paginate(**kwargs):
        shutdown_state.checkpoint()
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
            if adopt:
                entries.append(item)
        markers += len(page.get("DeleteMarkers", []))
        if adopt:
            marker_keys.update(str(marker["Key"]) for marker in page.get("DeleteMarkers", []))
        if reconnect:
            for marker in page.get("DeleteMarkers", []):
                _note_latest_listed_version(latest, {**marker, "_delete_marker": True})
    if reconnect and invalid_keys:
        raise RuntimeError("Cloud listing is ambiguous; reconnect remains suspended")

    count = 0
    for offset in range(0, len(listed), CLOUD_SCAN_WRITE_BATCH_SIZE):
        shutdown_state.checkpoint()
        if not _cloud_scan_still_current(vault, scan_id):
            return count
        batch = listed[offset : offset + CLOUD_SCAN_WRITE_BATCH_SIZE]
        with db() as connection:
            # The listed storage class of an exact known version is
            # observation of that version, like its availability. Its Class
            # Entry moves to now only when the class changed (#731).
            from .services.minimum_storage import observed_since_sql

            since = observed_since_sql(
                old_class="av.storage_class", new_class="item.storage_class",
                old_since="av.storage_class_since", observed_at="now()",
                created_at="av.created_at", size="av.size", plan="v.storage_plan")
            known = connection.execute(
                f"""
                UPDATE archive_versions av
                SET storage_class = item.storage_class,
                    storage_class_since = {since}
                FROM unnest(%s::text[], %s::text[], %s::text[])
                     AS item(object_key, version_id, storage_class),
                     files f, vaults v
                WHERE av.object_key = item.object_key
                  AND av.provider_version_id = item.version_id
                  AND f.id = av.file_id AND f.vault_id = %s AND v.id = f.vault_id
                RETURNING av.id
                """,
                (
                    [entry[0] for entry in batch], [entry[1] for entry in batch],
                    [entry[2] for entry in batch], int(vault["id"]),
                ),
            ).fetchall()
            ArchiveVersions(connection).mark_archive_versions_listed(
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
    # A metadata restore is reconciled before the missing transition: a
    # version the cloud replaced after the backup is superseded, not lost.
    restoring = (
        _reconcile_restored_cloud(vault, scan_id, latest, prefix=prefix,
                                  entries=entries, marker_keys=marker_keys)
        if reconnect else False
    )
    # An unrecognised key or a truncated listing is not evidence that
    # prior catalog rows disappeared. Fail closed by retaining availability.
    if not invalid_keys and _cloud_scan_still_current(vault, scan_id):
        with db() as connection:
            newly_missing = ArchiveVersions(
                connection
            ).mark_unseen_archive_versions_missing(
                vault_id=vault["id"],
                scan_id=scan_id,
                scan_started_at=scan_id,
                superseded=not restoring,
            )
            from .services.catalog_incidents import remember_missing_archives

            remember_missing_archives(
                connection,
                newly_missing,
                reason="cloud_scan_unseen",
                observed_at=scan_id,
            )
    if reconnect:
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
    entries: list[dict[str, Any]] | None = None, marker_keys: set[str] | None = None,
) -> bool:
    """First Reconnect after a manual metadata restore: catch up the cloud.

    The restored catalog predates the backup. Afterwards a catalogued file may
    have been replaced in the cloud (its old Current possibly already cleaned
    up) and a new file may have been uploaded. Only for a Vault queued by
    ``record_metadata_restore``, for every key whose newest listed entry is a
    version the catalog does not know (never a Delete Marker):

    - a key with no Vault File at its path is adopted: the file is created
      with no Local Copy (#638), so the local rehash then decides Protected,
      Cloud-only or Cloud-only Conflict like for any known file;
    - that version becomes the file's replacement in progress and then
      Current only after its exact VersionId has been read and hashed
      (read-back proof), and the file is counted in the restore notice. Its
      ``created_at`` is its ``LastModified`` and its Class Entry is left
      unrecorded (estimated), never the reconnect's time (#743);
    - with a Retention Lock, a superseded version written after the backup
      that no table names any more becomes a Retained Version, deletable
      from its ``LastModified`` plus the bucket default days
      (``retention_lock.adopt_orphans``, database writes only, #749);
    - a superseded version the complete listing no longer shows was removed
      by the Current Snapshot cleanup after the backup: it is closed with
      that proof, never reported missing (#639).

    Nothing is deleted from S3. A GLACIER/DEEP_ARCHIVE version cannot be read
    before a manual S3 restore: the other keys are still reconciled, then the
    reconnect stays suspended. Returns whether a restore was reconciled.
    """
    from .services import recovery_kit
    from .services.catalog_incidents import resolve_archive_version_incidents
    from .services.upload_provenance import provider_identity

    vault_id = int(vault["id"])
    with db() as connection:
        if not recovery_kit.restore_reconciliation_pending(connection, vault_id):
            return False
        rows = connection.execute(
            "SELECT f.id, f.path, f.replacing_version_id FROM files f WHERE f.vault_id=%s",
            (vault_id,),
        ).fetchall()
        known = {
            (str(row["object_key"]), str(row["provider_version_id"])): (
                str(row["id"]) if row["unverified_replacement"] else None
            )
            for row in connection.execute(
                """
                SELECT av.id, av.object_key, av.provider_version_id,
                       (av.id = f.replacing_version_id
                        AND av.integrity = 'unverified') AS unverified_replacement
                FROM archive_versions av JOIN files f ON f.id = av.file_id
                WHERE f.vault_id=%s AND av.provider_version_id IS NOT NULL
                """,
                (vault_id,),
            ).fetchall()
        }
        # Superseded versions a Retention Lock holds back (#736) are garbage
        # waiting for their date, never content to adopt.
        retained = retention_lock_service.retained_versions(connection, vault_id)
    files = {str(row["path"]): row for row in rows}
    candidates: list[tuple[str, dict[str, Any], str | None]] = []
    for object_key, item in latest.items():
        if item.get("_delete_marker"):
            continue
        path = object_key_to_path(object_key, prefix)
        if path is None:
            continue
        identity = (object_key, str(item["VersionId"]))
        if identity in retained:
            continue
        # A replacement a failed earlier attempt left unverified is read
        # again; any other known version is already accounted for.
        if identity in known and known[identity] is None:
            continue
        candidates.append((path, item, known.get(identity)))
    client = s3_client()
    cold = 0
    for path, item, pending_version in candidates:
        if not _cloud_scan_still_current(vault, scan_id):
            raise RuntimeError("Reconnect scan superseded")
        storage_class = str(item.get("StorageClass") or "STANDARD")
        if storage_class in {"GLACIER", "DEEP_ARCHIVE"}:
            # Reading needs a cloud restore first; nothing is elected or
            # adopted without a proof, and the reconnect stays suspended.
            cold += 1
            continue
        version_id = str(item["VersionId"])
        size = int(item.get("Size") or 0)
        with db() as connection:
            versions = ArchiveVersions(connection)
            row = files.get(path)
            if row is None:
                file_id = versions.adopt_cloud_path(vault_id, path, at=scan_id)
                replacing = None
            else:
                file_id = str(row["id"])
                replacing = row["replacing_version_id"]
            if pending_version is not None and str(replacing) == pending_version:
                archive_version_id = pending_version
            else:
                if replacing is not None:
                    versions.abandon_replacement(
                        file_id, archive_version_id=str(replacing),
                    )
                # Written when S3 says, not now: its Retention Lock estimate
                # and Minimum Storage Duration start then (#743, #749).
                archive_version_id = versions.begin_replacement(
                    file_id, object_key=str(item["Key"]), provider_version_id=version_id,
                    size=size, storage_class=storage_class,
                    created_at=item.get("LastModified"), class_entry_recorded=False,
                )
            versions.record_verification_evidence(
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
            versions = ArchiveVersions(connection)
            if read != size:
                versions.abandon_replacement(
                    file_id, archive_version_id=archive_version_id,
                    mismatch=True, checked_at=now_iso(),
                )
                raise RuntimeError("Cloud version size differs from its listing")
            versions.promote_replacement(
                file_id, archive_version_id=archive_version_id,
                plaintext_sha256=digest.digest(), verified_at=now_iso(),
            )
            versions.mark_archive_versions_listed([archive_version_id], scan_id=scan_id)
            recovery_kit.remember_cloud_update(connection, file_id)
    if not _cloud_scan_still_current(vault, scan_id):
        raise RuntimeError("Reconnect scan superseded")
    with db() as connection:
        if entries:
            retention_lock_service.adopt_orphans(
                connection, vault_id=vault_id, days=vault.get("s3_retention_lock_days"),
                prefix=prefix, versions=entries, marker_keys=marker_keys or set())
        closed = ArchiveVersions(connection).close_absent_superseded_versions(
            vault_id=vault_id, scan_id=scan_id, provider=provider_identity(client),
            source_root=str(vault["source_root"]), root_identity=vault.get("root_identity"),
        )
        resolve_archive_version_incidents(connection, closed)
    if cold:
        raise RuntimeError(
            "Cloud versions need an S3 restore before they can be read; "
            "reconnect remains suspended"
        )
    return True


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
    version = vault.get("root_identity_version")
    if not vault.get("root_identity"):
        raise RuntimeError("Vault root identity is ambiguous; local scan blocked")
    if version != vault_relocation.ROOT_IDENTITY_VERSION:
        # A retired (v1/v2) identity the startup upgrade could not carry over:
        # the folder it names is not provably this one (#787).
        if version in vault_relocation.RETIRED_ROOT_IDENTITY_VERSIONS:
            raise RuntimeError("Vault root identity mismatch; local scan blocked")
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


def _scan_root_check(vault: dict[str, Any]) -> Callable[[int], bool] | None:
    """The in-scan root fence of a Vault whose Root Identity is not its inode.

    On FUSE (ADR-0055) the root's inode is a union branch's and a mover run
    during the scan may change it: the fence then asks whether the pinned
    descriptor is still the directory at the Vault path with the enrolled
    identity, instead of comparing inodes. Folders below the root keep the
    inode fence: a changed one is a replaced subtree, walked again (#686).
    Elsewhere the default inode fence stays.
    """
    try:
        on_fuse = vault_relocation.root_is_on_fuse(vault["source_root"])
    except vault_relocation.VaultRelocationError:
        return None
    if not on_fuse:
        return None
    expected = vault.get("root_identity")
    return lambda fd: _opened_scan_root_identity(fd, vault) == expected


def _opened_scan_root_identity(fd: int, vault: dict[str, Any]) -> str | None:
    """The Root Identity of the pinned root descriptor; None when it is not the root."""
    try:
        return vault_relocation.opened_root_identity(fd, vault["source_root"])
    except vault_relocation.VaultRelocationError:
        return None


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
        try:
            opened = vault_relocation.opened_root_identity(fd, vault["source_root"])
        except vault_relocation.VaultRelocationError as exc:
            raise _ScanRootMismatch("Vault root changed while it was being pinned") from exc
        if opened != vault.get("root_identity"):
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
        rows = LocalCopies(connection).local_copies_to_fingerprint(vault_id)
        excluded = excluded_folder_rules.load(connection, vault_id)
    # Files in an Excluded Folder keep their last known state (ADR-0044).
    return [row for row in rows if not excluded.contains_path(row["path"])] if excluded else rows


def _hash_local_copy(
    root: Path, row: dict[str, Any], *, device: str | None = None,
    cancelled: threading.Event | None = None,
    on_read: Callable[[int], None] | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Fingerprint one Local Copy; runs on a hashing thread, writes nothing.

    The outcome is applied by the scan thread: ``hashed`` with its update,
    ``absent`` (no longer a regular file), ``unreadable`` (an OSError, named by
    its path), ``unattributed`` (changed while read, or not a regular file) or
    ``fingerprinted`` (an upload's whole read of this very observation already
    published its fingerprint, #766: nothing is read again).
    The read holds a slot of the file's Source Device (#713), shared with
    uploads; waiting for it stops at shutdown or when the scan is closed.
    """
    def waiting() -> None:
        if shutdown_state.requested or (cancelled is not None and cancelled.is_set()):
            raise WorkerStopping("Scan read cancelled while waiting for its device")

    def chunk(size: int) -> None:
        source_devices.note_read(size)
        if on_read is not None:
            on_read(size)

    fingerprinted = source_devices.upload_fingerprints()
    try:
        if fingerprinted.claim(row):
            return "fingerprinted", None
        local_path = safe_local_path(str(root), row["path"])
        if not local_path.is_file():
            return "absent", None
        # The same read records the multipart part digests of a file of at
        # least 8 MiB, so its upload needs no reading pass of its own (#703).
        parts = PartDigests()
        with source_devices.slots().reading(
            device or source_devices.device_of(local_path), waiting,
        ):
            # An upload may have read it while this read waited for its slot.
            if fingerprinted.claim(row):
                return "fingerprinted", None
            digest, stat_result = hash_stable_regular_file(
                local_path, should_stop=lambda: shutdown_state.requested, parts=parts,
                on_chunk=chunk,
            )
    except WorkerStopping:
        raise
    except FileNotFoundError:
        # Gone since the listing: the watcher, a Local Copy cleanup claim or
        # the next scan observes it (ADR-0043), it is not unreadable.
        return "absent", None
    except OSError:
        return "unreadable", None
    except (ValueError, RuntimeError):
        return "unattributed", None
    return "hashed", {
        "vault_file_id": row["vault_file_id"],
        "path": row["path"],
        "generation": row["generation"],
        "old_size": row["size"],
        "old_mtime_ns": row["mtime_ns"],
        "size": stat_result.st_size,
        "mtime_ns": stat_result.st_mtime_ns,
        "digest": digest,
        "parts": parts.record(stat_result.st_size),
        "observed_at": now_iso(),
    }


def _iter_local_fingerprints(
    vault: dict[str, Any],
    root: Path,
    rows: list[dict[str, Any]],
    *,
    scan_journal: dict[str, Any] | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
    on_read: Callable[[int], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Hash Local Copies ``SCAN_HASH_CONCURRENCY`` at a time (#691).

    Results are yielded as they complete, so the caller publishes them in
    batches while the rest is still being read. Journal and Problem updates
    stay on the calling thread. Closing the iterator (a superseded scan)
    cancels the reads not yet started and waits for the running ones.
    Reads are fed round-robin across Source Devices, at most
    ``SOURCE_DEVICE_READ_CONCURRENCY`` of this scan per device (#713), so a
    busy disk does not starve the others.
    """
    concurrency = max(1, int(settings.scan_hash_concurrency))

    def outcome(row: dict[str, Any], kind: str, update: dict[str, Any] | None):
        if on_result is not None:
            on_result(row)
        if kind == "unreadable":
            if scan_journal is not None:
                scan_journal["incomplete"] = True
                scan_journal.setdefault("unreadable_paths", set()).add(display_path(row["path"]))
                scan_journal.setdefault("unreadable_files", set()).add(row["path"])
            with db() as connection:
                report_problem(
                    connection, event="file_unreadable", vault_id=int(vault["id"]),
                    path=row["path"],
                )
        elif kind == "hashed" and update is not None:
            # Its queued upload reads it next, from the page cache (#713).
            source_devices.hot_files().add(update["vault_file_id"])
        elif kind == "unattributed" and scan_journal is not None:
            scan_journal["incomplete"] = True
            # Not attributable to a path: no problem may be closed on this walk.
            scan_journal["unattributed"] = True
        return update

    if concurrency == 1 or len(rows) <= 1:
        for row in rows:
            update = outcome(row, *_hash_local_copy(
                root, row, **({"on_read": on_read} if on_read is not None else {})))
            if update is not None:
                yield update
        return

    # A bounded window, not one future per row: a 139,000-file adoption keeps
    # only a few reads queued, and cancelling it leaves nothing behind.
    window = concurrency * 2
    pending: dict[Future, tuple[dict[str, Any], str]] = {}
    in_flight: Counter[str] = Counter()
    slots = source_devices.slots()

    feed = source_devices.interleave_by_device(
        rows, device_of=lambda row: source_devices.device_of_entry(root, row["path"]),
        busy=lambda device: in_flight[device], limit=slots.limit,
    )
    cancelled = threading.Event()
    pool = ThreadPoolExecutor(
        max_workers=concurrency, thread_name_prefix=f"scan-hash-{int(vault['id'])}",
    )
    try:
        while True:
            # No new read starts once the process is stopping (#696).
            while not shutdown_state.requested and len(pending) < window:
                step = next(feed, False)
                if not step:
                    # Exhausted (False) or every device at its limit (None).
                    break
                device, row = step
                pending[pool.submit(
                    _hash_local_copy, root, row, device=device, cancelled=cancelled,
                    **({"on_read": on_read} if on_read is not None else {}),
                )] = (row, device)
                in_flight[device] += 1
            if not pending:
                return
            done, _ = wait_for_futures(pending, return_when=FIRST_COMPLETED)
            for future in done:
                row, device = pending.pop(future)
                in_flight[device] -= 1
                update = outcome(row, *future.result())
                if update is not None:
                    yield update
    finally:
        # Reads still waiting for a device slot give up; running ones finish.
        cancelled.set()
        pool.shutdown(wait=True, cancel_futures=True)


def _collect_local_fingerprint_updates(
    vault: dict[str, Any], root: Path, *, scan_journal: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Hash Local Copies without holding a database write transaction."""
    rows = _unfingerprinted_local_rows(int(vault["id"]))
    return list(_iter_local_fingerprints(vault, root, rows, scan_journal=scan_journal))


def _scan_progress(vault_id: int, scan_id: str, **fields: Any) -> None:
    """Record the running scan's phase/progress in the runtime status only."""
    with status_lock:
        status = runtime_status.get(int(vault_id))
        if status is not None and status.get("scan_id") == scan_id:
            status.update(fields)


def _fingerprint_scanned_local_copies(
    vault: dict[str, Any], root: Path, scan_id: str, *, scan_journal: dict[str, Any],
) -> int:
    """Verify the scan's unfingerprinted Local Copies, publishing as it goes.

    Each committed batch is durable progress: a digest is published by
    compare-and-swap on the generation read before hashing, so a watcher
    replacement or a compensated subtree in the meantime still wins.
    Returns how many digests were computed.
    """
    vault_id = int(vault["id"])
    rows = _unfingerprinted_local_rows(vault_id)
    done = {"files": 0, "bytes": 0}
    _scan_progress(
        vault_id, scan_id, scan_phase="verification",
        verify_files_total=len(rows), verify_files_done=0,
        verify_bytes_total=sum(int(row.get("size") or 0) for row in rows),
        verify_bytes_done=0, verify_bytes_read=0,
    )

    def read(size: int) -> None:
        # Bytes read so far, partial files included, from the hashing
        # threads: a large file read for minutes still moves (#759).
        with status_lock:
            status = runtime_status.get(vault_id)
            if status is not None and status.get("scan_id") == scan_id:
                status["verify_bytes_read"] = int(status.get("verify_bytes_read") or 0) + size

    def advance(row: dict[str, Any]) -> None:
        done["files"] += 1
        done["bytes"] += int(row.get("size") or 0)
        # The pace is measured from these bytes by the throughput sampler (#755).
        _scan_progress(
            vault_id, scan_id,
            verify_files_done=done["files"], verify_bytes_done=done["bytes"],
        )

    hashed = 0
    batch: list[dict[str, Any]] = []
    stopping = False
    # At process shutdown (#696) the reads not started are cancelled and the
    # running ones abort; closing the iterator waits for them. The digests
    # already hashed are published like any batch, then the scan stops.
    with closing(_iter_local_fingerprints(
        vault, root, rows, scan_journal=scan_journal, on_result=advance, on_read=read,
    )) as results:
        try:
            for update in results:
                hashed += 1
                batch.append(update)
                if len(batch) >= LOCAL_SCAN_WRITE_BATCH_SIZE:
                    _apply_local_fingerprint_updates(vault, scan_id, batch)
                    batch = []
                if shutdown_state.requested:
                    stopping = True
                    break
        except WorkerStopping:
            stopping = True
    if batch:
        _apply_local_fingerprint_updates(vault, scan_id, batch)
    if stopping or shutdown_state.requested:
        raise WorkerStopping("Local scan stopped for process shutdown")
    return hashed


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
            local_copies = LocalCopies(connection)
            for update in batch:
                # A watcher may have replaced the file after hashing, or the
                # file changed while it was hashed: the generation and stat
                # compare-and-swap leave the digest unpublished; the next
                # observation hashes it again.
                if (update["size"], update["mtime_ns"]) != (
                    update["old_size"], update["old_mtime_ns"],
                ):
                    continue
                local_copies.publish_local_fingerprint(
                    str(update["vault_file_id"]),
                    generation=int(update["generation"]),
                    plaintext_sha256=update["digest"],
                    size=update["size"],
                    mtime_ns=update["mtime_ns"],
                    parts=update.get("parts"),
                )
            if persisted:
                _current_verified_scan_row(connection, vault)


# The Local Copy columns a scan journal saves before observing a path, so a
# scan that loses its proof can restore them (#593).
_JOURNAL_COLUMNS_SQL = """
    id, local_presence, local_file_type, local_size, local_mtime_ns,
    local_sha256, local_part_size, local_part_sha256, matched_version_id,
    held_version_id, local_seen_at,
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
            _restore_journal_entries(connection, vault_id, scan_id, entries)
    except Exception:
        # Compensation is best effort. The scan is still reported as blocked,
        # and no missing transition or relocation completion can commit.
        return


def _compensate_scan_subtree(
    connection: Any, vault_id: int, scan_id: str, journal: dict[str, Any], relative: str,
) -> int:
    """Undo this scan's observations at and below one replaced folder (#686).

    The entries leave the journal, so a later walk of the folder records its
    snapshot again and a later whole rollback does not repeat the restore.
    Returns how many observations were undone.
    """
    entries = journal.get("entries") or {}
    prefix = relative + "/"
    taken = {
        path: entries.pop(path)
        for path in [path for path in entries if path == relative or path.startswith(prefix)]
    }
    _restore_journal_entries(connection, vault_id, scan_id, taken)
    return len(taken)


def _restore_journal_entries(
    connection: Any, vault_id: int, scan_id: str, entries: Mapping[str, Any],
) -> None:
    """Restore the saved Local Copy columns of the given journal entries."""
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
            if not deleted:
                # Kept because something attached to it, such as an upload
                # admitted while the scan verified (#689): the withdrawn
                # observation becomes a new generation, so that Job is
                # cancelled at its claim and no hash on it can publish.
                deleted = connection.execute(
                    """
                    UPDATE files SET generation=generation + 1
                    WHERE vault_id=%s AND path=%s AND local_seen_at=%s
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
                local_mtime_ns=%s, local_sha256=%s, local_part_size=%s,
                local_part_sha256=%s, matched_version_id=%s,
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
                snapshot.get("local_part_size"),
                snapshot.get("local_part_sha256"),
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


def _verified_local_scan(
    vault: dict[str, Any], scan_id: str, *, scan_lock: Any | None = None,
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
            # The rules the walk prunes with also keep the missing transition
            # out of those folders, even if the policy changes meanwhile.
            excluded_folders = excluded_folder_rules.load(connection, int(vault["id"]))
            # A time estimate needs the Local Copies a previous scan found;
            # a first adoption has none (#696).
            expected = int(connection.execute(
                "SELECT count(*) AS n FROM files WHERE vault_id=%s"
                " AND local_presence IN ('present', 'unsupported')",
                (int(vault["id"]),),
            ).fetchone()["n"])
            count = scan_tree(
                vault,
                scan_id,
                _connection=connection,
                _root=pinned_root,
                _finalize_missing=False,
                _scan_journal=scan_journal,
                _excluded_folders=excluded_folders,
                _verification_follows=True,
                _expected_files=expected or None,
            )
            _current_verified_scan_row(connection, vault)

        # Hashing happens with no write transaction open. Publishing uses
        # compare-and-swap predicates so watcher replacements win safely.
        # The verification runs without the Vault scan lock (#689, ADR-0045):
        # watcher batches and automatic admission of this Vault go on
        # meanwhile. Digests publish by compare-and-swap, so their newer
        # observations win; the finalization below takes the lock again and
        # re-checks the enrolled root and the scan generation first.
        if scan_lock is not None:
            scan_lock.release()
        try:
            hashed = _fingerprint_scanned_local_copies(
                vault, pinned_root, scan_id, scan_journal=scan_journal,
            )
        finally:
            if scan_lock is not None:
                scan_lock.acquire(operation="full_scan")
        # A shutdown during hashing stops before the finalization (#696).
        shutdown_state.checkpoint()
        # The walk skipped the readability probe of the files just hashed
        # (#696). One the hashing could not read is not observed by this scan,
        # as when the probe failed: its observation is undone (compare-and-swap,
        # a newer watcher observation wins) and it is a gap the watcher re-checks.
        unreadable_files = sorted(scan_journal.get("unreadable_files", ()))
        if unreadable_files:
            with db() as connection:
                for relative in unreadable_files:
                    count -= _compensate_scan_subtree(
                        connection, int(vault["id"]), scan_id, scan_journal, relative,
                    )
            watch_plan.note_unreadable_files(int(vault["id"]), unreadable_files)
        _scan_progress(int(vault["id"]), scan_id, scan_phase="finalization")
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
            rename_summary["hashed"] = hashed
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
                LocalCopies(connection).mark_unseen_local_copies_missing(
                    vault_id=int(vault["id"]),
                    seen_at=scan_id,
                    observed_at=now_iso(),
                    scan_started_at=scan_id,
                    excluded_folders=excluded_folders,
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


class _WorkerHealth:
    """Production Adapter of ``periodic_workers.WorkerHealth`` (#795).

    Stateless and built once: each method resolves the storage function when
    it is called, so a patched ``app.storage.X`` is the one that runs.
    """

    __slots__ = ()

    def report_error(self, worker: str, exc: BaseException) -> None:
        _report_worker_error(worker, exc)

    def note_ok(self, worker: str) -> None:
        _note_worker_ok(worker)


worker_health = _WorkerHealth()


def run_periodic_worker(worker: periodic_workers.PeriodicWorker) -> None:
    """One pass of a declared periodic worker, on the calling thread."""
    periodic_workers.run_once(worker, worker_health)


# A watcher batch is a complete DB unit plus read-only filesystem inspection.
# A PostgreSQL deadlock/serialization victim rolled back everything, the
# scan journal is compensated by apply_filesystem_changes, and the pending
# change set is idempotent, so replaying the same batch is safe.
WATCHER_APPLY_MAX_ATTEMPTS = 5


def _apply_watcher_changes(
    vault: dict[str, Any], changes: set[tuple[Change, str]], *, wait: bool = True,
) -> int | None:
    """Apply one watcher batch, replaying it when PostgreSQL chose it as victim.

    None means the Vault was busy (``wait=False``) and nothing was applied.
    """
    for attempt in range(WATCHER_APPLY_MAX_ATTEMPTS):
        try:
            return apply_filesystem_changes(vault, changes, wait=wait)
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
                from .services.scan_causes import source_error_cause
                cause = source_error_cause(error) if component == "source" and not ok else None
                notification_service.set_problem(
                    connection, key=f"blocked:{vault_id}:{component}",
                    event="protection_blocked", vault_id=int(vault_id), active=not ok,
                    params={"component": component, **(
                        {"cause_key": f"notification.protection_blocked.cause.{cause}"} if cause else {})},
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
        status[f"last_error_{component}_at"] = None if ok else stamp
        if ok:
            status[f"last_success_{component}"] = stamp
            if verified:
                status["last_verified_at"] = stamp
        if component == "audit" and extra is not None and "healthy" in extra:
            status["last_audit_healthy"] = bool(extra.get("healthy"))


# Verification progress of the running scan (#691), runtime status only.
_NO_VERIFICATION_PROGRESS = {
    "verify_files_total": None, "verify_files_done": None,
    "verify_bytes_total": None, "verify_bytes_done": None, "verify_bytes_read": None,
    # Traversal progress of the running scan (#696).
    "traversal_files_seen": None, "traversal_files_per_minute": None,
    "traversal_files_expected": None,
    "traversal_lock_wait_seconds": None, "traversal_write_seconds": None,
    "traversal_probe_seconds": None,
}


def scan_vault(vault: dict[str, Any]) -> dict[str, int]:
    """Run a full scan now, or return ``already_running`` when the Vault is busy.

    A scan that takes the Vault also takes its queued request, if any
    (:func:`queue_scan`), and resolves it with its own result (#759): the
    request is satisfied, no second scan follows.
    """
    vault_id = int(vault["id"])
    taken: list[Future[dict[str, int]]] = []

    def take() -> None:
        pending = _take_queued_scan(vault_id)
        if pending is not None:
            taken.append(pending)

    try:
        result = _scan_vault(vault, _on_start=take)
    except BaseException as exc:
        for pending in taken:
            _settle_queued_scan(pending, exception=exc)
        raise
    for pending in taken:
        _settle_queued_scan(pending, result=result)
    return result


def _scan_vault(vault: dict[str, Any], *,
                _on_start: Callable[[], None] | None = None) -> dict[str, int]:
    vault_id = int(vault["id"])
    lock = scan_lock_for_vault(vault_id)
    if not lock.acquire(blocking=False, operation="full_scan"):
        return {"already_running": 1}

    with status_lock:
        status = runtime_status.setdefault(
            vault_id, {"scanning": False, "last_scan": None, "last_error": None}
        )
        # A scan verifying fingerprints runs without the lock (#689).
        running = bool(status.get("scanning"))
    if running:
        lock.release()
        return {"already_running": 1}
    try:
        if _on_start is not None:
            _on_start()
        # The relocation route holds this same lock while it publishes the
        # recovery state. A direct caller can still observe the short runtime
        # handoff gate, so do not turn that into a scan while the DB row is
        # still ready. A persisted scan_required row is intentionally allowed:
        # that scan is the recovery operation which must clear the suspension.
        vault = _current_scan_vault(vault)
        # A new Vault's bucket is not ready yet (#735): nothing to scan for.
        if vault.get("lifecycle") == "provisioning":
            return {"suspended": 1}
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
            status.update(
                scanning=True, last_error=None, scan_id=scan_id,
                scan_phase="traversal", **_NO_VERIFICATION_PROGRESS,
            )
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
            except WorkerStopping:
                return {"stopped": 1}
            except cloud_preflight_gate.CloudPreflightBusy:
                # Not an S3 failure (#777): reconnect waits, health is untouched.
                return {"cloud_deferred": 1, "reconnect_suspended": 1}
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
                    vault, scan_id, scan_lock=lock,
                )
                result["local"] = count
                if not complete:
                    result["local_incomplete"] = 1
                result.update(
                    {f"rename_{key}": value for key, value in rename_summary.items()}
                )
            except WorkerStopping:
                # Process shutdown (#696, ADR-0045): not a failure. The batches
                # already committed stay as after a crash, with no journal
                # compensation, no health or Problem; the next start rescans.
                _active_scan_journals.pop((int(vault["id"]), scan_id), None)
                return {"stopped": 1}
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
                           SOURCE_SCAN_INCOMPLETE),
                    notify_problem=confirmed,
                )
        else:
            result["local_skipped"] = 1
            result["local"] = 0
            _record_scan_component_health(
                vault_id, "source", ok=False,
                error="Protection blocked: Source Volume unavailable",
            )
        if shutdown_state.requested:
            # The local scan completed; the cloud and policy phases wait for
            # the scan of the next start (#696).
            return {**result, "stopped": 1}
        if local_scan_identity_valid:
            try:
                if not reconnect:
                    result["cloud"] = scan_cloud(vault, scan_id, report=cloud_report)
            except WorkerStopping:
                return {**result, "stopped": 1}
            except cloud_preflight_gate.CloudPreflightBusy:
                # The readiness probe is running in another process (#777): no
                # S3 failure, so the last recorded cloud health stays as it was.
                result["cloud_deferred"] = 1
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
                        s3_client(region=vault.get("s3_region")),
                        bucket=vault["s3_bucket"],
                    )
                    local_access = source_layout.vault_local_access(vault["source_root"])
                    if local_access.local_operations_allowed:
                        result["auto_local_cleanups"] = queue_auto_local_cleanups(
                            connection,
                            vault_id=int(vault["id"]),
                        )
            except cloud_preflight_gate.CloudPreflightBusy:
                result["lifecycle_rules_deferred"] = 1
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
            status.update(scanning=False, scan_phase=None, **_NO_VERIFICATION_PROGRESS)
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
    renames = FileRenames(connection)
    local_copies = LocalCopies(connection)
    from .services.catalog_incidents import settle_cloud_only_copies
    if hash_missing:
        for row in local_copies.local_copies_to_fingerprint(int(vault["id"])):
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
            if local_copies.publish_local_fingerprint(
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
    candidates = renames.list_rename_candidates(vault_id=vault["id"])
    changed_at = now_iso()
    for candidate in candidates:
        renames.confirm_file_rename(
            vault_file_id=candidate["missing_vault_file_id"],
            new_path=candidate["new_path"],
            changed_at=changed_at,
            vault_id=vault["id"],
        )
        confirmed_paths.append(candidate["new_path"])
        summary["confirmed"] += 1

    for path in confirmed_paths:
        # A rename the Owner made on disk: system work, never an Owner
        # operation in priority, audit or Activity (#629).
        job_ids, _total, eligible = JobAdmission(connection).queue_jobs(
            vault_id=vault["id"],
            path=path,
            action="rename",
            requested_at=now_iso(),
            group_id=uuid.uuid4().hex,
            is_directory=False,
            origin="automatic",
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


def _identify_new_local_copies(
    vault: dict[str, Any], root: Path, *, now: datetime,
) -> dict[str, int]:
    """Decide stable new Local Copies by digest before automatic upload (#621).

    The watcher sees additions and deletions, never a move. Two kinds of new
    content are hashed (outside any write transaction) as soon as they are
    stable: content back at a cloud-only path, which becomes the Local Copy
    again when identical and a Cloud-only Conflict when different (ADR-0016),
    and a new Local Copy with the size of one that just went missing, which
    keeps that Vault File's identity when it is its unique equal-digest match
    (ADR-0001). Automatic admission skips the latter until this decided it, so
    an upload never races ahead of the rename.
    """
    from .services.catalog_shared import VaultFileNotFound
    from .services.catalog_incidents import settle_cloud_only_copies

    vault_id = int(vault["id"])
    summary = {"hashed": 0, "confirmed": 0, "queued": 0}
    with db() as connection:
        policy = get_policy(connection, vault_id)
        rows = LocalCopies(connection).local_copies_to_identify(vault_id)
    updates: list[dict[str, Any]] = []
    for row in rows:
        if row["mtime_ns"] is None or not file_is_stable(
            mtime_ns=int(row["mtime_ns"]), now=now,
            stability_seconds=policy.stability_seconds,
            content_observed_at=(row["content_observed_at"].isoformat()
                                 if row["content_observed_at"] else None),
        ):
            continue
        try:
            local_path = safe_local_path(str(root), row["path"])
            if not local_path.is_file():
                continue
            digest, stat_result = hash_stable_regular_file(local_path)
        except OSError:
            with db() as connection:
                report_problem(
                    connection, event="file_unreadable", vault_id=vault_id,
                    path=row["path"],
                )
            continue
        except (ValueError, RuntimeError):
            continue
        if (stat_result.st_size, stat_result.st_mtime_ns) != (row["size"], row["mtime_ns"]):
            # Changed since observed: the watcher reports it again.
            continue
        updates.append({**row, "digest": digest})
    if not updates:
        return summary
    with db() as connection:
        if "root_identity" in vault:
            _current_verified_scan_row(connection, vault)
        renames = FileRenames(connection)
        local_copies = LocalCopies(connection)
        published: list[dict[str, Any]] = []
        for update in updates:
            if local_copies.publish_local_fingerprint(
                update["vault_file_id"], generation=update["generation"],
                plaintext_sha256=update["digest"], size=update["size"],
                mtime_ns=update["mtime_ns"],
            ):
                published.append(update)
        summary["hashed"] = len(published)
        for update in published:
            if update["held"]:
                # Identical content was released by its fingerprint; different
                # content opens the Owner's Cloud-only Conflict now.
                settle_cloud_only_copies(
                    connection, vault_id=vault_id, path=update["path"],
                    observed_at=now_iso(),
                )
        new_ids = {update["vault_file_id"] for update in published if not update["held"]}
        renamed: list[str] = []
        changed_at = now_iso()
        for digest in sorted({update["digest"] for update in published if not update["held"]}):
            # The same unique, in-window, history-free match a scan accepts.
            for candidate in renames.list_rename_candidates(vault_id, digest=digest):
                if candidate["new_vault_file_id"] not in new_ids:
                    continue
                try:
                    renames.confirm_file_rename(
                        vault_file_id=candidate["missing_vault_file_id"],
                        new_path=candidate["new_path"],
                        changed_at=changed_at,
                        vault_id=vault_id,
                    )
                except VaultFileNotFound:
                    continue
                renamed.append(candidate["new_path"])
        summary["confirmed"] = len(renamed)
        for path in renamed:
            job_ids, _total, _eligible = JobAdmission(connection).queue_jobs(
                vault_id=vault_id,
                path=path,
                action="rename",
                requested_at=now_iso(),
                group_id=uuid.uuid4().hex,
                is_directory=False,
                origin="automatic",
            )
            summary["queued"] += len(job_ids)
        if "root_identity" in vault:
            _current_verified_scan_row(connection, vault)
    return summary


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


def cleanup_abandoned_restore_files() -> None:
    """Retire catalog ghosts of recovery residues before workers are started.

    Catalog-only and quick. The filesystem sweep of the residues themselves
    walks every Vault tree: ``start_restore_residue_sweep`` runs it off the
    startup path (#777).
    """
    with db() as connection:
        vaults = connection.execute(
            "SELECT * FROM vaults WHERE lifecycle='active'"
        ).fetchall()
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
            continue
        with db() as connection:
            retire_absent_restore_ghosts(connection, int(vault["id"]), root, now_iso())


# Set while the startup residue sweep walks the Vault trees. Recover Jobs, the
# only writers of restore temporaries, are not dispatched meanwhile.
_restore_residue_sweep_running = threading.Event()


def start_restore_residue_sweep() -> None:
    """Sweep recovery residues on a thread of its own (#777).

    The walk of every Vault tree took ~100 s on Unraid, and run inside the
    application startup it kept the HTTP server, readiness included, from
    answering. Set before the thread starts, so no Recover Job can be
    dispatched before the sweep has finished.
    """
    _restore_residue_sweep_running.set()
    threading.Thread(target=_sweep_restore_residues, name="restore-residue-sweep",
                     daemon=True).start()


def _sweep_restore_residues() -> int:
    """Remove application-owned recovery residues of every active Vault."""
    removed = 0
    try:
        with db() as connection:
            vaults = connection.execute(
                "SELECT * FROM vaults WHERE lifecycle='active'"
            ).fetchall()
        for vault in vaults:
            access = source_layout.vault_local_access(vault["source_root"])
            if not access.local_operations_allowed:
                continue
            root = Path(vault["source_root"]).resolve()
            if not root.is_dir():
                continue
            try:
                for entry in root.rglob("*"):
                    if shutdown_state.requested:
                        return removed
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
    except Exception as exc:
        _report_worker_error("restore_residue_sweep", exc)
    else:
        _note_worker_ok("restore_residue_sweep")
    finally:
        _restore_residue_sweep_running.clear()
    return removed


def _record_upload_reconciliation_block(job: dict[str, Any], reason: str) -> None:
    from .services import upload_provenance as provenance
    if reason in provenance.WAIT_REASONS:
        return  # Self-resolving: the Job waits queued, no durable block or notification.
    if reason in provenance.TERMINAL_JOB_REASONS:
        with db() as connection:
            row = connection.execute("SELECT status FROM jobs WHERE id=%s", (job["id"],)).fetchone()
        if row and row["status"] in TERMINAL_JOB_STATUSES:
            return  # The Job is already over: nothing about the file is blocked.
    @retry_database_unit
    def persist() -> None:
        with owned_connection(db()) as connection:
            # The Job's own failure keeps the block as open Remediation work.
            LocalCopies(connection).block_reconciliation(job["file_id"], reason=reason, at=now_iso())
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
        changed = job_queue.settle_interrupted_job(
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
    published = LocalCopies(connection).publish_recovered_local_copy(
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
    return "requeued" if job_queue.settle_interrupted_job(
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
    cloud_deletion_service.lock_vault_for_totals(
        connection, vault_id=int(lead["vault_id"]), group_id=str(group_id))
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
        row for row in group_rows if row["status"] not in TERMINAL_JOB_STATUSES
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
        if job_queue.settle_interrupted_job(
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
        row = JobQueue(connection).interrupted_job_backlog(now=timestamp)
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
    """Reconcile expired or legacy durable worker states.

    The Job queue selects the interrupted Jobs and publishes every decision
    (``job_queue.QueueRunner``); this worker says what the Local Copy or the
    bucket shows about each one.
    """
    return job_runner.reconcile_interrupted_jobs(
        reachable=_interrupted_job_reachable,
        reconcile_job=_reconcile_interrupted_job,
        reconcile_purge_group=_reconcile_cloud_purge_group,
    )


def _interrupted_job_reachable(job: dict[str, Any]) -> bool:
    """False while interrupted local work stays suspended: its tree is absent,
    inaccessible, or replaced, and must not be touched."""
    if job["action"] in {"recover", "upload", "rename", "free-space"}:
        access = source_layout.vault_local_access(job["source_root"])
        if not access.local_operations_allowed:
            return False
    return True


def _reconcile_interrupted_job(connection: Any, job: dict[str, Any], *, timestamp: str) -> str:
    """Settle one interrupted Job from what its Local Copy shows; its outcome."""
    if job["action"] == "recover":
        source_root = Path(job["source_root"])
        if not source_root.is_dir():
            raise RuntimeError("Source folder is unavailable")
        target_path = safe_local_entry_path(
            job["source_root"], job["path"]
        )
        _remove_abandoned_restore_files(target_path)
        recovery_target = ArchiveVersions(connection).get_job_target(
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
                if job_queue.settle_interrupted_job(
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
                    if job_queue.settle_interrupted_job(
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
                    LocalCopies(connection).mark_local_copy_missing(
                        job["file_id"], observed_at=timestamp
                    )
                    message = "Local space freed (reconciled after restart)"
                    outcome = "completed"
                changed = job_queue.settle_interrupted_job(
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
            if job_queue.settle_interrupted_job(
                connection,
                job_id=int(job["id"]),
                status="queued",
                message="Operation interrupted by restart; automatically resumed",
                timestamp=timestamp,
                reset_progress=True,
            )
            else "skipped"
        )
    return outcome


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
    interval_ms = int(settings.job_progress_min_interval_ms or 500)
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
    limit = settings.bandwidth_limit_kibps
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
        payload=job.get("_upload_payload"),
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
            # The Job's bytes stay at the file's size: this shows it moving (#763).
            throughput.note_read_back(job.get("vault_id"), len(chunk))
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
# Admitted during a full scan on an observation the scan then compensated
# (#689, ADR-0045): the Job ends before any read; admission decides again.
UPLOAD_OBSERVATION_WITHDRAWN = "withdrawn"


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
    ``UPLOAD_OBSERVATION_WITHDRAWN``: admitted during a full scan on an
    observation whose generation has changed since (a compensated scan
    observation, or a newer one): it is no authority for this Job (#689).
    """
    with owned_connection(db()) as connection:
        local = ArchiveVersions(connection).get_job_target(job["id"])
        if _upload_owes_cloud_work(connection, local, job["id"]):
            return UPLOAD_READY
        admitted = local.get("job_admitted_generation") if local else None
        if admitted is not None and int(admitted) != int(local["local_generation"]):
            return UPLOAD_OBSERVATION_WITHDRAWN
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
            LocalCopies(connection).observe_local_copy(
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
        target = ArchiveVersions(connection).get_job_target(job["id"])
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


def _close_withdrawn_upload(job: dict[str, Any]) -> bool:
    """Close an upload admitted on an observation that no longer stands (#689).

    Nothing was read or sent. The Job ends ``cancelled`` with its own message;
    automatic admission queues the file again if its current observation is
    eligible.
    """
    return set_job(job["id"], "cancelled", message_key="job.upload_observation_withdrawn")


def _upload_key(job: dict[str, Any]) -> str:
    """The Job path's key under its Vault Target's prefix (root or ``vaults/<uuid>/``)."""
    return expected_cloud_key(job["path"], object_prefix(job["s3_key_layout"], job["vault_uuid"]))


@retry_database_unit
def _upload_catalog_unit(
    job: dict[str, Any], mutation: Callable[[ArchiveVersions], Any], *,
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
    with catalog_capacity(job), job_queue.defer_job_counters():
        with owned_connection(db()) as connection, unit_dirty_marks(connection):
            # Same Vault -> Job/file order as admission/cleanup, in either mode.
            # The private capability exists only inside this DB unit and still
            # compares every pin; nested prepare/send/publication checks need
            # not re-query the same fenced target. It is revoked before commit.
            with provenance._validated_target(
                connection, job, key=_upload_key(job), provider="db-fence", shared=shared,
            ):
                ensure_job_claim_owned_in_transaction(connection, job, "Upload claim was lost")
                return mutation(ArchiveVersions(connection))


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
    from .services import snapshot_cleanup
    snapshot_cleanup.cleanup(worker_adapter, job, keeper_id, client)


def require_sole_keeper(job: dict[str, Any], keeper: dict[str, Any], *, client: Any,
                        bucket: str) -> None:
    """Postcondition of a published keeper that superseded nothing (#622)."""
    from .services import snapshot_cleanup
    snapshot_cleanup.require_sole_keeper(worker_adapter, job, keeper,
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


class _WorkerAdapter:
    """Production Adapter of the worker seam (``services/worker_seam.py``, #793).

    What the Upload/copy Modules ask of this worker, under the seam's names.
    Stateless and built once: every method resolves the storage function when
    it is called, so a patched ``app.storage.X`` is the one that runs.
    """

    __slots__ = ()

    # -- JobClaim ------------------------------------------------------------
    OperationCancelled = OperationCancelled
    JobLeaseLost = JobLeaseLost
    WorkerStopping = WorkerStopping

    def claim(self, job: dict[str, Any]) -> Any:
        return job_queue.job_claim_context(
            job, lost_event=job_queue.claim_lost_event_for(job["id"]))

    def claim_lost_event(self, job_id: int) -> threading.Event | None:
        return job_queue.claim_lost_event_for(job_id)

    def stopped_in_process(self, job_id: int) -> bool:
        # Runs for every chunk an Upload reads: ``shutdown_state.checkpoint()``
        # written out, so the seam adds no call to that loop.
        if shutdown_state.requested:
            raise WorkerStopping("Process shutdown requested")
        return job_cancelled(job_id)

    def job_cancelled(self, job_id: int) -> bool:
        return job_cancelled(job_id)

    def ensure_job_active(self, job_id: int, message: str) -> None:
        ensure_job_active(job_id, message)

    def ensure_job_claim_owned_in_transaction(
        self, connection: Any, job: dict[str, Any], message: str,
    ) -> None:
        ensure_job_claim_owned_in_transaction(connection, job, message)

    def set_job(self, job_id: int, status: str, message: str = "", *,
                message_key: str | None = None) -> bool:
        return set_job(job_id, status, message, message_key=message_key)

    def set_job_in_transaction(
        self, connection: Any, job_id: int, status: str, *,
        message_key: str | None = None, transferred_bytes: int | None = None,
        fenced_unit: bool = False,
    ) -> bool:
        return job_runner.set_job_in_transaction(
            connection, job_id, status, message_key=message_key,
            transferred_bytes=transferred_bytes, fenced_unit=fenced_unit)

    def set_job_message_in_transaction(
        self, connection: Any, job_id: int, message_key: str,
    ) -> None:
        job_runner.set_job_message_in_transaction(connection, job_id, message_key)

    # -- SourceVolume --------------------------------------------------------
    UploadSourceRemoved = UploadSourceRemoved

    def safe_local_path(self, source_root: str, path: str) -> Path:
        return safe_local_path(source_root, path)

    def source_volume_problem_open(self, connection: Any, job: dict[str, Any]) -> bool:
        return _source_volume_problem_open(connection, job)

    def resolve_source_volume_problem(self, job: dict[str, Any]) -> None:
        _resolve_source_volume_problem(job)

    def check_upload_source_root(self, job: dict[str, Any], vault: dict[str, Any] | None) -> None:
        _upload_source_root_check(job, vault)

    def require_upload_source_identity(self, job: dict[str, Any]) -> None:
        _require_upload_source_identity(job)

    # -- UploadCatalog -------------------------------------------------------
    def db(self) -> Any:
        return db()

    def now_iso(self) -> str:
        return now_iso()

    def catalog_unit(self, job: dict[str, Any], mutation: Callable[[ArchiveVersions], Any], *,
                     shared: bool = False) -> Any:
        return _upload_catalog_unit(job, mutation, shared=shared)

    def upload_key(self, job: dict[str, Any]) -> str:
        return _upload_key(job)

    def record_reconciliation_block(self, job: dict[str, Any], reason: str) -> None:
        _record_upload_reconciliation_block(job, reason)

    def clear_reconciliation_problem(self, job: dict[str, Any]) -> None:
        _clear_upload_reconciliation_problem(job)

    def record_verification_failure(self, reason: str) -> None:
        _record_verification_failure(reason)

    # -- AutomaticUploadGate -------------------------------------------------
    UPLOAD_READY = UPLOAD_READY
    UPLOAD_SOURCE_REMOVED = UPLOAD_SOURCE_REMOVED
    UPLOAD_OBSERVATION_WITHDRAWN = UPLOAD_OBSERVATION_WITHDRAWN

    def automatic_upload_state(self, job: dict[str, Any]) -> str:
        return _automatic_upload_state(job)

    def close_removed_upload(self, job: dict[str, Any]) -> bool:
        return _close_removed_upload(job)

    def close_withdrawn_upload(self, job: dict[str, Any]) -> bool:
        return _close_withdrawn_upload(job)

    def upload_protection_paused(self, job: dict[str, Any]) -> bool:
        return _upload_protection_paused(job)

    # -- UploadTransport -----------------------------------------------------
    def s3_client(self) -> Any:
        return s3_client()

    def validate_cloud_vault(self, job: dict[str, Any]) -> None:
        validate_cloud_vault(job)

    def is_cloud_readiness_error(self, error: BaseException) -> bool:
        return cloud_preflight_gate.is_readiness_error(error)

    def prepare_upload_snapshot(
        self, source: Path, check_active: Callable[[], None], *, payload: Any = None,
    ) -> Any:
        # Without a payload the call stays the two-argument one.
        if payload is None:
            return prepare_upload_snapshot(source, check_active)
        return prepare_upload_snapshot(source, check_active, payload=payload)

    def upload_s3_file(self, job: dict[str, Any], *, source: Path, key: str) -> dict[str, Any]:
        return upload_s3_file(job, source=source, key=key)

    def validated_s3_proof(
        self, proof: dict[str, Any], *, version_id: str, snapshot: Any, source: str,
        completion: bool = False,
    ) -> dict[str, str] | None:
        return validated_s3_proof(proof, version_id=version_id, snapshot=snapshot,
                                  source=source, completion=completion)

    def verification_version_head(
        self, client: Any, job: dict[str, Any], target: dict[str, Any],
    ) -> dict[str, Any]:
        return _verification_version_head(client, job, target)

    def stream_plaintext_for_verification(
        self, job: dict[str, Any], *, target: dict[str, Any], source_size: int,
        check_active: Callable[[], None] | None = None, floor_reported: bool = False,
    ) -> tuple[str, int]:
        return _stream_plaintext_for_verification(
            job, target=target, source_size=source_size, check_active=check_active,
            floor_reported=floor_reported)

    def cleanup_superseded_upload(
        self, job: dict[str, Any], *, keeper_id: str | None, client: Any = None,
    ) -> None:
        cleanup_superseded_upload(job, keeper_id=keeper_id, client=client)

    def require_sole_keeper(
        self, job: dict[str, Any], keeper: dict[str, Any], *, client: Any, bucket: str,
    ) -> None:
        require_sole_keeper(job, keeper, client=client, bucket=bucket)


worker_adapter = _WorkerAdapter()


def process_upload(job: dict[str, Any]) -> None:
    """Compatibility hook: the UploadExecution Module owns the per-file protocol."""
    from .services.upload_execution import UploadExecution
    UploadExecution(worker_adapter).run(job)


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
                keeper = copy_provenance.run(worker_adapter, job, purpose="rename")
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
    target = ArchiveVersions(connection).get_job_target(job["id"])
    if not local_release_is_confirmed(
        connection, int(job["vault_id"]), job["path"]
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
    with scan_lock_for_vault(int(job["vault_id"])).holding("free_space"):
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
        local_copies = LocalCopies(connection)
        observed_at = now_iso()
        if replacement:
            # Content that replaced the claimed original: Current may be the
            # only copy now, so it is held unless it proves identical.
            local_copies.observe_replaced_local_copy(
                file_id, file_type=file_type, size=info.st_size,
                mtime_ns=info.st_mtime_ns, observed_at=observed_at,
            )
        else:
            # New bytes (a different digest) restart the content window and
            # never inherit the released copy's fingerprint or match.
            local_copies.observe_local_copy(
                vault_id=job["vault_id"], path=job["path"], file_type=file_type,
                size=info.st_size, mtime_ns=info.st_mtime_ns, observed_at=observed_at,
                invalidate_content=digest != target["local_sha256"],
            )
        if digest:
            observed = connection.execute(
                "SELECT generation FROM files WHERE id=%s", (file_id,),
            ).fetchone()
            local_copies.publish_local_fingerprint(
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
    # The claim renames the Local Copy inside its folder (#681).
    write_access.require_writable_directory(local_path.parent)
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
        local_copies = LocalCopies(connection)
        if replacement_stat is None:
            local_copies.mark_local_copy_missing(
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
            local_copies.observe_replaced_local_copy(
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
            tier=str(job.get("restore_tier") or settings.restore_tier or "Bulk"),
            days=int(job.get("restore_days") or settings.restore_days or 3))
        ensure_job_active(job["id"], "Storage class change stopped")
        s3_client().restore_object(
            Bucket=job["s3_bucket"], Key=current["object_key"],
            VersionId=current["provider_version_id"],
            RestoreRequest={"Days": estimate.days,
                            "GlacierJobParameters": {"Tier": estimate.tier}})
        state, expiry = "restoring", None
    with db() as connection:
        ensure_job_claim_owned_in_transaction(connection, job, "Storage class claim was lost")
        ArchiveVersions(connection).update_restore_state(
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
            worker_adapter, job, purpose="storage-class", storage_class=target_class,
            check_source=partial(vault_moves.check_source, target_class) if colder_only else None)
    except copy_provenance.CopyNeedsRestore as exc:
        if colder_only:
            raise vault_moves.MoveBlocked(
                "api.vault_move.never_restores", storage_class=exc.storage_class) from exc
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


def _apply_original_ownership(fd: int, ownership: Any) -> bool:
    """Give the published Local Copy its original mode, then owner (#679).

    Works on the open descriptor of the published inode, so a path swapped
    after publication is never touched. The mode goes first: once the file
    belongs to another uid, a process without CAP_FOWNER may no longer chmod
    it. ``chown`` needs CAP_CHOWN for another owner; when it is refused (for
    example with a non-root PUID) the previous mode comes back and the file
    keeps the runtime identity's ownership, as before #679. Returns whether
    the original ownership was applied; a refusal never fails the Recovery.
    """
    before = os.fstat(fd)
    try:
        os.fchmod(fd, ownership.mode)
        os.fchown(fd, ownership.uid, ownership.gid)
    except OSError:
        # EPERM without CAP_CHOWN; EINVAL for an id unmapped in a user namespace.
        try:
            os.fchmod(fd, stat.S_IMODE(before.st_mode))
        except OSError:
            pass
        return False
    if stat.S_IMODE(os.fstat(fd).st_mode) != ownership.mode:
        # chown(2) may clear set-user/group-ID bits; the owner can reapply them
        # only while it is still us (uid 0 without CAP_FSETID cannot).
        try:
            os.fchmod(fd, ownership.mode)
        except PermissionError:
            pass
    return True


def _inherit_folder_owner(fd: int, folder: Path) -> None:
    """A version archived without owner evidence gets its folder's owner (#681).

    Before v0.8 no owner was recorded. Running as root, keeping the process
    identity would leave a ``0:0`` file that the share's other containers may
    no longer change. The mode stays the default one; a refused ``chown``
    (no CAP_CHOWN) keeps the process identity and never fails the Recovery.
    """
    try:
        folder_stat = os.stat(folder)
        current = os.fstat(fd)
        if (current.st_uid, current.st_gid) != (folder_stat.st_uid, folder_stat.st_gid):
            os.fchown(fd, folder_stat.st_uid, folder_stat.st_gid)
    except OSError:
        pass


def _restore_expiry_date(expiry: str | None) -> str | None:
    """The day an S3 restore expires (``expiry-date`` of the Restore header)."""
    if not expiry:
        return None
    try:
        return email.utils.parsedate_to_datetime(expiry).date().isoformat()
    except (TypeError, ValueError, IndexError):
        try:
            return datetime.fromisoformat(str(expiry)).date().isoformat()
        except ValueError:
            return None


def _local_failure_message(
    job: dict[str, Any], error: write_access.DestinationNotWritable,
) -> tuple[str, dict[str, Any]]:
    until = job.get("_restore_kept_until") if job["action"] == "recover" else None
    if until:
        return "job.destination_not_writable_restore_kept", {**error.message_params, "until": until}
    return error.message_key, dict(error.message_params)


def process_recover(job: dict[str, Any]) -> None:
    ensure_job_active(job["id"], "Recovery stopped")
    validate_cloud_vault(job)
    with db() as connection:
        target = _recovery_target(ArchiveVersions(connection).get_job_target(job["id"]))
    if target["local_presence"] == "present":
        raise RuntimeError("A local copy is already present")

    from .services.upload_snapshot import recovery_mtime_ns, recovery_ownership

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
    original_ownership = recovery_ownership(target.get("upload_snapshot_json"), head.get("Metadata") or {})
    ownership_applied: bool | None = None
    head_class = head.get("StorageClass") or storage_class
    restore_state, restore_expiry = restore_header_state(head.get("Restore"))
    needs_restore = storage_class_requires_restore(head_class)
    if needs_restore and restore_state != "available":
        tier = target.get("restore_tier") or settings.restore_tier or "Bulk"
        days = int(target.get("restore_days") or settings.restore_days or 3)
        estimate = estimate_restore(
            size_bytes=size_bytes,
            storage_class=str(head_class),
            tier=str(tier),
            days=days,
        )
        if job["status"] != "restoring" and restore_state == "not_requested":
            # A restore costs money and hours: never ask for one that the
            # final step could not publish (#681).
            write_access.require_writable_parent(
                job["source_root"], safe_local_entry_path(job["source_root"], job["path"]))
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
            ArchiveVersions(connection).update_restore_state(
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

    if needs_restore:
        # From here a failure leaves the restored copy at AWS: the Job says
        # until when Recover can reuse it without a new restore (#681).
        job["_restore_kept_until"] = _restore_expiry_date(restore_expiry)
    destination = safe_local_entry_path(job["source_root"], job["path"])
    write_access.require_writable_parent(job["source_root"], destination)
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
        with scan_lock_for_vault(int(job["vault_id"])).holding("recovery"):
            # Transfers/hash stay outside the filesystem critical section. The
            # observation token is read before the final path appears, and the
            # observation, digest and recovered_at are published with one
            # compare-and-swap on it: a newer observation is never overwritten.
            ensure_job_active(job["id"], "Recovery claim was lost")
            if not _local_stat_unchanged(temporary, recovered_stat):
                raise RuntimeError("Recovered plaintext changed before publication")
            with db() as connection:
                ensure_job_claim_owned_in_transaction(connection, job, "Recovery claim was lost")
                live = _recovery_target(ArchiveVersions(connection).get_job_target(job["id"]))
            if (str(live["archive_version_id"]), live["version_sha256"]) != (
                archive_version_id, expected_digest,
            ) or live["local_presence"] == "present":
                raise RuntimeError("The recovery target changed before publication")
            generation = int(live["local_generation"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Still ours and readable: ownership changes only after publication,
            # through this descriptor of the verified inode.
            published_fd = os.open(temporary, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
            try:
                opened = os.fstat(published_fd)
                if (opened.st_dev, opened.st_ino, opened.st_ctime_ns) != (
                    recovered_stat.st_dev, recovered_stat.st_ino, recovered_stat.st_ctime_ns,
                ):
                    raise RuntimeError("Recovered plaintext changed before publication")
                _publish_without_replacing(temporary, destination)
                if original_ownership is not None:
                    ownership_applied = _apply_original_ownership(published_fd, original_ownership)
                else:
                    _inherit_folder_owner(published_fd, destination.parent)
                published_stat = os.fstat(published_fd)
            finally:
                os.close(published_fd)
            recovered_at = now_iso()
            with db() as connection:
                ensure_job_claim_owned_in_transaction(connection, job, "Recovery claim was lost")
                versions = ArchiveVersions(connection)
                published = LocalCopies(connection).publish_recovered_local_copy(
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
                    versions.update_restore_state(
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
    if ownership_applied is False:
        set_job(job["id"], "completed", message_key="job.recovered_to_without_ownership",
                message_params={"target": str(destination), "uid": original_ownership.uid,
                                "gid": original_ownership.gid,
                                "mode": f"{original_ownership.mode:04o}"})
    else:
        set_job(job["id"], "completed", message_key="job.recovered_to",
                message_params={"target": str(destination)})


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

    Retention Lock (#736): a member whose Current is still locked is not sent
    (its estimated RetainUntilDate is ahead) or, refused by S3, is explained by
    its observed retention; either way it is deferred, never failed, and goes
    back to ``pending_delay`` in one last unit (``defer_locked``).
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
    expected_job_ids: list[int] = []
    revoked_ids: set[int] = set()

    def still_valid(connection: Any) -> bool:
        """Revalidate the consent. An automatic group may lose single members
        (#726): they leave the claim; a lost lead hands the rest back to the
        scheduler, which claims them again under another lead."""
        revoked: set[int] = set()
        valid = cloud_deletion_service.validate_execution(
            connection, vault_id=vault_id, group_id=group_id, at=now_iso(), revoked=revoked,
        )
        if not revoked:
            return valid
        revoked_ids.update(revoked)
        expected_job_ids[:] = [i for i in expected_job_ids if i not in revoked]
        if valid and int(job["id"]) in revoked:
            released = JobQueue(connection).release_purge_group_claim(
                vault_id=vault_id, group_id=group_id, claim_token=str(claim_token), now=now_iso(),
            )
            if released:
                cloud_deletion_service.revoke_grants(
                    connection, group_id=group_id,
                    file_ids=[str(r["file_id"]) for r in connection.execute(
                        "SELECT file_id FROM jobs WHERE id = ANY(%s::bigint[])", (released,),
                    ).fetchall()],
                )
            return False
        return valid

    with db() as connection:
        if not still_valid(connection):
            return
        purge_jobs, items = JobQueue(connection).load_claimed_purge_group(
            lead_job_id=int(job["id"]), claim_token=str(claim_token), now=now_iso(),
        )
        lock_days = retention_lock_service.vault_lock_days(connection, vault_id)
    if not purge_jobs:
        return
    # Members a Retention Lock still holds: never sent now (#736).
    deferred: dict[int, datetime] = {}
    if lock_days:
        moment = datetime.now(timezone.utc)
        for item in items:
            until = retention_lock_service.locked_at(item.get("version_created_at"), lock_days, moment)
            if until is not None:
                deferred[int(item["id"])] = until
        items = [item for item in items if int(item["id"]) not in deferred]
    # The lead carries the worker's claim checkpoints: settle it in the last
    # batch, so an ended lead never fences the members still to be deleted.
    items = sorted(items, key=lambda item: int(item["id"]) == int(job["id"]))
    expected_job_ids[:] = [int(purge_job["id"]) for purge_job in purge_jobs]
    actor = "owner"

    def renew(connection: Any) -> None:
        stamp = now_iso()
        if job_queue.claim_is_lost(int(job["id"])) or not JobQueue(connection).renew_purge_group_claim(
            vault_id=vault_id, group_id=group_id, claim_token=str(claim_token),
            expected_job_ids=expected_job_ids, now=stamp,
            claim_expires_at=job_runner.claim_expiry_at(stamp),
        ):
            job_queue.mark_claim_lost(int(job["id"]))
            raise JobLeaseLost("Cloud purge claim was lost")

    for offset in range(0, len(items), CLOUD_DELETE_BATCH_SIZE):
        batch = items[offset : offset + CLOUD_DELETE_BATCH_SIZE]
        ensure_job_active(job["id"], "Cloud purge stopped")
        with db() as connection:
            if not still_valid(connection):
                return
            renew(connection)
            batch = [item for item in batch if int(item["id"]) not in revoked_ids]
            if not batch:
                continue
            vault = cloud_deletion_service.lock_vault(connection, vault_id)
            cloud_deletion_service.freeze_grants(
                connection, vault=vault, group_id=group_id, jobs=batch, provider=provider,
            )
        response: dict[str, Any] | None = None
        ambiguous: Exception | None = None
        sent = False
        try:
            with owned_connection(db()) as connection:
                if not still_valid(connection):
                    return
                renew(connection)
                if job_cancelled(int(job["id"])):
                    raise OperationCancelled("Cloud purge stopped")
                # A member revoked just now had its grant revoked with it.
                batch = [item for item in batch if int(item["id"]) not in revoked_ids]
                if not batch:
                    continue
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
        if job_queue.claim_is_lost(int(job["id"])):
            job_queue.mark_claim_lost(int(job["id"]))
            raise _PurgeUnsettled("Cloud purge claim was lost")
        deleted: list[dict[str, Any]] = []
        failed: list[tuple[dict[str, Any], str]] = []
        locked: dict[int, datetime] = {}
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
            unreadable = False
            if lock_days and failed:
                # A version still locked cannot have been deleted: wait for
                # its observed RetainUntilDate instead of failing (#736).
                failed, unreadable = _locked_purge_members(client, str(job["s3_bucket"]), failed,
                                                           deleted, locked)
            stamp = now_iso()
            with owned_connection(db()) as connection:
                cloud_deletion_service.lock_vault_for_totals(
                    connection, vault_id=vault_id, group_id=group_id)
                renew(connection)
                cloud_deletion_service.record_deleted(
                    connection, jobs=deleted, at=stamp, actor=actor,
                )
                cloud_deletion_service.record_failed(
                    connection, failures=failed, at=stamp, actor=actor,
                )
                if unreadable:
                    # GetObjectRetention was refused: deferred, with a Problem (#749).
                    retention_lock_service.report_unreadable(connection, vault_id)
                if locked:
                    # Their grants go now; they stay claimed until the end.
                    cloud_deletion_service.revoke_grants(
                        connection, group_id=group_id,
                        file_ids=[str(item["file_id"]) for item in batch
                                  if int(item["id"]) in locked])
            deferred.update(locked)
            # Ended members dropped their lease; the claim covers the rest.
            ended = {int(item["id"]) for item in deleted} | {int(item["id"]) for item, _ in failed}
            expected_job_ids[:] = [i for i in expected_job_ids if i not in ended]
        except JobLeaseLost:
            raise _PurgeUnsettled("Cloud purge claim was lost")
        except Exception as exc:
            # Never fail a Job whose exact DELETE may have run without its
            # record: keep it held; the restart reconciliation lists and settles.
            raise _PurgeUnsettled(f"Cloud deletion outcome not recorded: {exc}") from exc
    if deferred:
        with owned_connection(db()) as connection:
            if not still_valid(connection):
                return
            renew(connection)
            remaining = {job_id: until for job_id, until in deferred.items()
                         if job_id in expected_job_ids}
            cloud_deletion_service.defer_locked(
                connection, vault_id=vault_id, group_id=group_id, deferred=remaining,
                at=now_iso())


def _locked_purge_members(
    client: Any, bucket: str, failed: list[tuple[dict[str, Any], str]],
    deleted: list[dict[str, Any]], locked: dict[int, datetime],
) -> tuple[list[tuple[dict[str, Any], str]], bool]:
    """Split refused members of a locked Vault: still locked, gone, or failed (#736).

    Only the version's observed retention decides (``locked_until``: 5 minutes
    of clock skew, at least an hour): a version locked until a later date
    waits (``locked``), one that is gone was deleted, anything else stays a
    failure. A retention S3 refuses to read waits an hour too; the second
    value says so, for its Problem (#749).
    """
    moment = datetime.now(timezone.utc)
    still_failed: list[tuple[dict[str, Any], str]] = []
    unreadable = False
    for item, message in failed:
        try:
            until = retention_lock_service.locked_until(
                client, bucket=bucket, key=str(item["object_key"]),
                version_id=str(item["provider_version_id"]), now=moment)
        except retention_lock_service.VersionGone:
            deleted.append(item)
            continue
        except retention_lock_service.RetentionUnreadable as exc:
            until, unreadable = exc.until, True
        except Exception:
            until = None
        if until is not None:
            locked[int(item["id"])] = until
        else:
            still_failed.append((item, message))
    return still_failed, unreadable


@retry_database_unit
def _load_claimed_job_status(job: dict[str, Any]) -> dict[str, Any] | None:
    claim_token = job.get("claim_token")
    if claim_token:
        return job_runner.renew_started_claim(job)
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
        JobQueue(connection).release_purge_group_claim(
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
        runtime = settings
        current = _load_claimed_job_status(job)
        if current is None:
            return False
        if current["status"] in TERMINAL_JOB_STATUSES:
            return False
        job["status"] = current["status"]
        if current["status"] in {"queued", "retrying", "restoring"}:
            # Drop stale in-memory cancel flags from prior process/test DBs that
            # reused this Job id; a live cancel after this point re-arms the set.
            job_queue.forget_job_cancellation(int(job["id"]))
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
        if job_queue.claim_is_lost(int(job["id"])):
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
        if job["action"] in {"recover", "free-space"}:
            # A folder FrostVault may not write: say which, whose and with
            # which mode, instead of a raw Errno 13 (#681). A rename Job
            # writes nothing locally (the Owner already renamed the file; the
            # Job copies its cloud key), so its EACCES is a read and keeps
            # its own message.
            not_writable = write_access.from_permission_error(exc)
            if not_writable is not None:
                key, params = _local_failure_message(job, not_writable)
                failed = set_job(job["id"], "failed", message_key=key, message_params=params)
                if failed and job["action"] == "free-space":
                    record_automatic_cleanup_outcome(
                        job, event="local_cleanup.failed", outcome="failure",
                    )
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
        if job["action"] == "recover" and job.get("_restore_kept_until"):
            return set_job(job["id"], "failed", message_key="job.recovery_failed_restore_kept",
                           message_params={"error": message, "until": job["_restore_kept_until"]})
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
    with job_runner.hold_claim(job):
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


# Its own component (#711): a failing fold is not the loop failing, and the
# loop's clean cycles must not close the fold's problem.
_DIRECTORY_AGGREGATES_WORKER = periodic_workers.PeriodicWorker(
    "directory_aggregates", (lambda: process_directory_aggregate_maintenance(),),
    lambda: DIRECTORY_AGGREGATE_BATCH_SECONDS,
)


def _run_directory_aggregates() -> None:
    run_periodic_worker(_DIRECTORY_AGGREGATES_WORKER)


def _submit_directory_aggregates() -> None:
    """Fold directory aggregates on their own single thread, one fold at a time.

    Not on the scheduler loop: one fold of a folder with ~140,000 descendants
    took ~2 s each on Unraid, and with the loop waiting for it no freed
    operation slot was refilled (#696). Not on the maintenance pool either: a
    full scan holds its only worker for hours.
    """
    global _aggregate_executor, _aggregate_future
    with _maintenance_lock:
        if shutdown_state.requested:
            return
        if _aggregate_future is not None and not _aggregate_future.done():
            return
        if _aggregate_executor is None:
            _aggregate_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="directory-aggregates",
            )
        _aggregate_future = _aggregate_executor.submit(_run_directory_aggregates)


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
    global _operation_executor, _maintenance_executor, _aggregate_executor, _aggregate_future
    futures_from_watchers = _detach_watcher_scan_futures()
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
        aggregates, _aggregate_executor = _aggregate_executor, None
        if _aggregate_future is not None:
            futures.append(_aggregate_future)
            _aggregate_future = None
        if aggregates is not None:
            aggregates.shutdown(wait=False, cancel_futures=True)
    return futures + futures_from_watchers


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
    if source_devices.throttled():
        # Uploads held back for the server (#713) are a choice, not a stall.
        _queue_progress.note_progress()
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


# Background work holding a Vault for this long makes it a Stalled Vault (#759):
# its scans and uploads wait behind it. A full scan is never one.
VAULT_STALL_SECONDS = 600.0
VAULT_STALL_REFRESH_SECONDS = 60.0
BACKGROUND_VAULT_OPERATIONS = frozenset({"auto_admission", "identify_local_copies"})
# Vault id -> (operation, since) of each open Stalled Vault problem; None until
# the first check, which also closes those a previous process left open.
_stalled_vaults: dict[int, tuple[str, str]] | None = None
_stalled_vaults_refreshed_at: dict[int, float] = {}


def observe_vault_stalls(*, now: float | None = None) -> dict[int, dict[str, Any]]:
    """Open, refresh or close the Stalled Vault problem of each Vault (#759).

    It reads the in-memory lock holders, so it runs from the background loop,
    never from the thread doing the work. Only state changes (and a refresh
    of the duration each minute) write. No email: it clears by itself.
    """
    global _stalled_vaults
    current = time.monotonic() if now is None else now
    stalled = {
        vault_id: holder for vault_id, holder in lock_holders().items()
        if holder["operation"] in BACKGROUND_VAULT_OPERATIONS
        and current - float(holder["since_monotonic"]) >= VAULT_STALL_SECONDS
    }
    metrics_service.set_gauge("vaults_stalled", float(len(stalled)))
    previous = _stalled_vaults
    if previous is None:
        with db() as connection:
            rows = connection.execute(
                "SELECT vault_id FROM notifications WHERE event='vault_stalled' "
                "AND problem_status='open' AND vault_id IS NOT NULL"
            ).fetchall()
        previous = {int(row["vault_id"]): ("", "") for row in rows}
    opened: dict[int, tuple[str, str]] = {}
    for vault_id, holder in stalled.items():
        state = (str(holder["operation"]), str(holder["since"]))
        opened[vault_id] = state
        if (previous.get(vault_id) == state and current
                - _stalled_vaults_refreshed_at.get(vault_id, current) < VAULT_STALL_REFRESH_SECONDS):
            continue
        with db() as connection:
            notification_service.set_problem(
                connection, key=f"vault_stalled:{vault_id}", event="vault_stalled",
                vault_id=vault_id, active=True, email=False,
                params={"operation_key": state[0], "held_since": state[1],
                        "minutes": int((current - float(holder["since_monotonic"])) // 60)},
            )
        _stalled_vaults_refreshed_at[vault_id] = current
    for vault_id in set(previous) - set(opened):
        with db() as connection:
            notification_service.set_problem(
                connection, key=f"vault_stalled:{vault_id}", event="vault_stalled",
                vault_id=vault_id, active=False, email=False,
            )
        _stalled_vaults_refreshed_at.pop(vault_id, None)
    _stalled_vaults = opened
    return stalled


def _claim_source_device(job: Mapping[str, Any]) -> str | None:
    """The Source Device an upload candidate reads, or None (not an upload)."""
    if job.get("action") != "upload" or not job.get("source_root") or not job.get("path"):
        return None
    return source_devices.device_of_entry(str(job["source_root"]), str(job["path"]))


def process_jobs_once(*, wait: bool = True) -> int:
    """Claim and dispatch one fair, concurrency-bounded scheduler batch.

    Selecting candidates is intentionally separate from acquisition.  Every
    selected row is rechecked by the conditional claim of ``JobQueue``;
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
    runtime = settings
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
            queue = JobQueue(connection)
            candidates = [
                row for row in queue.list_claimable_jobs(
                    now=now,
                    restore_due_before=_restore_due_before(runtime, current=current),
                    limit=max(10, concurrency * 10) * 8,
                    # Enough ready Jobs to rotate uploads across Source
                    # Devices, plus those the scan just read (#713).
                    per_vault_limit=(max(window, source_devices.CLAIM_WINDOW)
                                     if available_slots else window),
                    exclude_job_ids=_deferred_job_ids(scope),
                    unserved_only=not available_slots,
                    include_file_ids=(source_devices.hot_files().ids()
                                      if available_slots else ()),
                )
                if int(row["id"]) not in seen_ids
            ]
            if not candidates:
                # This includes a wholly deferred local queue: leaving its old
                # clock open would incorrectly bank the backoff after restart.
                queue.observe_scheduler_turns(now=now, runnable_vault_ids=[])
                break
            seen_ids.update(int(row["id"]) for row in candidates)
            if _scan_round == 0:
                active_by_vault = queue.active_job_counts(now=now)
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
                if job["action"] == "recover" and _restore_residue_sweep_running.is_set():
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
            turns = JobQueue(connection).observe_scheduler_turns(
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
        if source_devices.throttled():
            # Server priority (#713): running uploads finish, none starts.
            ordered = [job for job in ordered if job['action'] != 'upload']
        # Uploads rotate across Source Devices with a free read slot, and an
        # upload of a Local Copy the scan just read goes first (#713).
        ordered = source_devices.rotate_claims(
            ordered, device_of=_claim_source_device,
            slots=source_devices.slots(), hot=source_devices.hot_files(),
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
        if job_runner.claim(job, restore_due_before=lambda: _restore_due_before(runtime)):
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
        runtime = settings
        return JobQueue(connection).claimable_queue_depth(
            now=timestamp,
            restore_due_before=_restore_due_before(runtime),
            exclude_job_ids=(
                _deferred_job_ids(_database_scope(connection)) if exclude_deferred else ()
            ),
        )


# A requested full scan that waits for its Vault, one per Vault (#759).
_queued_scans: dict[int, Future[dict[str, int]]] = {}
QUEUED_SCAN_POLL_SECONDS = 1.0


def vault_busy(vault_id: int) -> bool:
    """Whether a scan started now would find the Vault taken.

    A full scan verifying fingerprints runs without the lock (#689), so a
    running scan counts as well.
    """
    with status_lock:
        scanning = bool((runtime_status.get(int(vault_id)) or {}).get("scanning"))
    return scanning or scan_lock_for_vault(vault_id).locked()


def queue_scan(vault: dict[str, Any]) -> Future[dict[str, int]]:
    """Run a full scan of the Vault now, or as soon as the Vault is free (#759).

    A scan requested while another operation holds the Vault is not lost: it
    waits, in memory, and the Vault's runtime status says what it waits for.
    At a restart the full scan is due anyway. Requests made before a scan
    starts share it, and so does any scan that starts first (it takes the
    request); one made while a scan runs waits for a scan of its own, since
    the running one may have walked past what made it due.
    """
    vault_id = int(vault["id"])
    with status_lock:
        pending = _queued_scans.get(vault_id)
        if pending is not None:
            return pending
        future: Future[dict[str, int]] = Future()
        # Shared by every requester: one that stops waiting cannot cancel it.
        future.set_running_or_notify_cancel()
        _queued_scans[vault_id] = future
        runtime_status.setdefault(
            vault_id, {"scanning": False, "last_scan": None, "last_error": None},
        )["scan_queued_at"] = now_iso()
    try:
        _submit_queued_scan(dict(vault), future)
    except BaseException as exc:
        # Never a request left pending forever: the next one starts afresh.
        _take_queued_scan(vault_id, future)
        _settle_queued_scan(future, exception=exc)
    return future


def _submit_queued_scan(vault: dict[str, Any], future: Future[dict[str, int]]) -> None:
    """Wait on the Vault's paired-scan thread, which the shutdown drain awaits
    (ADR-0045, #696). Under the pool lock: a drain cannot miss it."""
    with _watcher_scan_pool_lock:
        shutdown_state.checkpoint()
        task = _watcher_scan_executor_locked(int(vault["id"])).submit(
            _run_queued_scan, vault, future)
        _watcher_scan_futures.add(task)

    def done(task: Future[Any]) -> None:
        _forget_watcher_scan_future(task)
        if task.cancelled():  # Dropped by a shutdown before it ran.
            _take_queued_scan(int(vault["id"]), future)
            _settle_queued_scan(future, result={"stopped": 1})

    task.add_done_callback(done)


def _settle_queued_scan(future: Future[dict[str, int]], *, result: dict[str, int] | None = None,
                        exception: BaseException | None = None) -> None:
    """Resolve a queued scan once; whoever comes second changes nothing."""
    try:
        if exception is not None:
            future.set_exception(exception)
        else:
            future.set_result(result or {})
    except InvalidStateError:
        pass


def _take_queued_scan(vault_id: int, future: Future[dict[str, int]] | None = None,
                      ) -> Future[dict[str, int]] | None:
    """Remove the Vault's pending request (only ``future``, when given)."""
    with status_lock:
        pending = _queued_scans.get(vault_id)
        if pending is None or (future is not None and pending is not future):
            return None
        _queued_scans.pop(vault_id, None)
        (runtime_status.get(vault_id) or {}).pop("scan_queued_at", None)
        return pending


def _run_queued_scan(vault: dict[str, Any], future: Future[dict[str, int]]) -> None:
    vault_id = int(vault["id"])
    try:
        while not future.done():
            if shutdown_state.requested:
                _take_queued_scan(vault_id, future)
                _settle_queued_scan(future, result={"stopped": 1})
                return
            if vault_busy(vault_id):
                time.sleep(QUEUED_SCAN_POLL_SECONDS)
                continue
            result = scan_vault(vault)
            if not result.get("already_running"):
                # scan_vault took the request; settle it here as well in case
                # it did not (a scan that started first resolved it already).
                _take_queued_scan(vault_id, future)
                _settle_queued_scan(future, result=result)
    except BaseException as exc:
        _take_queued_scan(vault_id, future)
        if isinstance(exc, Exception) and not isinstance(exc, WorkerStopping):
            _report_worker_error("source_scan", exc, vault_id)
        _settle_queued_scan(future, exception=exc)


def request_full_scan(vault: dict[str, Any]) -> None:
    """Run a full scan of the Vault now, or right after the one in progress.

    Used when the Vault starts looking inside folders it did not observe
    (an Excluded Folder whose rule was removed, ADR-0044). A scan already
    walking used the older rules, so it does not count.
    """
    queue_scan(vault)


def _scan_or_queue(vault: dict[str, Any]) -> None:
    """A scheduled scan: run it now, or queue it behind the operation holding
    the Vault (#759). A scan already running is the scan that was due."""
    result = scan_vault(vault)
    if result.get("already_running"):
        with status_lock:
            scanning = bool((runtime_status.get(int(vault["id"])) or {}).get("scanning"))
        if not scanning:
            queue_scan(vault)


def scan_all_vaults() -> None:
    with db() as connection:
        vaults = connection.execute(
            f"{VAULT_ROW_SQL} WHERE v.lifecycle='active' ORDER BY v.id"
        ).fetchall()
    for vault in vaults:
        if shutdown_state.requested:
            return
        _scan_or_queue(vault)


AUTO_UPLOAD_POLL_SECONDS = 10


def queue_vault_auto_uploads(vault: dict[str, Any], *, now: datetime | None = None,
                             retry_failed_only: bool = False) -> int:
    """Drain stable watcher observations without a cloud call or full scan.

    The catalog is the durable pending set, including observations recovered by
    full-scan reconciliation. No in-memory timer is lost on restart.
    """
    lock = scan_lock_for_vault(int(vault["id"]))
    if not lock.acquire(blocking=False, operation="auto_admission"):
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
        with status_lock:
            # Holding the lock, a running full scan is verifying (#689).
            during_scan = bool((runtime_status.get(int(vault["id"])) or {}).get("scanning"))
        with pinned_root as root:
            # Renames and reappearances are decided first, in their own
            # transaction: admission then sees the outcome (#621). During a
            # full scan the scan hashes them and decides at its finalization.
            if not during_scan:
                # Named apart for a Stalled Vault (#759): the step that holds
                # the Vault longest on a large one.
                lock.relabel("identify_local_copies")
                try:
                    _identify_new_local_copies(
                        vault, root, now=now or datetime.now(timezone.utc),
                    )
                finally:
                    lock.relabel("auto_admission")
            with db() as connection:
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
                    during_scan=during_scan,
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
        if shutdown_state.requested:
            return
        _scan_or_queue(vault)


def recover_vaults_after_cloud_ready() -> None:
    scan_vaults_with_cloud_error()
    if shutdown_state.requested:
        return
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
        # A provisioning Vault's bucket may not exist yet: never probe it first.
        vault = connection.execute(
            f"{VAULT_ROW_SQL} WHERE v.lifecycle IN ('active', 'reconnect_required', 'stopping') "
            "ORDER BY v.id LIMIT 1").fetchone()
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


def _upload_remediation() -> Any:
    from .services import upload_remediation
    return upload_remediation


def _history_retention() -> Any:
    from .services import history_retention
    return history_retention


def _bulk_upload_admission() -> Any:
    from .services import bulk_upload_admission
    return bulk_upload_admission


# The workers that pace themselves (#795), each one task of the server loop
# run by ``periodic_workers.run_forever``. A declaration says what one pass
# runs, how long the worker waits, and what sets it apart from the others.
_CLOUD_PREFLIGHT_WORKER = periodic_workers.PeriodicWorker(
    "cloud_preflight", (lambda: reprobe_cloud_readiness_once(),), lambda: 1,
    reports_on_loop=True,
)
# Dedicated DB-only thread: neither long transfers, mount probes nor the
# singleton maintenance executor can delay discovery/admission ticks. A clean
# pass is not noted: the worker has never closed a problem of its own.
_UPLOAD_REMEDIATION_WORKER = periodic_workers.PeriodicWorker(
    "upload_remediation",
    (lambda: _upload_remediation().sweep(db), lambda: _upload_remediation().observe(db)),
    lambda: _upload_remediation().POLL_SECONDS,
    thread="upload-remediation", notes_ok=False,
)
# Retention of closed history (#596): short keyset batches on a dedicated
# DB-only thread; it deletes database rows only.
_HISTORY_RETENTION_WORKER = periodic_workers.PeriodicWorker(
    "history_retention", (lambda: _history_retention().collect(db),),
    lambda: _history_retention().POLL_SECONDS,
    thread="history-retention",
)
# Durable state lives in PostgreSQL, not this thread. One tick advances
# bounded transactions independently of transfer slots/cloud readiness.
# Stopping waits: a committing batch must not outlive shutdown/pool closure.
_BULK_UPLOAD_ADMISSION_WORKER = periodic_workers.PeriodicWorker(
    "bulk_upload_admission", (lambda: _bulk_upload_admission().tick(db),),
    lambda: _bulk_upload_admission().POLL_SECONDS,
    thread="bulk-upload-admission", drains_on_stop=True,
)
# Separate from scanning and cloud maintenance, including when a long scan
# or lifecycle request occupies their worker. Catalog observations persist.
# Each Vault notes itself healthy inside the pass (``queue_vault_auto_uploads``).
_AUTO_UPLOAD_WORKER = periodic_workers.PeriodicWorker(
    "auto_upload_queue", (lambda: queue_all_auto_uploads_once(),),
    lambda: AUTO_UPLOAD_POLL_SECONDS, notes_ok=False,
)


async def cloud_preflight_loop() -> None:
    await periodic_workers.run_forever(_CLOUD_PREFLIGHT_WORKER, worker_health)


async def upload_remediation_loop() -> None:
    await periodic_workers.run_forever(_UPLOAD_REMEDIATION_WORKER, worker_health)


async def history_retention_loop() -> None:
    await periodic_workers.run_forever(_HISTORY_RETENTION_WORKER, worker_health)


async def bulk_upload_admission_loop() -> None:
    await periodic_workers.run_forever(_BULK_UPLOAD_ADMISSION_WORKER, worker_health)


async def auto_upload_loop() -> None:
    await periodic_workers.run_forever(_AUTO_UPLOAD_WORKER, worker_health)


# Folder identities one watcher run already handed to a successor. Past this
# many it starts over: at worst a folder gets one more handoff.
WATCHER_HANDED_OFF_LIMIT = 4096


def _unknown_watcher_directories(
    vault_id: int, root: Path, changes: set[tuple[Change, str]],
    handed_off: set[tuple[int, int, int]],
) -> set[tuple[Change, str]]:
    """Folders a directory event reveals without an ``added`` event (#721).

    On an Unraid ``fuse.shfs`` source a new folder can arrive only as a
    ``modified`` of itself or of its parent, so no successor observer is
    registered and nothing inside it is ever reported. Each changed directory
    (not an added one, already handed off and walked whole) is listed one level
    deep; a child folder the catalog does not know is returned as an added
    directory. ``handed_off`` keeps one handoff per folder identity (device,
    inode and change time: a folder recreated under the same name, or changed,
    is another), so an empty folder does not re-register the tree each time
    its parent changes.
    """
    candidates: list[tuple[str, Path]] = []
    for change, value in changes:
        if change != Change.modified:
            continue
        entry = Path(value)
        if is_restore_temporary_name(entry.name) or ".." in entry.parts:
            continue
        try:
            relative = entry.relative_to(root).as_posix()
            if not entry.is_dir() or entry.is_symlink():
                continue
            if relative != ".":
                parent = entry.parent.resolve()
                if parent != root and root not in parent.parents:
                    continue
        except (ValueError, OSError):
            continue
        candidates.append((relative, entry))
    if not candidates:
        return set()
    found: set[tuple[Change, str]] = set()
    if len(handed_off) > WATCHER_HANDED_OFF_LIMIT:
        handed_off.clear()
    with db() as connection:
        excluded = excluded_folder_rules.load(connection, vault_id)
        for relative, entry in candidates:
            if relative != "." and excluded.covers_directory(relative):
                continue
            _files, folders, _excluded = _check_directory_level(
                connection, vault_id, root, entry, excluded, with_files=False,
            )
            for _relative, child in folders:
                try:
                    info = child.lstat()
                except OSError:
                    continue
                identity = (info.st_dev, info.st_ino, info.st_ctime_ns)
                if identity in handed_off:
                    continue
                handed_off.add(identity)
                found.add((Change.added, str(child)))
    return found


# A watcher that stopped is restarted after a growing pause. A run counts as
# healthy again, and resets the pause, once it stayed up this long. Its paired
# scan runs beside it (#690), so a long scan cannot hide a watcher that dies at
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
# A folder that vanishes while the observer registers it (Immich empties and
# recreates its upload/ folders) is not a failure: the watcher registers again
# at once and walks that path. Only this many failures in a row on the same
# path are reported as a stopped watcher (#690).
WATCHER_VANISHED_MAX_RETRIES = 5
# A vanished-folder restart waits this long instead of the growing pause.
WATCHER_VANISHED_RESTART_SECONDS = 0.5


def _watcher_restart_delay(streak: int) -> float:
    return float(min(WATCHER_RESTART_MAX_SECONDS, WATCHER_RESTART_BASE_SECONDS * 2 ** max(0, streak - 1)))


_WatcherScanOutcome = filesystem_watcher.ScanOutcome


_watcher_scan_pools: dict[int, ThreadPoolExecutor] = {}
_watcher_scan_pool_lock = threading.Lock()
# Running paired scans, awaited by the shutdown drain like maintenance work.
_watcher_scan_futures: set[Future[Any]] = set()


def _watcher_scan_executor_locked(vault_id: int) -> ThreadPoolExecutor:
    """One thread per Vault for its paired scans (#690); the caller holds the pool lock.

    The paired scans of one Vault take turns; another Vault's full scan, which
    can last hours on a large adoption, never delays them. Manual scans of
    different Vaults already ran side by side. Dedicated threads keep the long
    scans off the default executor, which the watchers and every request share.
    """
    pool = _watcher_scan_pools.get(int(vault_id))
    if pool is None:
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"watcher-scan-{int(vault_id)}")
        _watcher_scan_pools[int(vault_id)] = pool
    return pool


def _shutdown_watcher_scan_executor() -> None:
    with _watcher_scan_pool_lock:
        pools = list(_watcher_scan_pools.values())
        _watcher_scan_pools.clear()
    for pool in pools:
        pool.shutdown(wait=False, cancel_futures=True)


def _detach_watcher_scan_futures() -> list[Future[Any]]:
    _shutdown_watcher_scan_executor()
    with _watcher_scan_pool_lock:
        futures = list(_watcher_scan_futures)
        _watcher_scan_futures.clear()
    return futures


def _submit_watcher_scan(vault: dict[str, Any]) -> Future[Any]:
    # Under the pool lock: a drain that detached the pools cannot miss it.
    with _watcher_scan_pool_lock:
        shutdown_state.checkpoint()
        with status_lock:
            pending = _queued_scans.get(int(vault["id"]))
        if pending is not None:
            # A queued scan has not started yet, so it starts after this
            # registration: share it rather than a second full scan behind it
            # on the Vault's scan thread (#763). A request queued after this
            # check is taken by the scan submitted here, which runs first.
            return pending
        future = _watcher_scan_executor_locked(int(vault["id"])).submit(scan_vault, vault)
        _watcher_scan_futures.add(future)
    future.add_done_callback(_forget_watcher_scan_future)
    return future


def _forget_watcher_scan_future(future: Future[Any]) -> None:
    with _watcher_scan_pool_lock:
        _watcher_scan_futures.discard(future)


async def _reconcile_vault_after_watcher(vault: dict[str, Any]) -> _WatcherScanOutcome:
    """Run the full scan that a freshly registered watcher is paired with.

    Registration happens first, so anything that arrives during the scan is
    seen by the watcher and anything older is seen by the scan.
    """
    try:
        result = await asyncio.wrap_future(_submit_watcher_scan(vault))
        if result.get("already_running"):
            # The Vault is busy: the scan waits for it, visibly, and runs as
            # soon as it is free (#759). It starts after the registration.
            result = await asyncio.wrap_future(queue_scan(vault))
    except asyncio.CancelledError:
        raise
    except WorkerStopping:
        return _WatcherScanOutcome(float(WATCHER_SCAN_BUSY_SECONDS))
    except Exception as exc:
        worker_error_store.log_worker_error("source_watcher_scan", exc, int(vault["id"]))
        return _WatcherScanOutcome(float(WATCHER_SCAN_RETRY_SECONDS))
    if result.get("already_running") or result.get("suspended") or result.get("stopped"):
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


def _enabled_vaults() -> list[dict[str, Any]]:
    with db() as connection:
        return connection.execute(
            f"{VAULT_ROW_SQL} WHERE v.lifecycle='active' ORDER BY v.id"
        ).fetchall()


class _WatcherHost:
    """Production Adapter of ``filesystem_watcher.WatcherHost`` (#795).

    Stateless and built once: every member resolves the storage name when it
    is used, so a patched ``app.storage.X`` is the one that runs.
    """

    __slots__ = ()

    @property
    def stable_seconds(self) -> float:
        return WATCHER_STABLE_SECONDS

    @property
    def gap_rescan_min_seconds(self) -> float:
        return WATCHER_GAP_RESCAN_MIN_SECONDS

    @property
    def recheck_seconds(self) -> float:
        return WATCHER_RECHECK_SECONDS

    @property
    def batch_max_failures(self) -> int:
        return WATCHER_BATCH_MAX_FAILURES

    @property
    def vanished_max_retries(self) -> int:
        return WATCHER_VANISHED_MAX_RETRIES

    @property
    def vanished_restart_seconds(self) -> float:
        return WATCHER_VANISHED_RESTART_SECONDS

    def restart_delay(self, streak: int) -> float:
        return _watcher_restart_delay(streak)

    def observe(self, *paths: Path, **options: Any) -> Any:
        return awatch(*paths, **options)

    def enabled_vaults(self) -> list[dict[str, Any]]:
        return _enabled_vaults()

    def vault_inactive(self, vault_id: int) -> bool:
        with db() as connection:
            current = connection.execute(
                "SELECT lifecycle FROM vaults WHERE id=%s", (vault_id,),
            ).fetchone()
        return bool(current and current["lifecycle"] != "active")

    def record_source_health(self, vault_id: int, **state: Any) -> None:
        _record_scan_component_health(vault_id, "source", **state)

    def unknown_directories(
        self, vault_id: int, root: Path, changes: set[tuple[Change, str]],
        handed_off: set[tuple[int, int, int]],
    ) -> set[tuple[Change, str]]:
        return _unknown_watcher_directories(vault_id, root, changes, handed_off)

    def apply_changes(
        self, vault: dict[str, Any], changes: set[tuple[Change, str]], *, wait: bool,
    ) -> int | None:
        return _apply_watcher_changes(vault, changes, wait=wait)

    def reconcile(self, vault: dict[str, Any]) -> Any:
        return _reconcile_vault_after_watcher(vault)

    def resolve_error(self, vault_id: int) -> None:
        _resolve_worker_error("source_watcher", vault_id)

    def report_error(self, exc: BaseException, vault_id: int) -> None:
        _report_worker_error("source_watcher", exc, vault_id)

    def report_unreadable(self, vault_id: int, paths: Any) -> None:
        with db() as connection:
            for relative in sorted(paths):
                report_problem(
                    connection, event="file_unreadable", vault_id=vault_id, path=relative,
                )

    def note_last_error(self, vault_id: int, message: str) -> None:
        with status_lock:
            status = runtime_status.setdefault(
                vault_id,
                {"scanning": False, "last_scan": None, "last_error": None},
            )
            status["last_error"] = message


watcher_host = _WatcherHost()

# The watcher lives in ``services.filesystem_watcher``. ``app.main`` starts the
# loop from here, and the existing tests reach the watcher by these names.
_filesystem_watch_filter = filesystem_watcher.watch_filter
_log_watcher_batch = filesystem_watcher.log_batch
_is_undecodable_path_error = filesystem_watcher.is_undecodable_path_error
_watcher_denied_directories = filesystem_watcher.denied_directories
_watcher_vanished_directories = filesystem_watcher.vanished_directories
_watch_root_signature = filesystem_watcher.root_signature


async def _watch_vault_filesystem(vault: dict[str, Any]) -> None:
    await filesystem_watcher.watch_vault(watcher_host, vault)


async def filesystem_watch_loop() -> None:
    """Keep one recursive filesystem watcher aligned with each enabled vault."""
    await filesystem_watcher.watch_loop(watcher_host)


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
        if shutdown_state.requested:
            return  # Not an outcome: the next start audits again (#696).
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
    except cloud_preflight_gate.CloudPreflightBusy:
        # Not an audit failure (#777): the last audit outcome stays recorded.
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
        runtime = settings
        if runtime.metadata_backup_interval_seconds <= 0:
            return
        # S3 round trips (System Bucket creation/verification) before a pooled
        # connection is held for the run (#734).
        object_store = metadata_backup_service.resolve_object_store()
        with db() as connection:
            result = metadata_backup_service.run_metadata_backup(
                connection,
                reason="scheduled",
                backup_dir=settings.metadata_backup_dir,
                object_store=object_store,
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


# While FrostVault's System Bucket is not created yet, try again this often.
SYSTEM_BUCKET_POLL_SECONDS = 15 * 60
# Unfinished Vault Bucket creations (#735) are looked at this often: each
# failed one is retried RETRY_SECONDS after its last attempt, and a verified
# one becomes active once its versioning has settled (a minute's precision).
VAULT_BUCKET_POLL_SECONDS = 60
# Whether a look at the billing export is due (#738): one database read.
BILLED_COST_POLL_SECONDS = 15 * 60


def ensure_system_bucket_once() -> None:
    """Create the System Bucket ahead of the first backup (#734).

    The Recovery Kit names it, so it should exist soon after an upgrade, not
    only after the next daily backup. Once created, only backups verify it
    again; an operator-supplied METADATA_BACKUP_BUCKET is left to them. With
    scheduled backups off, a manual backup creates it.
    """
    run_periodic_worker(_SYSTEM_BUCKET_WORKER)


def _ensure_system_bucket() -> Any:
    if (settings.metadata_backup_interval_seconds <= 0
            or not metadata_backup_service.off_host_backup_configured()
            or system_bucket_service.configured_bucket() is not None):
        return periodic_workers.NOT_RUN
    saved = system_bucket_service.DatabaseStore().load()
    if saved is None or not saved.ready:
        system_bucket_service.ensure()


_SYSTEM_BUCKET_WORKER = periodic_workers.PeriodicWorker(
    "system_bucket", (_ensure_system_bucket,), lambda: SYSTEM_BUCKET_POLL_SECONDS,
)


def read_billed_cost_once() -> None:
    """Look for and read the billing export when a look is due (#738).

    ``run_due`` records every export or S3 failure as a quiet status, never a
    Problem; only an unusable catalog lands here, and is only logged: the
    other workers already report it. Shutdown stops a read, like the
    CloudWatch sizes (#737), without recording it.
    """
    from .services import billed_cost

    try:
        billed_cost.run_due(should_stop=lambda: shutdown_state.requested)
    except Exception as exc:  # noqa: BLE001 - logged, retried at the next poll
        worker_error_store.log_worker_error("billed_cost", exc)


def ensure_vault_buckets_once() -> None:
    """Resume every due Vault Bucket creation (#735).

    A creation interrupted by a restart, or failed (IAM, network), continues
    on its saved name; each failure stays recorded on its Vault with a
    Problem. A verified Vault becomes active once its versioning has settled
    and gets its first scan on its own scan thread, never on this
    single-worker maintenance pool, where a full scan of a large adopted
    folder would hold every other maintenance task for hours.
    """
    from .services import vault_buckets

    errors: list[Exception] = []

    def failed(vault_id: int, exc: Exception) -> None:
        errors.append(exc)
        _report_worker_error("vault_buckets", exc, vault_id)

    try:
        states = vault_buckets.resume_all(on_error=failed)
    except Exception as exc:
        _report_worker_error("vault_buckets", exc)
        return
    if not errors:
        _note_worker_ok("vault_buckets")
    for state in states:
        # A Vault that ran without an escaping error closes its own
        # per-Vault internal-error Problem, opened under its vault_id.
        _note_worker_ok("vault_buckets", int(state["vault_id"]))
        if state["state"] == "ready" and not shutdown_state.requested:
            submit_first_scan(int(state["vault_id"]))


def submit_first_scan(vault_id: int) -> None:
    """Scan a Vault that just became active, on its own scan thread (#735).

    The per-Vault pool of the paired watcher scans: drained at shutdown, and
    scans of one Vault take turns. A scan already running or a stopping
    process leaves it to the periodic scan.
    """
    try:
        future = _submit_watcher_scan({"id": int(vault_id)})
    except WorkerStopping:
        return

    def reported(done: Future[Any]) -> None:
        exc = None if done.cancelled() else done.exception()
        if exc is not None and not isinstance(exc, WorkerStopping):
            _report_worker_error("source_scan", exc, int(vault_id))

    future.add_done_callback(reported)


def _verify_latest_metadata_backup_once() -> None:
    """Periodically restore-verify the newest succeeded backup run's bound artifact."""
    runtime = settings
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
        )
        if not result.get("ok"):
            raise metadata_backup_service.BackupError("api.metadata_backup.restore_not_ok")
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


# The background scheduler lives in ``services/background_scheduler.py``
# (#795). These names stay here for ``app.main`` and for what reads the
# schedule through storage.
MAINTENANCE_TASKS = background_scheduler.MAINTENANCE_TASKS
BACKGROUND_LOOP_OK_CYCLES = background_scheduler.BACKGROUND_LOOP_OK_CYCLES
initial_maintenance_marks = background_scheduler.initial_maintenance_marks


def persisted_maintenance_ages(
    *, now: datetime | None = None
) -> dict[str, float | None]:
    return background_scheduler.persisted_maintenance_ages(db, _parse_iso_datetime, now=now)


async def background_loop() -> None:
    await background_scheduler.run(scheduler_host)


async def _background_loop(loop: asyncio.AbstractEventLoop, wakeup: asyncio.Event) -> None:
    await background_scheduler.run_cycles(scheduler_host, loop, wakeup)


def _background_loop_closes_problem(clean: bool, streak: int) -> tuple[int, bool]:
    """The new clean-cycle streak, and whether the loop's problem closes now.

    A loop that works again closes its internal-error problem by itself, like
    every other worker (#496, #711), once per recovery: only while it is not
    already known to be healthy, so a clean cycle costs no database write.
    """
    streak = streak + 1 if clean else 0
    with _workers_known_ok_lock:
        known_ok = ("background_loop", None) in _workers_known_ok
    return streak, streak >= BACKGROUND_LOOP_OK_CYCLES and not known_ok


def _record_background_loop_error(exc: Exception) -> None:
    # Never include paths/provider messages in logs. The existing bounded error
    # store and counter own diagnostics for ordinary and fast-refill cycles.
    with _workers_known_ok_lock:
        _workers_known_ok.discard(("background_loop", None))
    try:
        with db() as connection:
            worker_error_store.record_worker_error(
                connection, component="background_loop", exc=exc,
            )
        metrics_service.inc("worker_errors_total")
    except Exception:
        pass
    metrics_service.set_gauge("worker_up", 0)


# The periodic workers the scheduler submits to the maintenance pool (#795),
# in the order of a cycle. Each runs off the loop, one run of it at a time;
# a failed run is the worker's own Problem and a clean one closes it.
_SCHEDULED_WORKERS = (
    # Elapsed Vault Deletions (#717).
    periodic_workers.PeriodicWorker(
        "vault_deletion", (lambda: vault_deletion_service.run_due(db, s3_client),),
        lambda: vault_deletion_service.POLL_SECONDS,
    ),
    # Superseded versions a Retention Lock held back (#736).
    periodic_workers.PeriodicWorker(
        "retained_versions", (lambda: retention_lock_service.run_due(db, s3_client),),
        lambda: retention_lock_service.POLL_SECONDS,
    ),
    # Automatic Duplicate Cleanup (#726): each Vault runs once a day from its
    # persisted last run; the poll only looks for due ones.
    periodic_workers.PeriodicWorker(
        "duplicate_cleanup", (lambda: duplicate_cleanup_service.run_due(db),),
        lambda: duplicate_cleanup_service.POLL_SECONDS,
    ),
    # Mirror Cleanup (#771): the same daily cadence, its own last run.
    periodic_workers.PeriodicWorker(
        "mirror_cleanup", (lambda: mirror_cleanup_service.run_due(db),),
        lambda: mirror_cleanup_service.POLL_SECONDS,
    ),
    _SYSTEM_BUCKET_WORKER,
    # Unfinished Vault Bucket creations resume soon after a start. Each
    # failure is recorded on its own Vault, inside the pass.
    periodic_workers.PeriodicWorker(
        "vault_buckets", (lambda: ensure_vault_buckets_once(),),
        lambda: VAULT_BUCKET_POLL_SECONDS, own_health=True,
    ),
    # CloudWatch sizes of the Vault Buckets (#737): each Vault is read once a
    # day from its persisted last read; the poll finds due ones. AWS errors
    # are recorded on their Vault and shown on its page, never a Problem; an
    # unexpected error is recorded on its Vault too (so it waits its retry
    # cadence) and reported as this worker's. A pass stops on shutdown, after
    # its time budget or at the first connection error.
    periodic_workers.PeriodicWorker(
        "bucket_metrics",
        (lambda: bucket_metrics_service.run_due(db, should_stop=lambda: shutdown_state.requested),),
        lambda: bucket_metrics_service.POLL_SECONDS,
    ),
    # Billed Cost (#738): the export is read at most once a day, from the
    # persisted last look; the poll only asks if one is due. Never a Problem:
    # the pass only logs.
    periodic_workers.PeriodicWorker(
        "billed_cost", (lambda: read_billed_cost_once(),),
        lambda: BILLED_COST_POLL_SECONDS, own_health=True,
    ),
)


class _SchedulerHost:
    """Production Adapter of ``background_scheduler.SchedulerHost`` (#795).

    Stateless and built once: every member resolves the storage name when it
    is used, so a patched ``app.storage.X`` is the one that runs.
    """

    __slots__ = ()

    workers = _SCHEDULED_WORKERS

    @property
    def directory_aggregate_seconds(self) -> float:
        return DIRECTORY_AGGREGATE_BATCH_SECONDS

    @property
    def backlog_observe_seconds(self) -> float:
        return BACKLOG_OBSERVE_SECONDS

    @property
    def notification_max_latency_seconds(self) -> float:
        return NOTIFICATION_MAX_LATENCY_SECONDS

    def maintenance_ages(self) -> Mapping[str, float | None]:
        return persisted_maintenance_ages()

    def forget_audit_retry(self) -> None:
        _note_audit_outcome(False)

    def completion_event(self, loop: asyncio.AbstractEventLoop) -> Any:
        return _operation_completion_event(loop)

    def dispatch_jobs(self) -> None:
        process_jobs_once(wait=False)

    def deliver_notifications(self) -> None:
        _deliver_notifications_once()

    def submit_directory_aggregates(self) -> None:
        _submit_directory_aggregates()

    def submit(self, name: str, func: Callable[[], Any]) -> None:
        _submit_maintenance(name, func)

    def run_worker(self, worker: periodic_workers.PeriodicWorker) -> None:
        run_periodic_worker(worker)

    def claimable_queue_depth(self, **filters: Any) -> int:
        return claimable_queue_depth(**filters)

    def observe_queue_stall(self, runnable: int) -> None:
        observe_queue_stall(runnable)

    def observe_vault_stalls(self) -> None:
        observe_vault_stalls()

    def observe_stuck_lease_jobs(self) -> tuple[int, float]:
        return observe_stuck_lease_jobs()

    def observe_failed_job_backlog(self) -> tuple[int, float]:
        return observe_failed_job_backlog()

    def scan_all_vaults(self) -> None:
        scan_all_vaults()

    def audit_all_vaults(self) -> None:
        audit_all_vaults()

    def audit_retry_due(self) -> bool:
        return audit_retry_due()

    def audit_vaults_with_error(self) -> None:
        audit_vaults_with_error()

    def run_scheduled_metadata_backup(self) -> None:
        _run_scheduled_metadata_backup_once()

    def verify_latest_metadata_backup(self) -> None:
        _verify_latest_metadata_backup_once()

    async def refill_until_deadline(self, wakeup: asyncio.Event, delay: float) -> None:
        await _refill_until_deadline(wakeup, delay)

    def record_error(self, exc: Exception) -> None:
        _record_background_loop_error(exc)

    def closes_problem(self, clean: bool, streak: int) -> tuple[int, bool]:
        return _background_loop_closes_problem(clean, streak)

    def note_ok(self) -> None:
        _note_worker_ok("background_loop")


scheduler_host = _SchedulerHost()
