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
    *(f"last_{kind}_{component}" for kind in ("error", "success")
      for component in ("source", "cloud", "policy", "audit")),
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
