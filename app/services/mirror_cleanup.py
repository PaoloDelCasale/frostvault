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

from pathlib import PurePosixPath
from typing import Any

from . import cleanup_engine, cloud_deletion
from .notifications import ProblemFamily
from .lifecycle_policies import load_vault_plan
from .vault_moves import MINIMUM_DAYS

MAX_FOLDERS = 100
MAX_FOLDER_LENGTH = 1024
DELETION_DELAY_DAYS = cloud_deletion.DELETION_DELAY_SECONDS // 86400
_GLOB = frozenset("*?[]{}")


class MirrorCleanupError(cleanup_engine.CleanupError):
    """The settings cannot be changed: ``reason`` names the Owner-facing message."""

    prefix = "api.mirror_cleanup"
    reasons = frozenset({"stopped", "folders"})


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


def _candidates(connection: Any, vault: dict[str, Any],
                settings: cleanup_engine.Settings) -> list[dict[str, Any]]:
    """The Cloud-only files under the scope now."""
    return cloud_deletion.cloud_only_files(connection, vault, folders=settings["folders"])[0]


def _select(connection: Any, vault: dict[str, Any], settings: cleanup_engine.Settings, *,
            seen_before: str, now: Any):
    return cloud_deletion.automatic_mirror(
        connection, vault, seen_before=seen_before, folders=settings["folders"],
        beyond_minimum=settings["beyond_minimum"], now=now)


def _cloud_life(connection: Any, row: dict[str, Any]) -> dict[str, Any]:
    return {"cloud_life": cloud_life(row, load_vault_plan(connection, int(row["id"])))}


CLEANUP = cleanup_engine.Cleanup(
    name="mirror_cleanup", grace_days_range=(1, 180), error=MirrorCleanupError,
    mirror=True, candidates=_candidates, select=_select, normalize_folders=normalize_folders,
    loosens=loosens, status_extra=_cloud_life, beyond_minimum_checked_first=True)

POLL_SECONDS = cleanup_engine.POLL_SECONDS
NOTICE_EVENT = CLEANUP.notice_event
problem_key = CLEANUP.problem_key
# A capped Mirror Cleanup (#771) closes the same way, once its cleanup is off.
PROBLEM_FAMILY = ProblemFamily(frozenset({CLEANUP.problem_event}), """
    n.event='mirror_cleanup_capped' AND NOT EXISTS (
        SELECT 1 FROM vaults v WHERE v.id=n.vault_id AND v.mirror_cleanup_enabled
    )
""")
status = CLEANUP.status
update_settings = CLEANUP.update_settings
due_vaults = CLEANUP.due_vaults
run_vault = CLEANUP.run_vault
run_due = CLEANUP.run_due
