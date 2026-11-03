"""Read-only protection monitoring and weekly Owner summaries (#425).

Only notification state and pending timers are written. No admission, transfer,
verification, recovery or deletion is authorized here. Timers survive restart;
content changing repeatedly does not restart the continuous unprotected age.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
import time

_last_maintenance = 0.0

from . import notifications as inbox
from .operation_policies import auto_upload_path_is_included, file_is_stable, get_policy
from .protection_states import LATEST_JOIN_SQL, PROTECTED_SQL, STATE_SQL, STATES, VISIBLE_SQL


def _time(value: str | None, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except ValueError:
        return fallback


def delay_counts(rows: list[Mapping[str, Any]], *, now: datetime,
                 last_progress: str | None, stability_seconds: int) -> dict[str, int]:
    """Progress suppresses stall warnings, not the continuous 24-hour ceiling."""
    overdue = unstable = 0
    stable_waits = []
    for row in rows:
        since = _time(row.get("pending_since"), now)
        stable = file_is_stable(mtime_ns=int(row["mtime_ns"] or 0), now=now,
                               stability_seconds=stability_seconds,
                               content_observed_at=row.get("content_observed_at"))
        if now - since >= timedelta(hours=24):
            overdue += 1
            unstable += int(not stable)
        if stable:
            # Time still being copied is not a stalled upload queue.
            stable_waits.append(max(since, _time(row.get("content_observed_at"), since))
                                + timedelta(seconds=stability_seconds))
    stall = bool(stable_waits) and now - max(
        min(stable_waits), _time(last_progress, min(stable_waits))
    ) >= timedelta(hours=2)
    return {"pending": len(rows), "overdue": overdue, "unstable": unstable,
            "stalled": len(stable_waits) if stall else 0}


def monitor_vault(connection: Any, vault: Mapping[str, Any], *, now: datetime) -> None:
    from .source_layout import vault_local_access
    vault_id = int(vault["id"])
    policy = get_policy(connection, vault_id)
    suspended = connection.execute(
        "SELECT suspended_at FROM vault_operation_policies WHERE vault_id=%s", (vault_id,),
    ).fetchone()
    suspended_since = suspended["suspended_at"] if suspended else None
    if not policy.auto_upload and not suspended_since:
        suspended_since = now.isoformat()
        connection.execute("UPDATE vault_operation_policies SET suspended_at=%s WHERE vault_id=%s",
                           (suspended_since, vault_id))
    inbox.set_problem(connection, key=f"blocked:{vault_id}:suspended", event="protection_blocked",
                      vault_id=vault_id, active=not policy.auto_upload and
                      now - _time(suspended_since, now) >= timedelta(hours=24),
                      params={"component": "suspended"})
    access = vault_local_access(str(vault["source_root"]))
    # This gate checks the mount only. A successful mount check cannot resolve
    # a failed full source scan; that belongs to record_component_health.
    inbox.set_problem(connection, key=f"blocked:{vault_id}:volume", event="protection_blocked",
                      vault_id=vault_id, active=not access.local_operations_allowed,
                      params={"component": "source_volume"})
    rows = connection.execute(f"""
        SELECT vf.id, fp.path, lc.mtime_ns, lc.content_observed_at,
               COALESCE(lc.content_observed_at,lc.observed_at,vf.created_at) AS observed_at
        FROM vault_files vf JOIN local_copies lc ON lc.vault_file_id=vf.id
        JOIN file_paths fp ON fp.vault_file_id=vf.id AND fp.valid_to IS NULL
        {LATEST_JOIN_SQL}
        WHERE vf.vault_id=%s AND vf.status='active' AND lc.presence='present'
          AND lc.file_type='regular' AND lc.cloud_only_version_id IS NULL
          AND NOT COALESCE({PROTECTED_SQL},FALSE)
    """, (vault_id,)).fetchall()
    candidates = [dict(row) for row in rows if auto_upload_path_is_included(row["path"], policy)]
    # One durable continuous timer per included pending file; no path or secret
    # persisted in monitoring tables. Existing timers never reset on writes.
    for start in range(0, len(candidates), 500):
        chunk = candidates[start:start + 500]
        connection.execute(
            "INSERT INTO protection_pending(vault_file_id,pending_since) VALUES " +
            ",".join(["(%s,%s)"] * len(chunk)) + " ON CONFLICT(vault_file_id) DO NOTHING",
            [value for row in chunk for value in (row["id"], row["observed_at"])],
        )
    ages = {row["vault_file_id"]: row["pending_since"] for row in connection.execute(
        "SELECT p.* FROM protection_pending p JOIN vault_files vf ON vf.id=p.vault_file_id WHERE vf.vault_id=%s",
        (vault_id,),
    ).fetchall()}
    active = {row["id"] for row in candidates}
    stale = list(ages.keys() - active)
    for start in range(0, len(stale), 500):
        chunk = stale[start:start + 500]
        connection.execute("DELETE FROM protection_pending WHERE vault_file_id IN (" +
                           ",".join(["%s"] * len(chunk)) + ")", chunk)
    for row in candidates:
        row["pending_since"] = ages[row["id"]]
    progress = connection.execute(
        "SELECT MAX(updated_at) AS at FROM jobs WHERE vault_id=%s AND action='upload' AND status='completed'",
        (vault_id,),
    ).fetchone()
    counts = delay_counts(candidates, now=now, last_progress=progress["at"] if progress else None,
                          stability_seconds=policy.stability_seconds)
    inbox.set_problem(connection, key=f"delay:{vault_id}", event="protection_delayed", vault_id=vault_id,
                      active=bool(policy.auto_upload and (counts["stalled"] or counts["overdue"])), params=counts)


def weekly_summary(connection: Any, *, now: datetime) -> dict[str, Any]:
    """Dates are persisted successful evidence; missing dates remain explicit."""
    from ..owner import owner_user_id
    from .component_health import load_vault_health
    from .cost_estimates import estimate_storage_month, list_prices
    from .protection_states import observation_is_unconfirmed
    from .source_layout import vault_local_access
    totals = dict.fromkeys(STATES, 0)
    vaults = []
    for vault in connection.execute("SELECT * FROM vaults WHERE decommission_state<>'decommissioned' ORDER BY id").fetchall():
        health = load_vault_health(connection, vault["id"])
        unknown = (not vault_local_access(vault["source_root"]).local_operations_allowed
                   or observation_is_unconfirmed(health))
        counts = dict.fromkeys(STATES, 0)
        for row in connection.execute(f"""
            SELECT {STATE_SQL} AS state, count(*) AS total FROM vault_files vf
            LEFT JOIN local_copies lc ON lc.vault_file_id=vf.id {LATEST_JOIN_SQL}
            WHERE vf.vault_id=%s AND vf.status='active' AND {VISIBLE_SQL} GROUP BY 1
        """, (vault["id"],)).fetchall():
            counts["unconfirmed" if unknown else row["state"]] += int(row["total"])
        for state in STATES:
            totals[state] += counts[state]
        vaults.append({"vault_id": vault["id"], "states": counts,
                       "suspended": not get_policy(connection, vault["id"]).auto_upload,
                       "last_scan": (health.get("source") or {}).get("last_success_at"),
                       "last_audit": (health.get("audit") or {}).get("last_success_at")})
    verified = connection.execute("SELECT MAX(verified_at) AS at FROM metadata_backup_runs WHERE status='verified'").fetchone()
    freed = connection.execute("SELECT count(*) AS files, COALESCE(sum(total_bytes),0) AS bytes FROM jobs "
                               "WHERE action='free-space' AND status='completed' AND updated_at >= %s",
                               ((now - timedelta(days=7)).isoformat(),)).fetchone()
    cleanup_blocked = connection.execute("SELECT count(*) AS total FROM jobs WHERE action='free-space' AND status='failed' "
                                         "AND updated_at >= %s", ((now - timedelta(days=7)).isoformat(),)).fetchone()
    prices = list_prices()
    cost = sum(estimate_storage_month(prices, size_bytes=int(row["bytes"]), storage_class=row["storage_class"]).estimated_cost_eur
               for row in connection.execute("SELECT storage_class, COALESCE(sum(size),0) AS bytes FROM archive_versions "
                                             "WHERE availability='available' GROUP BY storage_class").fetchall())
    owner = owner_user_id(connection)
    problems = connection.execute("SELECT event, vault_id, body_key, message_params FROM notifications "
                                  "WHERE user_id=%s AND problem_status='open' ORDER BY id", (owner,)).fetchall() if owner else []
    return {"states": totals, "vaults": vaults, "last_verified_backup": verified["at"],
            "open_problems": len(problems), "problems": [dict(row) for row in problems],
            "freed_files": int(freed["files"]), "freed_bytes": int(freed["bytes"]),
            "cleanup_blocked": int(cleanup_blocked["total"]),
            "monthly_cost_estimate": round(cost, 6), "currency": prices.currency,
            "cost_disclaimer": prices.assumptions["disclaimer"]}


def maintain_notifications(connection: Any, *, now: datetime | None = None) -> None:
    from ..owner import owner_user_id
    global _last_maintenance
    automatic = now is None
    tick = time.monotonic()
    if automatic and tick - _last_maintenance < 60:
        return
    now = now or datetime.now(timezone.utc)
    owner = owner_user_id(connection)
    if owner is None:
        return
    # Serialize summary scheduling and episode updates against another worker.
    connection.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (owner,))
    for vault in connection.execute("SELECT * FROM vaults WHERE enabled=TRUE AND decommission_state='active'").fetchall():
        monitor_vault(connection, vault, now=now)
    inbox.reconcile_problem_states(connection)
    latest = connection.execute("SELECT created_at FROM notifications WHERE user_id=%s AND event='weekly_summary' ORDER BY id DESC LIMIT 1",
                                (owner,)).fetchone()
    if latest and now - _time(latest["created_at"], now) < timedelta(days=7):
        if automatic:
            _last_maintenance = tick
        return
    summary = weekly_summary(connection, now=now)
    inbox.enqueue_notification(connection, user_id=owner, event="weekly_summary", title="",
                               title_key="notification.weekly_summary.title", body_key="notification.weekly_summary.body",
                               message_params=summary, channels=inbox.fixed_channels(),
                               dedupe_key=f"summary:{now.date().isoformat()}")
    if automatic:
        _last_maintenance = tick
