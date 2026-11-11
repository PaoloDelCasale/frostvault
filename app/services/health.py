"""Process liveness helpers and dependency-aware readiness."""
from __future__ import annotations

import threading
import time
from typing import Any

from ..config import settings
from ..database import db
from . import source_layout

_lock = threading.Lock()
_worker_heartbeat_at: float | None = None
WORKER_STALE_SECONDS = 120.0


_queue_status: dict[str, Any] = {"stalled": False, "queued": 0, "seconds_without_progress": 0.0}


def set_queue_status(*, stalled: bool, queued: int, idle_seconds: float) -> None:
    """Record whether the Job queue has work but is not advancing (issue #493)."""
    with _lock:
        _queue_status.update(
            stalled=bool(stalled), queued=int(queued),
            seconds_without_progress=round(float(idle_seconds), 1),
        )


def queue_status() -> dict[str, Any]:
    with _lock:
        return dict(_queue_status)


def mark_worker_heartbeat(now: float | None = None) -> None:
    """Record that the background worker loop is alive."""
    global _worker_heartbeat_at
    with _lock:
        _worker_heartbeat_at = now if now is not None else time.monotonic()


def worker_is_healthy(
    *, now: float | None = None, stale_after: float = WORKER_STALE_SECONDS,
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
        from psycopg.conninfo import conninfo_to_dict
        conninfo_to_dict(settings.database_dsn)
        return True
    except Exception:
        return False


def readiness_report() -> dict[str, Any]:
    from . import upload_pipeline, upload_remediation
    checks = {
        "database": check_database(),
        "worker": worker_is_healthy(),
        "config": check_config(),
        "sources_layout": source_layout.sources_layout_is_ready(),
    }
    return {
        "status": "ready" if all(checks.values()) else "not_ready",
        "checks": checks,
        # Informational only: a stalled queue must be visible without turning a
        # healthy process into a failing container health check.
        "queue": queue_status(),
        "upload_journal": upload_pipeline.journal_status(),
        "upload_remediation": (upload_remediation.sweep_health(db) if checks["database"]
                               else {"available": False, "stale": True}),
    }
