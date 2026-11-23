"""Bounded Job read models. Group membership never crosses into the poll's Python loop.

A Job row holds only its core; the read model joins its File (path), group
(path), request (estimate, approval deadline/instant) and per-action details
(#589) through ``JOB_ROWS``, inlined by the planner.
"""
from typing import Any

from app.i18n import present_job_message, translate
from .job_queue import OPEN_JOB_SQL, open_job_sql
from .recovery_progress import origin_sql, ready_by
from .upload_remediation import project_jobs

PAGE_SIZE = 50
GROUP_KEY = "COALESCE(group_id::text, 'job-' || id::text)"
JOB_COLUMNS = """id,path,action,status,message,message_key,message_params,requested_at,
    updated_at,group_id,group_path,total_bytes,transferred_bytes,pending_until,
    estimated_cost_eur,estimated_hours,restore_tier,restore_days,approved_at,
    archive_version_id,upload_reconcile_only"""
# One flat row per Job for display. ``pending_until`` is what the Job waits
# for: its request's approval deadline, or a delayed deletion's due time.
JOB_ROWS = """job_rows AS NOT MATERIALIZED (
    SELECT j.id, j.vault_id, f.path, j.action, j.status, j.message, j.message_key,
        j.message_params::text AS message_params, j.requested_at, j.updated_at,
        j.group_id, g.path AS group_path, j.total_bytes, j.transferred_bytes,
        CASE j.status WHEN 'pending_approval' THEN rr.approval_deadline
                      WHEN 'pending_delay' THEN j.run_after END AS pending_until,
        rr.estimated_cost_eur, rr.estimated_hours, jr.tier AS restore_tier,
        jr.days AS restore_days, rr.approved_at, j.archive_version_id,
        COALESCE(ju.reconcile_only, false) AS upload_reconcile_only
    FROM jobs j
    JOIN files f ON f.id = j.file_id
    LEFT JOIN job_groups g ON g.id = j.group_id
    LEFT JOIN restore_requests rr ON rr.group_id = j.group_id
    LEFT JOIN job_restores jr ON jr.job_id = j.id
    LEFT JOIN job_uploads ju ON ju.job_id = j.id
)"""


def localized_jobs(connection: Any, rows: list[dict], locale: str) -> list[dict]:
    return [{**row, "message": present_job_message(row, locale)}
            for row in project_jobs(connection, rows)]


