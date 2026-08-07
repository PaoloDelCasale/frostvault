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
# spawn=False may wait this long for an active Vault walker before giving up
# on the inline path. On timeout the live owner is preserved — different-config
# requests are queued as pending replacements; same-config requests return a
# bounded synopsis without queueing a redundant walk. Never overlap walkers.
HEALTH_INLINE_JOIN_TIMEOUT_SECONDS = 120.0

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
    config_key: str
    error: str | None = None


@dataclass
class _InflightWork:
    """Single-flight owner for one Vault (background thread or inline caller).

    Liveness is ``finished`` (not ``thread.is_alive()``): inline owners run on
    long-lived request threads, so join/wait must use the completion event.
    ``thread`` remains for diagnostics and crashed-background reaping.
    """

    thread: threading.Thread
    started_mono: float
    generation: int
    config_key: str
    finished: threading.Event


@dataclass
class _PendingReplacement:
    """Latest config waiting for the active Vault walker to finish."""

    source_root: str
    allowed_bases: tuple[str, ...]
    config_key: str
    generation: int
    walker: Callable[..., FilesystemPreflightResult]


_health_lock = threading.RLock()
_health_cache: dict[int, _HealthCacheEntry] = {}
_health_inflight: dict[int, _InflightWork] = {}
_health_pending: dict[int, _PendingReplacement] = {}
_health_generation: dict[int, int] = {}
_health_revision_seq: dict[int, int] = {}


