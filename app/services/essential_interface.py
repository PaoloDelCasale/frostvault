"""Read-only Owner overview. No cloud requests, filesystem walks or Session writes."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .cost_estimates import estimate_storage_month, list_prices
from . import notifications, cloud_preflight_gate


def overview(connection: Any, *, user_id: int) -> dict[str, Any]:
    notifications.reconcile_problem_states(connection)
    prices = list_prices()
    rows = connection.execute(
        "SELECT id, name, slug, decommission_state FROM vaults ORDER BY lower(name), id"
    ).fetchall()
    costs = connection.execute(
        "SELECT vf.vault_id, av.storage_class, sum(av.size) AS bytes FROM archive_versions av "
        "JOIN vault_files vf ON vf.id=av.vault_file_id WHERE av.availability='available' "
        "GROUP BY vf.vault_id, av.storage_class"
    ).fetchall()
    totals: dict[int, float] = {}
    for row in costs:
        cost = estimate_storage_month(prices, size_bytes=int(row["bytes"] or 0),
                                      storage_class=row["storage_class"]).estimated_cost_eur
        totals[row["vault_id"]] = totals.get(row["vault_id"], 0) + cost
    problems = connection.execute(
        "SELECT vault_id, count(*) AS total FROM notifications WHERE user_id=%s "
        "AND problem_status='open' AND in_app_enabled=TRUE GROUP BY vault_id", (user_id,),
    ).fetchall()
    counts = {row["vault_id"]: int(row["total"]) for row in problems}
    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    previous_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    previous = connection.execute(
        "SELECT message_params FROM notifications WHERE user_id=%s AND event='weekly_summary' "
        "AND substring(created_at,1,7)=%s ORDER BY created_at DESC LIMIT 1", (user_id, previous_month),
    ).fetchone()
    baseline = None
    if previous:
        params = previous["message_params"]
        if isinstance(params, str):
            params = json.loads(params)
        baseline = params.get("monthly_cost_estimate")
    backup = connection.execute(
        "SELECT max(verified_at) AS verified_at FROM metadata_backup_runs WHERE verified_at IS NOT NULL"
    ).fetchone()
    total = sum(totals.values())
    return {
        "vaults": [{**row, "monthly_estimate": round(totals.get(row["id"], 0), 6),
                    "open_problem_count": counts.get(row["id"], 0)} for row in rows],
        "open_problem_count": sum(counts.values()), "month": month,
        "monthly_estimate": round(total, 6),
        "previous_month_delta": round(total - float(baseline), 6) if baseline is not None else None,
        "currency": prices.currency, "pricing_effective_at": prices.effective_at,
        "email_configured": notifications.email_configured(),
        "cloud_preflight": cloud_preflight_gate.snapshot(connection),
        "last_verified_backup": backup["verified_at"] if backup else None,
    }
