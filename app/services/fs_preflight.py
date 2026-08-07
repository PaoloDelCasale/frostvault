"""POSIX vault filesystem readiness diagnostics.

Framework-agnostic: callers pass a vault root path. The checker never changes
ownership or modes; it only reports access, identity, and per-entry problems.

Filesystem health for ``/api/stats`` is cached in-process with a bounded
synopsis and single-flight background recomputation so summary metrics never
block on a full tree walk.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import metrics as metrics_service
from .s3_preflight import PreflightCheck

CheckStatus = str  # reused vocabulary: pass / fail / warn

# Bound the findings array returned on the hot stats path. Full progressive
# inspection belongs to the diagnostics detail flow (#222), not archive stats.
FINDINGS_SAMPLE_LIMIT = 25
# Coalesce repeated refresh signals: a current snapshot younger than this is
# reused without starting another walk.
HEALTH_CACHE_TTL_SECONDS = 300.0

HealthStatus = str  # checking | current | stale | failed


@dataclass(frozen=True)
class FilesystemFinding:
    path: str
    code: str
    message: str


@dataclass(frozen=True)
class FilesystemPreflightResult:
    root: str
    ok: bool
    uid: int
    gid: int
    checks: tuple[PreflightCheck, ...]
    findings: tuple[FilesystemFinding, ...]


@dataclass(frozen=True)
class FilesystemHealthSnapshot:
    """Bounded, cacheable filesystem-health synopsis for one Vault."""

    vault_id: int
    revision: int
    status: HealthStatus
    checked_at: str | None
    root: str
    ok: bool
    uid: int
    gid: int
    checks: tuple[dict[str, Any], ...]
    findings_sample: tuple[dict[str, Any], ...]
    finding_counts: dict[str, int]
    findings_total: int
    error: str | None = None
    cache_age_seconds: float | None = None


@dataclass
class _HealthCacheEntry:
    revision: int
    status: HealthStatus
    checked_at: str | None
    checked_mono: float | None
    root: str
    ok: bool
    uid: int
    gid: int
    checks: tuple[dict[str, Any], ...]
    findings_sample: tuple[dict[str, Any], ...]
    finding_counts: dict[str, int]
    findings_total: int
    error: str | None = None


@dataclass
class _InflightWork:
    thread: threading.Thread
    started_mono: float
    generation: int


_health_lock = threading.RLock()
_health_cache: dict[int, _HealthCacheEntry] = {}
_health_inflight: dict[int, _InflightWork] = {}
_health_generation: dict[int, int] = {}
_health_revision_seq: dict[int, int] = {}


def resolve_configured_vault_root(
    raw: str | Path,
    *,
    allowed_bases: Sequence[str | Path],
) -> Path | None:
    """Return a canonical vault root only when it stays under an allowed base.

    Uses realpath + prefix checks so callers never walk operator-unexpected
    paths derived from stored vault configuration.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    resolved = os.path.realpath(text)
    for base in allowed_bases:
        base_text = str(base or "").strip()
        if not base_text:
            continue
        base_real = os.path.realpath(base_text)
        if resolved == base_real or resolved.startswith(base_real + os.sep):
            return Path(resolved)
    return None


