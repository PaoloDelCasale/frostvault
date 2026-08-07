"""Durable directory aggregates for scaled archive browsing (issue #229).

Canonical Vault File / Archive Version rows remain authoritative. This module
maintains a derived projection used only by directory listing:

- one row per non-root directory that currently has visible descendants
- dirty-directory coalescing so watcher/job bursts rebuild each ancestor once
- full Vault rebuild when bulk reconciliation cannot cheaply name every path

Callers mutate the catalog on a shared connection, mark dirty paths/files, and
:func:`flush_directory_aggregates` before commit (also invoked from catalog
revision publication). The listing read path only *schedules* maintenance and
never performs an unbounded rebuild: it returns ``ready`` / ``loading`` /
``stale`` plus the existing projection when one exists.

Dirty-directory refresh uses SQL-side rollups (bounded result cardinality).
Full Vault rebuild streams contributions in bounded batches and folds them into
per-directory rollups without retaining a full descendant list in Python.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from . import metrics as metrics_service
from .lifecycle_pins import is_path_pinned, load_lifecycle_pins


STATUS_READY = "ready"
STATUS_REBUILD_REQUIRED = "rebuild_required"

# Public listing readiness (returned by /api/files). Distinct from durable
# directory_aggregate_status.status which only stores ready/rebuild_required.
AGGREGATE_LISTING_READY = "ready"
AGGREGATE_LISTING_LOADING = "loading"
AGGREGATE_LISTING_STALE = "stale"

# Full-rebuild contribution stream size. Keeps peak Python cardinality bounded
# while folding into O(directories) rollups.
REBUILD_CONTRIBUTION_BATCH_SIZE = 2_000

_logger = logging.getLogger(__name__)
_maintenance_lock = threading.Lock()
_maintenance_inflight: dict[int | None, threading.Thread] = {}
_maintenance_scheduling_enabled = True

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
    # Coalesce burst marks inside one connection. Cross-connection re-marks
    # still bump durable marked_at so a concurrent flush cannot drop them.
    if key in tracker.dirty_directories:
        return
    marked_at = _now()
    connection.execute(
        """
        INSERT INTO directory_aggregate_dirty(vault_id, path, marked_at)
        VALUES (%s, %s, %s)
        ON CONFLICT(vault_id, path) DO UPDATE SET
            marked_at=excluded.marked_at
        """,
        (int(vault_id), directory, marked_at),
    )
    tracker.dirty_directories.add(key)


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


def invalidate_for_vault_file(
    connection: Any,
    vault_id: int,
    vault_file_id: str | None,
) -> None:
    """Central seam: one Vault File mutation dirties its ancestor directories."""
    mark_file_id_dirty(connection, vault_id, vault_file_id)


def invalidate_for_confirmed_rename(
    connection: Any,
    vault_id: int,
    *,
    old_path: str | None,
    provisional_path: str | None,
    new_path: str | None,
) -> None:
    """Central seam: rename confirmation dirties old + provisional/new chains.

    Paths come from the confirmation snapshot, not a post-mutation file_paths
    lookup. After CAS moves Path History, the surviving Vault File only has
    ``new_path`` current and the provisional identity is retired — so both the
    pre-rename missing location and the consumed provisional location must be
    marked explicitly for ancestor convergence.
    """
    mark_paths_dirty(
        connection,
        vault_id,
        (old_path, provisional_path, new_path),
    )


def invalidate_for_vault_files(
    connection: Any,
    items: Iterable[tuple[int, str]],
) -> None:
    """Invalidate many ``(vault_id, vault_file_id)`` pairs with path coalescing."""
    seen: set[tuple[int, str]] = set()
    for vault_id, vault_file_id in items:
        key = (int(vault_id), str(vault_file_id))
        if not vault_file_id or key in seen:
            continue
        seen.add(key)
        mark_file_id_dirty(connection, int(vault_id), str(vault_file_id))


def invalidate_for_archive_version_ids(
    connection: Any,
    version_ids: Iterable[str],
) -> None:
    """Resolve Archive Version ids to current paths and mark ancestors dirty.

    Prefer this over ad-hoc SQL in audit/purge/storage writers so every semantic
    catalog mutation shares one invalidation path.
    """
    ids = [str(version_id) for version_id in version_ids if version_id]
    if not ids:
        return
    # Chunk to keep parameter lists portable across SQLite/PostgreSQL.
    for offset in range(0, len(ids), 500):
        batch = ids[offset : offset + 500]
        placeholders = ", ".join(["%s"] * len(batch))
        rows = connection.execute(
            f"""
            SELECT DISTINCT av.vault_id, fp.path
            FROM archive_versions av
            JOIN file_paths fp
              ON fp.vault_file_id=av.vault_file_id AND fp.valid_to IS NULL
            WHERE av.id IN ({placeholders})
            """,
            batch,
        ).fetchall()
        for row in rows:
            mark_path_dirty(connection, int(row["vault_id"]), row["path"])


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


def set_maintenance_scheduling_enabled(enabled: bool) -> None:
    """Test seam: disable background scheduling without patching threads."""
    global _maintenance_scheduling_enabled
    _maintenance_scheduling_enabled = bool(enabled)


def _vault_has_projection(connection: Any, vault_id: int) -> bool:
    try:
        row = connection.execute(
            """
            SELECT 1 AS present
            FROM directory_aggregates
            WHERE vault_id=%s
            LIMIT 1
            """,
            (int(vault_id),),
        ).fetchone()
    except Exception:
        return False
    return row is not None


def _vault_has_dirty(connection: Any, vault_id: int) -> bool:
    try:
        row = connection.execute(
            """
            SELECT 1 AS present
            FROM directory_aggregate_dirty
            WHERE vault_id=%s
            LIMIT 1
            """,
            (int(vault_id),),
        ).fetchone()
    except Exception:
        return False
    return row is not None


def schedule_directory_aggregate_maintenance(vault_id: int | None = None) -> bool:
    """Single-flight background maintenance for one Vault (or all pending).

    Returns True when a new worker thread was started. Durable dirty /
    rebuild_required rows remain the source of truth for restart recovery even
    when scheduling is disabled (tests) or a worker is already running.
    """
    if not _maintenance_scheduling_enabled:
        return False
    key: int | None = int(vault_id) if vault_id is not None else None
    with _maintenance_lock:
        existing = _maintenance_inflight.get(key)
        if existing is not None and existing.is_alive():
            return False
        thread = threading.Thread(
            target=_run_scheduled_maintenance,
            kwargs={"vault_id": key},
            name=f"directory-aggregates-{key if key is not None else 'all'}",
            daemon=True,
        )
        _maintenance_inflight[key] = thread
        thread.start()
        return True


def _run_scheduled_maintenance(*, vault_id: int | None) -> None:
    try:
        process_directory_aggregate_maintenance(vault_id=vault_id, publish=True)
    except Exception:
        _logger.exception(
            "directory aggregate maintenance failed vault_id=%s",
            vault_id,
        )
    finally:
        with _maintenance_lock:
            current = _maintenance_inflight.get(vault_id)
            if current is threading.current_thread():
                _maintenance_inflight.pop(vault_id, None)


def process_directory_aggregate_maintenance(
    *,
    vault_id: int | None = None,
    publish: bool = True,
    connection: Any | None = None,
) -> dict[str, int]:
    """Apply pending dirty/rebuild work (background worker / recovery path).

    When ``connection`` is omitted, opens a short-lived ``db()`` handle. When
    provided (tests or an outer transaction owner), uses that connection and
    does not commit/close it. Publishes one catalog revision per Vault that had
    work so open browsers converge via #227 invalidation without idle polling.
    """
    # Local import keeps module import light and avoids cycle at import time.
    from ..database import db
    from .catalog_event_hub import publish_committed_event
    from .catalog_events import record_catalog_revision

    totals = {
        "rebuilt_directories": 0,
        "full_rebuilds": 0,
        "vaults": 0,
        "dirty_file_marks": 0,
    }
    published_events: list[dict[str, Any]] = []

    def _run(active: Any) -> None:
        if vault_id is not None:
            vault_ids = [int(vault_id)]
        else:
            status_rows = active.execute(
                """
                SELECT vault_id FROM directory_aggregate_status
                WHERE status=%s
                """,
                (STATUS_REBUILD_REQUIRED,),
            ).fetchall()
            dirty_rows = active.execute(
                "SELECT DISTINCT vault_id FROM directory_aggregate_dirty"
            ).fetchall()
            vault_ids = sorted(
                {
                    int(row["vault_id"])
                    for row in (*status_rows, *dirty_rows)
                }
            )
        for vid in vault_ids:
            result = flush_directory_aggregates(active, vault_id=vid)
            totals["rebuilt_directories"] += int(result["rebuilt_directories"])
            totals["full_rebuilds"] += int(result["full_rebuilds"])
            totals["dirty_file_marks"] += int(result["dirty_file_marks"])
            totals["vaults"] += 1
            if publish and (
                result["rebuilt_directories"] or result["full_rebuilds"]
            ):
                published_events.append(
                    record_catalog_revision(
                        active,
                        vault_id=vid,
                        reason="directory_aggregates",
                        invalidate=("files",),
                    )
                )

    if connection is not None:
        _run(connection)
    else:
        with db() as owned:
            _run(owned)

    if publish:
        for event in published_events:
            try:
                publish_committed_event(event)
            except Exception:
                _logger.exception(
                    "failed to publish directory aggregate revision"
                )
    return totals


def ensure_directory_aggregates(connection: Any, vault_id: int) -> str:
    """Return listing readiness; never run unbounded rebuild on the request path.

    Schedules background maintenance when the durable projection is missing,
    marked ``rebuild_required``, or has pending dirty rows. Callers must treat
    ``loading`` / ``stale`` as non-authoritative empty/prior projection states.
    """
    status = _status_row(connection, vault_id)
    if status is None:
        request_vault_rebuild(connection, vault_id)
        schedule_directory_aggregate_maintenance(int(vault_id))
        return AGGREGATE_LISTING_LOADING

    current = str(status.get("status") or "")
    if current != STATUS_READY:
        schedule_directory_aggregate_maintenance(int(vault_id))
        if _vault_has_projection(connection, vault_id):
            return AGGREGATE_LISTING_STALE
        return AGGREGATE_LISTING_LOADING

    # Ready projection with durable dirty rows (restart / partial flush). Do not
    # rebuild large dirty ancestors on the listing connection — schedule only.
    if _vault_has_dirty(connection, vault_id):
        schedule_directory_aggregate_maintenance(int(vault_id))
        return AGGREGATE_LISTING_STALE
    return AGGREGATE_LISTING_READY


def _claim_dirty_rows(
    connection: Any,
    *,
    vault_id: int | None,
    cutoff: str,
) -> list[tuple[int, str]]:
    """Snapshot durable dirty rows with marked_at at or before ``cutoff``."""
    if vault_id is None:
        rows = connection.execute(
            """
            SELECT vault_id, path FROM directory_aggregate_dirty
            WHERE marked_at <= %s
            """,
            (cutoff,),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT vault_id, path FROM directory_aggregate_dirty
            WHERE vault_id=%s AND marked_at <= %s
            """,
            (int(vault_id), cutoff),
        ).fetchall()
    return [(int(row["vault_id"]), str(row["path"])) for row in rows]


