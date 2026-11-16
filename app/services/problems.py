"""Minimal Owner-facing problems, persisted in the notification inbox.

Every unreadable path has its own durable entry: no sampling or diagnostic
walker. Repeated failures of the same path/worker are deduplicated. Exception
messages are deliberately excluded (they may contain credentials or URLs).
"""
from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Collection
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .notifications import set_problem


def report_problem(
    connection: Any, *, event: str, vault_id: int | None = None,
    path: str = "", component: str = "", active: bool = True,
    error_type: str = "",
) -> dict[str, Any] | None:
    identity = hashlib.sha256(
        f"{path}\0{component}".encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    # Linux may expose undecodable filename bytes through surrogateescape.
    # Render them as escapes rather than letting a DB encoding error hide the
    # problem. The identity above still distinguishes the original paths.
    shown = display_path(path)
    params: dict[str, Any] = {"path": shown, "component": component}
    if error_type:
        params["error_type"] = error_type
    if event == "file_unreadable" and active and vault_id is not None and path:
        params.update(_unreadable_access(connection, int(vault_id), path))
    return set_problem(
        connection, vault_id=vault_id, event=event, active=active,
        key=f"{event}:{vault_id}:{identity}",
        params=params,
        email=event == "protection_blocked",
    )


def _unreadable_access(connection: Any, vault_id: int, path: str) -> dict[str, Any]:
    """Whose the unreadable path is, and who FrostVault is (#681).

    Only when the entry itself can be stat'ed (without following a symlink):
    a folder above it that may not be searched hides it, and then nothing is
    added.
    """
    from .write_access import runtime_identity

    try:
        row = connection.execute("SELECT source_root FROM vaults WHERE id=%s", (vault_id,)).fetchone()
        info = os.lstat(Path(str(row["source_root"])) / path)
    except Exception:
        return {}
    return {"owner_uid": info.st_uid, "owner_gid": info.st_gid,
            "mode": f"{stat.S_IMODE(info.st_mode):04o}", **runtime_identity()}


def display_path(path: str) -> str:
    """The safe form of a path: undecodable bytes become visible escapes."""
    return path.encode("utf-8", errors="backslashreplace").decode("utf-8")


def resolve_stale_path_problems(
    connection: Any, *, event: str, vault_id: int, keep_paths: Collection[str],
    keep_prefixes: Collection[str] = (),
) -> None:
    """Close the path problems of one event that a walk proved are gone.

    ``keep_paths`` holds the paths the same walk reported again (as shown to the
    Owner). ``keep_prefixes`` holds folders the walk could not enter: a problem
    below one of them is unknown, not resolved. The walk must have covered the
    whole Vault apart from those folders.
    """
    stamp = datetime.now(timezone.utc)
    connection.execute(
        "UPDATE notifications SET problem_status='resolved', resolved_at=%s, "
        "read_at=COALESCE(read_at,%s) WHERE event=%s AND vault_id=%s "
        "AND problem_status='open' "
        "AND NOT ((message_params->>'path') = ANY(CAST(%s AS TEXT[]))) "
        "AND NOT EXISTS (SELECT 1 FROM unnest(CAST(%s AS TEXT[])) AS kept(prefix) "
        "WHERE left(message_params->>'path', length(kept.prefix) + 1) "
        "= kept.prefix || '/')",
        (stamp, stamp, event, int(vault_id), list(keep_paths), list(keep_prefixes)),
    )
