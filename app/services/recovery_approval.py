"""Request-level approval maintenance; never calls cloud or filesystem APIs."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..i18n import DEFAULT_LOCALE, translate


def expire_pending(connection: Any, *, vault_id: int, now: str, group_id: str | None = None) -> int:
    """Cancel due groups atomically. Caller owns the Vault lock and transaction.

    Compare instants, not ISO strings (legacy timestamps may have offsets).
    A single expired member revokes the whole still-pending request. Never touch
    queued/approved/restoring work, even if a historical deadline remains.
    """
    rows = connection.execute(
        """SELECT group_id, pending_until FROM jobs
           WHERE vault_id=%s AND action='recover' AND status='pending_approval'
             AND (%s::text IS NULL OR group_id=%s)""",
        (vault_id, group_id, group_id),
    ).fetchall()
    current = datetime.fromisoformat(now).astimezone(timezone.utc)
    due = {row["group_id"] for row in rows if row["pending_until"] is not None
           and datetime.fromisoformat(str(row["pending_until"])).astimezone(timezone.utc) <= current}
    if not due:
        return 0
    key = "job.recovery_approval_expired"
    result = connection.execute(
        """UPDATE jobs SET status='cancelled', message=%s, message_key=%s,
               message_params='{}', pending_until=NULL, updated_at=%s
           WHERE vault_id=%s AND action='recover' AND status='pending_approval'
             AND group_id=ANY(%s)""",
        (translate(key, locale=DEFAULT_LOCALE), key, now, vault_id, list(due)),
    )
    return result.rowcount


def sweep_expired(connection: Any, *, now: str) -> int:
    """Sweep even when workers are busy or the Vault is unavailable/paused.

    Lock Vaults in stable order, shared with admission, approval and cancellation.
    No expired approval is made claimable by the scheduler.
    """
    vaults = connection.execute(
        """SELECT DISTINCT vault_id FROM jobs WHERE action='recover'
           AND status='pending_approval' AND pending_until IS NOT NULL
           AND pending_until::timestamptz <= %s::timestamptz ORDER BY vault_id""", (now,),
    ).fetchall()
    count = 0
    for vault in vaults:
        connection.execute("SELECT id FROM vaults WHERE id=%s FOR UPDATE", (vault["vault_id"],))
        count += expire_pending(connection, vault_id=vault["vault_id"], now=now)
    return count
