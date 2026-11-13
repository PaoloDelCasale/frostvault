"""Retention of closed operational history (#596, ADR-0035).

Terminal Jobs and read or resolved notifications are collected after
``JOB_RETENTION_DAYS`` / ``NOTIFICATION_RETENTION_DAYS`` (30), audit events
after ``AUDIT_RETENTION_DAYS`` (90). The proofs that hang off closed work
(settled upload and copy attempts with their receipts, closed Upload
Snapshots, closed remediation episodes, finished Job groups, the catalog
journal and File rows left without any cloud content) follow the Job window
once nothing references them any more.

Retention only deletes database rows. It never deletes a local file, a cloud
object or a metadata backup, and it never decides a cloud state.

What is never collected, whatever its age:

* a Current, a replacement in progress, a version a cleanup grant still
  names, or anything an open cleanup batch needs;
* an unsettled attempt, an attempt with an open multipart upload or a receipt
  that contradicts its settlement (open uncertainty), and an attempt whose
  landed version still exists;
* an open remediation episode, an open problem, a notification still in the
  terminal outbox or with a pending delivery;
* a Job group whose request still holds work, a cleanup grant or an active
  Stop Protection consent;
* anything referenced by history that is itself kept: a settlement or an age
  alone is never enough.

Every pass is a keyset walk in short transactions of at most ``batch_size``
rows. A batch reads its candidates, locks them with ``FOR UPDATE SKIP LOCKED``
and deletes them with a statement that rechecks every predicate on a fresh
snapshot: a writer that referenced a candidate before the lock either still
holds a key lock (the row is skipped) or has committed (the recheck sees it),
and a writer that comes after the lock waits for this transaction and then
finds the row gone (its foreign key fails; it never attaches to a deleted
row). A batch that still trips a guard is retried row by row so one row never
blocks the rest.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

import psycopg

from ..config import settings
from .catalog_events import CatalogEventStore
from .directory_aggregates import mark_path_dirty

POLL_SECONDS = 60 * 60
BATCH_SIZE = 500

_TERMINAL = "('completed', 'failed', 'cancelled')"


@dataclass(frozen=True)
class Windows:
    jobs: timedelta
    notifications: timedelta
    audit: timedelta

    @classmethod
    def from_settings(cls) -> "Windows":
        return cls(
            jobs=timedelta(days=settings.job_retention_days),
            notifications=timedelta(days=settings.notification_retention_days),
            audit=timedelta(days=settings.audit_retention_days),
        )


# -- predicates ----------------------------------------------------------------
# Each predicate is written against the alias of its table and the cutoff
# parameter %(cutoff)s. The same text selects candidates and rechecks them in
# the DELETE.

_NOTIFICATION = """
    n.problem_status IS DISTINCT FROM 'open'
    AND (
        (n.problem_status = 'resolved' AND n.resolved_at < %(cutoff)s)
        OR (n.problem_status IS NULL AND n.read_at < %(cutoff)s)
        OR (n.problem_status IS NULL AND NOT n.in_app_enabled AND n.created_at < %(cutoff)s)
    )
    AND NOT EXISTS (
        SELECT 1 FROM notification_deliveries d
        WHERE d.notification_id = n.id AND d.status = 'pending'
    )
"""

_JOB = f"""
    j.status IN {_TERMINAL} AND j.finished_at < %(cutoff)s
    AND NOT EXISTS (SELECT 1 FROM job_terminal_notifications o WHERE o.job_id = j.id)
    AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.job_id = j.id)
    AND NOT EXISTS (SELECT 1 FROM job_leases l WHERE l.job_id = j.id)
    AND NOT EXISTS (
        SELECT 1 FROM upload_attempts a WHERE a.job_id = j.id AND a.settled_at IS NULL
    )
    AND NOT EXISTS (
        SELECT 1 FROM copy_attempts a WHERE a.job_id = j.id AND a.settled_at IS NULL
    )
    AND NOT EXISTS (SELECT 1 FROM cleanup_batches b WHERE b.job_id = j.id)
    AND NOT EXISTS (
        SELECT 1 FROM job_uploads u JOIN upload_snapshots s ON s.id = u.snapshot_id
        WHERE u.job_id = j.id AND s.closed_at IS NULL
    )
    AND NOT EXISTS (
        SELECT 1 FROM upload_remediations r
        WHERE r.closed_at IS NULL AND (r.current_job_id = j.id OR r.resolved_by_job_id = j.id)
    )
    AND NOT EXISTS (
        SELECT 1 FROM upload_remediation_jobs rj
        JOIN upload_remediations r ON r.id = rj.remediation_id
        WHERE rj.job_id = j.id AND r.closed_at IS NULL
    )
    AND NOT EXISTS (
        SELECT 1 FROM files f
        WHERE f.id = j.file_id AND j.archive_version_id IS NOT NULL
          AND f.replacing_version_id = j.archive_version_id
    )