def check_vault_filesystem(
    root: str | Path,
    *,
    allowed_bases: Sequence[str | Path],
) -> FilesystemPreflightResult:
    """Inspect ``root`` for archive-safe local filesystem access.

    Reports vault-root read/write/execute access, the effective UID/GID,
    unreadable files, unwritable directories, and symbolic links. Does not
    follow directory symlinks when walking, and never mutates permissions.
    """
    root_path = resolve_configured_vault_root(root, allowed_bases=allowed_bases)
    uid = os.geteuid()
    gid = os.getegid()
    checks: list[PreflightCheck] = [
        PreflightCheck(
            code="fs.identity",
            status="pass",
            message=f"Effective identity is uid={uid} gid={gid}",
        )
    ]
    findings: list[FilesystemFinding] = []

    if root_path is None:
        checks.append(
            PreflightCheck(
                code="fs.root_access",
                status="fail",
                message="Vault root is outside the configured source roots",
                remediation=(
                    "Choose a vault directory that stays under a configured "
                    "source root"
                ),
            )
        )
        return FilesystemPreflightResult(
            root=str(root or ""),
            ok=False,
            uid=uid,
            gid=gid,
            checks=tuple(checks),
            findings=(),
        )

    if not root_path.exists() or not root_path.is_dir():
        checks.append(
            PreflightCheck(
                code="fs.root_access",
                status="fail",
                message=f"Vault root is not available: {root_path}",
                remediation=(
                    "Create the vault directory on the host and mount it at the "
                    "configured source root"
                ),
            )
        )
        return FilesystemPreflightResult(
            root=str(root_path),
            ok=False,
            uid=uid,
            gid=gid,
            checks=tuple(checks),
            findings=(),
        )

    missing: list[str] = []
    if not os.access(root_path, os.R_OK):
        missing.append("read")
    if not os.access(root_path, os.W_OK):
        missing.append("write")
    if not os.access(root_path, os.X_OK):
        missing.append("execute")

    if missing:
        needed = "/".join(missing)
        checks.append(
            PreflightCheck(
                code="fs.root_access",
                status="fail",
                message=f"Vault root lacks {needed} access for uid={uid} gid={gid}",
                remediation=(
                    f"Grant the archive user (PUID={uid}/PGID={gid}) {needed} "
                    "permission on the host vault directory without running the "
                    "container as root"
                ),
            )
        )
    else:
        checks.append(
            PreflightCheck(
                code="fs.root_access",
                status="pass",
                message="Vault root is readable, writable, and executable",
            )
        )

    for dirpath, dirnames, filenames in os.walk(
        root_path, topdown=True, followlinks=False, onerror=_walk_error_noop
    ):
        current = Path(dirpath)
        keep_dirs: list[str] = []
        for name in list(dirnames):
            entry = current / name
            relative = entry.relative_to(root_path).as_posix()
            if entry.is_symlink():
                findings.append(
                    FilesystemFinding(
                        path=relative,
                        code="fs.symlink",
                        message=f"Symbolic link rejected: {relative}",
                    )
                )
                continue
            if not os.access(entry, os.X_OK) or not os.access(entry, os.R_OK):
                findings.append(
                    FilesystemFinding(
                        path=relative,
                        code="fs.unreadable_file",
                        message=f"Directory is unreadable: {relative}",
                    )
                )
                continue
            if not os.access(entry, os.W_OK):
                findings.append(
                    FilesystemFinding(
                        path=relative,
                        code="fs.unwritable_directory",
                        message=f"Directory is not writable: {relative}",
                    )
                )
            # Descend only into real directories (never through symlinks).
            keep_dirs.append(name)
        dirnames[:] = keep_dirs

        for name in filenames:
            entry = current / name
            relative = entry.relative_to(root_path).as_posix()
            if entry.is_symlink():
                findings.append(
                    FilesystemFinding(
                        path=relative,
                        code="fs.symlink",
                        message=f"Symbolic link rejected: {relative}",
                    )
                )
                continue
            if not os.access(entry, os.R_OK):
                findings.append(
                    FilesystemFinding(
                        path=relative,
                        code="fs.unreadable_file",
                        message=f"File is unreadable: {relative}",
                    )
                )

    ok = all(check.status != "fail" for check in checks) and not findings
    if findings:
        checks.append(
            PreflightCheck(
                code="fs.entries",
                status="fail",
                message=f"{len(findings)} filesystem problem(s) under the vault root",
                remediation=(
                    "Fix host permissions for the reported paths or remove "
                    "symbolic links; the archive never changes ownership or modes"
                ),
            )
        )
    else:
        checks.append(
            PreflightCheck(
                code="fs.entries",
                status="pass",
                message="No unreadable files, unwritable directories, or symbolic links",
            )
        )

    return FilesystemPreflightResult(
        root=str(root_path),
        ok=ok,
        uid=uid,
        gid=gid,
        checks=tuple(checks),
        findings=tuple(findings),
    )


def reset_filesystem_health_cache_for_tests() -> None:
    """Clear process-local health cache and inflight work (tests only)."""
    with _health_lock:
        _health_cache.clear()
        _health_inflight.clear()
        _health_generation.clear()
        _health_revision_seq.clear()


def mark_vault_filesystem_health_stale(vault_id: int) -> None:
    """Mark a cached synopsis stale so the next ensure coalesces one refresh."""
    key = int(vault_id)
    with _health_lock:
        entry = _health_cache.get(key)
        if entry is None:
            return
        if entry.status == "current":
            entry.status = "stale"


def get_filesystem_health_snapshot(vault_id: int) -> FilesystemHealthSnapshot | None:
    """Return the current cached synopsis without starting a walk."""
    key = int(vault_id)
    with _health_lock:
        entry = _health_cache.get(key)
        if entry is None:
            return None
        return _snapshot_from_entry(key, entry)