def list_jobs(connection: Any, *, vault_id: int, locale: str,
              actions: list[str] | None = None, active_only: bool = False,
              path: str | None = None, groups_before_id: int | None = None) -> dict:
    # Apply display filters BEFORE the bounded selection. In particular a flood
    # of uploads must not hide a pending restore, nor recent work hide a file's
    # old delayed deletion. Aggregation still includes ALL members of each group.
    filters = "vault_id=%s"
    params: list[Any] = [vault_id]
    if actions:
        filters += " AND action=ANY(%s)"
        params.append(actions)
    if active_only:
        filters += f" AND {OPEN_JOB_SQL}"
    if path is not None:
        filters += " AND path=%s"
        params.append(path)
    recent = connection.execute(
        f"WITH {JOB_ROWS} SELECT {JOB_COLUMNS} FROM job_rows WHERE {filters} "
        "ORDER BY requested_at DESC,id DESC LIMIT %s",
        [*params, PAGE_SIZE],
    ).fetchall()
    aggregates = connection.execute(f"""
        WITH {JOB_ROWS}, matching AS (
            SELECT DISTINCT {GROUP_KEY} AS group_key FROM job_rows WHERE {filters}
        ), members AS NOT MATERIALIZED (
            SELECT j.*, {GROUP_KEY} AS group_key FROM job_rows j WHERE vault_id=%s
        ), candidates AS (
            -- Match on display filters, but keep the ordering key independent
            -- of changing member status so completion cannot move a group
            -- backwards across an already returned pagination cursor.
            SELECT m.group_key, max(m.id) AS last_id
            FROM members m JOIN matching f ON f.group_key=m.group_key
            GROUP BY m.group_key
            HAVING max(m.id)<%s ORDER BY last_id DESC LIMIT %s
        ), totals AS (
            SELECT c.group_key, c.last_id, count(*) AS item_count,
                count(*) FILTER (WHERE j.status='completed') AS completed_count,
                count(*) FILTER (WHERE j.status='failed') AS failed_count,
                count(*) FILTER (WHERE j.status='failed' AND r.state='resolved') AS resolved_failed_count,
                count(*) FILTER (WHERE j.status='cancelled') AS cancelled_count,
                sum(COALESCE(j.total_bytes,0)) AS total_bytes,
                sum(CASE WHEN j.status='completed' THEN COALESCE(j.total_bytes,0)
                    ELSE least(COALESCE(j.transferred_bytes,0),COALESCE(j.total_bytes,0)) END) AS transferred_bytes,
                max(j.updated_at) AS updated_at,
                min(j.requested_at) AS requested_at,
                max(j.pending_until) AS pending_until,
                max(j.estimated_cost_eur) AS estimated_cost_eur,
                max(j.estimated_hours) AS estimated_hours,
                max(j.restore_tier) AS restore_tier, max(j.restore_days) AS restore_days
            FROM candidates c JOIN members j ON j.group_key=c.group_key
            LEFT JOIN upload_remediation_jobs b ON b.job_id=j.id
            LEFT JOIN upload_remediations r ON r.id=b.remediation_id
            GROUP BY c.group_key,c.last_id
        ), representatives AS (
            SELECT DISTINCT ON (j.group_key) j.group_key,j.path,j.group_path,j.action,
                j.message,j.message_key,j.message_params
            FROM members j JOIN candidates c ON c.group_key=j.group_key
            ORDER BY j.group_key,(j.status<>'completed') DESC,j.requested_at ASC,j.id ASC
        ), active AS (
            SELECT DISTINCT ON (j.group_key) j.group_key,j.status
            FROM members j JOIN candidates c ON c.group_key=j.group_key
            WHERE {open_job_sql("j")}
            ORDER BY j.group_key,j.requested_at ASC,j.id ASC
        )
        SELECT t.*, representative.path, representative.group_path, representative.action,
            representative.message, representative.message_key, representative.message_params,
            active.status AS active_status
        FROM totals t JOIN representatives representative ON representative.group_key=t.group_key
        LEFT JOIN active ON active.group_key=t.group_key
        ORDER BY t.last_id DESC
        """, [*params, vault_id, groups_before_id or 2**63-1, PAGE_SIZE+1]).fetchall()
    origins = _resumed_restore_origins(connection, vault_id, [
        row["group_key"] for row in aggregates[:PAGE_SIZE]
        if row["action"] == "recover" and row["active_status"] is not None])
    groups = []
    for row in aggregates[:PAGE_SIZE]:
        group = dict(row)
        origin = origins.get(group["group_key"])
        if origin is not None and origin < group["requested_at"]:
            # A resumed cloud restore began with its first request (#677).
            group["requested_at"] = origin
        group["id"] = group.pop("group_key")
        group.pop("last_id")
        group["path"] = group.pop("group_path") or group["path"]
        group["status"] = group.pop("active_status") or (
            "failed" if group["failed_count"] else "cancelled" if group["cancelled_count"] else "completed")
        group["unresolved_failed_count"] = group["failed_count"] - group["resolved_failed_count"]
        # A cloud restore takes hours: the panel shows when it should be ready
        # instead of a byte percentage that stays at zero meanwhile (#646).
        group["ready_by"] = ready_by(group["requested_at"], group["estimated_hours"])
        for field in ("total_bytes", "transferred_bytes"):
            group[field] = int(group[field])
        finished = group["completed_count"] + group["failed_count"] + group["cancelled_count"]
        group["percent"] = round(100 * group["transferred_bytes"] / group["total_bytes"]
                                 if group["total_bytes"] else 100 * finished / group["item_count"])
        group["message"] = present_job_message(group, locale)
        if not group["message"] and group["status"] in {"failed", "cancelled"}:
            group["message_key"] = "job.operation_failed" if group["status"] == "failed" else "job.operation_stopped"
            group["message"] = translate(group["message_key"], locale=locale)
        group.pop("message_params", None)
        groups.append(group)
    return {"items": localized_jobs(connection, recent, locale), "groups": groups, "locale": locale,
            "next_groups_before_id": aggregates[PAGE_SIZE-1]["last_id"] if len(aggregates)>PAGE_SIZE else None}


def group_jobs(connection: Any, *, vault_id: int, group_id: str, locale: str,
               before_id: int | None, limit: int) -> dict:
    rows = connection.execute(f"""WITH {JOB_ROWS} SELECT {JOB_COLUMNS} FROM job_rows
        WHERE vault_id=%s AND {GROUP_KEY}=%s AND id<%s ORDER BY id DESC LIMIT %s""",
        (vault_id, group_id, before_id or 2**63-1, limit+1)).fetchall()
    return {"items": localized_jobs(connection, rows[:limit], locale),
            "next_before_id": rows[limit-1]["id"] if len(rows)>limit else None}


