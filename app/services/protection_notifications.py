"""Read-only protection monitoring and weekly Owner summaries (#425).

Only notification state and pending timers are written. No admission, transfer,
verification, recovery or deletion is authorized here. Timers survive restart;
content changing repeatedly does not restart the continuous unprotected age.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from psycopg.rows import tuple_row

from . import notifications as inbox
from .operation_policies import (
    AutoUploadPathMatcher, file_is_stable, get_policy, stopped_upload_sql,
    unconfirmed_scope,
)
from .protection_states import (
    CURRENT_JOIN_SQL, FAILED_UPLOAD_SQL, PROTECTED_SQL, STATES, VISIBLE_SQL, masked_state_sql,
    state_sql,
)

_last_maintenance = 0.0


def _time(value: str | datetime | None, fallback: datetime) -> datetime:
    if not value:
        return fallback
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
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


@dataclass(frozen=True)
class VaultObservation:
    """What one Vault's monitoring read, waiting for its short Problem writes."""

    vault_id: int
    local_operations_allowed: bool
    auto_upload: bool
    counts: dict[str, int]


def observe_vault(connection: Any, vault: Mapping[str, Any], *, now: datetime) -> VaultObservation:
    """Read and classify one Vault's pending files and keep their timers.

    Needs no Owner lock (#767): only pending timers are written here, each
    guarded on its own row (see ``record_pending_files``). The Problems it
    implies are written by :func:`publish_vault_problems`.
    """
    from .source_layout import vault_local_access
    vault_id = int(vault["id"])
    policy = get_policy(connection, vault_id)
    access = vault_local_access(str(vault["source_root"]))
    rows = connection.execute(f"""
        SELECT f.id::text, f.path, f.local_mtime_ns AS mtime_ns,
               f.local_content_observed_at AS content_observed_at,
               COALESCE(f.local_content_observed_at, f.local_observed_at, f.created_at) AS observed_at
        FROM files f
        {CURRENT_JOIN_SQL}
        WHERE f.vault_id=%s AND f.local_presence='present'
          AND f.held_version_id IS NULL
          AND NOT COALESCE({PROTECTED_SQL},FALSE)
          -- A Stopped Upload waits for the Owner, not for the queue (#630);
          -- a failed upload still counts, as before.
          AND (({FAILED_UPLOAD_SQL}) OR NOT {stopped_upload_sql("f")})
    """, (vault_id,))
    # Plain tuples (id, path, mtime_ns, content_observed_at, observed_at): an
    # adoption can have 100k pending files (#767).
    rows.row_factory = tuple_row
    # The globs are compiled once per cycle, not per file (#767).
    included = AutoUploadPathMatcher(policy)
    candidates = {row[0]: row for row in rows.fetchall() if included(row[1])}
    # One durable continuous timer per included pending file, on its files row.
    # Existing timers never reset on writes; files no longer pending lose theirs.
    from .local_copies import LocalCopies
    ages = LocalCopies(connection).record_pending_files(
        vault_id, {file_id: row[4] for file_id, row in candidates.items()},
    )
    # A candidate protected since it was read has no timer: it is not pending.
    pending = [{"mtime_ns": candidates[file_id][2], "content_observed_at": candidates[file_id][3],
                "pending_since": since}
               for file_id, since in ages.items() if file_id in candidates]
    progress = connection.execute(
        "SELECT MAX(updated_at) AS at FROM jobs WHERE vault_id=%s AND action='upload' AND status='completed'",
        (vault_id,),
    ).fetchone()
    counts = delay_counts(pending, now=now, last_progress=progress["at"] if progress else None,
                          stability_seconds=policy.stability_seconds)
    return VaultObservation(vault_id=vault_id, local_operations_allowed=access.local_operations_allowed,
                            auto_upload=policy.auto_upload, counts=counts)


