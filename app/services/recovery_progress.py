"""Read-only progress of a Vault File's Recovery (#646).

A Recovery is the Owner's request to bring a cloud-only file back. Its
progress is read from the latest ``recover`` Job of the file and from the
last observed cloud restore of its Current Snapshot (``restore_state``). The
cloud restore outlives a stopped Job: AWS keeps restoring (and charging) and a
new Recovery resumes it while the temporary copy lasts.

States:
    queued             a Recovery Job waits for a worker
    awaiting_approval  the request waits for the Owner's Restore Approval
    waiting_for_cloud  the cloud restore is in progress
    ready              a restored temporary copy is readable until expires_at
    downloading        the file is being downloaded and verified
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .restore_estimates import DEFAULT_RESTORE_HOURS

TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})

# A Recovery Job stopped while AWS was restoring ends with this key: the cloud
# restore it requested goes on, and a new Recovery resumes it (#677).
STOPPED_WAITING_KEY = "job.recovery_stopped_waiting"

# Restore hours never exceed two days (Deep Archive Bulk): past that plus the
# copy's lifetime, an old request cannot be the restore still observed.
_MAX_RESTORE_HOURS = 48


def origin_sql(job: str) -> str:
    """The first request of the cloud restore that Job ``{job}`` resumes.

    ``{job}`` exposes ``id``, ``file_id``, ``archive_version_id`` and
    ``requested_at``. Walking back from it, consecutive Recovery Jobs of the
    same Archive Version stopped while AWS was restoring form one cloud
    restore; the oldest of them sent it. None when ``{job}`` resumed nothing.
    """
    return f"""(
    SELECT p.requested_at AS origin_requested_at, pr.tier AS origin_tier, pr.days AS origin_days
    FROM jobs p LEFT JOIN job_restores pr ON pr.job_id = p.id
    WHERE p.file_id = {job}.file_id AND p.action = 'recover' AND p.id < {job}.id
      AND p.message_key = '{STOPPED_WAITING_KEY}'
      AND p.archive_version_id = {job}.archive_version_id
      AND p.requested_at + make_interval(hours => {_MAX_RESTORE_HOURS}, days => coalesce(pr.days, 1))
          > {job}.requested_at
      AND NOT EXISTS (
          SELECT 1 FROM jobs q
          WHERE q.file_id = p.file_id AND q.action = 'recover'
            AND q.id > p.id AND q.id < {job}.id
            AND (q.message_key IS DISTINCT FROM '{STOPPED_WAITING_KEY}'
                 OR q.archive_version_id IS DISTINCT FROM p.archive_version_id)
      )
    ORDER BY p.id ASC LIMIT 1
)"""


# Latest Recovery Job of ``f`` with its requested restore parameters. Jobs are
# found through ``jobs_file_idx``; a file has few Recovery Jobs, so a browse
# page stays one bounded lookup per listed file.
# The second lookup runs only for a file whose cloud restore is observed.
LATEST_RECOVERY_JOIN_SQL = f"""LEFT JOIN LATERAL (
    SELECT j.id AS recovery_job_id, j.file_id, j.archive_version_id,
        j.status AS recovery_job_status, j.requested_at AS recovery_requested_at,
        jr.tier AS recovery_tier, jr.days AS recovery_days
    FROM jobs j LEFT JOIN job_restores jr ON jr.job_id = j.id
    WHERE j.file_id = f.id AND j.action = 'recover'
    ORDER BY j.id DESC LIMIT 1
) recovery ON true
LEFT JOIN LATERAL (
    SELECT o.origin_requested_at, o.origin_tier, o.origin_days
    FROM (SELECT recovery.recovery_job_id AS id, recovery.file_id,
              recovery.archive_version_id, recovery.recovery_requested_at AS requested_at
          WHERE cur.restore_state IS NOT NULL) latest
    CROSS JOIN LATERAL {origin_sql("latest")} o
) recovery_origin ON true"""


def _instant(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        instant = value
    else:
        try:
            instant = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)


def _iso(value: Any) -> str | None:
    instant = _instant(value)
    return instant.isoformat() if instant else None


def ready_by(requested_at: Any, hours: float | None) -> str | None:
    """Expected readiness: request instant plus the tier's restore hours."""
    instant = _instant(requested_at)
    if instant is None or not hours:
        return None
    return (instant + timedelta(hours=float(hours))).isoformat()


def tier_hours(storage_class: Any, tier: Any) -> float | None:
    if not tier:
        return None
    hours = DEFAULT_RESTORE_HOURS.get(str(storage_class or "").upper(), {}).get(str(tier))
    return float(hours) if hours else None


def project_recovery(row: Mapping[str, Any], *, local_exists: bool,
                     now: datetime | None = None) -> dict[str, Any] | None:
    """The ``recovery`` object of a browse item, or None when none is in progress."""
    status = row.get("recovery_job_status")
    active = status is not None and status not in TERMINAL_JOB_STATUSES
    restore_state = row.get("restore_state")
    expiry = _instant(row.get("restore_expiry"))
    copy_readable = restore_state == "available" and (
        expiry is None or expiry > (now or datetime.now(timezone.utc))
    )
    if active:
        if status in {"downloading", "verifying"}:
            state = "downloading"
        elif status == "pending_approval":
            state = "awaiting_approval"
        elif status == "restoring" or restore_state == "restoring":
            state = "waiting_for_cloud"
        elif copy_readable:
            state = "ready"
        else:
            state = "queued"
    elif local_exists:
        return None
    elif restore_state == "restoring":
        state = "waiting_for_cloud"
    elif copy_readable:
        state = "ready"
    else:
        return None
    # A Recovery that resumes a cloud restore shows the restore AWS runs: its
    # first request, tier and lifetime, not the later click (#677).
    resumed = row.get("origin_requested_at") is not None
    requested_at = row.get("origin_requested_at") if resumed else row.get("recovery_requested_at")
    tier = (row.get("origin_tier") or row.get("recovery_tier")) if resumed else row.get("recovery_tier")
    days = (row.get("origin_days") or row.get("recovery_days")) if resumed else row.get("recovery_days")
    expected = (
        ready_by(requested_at, tier_hours(row.get("storage_class"), tier))
        if state in {"queued", "awaiting_approval", "waiting_for_cloud"} else None
    )
    return {
        "state": state,
        "active": active,
        "tier": tier,
        "days": days,
        "requested_at": _iso(requested_at),
        "ready_by": expected,
        "expires_at": expiry.isoformat() if copy_readable and expiry else None,
    }
