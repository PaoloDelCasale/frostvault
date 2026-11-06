"""Installation-wide, restart-safe S3 readiness gate (read-only provider probes).

A transaction advisory lock gives single-flight across threads/processes without
waiting behind a slow provider. No Job or Vault lock is held during the probe.
Only this probe is replayable: this module never runs cloud mutations.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import notifications

LOCK_ID = 461062
MAX_REPROBE_SECONDS = 60


class CloudPreflightBlocked(RuntimeError):
    """Queue suspension, NOT a Job failure or a transfer retry."""


class ReadinessFailure(RuntimeError):
    """Only locally composed, safe remediation text may use this exception."""


def reprobe_seconds(failures: int) -> int:
    return min(MAX_REPROBE_SECONDS, 5 * 2 ** min(max(failures - 1, 0), 4))


def snapshot(connection: Any) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM cloud_preflight_state WHERE id=1").fetchone()
    return dict(row) if row else {"status": "unknown", "remediation": None, "next_probe_at": None}


def check(connect: Callable, probe: Callable[[], None], *, now: datetime | None = None) -> dict[str, Any]:
    """Return a committed cached outcome; never wait for another probe.

    Outcome and due time survive restart. A crash releases the advisory lock;
    it cannot leave a durable 'probing' lease blocking recovery. Failure details
    are allowlisted local prose, never provider exception strings or credentials.
    """
    injected_now = now
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat()
    with connect() as connection:
        state = snapshot(connection)
        if state.get("next_probe_at") and state["next_probe_at"] > stamp:
            return state
        acquired = connection.execute(
            "SELECT pg_try_advisory_xact_lock(%s) AS acquired", (LOCK_ID,),
        ).fetchone()["acquired"]
        if not acquired:
            return {**state, "status": "blocked", "remediation": state.get("remediation") or
                    "S3 readiness check in progress; Jobs remain queued"}
        # A concurrent probe may have committed between our read and lock.
        state = snapshot(connection)
        if state.get("next_probe_at") and state["next_probe_at"] > stamp:
            return state
        remediation = None
        try:
            probe()
        except ReadinessFailure as exc:
            remediation = str(exc)
        except Exception:
            remediation = "S3 readiness could not be confirmed. Check AWS credentials, bucket configuration and connectivity"
        failures = int(state.get("failure_count") or 0) + 1 if remediation else 0
        # Count backoff from completion, not the beginning of a slow request.
        completed = injected_now or datetime.now(timezone.utc)
        due = (completed + timedelta(seconds=reprobe_seconds(failures) if remediation else MAX_REPROBE_SECONDS)).isoformat()
        connection.execute(
            "INSERT INTO cloud_preflight_state(id,status,remediation,failure_count,next_probe_at) "
            "VALUES (1,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET status=EXCLUDED.status, "
            "remediation=EXCLUDED.remediation,failure_count=EXCLUDED.failure_count,next_probe_at=EXCLUDED.next_probe_at",
            ("blocked" if remediation else "ready", remediation, failures, due),
        )
        notifications.set_problem(
            connection, key="blocked:installation:preflight", event="cloud_preflight_blocked",
            active=bool(remediation), params={"remediation": remediation or ""},
        )
        # Supersede pre-461 per-Vault episodes, not Job failure history.
        connection.execute(
            "UPDATE notifications SET problem_status='resolved',resolved_at=%s,read_at=COALESCE(read_at,%s) "
            "WHERE problem_status='open' AND dedupe_key LIKE 'blocked:%%:preflight' "
            "AND dedupe_key<>'blocked:installation:preflight'", (stamp, stamp),
        )
        return snapshot(connection)
