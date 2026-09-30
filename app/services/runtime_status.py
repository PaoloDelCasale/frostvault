"""Process-local scan/audit status shared by the worker and ``/api/stats``.

``runtime_status`` is mutated by scans, watchers and audits under
``status_lock``; ``snapshot_runtime_status_for_stats`` returns a bounded copy
so the request path never serialises unbounded producer state.
"""
from __future__ import annotations

import threading
from collections.abc import Mapping
from itertools import islice
from typing import Any

from .fs_preflight import (
    FINDING_COUNTS_UNKNOWN_KEY_BUDGET,
    FINDINGS_SAMPLE_LIMIT,
    KNOWN_FINDING_COUNT_CODES,
    bound_mapping_key_name,
    bound_runtime_filesystem_synopsis,
    bound_synopsis_text,
    normalize_finding_counts,
)


runtime_status: dict[int, dict[str, Any]] = {}


scan_locks: dict[int, threading.Lock] = {}


status_lock = threading.Lock()


def scan_lock_for_vault(vault_id: int) -> threading.Lock:
    """Return the process-wide scan/relocation lock for one Vault."""
    with status_lock:
        return scan_locks.setdefault(int(vault_id), threading.Lock())


def _empty_scan_filesystem_status(*, ok: bool) -> dict[str, Any]:
    """Bounded scan-time filesystem synopsis stored on ``runtime_status``."""
    return {
        "ok": ok,
        "uid": None,
        "gid": None,
        "checks": [],
        # Sample only — full progressive detail is not kept on the hot path.
        "findings": [],
        "findings_total": 0,
        "finding_counts": {},
        "findings_truncated": False,
        "synopsis_truncated": False,
    }


# Schema-bound counters produced by ``audit_vault_catalog``.
_AUDIT_REPORT_KNOWN_KEYS: tuple[str, ...] = (
    "catalog_versions",
    "cloud_versions",
    "missing_in_cloud",
    "missing_in_catalog",
    "storage_class_drift",
    "policy_tag_drift",
    "missing_delete_markers",
    "healthy",
    "command_ok",
    "incidents_opened",
    "incidents_resolved",
)


# Top-level runtime_status keys copied into /api/stats (never dict(shared)).
_RUNTIME_STATUS_SCALAR_KEYS: tuple[str, ...] = (
    "scanning",
    "last_scan",
    "last_error",
    "scan_id",
    "last_audit",
    "last_audit_healthy",
    "last_verified_at",
    "last_error_source",
    "last_error_cloud",
    "last_error_policy",
    "last_error_audit",
    "last_success_source",
    "last_success_cloud",
    "last_success_policy",
    "last_success_audit",
)


_RUNTIME_STATUS_KNOWN_KEYS: tuple[str, ...] = (
    *_RUNTIME_STATUS_SCALAR_KEYS,
    "last_audit_report",
)


# Bound any unexpected top-level keys for forward-compatible responses.
_RUNTIME_STATUS_UNKNOWN_KEY_BUDGET = 16


# Mapping/list budgets for schema-supported nested values (audit report).
_RUNTIME_STATUS_MAPPING_KEY_BUDGET = 32


_RUNTIME_STATUS_LIST_ITEM_BUDGET = 16


# Explicit depth budget — never recursive unbounded deepcopy.
_RUNTIME_STATUS_MAX_DEPTH = 2


_RUNTIME_STATUS_MAX_STRING_CHARS = 512