def _delete_claimed_dirty_rows(
    connection: Any,
    *,
    vault_id: int,
    paths: Iterable[str],
    cutoff: str,
) -> None:
    """Drop only claimed dirty rows; newer concurrent marks survive."""
    unique_paths = sorted({str(path) for path in paths if path is not None})
    if not unique_paths:
        # Full-vault claim (rebuild): delete every pre-cutoff dirty row.
        connection.execute(
            """
            DELETE FROM directory_aggregate_dirty
            WHERE vault_id=%s AND marked_at <= %s
            """,
            (int(vault_id), cutoff),
        )
        return
    for offset in range(0, len(unique_paths), 500):
        batch = unique_paths[offset : offset + 500]
        placeholders = ", ".join(["%s"] * len(batch))
        connection.execute(
            f"""
            DELETE FROM directory_aggregate_dirty
            WHERE vault_id=%s
              AND path IN ({placeholders})
              AND marked_at <= %s
            """,
            (int(vault_id), *batch, cutoff),
        )


def flush_directory_aggregates(
    connection: Any,
    *,
    vault_id: int | None = None,
) -> dict[str, int]:
    """Apply coalesced dirty-directory rebuilds for one or all tracked Vaults.

    Claim/delete is cutoff-bounded: only dirty rows with ``marked_at`` at or
    before the flush snapshot are removed. Marks committed during rebuild
    (including same-path re-marks that bump ``marked_at``) remain durable for
    the next flush — required for PostgreSQL READ COMMITTED concurrency.
    """
    tracker = _tracker(connection)
    started = time.perf_counter()
    # Cutoff freezes the claim set before any rebuild work observes catalog rows.
    cutoff = _now()

    # Merge durable dirty rows (survives connection boundaries) with the
    # in-memory tracker so burst updates inside one transaction still coalesce.
    if vault_id is None:
        status_rows = connection.execute(
            """
            SELECT vault_id FROM directory_aggregate_status
            WHERE status=%s
            """,
            (STATUS_REBUILD_REQUIRED,),
        ).fetchall()
    else:
        status_rows = connection.execute(
            """
            SELECT vault_id FROM directory_aggregate_status
            WHERE vault_id=%s AND status=%s
            """,
            (int(vault_id), STATUS_REBUILD_REQUIRED),
        ).fetchall()

    claimed_dirty = set(_claim_dirty_rows(connection, vault_id=vault_id, cutoff=cutoff))
    # In-connection marks are always part of this claim even if a clock skew
    # made their durable marked_at appear after cutoff (same transaction).
    tracker_dirty = set(tracker.dirty_directories)
    if vault_id is not None:
        tracker_dirty = {
            (vid, path) for vid, path in tracker_dirty if vid == int(vault_id)
        }
    dirty = set(claimed_dirty)
    dirty.update(tracker_dirty)

    rebuild_vaults = set(tracker.rebuild_vaults)
    rebuild_vaults.update(int(row["vault_id"]) for row in status_rows)
    if vault_id is not None:
        rebuild_vaults = {vid for vid in rebuild_vaults if vid == int(vault_id)}
        dirty = {(vid, path) for vid, path in dirty if vid == int(vault_id)}
    file_marks = int(tracker.dirty_file_marks)

    rebuilt_dirs = 0
    full_rebuilds = 0
    for vid in sorted(rebuild_vaults):
        rebuild_vault_directory_aggregates(
            connection,
            vid,
            dirty_cutoff=cutoff,
        )
        full_rebuilds += 1
        dirty = {(d_vid, path) for d_vid, path in dirty if d_vid != vid}

    by_vault: dict[int, list[str]] = defaultdict(list)
    for vid, path in dirty:
        by_vault[vid].append(path)
    for vid, directories in by_vault.items():
        unique_dirs = sorted(set(directories), key=lambda item: (item.count("/"), item))
        for directory in unique_dirs:
            _rebuild_directory(connection, vid, directory)
            rebuilt_dirs += 1
        _delete_claimed_dirty_rows(
            connection,
            vault_id=vid,
            paths=unique_dirs,
            cutoff=cutoff,
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


def rebuild_vault_directory_aggregates(
    connection: Any,
    vault_id: int,
    *,
    dirty_cutoff: str | None = None,
) -> int:
    """Replace every directory aggregate row for ``vault_id`` from the catalog.

    Streams visible file contributions in bounded batches and folds each batch
    into per-directory rollups. Peak Python cardinality is O(batch + dirs),
    never a full descendant materialization. Dirty rows newer than
    ``dirty_cutoff`` (default: now at rebuild start) are preserved so concurrent
    transactions that mark after the claim remain durable for the next flush.
    """
    started = time.perf_counter()
    cutoff = dirty_cutoff or _now()
    connection.execute(
        "DELETE FROM directory_aggregates WHERE vault_id=%s",
        (int(vault_id),),
    )
    rolled: dict[str, _DirectoryRollup] = {}
    max_batch_seen = 0
    for batch in _iter_visible_file_contribution_batches(connection, vault_id):
        max_batch_seen = max(max_batch_seen, len(batch))
        for contribution in batch:
            for directory in ancestor_directories(contribution.path):
                rollup = rolled.setdefault(directory, _DirectoryRollup())
                rollup.add(contribution)
    for directory, rollup in rolled.items():
        _upsert_rollup(connection, vault_id, directory, rollup)
    _delete_claimed_dirty_rows(
        connection,
        vault_id=int(vault_id),
        paths=(),
        cutoff=cutoff,
    )
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
        float(max_batch_seen if max_batch_seen else len(rolled)),
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


def _visible_file_select_sql(
    *,
    path_prefix: str | None = None,
    keyset_path: str | None = None,
    limit: int | None = None,
) -> tuple[str, list[Any]]:
    """Shared SELECT for visible catalog rows (SQLite/PG portable)."""
    clauses = ["vf.vault_id=%s", "vf.status='active'"]
    params: list[Any] = []
    # vault_id is bound by the caller as the first param after building SQL.
    if path_prefix:
        escaped = _escape_like(path_prefix)
        # Strict descendants under directory/ (directory path itself is never a file).
        clauses.append("fp.path LIKE %s ESCAPE '\\'")
        params.append(f"{escaped}/%")
    if keyset_path is not None:
        clauses.append("fp.path > %s")
        params.append(keyset_path)
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT %s"
        params.append(int(limit))
    sql = f"""
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
        ORDER BY fp.path ASC
        {limit_sql}
        """
    return sql, params


def _iter_visible_file_contribution_batches(
    connection: Any,
    vault_id: int,
    *,
    path_prefix: str | None = None,
    batch_size: int | None = None,
) -> Iterator[list[_FileContribution]]:
    """Yield bounded batches of visible file contributions (no full list)."""
    size = int(batch_size or REBUILD_CONTRIBUTION_BATCH_SIZE)
    if size <= 0:
        size = REBUILD_CONTRIBUTION_BATCH_SIZE
    pins = load_lifecycle_pins(connection, vault_id)
    keyset_path = ""
    while True:
        sql, extra_params = _visible_file_select_sql(
            path_prefix=path_prefix,
            keyset_path=keyset_path,
            limit=size,
        )
        rows = connection.execute(
            sql,
            (int(vault_id), *extra_params),
        ).fetchall()
        if not rows:
            return
        batch: list[_FileContribution] = []
        for row in rows:
            contribution = _contribution_from_row(row, pins=pins)
            if contribution is not None:
                batch.append(contribution)
        # Advance keyset even when a page had only invisible rows so we cannot
        # spin forever on a dense missing/local-less band.
        keyset_path = str(rows[-1]["path"])
        if batch:
            yield batch
        if len(rows) < size:
            return


def _iter_visible_file_contributions(
    connection: Any,
    vault_id: int,
    *,
    path_prefix: str | None = None,
) -> list[_FileContribution]:
    """Compatibility helper: materialize contributions (tests / small scopes).

    Production rebuild and dirty refresh must not call this for unbounded
    prefixes — use batch streaming or :func:`_rebuild_directory` SQL rollup.
    """
    contributions: list[_FileContribution] = []
    for batch in _iter_visible_file_contribution_batches(
        connection,
        vault_id,
        path_prefix=path_prefix,
    ):
        contributions.extend(batch)
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


def _pin_match_sql(
    pins: tuple[tuple[str, bool], ...],
) -> tuple[str, list[Any]]:
    """SQL CASE expression counting lifecycle-pinned paths (bounded pin list)."""
    if not pins:
        return "0", []
    clauses: list[str] = []
    params: list[Any] = []
    for pin_path, is_directory in pins:
        normalized = str(pin_path).strip().strip("/")
        if not normalized:
            continue
        if is_directory:
            escaped = _escape_like(normalized)
            clauses.append("(fp.path = %s OR fp.path LIKE %s ESCAPE '\\')")
            params.extend([normalized, f"{escaped}/%"])
        else:
            clauses.append("fp.path = %s")
            params.append(normalized)
    if not clauses:
        return "0", []
    return f"CASE WHEN ({' OR '.join(clauses)}) THEN 1 ELSE 0 END", params


def _rebuild_directory(connection: Any, vault_id: int, directory: str) -> None:
    """Refresh one directory rollup via SQL aggregation (no descendant list)."""
    directory = (directory or "").strip().strip("/")
    if not directory:
        return
    pins = load_lifecycle_pins(connection, vault_id)
    pin_expr, pin_params = _pin_match_sql(pins)
    escaped = _escape_like(directory)
    # Classification mirrors _contribution_from_row / list_file_rows exactly.
    summary = connection.execute(
        f"""
        SELECT
            COUNT(*) AS item_count,
            COALESCE(SUM(classified.total_size), 0) AS total_size,
            COALESCE(SUM(classified.local_size), 0) AS local_size,
            COALESCE(SUM(classified.cloud_size), 0) AS cloud_size,
            COALESCE(SUM(classified.state_local_only), 0) AS state_local_only,
            COALESCE(SUM(classified.state_cloud_only), 0) AS state_cloud_only,
            COALESCE(SUM(classified.state_both), 0) AS state_both,
            COALESCE(SUM(classified.state_restoring), 0) AS state_restoring,
            COALESCE(SUM(classified.upload_eligible), 0) AS action_upload,
            COALESCE(SUM(classified.recover_eligible), 0) AS action_recover,
            COALESCE(SUM(classified.cleanup_eligible), 0) AS action_free_space,
            COALESCE(SUM(classified.has_cloud), 0) AS action_cloud_archive,
            COALESCE(SUM(classified.has_cloud), 0) AS action_cloud_purge,
            COALESCE(SUM(classified.storage_class_eligible), 0) AS action_storage_class,
            COALESCE(SUM(classified.lifecycle_pinned), 0) AS pinned_count
        FROM (
            SELECT
                CASE
                    WHEN lc.size IS NOT NULL THEN lc.size
                    ELSE COALESCE(av.size, 0)
                END AS total_size,
                CASE
                    WHEN lc.presence IN ('present', 'unsupported')
                    THEN COALESCE(lc.size, 0)
                    ELSE 0
                END AS local_size,
                CASE
                    WHEN av.id IS NOT NULL THEN COALESCE(av.size, 0)
                    ELSE 0
                END AS cloud_size,
                CASE
                    WHEN av.restore_state = 'restoring' THEN 0
                    WHEN lc.presence IN ('present', 'unsupported')
                     AND av.id IS NULL THEN 1
                    ELSE 0
                END AS state_local_only,
                CASE
                    WHEN av.restore_state = 'restoring' THEN 0
                    WHEN (
                        lc.presence IS NULL
                        OR lc.presence NOT IN ('present', 'unsupported')
                    ) AND av.id IS NOT NULL THEN 1
                    ELSE 0
                END AS state_cloud_only,
                CASE
                    WHEN av.restore_state = 'restoring' THEN 0
                    WHEN lc.presence IN ('present', 'unsupported')
                     AND av.id IS NOT NULL THEN 1
                    ELSE 0
                END AS state_both,
                CASE
                    WHEN av.restore_state = 'restoring' THEN 1
                    ELSE 0
                END AS state_restoring,
                CASE
                    WHEN lc.presence = 'present'
                     AND lc.file_type = 'regular'
                     AND av.id IS NULL THEN 1
                    ELSE 0
                END AS upload_eligible,
                CASE
                    WHEN (
                        lc.presence IS NULL
                        OR lc.presence NOT IN ('present', 'unsupported')
                    )
                     AND (
                        SELECT COUNT(*)
                        FROM archive_versions recoverable
                        WHERE recoverable.vault_file_id = vf.id
                          AND recoverable.integrity = 'verified'
                          AND recoverable.availability = 'available'
                     ) > 0 THEN 1
                    ELSE 0
                END AS recover_eligible,
                CASE
                    WHEN lc.presence = 'present'
                     AND lc.file_type = 'regular'
                     AND av.id IS NOT NULL
                     AND av.integrity = 'verified'
                     AND av.availability = 'available'
                     AND lc.matched_archive_version_id = av.id
                     AND lc.plaintext_sha256 IS NOT NULL
                     AND lc.plaintext_sha256 = av.plaintext_sha256 THEN 1
                    ELSE 0
                END AS cleanup_eligible,
                CASE
                    WHEN av.id IS NOT NULL THEN 1
                    ELSE 0
                END AS has_cloud,
                CASE
                    WHEN av.id IS NOT NULL
                     AND av.availability = 'available'
                     AND (
                        av.restore_state IS NULL
                        OR av.restore_state <> 'restoring'
                     ) THEN 1
                    ELSE 0
                END AS storage_class_eligible,
                {pin_expr} AS lifecycle_pinned
            FROM vault_files vf
            JOIN file_paths fp
              ON fp.vault_file_id = vf.id AND fp.valid_to IS NULL
            LEFT JOIN local_copies lc ON lc.vault_file_id = vf.id
            LEFT JOIN archive_versions av
              ON av.id = (
                  SELECT latest.id
                  FROM archive_versions latest
                  WHERE latest.vault_file_id = vf.id
                    AND latest.availability NOT IN ('missing', 'purged')
                  ORDER BY latest.version_number DESC
                  LIMIT 1
              )
            WHERE vf.vault_id = %s
              AND vf.status = 'active'
              AND fp.path LIKE %s ESCAPE '\\'
              AND (
                    lc.presence IN ('present', 'unsupported')
                 OR av.id IS NOT NULL
              )
        ) AS classified
        """,
        (*pin_params, int(vault_id), f"{escaped}/%"),
    ).fetchone()
    item_count = int(summary["item_count"] or 0) if summary else 0
    if item_count <= 0:
        connection.execute(
            """
            DELETE FROM directory_aggregates
            WHERE vault_id=%s AND path=%s
            """,
            (int(vault_id), directory),
        )
        return

    storage_rows = connection.execute(
        """
        SELECT av.storage_class AS storage_class, COUNT(*) AS total
        FROM vault_files vf
        JOIN file_paths fp
          ON fp.vault_file_id = vf.id AND fp.valid_to IS NULL
        LEFT JOIN local_copies lc ON lc.vault_file_id = vf.id
        JOIN archive_versions av
          ON av.id = (
              SELECT latest.id
              FROM archive_versions latest
              WHERE latest.vault_file_id = vf.id
                AND latest.availability NOT IN ('missing', 'purged')
              ORDER BY latest.version_number DESC
              LIMIT 1
          )
        WHERE vf.vault_id = %s
          AND vf.status = 'active'
          AND fp.path LIKE %s ESCAPE '\\'
          AND av.storage_class IS NOT NULL
          AND (
                lc.presence IN ('present', 'unsupported')
             OR av.id IS NOT NULL
          )
        GROUP BY av.storage_class
        """,
        (int(vault_id), f"{escaped}/%"),
    ).fetchall()
    storage_classes = {
        str(row["storage_class"]): int(row["total"] or 0)
        for row in storage_rows
        if row["storage_class"] is not None and int(row["total"] or 0) > 0
    }
    rollup = _DirectoryRollup(
        item_count=item_count,
        total_size=int(summary["total_size"] or 0),
        local_size=int(summary["local_size"] or 0),
        cloud_size=int(summary["cloud_size"] or 0),
        state_counts={
            "local_only": int(summary["state_local_only"] or 0),
            "cloud_only": int(summary["state_cloud_only"] or 0),
            "both": int(summary["state_both"] or 0),
            "restoring": int(summary["state_restoring"] or 0),
        },
        action_counts={
            "upload": int(summary["action_upload"] or 0),
            "recover": int(summary["action_recover"] or 0),
            "free-space": int(summary["action_free_space"] or 0),
            "cloud-archive": int(summary["action_cloud_archive"] or 0),
            "cloud-purge": int(summary["action_cloud_purge"] or 0),
            "storage-class": int(summary["action_storage_class"] or 0),
        },
        storage_classes=storage_classes,
        pinned_count=int(summary["pinned_count"] or 0),
    )
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
