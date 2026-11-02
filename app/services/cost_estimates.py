"""Storage and restore cost estimates from bundled AWS list prices (#413).

FrostVault ships the public AWS list prices of every commercial region in
``aws_list_prices.json`` (regenerate with ``scripts/update_aws_list_prices.py``).
They are not editable at runtime. Estimates use the prices of the configured
``AWS_DEFAULT_REGION`` and always return the publication date and assumptions,
so the UI can show that figures are estimates, not AWS Billing quotes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .restore_estimates import DEFAULT_RESTORE_HOURS, normalize_restore_tier


FALLBACK_REGION = "us-east-1"
DISCLAIMER = "Estimate from AWS list prices; not an AWS Billing quote."
# AWS publishes S3 list prices in USD. FrostVault keeps its historical
# EUR-labelled amounts and uses the USD list figures 1:1.
CURRENCY = "EUR"


@dataclass(frozen=True)
class PriceList:
    """AWS list prices of one region, per GiB-month and per GiB retrieved."""

    region: str
    effective_at: str
    assumptions: dict[str, Any]
    storage_rates: dict[str, float]
    restore_rates: dict[str, dict[str, float]]
    currency: str = CURRENCY


@dataclass(frozen=True)
class CostEstimate:
    size_bytes: int
    storage_class: str
    estimated_cost_eur: float
    pricing_effective_at: str
    assumptions: dict[str, Any]
    kind: str
    region: str
    tier: str | None = None
    estimated_hours: float | None = None
    currency: str = CURRENCY

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "storage_class": self.storage_class,
            "tier": self.tier,
            "estimated_cost_eur": self.estimated_cost_eur,
            "estimated_hours": self.estimated_hours,
            "currency": self.currency,
            "region": self.region,
            "pricing_effective_at": self.pricing_effective_at,
            "assumptions": dict(self.assumptions),
        }


@lru_cache(maxsize=1)
def _bundled_prices() -> dict[str, Any]:
    path = Path(__file__).with_name("aws_list_prices.json")
    return json.loads(path.read_text(encoding="utf-8"))


def available_regions() -> tuple[str, ...]:
    return tuple(sorted(_bundled_prices()["regions"]))


def list_prices(region: str | None = None) -> PriceList:
    """Return the bundled prices of ``region`` (default: the configured one).

    A region missing from the bundle uses the us-east-1 prices; the
    assumptions name both regions so the estimate is not mistaken for exact.
    """
    if region is None:
        from ..config import settings

        region = settings.aws_region
    bundle = _bundled_prices()
    requested = str(region or "").strip().lower()
    priced_region = requested if requested in bundle["regions"] else FALLBACK_REGION
    data = bundle["regions"][priced_region]
    restore_rates = {
        storage_class: {tier: float(rate) for tier, rate in tiers.items()}
        for storage_class, tiers in data["restore"].items()
    }
    for storage_class, rate in data["instant_retrieval"].items():
        restore_rates[storage_class] = {"Instant": float(rate)}
    assumptions: dict[str, Any] = {
        "region": priced_region,
        "unit": (
            "AWS list price per GiB-month for storage; per GiB for Glacier "
            "restore tiers and Instant/IA GET retrieval"
        ),
        "source": "AWS Price List (USD, used 1:1)",
        "disclaimer": DISCLAIMER,
    }
    if priced_region != requested:
        assumptions["requested_region"] = requested
    return PriceList(
        region=priced_region,
        effective_at=str(bundle["publication_date"]),
        assumptions=assumptions,
        storage_rates={key: float(value) for key, value in data["storage"].items()},
        restore_rates=restore_rates,
    )


def estimate_storage_month(
    prices: PriceList,
    *,
    size_bytes: int,
    storage_class: str,
) -> CostEstimate:
    class_key = (storage_class or "STANDARD").upper()
    gib = max(0, int(size_bytes)) / (1024**3)
    rate = float(prices.storage_rates.get(class_key, 0.0))
    return CostEstimate(
        kind="storage_month",
        size_bytes=max(0, int(size_bytes)),
        storage_class=class_key,
        estimated_cost_eur=round(gib * rate, 6),
        pricing_effective_at=prices.effective_at,
        assumptions=dict(prices.assumptions),
        region=prices.region,
        currency=prices.currency,
    )


def estimate_restore_cost(
    prices: PriceList,
    *,
    size_bytes: int,
    storage_class: str,
    tier: str = "Bulk",
    days: int = 3,
) -> CostEstimate:
    class_key = (storage_class or "").upper()
    resolved_tier = normalize_restore_tier(tier, storage_class=class_key)
    gib = max(0, int(size_bytes)) / (1024**3)
    rate = float(prices.restore_rates.get(class_key, {}).get(resolved_tier, 0.0))
    hours = float(DEFAULT_RESTORE_HOURS.get(class_key, {}).get(resolved_tier, 0.0))
    return CostEstimate(
        kind="restore",
        size_bytes=max(0, int(size_bytes)),
        storage_class=class_key,
        tier=resolved_tier,
        estimated_cost_eur=round(gib * rate, 6),
        estimated_hours=hours,
        pricing_effective_at=prices.effective_at,
        region=prices.region,
        assumptions={
            **dict(prices.assumptions),
            "restore_days": max(1, int(days)),
            "restore_object_irreversible": True,
        },
        currency=prices.currency,
    )
