"""Minimal Owner-facing problems, persisted in the notification inbox.

Every unreadable path has its own durable entry: no sampling or diagnostic
walker. Repeated failures of the same path/worker are deduplicated. Exception
messages are deliberately excluded (they may contain credentials or URLs).
"""
from __future__ import annotations

import hashlib
from typing import Any

from ..owner import owner_user_id
from .notifications import enqueue_notification


def report_problem(
    connection: Any, *, event: str, vault_id: int | None = None,
    path: str = "", component: str = "",
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
    display_path = path.encode("utf-8", errors="backslashreplace").decode("utf-8")
    notification = enqueue_notification(
        connection, user_id=owner_id, vault_id=vault_id, event=event,
        title="", body="", title_key=f"notification.{event}.title",
        body_key=f"notification.{event}.body",
        message_params={"path": display_path, "component": component},
        dedupe_key=f"{event}:{vault_id}:{identity}", channels=("in_app",),
    )
    # Reading an earlier occurrence must not hide a later failure of this same
    # path or worker. Keep one durable entry, but make the problem visible again.
    if notification["read"]:
        connection.execute(
            "UPDATE notifications SET read_at=NULL WHERE id=%s AND user_id=%s",
            (notification["id"], owner_id),
        )
        notification["read"] = False
        notification["read_at"] = None
    return notification
