"""Mirror Cleanup: delete from the cloud what left the server (#771, ADR-0052).

Off by default, per Vault, apart from Automatic Duplicate Cleanup. Once the
Owner enables it (with a fresh authentication), each active, unpaused Vault
runs it once a day: the Cloud-only Vault Files whose Local Copy was last seen
at least ``grace_days`` ago (1-180), optionally only under the chosen
``folders``, become one automatic Delayed Cloud Deletion through the same path
as the Owner's: the seven-day cancellable delay, the execution re-checks, a
notification and an audit event. Unlike the Duplicate Cleanup these files can
be the only copy of their content: the cloud then mirrors the server, which
suits copies that are already backups (rotating archives), never original
data.

A file never seen locally never qualifies; a file already in pending work (a
deletion of the Owner or of either cleanup) is skipped, so the two cleanups
never schedule the same file twice. Cloud Duplicates are Cloud-only too and
qualify like any other file.

A run whose candidates exceed the cap (files or bytes) schedules nothing and
opens a Vault Problem pointing to the manual «Delete from cloud» flow; it
closes at the next run under the cap or when the cleanup is turned off. With
``beyond_minimum`` a run schedules only what its Minimum Storage Duration no
longer bills by the earliest execution (#731).

Loosening the settings (turning it on, raising a cap, shortening the wait,
widening the folders, no longer waiting for the minimum) needs a fresh
authentication; tightening them or turning it off never does, since that only
deletes less.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any

from psycopg.types.json import Jsonb

from ..i18n import DEFAULT_LOCALE, translate
from . import cloud_deletion, minimum_storage
from .audit_events import record_audit_event
from .duplicate_cleanup import RUN_INTERVAL, _amount, _earliest_run, _iso
from .lifecycle_policies import load_vault_plan
from .vault_moves import MINIMUM_DAYS

POLL_SECONDS = 300.0
GRACE_DAYS_RANGE = (1, 180)
MAX_CAP_FILES = 1_000_000
MAX_CAP_BYTES = 1024 ** 5  # 1 PiB
MAX_FOLDERS = 100
MAX_FOLDER_LENGTH = 1024
PROBLEM_EVENT = "mirror_cleanup_capped"
NOTICE_EVENT = "mirror_cleanup_scheduled"
OUTCOMES = ("scheduled", "none", "capped", "skipped")
DELETION_DELAY_DAYS = cloud_deletion.DELETION_DELAY_SECONDS // 86400
_GLOB = frozenset("*?[]{}")

Database = Callable[[], AbstractContextManager[Any]]


class MirrorCleanupError(ValueError):
    """The settings cannot be changed: ``code`` names the Owner-facing message."""

    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


def problem_key(vault_id: int) -> str:
    return f"mirror_cleanup:{vault_id}"


def normalize_folders(value: Any) -> list[str]:
    """The folders of the scope, relative to the Vault root, normalized.

    Leading, trailing and repeated slashes and ``.`` parts are dropped; ``..``,
    absolute paths, globs and control characters are refused. A folder inside
    another listed one adds nothing and is dropped; the result is sorted.
    An empty list is the whole Vault.
    """
    if not isinstance(value, (list, tuple)):
        raise MirrorCleanupError("folders must be a list", "folders")
    folders: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise MirrorCleanupError("Each folder must be text", "folders")
        text = item.strip()
        if (not text or len(text) > MAX_FOLDER_LENGTH or text.startswith("/") or "\\" in text
                or any(ch in _GLOB for ch in text)
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in text)):
            raise MirrorCleanupError(f"Invalid folder: {item!r}", "folders")
        parts = [part for part in text.split("/") if part not in {"", "."}]
        if not parts or ".." in parts:
            raise MirrorCleanupError(f"Invalid folder: {item!r}", "folders")
        folders.add(str(PurePosixPath(*parts)))
    kept = [f for f in sorted(folders)
            if not any(f.startswith(other + "/") for other in folders if other != f)]
    if len(kept) > MAX_FOLDERS:
        raise MirrorCleanupError(f"At most {MAX_FOLDERS} folders", "folders")
    return kept


def _covers(broad: list[str], narrow: list[str]) -> bool:
    """True when every file under ``narrow`` is also under ``broad``."""
    if not broad:
        return True
    if not narrow:
        return False
    return all(any(f == b or f.startswith(b + "/") for b in broad) for f in narrow)


def _settings(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(row["mirror_cleanup_enabled"]),
        "grace_days": int(row["mirror_cleanup_grace_days"]),
        "cap_files": int(row["mirror_cleanup_cap_files"]),
        "cap_bytes": int(row["mirror_cleanup_cap_bytes"]),
        "folders": list(row["mirror_cleanup_folders"] or []),
        "beyond_minimum": bool(row["mirror_cleanup_beyond_minimum"]),
    }


def loosens(current: dict[str, Any], merged: dict[str, Any]) -> bool:
    """True when ``merged`` could delete more than ``current``: it then needs a
    fresh authentication (#771). Turning it off, a longer wait, lower caps,
    fewer folders or waiting for the minimum only delete less."""
    return ((merged["enabled"] and not current["enabled"])
            or merged["grace_days"] < current["grace_days"]
            or merged["cap_files"] > current["cap_files"]
            or merged["cap_bytes"] > current["cap_bytes"]
            or not _covers(current["folders"], merged["folders"])
            or (current["beyond_minimum"] and not merged["beyond_minimum"]))


def cloud_life(row: dict[str, Any], plan: Any) -> dict[str, Any]:
    """How long a file stays in the cloud after it leaves the server (#771).

    The local stay is unknown, so the life is counted from the disappearance:
    the wait plus the seven-day delay (and up to a day until the daily run).
    The object is at least that old when deleted; the class the Storage Plan
    has it in at that age and the minimum that class bills are returned with
    whether the life already passes it (each further day then costs again) or
    not (deleting earlier would save nothing).
    """
    days = int(row["mirror_cleanup_grace_days"]) + DELETION_DELAY_DAYS
    storage_class, entered = "STANDARD", 0
    for step in plan.transitions:
        if step.days <= days:
            storage_class, entered = step.storage_class, step.days
    minimum = MINIMUM_DAYS.get(storage_class, 0)
    until = entered + minimum if minimum else None
    return {"days_after_disappearance": days, "storage_class": storage_class,
            "class_from_day": entered, "minimum_days": minimum, "minimum_until_day": until,
            "beyond_minimum": until is not None and days > until}


def _candidates(connection: Any, row: dict[str, Any], *, now: datetime,
                next_run: datetime | None) -> dict[str, Any]:
    """The Cloud-only files under the scope now, live, split as a run would.

    ``current``: all of them; ``waiting``: seen within the last ``grace_days``,
    with when the first becomes eligible and the earliest it can be deleted;
    ``ready``: those the next run would take; ``unseen``: never seen locally,
    never deleted by the cleanup; ``held``: with ``beyond_minimum``, those kept
    for their Minimum Storage Duration. All None when nothing can be proven.
    """
    unknown = {"current": None, "waiting": None, "ready": None, "unseen": None,
               "held": None, "minimum_storage": None, "retention_lock": None}
    if row["lifecycle"] != "active" or row.get("root_released_at"):
        return unknown
    try:
        rows, _ = cloud_deletion.cloud_only_files(
            connection, row, folders=list(row["mirror_cleanup_folders"] or []))
    except cloud_deletion.CloudDeletionError:
        return unknown
    grace = timedelta(days=int(row["mirror_cleanup_grace_days"]))
    sighting = cloud_deletion.split_by_sighting(rows, now - grace)
    plan = load_vault_plan(connection, int(row["id"]))
    executes = now + timedelta(seconds=cloud_deletion.DELETION_DELAY_SECONDS)
    ready, held = sighting.ready, None
    if row["mirror_cleanup_beyond_minimum"]:
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
    if row["mirror_cleanup_deleted_count"] is None:
        return None
    return {"deleted_count": int(row["mirror_cleanup_deleted_count"]),
            "deleted_bytes": int(row["mirror_cleanup_deleted_bytes"]),
            "last_deleted_at": _iso(row["mirror_cleanup_last_deleted_at"])}


def status(connection: Any, vault_id: int, *, now: datetime | None = None) -> dict[str, Any]:
    """The Vault's settings, its last run, its pending deletions, its
    candidates now and the totals deleted so far."""
    from .vaults import VAULT_ROW_SQL

    row = connection.execute(f"{VAULT_ROW_SQL} WHERE v.id=%s", (vault_id,)).fetchone()
    if row is None:
        raise LookupError("Vault not found")
    moment = now or datetime.now(timezone.utc).replace(microsecond=0)
    pending = connection.execute(
        """SELECT j.group_id, count(*) AS file_count, COALESCE(sum(j.total_bytes), 0) AS byte_count,
                  min(j.run_after) AS run_after, bool_and(j.status = 'pending_delay') AS cancellable
           FROM jobs j JOIN cloud_purge_requests r ON r.group_id = j.group_id
           WHERE j.vault_id=%s AND r.automatic AND r.mirror_cleanup AND j.action='cloud-purge'
             AND j.status NOT IN ('completed','failed','cancelled')
           GROUP BY j.group_id ORDER BY min(j.run_after), j.group_id""",
        (vault_id,)).fetchall()
    last = row["mirror_cleanup_last_run_at"]
    enabled = bool(row["mirror_cleanup_enabled"])
    next_run = last + RUN_INTERVAL if last and enabled else None
    upcoming = (next_run or moment) if enabled else None
    return {
        **_settings(row),
        "grace_days_min": GRACE_DAYS_RANGE[0],
        "grace_days_max": GRACE_DAYS_RANGE[1],
        "last_run_at": _iso(last),
        "next_run_at": _iso(next_run),
        "last_outcome": row["mirror_cleanup_last_outcome"],
        "cloud_life": cloud_life(row, load_vault_plan(connection, vault_id)),
        **_candidates(connection, row, now=moment, next_run=upcoming),
        "totals": _totals(row),
        "pending": [{"group_id": str(p["group_id"]), "file_count": int(p["file_count"]),
                     "byte_count": int(p["byte_count"]), "run_after": _iso(p["run_after"]),
                     "cancellable": bool(p["cancellable"])} for p in pending],
    }


def update_settings(connection: Any, *, vault_id: int, actor: str, values: dict[str, Any],
                    require_reauth: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Change the Vault's settings; turning the cleanup off closes its Problem.

    ``require_reauth`` is called (and may raise) when the change loosens the
    settings (:func:`loosens`); the HTTP layer passes its fresh-authentication
    check. Without it every change is accepted (the caller already checked).
    """
    vault = cloud_deletion.lock_vault(connection, vault_id)
    if vault["lifecycle"] in {"stopping", "tombstone"}:
        raise MirrorCleanupError("Folder protection has stopped", "stopped")
    current = _settings(vault)
    unknown = set(values) - set(current)
    if unknown:
        raise MirrorCleanupError(f"Unknown settings: {sorted(unknown)}")
    merged = {**current, **{k: v for k, v in values.items() if v is not None}}
    for name in ("enabled", "beyond_minimum"):
        if not isinstance(merged[name], bool):
            raise MirrorCleanupError(f"{name} must be true or false")
    low, high = GRACE_DAYS_RANGE
    for name, minimum, maximum in (("grace_days", low, high), ("cap_files", 1, MAX_CAP_FILES),
                                   ("cap_bytes", 1, MAX_CAP_BYTES)):
        value = merged[name]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise MirrorCleanupError(f"{name} must be between {minimum} and {maximum}", name)
    merged["folders"] = normalize_folders(merged["folders"])
    if require_reauth is not None and loosens(current, merged):
        require_reauth()
    connection.execute(
        """UPDATE vaults SET mirror_cleanup_enabled=%s, mirror_cleanup_grace_days=%s,
               mirror_cleanup_cap_files=%s, mirror_cleanup_cap_bytes=%s,
               mirror_cleanup_folders=%s, mirror_cleanup_beyond_minimum=%s
           WHERE id=%s""",
        (merged["enabled"], merged["grace_days"], merged["cap_files"], merged["cap_bytes"],
         merged["folders"], merged["beyond_minimum"], vault_id))
    if not merged["enabled"]:
        _set_capped_problem(connection, vault_id, active=False)
    if merged != current:
        record_audit_event(connection, event="mirror_cleanup.settings_updated", actor=actor,
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
           WHERE mirror_cleanup_enabled AND lifecycle='active' AND NOT paused
             AND root_released_at IS NULL
             AND (mirror_cleanup_last_run_at IS NULL
                  OR mirror_cleanup_last_run_at <= %s::timestamptz)
           ORDER BY id""",
        (now - RUN_INTERVAL,)).fetchall()
    return [int(r["id"]) for r in rows]


def _finish(connection: Any, vault_id: int, now: datetime, outcome: dict[str, Any]) -> dict[str, Any]:
    outcome = {**outcome, "at": now.isoformat()}
    connection.execute(
        "UPDATE vaults SET mirror_cleanup_last_run_at=%s, mirror_cleanup_last_outcome=%s WHERE id=%s",
        (now, Jsonb(outcome), vault_id))
    return outcome


def _size(rows: list[dict[str, Any]]) -> int:
    return sum(int(r["size"] or 0) for r in rows)


def run_vault(connection: Any, vault_id: int, *, now: datetime) -> dict[str, Any] | None:
    """One daily run of one Vault, in the caller's transaction.

    Returns the recorded outcome, or None when the Vault is not due.
    """
    vault = cloud_deletion.lock_vault(connection, vault_id)
    last = vault["mirror_cleanup_last_run_at"]
    if (not vault["mirror_cleanup_enabled"] or vault["lifecycle"] != "active"
            or vault["paused"] or vault.get("root_released_at")
            or (last is not None and last > now - RUN_INTERVAL)):
        return None
    settings = _settings(vault)
    seen_before = (now - timedelta(days=settings["grace_days"])).isoformat()
    try:
        preview, sighting = cloud_deletion.automatic_mirror(
            connection, vault, seen_before=seen_before, folders=settings["folders"],
            beyond_minimum=settings["beyond_minimum"], now=now)
    except cloud_deletion.CloudDeletionError as exc:
        record_audit_event(connection, event="mirror_cleanup.skipped", actor="system",
                           vault_id=vault_id, outcome="skipped", reason=str(exc)[:200])
        return _finish(connection, vault_id, now, {"outcome": "skipped"})
    files, count, size = sighting.ready, preview.object_count, preview.byte_count
    waits = {"waiting_count": len(sighting.waiting), "waiting_bytes": _size(sighting.waiting),
             "unseen_count": len(sighting.unseen)}
    if settings["beyond_minimum"]:
        waits.update(held_count=len(sighting.held), held_bytes=_size(sighting.held))
    if not files:
        _set_capped_problem(connection, vault_id, active=False)
        return _finish(connection, vault_id, now,
                       {"outcome": "none", "file_count": 0, "byte_count": 0, **waits})
    if count > settings["cap_files"] or size > settings["cap_bytes"]:
        params = {"count": count, "byte_count": size, "cap_files": settings["cap_files"],
                  "cap_bytes": settings["cap_bytes"], "mirror_cleanup": True}
        _set_capped_problem(connection, vault_id, active=True, params=params)
        record_audit_event(connection, event="mirror_cleanup.capped", actor="system",
                           vault_id=vault_id, outcome="capped", file_count=count, byte_count=size,
                           cap_files=settings["cap_files"], cap_bytes=settings["cap_bytes"])
        return _finish(connection, vault_id, now,
                       {"outcome": "capped", "file_count": count, "byte_count": size, **waits})
    _set_capped_problem(connection, vault_id, active=False)
    minimum = preview.minimum_storage or {}
    if minimum.get("count"):
        waits.update(minimum_count=int(minimum["count"]), minimum_bytes=int(minimum["bytes"]),
                     minimum_cost=float(minimum["remaining_cost"]),
                     savings_from=minimum.get("savings_from"))
    scheduled = cloud_deletion.schedule_automatic_duplicates(
        connection, vault=vault, files=files, preview=preview, requested_at=now.isoformat(),
        reason=translate("mirror_cleanup.reason", locale=DEFAULT_LOCALE), mirror=True)
    from .notifications import enqueue_notification
    enqueue_notification(
        connection, vault_id=vault_id, event=NOTICE_EVENT, title="",
        title_key=f"notification.{NOTICE_EVENT}.title", body_key=f"notification.{NOTICE_EVENT}.body",
        message_params={"count": count, "byte_count": size, "executes_at": scheduled.pending_until,
                        "group_id": scheduled.group_id, "mirror_cleanup": True},
        dedupe_key=f"{NOTICE_EVENT}:{scheduled.group_id}")
    return _finish(connection, vault_id, now, {"outcome": "scheduled", "file_count": count,
                                               "byte_count": size, "group_id": scheduled.group_id,
                                               "run_after": scheduled.pending_until, **waits})


def run_due(database: Database, *, now: datetime | None = None) -> int:
    """Run every due Vault, each in its own transaction; returns how many ran.

    A failing Vault does not hold back the others; the first error is raised
    after all of them were tried.
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