def _runtime_scalar_snapshot(value: Any) -> tuple[Any, bool, bool]:
    """Return ``(detached_value, supported, truncated)`` for JSON-safe scalars.

    Oversized strings are clipped and reported as ``truncated=True`` so callers
    can set a truthful runtime truncation marker. Unsupported types return
    ``supported=False`` without invoking producer ``__str__``.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value, True, False
    if isinstance(value, str):
        if len(value) > _RUNTIME_STATUS_MAX_STRING_CHARS:
            return value[:_RUNTIME_STATUS_MAX_STRING_CHARS], True, True
        return value, True, False
    return None, False, False


def _bound_last_audit_report(raw: Any) -> tuple[dict[str, Any] | None, bool]:
    """Detach a bounded audit-report mapping; signal truncation fail-closed.

    Known counter keys are read by exact lookup. A hard key budget covers a few
    forward-compatible unknown *scalar* counters. Nested mappings/lists and
    self-references are never walked recursively. Key names use
    :func:`bound_mapping_key_name` — never bare ``str(key)``.
    """
    if raw is None:
        return None, False
    if not isinstance(raw, Mapping):
        return None, True

    out: dict[str, Any] = {}
    truncated = False
    known = frozenset(_AUDIT_REPORT_KNOWN_KEYS)

    for key in _AUDIT_REPORT_KNOWN_KEYS:
        try:
            present = key in raw
        except Exception:
            present = False
        if not present:
            continue
        try:
            value = raw.get(key)  # type: ignore[attr-defined]
        except Exception:
            value = None
        detached, supported, value_truncated = _runtime_scalar_snapshot(value)
        if not supported:
            truncated = True
            continue
        if value_truncated:
            truncated = True
        out[key] = detached

    examine_limit = len(_AUDIT_REPORT_KNOWN_KEYS) + _RUNTIME_STATUS_MAPPING_KEY_BUDGET
    try:
        item_view = raw.items()
    except Exception:
        item_view = ()

    for key, value in islice(item_view, examine_limit):
        name = bound_mapping_key_name(key)
        if name is None:
            truncated = True
            continue
        if name in known or name in out:
            continue
        detached, supported, value_truncated = _runtime_scalar_snapshot(value)
        if not supported:
            truncated = True
            continue
        if value_truncated:
            truncated = True
        if len(out) >= _RUNTIME_STATUS_MAPPING_KEY_BUDGET:
            truncated = True
            break
        out[name] = detached

    try:
        raw_len = int(len(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raw_len = None
    if raw_len is not None and raw_len > examine_limit:
        truncated = True
    if truncated:
        # Leave room for the marker even when the scalar budget is saturated.
        out["truncated"] = True
    return out, truncated


def _bound_unknown_runtime_value(value: Any, *, depth: int) -> tuple[Any, bool, bool]:
    """Detach a forward-compatible unknown value under explicit budgets.

    Returns ``(detached, supported, truncated)``. Unsupported nested shapes are
    omitted (caller signals overflow). Depth and item budgets prevent O(n)
    walks and self-referential blow-ups; there is no generic deepcopy path.
    """
    detached, supported, truncated = _runtime_scalar_snapshot(value)
    if supported:
        return detached, True, truncated
    if depth >= _RUNTIME_STATUS_MAX_DEPTH:
        return None, False, False
    # Nested unknowns are fail-closed: do not serialize arbitrary graphs.
    if isinstance(value, Mapping) or (
        isinstance(value, (list, tuple)) and not isinstance(value, (str, bytes))
    ):
        return None, False, False
    return None, False, False


def snapshot_runtime_status_for_stats(vault_id: int) -> dict[str, Any]:
    """Consistent bounded runtime snapshot for ``GET /api/stats``.

    Lock ownership: the entire detached object graph is built under
    ``status_lock`` (known scalars, schema-bound ``last_audit_report``,
    bounded filesystem synopsis, and a hard budget of unknown *scalar*
    extras). Nested producer containers are never shared by reference.
    Callers must not perform DB/FS work while this lock is held;
    merge/response assembly happens after release on the detached values only.
    """
    with status_lock:
        shared = runtime_status.get(int(vault_id))
        if not shared:
            return {
                "scanning": False,
                "last_scan": None,
                "last_error": None,
                "filesystem": bound_runtime_filesystem_synopsis(None),
            }
        runtime: dict[str, Any] = {}
        truncated = False
        known = frozenset(_RUNTIME_STATUS_KNOWN_KEYS)

        for key in _RUNTIME_STATUS_SCALAR_KEYS:
            if key not in shared:
                continue
            detached, supported, value_truncated = _runtime_scalar_snapshot(
                shared.get(key)
            )
            if supported:
                runtime[key] = detached
                if value_truncated:
                    truncated = True
            else:
                truncated = True

        if "last_audit_report" in shared:
            report, report_truncated = _bound_last_audit_report(
                shared.get("last_audit_report")
            )
            if report is not None:
                runtime["last_audit_report"] = report
            if report_truncated or report is None:
                truncated = True

        # Forward-compatible extras: hard budget, scalars only, never by ref.
        extras = 0
        try:
            items = shared.items()
        except Exception:
            items = ()
        examine_limit = (
            len(_RUNTIME_STATUS_KNOWN_KEYS) + _RUNTIME_STATUS_UNKNOWN_KEY_BUDGET
        )
        for key, value in islice(items, examine_limit):
            if key == "filesystem":
                continue
            name = bound_mapping_key_name(key)
            if name is None:
                # Refuse arbitrary key objects (no str(key) / __str__ call).
                truncated = True
                continue
            if name in runtime or name in known:
                continue
            if extras >= _RUNTIME_STATUS_UNKNOWN_KEY_BUDGET:
                truncated = True
                break
            detached, supported, value_truncated = _bound_unknown_runtime_value(
                value, depth=0
            )
            if not supported:
                truncated = True
                continue
            if value_truncated:
                truncated = True
            runtime[name] = detached
            extras += 1

        try:
            shared_len = int(len(shared))
        except (TypeError, ValueError):
            shared_len = None
        if shared_len is not None and shared_len > examine_limit:
            truncated = True

        raw_fs = shared.get("filesystem")
        filesystem = bound_runtime_filesystem_synopsis(
            raw_fs if isinstance(raw_fs, Mapping) else None
        )
        runtime["filesystem"] = filesystem
        # Propagate synopsis/field/checks truncation to the top-level runtime marker.
        if filesystem.get("synopsis_truncated") is True:
            truncated = True
        if truncated:
            runtime["runtime_truncated"] = True
        return runtime


def _record_scan_finding(
    vault_id: int, *, path: str, code: str, message: str
) -> None:
    """Record one scan-time finding into a bounded runtime synopsis.

    Keeps accurate totals/code counts while capping the sample so ``/api/stats``
    never has to merge an unbounded findings list. Mutates owned containers in
    place under ``status_lock`` — never ``dict()``/full ``list()`` copies of
    legacy oversized mappings.
    """
    with status_lock:
        status = runtime_status.setdefault(
            vault_id,
            {"scanning": False, "last_scan": None, "last_error": None},
        )
        filesystem = status.setdefault(
            "filesystem",
            _empty_scan_filesystem_status(ok=False),
        )
        findings_raw = filesystem.get("findings")
        if isinstance(findings_raw, list):
            if len(findings_raw) > FINDINGS_SAMPLE_LIMIT:
                # Bound in O(limit) — drop the oversized tail without walking it.
                findings = findings_raw[:FINDINGS_SAMPLE_LIMIT]
                filesystem["findings"] = findings
            else:
                findings = findings_raw
        else:
            findings = []
            filesystem["findings"] = findings

        counts_raw = filesystem.get("finding_counts")
        counts_cap = len(KNOWN_FINDING_COUNT_CODES) + FINDING_COUNTS_UNKNOWN_KEY_BUDGET
        if isinstance(counts_raw, dict) and len(counts_raw) <= counts_cap:
            counts = counts_raw
        else:
            counts = normalize_finding_counts(
                counts_raw if isinstance(counts_raw, Mapping) else None
            )
            filesystem["finding_counts"] = counts

        try:
            total = int(filesystem.get("findings_total") or 0)
        except (TypeError, ValueError):
            total = 0
        if total < 0:
            total = 0
        synopsis_truncated = bool(filesystem.get("synopsis_truncated"))
        path_text, path_trunc = bound_synopsis_text(path)
        synopsis_truncated = synopsis_truncated or path_trunc
        if code is None or code == "":
            code_text = "fs.unknown"
        else:
            code_text, code_trunc = bound_synopsis_text(code)
            synopsis_truncated = synopsis_truncated or code_trunc
            if not code_text:
                code_text = "fs.unknown"
                synopsis_truncated = True
        message_text, message_trunc = bound_synopsis_text(message)
        synopsis_truncated = synopsis_truncated or message_trunc
        total += 1
        counts[code_text] = int(counts.get(code_text, 0) or 0) + 1
        if len(findings) < FINDINGS_SAMPLE_LIMIT:
            findings.append(
                {
                    "path": path_text,
                    "code": code_text,
                    "message": message_text,
                }
            )
        filesystem["findings_total"] = total
        filesystem["findings_truncated"] = total > len(findings)
        if synopsis_truncated:
            filesystem["synopsis_truncated"] = True
        filesystem["ok"] = False