def _health_config_key(
    *,
    source_root: str,
    allowed_bases: Sequence[str | Path],
    preflight_allowed: bool,
) -> str:
    """Bind cache/inflight validity to the effective Vault health inputs.

    Fresh hits require an exact match on source root, allowed bases, and whether
    the Source Volume gate permits preflight. A relocation, base-list change, or
    fail-closed transition therefore cannot reuse a prior synopsis.
    """
    # Use normpath only — never realpath/resolve. Fail-closed Source Volume
    # gates forbid resolving the configured root, and cache keys must not
    # reintroduce that side effect on the stats path.
    bases = tuple(
        sorted(
            {
                os.path.normpath(str(base).strip())
                for base in allowed_bases
                if str(base or "").strip()
            }
        )
    )
    root = str(source_root or "").strip()
    root_key = os.path.normpath(root) if root else ""
    gate = "allowed" if preflight_allowed else "gated"
    return f"{gate}\0{root_key}\0" + "\0".join(bases)


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
        _health_pending.clear()
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
    stale, failed, expired, or config-mismatched cache entry starts at most one
    background walk per Vault. Concurrent ``spawn=True`` callers share that
    flight and observe ``checking`` or the previous ``stale`` synopsis
    immediately. A ``spawn=False`` caller with an active same-config flight
    waits up to ``HEALTH_INLINE_JOIN_TIMEOUT_SECONDS`` for the owner and returns
    the completed synopsis (or a bounded view on timeout) without starting
    overlap.

    Config changes under an active walker never start a second overlapping walk:
    generation advances (fail-closed writeback suppression) and the latest
    config is queued as a pending replacement that chains after the current
    flight finishes.
    """
    key = int(vault_id)
    now_mono = time.monotonic()
    check_fn = walker or check_vault_filesystem
    config_key = _health_config_key(
        source_root=source_root,
        allowed_bases=allowed_bases,
        preflight_allowed=preflight_allowed,
    )

    if not preflight_allowed:
        snapshot = _store_gated_snapshot(
            key,
            source_root=source_root,
            config_key=config_key,
        )
        _publish_health_metrics(snapshot)
        return snapshot

    join_before_inline: _InflightWork | None = None
    join_same_config: _InflightWork | None = None
    inline_reserved = False
    bases_tuple = tuple(str(base) for base in allowed_bases)

    with _health_lock:
        entry = _health_cache.get(key)
        _reap_dead_inflight_locked(key)
        inflight = _health_inflight.get(key)
        inflight_alive = inflight is not None and _inflight_is_live(inflight)
        current_generation = _health_generation.get(key, 0)
        # A live flight is only shareable when it still matches both config and
        # the current generation (interim churn advances generation).
        same_inflight = bool(
            inflight_alive
            and inflight is not None
            and inflight.config_key == config_key
            and inflight.generation == current_generation
        )

        if entry is not None and not force:
            age = (
                None
                if entry.checked_mono is None
                else max(0.0, now_mono - entry.checked_mono)
            )
            config_matches = entry.config_key == config_key
            fresh = (
                config_matches
                and entry.status == "current"
                and age is not None
                and age < HEALTH_CACHE_TTL_SECONDS
            )
            if fresh:
                snapshot = _snapshot_from_entry(key, entry, now_mono=now_mono)
                _publish_health_metrics(snapshot)
                return snapshot
            if entry.status == "current" and config_matches and not same_inflight:
                entry.status = "stale"
            elif entry.status == "current" and not config_matches:
                # Prior synopsis is for a different root/bases/gate — do not
                # present it as authoritative for the new configuration.
                entry.status = "stale"

        if same_inflight:
            if entry is None or entry.config_key != config_key:
                entry = _placeholder_checking_entry(source_root, config_key=config_key)
                _health_cache[key] = entry
            elif entry.status == "current":
                entry.status = "stale"
            if spawn:
                # Async callers share the live flight immediately.
                snapshot = _snapshot_from_entry(key, entry, now_mono=now_mono)
                _publish_health_metrics(snapshot)
                return snapshot
            # spawn=False: bounded synchronous wait for the same-config owner.
            # Do not advance generation or queue pending — the owner already
            # computes this config. Capture the flight and join outside the lock.
            join_same_config = inflight
        else:
            # Authoritative cache view tracks the requested config immediately.
            if entry is None or entry.config_key != config_key:
                entry = _placeholder_checking_entry(source_root, config_key=config_key)
                _health_cache[key] = entry
            elif entry.status == "current":
                entry.status = "stale"
            elif entry.status not in {"stale", "failed", "checking"}:
                entry.status = "checking"

            if inflight_alive:
                # Strict single-flight: chain after the active walker. Advance
                # generation so the old flight cannot write back, and coalesce
                # repeated churn onto the latest pending config.
                pending = _health_pending.get(key)
                if (
                    pending is not None
                    and pending.config_key == config_key
                    and pending.generation == current_generation
                ):
                    snapshot = _snapshot_from_entry(key, entry, now_mono=now_mono)
                    _publish_health_metrics(snapshot)
                    return snapshot

                generation = current_generation + 1
                _health_generation[key] = generation
                if spawn:
                    _health_pending[key] = _PendingReplacement(
                        source_root=str(source_root),
                        allowed_bases=bases_tuple,
                        config_key=config_key,
                        generation=generation,
                        walker=check_fn,
                    )
                    snapshot = _snapshot_from_entry(key, entry, now_mono=now_mono)
                    _publish_health_metrics(snapshot)
                    return snapshot

                # spawn=False must still avoid overlapping walks: wait for the
                # active flight outside the lock, then claim the slot inline.
                _health_pending.pop(key, None)
                join_before_inline = inflight
            else:
                _health_pending.pop(key, None)
                generation = current_generation + 1
                _health_generation[key] = generation

                if spawn:
                    _start_health_flight(
                        vault_id=key,
                        source_root=str(source_root),
                        allowed_bases=bases_tuple,
                        generation=generation,
                        config_key=config_key,
                        walker=check_fn,
                        started_mono=now_mono,
                    )
                    snapshot = _snapshot_from_entry(key, entry, now_mono=now_mono)
                    _publish_health_metrics(snapshot)
                    return snapshot

                # Free slot: atomically reserve inline ownership before unlock so a
                # concurrent spawn=True cannot start an overlapping walker.
                _book_inline_flight_locked(
                    vault_id=key,
                    generation=generation,
                    config_key=config_key,
                    started_mono=now_mono,
                )
                inline_reserved = True

    # spawn=False + same-config owner: wait for completion (or timeout) without
    # starting a second overlapping walk of the same inputs.
    if join_same_config is not None:
        return _await_same_config_inflight(
            vault_id=key,
            source_root=str(source_root),
            allowed_bases=bases_tuple,
            config_key=config_key,
            walker=check_fn,
            work=join_same_config,
        )

    # spawn=False: run inline only with reserved ownership. After a join timeout
    # the live owner keeps the slot; queue/coalesce instead of overlapping.
    if join_before_inline is not None:
        if not _wait_for_inflight_before_inline(join_before_inline):
            bounded = _queue_after_inline_join_timeout(
                vault_id=key,
                source_root=str(source_root),
                allowed_bases=bases_tuple,
                config_key=config_key,
                generation=generation,
                walker=check_fn,
            )
            if bounded is not None:
                return bounded
        claimed_generation, bounded = _claim_inline_flight(
            vault_id=key,
            source_root=str(source_root),
            allowed_bases=bases_tuple,
            config_key=config_key,
            generation=generation,
            walker=check_fn,
        )
        if bounded is not None:
            return bounded
        assert claimed_generation is not None
        generation = claimed_generation
        inline_reserved = True

    assert inline_reserved
    try:
        _run_health_recompute(
            vault_id=key,
            source_root=str(source_root),
            allowed_bases=bases_tuple,
            generation=generation,
            config_key=config_key,
            walker=check_fn,
        )
    finally:
        # Idempotent: normal completion already released inside recompute.
        with _health_lock:
            _release_flight_if_owner_locked(key, generation)

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


def _inflight_is_live(work: _InflightWork) -> bool:
    """True while the flight still owns the single-flight slot."""
    if work.finished.is_set():
        return False
    # Background workers are dedicated ``fs-health-*`` threads; crash ⇒ not live.
    # Inline owners run on long-lived request threads — only ``finished`` counts.
    if work.thread.name.startswith("fs-health-"):
        return work.thread.is_alive()
    return True


def _reap_dead_inflight_locked(vault_id: int) -> None:
    """Drop crashed owners and promote pending. Caller holds ``_health_lock``."""
    inflight = _health_inflight.get(vault_id)
    if inflight is None or _inflight_is_live(inflight):
        return
    inflight.finished.set()
    _health_inflight.pop(vault_id, None)
    _start_pending_replacement_locked(vault_id)


def _release_flight_if_owner_locked(vault_id: int, generation: int) -> bool:
    """Release the slot when *generation* still owns it. Caller holds lock."""
    inflight = _health_inflight.get(vault_id)
    if inflight is None or inflight.generation != generation:
        return False
    inflight.finished.set()
    _health_inflight.pop(vault_id, None)
    _start_pending_replacement_locked(vault_id)
    return True


def _book_inline_flight_locked(
    *,
    vault_id: int,
    generation: int,
    config_key: str,
    started_mono: float | None = None,
) -> None:
    """Reserve the single-flight slot for the calling thread. Holds lock."""
    _health_inflight[vault_id] = _InflightWork(
        thread=threading.current_thread(),
        started_mono=time.monotonic() if started_mono is None else started_mono,
        generation=generation,
        config_key=config_key,
        finished=threading.Event(),
    )


def _coalesce_pending_locked(
    *,
    vault_id: int,
    source_root: str,
    allowed_bases: tuple[str, ...],
    config_key: str,
    generation: int,
    walker: Callable[..., FilesystemPreflightResult],
) -> int:
    """Install/replace pending for *config_key*. Returns the pending generation."""
    current_generation = _health_generation.get(vault_id, 0)
    pending = _health_pending.get(vault_id)
    if (
        pending is not None
        and pending.config_key == config_key
        and pending.generation == current_generation
    ):
        return current_generation
    if generation == current_generation:
        gen_for_pending = generation
    else:
        gen_for_pending = current_generation + 1
        _health_generation[vault_id] = gen_for_pending
    _health_pending[vault_id] = _PendingReplacement(
        source_root=source_root,
        allowed_bases=allowed_bases,
        config_key=config_key,
        generation=gen_for_pending,
        walker=walker,
    )
    return gen_for_pending


def _bounded_snapshot_for_config_locked(
    *,
    vault_id: int,
    source_root: str,
    config_key: str,
    now_mono: float,
) -> FilesystemHealthSnapshot:
    entry = _health_cache.get(vault_id)
    if entry is None or entry.config_key != config_key:
        entry = _placeholder_checking_entry(source_root, config_key=config_key)
        _health_cache[vault_id] = entry
    snapshot = _snapshot_from_entry(vault_id, entry, now_mono=now_mono)
    _publish_health_metrics(snapshot)
    return snapshot


def _wait_for_inflight_before_inline(work: _InflightWork) -> bool:
    """Wait up to the inline join budget for an active flight to finish.

    Returns True when the prior owner released (safe to claim inline).
    Returns False on timeout while the prior owner is still live.
    """
    return bool(work.finished.wait(timeout=HEALTH_INLINE_JOIN_TIMEOUT_SECONDS))


def _await_same_config_inflight(
    *,
    vault_id: int,
    source_root: str,
    allowed_bases: tuple[str, ...],
    config_key: str,
    walker: Callable[..., FilesystemPreflightResult],
    work: _InflightWork,
) -> FilesystemHealthSnapshot:
    """Bounded wait for a live same-config owner without starting overlap.

    When the owner finishes within the join budget, return its completed
    synopsis (``current`` / ``failed``). On timeout while the owner is still
    live, return the bounded cache view and do **not** queue a redundant
    same-config pending walk. If the slot is free without a terminal result
    (suppressed writeback / crash), claim inline ownership and recompute.
    """
    _wait_for_inflight_before_inline(work)

    inline_generation: int | None = None
    now_mono = time.monotonic()
    with _health_lock:
        _reap_dead_inflight_locked(vault_id)
        entry = _health_cache.get(vault_id)
        inflight = _health_inflight.get(vault_id)
        inflight_alive = inflight is not None and _inflight_is_live(inflight)

        # Owner produced a terminal synopsis for this config — surface it.
        if (
            entry is not None
            and entry.config_key == config_key
            and entry.status in {"current", "failed"}
        ):
            snapshot = _snapshot_from_entry(vault_id, entry, now_mono=now_mono)
            _publish_health_metrics(snapshot)
            return snapshot

        if inflight_alive:
            # Still live after timeout, or a different owner holds the slot
            # after churn. Return bounded state without overlapping and
            # without queueing same-config work the live owner may already
            # be computing.
            if entry is not None and entry.config_key == config_key:
                snapshot = _snapshot_from_entry(vault_id, entry, now_mono=now_mono)
                _publish_health_metrics(snapshot)
                return snapshot
            # Cache tracks another config — do not clobber it; return an
            # ephemeral checking snapshot for the caller's requested config.
            ephemeral = _placeholder_checking_entry(
                source_root, config_key=config_key
            )
            snapshot = _snapshot_from_entry(
                vault_id, ephemeral, now_mono=now_mono
            )
            _publish_health_metrics(snapshot)
            return snapshot

        # Slot free without a terminal result for this config: recover via
        # reserved inline recompute (generation-gated writeback).
        if entry is None or entry.config_key != config_key:
            entry = _placeholder_checking_entry(source_root, config_key=config_key)
            _health_cache[vault_id] = entry
        elif entry.status == "current":
            entry.status = "stale"
        elif entry.status not in {"stale", "failed", "checking"}:
            entry.status = "checking"

        current_generation = _health_generation.get(vault_id, 0)
        inline_generation = current_generation + 1
        _health_generation[vault_id] = inline_generation
        pending = _health_pending.get(vault_id)
        if (
            pending is not None
            and pending.config_key == config_key
            and pending.generation == inline_generation
        ):
            _health_pending.pop(vault_id, None)
        _book_inline_flight_locked(
            vault_id=vault_id,
            generation=inline_generation,
            config_key=config_key,
            started_mono=now_mono,
        )

    assert inline_generation is not None
    try:
        _run_health_recompute(
            vault_id=vault_id,
            source_root=source_root,
            allowed_bases=allowed_bases,
            generation=inline_generation,
            config_key=config_key,
            walker=walker,
        )
    finally:
        with _health_lock:
            _release_flight_if_owner_locked(vault_id, inline_generation)

    snapshot = get_filesystem_health_snapshot(vault_id)
    assert snapshot is not None
    _publish_health_metrics(snapshot)
    return snapshot


def _queue_after_inline_join_timeout(
    *,
    vault_id: int,
    source_root: str,
    allowed_bases: tuple[str, ...],
    config_key: str,
    generation: int,
    walker: Callable[..., FilesystemPreflightResult],
) -> FilesystemHealthSnapshot | None:
    """Preserve live ownership after a spawn=False join timeout.

    When a live walker still owns the single-flight slot, coalesce the
    requested config as a pending replacement and return the bounded cache
    synopsis. Returns None when the slot is already free so the caller can
    claim inline ownership.
    """
    now_mono = time.monotonic()
    with _health_lock:
        _reap_dead_inflight_locked(vault_id)
        inflight = _health_inflight.get(vault_id)
        if inflight is None or not _inflight_is_live(inflight):
            return None

        _coalesce_pending_locked(
            vault_id=vault_id,
            source_root=source_root,
            allowed_bases=allowed_bases,
            config_key=config_key,
            generation=generation,
            walker=walker,
        )
        return _bounded_snapshot_for_config_locked(
            vault_id=vault_id,
            source_root=source_root,
            config_key=config_key,
            now_mono=now_mono,
        )


def _claim_inline_flight(
    *,
    vault_id: int,
    source_root: str,
    allowed_bases: tuple[str, ...],
    config_key: str,
    generation: int,
    walker: Callable[..., FilesystemPreflightResult],
) -> tuple[int | None, FilesystemHealthSnapshot | None]:
    """Atomically reserve the slot for inline recompute after a prior owner exits.

    Returns ``(generation, None)`` on success. If another live owner already
    holds the slot (e.g. a promoted pending flight), queues this config and
    returns ``(None, bounded_snapshot)``.
    """
    now_mono = time.monotonic()
    with _health_lock:
        _reap_dead_inflight_locked(vault_id)
        inflight = _health_inflight.get(vault_id)
        if inflight is not None and _inflight_is_live(inflight):
            _coalesce_pending_locked(
                vault_id=vault_id,
                source_root=source_root,
                allowed_bases=allowed_bases,
                config_key=config_key,
                generation=generation,
                walker=walker,
            )
            snapshot = _bounded_snapshot_for_config_locked(
                vault_id=vault_id,
                source_root=source_root,
                config_key=config_key,
                now_mono=now_mono,
            )
            return None, snapshot

        current_generation = _health_generation.get(vault_id, 0)
        if generation != current_generation:
            # Churn superseded the pre-join epoch; mint a fresh one for inline.
            generation = current_generation + 1
            _health_generation[vault_id] = generation

        pending = _health_pending.get(vault_id)
        if (
            pending is not None
            and pending.config_key == config_key
            and pending.generation == generation
        ):
            # We are about to perform this work ourselves.
            _health_pending.pop(vault_id, None)

        _book_inline_flight_locked(
            vault_id=vault_id,
            generation=generation,
            config_key=config_key,
            started_mono=now_mono,
        )
        return generation, None


def _start_health_flight(
    *,
    vault_id: int,
    source_root: str,
    allowed_bases: tuple[str, ...],
    generation: int,
    config_key: str,
    walker: Callable[..., FilesystemPreflightResult],
    started_mono: float | None = None,
) -> None:
    """Install and start one background walker. Caller holds ``_health_lock``."""
    finished = threading.Event()
    thread = threading.Thread(
        target=_run_health_recompute,
        name=f"fs-health-{vault_id}",
        kwargs={
            "vault_id": vault_id,
            "source_root": source_root,
            "allowed_bases": allowed_bases,
            "generation": generation,
            "config_key": config_key,
            "walker": walker,
        },
        daemon=True,
    )
    _health_inflight[vault_id] = _InflightWork(
        thread=thread,
        started_mono=time.monotonic() if started_mono is None else started_mono,
        generation=generation,
        config_key=config_key,
        finished=finished,
    )
    thread.start()


def _start_pending_replacement_locked(vault_id: int) -> None:
    """If the single-flight slot is free, promote the pending replacement.

    Caller holds ``_health_lock``. No-op when another flight is already booked
    or when no pending config remains.
    """
    if vault_id in _health_inflight:
        return
    pending = _health_pending.pop(vault_id, None)
    if pending is None:
        return
    # Drop a pending entry that no longer matches the authoritative generation
    # (e.g. a fail-closed gate advanced generation after it was queued).
    if pending.generation != _health_generation.get(vault_id, 0):
        return
    _start_health_flight(
        vault_id=vault_id,
        source_root=pending.source_root,
        allowed_bases=pending.allowed_bases,
        generation=pending.generation,
        config_key=pending.config_key,
        walker=pending.walker,
    )


def _run_health_recompute(
    *,
    vault_id: int,
    source_root: str,
    allowed_bases: Sequence[str],
    generation: int,
    config_key: str,
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
    wrote_back = False

    try:
        with _health_lock:
            current_generation = _health_generation.get(vault_id, 0)
            inflight = _health_inflight.get(vault_id)
            owns_slot = bool(
                inflight is not None and inflight.generation == generation
            )
            # A newer schedule or fail-closed gate superseded this flight: drop.
            if generation == current_generation:
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
                    config_key=config_key,
                    error=error,
                )
                wrote_back = True
            # Only the owning generation may release the slot and promote a
            # chained replacement (including after a gate advanced generation
            # while leaving ownership in place).
            if owns_slot:
                _release_flight_if_owner_locked(vault_id, generation)
    finally:
        # Crash safety for both background and inline owners.
        with _health_lock:
            _release_flight_if_owner_locked(vault_id, generation)

    if wrote_back:
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


def _store_gated_snapshot(
    vault_id: int,
    *,
    source_root: str,
    config_key: str,
) -> FilesystemHealthSnapshot:
    """Record a definitive fail-closed synopsis without walking the tree.

    Advances the Vault health generation so any in-flight walk started before
    the gate cannot write a healthy synopsis over this fail-closed state.
    Clears queued replacements. Does **not** drop live inflight ownership: the
    active worker keeps the single-flight slot until it exits, so a later
    allow cannot start a second overlapping walker.
    """
    uid = os.geteuid()
    gid = os.getegid()
    checked_at = datetime.now(timezone.utc).isoformat()
    checked_mono = time.monotonic()
    with _health_lock:
        revision = _health_revision_seq.get(vault_id, 0) + 1
        _health_revision_seq[vault_id] = revision
        # Invalidate writeback and drop queued work; keep a live worker's slot.
        _health_generation[vault_id] = _health_generation.get(vault_id, 0) + 1
        _reap_dead_inflight_locked(vault_id)
        _health_pending.pop(vault_id, None)
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
            config_key=config_key,
            error=None,
        )
        _health_cache[vault_id] = entry
        return _snapshot_from_entry(vault_id, entry, now_mono=checked_mono)


def _placeholder_checking_entry(
    source_root: str,
    *,
    config_key: str,
) -> _HealthCacheEntry:
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
        config_key=config_key,
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
