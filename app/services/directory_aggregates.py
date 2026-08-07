"""Durable directory aggregates for scaled archive browsing (issue #229).

Canonical Vault File / Archive Version rows remain authoritative. This module
maintains a derived projection used only by directory listing:

- one row per non-root directory that currently has visible descendants
- dirty-directory coalescing so watcher/job bursts rebuild each ancestor once
- full Vault rebuild when bulk reconciliation cannot cheaply name every path

Callers mutate the catalog on a shared connection, mark dirty paths/files, and
:func:`flush_directory_aggregates` before commit (also invoked from catalog
revision publication and the listing read path).
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from . import metrics as metrics_service
from .lifecycle_pins import is_path_pinned, load_lifecycle_pins


STATUS_READY = "ready"
STATUS_REBUILD_REQUIRED = "rebuild_required"

_STATE_COLUMNS = {
    "local_only": "state_local_only",
    "cloud_only": "state_cloud_only",
    "both": "state_both",
    "restoring": "state_restoring",
}

_ACTION_COLUMNS = {
    "upload": "action_upload",
    "recover": "action_recover",
    "free-space": "action_free_space",
    "cloud-archive": "action_cloud_archive",
    "cloud-purge": "action_cloud_purge",
    "storage-class": "action_storage_class",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ancestor_directories(path: str) -> list[str]:
    """Return every directory prefix of a Vault-relative file path."""
    normalized = (path or "").strip().strip("/")
    if not normalized or "/" not in normalized:
        return []
    parts = normalized.split("/")
    return ["/".join(parts[:index]) for index in range(1, len(parts))]


def parent_and_name(directory_path: str) -> tuple[str, str]:
    normalized = (directory_path or "").strip().strip("/")
    if not normalized:
        return "", ""
    if "/" not in normalized:
        return "", normalized
    parent, _, name = normalized.rpartition("/")
    return parent, name


def _escape_like(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _tracker(connection: Any) -> "_DirectoryAggregateTracker":
    tracker = getattr(connection, "_directory_aggregate_tracker", None)
    if tracker is None:
        tracker = _DirectoryAggregateTracker()
        setattr(connection, "_directory_aggregate_tracker", tracker)
    return tracker


@dataclass
class _DirectoryAggregateTracker:
    dirty_directories: set[tuple[int, str]] = field(default_factory=set)
    rebuild_vaults: set[int] = field(default_factory=set)
    dirty_file_marks: int = 0

    def clear(self) -> None:
        self.dirty_directories.clear()
        self.rebuild_vaults.clear()
        self.dirty_file_marks = 0


def _persist_dirty_directory(connection: Any, vault_id: int, directory: str) -> None:
    tracker = _tracker(connection)
    key = (int(vault_id), directory)
    if key in tracker.dirty_directories:
        return
    tracker.dirty_directories.add(key)
    connection.execute(
        """
        INSERT INTO directory_aggregate_dirty(vault_id, path, marked_at)
        VALUES (%s, %s, %s)
        ON CONFLICT(vault_id, path) DO NOTHING
        """,
        (int(vault_id), directory, _now()),
    )


def mark_path_dirty(connection: Any, vault_id: int, path: str | None) -> None:
    """Mark every ancestor directory of ``path`` dirty for the next flush."""
    if vault_id is None or not path:
        return
    tracker = _tracker(connection)
    tracker.dirty_file_marks += 1
    for directory in ancestor_directories(str(path)):
        _persist_dirty_directory(connection, int(vault_id), directory)


def mark_directory_dirty(connection: Any, vault_id: int, directory: str | None) -> None:
    """Mark one directory path (and its ancestors) dirty."""
    if vault_id is None or directory is None:
        return
    normalized = str(directory).strip().strip("/")
    if not normalized:
        return
    tracker = _tracker(connection)
    tracker.dirty_file_marks += 1
    _persist_dirty_directory(connection, int(vault_id), normalized)
    for ancestor in ancestor_directories(f"{normalized}/_"):
        _persist_dirty_directory(connection, int(vault_id), ancestor)


def mark_file_id_dirty(connection: Any, vault_id: int, vault_file_id: str | None) -> None:
    """Resolve the current path for ``vault_file_id`` and mark its ancestors."""
    if not vault_file_id:
        return
    row = connection.execute(
        """
        SELECT fp.path
        FROM vault_files vf
        JOIN file_paths fp
          ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
        WHERE vf.id=%s AND vf.vault_id=%s
        """,
        (vault_file_id, vault_id),
    ).fetchone()
    if row is not None:
        mark_path_dirty(connection, vault_id, row["path"])


def mark_paths_dirty(connection: Any, vault_id: int, paths: Iterable[str]) -> None:
    for path in paths:
        mark_path_dirty(connection, vault_id, path)


def request_vault_rebuild(connection: Any, vault_id: int) -> None:
    """Schedule a full aggregate rebuild for ``vault_id`` on the next flush."""
    tracker = _tracker(connection)
    tracker.rebuild_vaults.add(int(vault_id))
    connection.execute(
        """
        INSERT INTO directory_aggregate_status(vault_id, status, updated_at)
        VALUES (%s, %s, %s)
        ON CONFLICT(vault_id) DO UPDATE SET
            status=excluded.status,
            updated_at=excluded.updated_at
        """,
        (int(vault_id), STATUS_REBUILD_REQUIRED, _now()),
    )


def _status_row(connection: Any, vault_id: int) -> dict[str, Any] | None:
    try:
        return connection.execute(
            """
            SELECT vault_id, status, updated_at
            FROM directory_aggregate_status
            WHERE vault_id=%s
            """,
            (int(vault_id),),
        ).fetchone()
    except Exception:
        # Pre-migration callers (or tests on partial schemas) skip silently.
        return None


def ensure_directory_aggregates(connection: Any, vault_id: int) -> None:
    """Rebuild when the Vault has never been projected or is marked stale."""
    status = _status_row(connection, vault_id)
    if status is None or str(status.get("status") or "") != STATUS_READY:
        rebuild_vault_directory_aggregates(connection, vault_id)
        return
    flush_directory_aggregates(connection, vault_id=vault_id)


def flush_directory_aggregates(
    connection: Any,
    *,
    vault_id: int | None = None,
) -> dict[str, int]:
    """Apply coalesced dirty-directory rebuilds for one or all tracked Vaults."""
    tracker = _tracker(connection)
    started = time.perf_counter()

    # Merge durable dirty rows (survives connection boundaries) with the
    # in-memory tracker so burst updates inside one transaction still coalesce.
    if vault_id is None:
        durable_rows = connection.execute(
            "SELECT vault_id, path FROM directory_aggregate_dirty"
        ).fetchall()
        status_rows = connection.execute(
            """
            SELECT vault_id FROM directory_aggregate_status
            WHERE status=%s
            """,
            (STATUS_REBUILD_REQUIRED,),
        ).fetchall()
    else:
        durable_rows = connection.execute(
            """
            SELECT vault_id, path FROM directory_aggregate_dirty
            WHERE vault_id=%s
            """,
            (int(vault_id),),
        ).fetchall()
        status_rows = connection.execute(
            """
            SELECT vault_id FROM directory_aggregate_status
            WHERE vault_id=%s AND status=%s
            """,
            (int(vault_id), STATUS_REBUILD_REQUIRED),
        ).fetchall()

    rebuild_vaults = set(tracker.rebuild_vaults)
    rebuild_vaults.update(int(row["vault_id"]) for row in status_rows)
    dirty = set(tracker.dirty_directories)
    dirty.update((int(row["vault_id"]), str(row["path"])) for row in durable_rows)
    if vault_id is not None:
        rebuild_vaults = {vid for vid in rebuild_vaults if vid == int(vault_id)}
        dirty = {(vid, path) for vid, path in dirty if vid == int(vault_id)}
    file_marks = int(tracker.dirty_file_marks)

    rebuilt_dirs = 0
    full_rebuilds = 0
    for vid in sorted(rebuild_vaults):
        rebuild_vault_directory_aggregates(connection, vid)
        connection.execute(
            "DELETE FROM directory_aggregate_dirty WHERE vault_id=%s",
            (vid,),
        )
        full_rebuilds += 1
        dirty = {(d_vid, path) for d_vid, path in dirty if d_vid != vid}

    by_vault: dict[int, list[str]] = defaultdict(list)
    for vid, path in dirty:
        by_vault[vid].append(path)
    for vid, directories in by_vault.items():
        for directory in sorted(set(directories), key=lambda item: (item.count("/"), item)):
            _rebuild_directory(connection, vid, directory)
            rebuilt_dirs += 1
        connection.execute(
            "DELETE FROM directory_aggregate_dirty WHERE vault_id=%s",
            (vid,),
        )
        _mark_status(connection, vid, STATUS_READY)

    if vault_id is None:
        tracker.clear()
    else:
        tracker.rebuild_vaults.difference_update(rebuild_vaults)
        tracker.dirty_directories.difference_update(
            {(vid, path) for vid, path in tracker.dirty_directories if vid == int(vault_id)}
        )
        if not tracker.dirty_directories and not tracker.rebuild_vaults:
            tracker.dirty_file_marks = 0

    duration = time.perf_counter() - started
    batch_size = file_marks if file_marks else (rebuilt_dirs + full_rebuilds)
    if batch_size or rebuilt_dirs or full_rebuilds:
        metrics_service.set_gauge(
            "directory_aggregate_update_batch_size",
            float(batch_size),
        )
        metrics_service.set_gauge(
            "directory_aggregate_update_duration_seconds",
            float(duration),
        )
        metrics_service.set_gauge(
            "directory_aggregate_rebuild_status",
            1.0 if full_rebuilds else 0.0,
        )
    return {
        "rebuilt_directories": rebuilt_dirs,
        "full_rebuilds": full_rebuilds,
        "dirty_file_marks": file_marks,
    }


def rebuild_vault_directory_aggregates(connection: Any, vault_id: int) -> int:
    """Replace every directory aggregate row for ``vault_id`` from the catalog."""
    started = time.perf_counter()
    connection.execute(
        "DELETE FROM directory_aggregates WHERE vault_id=%s",
        (int(vault_id),),
    )
    connection.execute(
        "DELETE FROM directory_aggregate_dirty WHERE vault_id=%s",
        (int(vault_id),),
    )
    contributions = _iter_visible_file_contributions(connection, vault_id)
    rolled: dict[str, _DirectoryRollup] = {}
    for contribution in contributions:
        for directory in ancestor_directories(contribution.path):
            rollup = rolled.setdefault(directory, _DirectoryRollup())
            rollup.add(contribution)
    for directory, rollup in rolled.items():
        _upsert_rollup(connection, vault_id, directory, rollup)
    _mark_status(connection, vault_id, STATUS_READY)
    tracker = _tracker(connection)
    tracker.dirty_directories = {
        key for key in tracker.dirty_directories if key[0] != int(vault_id)
    }
    tracker.rebuild_vaults.discard(int(vault_id))
    duration = time.perf_counter() - started
    metrics_service.set_gauge(
        "directory_aggregate_update_duration_seconds",
        float(duration),
    )
    metrics_service.set_gauge("directory_aggregate_rebuild_status", 1.0)
    metrics_service.set_gauge(
        "directory_aggregate_update_batch_size",
        float(len(rolled)),
    )
    return len(rolled)


def _mark_status(connection: Any, vault_id: int, status: str) -> None:
    connection.execute(
        """
        INSERT INTO directory_aggregate_status(vault_id, status, updated_at)
        VALUES (%s, %s, %s)
        ON CONFLICT(vault_id) DO UPDATE SET
            status=excluded.status,
            updated_at=excluded.updated_at
        """,
        (int(vault_id), status, _now()),
    )


@dataclass
class _FileContribution:
    path: str
    state: str
    total_size: int
    local_size: int
    cloud_size: int
    upload_eligible: bool
    recover_eligible: bool
    cleanup_eligible: bool
    storage_class_eligible: bool
    has_cloud: bool
    lifecycle_pinned: bool
    storage_class: str | None


@dataclass
class _DirectoryRollup:
    item_count: int = 0
    total_size: int = 0
    local_size: int = 0
    cloud_size: int = 0
    state_counts: dict[str, int] = field(default_factory=dict)
    action_counts: dict[str, int] = field(
        default_factory=lambda: {key: 0 for key in _ACTION_COLUMNS}
    )
    storage_classes: dict[str, int] = field(default_factory=dict)
    pinned_count: int = 0

    def add(self, contribution: _FileContribution) -> None:
        self.item_count += 1
        self.total_size += contribution.total_size
        self.local_size += contribution.local_size
        self.cloud_size += contribution.cloud_size
        self.state_counts[contribution.state] = (
            self.state_counts.get(contribution.state, 0) + 1
        )
        if contribution.upload_eligible:
            self.action_counts["upload"] += 1
        if contribution.recover_eligible:
            self.action_counts["recover"] += 1
        if contribution.cleanup_eligible:
            self.action_counts["free-space"] += 1
        if contribution.has_cloud:
            self.action_counts["cloud-archive"] += 1
            self.action_counts["cloud-purge"] += 1
        if contribution.storage_class_eligible:
            self.action_counts["storage-class"] += 1
        if contribution.lifecycle_pinned:
            self.pinned_count += 1
        if contribution.storage_class:
            key = str(contribution.storage_class)
            self.storage_classes[key] = self.storage_classes.get(key, 0) + 1


def _iter_visible_file_contributions(
    connection: Any,
    vault_id: int,
    *,
    path_prefix: str | None = None,
) -> list[_FileContribution]:
    """Load visible file contributions (same classification as list_file_rows)."""
    clauses = ["vf.vault_id=%s", "vf.status='active'"]
    params: list[Any] = [vault_id]
    if path_prefix:
        escaped = _escape_like(path_prefix)
        clauses.append("(fp.path=%s OR fp.path LIKE %s ESCAPE '\\')")
        params.extend([path_prefix, f"{escaped}/%"])
    rows = connection.execute(
        f"""
        SELECT
            vf.id AS vault_file_id,
            fp.path,
            lc.presence AS local_presence,
            lc.file_type AS local_file_type,
            lc.size AS local_size,
            lc.plaintext_sha256 AS local_sha256,
            lc.matched_archive_version_id,
            av.id AS archive_version_id,
            av.size AS cloud_size,
            av.storage_class,
            av.plaintext_sha256 AS version_sha256,
            av.integrity,
            av.availability,
            av.restore_state,
            (
                SELECT COUNT(*)
                FROM archive_versions recoverable
                WHERE recoverable.vault_file_id=vf.id
                  AND recoverable.integrity='verified'
                  AND recoverable.availability='available'
            ) AS recoverable_version_count
        FROM vault_files vf
        JOIN file_paths fp
          ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
        LEFT JOIN local_copies lc ON lc.vault_file_id=vf.id
        LEFT JOIN archive_versions av
          ON av.id=(
              SELECT latest.id
              FROM archive_versions latest
              WHERE latest.vault_file_id=vf.id
                AND latest.availability NOT IN ('missing', 'purged')
              ORDER BY latest.version_number DESC
              LIMIT 1
          )
        WHERE {" AND ".join(clauses)}
        ORDER BY lower(fp.path)
        """,
        params,
    ).fetchall()
    # Pin matching is path-prefix based; load once per rebuild scope.
    pins = load_lifecycle_pins(connection, vault_id)
    contributions: list[_FileContribution] = []
    for row in rows:
        contribution = _contribution_from_row(row, pins=pins)
        if contribution is not None:
            contributions.append(contribution)
    return contributions


def _contribution_from_row(
    row: Mapping[str, Any],
    *,
    pins: tuple[tuple[str, bool], ...] | None = None,
    connection: Any | None = None,
    vault_id: int | None = None,
) -> _FileContribution | None:
    local_exists = row["local_presence"] in {"present", "unsupported"}
    cloud_exists = row["archive_version_id"] is not None
    if not local_exists and not cloud_exists:
        return None
    state = (
        "restoring"
        if row["restore_state"] == "restoring"
        else "both"
        if local_exists and cloud_exists
        else "local_only"
        if local_exists
        else "cloud_only"
    )
    local_size = int(row["local_size"] or 0) if row["local_size"] is not None else 0
    cloud_size = int(row["cloud_size"] or 0) if row["cloud_size"] is not None else 0
    total_size = (
        int(row["local_size"])
        if row["local_size"] is not None
        else int(row["cloud_size"] or 0)
    )
    recoverable_count = int(row["recoverable_version_count"] or 0)
    upload_eligible = (
        row["local_presence"] == "present"
        and row["local_file_type"] == "regular"
        and not cloud_exists
    )
    recover_eligible = not local_exists and recoverable_count > 0
    cleanup_eligible = (
        row["local_presence"] == "present"
        and row["local_file_type"] == "regular"
        and cloud_exists
        and row["integrity"] == "verified"
        and row["availability"] == "available"
        and row["matched_archive_version_id"] == row["archive_version_id"]
        and row["local_sha256"] is not None
        and row["local_sha256"] == row["version_sha256"]
    )
    has_cloud = bool(cloud_exists or state in {"both", "cloud_only", "restoring"})
    storage_class_eligible = (
        cloud_exists
        and row["availability"] == "available"
        and row["restore_state"] != "restoring"
    )
    path = str(row["path"])
    if pins is not None:
        lifecycle_pinned = _pinned_with_loaded(pins, path)
    else:
        lifecycle_pinned = bool(
            connection is not None
            and vault_id is not None
            and is_path_pinned(connection, vault_id, path)
        )
    return _FileContribution(
        path=path,
        state=state,
        total_size=total_size,
        local_size=local_size if local_exists else 0,
        cloud_size=cloud_size if cloud_exists else 0,
        upload_eligible=upload_eligible,
        recover_eligible=recover_eligible,
        cleanup_eligible=cleanup_eligible,
        storage_class_eligible=storage_class_eligible,
        has_cloud=has_cloud,
        lifecycle_pinned=lifecycle_pinned,
        storage_class=row["storage_class"] if cloud_exists else None,
    )


def _pinned_with_loaded(pins: tuple[tuple[str, bool], ...], path: str) -> bool:
    normalized = path.strip().strip("/")
    for pin_path, is_directory in pins:
        if is_directory:
            if normalized == pin_path or normalized.startswith(f"{pin_path}/"):
                return True
        elif normalized == pin_path:
            return True
    return False


def _rebuild_directory(connection: Any, vault_id: int, directory: str) -> None:
    directory = (directory or "").strip().strip("/")
    if not directory:
        return
    contributions = _iter_visible_file_contributions(
        connection,
        vault_id,
        path_prefix=directory,
    )
    # path_prefix includes the directory path itself if it were a file; filter
    # to strict descendants only (files under directory/).
    prefix = f"{directory}/"
    rollup = _DirectoryRollup()
    for contribution in contributions:
        if contribution.path.startswith(prefix):
            rollup.add(contribution)
    if rollup.item_count <= 0:
        connection.execute(
            """
            DELETE FROM directory_aggregates
            WHERE vault_id=%s AND path=%s
            """,
            (int(vault_id), directory),
        )
        return
    _upsert_rollup(connection, vault_id, directory, rollup)


def _upsert_rollup(
    connection: Any,
    vault_id: int,
    directory: str,
    rollup: _DirectoryRollup,
) -> None:
    parent_path, name = parent_and_name(directory)
    storage_json = json.dumps(
        rollup.storage_classes,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute(
        """
        INSERT INTO directory_aggregates(
            vault_id, path, parent_path, name,
            item_count, total_size, local_size, cloud_size,
            state_local_only, state_cloud_only, state_both, state_restoring,
            action_upload, action_recover, action_free_space,
            action_cloud_archive, action_cloud_purge, action_storage_class,
            pinned_count, storage_class_counts, updated_at
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s
        )
        ON CONFLICT(vault_id, path) DO UPDATE SET
            parent_path=excluded.parent_path,
            name=excluded.name,
            item_count=excluded.item_count,
            total_size=excluded.total_size,
            local_size=excluded.local_size,
            cloud_size=excluded.cloud_size,
            state_local_only=excluded.state_local_only,
            state_cloud_only=excluded.state_cloud_only,
            state_both=excluded.state_both,
            state_restoring=excluded.state_restoring,
            action_upload=excluded.action_upload,
            action_recover=excluded.action_recover,
            action_free_space=excluded.action_free_space,
            action_cloud_archive=excluded.action_cloud_archive,
            action_cloud_purge=excluded.action_cloud_purge,
            action_storage_class=excluded.action_storage_class,
            pinned_count=excluded.pinned_count,
            storage_class_counts=excluded.storage_class_counts,
            updated_at=excluded.updated_at
        """,
        (
            int(vault_id),
            directory,
            parent_path,
            name,
            rollup.item_count,
            rollup.total_size,
            rollup.local_size,
            rollup.cloud_size,
            int(rollup.state_counts.get("local_only", 0)),
            int(rollup.state_counts.get("cloud_only", 0)),
            int(rollup.state_counts.get("both", 0)),
            int(rollup.state_counts.get("restoring", 0)),
            int(rollup.action_counts.get("upload", 0)),
            int(rollup.action_counts.get("recover", 0)),
            int(rollup.action_counts.get("free-space", 0)),
            int(rollup.action_counts.get("cloud-archive", 0)),
            int(rollup.action_counts.get("cloud-purge", 0)),
            int(rollup.action_counts.get("storage-class", 0)),
            int(rollup.pinned_count),
            storage_json,
            _now(),
        ),
    )


def directory_row_to_item(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project a durable aggregate row into the public directory list item."""
    state_counts = {
        state: int(row[column] or 0)
        for state, column in _STATE_COLUMNS.items()
        if int(row[column] or 0) > 0
    }
    states = list(state_counts)
    state = states[0] if len(states) == 1 else "mixed"
    try:
        storage_map = json.loads(row.get("storage_class_counts") or "{}")
    except (TypeError, json.JSONDecodeError):
        storage_map = {}
    if not isinstance(storage_map, dict):
        storage_map = {}
    storage_classes = sorted(
        str(name) for name, count in storage_map.items() if int(count or 0) > 0
    )
    item_count = int(row["item_count"] or 0)
    pinned_count = int(row.get("pinned_count") or 0)
    return {
        "type": "directory",
        "name": row["name"],
        "path": row["path"],
        "item_count": item_count,
        "total_size": int(row["total_size"] or 0),
        "local_size": int(row["local_size"] or 0),
        "cloud_size": int(row["cloud_size"] or 0),
        "state": state,
        "state_counts": state_counts,
        "storage_class": storage_classes[0] if len(storage_classes) == 1 else None,
        "storage_class_count": len(storage_classes),
        "available_actions": {
            "upload": int(row["action_upload"] or 0),
            "recover": int(row["action_recover"] or 0),
            "free-space": int(row["action_free_space"] or 0),
            "cloud-archive": int(row["action_cloud_archive"] or 0),
            "cloud-purge": int(row["action_cloud_purge"] or 0),
            "storage-class": int(row["action_storage_class"] or 0),
        },
        "lifecycle_pinned": pinned_count == item_count and pinned_count > 0,
        "lifecycle_pinned_partial": pinned_count > 0 and pinned_count < item_count,
    }


