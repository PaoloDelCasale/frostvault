"""The daily run shared by the cleanups (#799; ADR-0048, ADR-0052).

Automatic Duplicate Cleanup and Mirror Cleanup differ in what they delete, not
in how a run works. Each describes itself with one :class:`Cleanup`: its name
(the prefix of its ``vaults`` columns, its Problem, its events and messages),
its limits, and the two functions that choose its candidates. This module owns
the rest, per Vault: the settings, when a run is due, what a run records, the
capped Problem, the pending deletions and the totals.

Once the Owner enables a cleanup, each active, unpaused Vault runs it once a
day: the candidates whose Local Copy was last seen at least ``grace_days`` ago
become one Delayed Cloud Deletion marked automatic. A run whose candidates
exceed the Vault's cap (files or bytes) schedules nothing and opens a Vault
Problem; it closes at the next run under the cap, or when the cleanup is
turned off. ``<name>_last_run_at`` is persisted with the run, so a restart
never runs a Vault again early.

The column names are composed from ``Cleanup.name`` and the fixed suffixes of
``_COLUMNS`` only: both are constants of the code, never input.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.types.json import Jsonb

from ..i18n import DEFAULT_LOCALE, translate
from . import cloud_deletion, minimum_storage
from .audit_events import record_audit_event
from .lifecycle_policies import load_vault_plan

POLL_SECONDS = 300.0
RUN_INTERVAL = timedelta(days=1)
MAX_CAP_FILES = 1_000_000
MAX_CAP_BYTES = 1024 ** 5  # 1 PiB
OUTCOMES = ("scheduled", "none", "capped", "skipped")
_COLUMNS = frozenset({
    "enabled", "grace_days", "cap_files", "cap_bytes", "folders", "beyond_minimum",
    "last_run_at", "last_outcome", "deleted_count", "deleted_bytes", "last_deleted_at"})

Database = Callable[[], AbstractContextManager[Any]]
Settings = dict[str, Any]


class CleanupError(ValueError):
    """The settings cannot be changed: ``code`` names the Owner-facing message."""

    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value else None)


def _size(rows: list[dict[str, Any]]) -> int:
    return sum(int(r["size"] or 0) for r in rows)


def _amount(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {"count": len(rows), "bytes": _size(rows)}


def _earliest_run(next_run: datetime, at: datetime) -> datetime:
    """The first daily run at or after ``at``, counting from ``next_run``."""
    if next_run >= at:
        return next_run
    days = -(-(at - next_run) // RUN_INTERVAL)  # ceiling division
    return next_run + days * RUN_INTERVAL


@dataclass(frozen=True, kw_only=True)
class Cleanup:
    """One cleanup, as its module declares it.

    ``candidates(connection, vault, settings)`` lists what the cleanup could
    delete now, for the status; ``select(connection, vault, settings,
    seen_before=, now=)`` returns the preview and the sighting split of what a
    run takes. Both raise ``CloudDeletionError`` when nothing can be proven.

    Where the two cleanups already differed, each keeps its side here:
    ``mirror`` (its groups are marked ``mirror_cleanup`` and may hold the only
    copy), ``normalize_folders`` (a scope of folders, validated by the
    cleanup), ``loosens`` (which changes ask for a fresh authentication inside
    :meth:`update_settings`), ``status_extra`` (more to say in the status) and
    ``beyond_minimum_checked_first`` (which of two invalid settings is
    reported).
    """

    name: str
    grace_days_range: tuple[int, int]
    error: type[CleanupError]
    mirror: bool
    candidates: Callable[[Any, dict[str, Any], Settings], list[dict[str, Any]]]
    select: Callable[..., tuple[cloud_deletion.DeletionPreview, cloud_deletion.Sighting]]
    normalize_folders: Callable[[Any], list[str]] | None = None
    loosens: Callable[[Settings, Settings], bool] | None = None
    status_extra: Callable[[Any, dict[str, Any]], dict[str, Any]] | None = None
    beyond_minimum_checked_first: bool = False

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z]+(_[a-z]+)*", self.name):
            raise ValueError(f"invalid cleanup name: {self.name!r}")

    # -- names ---------------------------------------------------------------

    def column(self, suffix: str) -> str:
        """Its ``vaults`` column: two constants of the code, never input."""
        if suffix not in _COLUMNS:
            raise KeyError(suffix)
        return f"{self.name}_{suffix}"

    @property
    def problem_event(self) -> str:
        return f"{self.name}_capped"

    @property
    def notice_event(self) -> str:
        return f"{self.name}_scheduled"

    def problem_key(self, vault_id: int) -> str:
        return f"{self.name}:{vault_id}"

    # -- settings ------------------------------------------------------------

    def settings(self, row: dict[str, Any]) -> Settings:
        values: Settings = {
            "enabled": bool(row[self.column("enabled")]),
            "grace_days": int(row[self.column("grace_days")]),
            "cap_files": int(row[self.column("cap_files")]),
            "cap_bytes": int(row[self.column("cap_bytes")]),
        }
        if self.normalize_folders is not None:
            values["folders"] = list(row[self.column("folders")] or [])
        values["beyond_minimum"] = bool(row[self.column("beyond_minimum")])
        return values

    def update_settings(self, connection: Any, *, vault_id: int, actor: str, values: dict[str, Any],
                        require_reauth: Callable[[], Any] | None = None) -> dict[str, Any]:
        """Change the Vault's settings; turning the cleanup off closes its Problem.

        ``require_reauth`` is called (and may raise) when the change loosens
        the settings (``loosens``); the HTTP layer passes its
        fresh-authentication check. Without it, or for a cleanup without
        ``loosens``, every change is accepted (the caller already checked).
        """
        vault = cloud_deletion.lock_vault(connection, vault_id)
        if vault["lifecycle"] in {"stopping", "tombstone"}:
            raise self.error("Folder protection has stopped", "stopped")
        current = self.settings(vault)
        unknown = set(values) - set(current)
        if unknown:
            raise self.error(f"Unknown settings: {sorted(unknown)}")
        merged = {**current, **{k: v for k, v in values.items() if v is not None}}

        def flag(name: str) -> None:
            if not isinstance(merged[name], bool):
                raise self.error(f"{name} must be true or false")

        flag("enabled")
        if self.beyond_minimum_checked_first:
            flag("beyond_minimum")
        low, high = self.grace_days_range
        for name, minimum, maximum in (("grace_days", low, high), ("cap_files", 1, MAX_CAP_FILES),
                                       ("cap_bytes", 1, MAX_CAP_BYTES)):
            value = merged[name]
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise self.error(f"{name} must be between {minimum} and {maximum}", name)
        if not self.beyond_minimum_checked_first:
            flag("beyond_minimum")
        if self.normalize_folders is not None:
            merged["folders"] = self.normalize_folders(merged["folders"])
        if require_reauth is not None and self.loosens is not None and self.loosens(current, merged):
            require_reauth()
        assignments = ", ".join(f"{self.column(name)}=%s" for name in merged)
        connection.execute(f"UPDATE vaults SET {assignments} WHERE id=%s", (*merged.values(), vault_id))
        if not merged["enabled"]:
            self._set_capped_problem(connection, vault_id, active=False)
        if merged != current:
            record_audit_event(connection, event=f"{self.name}.settings_updated", actor=actor,
                               vault_id=vault_id, outcome="updated", before=current, after=merged)
        return self.status(connection, vault_id)

    # -- status --------------------------------------------------------------

    def _live(self, connection: Any, row: dict[str, Any], settings: Settings, *, now: datetime,
              next_run: datetime | None) -> dict[str, Any]:
        """The candidates there are now, live, split as a run would (#729).

        ``current``: all of them; ``waiting``: those whose Local Copy was seen
        within the last ``grace_days``, with when the first becomes eligible and
        the earliest it can be deleted (the first daily run from then, plus the
        seven-day delay; None while the cleanup is off); ``ready``: those the next
        run would take; ``unseen``: those whose Local Copy was never seen, which
        the cleanup never deletes. All None when nothing can be proven now (a
        stopped Vault, an unavailable or replaced root, unconfirmed observations).

        ``minimum_storage``: the current candidates still within their Minimum
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
            rows = self.candidates(connection, row, settings)
        except cloud_deletion.CloudDeletionError:
            return unknown
        grace = timedelta(days=settings["grace_days"])
        sighting = cloud_deletion.split_by_sighting(rows, now - grace)
        plan = load_vault_plan(connection, int(row["id"]))
        executes = now + timedelta(seconds=cloud_deletion.DELETION_DELAY_SECONDS)
        ready, held = sighting.ready, None
        if settings["beyond_minimum"]:
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

    def _totals(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """What automatic deletions removed so far, as recorded on the Vault when
        each member was deleted; None until a first one is recorded (#729)."""
        if row[self.column("deleted_count")] is None:
            return None
        return {"deleted_count": int(row[self.column("deleted_count")]),
                "deleted_bytes": int(row[self.column("deleted_bytes")]),
                "last_deleted_at": _iso(row[self.column("last_deleted_at")])}

    def status(self, connection: Any, vault_id: int, *, now: datetime | None = None) -> dict[str, Any]:
        """The Vault's settings, its last run, its pending automatic deletions,
        its candidates now, waiting and never seen, and the totals deleted so
        far, kept on the Vault so they outlive the history retention of their
        Jobs (#729)."""
        from .vaults import VAULT_ROW_SQL

        # With its Vault Target: the candidates are listed under its prefix (#735).
        row = connection.execute(f"{VAULT_ROW_SQL} WHERE v.id=%s", (vault_id,)).fetchone()
        if row is None:
            raise LookupError("Vault not found")
        moment = now or datetime.now(timezone.utc).replace(microsecond=0)
        own = "r.mirror_cleanup" if self.mirror else "NOT r.mirror_cleanup"
        pending = connection.execute(
            f"""SELECT j.group_id, count(*) AS file_count, COALESCE(sum(j.total_bytes), 0) AS byte_count,
                  min(j.run_after) AS run_after, bool_and(j.status = 'pending_delay') AS cancellable
           FROM jobs j JOIN cloud_purge_requests r ON r.group_id = j.group_id
           WHERE j.vault_id=%s AND r.automatic AND {own} AND j.action='cloud-purge'
             AND j.status NOT IN ('completed','failed','cancelled')
           GROUP BY j.group_id ORDER BY min(j.run_after), j.group_id""",
            (vault_id,)).fetchall()
        settings = self.settings(row)
        last = row[self.column("last_run_at")]
        next_run = last + RUN_INTERVAL if last and settings["enabled"] else None
        # Never run yet: the first run is due at the next poll.
        upcoming = (next_run or moment) if settings["enabled"] else None
        low, high = self.grace_days_range
        return {
            **settings,
            "grace_days_min": low,
            "grace_days_max": high,
            "last_run_at": _iso(last),
            "next_run_at": _iso(next_run),
            "last_outcome": row[self.column("last_outcome")],
            **(self.status_extra(connection, row) if self.status_extra is not None else {}),
            **self._live(connection, row, settings, now=moment, next_run=upcoming),
            "totals": self._totals(row),
            "pending": [{"group_id": str(p["group_id"]), "file_count": int(p["file_count"]),
                         "byte_count": int(p["byte_count"]), "run_after": _iso(p["run_after"]),
                         "cancellable": bool(p["cancellable"])} for p in pending],
        }

    # -- the daily run -------------------------------------------------------

    def _set_capped_problem(self, connection: Any, vault_id: int, *, active: bool,
                            params: dict[str, Any] | None = None) -> None:
        from .notifications import set_problem
        set_problem(connection, key=self.problem_key(vault_id), event=self.problem_event,
                    vault_id=vault_id, active=active, params=params, email=True)

    def due_vaults(self, connection: Any, *, now: datetime) -> list[int]:
        """Active, unpaused Vaults with the cleanup on and no run in the last day."""
        enabled, last_run_at = self.column("enabled"), self.column("last_run_at")
        rows = connection.execute(
            f"""SELECT id FROM vaults
               WHERE {enabled} AND lifecycle='active' AND NOT paused
                 AND root_released_at IS NULL
                 AND ({last_run_at} IS NULL
                      OR {last_run_at} <= %s::timestamptz)
               ORDER BY id""",
            (now - RUN_INTERVAL,)).fetchall()
        return [int(r["id"]) for r in rows]

    def _finish(self, connection: Any, vault_id: int, now: datetime,
                outcome: dict[str, Any]) -> dict[str, Any]:
        outcome = {**outcome, "at": now.isoformat()}
        connection.execute(
            f"UPDATE vaults SET {self.column('last_run_at')}=%s, {self.column('last_outcome')}=%s WHERE id=%s",
            (now, Jsonb(outcome), vault_id))
        return outcome

    def run_vault(self, connection: Any, vault_id: int, *, now: datetime) -> dict[str, Any] | None:
        """One daily run of one Vault, in the caller's transaction.

        Returns the recorded outcome, or None when the Vault is not due (turned
        off, paused, no longer active, or already run within the last day).
        """
        vault = cloud_deletion.lock_vault(connection, vault_id)
        last = vault[self.column("last_run_at")]
        if (not vault[self.column("enabled")] or vault["lifecycle"] != "active"
                or vault["paused"] or vault.get("root_released_at")
                or (last is not None and last > now - RUN_INTERVAL)):
            return None
        settings = self.settings(vault)
        seen_before = (now - timedelta(days=settings["grace_days"])).isoformat()
        try:
            preview, sighting = self.select(connection, vault, settings, seen_before=seen_before, now=now)
        except cloud_deletion.CloudDeletionError as exc:
            # Nothing can be proven today (an unreadable root, unconfirmed
            # observations): schedule nothing and try again tomorrow.
            record_audit_event(connection, event=f"{self.name}.skipped", actor="system",
                               vault_id=vault_id, outcome="skipped", reason=str(exc)[:200])
            return self._finish(connection, vault_id, now, {"outcome": "skipped"})
        files, count, size = sighting.ready, preview.object_count, preview.byte_count
        # Every outcome also says how many candidates are still in their wait, and
        # how many were never seen locally, which the cleanup never deletes (#729).
        waits = {"waiting_count": len(sighting.waiting), "waiting_bytes": _size(sighting.waiting),
                 "unseen_count": len(sighting.unseen)}
        if settings["beyond_minimum"]:
            # Held until their Minimum Storage Duration ends (#731).
            waits.update(held_count=len(sighting.held), held_bytes=_size(sighting.held))
        if not files:
            self._set_capped_problem(connection, vault_id, active=False)
            return self._finish(connection, vault_id, now,
                                {"outcome": "none", "file_count": 0, "byte_count": 0, **waits})
        if count > settings["cap_files"] or size > settings["cap_bytes"]:
            params = {"count": count, "byte_count": size, "cap_files": settings["cap_files"],
                      "cap_bytes": settings["cap_bytes"], self.name: True}
            self._set_capped_problem(connection, vault_id, active=True, params=params)
            record_audit_event(connection, event=f"{self.name}.capped", actor="system",
                               vault_id=vault_id, outcome="capped", file_count=count, byte_count=size,
                               cap_files=settings["cap_files"], cap_bytes=settings["cap_bytes"])
            return self._finish(connection, vault_id, now,
                                {"outcome": "capped", "file_count": count, "byte_count": size, **waits})
        self._set_capped_problem(connection, vault_id, active=False)
        minimum = preview.minimum_storage or {}
        if minimum.get("count"):
            # What the scheduled candidates still bill for their minimum (#731).
            waits.update(minimum_count=int(minimum["count"]), minimum_bytes=int(minimum["bytes"]),
                         minimum_cost=float(minimum["remaining_cost"]),
                         savings_from=minimum.get("savings_from"))
        scheduled = cloud_deletion.schedule_automatic_duplicates(
            connection, vault=vault, files=files, preview=preview, requested_at=now.isoformat(),
            reason=translate(f"{self.name}.reason", locale=DEFAULT_LOCALE), mirror=self.mirror)
        from .notifications import enqueue_notification
        notice = self.notice_event
        enqueue_notification(
            connection, vault_id=vault_id, event=notice, title="",
            title_key=f"notification.{notice}.title", body_key=f"notification.{notice}.body",
            message_params={"count": count, "byte_count": size, "executes_at": scheduled.pending_until,
                            "group_id": scheduled.group_id, self.name: True},
            dedupe_key=f"{notice}:{scheduled.group_id}")
        return self._finish(connection, vault_id, now, {"outcome": "scheduled", "file_count": count,
                                                        "byte_count": size, "group_id": scheduled.group_id,
                                                        "run_after": scheduled.pending_until, **waits})

    def run_due(self, database: Database, *, now: datetime | None = None) -> int:
        """Run every due Vault, each in its own transaction; returns how many ran.

        A failing Vault does not hold back the others; the first error is raised
        after all of them were tried, and that Vault is tried again at the next poll.
        """
        moment = (now or datetime.now(timezone.utc)).replace(microsecond=0)
        with database() as connection:
            vault_ids = self.due_vaults(connection, now=moment)
        ran = 0
        failure: Exception | None = None
        for vault_id in vault_ids:
            try:
                with database() as connection:
                    ran += self.run_vault(connection, vault_id, now=moment) is not None
            except Exception as exc:  # noqa: BLE001 - reported once below
                failure = failure or exc
        if failure is not None:
            raise failure
        return ran
