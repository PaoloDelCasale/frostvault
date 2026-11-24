"""The Vault row locks, by name, in one place (ADR-0031, #804).

A transaction that serializes on a Vault takes its ``vaults`` row with one of
the modes below, and always before the Job, File, lease or aggregate rows it
then touches: the repository-wide lock order (Vault row first, #485). Each
function emits exactly the statement its callers wrote by hand before; what a
caller does with a missing or non-active Vault stays with the caller.

The modes, weakest first (PostgreSQL row-level locks):

- ``KEY_SHARE``: a claimed worker's own step between two held statuses
  (ADR-0031). It conflicts only with ``UPDATE``.
- ``SHARE``: the audited upload catalog units and snapshot cleanup read units
  (ADR-0031, #551). Shared holders overlap; lifecycle, identity and admission
  writers still wait.
- ``NO_KEY_UPDATE``: admission, result publication and every writer of
  Vault-scoped derived rows. It is the mode of ``vault_lock_touch`` and stays
  compatible with the foreign-key ``KEY SHARE`` of inserts.
- ``UPDATE``: decisions on unstarted work (approve, cancel, expire), pause,
  Stop Protection and deletions. It also waits for the claims' ``KEY SHARE``.

The module imports nothing from the application, so any service can take a
Vault lock without an import cycle.
"""
from __future__ import annotations

from typing import Any, Iterable, Literal

VaultLockMode = Literal["KEY SHARE", "SHARE", "NO KEY UPDATE", "UPDATE"]

KEY_SHARE: VaultLockMode = "KEY SHARE"
SHARE: VaultLockMode = "SHARE"
NO_KEY_UPDATE: VaultLockMode = "NO KEY UPDATE"
UPDATE: VaultLockMode = "UPDATE"


def lock_vault(
    connection: Any,
    vault_id: int,
    mode: VaultLockMode,
    *,
    columns: str = "id",
    active_only: bool = False,
    skip_locked: bool = False,
) -> dict[str, Any] | None:
    """Lock one Vault row in ``mode`` and return its ``columns``, or ``None``.

    ``active_only`` locks (and returns) the row only while the Vault is
    active. ``skip_locked`` returns ``None`` instead of waiting for a holder.
    """
    where = "id=%s AND lifecycle='active'" if active_only else "id=%s"
    skip = " SKIP LOCKED" if skip_locked else ""
    return connection.execute(
        f"SELECT {columns} FROM vaults WHERE {where} FOR {mode}{skip}",
        (vault_id,),
    ).fetchone()


def lock_vault_view(
    connection: Any, vault_id: int, mode: VaultLockMode, *, view_sql: str
) -> dict[str, Any] | None:
    """Lock the Vault row of ``view_sql`` (``vaults v`` joined) and return the row."""
    return connection.execute(
        f"{view_sql} WHERE v.id=%s FOR {mode} OF v", (vault_id,)
    ).fetchone()


def lock_job_vault(connection: Any, job_id: int, mode: VaultLockMode) -> None:
    """Lock the row of the Vault a Job belongs to, before the Job row itself."""
    connection.execute(
        f"SELECT id FROM vaults WHERE id=(SELECT vault_id FROM jobs WHERE id=%s) FOR {mode}",
        (job_id,),
    )


def lock_claim_vault(connection: Any, job_id: int) -> bool:
    """A claim's shared fence on its Job's Vault: ``KEY SHARE``, ``SKIP LOCKED``.

    False when the Vault is held by a conflicting lock (pause, a decision), so
    the scheduler skips it instead of blocking (ADR-0031).
    """
    return connection.execute(
        """SELECT v.id FROM vaults v
               WHERE v.id=(SELECT vault_id FROM jobs WHERE id=%s)
               FOR KEY SHARE SKIP LOCKED""", (job_id,),
    ).fetchone() is not None


def lock_vaults_in_order(connection: Any, vault_ids: Iterable[int]) -> None:
    """Take the Vault row lock(s) in ascending id order (the repo-wide order).

    Every writer of Vault-scoped derived rows (scanner/worker transactions,
    catalog revisions, aggregate maintenance) must hold the Vault row BEFORE it
    writes ``directory_aggregates`` or ``directory_aggregate_status``. Taking
    it afterwards inverts the order of ``vault_lock_touch`` /
    ``CatalogEventStore._lock_vault`` / ``lock_vault`` and produced the #485
    deadlock (``INSERT INTO directory_aggregates`` vs ``UPDATE vaults``).

    ``FOR NO KEY UPDATE`` is exactly the mode of the ``UPDATE vaults SET
    name=name`` touch and stays compatible with FK ``KEY SHARE`` inserts. The
    lock is reentrant inside one transaction, so later touches are free. Several
    Vaults are locked in one ordered statement so concurrent global flushes
    cannot cross each other either.
    """
    ordered = sorted({int(vault_id) for vault_id in vault_ids})
    if not ordered:
        return
    connection.execute(
        "SELECT id FROM vaults WHERE id = ANY(%s) ORDER BY id FOR NO KEY UPDATE",
        (ordered,),
    ).fetchall()


def vault_lock_touch(
    connection: Any, vault_id: int, *, returning: str | None = None
) -> Any:
    """Lock one Vault row ``FOR NO KEY UPDATE`` with a same-value UPDATE.

    The touch rewrites no canonical data. It serializes identity creation per
    Vault across scanner and worker transactions, and keeps the repository-wide
    lock order: Vault row first, then file rows. Taken once per batch, never
    once per file. Callers read ``rowcount`` (or the ``returning`` columns) to
    learn whether the Vault exists.
    """
    if returning is None:
        return connection.execute(
            "UPDATE vaults SET name=name WHERE id=%s",
            (vault_id,),
        )
    return connection.execute(
        f"UPDATE vaults SET name=name WHERE id=%s RETURNING {returning}",
        (vault_id,),
    )