def _resumed_restore_origins(connection: Any, vault_id: int, group_keys: list[str]) -> dict[str, Any]:
    """First request of the cloud restores that active Recovery groups resume."""
    if not group_keys:
        return {}
    rows = connection.execute(f"""
        SELECT COALESCE(j.group_id::text, 'job-' || j.id::text) AS group_key,
            min(o.origin_requested_at) AS origin
        FROM jobs j CROSS JOIN LATERAL {origin_sql("j")} o
        WHERE j.vault_id=%s AND j.action='recover' AND {open_job_sql("j")}
          AND COALESCE(j.group_id::text, 'job-' || j.id::text) = ANY(%s)
        GROUP BY 1
        """, [vault_id, group_keys]).fetchall()
    return {row["group_key"]: row["origin"] for row in rows}


_JOB_HISTORY_COLUMNS = "id,path,action,status,message,message_key,message_params,requested_at,updated_at,group_id,group_path,total_bytes,transferred_bytes,archive_version_id,upload_reconcile_only"


def job_record(connection: Any, *, vault_id: int, job_id: int, locale: str) -> dict | None:
    """One Job of this Vault for display, or ``None`` when it is not there."""
    row = connection.execute(f"WITH {JOB_ROWS} SELECT {_JOB_HISTORY_COLUMNS} FROM job_rows WHERE id=%s AND vault_id=%s", (job_id,vault_id)).fetchone()
    if not row:
        return None
    projected = project_jobs(connection,[row])[0]
    return {**projected,"message":present_job_message(row,locale)}


def file_jobs(connection: Any, *, vault_id: int, path: str, locale: str,
              before_id: int | None, limit: int) -> dict | None:
    """Newest-first keyset page of one Vault File's Jobs; ``None`` without the file."""
    file = connection.execute("SELECT id FROM files WHERE vault_id=%s AND path=%s",(vault_id,path)).fetchone()
    if not file:
        return None
    rows = connection.execute(f"WITH {JOB_ROWS} SELECT {_JOB_HISTORY_COLUMNS} FROM job_rows WHERE vault_id=%s AND file_id=%s AND id<%s ORDER BY id DESC LIMIT %s",
        (vault_id,file["id"],before_id or 2**63-1,limit+1)).fetchall()
    items = project_jobs(connection,rows[:limit])
    return {"items":[{**row,"message":present_job_message(row,locale)} for row in items],
            "next_before_id":items[-1]["id"] if len(rows)>limit else None}


# An upload that has not begun: waiting in the queue, never admitted to the
# upload provenance machinery, nothing linked in the cloud, not part of a
# remediation episode. Cancelling anything else could close an ambiguous send
# for good (ADR-0024/0025), so it stays with the single-Job cancel and Resume.
_UNSTARTED_UPLOAD_SQL = """
    j.vault_id=%s AND j.action='upload'
    AND j.status IN ('queued', 'retrying')
    AND j.archive_version_id IS NULL
    AND NOT EXISTS (SELECT 1 FROM job_uploads b
                    WHERE b.job_id=j.id AND (b.reconcile_only OR b.snapshot_id IS NOT NULL))
    AND NOT EXISTS (SELECT 1 FROM upload_attempts a
                    WHERE a.file_id=j.file_id AND a.send_started_at IS NOT NULL
                      AND a.settled_at IS NULL)
    AND NOT EXISTS (SELECT 1 FROM upload_remediation_jobs rj WHERE rj.job_id=j.id)
    AND NOT EXISTS (SELECT 1 FROM upload_remediations r
                    WHERE r.file_id=j.file_id AND r.closed_at IS NULL)
"""


def unstarted_upload_job_ids(connection: Any, *, vault_id: int, folder: str) -> list[int]:
    """The uploads of the Vault, or of one folder and below, that have not begun.

    An empty ``folder`` is the whole Vault; a folder matches itself and its
    descendants only (``foo`` never matches ``foobar``).
    """
    scope_sql, scope_params = "", []
    if folder:
        scope_sql = "AND (f.path=%s OR starts_with(f.path, %s))"
        scope_params = [folder, folder + "/"]
    rows = connection.execute(
        f"SELECT j.id FROM jobs j JOIN files f ON f.id=j.file_id "
        f"WHERE {_UNSTARTED_UPLOAD_SQL} {scope_sql} ORDER BY j.id",
        [vault_id, *scope_params],
    ).fetchall()
    return [int(row["id"]) for row in rows]