"""

# A request with no Job left holds no work; its approval or consent is
# history, except an active Stop Protection consent of a stopping Vault.
_GROUP = """
    g.created_at < %(cutoff)s
    AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.group_id = g.id)
    AND NOT EXISTS (SELECT 1 FROM cleanup_batches b WHERE b.purge_group_id = g.id)
    AND NOT EXISTS (
        SELECT 1 FROM cloud_purge_requests p JOIN vaults v ON v.id = g.vault_id
        WHERE p.group_id = g.id AND p.stop_protection AND v.lifecycle = 'stopping'
    )
"""

_REMEDIATION = "r.closed_at IS NOT NULL AND r.closed_at < %(cutoff)s"

# A settled attempt follows its Job (job_id is cleared when the Job is
# collected) and is history only when nothing else depends on it: its
# version is gone (removed by a completed cleanup, #591) or it never produced
# one, and the file has no open work that could still settle, materialize or
# reconcile it. The guard triggers forbid open uncertainty independently.
_ATTEMPT_FILE_QUIET = f"""
    AND NOT EXISTS (
        SELECT 1 FROM jobs j WHERE j.file_id = a.file_id AND j.status NOT IN {_TERMINAL}
    )
    AND NOT EXISTS (SELECT 1 FROM cleanup_batches b WHERE b.file_id = a.file_id)
    AND NOT EXISTS (
        SELECT 1 FROM upload_remediations r WHERE r.file_id = a.file_id AND r.closed_at IS NULL
    )
    AND EXISTS (
        SELECT 1 FROM files f
        WHERE f.id = a.file_id AND f.replacing_version_id IS NULL
          AND f.reconciliation_block IS NULL
    )
"""

_UPLOAD_ATTEMPT = f"""
    a.settled_at < %(cutoff)s
    AND a.job_id IS NULL
    AND NOT EXISTS (SELECT 1 FROM archive_versions v WHERE v.upload_attempt_id = a.id)
    AND NOT EXISTS (SELECT 1 FROM upload_remediations r WHERE r.attempt_id = a.id
                    AND r.closed_at IS NULL)
    AND NOT EXISTS (SELECT 1 FROM upload_multiparts m WHERE m.attempt_id = a.id
                    AND m.closed_at IS NULL)
    AND NOT EXISTS (
        SELECT 1 FROM upload_receipts rc WHERE rc.attempt_id = a.id
          AND (a.outcome <> 'landed'
               OR (rc.response ->> 'VersionId') IS DISTINCT FROM a.landed_version_id)
    )
    {_ATTEMPT_FILE_QUIET}
"""

_COPY_ATTEMPT = f"""
    a.settled_at < %(cutoff)s
    AND a.job_id IS NULL
    AND NOT EXISTS (SELECT 1 FROM archive_versions v WHERE v.copy_attempt_id = a.id)
    AND NOT EXISTS (SELECT 1 FROM copy_multiparts m WHERE m.attempt_id = a.id
                    AND m.closed_at IS NULL)
    AND NOT EXISTS (
        SELECT 1 FROM copy_receipts rc WHERE rc.attempt_id = a.id
          AND (a.outcome <> 'landed'
               OR (rc.response ->> 'VersionId') IS DISTINCT FROM a.landed_version_id)
    )
    {_ATTEMPT_FILE_QUIET}
"""

_SNAPSHOT = """
    s.closed_at < %(cutoff)s
    AND NOT EXISTS (SELECT 1 FROM upload_attempts a WHERE a.snapshot_id = s.id)
    AND NOT EXISTS (SELECT 1 FROM job_uploads u WHERE u.snapshot_id = s.id)
