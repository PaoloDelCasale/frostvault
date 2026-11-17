"""Automatic Duplicate Cleanup: a per-Vault, opt-in daily run (#726, ADR-0048).

Off by default. Once the Owner enables it (with a fresh authentication), each
active, unpaused Vault runs it once a day: the Cloud Duplicates whose Local
Copy was last seen at least ``grace_days`` ago, and that no other work already
covers, become one Delayed Cloud Deletion marked automatic. The usual seven-day
cancellable delay and the execution re-checks follow; at execution a member
that is no longer a Cloud Duplicate is skipped alone.

A run whose candidates exceed the Vault's cap (files or bytes) schedules
nothing and opens a Vault Problem asking the Owner to review them through the
manual «Delete from cloud» flow with «Only Cloud Duplicates». The Problem closes at
the next run under the cap, or when the cleanup is turned off.

``duplicate_cleanup_last_run_at`` is persisted with the run, so a restart
never runs a Vault again early.

The status also counts, live, the Cloud Duplicates there are now, those still
in their wait and those never seen locally (which the cleanup never deletes),
and reads the totals already deleted automatically, kept on the Vault so they
outlive the history retention of their Jobs (#729).

Deleting a version within its Minimum Storage Duration saves nothing: the
status says how many Cloud Duplicates are still within it and what deleting
them still bills (#731). With ``beyond_minimum`` (off by default) a run
schedules only the duplicates whose minimum has ended by the time they would
be deleted; the others stay held, at no extra cost, until it ends.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.types.json import Jsonb

from ..i18n import DEFAULT_LOCALE, translate
from . import cloud_deletion, minimum_storage
from .audit_events import record_audit_event
from .lifecycle_policies import load_vault_plan

POLL_SECONDS = 300.0
RUN_INTERVAL = timedelta(days=1)
GRACE_DAYS_RANGE = (1, 90)
MAX_CAP_FILES = 1_000_000
MAX_CAP_BYTES = 1024 ** 5  # 1 PiB
PROBLEM_EVENT = "duplicate_cleanup_capped"
NOTICE_EVENT = "duplicate_cleanup_scheduled"
OUTCOMES = ("scheduled", "none", "capped", "skipped")

Database = Callable[[], AbstractContextManager[Any]]


class DuplicateCleanupError(ValueError):
    """The settings cannot be changed: ``code`` names the Owner-facing message."""

    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


def problem_key(vault_id: int) -> str:
    return f"duplicate_cleanup:{vault_id}"


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value else None)


def _settings(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(row["duplicate_cleanup_enabled"]),
        "grace_days": int(row["duplicate_cleanup_grace_days"]),
        "cap_files": int(row["duplicate_cleanup_cap_files"]),
        "cap_bytes": int(row["duplicate_cleanup_cap_bytes"]),
        "beyond_minimum": bool(row["duplicate_cleanup_beyond_minimum"]),
    }


def _amount(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {"count": len(rows), "bytes": sum(int(r["size"] or 0) for r in rows)}


def _earliest_run(next_run: datetime, at: datetime) -> datetime:
    """The first daily run at or after ``at``, counting from ``next_run``."""
    if next_run >= at:
        return next_run
    days = -(-(at - next_run) // RUN_INTERVAL)  # ceiling division
    return next_run + days * RUN_INTERVAL


def _duplicates(connection: Any, row: dict[str, Any], *, now: datetime,
                next_run: datetime | None) -> dict[str, Any]:
    """The Cloud Duplicates there are now, live, as the manual «Only Cloud
    Duplicates» preview of the whole Vault counts them (#729).

    ``current``: all of them; ``waiting``: those whose Local Copy was seen
    within the last ``grace_days``, with when the first becomes eligible and
    the earliest it can be deleted (the first daily run from then, plus the
    seven-day delay; None while the cleanup is off); ``ready``: those the next
    run would take; ``unseen``: those whose Local Copy was never seen, which
    the cleanup never deletes. All None when nothing can be proven now (a
    stopped Vault, an unavailable or replaced root, unconfirmed observations).

    ``minimum_storage``: the current duplicates still within their Minimum
    Storage Duration at the earliest deletion, and what deleting them then
    still bills (#731). ``held``: with ``beyond_minimum`` on, those seen long
    enough ago that the next run keeps for that reason (None when off).
    ``retention_lock``: those the next run would take that a Retention Lock
    still holds at the earliest deletion, and until when (#736): they are
    deleted from that date, never earlier.
    """
    unknown = {"current": None, "waiting": None, "ready": None, "unseen": None,
               "held": None, "minimum_storage": None, "retention_lock": None}
    if row["lifecycle"] != "active" or row.get("root_released_at"):
        return unknown
    try:
        rows, _ = cloud_deletion.cloud_duplicates(connection, row)
    except cloud_deletion.CloudDeletionError:
        return unknown
    grace = timedelta(days=int(row["duplicate_cleanup_grace_days"]))
    sighting = cloud_deletion.split_by_sighting(rows, now - grace)
    plan = load_vault_plan(connection, int(row["id"]))
    executes = now + timedelta(seconds=cloud_deletion.DELETION_DELAY_SECONDS)
    ready, held = sighting.ready, None
    if row["duplicate_cleanup_beyond_minimum"]:
        beyond: list[dict[str, Any]] = []
        kept: list[dict[str, Any]] = []
        for item in ready:
            (beyond if minimum_storage.beyond_minimum(item, plan=plan, at=executes, now=now)
             else kept).append(item)
        held = _amount(kept)
        ready = beyond
    waiting = sighting.waiting
    first = min((r["local_seen_at"] for r in waiting), default=None)
    first_eligible = first + grace if first is not None else None
    earliest = None
    if first_eligible is not None and next_run is not None:
        earliest = _earliest_run(next_run, first_eligible) + timedelta(
            seconds=cloud_deletion.DELETION_DELAY_SECONDS)
    return {"current": _amount(rows), "ready": _amount(ready),
            "waiting": {**_amount(waiting), "first_eligible_at": _iso(first_eligible),
                        "earliest_deletion_at": _iso(earliest)},
            "unseen": _amount(sighting.unseen), "held": held,
            "minimum_storage": minimum_storage.deletion_summary(
                rows, plan=plan, at=executes, now=now),
            "retention_lock": cloud_deletion.retention_lock_summary(row, ready, now=now)}


def _totals(row: dict[str, Any]) -> dict[str, Any] | None:
    """What automatic deletions removed so far, as recorded on the Vault when
    each member was deleted; None until a first one is recorded (#729)."""
    if row["duplicate_cleanup_deleted_count"] is None:
        return None
    return {"deleted_count": int(row["duplicate_cleanup_deleted_count"]),
            "deleted_bytes": int(row["duplicate_cleanup_deleted_bytes"]),
            "last_deleted_at": _iso(row["duplicate_cleanup_last_deleted_at"])}


def status(connection: Any, vault_id: int, *, now: datetime | None = None) -> dict[str, Any]:
    """The Vault's settings, its last run, its pending automatic deletions and
    its Cloud Duplicates now, waiting, never seen and deleted so far (#729)."""
    from .vaults import VAULT_ROW_SQL

    # With its Vault Target: the duplicates are listed under its prefix (#735).
    row = connection.execute(f"{VAULT_ROW_SQL} WHERE v.id=%s", (vault_id,)).fetchone()
    if row is None:
        raise LookupError("Vault not found")
    moment = now or datetime.now(timezone.utc).replace(microsecond=0)
    pending = connection.execute(
        """SELECT j.group_id, count(*) AS file_count, COALESCE(sum(j.total_bytes), 0) AS byte_count,
                  min(j.run_after) AS run_after, bool_and(j.status = 'pending_delay') AS cancellable
           FROM jobs j JOIN cloud_purge_requests r ON r.group_id = j.group_id
           WHERE j.vault_id=%s AND r.automatic AND NOT r.mirror_cleanup
             AND j.action='cloud-purge'
             AND j.status NOT IN ('completed','failed','cancelled')
           GROUP BY j.group_id ORDER BY min(j.run_after), j.group_id""",
        (vault_id,)).fetchall()
    last = row["duplicate_cleanup_last_run_at"]
    enabled = bool(row["duplicate_cleanup_enabled"])
    next_run = last + RUN_INTERVAL if last and enabled else None
    # Never run yet: the first run is due at the next poll.
    upcoming = (next_run or moment) if enabled else None
    return {
        **_settings(row),
        "grace_days_min": GRACE_DAYS_RANGE[0],
        "grace_days_max": GRACE_DAYS_RANGE[1],
        "last_run_at": _iso(last),
        "next_run_at": _iso(next_run),
        "last_outcome": row["duplicate_cleanup_last_outcome"],
        **_duplicates(connection, row, now=moment, next_run=upcoming),
        "totals": _totals(row),
        "pending": [{"group_id": str(p["group_id"]), "file_count": int(p["file_count"]),
                     "byte_count": int(p["byte_count"]), "run_after": _iso(p["run_after"]),
                     "cancellable": bool(p["cancellable"])} for p in pending],
    }


def update_settings(connection: Any, *, vault_id: int, actor: str,
                    values: dict[str, Any]) -> dict[str, Any]:
    """Change the Vault's settings; turning the cleanup off closes its Problem.

    The caller requires a fresh authentication (the HTTP layer).
    """
    vault = cloud_deletion.lock_vault(connection, vault_id)
    if vault["lifecycle"] in {"stopping", "tombstone"}:
        raise DuplicateCleanupError("Folder protection has stopped", "stopped")
    current = _settings(vault)
    unknown = set(values) - set(current)
    if unknown:
        raise DuplicateCleanupError(f"Unknown settings: {sorted(unknown)}")
    merged = {**current, **{k: v for k, v in values.items() if v is not None}}
    if not isinstance(merged["enabled"], bool):
        raise DuplicateCleanupError("enabled must be true or false")
    low, high = GRACE_DAYS_RANGE
    for name, minimum, maximum in (("grace_days", low, high), ("cap_files", 1, MAX_CAP_FILES),
                                   ("cap_bytes", 1, MAX_CAP_BYTES)):
        value = merged[name]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise DuplicateCleanupError(f"{name} must be between {minimum} and {maximum}", name)
    if not isinstance(merged["beyond_minimum"], bool):
        raise DuplicateCleanupError("beyond_minimum must be true or false")
    connection.execute(
        """UPDATE vaults SET duplicate_cleanup_enabled=%s, duplicate_cleanup_grace_days=%s,
               duplicate_cleanup_cap_files=%s, duplicate_cleanup_cap_bytes=%s,
               duplicate_cleanup_beyond_minimum=%s
           WHERE id=%s""",
        (merged["enabled"], merged["grace_days"], merged["cap_files"], merged["cap_bytes"],
         merged["beyond_minimum"], vault_id))
    if not merged["enabled"]:
        _set_capped_problem(connection, vault_id, active=False)
    if merged != current:
        record_audit_event(connection, event="duplicate_cleanup.settings_updated", actor=actor,
                           vault_id=vault_id, outcome="updated", before=current, after=merged)
    return status(connection, vault_id)


def _set_capped_problem(connection: Any, vault_id: int, *, active: bool,
                        params: dict[str, Any] | None = None) -> None:
    from .notifications import set_problem
    set_problem(connection, key=problem_key(vault_id), event=PROBLEM_EVENT, vault_id=vault_id,
                active=active, params=params, email=True)


def due_vaults(connection: Any, *, now: datetime) -> list[int]:
    """Active, unpaused Vaults with the cleanup on and no run in the last day."""
    rows = connection.execute(
        """SELECT id FROM vaults
           WHERE duplicate_cleanup_enabled AND lifecycle='active' AND NOT paused
             AND root_released_at IS NULL
             AND (duplicate_cleanup_last_run_at IS NULL
                  OR duplicate_cleanup_last_run_at <= %s::timestamptz)
           ORDER BY id""",
        (now - RUN_INTERVAL,)).fetchall()
    return [int(r["id"]) for r in rows]


def _finish(connection: Any, vault_id: int, now: datetime, outcome: dict[str, Any]) -> dict[str, Any]:
    outcome = {**outcome, "at": now.isoformat()}
    connection.execute(
        """UPDATE vaults SET duplicate_cleanup_last_run_at=%s, duplicate_cleanup_last_outcome=%s
           WHERE id=%s""", (now, Jsonb(outcome), vault_id))
    return outcome


def run_vault(connection: Any, vault_id: int, *, now: datetime) -> dict[str, Any] | None:
    """One daily run of one Vault, in the caller's transaction.

    Returns the recorded outcome, or None when the Vault is not due (turned
    off, paused, no longer active, or already run within the last day).
    """
    vault = cloud_deletion.lock_vault(connection, vault_id)
    last = vault["duplicate_cleanup_last_run_at"]
    if (not vault["duplicate_cleanup_enabled"] or vault["lifecycle"] != "active"
            or vault["paused"] or vault.get("root_released_at")
            or (last is not None and last > now - RUN_INTERVAL)):
        return None
    settings = _settings(vault)
    seen_before = (now - timedelta(days=settings["grace_days"])).isoformat()
    try:
        preview, sighting = cloud_deletion.automatic_duplicates(
            connection, vault, seen_before=seen_before,
            beyond_minimum=settings["beyond_minimum"], now=now)
    except cloud_deletion.CloudDeletionError as exc:
        # Nothing can be proven today (an unreadable root, unconfirmed
        # observations): schedule nothing and try again tomorrow.
        record_audit_event(connection, event="duplicate_cleanup.skipped", actor="system",
                           vault_id=vault_id, outcome="skipped", reason=str(exc)[:200])
        return _finish(connection, vault_id, now, {"outcome": "skipped"})
    files, count, size = sighting.ready, preview.object_count, preview.byte_count
    # Every outcome also says how many duplicates are still in their wait, and
    # how many were never seen locally, which the cleanup never deletes (#729).
    waits = {"waiting_count": len(sighting.waiting),
             "waiting_bytes": sum(int(r["size"] or 0) for r in sighting.waiting),
             "unseen_count": len(sighting.unseen)}
    if settings["beyond_minimum"]:
        # Held until their Minimum Storage Duration ends (#731).
        waits.update(held_count=len(sighting.held),
                     held_bytes=sum(int(r["size"] or 0) for r in sighting.held))
    if not files:
        _set_capped_problem(connection, vault_id, active=False)
        return _finish(connection, vault_id, now,
                       {"outcome": "none", "file_count": 0, "byte_count": 0, **waits})
    if count > settings["cap_files"] or size > settings["cap_bytes"]:
        params = {"count": count, "byte_count": size, "cap_files": settings["cap_files"],
                  "cap_bytes": settings["cap_bytes"], "duplicate_cleanup": True}
        _set_capped_problem(connection, vault_id, active=True, params=params)
        record_audit_event(connection, event="duplicate_cleanup.capped", actor="system",
                           vault_id=vault_id, outcome="capped", file_count=count, byte_count=size,
                           cap_files=settings["cap_files"], cap_bytes=settings["cap_bytes"])
        return _finish(connection, vault_id, now,
                       {"outcome": "capped", "file_count": count, "byte_count": size, **waits})
    _set_capped_problem(connection, vault_id, active=False)
    minimum = preview.minimum_storage or {}
    if minimum.get("count"):
        # What the scheduled duplicates still bill for their minimum (#731).
        waits.update(minimum_count=int(minimum["count"]), minimum_bytes=int(minimum["bytes"]),
                     minimum_cost=float(minimum["remaining_cost"]),
                     savings_from=minimum.get("savings_from"))
    scheduled = cloud_deletion.schedule_automatic_duplicates(
        connection, vault=vault, files=files, preview=preview, requested_at=now.isoformat(),
        reason=translate("duplicate_cleanup.reason", locale=DEFAULT_LOCALE))
    from .notifications import enqueue_notification
    enqueue_notification(
        connection, vault_id=vault_id, event=NOTICE_EVENT, title="",
        title_key=f"notification.{NOTICE_EVENT}.title", body_key=f"notification.{NOTICE_EVENT}.body",
        message_params={"count": count, "byte_count": size, "executes_at": scheduled.pending_until,
                        "group_id": scheduled.group_id, "duplicate_cleanup": True},
        dedupe_key=f"{NOTICE_EVENT}:{scheduled.group_id}")
    return _finish(connection, vault_id, now, {"outcome": "scheduled", "file_count": count,
                                               "byte_count": size, "group_id": scheduled.group_id,
                                               "run_after": scheduled.pending_until, **waits})


def run_due(database: Database, *, now: datetime | None = None) -> int:
    """Run every due Vault, each in its own transaction; returns how many ran.

    A failing Vault does not hold back the others; the first error is raised
    after all of them were tried, and that Vault is tried again at the next poll.
    """
    moment = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    with database() as connection:
        vault_ids = due_vaults(connection, now=moment)
    ran = 0
    failure: Exception | None = None
    for vault_id in vault_ids:
        try:
            with database() as connection:
                ran += run_vault(connection, vault_id, now=moment) is not None
        except Exception as exc:  # noqa: BLE001 - reported once below
            failure = failure or exc
    if failure is not None:
        raise failure
    return ran
