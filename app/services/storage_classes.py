"""Internal class-copy Job validation, retained for Vault-wide movement (#429).

There is no public file/folder class-change operation or class picker catalog.
"""
from __future__ import annotations

from .lifecycle_profiles import COST_WARNING_CLASSES, SUPPORTED_STORAGE_CLASSES

COPY_STORAGE_CLASSES = ("STANDARD",) + SUPPORTED_STORAGE_CLASSES


def normalize_storage_class(value: str | None) -> str:
    return (value or "STANDARD").upper()


def validate_manual_target_class(target: str) -> str:
    # Existing queued Jobs remain resumable, including restores to STANDARD.
    normalized = normalize_storage_class(target)
    if normalized not in COPY_STORAGE_CLASSES:
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


def source_requires_restore_for_class_change(storage_class: str | None, *, restore_state: str | None = None) -> bool:
    """True when the internal CopyObject Job needs a temporary restore first."""
    return normalize_storage_class(storage_class) in {"GLACIER", "DEEP_ARCHIVE"} and restore_state != "available"