"""

# A File row left after a purge: no Current, no replacement, nothing local to
# describe and no history attached (the RESTRICT keys of every proof). Only
# in an active Vault: a disconnected root makes present files look missing.
_FILE = """
    f.current_version_id IS NULL AND f.replacing_version_id IS NULL
    AND f.matched_version_id IS NULL AND f.held_version_id IS NULL
    AND f.cloud_since IS NOT NULL
    AND (f.local_presence IS NULL OR f.local_presence = 'missing')
    AND f.reconciliation_block IS NULL
    AND GREATEST(f.created_at, f.path_since, f.local_seen_at, f.local_observed_at,
                 f.local_content_observed_at, f.local_recovered_at, f.pending_since,
                 f.cloud_since) < %(cutoff)s
    AND EXISTS (SELECT 1 FROM vaults v WHERE v.id = f.vault_id AND v.lifecycle = 'active')
    AND NOT EXISTS (SELECT 1 FROM archive_versions x WHERE x.file_id = f.id)
    AND NOT EXISTS (SELECT 1 FROM jobs x WHERE x.file_id = f.id)
    AND NOT EXISTS (SELECT 1 FROM upload_snapshots x WHERE x.file_id = f.id)
    AND NOT EXISTS (SELECT 1 FROM copy_attempts x WHERE x.file_id = f.id)
    AND NOT EXISTS (SELECT 1 FROM cleanup_batches x WHERE x.file_id = f.id)
    AND NOT EXISTS (SELECT 1 FROM upload_remediations x WHERE x.file_id = f.id)
