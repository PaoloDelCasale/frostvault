"""Storage class names as the catalog compares them."""
from __future__ import annotations


def normalize_storage_class(value: str | None) -> str:
    return (value or "STANDARD").upper()