def publish_vault_problems(connection: Any, observation: VaultObservation) -> None:
    """The short Problem writes of one observation; each takes the Owner lock."""
    vault_id = observation.vault_id
    # Manual Upload is the Owner's choice, not a failure (#647): it never opens
    # a Problem, and one opened by an earlier release is resolved here.
    inbox.set_problem(connection, key=f"blocked:{vault_id}:suspended", event="protection_blocked",
                      vault_id=vault_id, active=False)
    # Share the source episode with failed scans, without allowing a mount-only
    # check to clear a failed full source scan. Its next success owns resolution.
    # Read here, under the Owner lock set_problem took: a scan failure that
    # commits its health with its Problem is never cleared by an older read.
    source_health = connection.execute(
        "SELECT last_error FROM vault_component_health WHERE vault_id=%s AND component='source'",
        (vault_id,),
    ).fetchone()
    allowed = observation.local_operations_allowed
    if not allowed or not (source_health and source_health['last_error']):
        inbox.set_problem(connection, key=f"blocked:{vault_id}:source", event="protection_blocked",
                          vault_id=vault_id, active=not allowed,
                          params={"component": "source",
                                  "cause_key": "notification.protection_blocked.cause.volume_unavailable"})
    counts = observation.counts
    inbox.set_problem(connection, key=f"delay:{vault_id}", event="protection_delayed", vault_id=vault_id,
                      active=bool(observation.auto_upload and (counts["stalled"] or counts["overdue"])),
                      params=counts)


def weekly_summary(connection: Any, *, now: datetime) -> dict[str, Any]:
    """Dates are persisted successful evidence; missing dates remain explicit."""
    from .component_health import load_vault_health
    from .cost_estimates import estimate_storage_month, list_prices
    from .source_layout import vault_local_access
    totals = dict.fromkeys(STATES, 0)
    vaults = []
    for vault in connection.execute(
            # A provisioning Vault has no bucket and no files yet (#735).
            "SELECT * FROM vaults WHERE lifecycle NOT IN ('tombstone', 'provisioning') ORDER BY id").fetchall():
        health = load_vault_health(connection, vault["id"])
        # The same per-path rule as browse, stats and the release gate (#684).
        scope = unconfirmed_scope(
            connection, int(vault["id"]), lifecycle=vault["lifecycle"],
            local_operations_allowed=vault_local_access(vault["source_root"]).local_operations_allowed,
        )
        blocked, blocked_params = scope.sql("f.path")
        counts = dict.fromkeys(STATES, 0)
        state_expression = state_sql(get_policy(connection, vault["id"]))
        for row in connection.execute(f"""
            SELECT {masked_state_sql(blocked, state_expression)} AS state,
                   count(*) AS total FROM files f
            {CURRENT_JOIN_SQL}
            WHERE f.vault_id=%s AND {VISIBLE_SQL} GROUP BY 1
        """, [*blocked_params, vault["id"]]).fetchall():
            counts[row["state"]] += int(row["total"])
        for state in STATES:
            totals[state] += counts[state]
        vaults.append({"vault_id": vault["id"], "states": counts,
                       "manual_upload": not get_policy(connection, vault["id"]).auto_upload,
                       "last_scan": (health.get("source") or {}).get("last_success_at"),
                       "last_audit": (health.get("audit") or {}).get("last_success_at")})
    from .metadata_backups import verification_overview
    verification = verification_overview(connection, now=now)
    freed = connection.execute("SELECT count(*) AS files, COALESCE(sum(total_bytes),0) AS bytes FROM jobs "
                               "WHERE action='free-space' AND status='completed' AND updated_at >= %s",
                               ((now - timedelta(days=7)).isoformat(),)).fetchone()
    cleanup_blocked = connection.execute("SELECT count(*) AS total FROM jobs WHERE action='free-space' AND status='failed' "
                                         "AND updated_at >= %s", ((now - timedelta(days=7)).isoformat(),)).fetchone()
    prices = list_prices()
    cost = sum(estimate_storage_month(prices, size_bytes=int(row["bytes"]), storage_class=row["storage_class"]).estimated_cost_eur
               for row in connection.execute("SELECT storage_class, COALESCE(sum(size),0) AS bytes FROM archive_versions "
                                             "WHERE availability='available' GROUP BY storage_class").fetchall())
    problems = connection.execute("SELECT event, vault_id, body_key, message_params FROM notifications "
                                  "WHERE problem_status='open' ORDER BY id").fetchall()
    return {"states": totals, "vaults": vaults, "last_verified_backup": (verification["last_verified"] or {}).get("verified_at"),
            "metadata_backup_verification": verification,
            "open_problems": len(problems), "problems": [dict(row) for row in problems],
            "freed_files": int(freed["files"]), "freed_bytes": int(freed["bytes"]),
            "cleanup_blocked": int(cleanup_blocked["total"]),
            "monthly_cost_estimate": round(cost, 6), "currency": prices.currency,
            "cost_disclaimer": prices.assumptions["disclaimer"]}


