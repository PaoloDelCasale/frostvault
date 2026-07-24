"""Per-Vault operation policies for automation and transfer controls.

This module is the operation-policy seam used by scan auto-upload, the worker
bandwidth/window gates, and HTTP configuration. A missing row means the
product defaults: manual upload, a five-minute stability window, and no
include/exclude globs (every cataloged path is eligible for manual ops).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from .job_scheduler import job_is_within_operating_window
from .vault_quotas import QuotaBlocked


DEFAULT_STABILITY_SECONDS = 300


@dataclass(frozen=True)
class OperationPolicy:
    auto_upload: bool = False
    auto_local_cleanup: bool = False
    local_retention_days: int | None = None
    stability_seconds: int = DEFAULT_STABILITY_SECONDS
    include_globs: tuple[str, ...] = ()
    exclude_globs: tuple[str, ...] = ()
    bandwidth_limit_kibps: int | None = None
    operating_windows: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "auto_upload": self.auto_upload,
            "auto_local_cleanup": self.auto_local_cleanup,
            "local_retention_days": self.local_retention_days,
            "stability_seconds": self.stability_seconds,
            "include_globs": list(self.include_globs),
            "exclude_globs": list(self.exclude_globs),
            "bandwidth_limit_kibps": self.bandwidth_limit_kibps,
            "operating_windows": [dict(item) for item in self.operating_windows],
        }


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _load_json_list(raw: str | None) -> tuple[Any, ...]:
    if not raw:
        return ()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("expected a JSON list")
    return tuple(data)


def _normalize_globs(values: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        text = str(value).strip().replace("\\", "/")
        if not text:
            continue
        if text.startswith("/"):
            raise ValueError("globs must be relative paths")
        normalized.append(text)
    return tuple(normalized)


def _normalize_windows(
    values: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    windows: list[dict[str, Any]] = []
    for item in values:
        weekday = int(item["weekday"])
        if weekday < 0 or weekday > 6:
            raise ValueError("weekday must be in 0..6")
        start = str(item["start"])
        end = str(item["end"])
        # Validate HH:MM shape early so HTTP callers get a clear 422.
        parsed: dict[str, tuple[int, int]] = {}
        for label, stamp in (("start", start), ("end", end)):
            hour_text, minute_text = stamp.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                raise ValueError(f"invalid {label} time")
            parsed[label] = (hour, minute)
        if parsed["start"] >= parsed["end"]:
            raise ValueError("operating window start must be before end")
        windows.append({"weekday": weekday, "start": start, "end": end})
    return tuple(windows)


def validate_policy(policy: OperationPolicy) -> None:
    if policy.stability_seconds < 0:
        raise ValueError("stability_seconds must be nonnegative")
    if (
        policy.local_retention_days is not None
        and policy.local_retention_days <= 0
    ):
        raise ValueError("local_retention_days must be positive")
    if policy.auto_local_cleanup and policy.local_retention_days is None:
        raise ValueError(
            "local_retention_days is required when automatic local cleanup is enabled"
        )
    if (
        policy.bandwidth_limit_kibps is not None
        and policy.bandwidth_limit_kibps < 0
    ):
        raise ValueError("bandwidth_limit_kibps must be nonnegative")
    _normalize_globs(policy.include_globs)
    _normalize_globs(policy.exclude_globs)
    _normalize_windows(policy.operating_windows)


def get_policy(connection: Any, vault_id: int) -> OperationPolicy:
    row = connection.execute(
        "SELECT * FROM vault_operation_policies WHERE vault_id=%s",
        (vault_id,),
    ).fetchone()
    if not row:
        return OperationPolicy()
    return OperationPolicy(
        auto_upload=_as_bool(row["auto_upload"]),
        auto_local_cleanup=_as_bool(row["auto_local_cleanup"]),
        local_retention_days=(
            int(row["local_retention_days"])
            if row["local_retention_days"] is not None
            else None
        ),
        stability_seconds=int(row["stability_seconds"]),
        include_globs=tuple(_load_json_list(row["include_globs_json"])),
        exclude_globs=tuple(_load_json_list(row["exclude_globs_json"])),
        bandwidth_limit_kibps=(
            int(row["bandwidth_limit_kibps"])
            if row["bandwidth_limit_kibps"] is not None
            else None
        ),
        operating_windows=tuple(
            dict(item) for item in _load_json_list(row["operating_windows_json"])
        ),
    )


def set_policy(
    connection: Any, vault_id: int, policy: OperationPolicy
) -> OperationPolicy:
    validate_policy(policy)
    exists = connection.execute(
        "SELECT id FROM vaults WHERE id=%s", (vault_id,)
    ).fetchone()
    if not exists:
        raise LookupError("vault_not_found")
    include_globs = _normalize_globs(policy.include_globs)
    exclude_globs = _normalize_globs(policy.exclude_globs)
    windows = _normalize_windows(policy.operating_windows)
    canonical = OperationPolicy(
        auto_upload=bool(policy.auto_upload),
        auto_local_cleanup=bool(policy.auto_local_cleanup),
        local_retention_days=policy.local_retention_days,
        stability_seconds=int(policy.stability_seconds),
        include_globs=include_globs,
        exclude_globs=exclude_globs,
        bandwidth_limit_kibps=policy.bandwidth_limit_kibps,
        operating_windows=windows,
    )
    connection.execute(
        """
        INSERT INTO vault_operation_policies(
            vault_id, auto_upload, auto_local_cleanup, local_retention_days,
            stability_seconds,
            include_globs_json, exclude_globs_json,
            bandwidth_limit_kibps, operating_windows_json
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(vault_id) DO UPDATE SET
            auto_upload=excluded.auto_upload,
            auto_local_cleanup=excluded.auto_local_cleanup,
            local_retention_days=excluded.local_retention_days,
            stability_seconds=excluded.stability_seconds,
            include_globs_json=excluded.include_globs_json,
            exclude_globs_json=excluded.exclude_globs_json,
            bandwidth_limit_kibps=excluded.bandwidth_limit_kibps,
            operating_windows_json=excluded.operating_windows_json
        """,
        (
            vault_id,
            canonical.auto_upload,
            canonical.auto_local_cleanup,
            canonical.local_retention_days,
            canonical.stability_seconds,
            json.dumps(list(canonical.include_globs)),
            json.dumps(list(canonical.exclude_globs)),
            canonical.bandwidth_limit_kibps,
            json.dumps(list(canonical.operating_windows)),
        ),
    )
    return canonical


def _path_matches(path: str, pattern: str) -> bool:
    normalized = path.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    # Support ** across path segments the way operators expect for vault trees.
    if "**" in pattern:
        # Convert glob to a sequence of fnmatch checks over suffixes/prefixes.
        parts = pattern.split("/")
        if pattern.endswith("/**"):
            prefix = pattern[: -len("/**")]
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return True
        if pattern.startswith("**/"):
            suffix = pattern[3:]
            if fnmatchcase(normalized, suffix):
                return True
            if any(
                fnmatchcase(str(PurePosixPath(*candidate.parts[index:])), suffix)
                for index in range(len(candidate.parts))
            ):
                return True
        return fnmatchcase(normalized, pattern)
    return fnmatchcase(normalized, pattern) or any(
        fnmatchcase(part, pattern) for part in candidate.parts
    )


def path_is_included(path: str, policy: OperationPolicy) -> bool:
    normalized = path.replace("\\", "/")
    if policy.include_globs and not any(
        _path_matches(normalized, pattern) for pattern in policy.include_globs
    ):
        return False
    if any(_path_matches(normalized, pattern) for pattern in policy.exclude_globs):
        return False
    return True


def preview_glob_rules(
    *,
    paths: Sequence[str],
    include_globs: Sequence[str] = (),
    exclude_globs: Sequence[str] = (),
) -> dict[str, list[str]]:
    policy = OperationPolicy(
        include_globs=_normalize_globs(include_globs),
        exclude_globs=_normalize_globs(exclude_globs),
    )
    included: list[str] = []
    excluded: list[str] = []
    for path in paths:
        if path_is_included(path, policy):
            included.append(path)
        else:
            excluded.append(path)
    return {"included": included, "excluded": excluded}


def file_is_stable(
    *,
    mtime_ns: int,
    now: datetime,
    stability_seconds: int,
) -> bool:
    current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    mtime = datetime.fromtimestamp(mtime_ns / 1e9, tz=timezone.utc)
    age = (current - mtime).total_seconds()
    return age >= stability_seconds


def local_cleanup_is_due(
    *,
    verified_at: str,
    retention_days: int,
    now: datetime,
) -> bool:
    verified = datetime.fromisoformat(verified_at)
    if verified.tzinfo is None:
        verified = verified.replace(tzinfo=timezone.utc)
    current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return current >= verified + timedelta(days=retention_days)


def effective_bandwidth_kibps(
    *,
    global_limit: int | None,
    vault_limit: int | None,
) -> int | None:
    if vault_limit is not None:
        return vault_limit
    return global_limit


def rclone_bwlimit_arg(limit_kibps: int | None) -> str | None:
    if limit_kibps is None:
        return None
    return f"{int(limit_kibps)}k"


def policy_allows_transfer_now(
    policy: OperationPolicy,
    *,
    now: datetime | None = None,
) -> bool:
    current = now or datetime.now(timezone.utc)
    return job_is_within_operating_window(current, policy.operating_windows)


def queue_auto_uploads(
    connection: Any,
    *,
    vault_id: int,
    source_root: str,
    requested_by: int,
    now: datetime | None = None,
) -> int:
    """Queue upload Jobs for stable, included Local Copies when enabled."""
    from ..catalog import ArchiveCatalog

    policy = get_policy(connection, vault_id)
    if not policy.auto_upload:
        return 0
    current = now or datetime.now(timezone.utc)
    if not policy_allows_transfer_now(policy, now=current):
        return 0
    catalog = ArchiveCatalog(connection)
    rows = connection.execute(
        """
        SELECT vf.id AS vault_file_id, fp.path, lc.size, lc.mtime_ns,
               lc.plaintext_sha256, lc.matched_archive_version_id
        FROM vault_files vf
        JOIN file_paths fp
          ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
        JOIN local_copies lc ON lc.vault_file_id=vf.id
        WHERE vf.vault_id=%s
          AND vf.status='active'
          AND lc.presence='present'
          AND lc.file_type='regular'
        ORDER BY lower(fp.path)
        """,
        (vault_id,),
    ).fetchall()
    root = Path(source_root)
    queued = 0
    timestamp = current.isoformat()
    for row in rows:
        path = row["path"]
        if not path_is_included(path, policy):
            continue
        if row["matched_archive_version_id"] and row["plaintext_sha256"]:
            # Already verified against a matching Archive Version.
            continue
        local_path = root.joinpath(*PurePosixPath(path).parts)
        try:
            stat_result = local_path.stat(follow_symlinks=False)
        except OSError:
            continue
        if (
            row["size"] != stat_result.st_size
            or row["mtime_ns"] != stat_result.st_mtime_ns
        ):
            continue
        if not file_is_stable(
            mtime_ns=int(stat_result.st_mtime_ns),
            now=current,
            stability_seconds=policy.stability_seconds,
        ):
            continue
        try:
            job_ids, _total, _eligible = catalog.queue_jobs(
                vault_id=vault_id,
                path=path,
                action="upload",
                requested_by=requested_by,
                requested_at=timestamp,
                group_id=uuid.uuid4().hex,
                is_directory=False,
            )
        except QuotaBlocked:
            break
        queued += len(job_ids)
    return queued


def queue_auto_local_cleanups(
    connection: Any,
    *,
    vault_id: int,
    requested_by: int,
    local_delete_enabled: bool,
    now: datetime | None = None,
) -> int:
    """Queue safe Local Copy cleanup Jobs when the current policy is due."""
    from ..catalog import ArchiveCatalog
    from .audit_events import record_audit_event
    from .notifications import enqueue_notification

    policy = get_policy(connection, vault_id)
    if (
        not local_delete_enabled
        or not policy.auto_local_cleanup
        or policy.local_retention_days is None
    ):
        return 0
    current = now or datetime.now(timezone.utc)
    if not policy_allows_transfer_now(policy, now=current):
        return 0

    rows = connection.execute(
        """
        SELECT fp.path, av.id AS archive_version_id, av.verified_at
        FROM vault_files vf
        JOIN file_paths fp
          ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
        JOIN local_copies lc ON lc.vault_file_id=vf.id
        JOIN archive_versions av ON av.id=lc.matched_archive_version_id
        WHERE vf.vault_id=%s
          AND vf.status='active'
          AND lc.presence='present'
          AND lc.file_type='regular'
          AND lc.plaintext_sha256 IS NOT NULL
          AND lc.plaintext_sha256=av.plaintext_sha256
          AND av.integrity='verified'
          AND av.availability='available'
          AND av.verified_at IS NOT NULL
        ORDER BY lower(fp.path)
        """,
        (vault_id,),
    ).fetchall()
    catalog = ArchiveCatalog(connection)
    timestamp = current.isoformat()
    queued = 0
    for row in rows:
        if not path_is_included(row["path"], policy):
            continue
        if not local_cleanup_is_due(
            verified_at=row["verified_at"],
            retention_days=policy.local_retention_days,
            now=current,
        ):
            continue
        try:
            job_ids, _total, _eligible = catalog.queue_jobs(
                vault_id=vault_id,
                path=row["path"],
                action="free-space",
                requested_by=requested_by,
                requested_at=timestamp,
                group_id=uuid.uuid4().hex,
                is_directory=False,
                archive_version_id=row["archive_version_id"],
                origin="automatic",
            )
        except QuotaBlocked:
            break
        for job_id in job_ids:
            record_audit_event(
                connection,
                event="local_cleanup.auto_queued",
                actor_user_id=requested_by,
                vault_id=vault_id,
                job_id=job_id,
                outcome="queued",
                path=row["path"],
                archive_version_id=row["archive_version_id"],
            )
            enqueue_notification(
                connection,
                user_id=requested_by,
                event="local_cleanup.auto_queued",
                title="Automatic local cleanup queued",
                body=f"{row['path']} reached its Local Copy retention period.",
                vault_id=vault_id,
                job_id=job_id,
            )
        queued += len(job_ids)
    return queued
