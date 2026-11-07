"""Fair Job scheduling across Vaults (issue #12).

This module is the scheduler seam used by the worker. Callers supply the
candidate Jobs already loaded from the database; selection is pure and
deterministic so tests can pin fairness without racing a thread pool.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Mapping, Sequence


USER_OPERATION_PRIORITY = 0
MANUAL_UPLOAD_PRIORITY = 1
AUTOMATIC_UPLOAD_PRIORITY = 2


def job_priority(job: Mapping[str, Any]) -> int:
    """Scheduling tier: lower runs first (ADR-0026, issue #500).

    Everything that is not an upload (restore, free-space, rename, storage-class,
    cloud purge) outranks uploads, so a restore never waits behind a burst of
    thousands of small automatic uploads. An upload the Owner asked for outranks
    one the watcher admitted on its own.
    """
    if job.get("action") != "upload":
        return USER_OPERATION_PRIORITY
    if job.get("origin") == "automatic":
        return AUTOMATIC_UPLOAD_PRIORITY
    return MANUAL_UPLOAD_PRIORITY


def select_fair_jobs(
    jobs: Sequence[Mapping[str, Any]],
    *,
    limit: int,
) -> list[Mapping[str, Any]]:
    """Order Jobs by tier, then interleave Vaults inside each tier.

    A lower ``job_priority`` tier is always served before a higher one. Inside a
    tier each Vault gets a turn before any Vault gets a second; within a Vault,
    older ``requested_at`` values win. Across Vaults, the Vault whose next Job
    is oldest is served first on each round. This keeps the schedule
    deterministic and prevents a busy Vault from starving others when the
    worker concurrency budget is smaller than the queue.
    """
    if limit <= 0 or not jobs:
        return []
    tiers: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for job in jobs:
        tiers[job_priority(job)].append(job)
    selected: list[Mapping[str, Any]] = []
    for tier in sorted(tiers):
        selected.extend(
            _interleave_vaults(tiers[tier], limit=limit - len(selected))
        )
        if len(selected) >= limit:
            break
    return selected


def _interleave_vaults(
    jobs: Sequence[Mapping[str, Any]],
    *,
    limit: int,
) -> list[Mapping[str, Any]]:
    if limit <= 0 or not jobs:
        return []

    by_vault: dict[Any, deque[Mapping[str, Any]]] = defaultdict(deque)
    for job in sorted(
        jobs,
        key=lambda item: (
            str(item.get("requested_at") or ""),
            int(item.get("id") or 0),
        ),
    ):
        by_vault[item_vault_id(job)].append(job)

    # Stable Vault order for the first round: oldest pending Job first.
    vault_order = sorted(
        by_vault.keys(),
        key=lambda vault_id: (
            str(by_vault[vault_id][0].get("requested_at") or ""),
            int(by_vault[vault_id][0].get("id") or 0),
            vault_id,
        ),
    )

    selected: list[Mapping[str, Any]] = []
    while len(selected) < limit and by_vault:
        progressed = False
        next_order: list[Any] = []
        for vault_id in vault_order:
            queue = by_vault.get(vault_id)
            if not queue:
                by_vault.pop(vault_id, None)
                continue
            selected.append(queue.popleft())
            progressed = True
            if queue:
                next_order.append(vault_id)
            else:
                by_vault.pop(vault_id, None)
            if len(selected) >= limit:
                break
        if not progressed:
            break
        vault_order = next_order or sorted(
            by_vault.keys(),
            key=lambda vault_id: (
                str(by_vault[vault_id][0].get("requested_at") or ""),
                int(by_vault[vault_id][0].get("id") or 0),
                vault_id,
            ),
        )
    return selected


def item_vault_id(job: Mapping[str, Any]) -> Any:
    return job.get("vault_id")