# Session advisory lock key ("MONITOR!") serializing whole monitoring cycles.
_MONITOR_LOCK_KEY = 0x4D4F4E49544F5221


def maintain_notifications(connection: Any, *, now: datetime | None = None) -> None:
    """One monitoring cycle, in short transactions committed on ``connection``.

    The Owner row lock serializes Problem episodes with every writer of the
    inbox (uploads, scans, Jobs). It is held only for the short Problem writes,
    never while pending files are read and classified, which for a large
    adoption takes seconds (#767): an upload that publishes a Problem would
    otherwise wait for the whole cycle, holding its Vault row meanwhile.

    Two workers running this cycle are serialized by a session advisory lock
    instead, held across its transactions: timers, Problem updates and the
    weekly summary of one cycle are never interleaved with another's, as the
    Owner lock held for the whole cycle used to guarantee. Nothing else takes
    that lock, so no upload or scan ever waits for it. Lock order stays
    advisory, then Owner (the uploads' order is Vault, then Owner): the cycle
    takes no Vault row lock and holds no files row lock while it waits for the
    Owner, since its timer writes are committed first. ``DISCARD ALL`` on a
    returned pooled connection, or the end of a direct one, releases the
    advisory lock even if the unlock below cannot run.
    """
    global _last_maintenance
    automatic = now is None
    tick = time.monotonic()
    if automatic and tick - _last_maintenance < 60:
        return
    now = now or datetime.now(timezone.utc)
    connection.execute("SELECT pg_advisory_lock(%s)", (_MONITOR_LOCK_KEY,))
    try:
        # 1. Reads, classification and timers, without the Owner lock.
        observations = [
            observe_vault(connection, vault, now=now)
            for vault in connection.execute(
                "SELECT * FROM vaults WHERE lifecycle='active' ORDER BY id").fetchall()
        ]
        connection.commit()
        # 2. Problem episodes, the only writes needing the Owner lock. A Vault
        # stopped since its read is closed by set_problem itself (#715); one
        # deleted meanwhile has no Problems left to write.
        inbox.lock_owner_inbox(connection)
        for observation in observations:
            if connection.execute("SELECT 1 FROM vaults WHERE id=%s",
                                  (observation.vault_id,)).fetchone():
                publish_vault_problems(connection, observation)
        connection.commit()
        inbox.reconcile_problem_states(connection)
        connection.commit()
        # 3. The weekly summary is read without the Owner lock and enqueued
        # under it, after checking again that no summary was enqueued meanwhile.
        if not _summary_due(connection, now):
            if automatic:
                _last_maintenance = tick
            return
        summary = weekly_summary(connection, now=now)
        connection.commit()
        inbox.lock_owner_inbox(connection)
        if _summary_due(connection, now):
            inbox.enqueue_notification(connection, event="weekly_summary", title="",
                                       title_key="notification.weekly_summary.title",
                                       body_key="notification.weekly_summary.body",
                                       message_params=summary, channels=inbox.fixed_channels(),
                                       dedupe_key=f"summary:{now.date().isoformat()}")
        connection.commit()
        if automatic:
            _last_maintenance = tick
    except BaseException:
        connection.rollback()
        raise
    finally:
        try:
            connection.execute("SELECT pg_advisory_unlock(%s)", (_MONITOR_LOCK_KEY,))
            connection.commit()
        except Exception:
            pass


def _summary_due(connection: Any, now: datetime) -> bool:
    latest = connection.execute("SELECT created_at FROM notifications WHERE event='weekly_summary' "
                                "ORDER BY id DESC LIMIT 1").fetchone()
    return not (latest and now - _time(latest["created_at"], now) < timedelta(days=7))
