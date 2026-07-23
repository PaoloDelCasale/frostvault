"""Persisted, classified background-worker errors (issue #16)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def classify_exception(exc: BaseException) -> str:
    """Map an exception to a coarse, operator-facing classification."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, (ConnectionError, BrokenPipeError, ConnectionResetError)):
        return "connectivity"
    if isinstance(exc, PermissionError):
        return "permission"
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return "configuration"
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if "timeout" in name or "timed out" in message:
        return "timeout"
    if "connection" in name or "network" in message:
        return "connectivity"
    if "permission" in message or "denied" in message or "forbidden" in message:
        return "permission"
    if "config" in message or "setting" in message:
        return "configuration"
    return "unexpected"


def record_worker_error(
    connection: Any,
    *,
    component: str,
    exc: BaseException,
    vault_id: int | None = None,
    **detail: Any,
) -> dict[str, Any]:
    classification = classify_exception(exc)
    message = str(exc)[:1000] or type(exc).__name__
    stamp = now_iso()
    detail_json = json.dumps(
        {"exception_type": type(exc).__name__, **detail},
        sort_keys=True,
        default=str,
    )
    row = connection.execute(
        """
        INSERT INTO worker_errors(
            created_at, component, classification, message, vault_id, detail_json
        ) VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (stamp, component, classification, message, vault_id, detail_json),
    ).fetchone()
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "component": row["component"],
        "classification": row["classification"],
        "message": row["message"],
        "vault_id": row.get("vault_id"),
        "detail": json.loads(row["detail_json"] or "{}"),
    }


def list_worker_errors(
    connection: Any, *, limit: int = 100
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT * FROM worker_errors
        ORDER BY id DESC
        LIMIT %s
        """,
        (max(1, min(limit, 500)),),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "created_at": row["created_at"],
            "component": row["component"],
            "classification": row["classification"],
            "message": row["message"],
            "vault_id": row.get("vault_id"),
            "detail": json.loads(row["detail_json"] or "{}"),
        }
        for row in rows
    ]
