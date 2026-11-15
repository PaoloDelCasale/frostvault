"""Bounded process-local scan/audit status; no background filesystem diagnostics."""
from __future__ import annotations

import threading
from typing import Any

runtime_status: dict[int, dict[str, Any]] = {}
scan_locks: dict[int, threading.Lock] = {}
status_lock = threading.Lock()

_SCALAR_KEYS = (
    "scanning", "last_scan", "last_error", "scan_id", "last_audit",
    "last_audit_healthy", "last_verified_at",
    # The running scan's phase and verification progress (#691).
    "scan_phase", "verify_files_total", "verify_files_done",
    "verify_bytes_total", "verify_bytes_done",
    # Traversal progress (#696): files seen, their rate, and the Local Copies a
    # previous scan found (None on a first adoption) for a time estimate.
    "traversal_files_seen", "traversal_files_per_minute", "traversal_files_expected",
    # Seconds of the traversal spent waiting for the Vault row and writing its
    # batches (#707); the rest is the walk.
    "traversal_lock_wait_seconds", "traversal_write_seconds",
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


def scan_lock_for_vault(vault_id: int) -> threading.Lock:
    with status_lock:
        return scan_locks.setdefault(int(vault_id), threading.Lock())


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
        return result
