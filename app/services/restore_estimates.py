"""Restore cost and time estimates for Glacier workflows (issue #4)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


DEFAULT_RESTORE_PRICING_EUR_PER_GIB: dict[str, dict[str, float]] = {
    "GLACIER": {"Expedited": 0.03, "Standard": 0.01, "Bulk": 0.0025},
    "DEEP_ARCHIVE": {"Standard": 0.02, "Bulk": 0.0025},
}

DEFAULT_RESTORE_HOURS: dict[str, dict[str, float]] = {
    "GLACIER": {"Expedited": 5 / 60, "Standard": 5.0, "Bulk": 12.0},
    "DEEP_ARCHIVE": {"Standard": 12.0, "Bulk": 48.0},
}

SUPPORTED_RESTORE_TIERS = ("Expedited", "Standard", "Bulk")


@dataclass(frozen=True)
class RestoreEstimate:
    size_bytes: int
    storage_class: str
    tier: str
    days: int
    estimated_cost_eur: float
    estimated_hours: float
    restore_object_irreversible: bool = True
    pricing_note: str = (
        "Internal estimate from configured price data; not an AWS Billing quote."
    )


def normalize_restore_tier(tier: str | None, *, storage_class: str) -> str:
    candidate = (tier or "Bulk").strip()
    class_key = (storage_class or "").upper()
    allowed = DEFAULT_RESTORE_PRICING_EUR_PER_GIB.get(class_key, {})
    if candidate not in allowed:
        if "Bulk" in allowed:
            return "Bulk"
        if allowed:
            return next(iter(allowed))
        return "Bulk"
    return candidate


def estimate_restore(
    *,
    size_bytes: int,
    storage_class: str,
    tier: str = "Bulk",
    days: int = 3,
    pricing: Mapping[str, Mapping[str, float]] | None = None,
    hours: Mapping[str, Mapping[str, float]] | None = None,
) -> RestoreEstimate:
    class_key = (storage_class or "").upper()
    price_table = pricing or DEFAULT_RESTORE_PRICING_EUR_PER_GIB
    hour_table = hours or DEFAULT_RESTORE_HOURS
    resolved_tier = normalize_restore_tier(tier, storage_class=class_key)
    gib = max(0, int(size_bytes)) / (1024**3)
    rate = float(price_table.get(class_key, {}).get(resolved_tier, 0.0))
    estimated_hours = float(hour_table.get(class_key, {}).get(resolved_tier, 0.0))
    return RestoreEstimate(
        size_bytes=max(0, int(size_bytes)),
        storage_class=class_key,
        tier=resolved_tier,
        days=max(1, int(days)),
        estimated_cost_eur=round(gib * rate, 6),
        estimated_hours=estimated_hours,
    )


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
