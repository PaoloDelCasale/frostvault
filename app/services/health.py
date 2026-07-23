"""Process liveness helpers and dependency-aware readiness (issue #16)."""
from __future__ import annotations

import threading
import time
from typing import Any

from ..config import settings
from ..database import db

_lock = threading.Lock()
_worker_heartbeat_at: float | None = None
# Workers are considered stale after this many seconds without a heartbeat.
WORKER_STALE_SECONDS = 120.0


def mark_worker_heartbeat(now: float | None = None) -> None:
    """Record that the background worker loop is alive."""
    global _worker_heartbeat_at
    with _lock:
        _worker_heartbeat_at = now if now is not None else time.monotonic()


def worker_is_healthy(
    *,
    now: float | None = None,
    stale_after: float = WORKER_STALE_SECONDS,
) -> bool:
    with _lock:
        heartbeat = _worker_heartbeat_at
    if heartbeat is None:
        return False
    current = now if now is not None else time.monotonic()
    return (current - heartbeat) <= stale_after


def check_database() -> bool:
    """Return True when a trivial database round-trip succeeds."""
    try:
        with db() as connection:
            row = connection.execute("SELECT 1 AS ok").fetchone()
            return bool(row and (row.get("ok") == 1 or list(row.values())[0] == 1))
    except Exception:
        return False


def check_config() -> bool:
    """Return True when core settings look startable."""
    try:
        if settings.db_backend not in {"sqlite", "postgresql"}:
            return False
        if settings.db_backend == "sqlite" and not str(settings.sqlite_path).strip():
            return False
        return True
    except Exception:
        return False


def readiness_report() -> dict[str, Any]:
    checks = {
        "database": check_database(),
        "worker": worker_is_healthy(),
        "config": check_config(),
    }
    ready = all(checks.values())
    return {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
    }