def ensure_vault_filesystem_health(
    vault_id: int,
    *,
    source_root: str,
    allowed_bases: Sequence[str | Path],
    preflight_allowed: bool,
    force: bool = False,
    walker: Callable[..., FilesystemPreflightResult] | None = None,
    spawn: bool = True,
) -> FilesystemHealthSnapshot:
    """Return a bounded health synopsis, scheduling single-flight recompute.

    When ``preflight_allowed`` is false the Source Volume gate has already
    failed closed: no path resolution or ``os.walk`` runs. Otherwise a missing,
    stale, failed, or expired cache entry starts at most one background walk per
    Vault. Concurrent callers share that flight and observe ``checking`` or the
    previous ``stale`` synopsis immediately.
    """
    key = int(vault_id)
    now_mono = time.monotonic()
    check_fn = walker or check_vault_filesystem

    if not preflight_allowed:
        snapshot = _store_gated_snapshot(key, source_root=source_root)
        _publish_health_metrics(snapshot)
        return snapshot

    with _health_lock:
        entry = _health_cache.get(key)
        inflight = _health_inflight.get(key)
        inflight_alive = bool(inflight and inflight.thread.is_alive())

        if entry is not None and not force:
            age = (
                None
                if entry.checked_mono is None
                else max(0.0, now_mono - entry.checked_mono)
            )
            fresh = (
                entry.status == "current"
                and age is not None
                and age < HEALTH_CACHE_TTL_SECONDS
            )
            if fresh:
                snapshot = _snapshot_from_entry(key, entry, now_mono=now_mono)
                _publish_health_metrics(snapshot)
                return snapshot
            if entry.status == "current" and not inflight_alive:
                entry.status = "stale"

        if inflight_alive:
            if entry is None:
                entry = _placeholder_checking_entry(source_root)
                _health_cache[key] = entry
            elif entry.status == "current":
                entry.status = "stale"
            snapshot = _snapshot_from_entry(key, entry, now_mono=now_mono)
            _publish_health_metrics(snapshot)
            return snapshot

        if entry is None:
            entry = _placeholder_checking_entry(source_root)
            _health_cache[key] = entry
        elif entry.status == "current":
            entry.status = "stale"
        elif entry.status not in {"stale", "failed", "checking"}:
            entry.status = "checking"

        generation = _health_generation.get(key, 0) + 1
        _health_generation[key] = generation

        if not spawn:
            # Synchronous path used by tests and explicit refresh helpers.
            pass
        else:
            thread = threading.Thread(
                target=_run_health_recompute,
                name=f"fs-health-{key}",
                kwargs={
                    "vault_id": key,
                    "source_root": source_root,
                    "allowed_bases": tuple(str(base) for base in allowed_bases),
                    "generation": generation,
                    "walker": check_fn,
                },
                daemon=True,
            )
            _health_inflight[key] = _InflightWork(
                thread=thread,
                started_mono=now_mono,
                generation=generation,
            )
            thread.start()
            snapshot = _snapshot_from_entry(key, entry, now_mono=now_mono)
            _publish_health_metrics(snapshot)
            return snapshot

    # spawn=False: run inline after releasing the scheduling decision.
    _run_health_recompute(
        vault_id=key,
        source_root=source_root,
        allowed_bases=tuple(str(base) for base in allowed_bases),
        generation=generation,
        walker=check_fn,
    )
    snapshot = get_filesystem_health_snapshot(key)
    assert snapshot is not None
    _publish_health_metrics(snapshot)
    return snapshot


