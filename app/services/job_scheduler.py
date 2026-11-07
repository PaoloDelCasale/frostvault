"""Fair Job scheduling across Vaults (issue #12, #500).

This module is the scheduler seam used by the worker. Callers supply the
candidate Jobs already loaded from the database; selection is pure and
deterministic so tests can pin fairness without racing a thread pool.
"""

from __future__ import annotations

from collections import defaultdict, deque
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

    A lower effective tier is always served before a higher one. Inside a tier
    the next Job always goes to the Vault with the fewest Jobs already running
    (``active_by_vault``) plus Jobs picked in this call, ties broken by the
    oldest waiting Job. A busy Vault therefore cannot starve the others even
    when workers free up one at a time. Inside one Vault, the original tier and
    then the oldest ``requested_at`` win. The result is deterministic.
    """
    if limit <= 0 or not jobs:
        return []
    active = dict(active_by_vault or {})
    tiers: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for job in jobs:
        tiers[effective_priority(job, now)].append(job)
    selected: list[Mapping[str, Any]] = []
    for tier in sorted(tiers):
        picked = _interleave_vaults(tiers[tier], limit=limit - len(selected), active=active)
        selected.extend(picked)
        if len(selected) >= limit:
            break
    return selected


def _job_order(job: Mapping[str, Any]) -> tuple[int, str, int]:
    return (job_priority(job), str(job.get("requested_at") or ""), int(job.get("id") or 0))


def _interleave_vaults(
    jobs: Sequence[Mapping[str, Any]],
    *,
    limit: int,
    active: dict[Any, int],
) -> list[Mapping[str, Any]]:
    if limit <= 0 or not jobs:
        return []

    by_vault: dict[Any, deque[Mapping[str, Any]]] = defaultdict(deque)
    for job in sorted(jobs, key=_job_order):
        by_vault[item_vault_id(job)].append(job)

    def vault_key(vault_id: Any) -> tuple[int, str, int, str]:
        head = by_vault[vault_id][0]
        return (
            int(active.get(vault_id, 0)),
            str(head.get("requested_at") or ""),
            int(head.get("id") or 0),
            str(vault_id),
        )

    selected: list[Mapping[str, Any]] = []
    while len(selected) < limit and by_vault:
        vault_id = min(by_vault, key=vault_key)
        selected.append(by_vault[vault_id].popleft())
        # Counted for later tiers too: this Vault now has one more Job running.
        active[vault_id] = int(active.get(vault_id, 0)) + 1
        if not by_vault[vault_id]:
            del by_vault[vault_id]
    return selected


def item_vault_id(job: Mapping[str, Any]) -> Any:
    return job.get("vault_id")
