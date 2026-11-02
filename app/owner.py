"""The single owner of this FrostVault instance (#412, #366 Q1).

FrostVault has exactly one User: the owner. The row is created at first
startup from ``BOOTSTRAP_ADMIN_*`` and every Vault belongs to it. Background
work that needs a recipient or a requester resolves it here.
"""
from __future__ import annotations

from typing import Any


def owner_user_id(connection: Any) -> int | None:
    """Return the owner's id, or ``None`` before bootstrap has run."""
    row = connection.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    return int(row["id"]) if row else None
