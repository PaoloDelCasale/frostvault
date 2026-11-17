"""Bounded process-local scan/audit status; no background filesystem diagnostics."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import threading
import time
from typing import Any, Iterator


class VaultLock:
    """The per-Vault filesystem lock, which knows who holds it and since when (#759).

    It behaves like :class:`threading.Lock`; an acquisition may name its
    operation (a stable key such as ``full_scan``). The holder is in-memory
    only: it lets a waiting scan say what it waits for, and lets a check that
    runs in another thread notice background work holding a Vault too long.
    """

    DEFAULT_OPERATION = "vault_operation"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holder: tuple[str, float, float] | None = None

    def acquire(self, blocking: bool = True, timeout: float = -1, *,
                operation: str = DEFAULT_OPERATION) -> bool:
        acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            self._holder = (operation, time.time(), time.monotonic())
        return acquired

    def release(self) -> None:
        self._holder = None
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_exc: Any) -> None:
        self.release()

    @contextmanager
    def holding(self, operation: str) -> Iterator[None]:
        """Hold the lock, blocking, for the named operation."""
        self.acquire(operation=operation)
        try:
            yield
        finally:
            self.release()

    def relabel(self, operation: str) -> None:
        """Name the current step of the holder; it keeps its acquisition time."""
        holder = self._holder
        if holder is not None:
            self._holder = (operation, holder[1], holder[2])

    def holder(self) -> dict[str, Any] | None:
        """``{operation, since, since_monotonic}`` of the holder, or None."""
        holder = self._holder
        if holder is None:
            return None
        operation, wall, mono = holder
        return {"operation": operation,
                "since": datetime.fromtimestamp(wall, timezone.utc).isoformat(),
                "since_monotonic": mono}


# The operations that hold a Vault, each with its UI name (#759).
VAULT_OPERATIONS = frozenset({
    "full_scan", "auto_admission", "identify_local_copies", "watcher_changes",
    "free_space", "recovery", "stop_protection", "reconnect",
})


def vault_operation_key(operation: str | None) -> str:
    """The catalog key naming an operation that holds a Vault."""
    return f"vault.operation.{operation if operation in VAULT_OPERATIONS else 'other'}"


runtime_status: dict[int, dict[str, Any]] = {}
scan_locks: dict[int, VaultLock] = {}
status_lock = threading.Lock()

_SCALAR_KEYS = (
    "scanning", "last_scan", "last_error", "scan_id", "last_audit",
    "last_audit_healthy", "last_verified_at",
    # The running scan's phase and verification progress (#691).
    "scan_phase", "verify_files_total", "verify_files_done",
    "verify_bytes_total", "verify_bytes_done",
    # Bytes read by the hashing threads, partial files included (#759).
    "verify_bytes_read",
    # Traversal progress (#696): files seen, their rate, and the Local Copies a
    # previous scan found (None on a first adoption) for a time estimate.
    "traversal_files_seen", "traversal_files_per_minute", "traversal_files_expected",
    # Seconds of the traversal spent waiting for the Vault row and writing its
    # batches (#707); the rest is the walk, including the per-entry file checks
    # (#709).
    "traversal_lock_wait_seconds", "traversal_write_seconds", "traversal_probe_seconds",
    *(f"last_{kind}_{component}" for kind in ("error", "success")
      for component in ("source", "cloud", "policy", "audit")),
    # When each component's error was recorded, so a running scan shows an
    # older failure as the last scan's outcome (#691).
    *(f"last_error_{component}_at" for component in ("source", "cloud", "policy", "audit")),
)
_AUDIT_COUNTERS = (
    "catalog_versions", "cloud_versions", "missing_in_cloud", "missing_in_catalog",
    "storage_class_drift", "policy_tag_drift", "missing_delete_markers", "healthy",
    "command_ok", "incidents_opened", "incidents_resolved",
)


def scan_lock_for_vault(vault_id: int) -> VaultLock:
    with status_lock:
        return scan_locks.setdefault(int(vault_id), VaultLock())


def lock_holders() -> dict[int, dict[str, Any]]:
    """The holder of every Vault lock currently held (#759)."""
    with status_lock:
        locks = list(scan_locks.items())
    return {vault_id: holder for vault_id, lock in locks
            if (holder := lock.holder()) is not None}


def scan_waiting(vault_id: int) -> dict[str, Any]:
    """Whether a requested scan waits for the Vault, for what and since when.

    A full scan verifying fingerprints runs without the lock (#689): a scan
    queued behind it waits for that scan, which started at its ``scan_id``.
    """
    with status_lock:
        status = runtime_status.get(int(vault_id)) or {}
        queued_at = status.get("scan_queued_at")
        scanning, scan_id = bool(status.get("scanning")), status.get("scan_id")
        lock = scan_locks.get(int(vault_id))
    if not queued_at:
        return {"scan_queued": False, "scan_queued_at": None,
                "scan_waiting_for": None, "scan_waiting_since": None}
    holder = lock.holder() if lock is not None else None
    if holder is not None:
        waiting_for, since = holder["operation"], holder["since"]
    elif scanning:
        waiting_for, since = "full_scan", scan_id
    else:  # Free now: the scan is about to start.
        waiting_for, since = None, None
    return {"scan_queued": True, "scan_queued_at": queued_at,
            "scan_waiting_for": waiting_for, "scan_waiting_since": since}


def snapshot_runtime_status_for_stats(vault_id: int) -> dict[str, Any]:
    """Copy only known scalar fields under the producer lock, in constant space."""
    with status_lock:
        shared = runtime_status.get(int(vault_id)) or {}
        result: dict[str, Any] = {"scanning": False, "last_scan": None, "last_error": None}
        for key in _SCALAR_KEYS:
            value = shared.get(key)
            if value is None or isinstance(value, (bool, int, float)):
                if key in shared:
                    result[key] = value
            elif isinstance(value, str):
                result[key] = value[:512]
        report = shared.get("last_audit_report")
        if isinstance(report, dict):
            result["last_audit_report"] = {
                key: report[key] for key in _AUDIT_COUNTERS
                if key in report and isinstance(report[key], (bool, int, float))
            }
    # A requested scan waiting for the Vault, and what holds it (#759).
    result.update(scan_waiting(int(vault_id)))
    return result
