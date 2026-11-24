"""Restore tiers and times for Glacier workflows (issue #4).

The restore estimate with list prices (``estimate_restore``) lives with the
prices in ``cost_estimates`` (#858).
"""

from __future__ import annotations


DEFAULT_RESTORE_HOURS: dict[str, dict[str, float]] = {
    "GLACIER": {"Expedited": 5 / 60, "Standard": 5.0, "Bulk": 12.0},
    "DEEP_ARCHIVE": {"Standard": 12.0, "Bulk": 48.0},
}

SUPPORTED_RESTORE_TIERS = ("Expedited", "Standard", "Bulk")


def normalize_restore_tier(tier: str | None, *, storage_class: str) -> str:
    candidate = (tier or "Bulk").strip()
    class_key = (storage_class or "").upper()
    allowed = DEFAULT_RESTORE_HOURS.get(class_key, {})
    if candidate not in allowed:
        if "Bulk" in allowed:
            return "Bulk"
        if allowed:
            return next(iter(allowed))
        return "Bulk"
    return candidate


def is_high_impact_restore(
    *,
    size_bytes: int,
    estimated_cost_eur: float,
    size_threshold_gib: float = 100,
    cost_threshold_eur: float = 10.0,
) -> bool:
    size_gib = max(0, int(size_bytes)) / (1024**3)
    return (
        size_gib >= float(size_threshold_gib)
        or float(estimated_cost_eur) >= float(cost_threshold_eur)
    )