def build_stats_filesystem_payload(
    *,
    vault_id: int,
    source_root: str,
    allowed_bases: Sequence[str | Path],
    volume_alias: str | None,
    volume_health: str,
    local_operations_allowed: bool,
    cloud_catalog_allowed: bool,
    preflight_allowed: bool,
    scan_findings: Sequence[Mapping[str, Any]] | None = None,
    force_refresh: bool = False,
    walker: Callable[..., FilesystemPreflightResult] | None = None,
    spawn: bool = True,
) -> dict[str, Any]:
    """Assemble the ``filesystem`` object embedded in ``GET /api/stats``.

    Always returns quickly: expensive walks run through
    :func:`ensure_vault_filesystem_health` single-flight cache, never inline on
    the request thread when ``spawn`` is true.
    """
    snapshot = ensure_vault_filesystem_health(
        vault_id,
        source_root=source_root,
        allowed_bases=allowed_bases,
        preflight_allowed=preflight_allowed,
        force=force_refresh,
        walker=walker,
        spawn=spawn,
    )
    checks = [dict(check) for check in snapshot.checks]
    findings = [dict(item) for item in snapshot.findings_sample]
    finding_counts = dict(snapshot.finding_counts)
    findings_total = int(snapshot.findings_total)
    ok = bool(snapshot.ok)

    if not local_operations_allowed:
        ok = False
        source_check = {
            "code": f"source_volume.{volume_health}",
            "status": "fail",
            "message": (
                f"Local operations suspended for Source Volume "
                f"{volume_alias or 'unknown'} "
                f"({volume_health})"
            ),
            "remediation": (
                "Remount /sources/<alias> as a direct rw sibling, then run a "
                "full local scan before local operations resume. Nested mounts "
                "are unsupported."
            ),
        }
        if not any(check.get("code") == source_check["code"] for check in checks):
            checks.append(source_check)

    if scan_findings:
        known = {(item.get("path"), item.get("code")) for item in findings}
        for item in scan_findings:
            path = item.get("path")
            code = item.get("code")
            key = (path, code)
            if key in known:
                continue
            known.add(key)
            code_text = str(code or "fs.unknown")
            finding_counts[code_text] = finding_counts.get(code_text, 0) + 1
            findings_total += 1
            ok = False
            if len(findings) < FINDINGS_SAMPLE_LIMIT:
                findings.append(
                    {
                        "path": str(path or ""),
                        "code": code_text,
                        "message": str(item.get("message") or ""),
                    }
                )

    truncated = findings_total > len(findings)
    return {
        "ok": ok,
        "uid": snapshot.uid,
        "gid": snapshot.gid,
        "root": snapshot.root,
        "checks": checks,
        "findings": findings,
        "source_volume": {
            "alias": volume_alias,
            "health": volume_health,
            "local_operations_allowed": local_operations_allowed,
            "cloud_catalog_allowed": cloud_catalog_allowed,
        },
        "health_status": snapshot.status,
        "revision": snapshot.revision,
        "checked_at": snapshot.checked_at,
        "findings_total": findings_total,
        "finding_counts": finding_counts,
        "findings_truncated": truncated,
        "cache_age_seconds": snapshot.cache_age_seconds,
        "error": snapshot.error,
    }


def _run_health_recompute(
    *,
    vault_id: int,
    source_root: str,
    allowed_bases: Sequence[str],
    generation: int,
    walker: Callable[..., FilesystemPreflightResult],
) -> None:
    started = time.perf_counter()
    uid = os.geteuid()
    gid = os.getegid()
    try:
        safe_root = resolve_configured_vault_root(
            source_root, allowed_bases=allowed_bases
        )
        if safe_root is None:
            # Report missing under the first allowed base; never walk raw input.
            base = str(allowed_bases[0]) if allowed_bases else ""
            preflight_root = Path(
                f"{base.rstrip(chr(47))}/.missing-vault-root"
            )
        else:
            preflight_root = safe_root
        result = walker(preflight_root, allowed_bases=allowed_bases)
        snapshot_fields = _fields_from_preflight(result)
        status: HealthStatus = "current"
        error = None
    except Exception as exc:  # noqa: BLE001 - cache must record failure, not raise
        snapshot_fields = {
            "root": str(source_root or ""),
            "ok": False,
            "uid": uid,
            "gid": gid,
            "checks": (
                {
                    "code": "fs.health",
                    "status": "fail",
                    "message": "Filesystem health check failed",
                    "remediation": (
                        "Inspect host mounts and permissions; the archive never "
                        "changes ownership or modes"
                    ),
                },
            ),
            "findings_sample": (),
            "finding_counts": {},
            "findings_total": 0,
        }
        status = "failed"
        error = exc.__class__.__name__
        result = None

    duration = max(0.0, time.perf_counter() - started)
    checked_at = datetime.now(timezone.utc).isoformat()
    checked_mono = time.monotonic()

    with _health_lock:
        current_generation = _health_generation.get(vault_id, 0)
        inflight = _health_inflight.get(vault_id)
        if inflight is not None and inflight.generation == generation:
            _health_inflight.pop(vault_id, None)
        # A newer schedule superseded this flight: drop results.
        if generation != current_generation:
            return
        revision = _health_revision_seq.get(vault_id, 0) + 1
        _health_revision_seq[vault_id] = revision
        _health_cache[vault_id] = _HealthCacheEntry(
            revision=revision,
            status=status,
            checked_at=checked_at,
            checked_mono=checked_mono,
            root=str(snapshot_fields["root"]),
            ok=bool(snapshot_fields["ok"]),
            uid=int(snapshot_fields["uid"]),
            gid=int(snapshot_fields["gid"]),
            checks=tuple(snapshot_fields["checks"]),
            findings_sample=tuple(snapshot_fields["findings_sample"]),
            finding_counts=dict(snapshot_fields["finding_counts"]),
            findings_total=int(snapshot_fields["findings_total"]),
            error=error,
        )

    metrics_service.set_gauge(
        "filesystem_health_last_duration_seconds",
        duration,
        result=status,
    )
    metrics_service.set_gauge(
        "filesystem_health_findings",
        float(int(snapshot_fields["findings_total"])),
    )
    metrics_service.set_gauge(
        "filesystem_health_status",
        1.0,
        status=status,
    )


