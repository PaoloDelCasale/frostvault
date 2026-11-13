"""Read-only Owner overview. No cloud requests, filesystem walks or Session writes."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .cost_estimates import estimate_storage_month, list_prices
from . import notifications, cloud_preflight_gate


def overview(connection: Any, *, locale: str | None = None) -> dict[str, Any]:
    notifications.reconcile_problem_states(connection)
    prices = list_prices()
    rows = connection.execute(
        "SELECT id, uuid::text AS uuid, name, lifecycle FROM vaults ORDER BY lower(name), id"
    ).fetchall()
    costs = connection.execute(
        # Every stored version costs, including a replacement in progress and
        # superseded versions awaiting exact cleanup (#591).
        "SELECT f.vault_id, av.storage_class, sum(av.size) AS bytes FROM archive_versions av "
        "JOIN files f ON f.id=av.file_id WHERE av.availability='available' "
        "GROUP BY f.vault_id, av.storage_class"
    ).fetchall()
    totals: dict[int, float] = {}
    for row in costs:
        cost = estimate_storage_month(prices, size_bytes=int(row["bytes"] or 0),
                                      storage_class=row["storage_class"]).estimated_cost_eur
        totals[row["vault_id"]] = totals.get(row["vault_id"], 0) + cost
    problems = connection.execute(
        "SELECT vault_id, count(*) AS total FROM notifications "
        "WHERE problem_status='open' AND in_app_enabled GROUP BY vault_id",
    ).fetchall()
    counts = {row["vault_id"]: int(row["total"]) for row in problems}
    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_start = (month_start - timedelta(days=1)).replace(day=1)
    previous = connection.execute(
        "SELECT message_params FROM notifications WHERE event='weekly_summary' "
        "AND created_at >= %s AND created_at < %s ORDER BY created_at DESC LIMIT 1",
        (previous_start, month_start),
    ).fetchone()
    baseline = None
    if previous:
        params = previous["message_params"]
        if isinstance(params, str):
            params = json.loads(params)
        baseline = params.get("monthly_cost_estimate")
    # Metadata backup runs are rebuilt with backup/restore (#598).
    backup = None
    total = sum(totals.values())
    return {
        "vaults": [{**row, "monthly_estimate": round(totals.get(row["id"], 0), 6),
                    "open_problem_count": counts.get(row["id"], 0)} for row in rows],
        "open_problem_count": sum(counts.values()), "month": month,
        "monthly_estimate": round(total, 6),
        "previous_month_delta": round(total - float(baseline), 6) if baseline is not None else None,
        "currency": prices.currency, "pricing_effective_at": prices.effective_at,
        "email_configured": notifications.email_configured(),
        "cloud_preflight": cloud_preflight_gate.localized_snapshot(
            cloud_preflight_gate.snapshot(connection), locale),
        "last_verified_backup": backup["verified_at"] if backup else None,
    }
