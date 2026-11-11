"""Per-Vault serialization of Job admission.

``lock_vault`` takes a PostgreSQL row lock before reading Job state, so
concurrent admissions never decide from
one stale snapshot. It also rejects normal work for a quiesced Vault.
"""
from __future__ import annotations

from typing import Any


TERMINAL_JOB_STATUSES = ("completed", "failed", "cancelled")


def lock_vault(
    connection: Any, vault_id: int
) -> None:
    """Serialize admission and reject normal work for a quiesced Vault."""
    row = connection.execute(
        """
        SELECT id, decommission_state FROM vaults WHERE id=%s FOR NO KEY UPDATE
        """,
        (vault_id,),
    ).fetchone()
    if not row:
        raise LookupError("vault_not_found")
    lifecycle = str(row.get("decommission_state") or "active")
    if lifecycle != "active":
        raise LookupError("vault_quiesced")
