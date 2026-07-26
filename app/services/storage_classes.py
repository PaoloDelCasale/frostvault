"""Manual Archive Version storage-class transitions (issue #110)."""
from __future__ import annotations

from .lifecycle_profiles import COST_WARNING_CLASSES, SUPPORTED_STORAGE_CLASSES

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
