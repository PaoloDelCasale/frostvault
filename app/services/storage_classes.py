"""Manual Archive Version storage-class transitions (issue #110)."""
from __future__ import annotations

from typing import Any, Mapping

from .cost_estimates import PriceBook, builtin_price_book
from .lifecycle_profiles import COST_WARNING_CLASSES, SUPPORTED_STORAGE_CLASSES
from .restore_estimates import (
    DEFAULT_INSTANT_RETRIEVAL_EUR_PER_GIB,
    DEFAULT_RESTORE_HOURS,
)

# Manual Jobs may also warm back to STANDARD; lifecycle never uses STANDARD as a target.
MANUAL_STORAGE_CLASSES = ("STANDARD",) + SUPPORTED_STORAGE_CLASSES

# Deeper = colder. STANDARD_IA and ONEZONE_IA share a depth band (#111).
STORAGE_CLASS_DEPTH: dict[str, int] = {
    "STANDARD": 0,
    "STANDARD_IA": 1,
    "ONEZONE_IA": 1,
    "GLACIER_IR": 2,
    "GLACIER": 3,
    "DEEP_ARCHIVE": 4,
}

# Operator-facing traits for the class picker (AWS-aligned product defaults).
STORAGE_CLASS_TRAITS: dict[str, dict[str, Any]] = {
    "STANDARD": {
        "retrieval": "instant",
        "min_duration_days": 0,
        "requires_restore": False,
        "availability_zones": "multi",
    },
    "STANDARD_IA": {
        "retrieval": "instant",
        "min_duration_days": 30,
        "requires_restore": False,
        "availability_zones": "multi",
    },
    "ONEZONE_IA": {
        "retrieval": "instant",
        "min_duration_days": 30,
        "requires_restore": False,
        "availability_zones": "single",
    },
    "GLACIER_IR": {
        "retrieval": "instant",
        "min_duration_days": 90,
        "requires_restore": False,
        "availability_zones": "multi",
    },
    "GLACIER": {
        "retrieval": "restore",
        "min_duration_days": 90,
        "requires_restore": True,
        "availability_zones": "multi",
    },
    "DEEP_ARCHIVE": {
        "retrieval": "restore",
        "min_duration_days": 180,
        "requires_restore": True,
        "availability_zones": "multi",
    },
}


def normalize_storage_class(value: str | None) -> str:
    return (value or "STANDARD").upper()


def storage_class_depth(storage_class: str | None) -> int:
    return STORAGE_CLASS_DEPTH.get(normalize_storage_class(storage_class), -1)


def is_deeper_storage_class(target: str, current: str) -> bool:
    return storage_class_depth(target) > storage_class_depth(current)


def is_shallower_storage_class(target: str, current: str) -> bool:
    return storage_class_depth(target) < storage_class_depth(current)


def lifecycle_may_apply_transition(*, current_class: str, target_class: str) -> bool:
    """Automatic policy may only deepen; never warm (issue #110 / #111)."""
    return is_deeper_storage_class(target_class, current_class)


def validate_manual_target_class(target: str) -> str:
    normalized = normalize_storage_class(target)
    if normalized not in MANUAL_STORAGE_CLASSES:
        raise ValueError(f"Unsupported storage class: {normalized}")
    return normalized


def cold_class_warning(target: str) -> str | None:
    normalized = normalize_storage_class(target)
    if normalized in COST_WARNING_CLASSES:
        return (
            f"Transition to {normalized} incurs retrieval charges and minimum "
            "storage-duration billing even if objects are deleted early"
        )
    return None


def list_storage_class_options(
    book: PriceBook | None = None,
) -> dict[str, Any]:
    """Public picker catalog: rates from the active price book + retrieval traits."""
    price_book = book or builtin_price_book()
    items: list[dict[str, Any]] = []
    for class_id in MANUAL_STORAGE_CLASSES:
        traits = STORAGE_CLASS_TRAITS[class_id]
        restore_rates = price_book.restore_rates.get(class_id, {})
        restore_hours = DEFAULT_RESTORE_HOURS.get(class_id, {})
        item: dict[str, Any] = {
            "id": class_id,
            "currency": price_book.currency,
            "storage_rate_eur_per_gib_month": float(
                price_book.storage_rates.get(class_id, 0.0)
            ),
            "retrieval": traits["retrieval"],
            "min_duration_days": int(traits["min_duration_days"]),
            "requires_restore": bool(traits["requires_restore"]),
            "availability_zones": traits["availability_zones"],
        }
        if traits["requires_restore"]:
            item["restore_hours_bulk"] = float(restore_hours.get("Bulk", 0.0))
            item["restore_hours_standard"] = float(
                restore_hours.get("Standard", 0.0)
            )
            item["restore_rate_eur_per_gib_bulk"] = float(
                restore_rates.get("Bulk", 0.0)
            )
            item["restore_rate_eur_per_gib_standard"] = float(
                restore_rates.get("Standard", 0.0)
            )
        else:
            instant_rate = restore_rates.get("Instant")
            if instant_rate is None:
                instant_rate = DEFAULT_INSTANT_RETRIEVAL_EUR_PER_GIB.get(class_id)
            if instant_rate is not None and float(instant_rate) > 0:
                item["retrieval_rate_eur_per_gib"] = float(instant_rate)
        items.append(item)
    return {
        "items": items,
        "pricing_effective_at": price_book.effective_at,
        "assumptions": dict(price_book.assumptions),
        "currency": price_book.currency,
    }


def source_requires_restore_for_class_change(
    storage_class: str | None,
    *,
    restore_state: str | None = None,
) -> bool:
    """True when CopyObject needs a temporary Glacier restore first."""
    normalized = normalize_storage_class(storage_class)
    if normalized not in {"GLACIER", "DEEP_ARCHIVE"}:
        return False
    return restore_state != "available"