def _store_gated_snapshot(vault_id: int, *, source_root: str) -> FilesystemHealthSnapshot:
    """Record a definitive fail-closed synopsis without walking the tree."""
    uid = os.geteuid()
    gid = os.getegid()
    checked_at = datetime.now(timezone.utc).isoformat()
    checked_mono = time.monotonic()
    with _health_lock:
        revision = _health_revision_seq.get(vault_id, 0) + 1
        _health_revision_seq[vault_id] = revision
        entry = _HealthCacheEntry(
            revision=revision,
            status="current",
            checked_at=checked_at,
            checked_mono=checked_mono,
            root=str(source_root or ""),
            ok=False,
            uid=uid,
            gid=gid,
            checks=(
                {
                    "code": "fs.identity",
                    "status": "pass",
                    "message": f"Effective identity is uid={uid} gid={gid}",
                    "remediation": None,
                },
            ),
            findings_sample=(),
            finding_counts={},
            findings_total=0,
            error=None,
        )
        _health_cache[vault_id] = entry
        # Cancel any obsolete flight bookkeeping; gated volumes must not walk.
        _health_inflight.pop(vault_id, None)
        return _snapshot_from_entry(vault_id, entry, now_mono=checked_mono)


def _placeholder_checking_entry(source_root: str) -> _HealthCacheEntry:
    uid = os.geteuid()
    gid = os.getegid()
    return _HealthCacheEntry(
        revision=0,
        status="checking",
        checked_at=None,
        checked_mono=None,
        root=str(source_root or ""),
        ok=False,
        uid=uid,
        gid=gid,
        checks=(
            {
                "code": "fs.identity",
                "status": "pass",
                "message": f"Effective identity is uid={uid} gid={gid}",
                "remediation": None,
            },
        ),
        findings_sample=(),
        finding_counts={},
        findings_total=0,
        error=None,
    )


def _fields_from_preflight(result: FilesystemPreflightResult) -> dict[str, Any]:
    counts: dict[str, int] = {}
    sample: list[dict[str, Any]] = []
    for finding in result.findings:
        counts[finding.code] = counts.get(finding.code, 0) + 1
        if len(sample) < FINDINGS_SAMPLE_LIMIT:
            sample.append(
                {
                    "path": finding.path,
                    "code": finding.code,
                    "message": finding.message,
                }
            )
    checks = tuple(
        {
            "code": check.code,
            "status": check.status,
            "message": check.message,
            "remediation": check.remediation,
        }
        for check in result.checks
    )
    return {
        "root": result.root,
        "ok": result.ok,
        "uid": result.uid,
        "gid": result.gid,
        "checks": checks,
        "findings_sample": tuple(sample),
        "finding_counts": counts,
        "findings_total": len(result.findings),
    }


def _snapshot_from_entry(
    vault_id: int,
    entry: _HealthCacheEntry,
    *,
    now_mono: float | None = None,
) -> FilesystemHealthSnapshot:
    age = None
    if entry.checked_mono is not None:
        base = time.monotonic() if now_mono is None else now_mono
        age = max(0.0, base - entry.checked_mono)
    return FilesystemHealthSnapshot(
        vault_id=vault_id,
        revision=entry.revision,
        status=entry.status,
        checked_at=entry.checked_at,
        root=entry.root,
        ok=entry.ok,
        uid=entry.uid,
        gid=entry.gid,
        checks=entry.checks,
        findings_sample=entry.findings_sample,
        finding_counts=dict(entry.finding_counts),
        findings_total=entry.findings_total,
        error=entry.error,
        cache_age_seconds=age,
    )


def _publish_health_metrics(snapshot: FilesystemHealthSnapshot) -> None:
    if snapshot.cache_age_seconds is not None:
        metrics_service.set_gauge(
            "filesystem_health_cache_age_seconds",
            float(snapshot.cache_age_seconds),
        )
    metrics_service.set_gauge(
        "filesystem_health_findings",
        float(snapshot.findings_total),
    )
    metrics_service.set_gauge(
        "filesystem_health_status",
        1.0,
        status=snapshot.status,
    )


def _walk_error_noop(_error: OSError) -> None:
    """Continue walking when a directory cannot be listed."""
    return None