"""

_AUDIT = "e.created_at < %(cutoff)s"


@dataclass(frozen=True)
class _Pass:
    name: str
    table: str
    alias: str
    key: str  # ordered keyset columns, the last one is the primary key
    predicate: str
    window: str  # 'jobs' | 'notifications' | 'audit'
    returning: str = ""


# Order matters only for throughput: dependents go before what they reference
# so one run usually collects a whole closed chain.
_PASSES: tuple[_Pass, ...] = (
    _Pass("notifications", "notifications", "n", "id", _NOTIFICATION, "notifications"),
    _Pass("remediations", "upload_remediations", "r", "closed_at, id", _REMEDIATION, "jobs"),
    _Pass("jobs", "jobs", "j", "finished_at, id", _JOB, "jobs"),
    _Pass("job_groups", "job_groups", "g", "id", _GROUP, "jobs"),
    _Pass("upload_attempts", "upload_attempts", "a", "settled_at, id", _UPLOAD_ATTEMPT, "jobs"),
    _Pass("copy_attempts", "copy_attempts", "a", "settled_at, id", _COPY_ATTEMPT, "jobs"),
    _Pass("upload_snapshots", "upload_snapshots", "s", "closed_at, id", _SNAPSHOT, "jobs"),
    _Pass("files", "files", "f", "id", _FILE, "jobs", returning=", f.vault_id, f.path"),
    _Pass("audit_events", "audit_events", "e", "created_at, id", _AUDIT, "audit"),
)

# The candidate query of a pass reads the keyset index first; the partial
# filter keeps it to rows that can ever qualify.
_SCAN_FILTER = {
    "notifications": "(n.problem_status IS DISTINCT FROM 'open')",
    "upload_remediations": "r.closed_at IS NOT NULL",
    "jobs": "j.finished_at IS NOT NULL",
    "job_groups": "true",
    "upload_attempts": "a.settled_at IS NOT NULL",
    "copy_attempts": "a.settled_at IS NOT NULL",
    "upload_snapshots": "s.closed_at IS NOT NULL",
    "files": "f.current_version_id IS NULL AND f.cloud_since IS NOT NULL",
    "audit_events": "true",
}


def _keyset(spec: _Pass, after: tuple | None) -> tuple[str, list[Any]]:
    columns = [f"{spec.alias}.{c.strip()}" for c in spec.key.split(",")]
    if after is None:
        return "true", []
    placeholders = ", ".join(["%s"] * len(columns))
    return f"({', '.join(columns)}) > ({placeholders})", list(after)


def _fill(sql: str, cutoff: datetime) -> str:
    # Inline the cutoff as a typed literal so predicates compose with
    # positional parameters; it is a datetime we built, never caller text.
    return sql.replace("%(cutoff)s", f"'{cutoff.isoformat()}'::timestamptz")


def _collect_batch(connection: Any, spec: _Pass, cutoff: datetime, after: tuple | None,
                   batch_size: int) -> tuple[list[tuple], list[dict]]:
    predicate = _fill(spec.predicate, cutoff)
    keyset, params = _keyset(spec, after)
    keys = [c.strip() for c in spec.key.split(",")]
    order = ", ".join(f"{spec.alias}.{c}" for c in keys)
    candidates = connection.execute(
        f"""SELECT {order} FROM {spec.table} {spec.alias}
            WHERE {_SCAN_FILTER[spec.table]} AND {keyset} AND {predicate}
            ORDER BY {order} LIMIT %s""",
        (*params, batch_size),
    ).fetchall()
    if not candidates:
        return [], []
    last = tuple(candidates[-1][c] for c in keys)
    ids = [row["id"] for row in candidates]
    locked_ids = _lock(connection, spec, ids)
    if not locked_ids:
        return [last], []
    # A fresh statement: the predicate is evaluated again after the lock.
    deleted = _delete(connection, spec, predicate, locked_ids)
    return [last], deleted


def _lock(connection: Any, spec: _Pass, ids: Sequence[Any]) -> list[Any]:
    """Lock the candidates no writer holds; a held one waits for the next run."""
    rows = connection.execute(
        f"SELECT id FROM {spec.table} WHERE id = ANY(%s) ORDER BY id FOR UPDATE SKIP LOCKED",
        (list(ids),),
    ).fetchall()
    return [row["id"] for row in rows]


def _delete(connection: Any, spec: _Pass, predicate: str, ids: Sequence[Any]) -> list[dict]:
    sql = (f"DELETE FROM {spec.table} {spec.alias} WHERE {spec.alias}.id = ANY(%s) "
           f"AND {predicate} RETURNING {spec.alias}.id{spec.returning}")
    connection.execute("SAVEPOINT history_retention_batch")
    try:
        rows = connection.execute(sql, (list(ids),)).fetchall()
    except psycopg.errors.IntegrityError:
        connection.execute("ROLLBACK TO SAVEPOINT history_retention_batch")
    else:
        connection.execute("RELEASE SAVEPOINT history_retention_batch")
        return rows
    # One row tripped a guard or a key the predicate did not foresee: keep
    # it and collect the others.
    rows = []
    for row_id in ids:
        connection.execute("SAVEPOINT history_retention_row")
        try:
            rows.extend(connection.execute(sql, ([row_id],)).fetchall())
        except psycopg.errors.IntegrityError:
            connection.execute("ROLLBACK TO SAVEPOINT history_retention_row")
        else:
            connection.execute("RELEASE SAVEPOINT history_retention_row")
    return rows


def _run_pass(db: Callable[[], Any], spec: _Pass, cutoff: datetime, batch_size: int) -> int:
    after: tuple | None = None
    total = 0
    while True:
        with db() as connection:
            last, deleted = _collect_batch(connection, spec, cutoff, after, batch_size)
            for row in deleted:
                if spec.table == "files":
                    mark_path_dirty(connection, int(row["vault_id"]), row["path"])
        if not last:
            return total
        total += len(deleted)
        after = last[0]


def _prune_catalog_events(db: Callable[[], Any], cutoff: datetime, batch_size: int) -> int:
    """Advance each Vault's retained_from_revision past events older than cutoff.

    Readers resuming before the new boundary see an explicit retention gap
    (``vault_catalog_revisions.retained_from_revision``) and reload.
    """
    total = 0
    after_vault = 0
    while True:
        with db() as connection:
            vault = connection.execute(
                "SELECT vault_id FROM vault_catalog_revisions WHERE vault_id > %s "
                "ORDER BY vault_id LIMIT 1", (after_vault,)).fetchone()
        if vault is None:
            return total
        after_vault = int(vault["vault_id"])
        while True:
            with db() as connection:
                state = connection.execute(
                    "SELECT revision, retained_from_revision FROM vault_catalog_revisions "
                    "WHERE vault_id=%s", (after_vault,)).fetchone()
                if state is None:
                    break
                newest_kept = connection.execute(
                    "SELECT revision FROM catalog_events WHERE vault_id=%s AND revision>=%s "
                    "AND created_at >= %s ORDER BY revision LIMIT 1",
                    (after_vault, state["retained_from_revision"], cutoff)).fetchone()
                target = (int(newest_kept["revision"]) if newest_kept
                          else int(state["revision"]) + 1)
                retained = int(state["retained_from_revision"])
                if target <= retained:
                    break
                step = min(target, retained + batch_size)
                result = CatalogEventStore(connection).prune_events(
                    vault_id=after_vault, retain_from_revision=step)
                total += int(result["deleted"])


def collect(db: Callable[[], Any], *, now: datetime | None = None,
            windows: Windows | None = None, batch_size: int = BATCH_SIZE) -> dict[str, int]:
    """Run every retention pass once; returns the rows deleted per pass."""
    now = now or datetime.now(timezone.utc)
    windows = windows or Windows.from_settings()
    counts: dict[str, int] = {}
    for spec in _PASSES:
        cutoff = now - getattr(windows, spec.window)
        counts[spec.name] = _run_pass(db, spec, cutoff, batch_size)
    counts["catalog_events"] = _prune_catalog_events(db, now - windows.jobs, batch_size)
    return counts
