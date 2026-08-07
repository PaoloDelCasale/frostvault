"""Lifecycle pin persistence and path matching (issue #110)."""
from __future__ import annotations

from typing import Any

from .lifecycle_policies import normalize_logical_path


def set_lifecycle_pin(
    connection: Any,
    *,
    vault_id: int,
    path: str,
    is_directory: bool,
    pinned_by: int | None,
    pinned_at: str,
) -> None:
    from .directory_aggregates import request_vault_rebuild

    normalized = normalize_logical_path(path)
    if not normalized:
        raise ValueError("Pin path is required")
    connection.execute(
        """
        INSERT INTO lifecycle_pins(vault_id, path, is_directory, pinned_at, pinned_by)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT(vault_id, path) DO UPDATE SET
            is_directory=excluded.is_directory,
            pinned_at=excluded.pinned_at,
            pinned_by=excluded.pinned_by
        """,
        (vault_id, normalized, bool(is_directory), pinned_at, pinned_by),
    )
    # Pin matching is prefix-based; rebuild once rather than walk descendants.
    request_vault_rebuild(connection, vault_id)


def clear_lifecycle_pin(connection: Any, *, vault_id: int, path: str) -> bool:
    from .directory_aggregates import request_vault_rebuild

    normalized = normalize_logical_path(path)
    result = connection.execute(
        "DELETE FROM lifecycle_pins WHERE vault_id=%s AND path=%s",
        (vault_id, normalized),
    )
    cleared = bool(getattr(result, "rowcount", 0))
    if cleared:
        request_vault_rebuild(connection, vault_id)
    return cleared


def load_lifecycle_pins(connection: Any, vault_id: int) -> tuple[tuple[str, bool], ...]:
    rows = connection.execute(
        """
        SELECT path, is_directory
        FROM lifecycle_pins
        WHERE vault_id=%s
        ORDER BY path
        """,
        (vault_id,),
    ).fetchall()
    return tuple((row["path"], bool(row["is_directory"])) for row in rows)


def is_path_pinned(connection: Any, vault_id: int, path: str) -> bool:
    normalized = normalize_logical_path(path)
    pins = load_lifecycle_pins(connection, vault_id)
    for pin_path, is_directory in pins:
        if is_directory:
            if normalized == pin_path or normalized.startswith(f"{pin_path}/"):
                return True
        elif normalized == pin_path:
            return True
    return False
