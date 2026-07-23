"""Fair Job scheduling across Vaults (issue #12).

This module is the scheduler seam used by the worker. Callers supply the
candidate Jobs already loaded from the database; selection is pure and
deterministic so tests can pin fairness without racing a thread pool.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, time, timezone
from typing import Any, Mapping, Sequence


def select_fair_jobs(
    jobs: Sequence[Mapping[str, Any]],
    *,
    limit: int,
) -> list[Mapping[str, Any]]:
    """Interleave Jobs so each Vault gets a turn before any Vault gets a second.

    Within a Vault, older ``requested_at`` values win. Across Vaults, the Vault
    whose next Job is oldest is served first on each round. This keeps the
    schedule deterministic and prevents a busy Vault from starving others when
    the worker concurrency budget is smaller than the queue.
    """
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


def _parse_hhmm(value: str) -> time:
    hour_text, minute_text = value.split(":", 1)
    return time(hour=int(hour_text), minute=int(minute_text))


def job_is_within_operating_window(
    now: datetime,
    windows: Sequence[Mapping[str, Any]],
) -> bool:
    """Return True when no windows are configured or ``now`` falls in one.

    Each window is ``{weekday, start, end}`` in the process local interpretation
    of the provided timezone-aware ``now`` (UTC in the worker). ``weekday`` uses
    Python's Monday=0 convention. An empty window list means always allow.
    """
    if not windows:
        return True
    current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    weekday = current.weekday()
    clock = current.timetz().replace(tzinfo=None)
    for window in windows:
        if int(window["weekday"]) != weekday:
            continue
        start = _parse_hhmm(str(window["start"]))
        end = _parse_hhmm(str(window["end"]))
        if start <= clock < end:
            return True
    return False
