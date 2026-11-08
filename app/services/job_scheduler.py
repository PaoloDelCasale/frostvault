"""Fair Job scheduling across Vaults (issue #12, #500).

This module is the scheduler seam used by the worker. Callers supply the
candidate Jobs already loaded from the database; selection is pure and
deterministic so tests can pin fairness without racing a thread pool.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


USER_OPERATION_PRIORITY = 0
MANUAL_UPLOAD_PRIORITY = 1
AUTOMATIC_PRIORITY = 2
# Kept for readers of the earlier name.
AUTOMATIC_UPLOAD_PRIORITY = AUTOMATIC_PRIORITY

# Work that has waited this long moves up one tier, so a huge Owner operation
# (e.g. "Move the whole Vault") cannot stop every upload of every other Vault
# for as long as it runs. Inside a Vault the original tier still orders Jobs,
# so an Owner operation is never queued behind aged uploads of its own Vault.
AGING_SECONDS = 900.0


def job_priority(job: Mapping[str, Any]) -> int:
    """Scheduling tier: lower runs first (ADR-0026, issue #500).

    * 0 - Owner operations: any non-upload Job the Owner (or Stop Protection)
      asked for: restore, free-space, rename, storage-class, cloud purge.
    * 1 - uploads the Owner asked for.
    * 2 - anything the system admitted on its own (automatic uploads and
      automatic free-space cleanups), so it never outranks an Owner upload.
    """
    if job.get("origin") == "automatic":
        return AUTOMATIC_PRIORITY
    if job.get("action") != "upload":
        return USER_OPERATION_PRIORITY
    return MANUAL_UPLOAD_PRIORITY


def _waited_seconds(job: Mapping[str, Any], now: datetime | None) -> float:
    if now is None:
        return 0.0
    try:
        requested = datetime.fromisoformat(str(job.get("requested_at")))
        if requested.tzinfo is None:
            requested = requested.replace(tzinfo=timezone.utc)
        current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        return max(0.0, (current - requested).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def effective_priority(job: Mapping[str, Any], now: datetime | None = None) -> int:
    """``job_priority`` lowered by one tier per ``AGING_SECONDS`` of waiting."""
    base = job_priority(job)
    return max(USER_OPERATION_PRIORITY, base - int(_waited_seconds(job, now) // AGING_SECONDS))


def select_fair_jobs(
    jobs: Sequence[Mapping[str, Any]],
    *,
    limit: int,
    active_by_vault: Mapping[Any, int] | None = None,
    now: datetime | None = None,
) -> list[Mapping[str, Any]]:
    """Order Jobs by tier, then share the workers fairly between Vaults.

    Inside one Vault Jobs always run in their original tier order (Owner
    operations, then Owner uploads, then automatic work), oldest first: aging
    never lets an automatic upload overtake an Owner request of its own Vault.
    Across Vaults, each Vault competes with its best *effective* tier, i.e. the
    lowest ``effective_priority`` among its waiting Jobs, so aging promotes a
    Vault's turn and a huge Owner operation elsewhere cannot starve it. Among
    Vaults on the same tier the next Job goes to the Vault with the fewest Jobs
    already running (``active_by_vault``) plus Jobs picked in this call, ties
    broken by the oldest waiting Job. A busy Vault therefore cannot starve the
    others even when workers free up one at a time. The result is deterministic.
    """
    if limit <= 0 or not jobs:
        return []
    active = dict(active_by_vault or {})
    by_vault: dict[Any, list[Mapping[str, Any]]] = defaultdict(list)
    for job in sorted(jobs, key=_job_order):
        by_vault[item_vault_id(job)].append(job)
    # best[v][i]: the best effective tier among the Jobs of Vault v from index i on.
    best: dict[Any, list[int]] = {}
    for vault_id, queue in by_vault.items():
        suffix = [0] * len(queue)
        running = None
        for index in range(len(queue) - 1, -1, -1):
            tier = effective_priority(queue[index], now)
            running = tier if running is None else min(running, tier)
            suffix[index] = running
        best[vault_id] = suffix
    position = {vault_id: 0 for vault_id in by_vault}

    def vault_key(vault_id: Any) -> tuple[int, int, str, int, str]:
        index = position[vault_id]
        head = by_vault[vault_id][index]
        return (
            best[vault_id][index],
            int(active.get(vault_id, 0)),
            str(head.get("requested_at") or ""),
            int(head.get("id") or 0),
            str(vault_id),
        )

    selected: list[Mapping[str, Any]] = []
    while len(selected) < limit and position:
        vault_id = min(position, key=vault_key)
        selected.append(by_vault[vault_id][position[vault_id]])
        position[vault_id] += 1
        # This Vault now has one more Job running.
        active[vault_id] = int(active.get(vault_id, 0)) + 1
        if position[vault_id] >= len(by_vault[vault_id]):
            del position[vault_id]
    return selected


def _job_order(job: Mapping[str, Any]) -> tuple[int, str, int]:
    return (job_priority(job), str(job.get("requested_at") or ""), int(job.get("id") or 0))


def item_vault_id(job: Mapping[str, Any]) -> Any:
    return job.get("vault_id")
