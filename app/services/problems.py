"""Minimal Owner-facing problems, persisted in the notification inbox.

Every unreadable path has its own durable entry: no sampling or diagnostic
walker. Repeated failures of the same path/worker are deduplicated. Exception
messages are deliberately excluded (they may contain credentials or URLs).
"""
from __future__ import annotations

import hashlib
from collections.abc import Collection
from typing import Any

from ..owner import owner_user_id
from .notifications import now_iso, set_problem


def report_problem(
    connection: Any, *, event: str, vault_id: int | None = None,
    path: str = "", component: str = "", active: bool = True,
) -> dict[str, Any] | None:
    owner_id = owner_user_id(connection)
    if owner_id is None:
        return None
    identity = hashlib.sha256(
        f"{path}\0{component}".encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    # Linux may expose undecodable filename bytes through surrogateescape.
    # Render them as escapes rather than letting a DB encoding error hide the
    # problem. The identity above still distinguishes the original paths.
    shown = display_path(path)
    return set_problem(
        connection, vault_id=vault_id, event=event, active=active,
        key=f"{event}:{vault_id}:{identity}",
        params={"path": shown, "component": component},
        email=event == "protection_blocked",
    )


def display_path(path: str) -> str:
    """The safe form of a path: undecodable bytes become visible escapes."""
    return path.encode("utf-8", errors="backslashreplace").decode("utf-8")


def resolve_stale_path_problems(
    connection: Any, *, event: str, vault_id: int, keep_paths: Collection[str],
) -> None:
    """Close the path problems of one event that a complete walk no longer saw.

    ``keep_paths`` holds the displayed paths the same walk reported again. Only
    a walk that covered the whole Vault may call this: it is the proof that the
    cause of every other open problem has disappeared.
    """
    stamp = now_iso()
    connection.execute(
        "UPDATE notifications SET problem_status='resolved', resolved_at=%s, "
        "read_at=COALESCE(read_at,%s) WHERE event=%s AND vault_id=%s "
        "AND problem_status='open' "
        "AND NOT ((message_params::jsonb->>'path') = ANY(CAST(%s AS TEXT[])))",
        (stamp, stamp, event, int(vault_id), list(keep_paths)),
    )
