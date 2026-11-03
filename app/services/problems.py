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
    identity = hashlib.sha256(f"{path}\0{component}".encode()).hexdigest()
    return enqueue_notification(
        connection, user_id=owner_id, vault_id=vault_id, event=event,
        title="", body="", title_key=f"notification.{event}.title",
        body_key=f"notification.{event}.body",
        message_params={"path": path, "component": component},
        dedupe_key=f"{event}:{vault_id}:{identity}", channels=("in_app",),
    )
