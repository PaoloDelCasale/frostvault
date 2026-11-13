"""Deterministic, fair selection of runnable Vault turns (ADR-0026, ADR-0037)."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Mapping, Sequence

USER_OPERATION_PRIORITY = 0
MANUAL_UPLOAD_PRIORITY = 1
AUTOMATIC_PRIORITY = 2
AUTOMATIC_UPLOAD_PRIORITY = AUTOMATIC_PRIORITY
AGING_SECONDS = 900.0


def job_priority(job: Mapping[str, Any]) -> int:
    """Base tier *inside a Vault*: Owner operations, Owner uploads, then automatic work.

    Bulk manual uploads remain manual. Aging never changes this order inside
    a Vault, nor the durable origin of a Job. Across Vaults see
    :func:`cross_vault_tier` (ADR-0037).
    """
    if job.get("origin") == "automatic":
        return AUTOMATIC_PRIORITY
    if job.get("action") != "upload":
        return USER_OPERATION_PRIORITY
    return MANUAL_UPLOAD_PRIORITY


def effective_priority(job: Mapping[str, Any], now: datetime | None = None) -> int:
    """Promote one turn using *accounted runnable/unserved* time, not Job age.

    ``now`` remains a compatible seam; the catalog owns the durable clock.
    Missing accounting means no credit, never a fallback to requested_at.
    """
    waited = max(0.0, float(job.get("scheduler_wait_seconds") or 0))
    return max(USER_OPERATION_PRIORITY, job_priority(job) - int(waited // AGING_SECONDS))


def cross_vault_tier(job: Mapping[str, Any]) -> int:
    """Tier at which a Vault's turn competes with other Vaults (ADR-0037).

    Owner operations still come first; uploads the Owner asked for and work
    admitted automatically share one tier, so a manual backlog in one Vault
    never holds another Vault's automatic protection back.
    """
    return min(job_priority(job), MANUAL_UPLOAD_PRIORITY)


def effective_cross_vault_tier(job: Mapping[str, Any]) -> int:
    """Cross-Vault tier after aging: only the aged-turn exception reaches tier 0.

    A manual turn reaches the Owner-operation tier after one aging interval,
    an automatic one after two, exactly as before ADR-0037.
    """
    return min(effective_priority(job), MANUAL_UPLOAD_PRIORITY)


def turn_is_aged(job: Mapping[str, Any]) -> bool:
    """True when banked credit lets this turn outrank its cross-Vault tier."""
    return effective_cross_vault_tier(job) < cross_vault_tier(job)


def select_fair_jobs(
    jobs: Sequence[Mapping[str, Any]],
    *,
    limit: int,
    active_by_vault: Mapping[Any, int] | None = None,
    now: datetime | None = None,
) -> list[Mapping[str, Any]]:
    """Select turns, preserving base-tier/oldest-first order inside each Vault.

    Across Vaults turns compete at their cross-Vault tier (ADR-0037): Owner
    operations, then every upload and automatic work together. Equal-tier turns prefer fewer live leases, then the longest-unserved turn
    (including concurrency 1, where every active count can be zero). A turn's
    credit is spent by its first nomination in this pass; later backlog entries
    compete at their base tier. Only the catalog's successful conditional claim
    actually consumes durable credit. A generation fences concurrent nominees.
    """
    if limit <= 0 or not jobs:
        return []
    active = dict(active_by_vault or {})
    by_vault: dict[Any, list[Mapping[str, Any]]] = defaultdict(list)
    for job in sorted(jobs, key=_job_order):
        by_vault[job.get("vault_id")].append(job)
    position = {vault_id: 0 for vault_id in by_vault}

    def vault_key(vault_id: Any) -> tuple:
        head = by_vault[vault_id][position[vault_id]]
        waited = float(head.get("scheduler_wait_seconds") or 0) if position[vault_id] == 0 else 0.0
        tier = (effective_cross_vault_tier(head) if position[vault_id] == 0
                else cross_vault_tier(head))
        return (
            tier,
            int(active.get(vault_id, 0)),
            -waited,
            str(head.get("scheduler_last_served_at") or ""),
            str(head.get("requested_at") or ""),
            int(head.get("id") or 0),
            str(vault_id),
        )

    selected: list[Mapping[str, Any]] = []
    while len(selected) < limit and position:
        vault_id = min(position, key=vault_key)
        selected.append(by_vault[vault_id][position[vault_id]])
        position[vault_id] += 1
        active[vault_id] = int(active.get(vault_id, 0)) + 1
        if position[vault_id] >= len(by_vault[vault_id]):
            del position[vault_id]
    return selected


def _job_order(job: Mapping[str, Any]) -> tuple[int, str, int]:
    return (job_priority(job), str(job.get("requested_at") or ""), int(job.get("id") or 0))


def item_vault_id(job: Mapping[str, Any]) -> Any:
    return job.get("vault_id")