def list_child_directory_rows(
    connection: Any,
    vault_id: int,
    *,
    parent_path: str,
    state_filter: str = "",
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    clauses = ["vault_id=%s", "parent_path=%s"]
    params: list[Any] = [int(vault_id), parent_path or ""]
    if state_filter:
        column = _STATE_COLUMNS.get(state_filter)
        if column is None:
            return []
        clauses.append(f"{column} > 0")
    if limit < 0:
        limit = 0
    if offset < 0:
        offset = 0
    rows = connection.execute(
        f"""
        SELECT *
        FROM directory_aggregates
        WHERE {" AND ".join(clauses)}
        ORDER BY lower(name) ASC, name ASC
        LIMIT %s OFFSET %s
        """,
        [*params, int(limit), int(offset)],
    ).fetchall()
    return [directory_row_to_item(row) for row in rows]


def count_child_directories(
    connection: Any,
    vault_id: int,
    *,
    parent_path: str,
    state_filter: str = "",
) -> int:
    clauses = ["vault_id=%s", "parent_path=%s"]
    params: list[Any] = [int(vault_id), parent_path or ""]
    if state_filter:
        column = _STATE_COLUMNS.get(state_filter)
        if column is None:
            return 0
        clauses.append(f"{column} > 0")
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM directory_aggregates
        WHERE {" AND ".join(clauses)}
        """,
        params,
    ).fetchone()
    return int(row["total"] or 0) if row else 0
