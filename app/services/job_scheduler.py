"""Deterministic, fair selection of runnable Vault turns (ADR-0026)."""
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
    """Base tier: Owner operations, Owner uploads, then automatic work.

    Bulk manual uploads remain manual. Aging never changes this order inside
    a Vault, nor the durable origin of a Job.
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


def select_fair_jobs(
    jobs: Sequence[Mapping[str, Any]],
    *,
    limit: int,
    active_by_vault: Mapping[Any, int] | None = None,
    now: datetime | None = None,
) -> list[Mapping[str, Any]]:
    """Select turns, preserving base-tier/oldest-first order inside each Vault.

    Equal-tier turns prefer fewer live leases, then the longest-unserved turn
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
        tier = effective_priority(head) if position[vault_id] == 0 else job_priority(head)
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
