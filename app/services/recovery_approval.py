"""Restore Approval of one recovery request; never calls cloud or filesystem APIs.

A high-impact recovery request (one Job group) waits in ``pending_approval``
until the Owner approves it or its deadline passes. The request itself lives in
``restore_requests`` (estimate, ``approval_deadline``, ``approved_at``); the
Jobs only carry their status. There is no per-user attribution: the Owner is
the only approver, so an approval records *when*, never *who* (#589).
"""
from __future__ import annotations

from typing import Any, Sequence

from ..i18n import DEFAULT_LOCALE, translate

APPROVED_MESSAGE = (
    "High-impact restore approved; RestoreObject cannot be cancelled after AWS accepts it"
)


def record_estimate(
    connection: Any, *, group_id: str, estimated_cost_eur: float | None,
    estimated_hours: float | None,
) -> None:
    """Store the request's Cost Estimate once, for the whole group."""
    connection.execute(
        """
        INSERT INTO restore_requests(group_id, estimated_cost_eur, estimated_hours)
        VALUES (%s, %s, %s)
        ON CONFLICT (group_id) DO UPDATE
        SET estimated_cost_eur=EXCLUDED.estimated_cost_eur,
            estimated_hours=EXCLUDED.estimated_hours
        """,
        (group_id, estimated_cost_eur, estimated_hours),
    )


def hold(
    connection: Any, *, group_id: str, job_ids: Sequence[int], deadline: str,
    message: str, now: str,
) -> list[int]:
    """Hold a just-queued recovery request for approval until ``deadline``.

    Runs in the queueing transaction, so no worker can claim a Job before the
    group is either released as ``queued`` or held as ``pending_approval``.
    """
    connection.execute(
        """
        INSERT INTO restore_requests(group_id, approval_deadline)
        VALUES (%s, %s)
        ON CONFLICT (group_id) DO UPDATE
        SET approval_deadline=EXCLUDED.approval_deadline, approved_at=NULL
        """,
        (group_id, deadline),
    )
    rows = connection.execute(
        """
        UPDATE jobs SET status='pending_approval', message=%s, message_key=NULL,
            message_params=NULL, updated_at=%s
        WHERE id=ANY(%s::bigint[]) AND group_id=%s AND action='recover'
          AND status='queued'
        RETURNING id
        """,
        (message, now, list(job_ids), group_id),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def expire_pending(connection: Any, *, vault_id: int, now: str, group_id: str | None = None) -> int:
    """Cancel due requests atomically. Caller owns the Vault lock and transaction.

    Instants are compared in PostgreSQL (``timestamptz``), never as strings.
    The deadline is inclusive. An approved request is never expired, and
    queued/restoring work is never touched, even if a historical deadline remains.
    """
    key = "job.recovery_approval_expired"
    result = connection.execute(
        """
        UPDATE jobs j SET status='cancelled', message=%s, message_key=%s,
               message_params='{}'::jsonb, updated_at=%s
        FROM restore_requests r
        WHERE r.group_id = j.group_id
          AND r.approved_at IS NULL
          AND r.approval_deadline <= %s::timestamptz
          AND j.vault_id=%s AND j.action='recover' AND j.status='pending_approval'
          AND (%s::uuid IS NULL OR j.group_id=%s::uuid)
        """,
        (translate(key, locale=DEFAULT_LOCALE), key, now, now, vault_id,
         group_id, group_id),
    )
    return result.rowcount


def approve(connection: Any, *, vault_id: int, group_id: str, now: str) -> list[int]:
    """Release a still-pending, not yet due request. Caller owns the Vault lock.

    Expiry runs first, in the same transaction, so a late approval releases
    nothing; ``approved_at`` stays strictly before the deadline (schema check).
    """
    expire_pending(connection, vault_id=vault_id, group_id=group_id, now=now)
    approved = connection.execute(
        """
        UPDATE restore_requests r SET approved_at=%s
        WHERE r.group_id=%s AND r.approved_at IS NULL
          AND r.approval_deadline > %s::timestamptz
          AND EXISTS (
              SELECT 1 FROM jobs j
              WHERE j.group_id = r.group_id AND j.vault_id=%s
                AND j.action='recover' AND j.status='pending_approval'
          )
        RETURNING r.group_id
        """,
        (now, group_id, now, vault_id),
    ).fetchone()
    if approved is None:
        return []
    rows = connection.execute(
        """
        UPDATE jobs SET status='queued', message=%s, message_key=NULL,
            message_params=NULL, updated_at=%s
        WHERE vault_id=%s AND group_id=%s AND action='recover'
          AND status='pending_approval'
        RETURNING id
        """,
        (APPROVED_MESSAGE, now, vault_id, group_id),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def sweep_expired(connection: Any, *, now: str) -> int:
    """Sweep even when workers are busy or the Vault is unavailable/paused.

    Lock Vaults in stable order, shared with admission, approval and cancellation.
    No expired approval is made claimable by the scheduler.
    """
    vaults = connection.execute(
        """
        SELECT DISTINCT j.vault_id FROM jobs j
        JOIN restore_requests r ON r.group_id = j.group_id
        WHERE j.action='recover' AND j.status='pending_approval'
          AND r.approved_at IS NULL
          AND r.approval_deadline <= %s::timestamptz
        ORDER BY j.vault_id
        """,
        (now,),
    ).fetchall()
    count = 0
    for vault in vaults:
        connection.execute("SELECT id FROM vaults WHERE id=%s FOR UPDATE", (vault["vault_id"],))
        count += expire_pending(connection, vault_id=vault["vault_id"], now=now)
    return count
