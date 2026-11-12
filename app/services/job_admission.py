"""Per-Vault serialization of Job admission and the Job status vocabulary.

``lock_vault`` takes a PostgreSQL row lock before reading Job state, so
concurrent admissions never decide from
one stale snapshot. It also rejects normal work for a Vault whose single
``lifecycle`` is not ``active`` (reconnect required, stopping, tombstone).
"""
from __future__ import annotations

from typing import Any


TERMINAL_JOB_STATUSES = ("completed", "failed", "cancelled")
# A Job in one of these states is running and holds a live lease
# (``job_leases``); every other non-terminal state waits for a claim.
JOB_LEASE_HELD_STATUSES = frozenset(
    {"downloading", "uploading", "verifying", "cleaning"}
)
WAITING_JOB_STATUSES = frozenset(
    {"queued", "pending_approval", "pending_delay", "retrying", "restoring"}
)


def lock_vault(
    connection: Any, vault_id: int
) -> None:
    """Serialize admission and reject normal work for a non-active Vault.

    ``FOR NO KEY UPDATE`` serializes admissions on one Vault without
    rewriting its row and still lets claims hold their shared ``KEY SHARE``
    fence (ADR-0031).
    """
    row = connection.execute(
        "SELECT id, lifecycle FROM vaults WHERE id=%s FOR NO KEY UPDATE",
        (vault_id,),
    ).fetchone()
    if not row:
        raise LookupError("vault_not_found")
    if row["lifecycle"] != "active":
        raise LookupError("vault_quiesced")
