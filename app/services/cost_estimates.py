"""Configurable storage and restore cost estimates (issue #12).

Price books are the public seam for operator-managed rates. Estimates always
return the active book's ``effective_at`` timestamp and assumptions so the UI
can show that figures are internal, not AWS Billing quotes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .restore_estimates import (
    DEFAULT_RESTORE_HOURS,
    DEFAULT_RESTORE_PRICING_EUR_PER_GIB,
    normalize_restore_tier,
)


BUILTIN_EFFECTIVE_AT = "2026-01-01T00:00:00+00:00"
BUILTIN_ASSUMPTIONS = {
    "region": "eu-south-1",
    "unit": "EUR per GiB-month for storage; EUR per GiB restored for Glacier",
    "disclaimer": (
        "Internal estimate from configured price data; not an AWS Billing quote."
    ),
}
# Approximate published list prices used only when no price book is active.
BUILTIN_STORAGE_RATES_EUR_PER_GIB_MONTH = {
    "STANDARD": 0.023,
    "STANDARD_IA": 0.0125,
    "ONEZONE_IA": 0.01,
    "INTELLIGENT_TIERING": 0.023,
    "GLACIER_IR": 0.004,
    "GLACIER": 0.004,
    "DEEP_ARCHIVE": 0.00099,
}


@dataclass(frozen=True)
class PriceBook:
    name: str
    currency: str
    effective_at: str
    assumptions: dict[str, Any]
    storage_rates: dict[str, float]
    restore_rates: dict[str, dict[str, float]]
    id: int | None = None
    updated_at: str | None = None
    is_active: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "currency": self.currency,
            "effective_at": self.effective_at,
            "updated_at": self.updated_at,
            "assumptions": dict(self.assumptions),
            "storage_rates": dict(self.storage_rates),
            "restore_rates": {
                storage_class: dict(tiers)
                for storage_class, tiers in self.restore_rates.items()
            },
            "is_active": self.is_active,
        }


@dataclass(frozen=True)
class CostEstimate:
    size_bytes: int
    storage_class: str
    estimated_cost_eur: float
    pricing_effective_at: str
    assumptions: dict[str, Any]
    kind: str
    tier: str | None = None
    estimated_hours: float | None = None
    currency: str = "EUR"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "storage_class": self.storage_class,
            "tier": self.tier,
            "estimated_cost_eur": self.estimated_cost_eur,
            "estimated_hours": self.estimated_hours,
            "currency": self.currency,
            "pricing_effective_at": self.pricing_effective_at,
            "assumptions": dict(self.assumptions),
        }


def builtin_price_book() -> PriceBook:
    return PriceBook(
        name="builtin-defaults",
        currency="EUR",
        effective_at=BUILTIN_EFFECTIVE_AT,
        assumptions=dict(BUILTIN_ASSUMPTIONS),
        storage_rates=dict(BUILTIN_STORAGE_RATES_EUR_PER_GIB_MONTH),
        restore_rates={
            storage_class: dict(tiers)
            for storage_class, tiers in DEFAULT_RESTORE_PRICING_EUR_PER_GIB.items()
        },
        is_active=True,
    )


def _row_to_book(row: Mapping[str, Any]) -> PriceBook:
    return PriceBook(
        id=int(row["id"]),
        name=row["name"],
        currency=row["currency"],
        effective_at=row["effective_at"],
        updated_at=row["updated_at"],
        assumptions=json.loads(row["assumptions_json"] or "{}"),
        storage_rates={
            key: float(value)
            for key, value in json.loads(row["storage_rates_json"] or "{}").items()
        },
        restore_rates={
            storage_class: {tier: float(rate) for tier, rate in tiers.items()}
            for storage_class, tiers in json.loads(
                row["restore_rates_json"] or "{}"
            ).items()
        },
        is_active=bool(row["is_active"]),
    )


def get_active_price_book(connection: Any) -> PriceBook:
    row = connection.execute(
        """
        SELECT * FROM cost_price_books
        WHERE is_active=TRUE
        ORDER BY effective_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return builtin_price_book()
    return _row_to_book(row)


def list_price_books(connection: Any) -> list[PriceBook]:
    rows = connection.execute(
        """
        SELECT * FROM cost_price_books
        ORDER BY effective_at DESC, id DESC
        """
    ).fetchall()
    return [_row_to_book(row) for row in rows]


def upsert_price_book(connection: Any, book: PriceBook) -> PriceBook:
    updated_at = datetime.now(timezone.utc).isoformat()
    if book.id is None:
        connection.execute(
            """
            INSERT INTO cost_price_books(
                name, currency, effective_at, updated_at,
                assumptions_json, storage_rates_json, restore_rates_json, is_active
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE)
            """,
            (
                book.name,
                book.currency,
                book.effective_at,
                updated_at,
                json.dumps(book.assumptions),
                json.dumps(book.storage_rates),
                json.dumps(book.restore_rates),
            ),
        )
        row = connection.execute(
            "SELECT * FROM cost_price_books ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return _row_to_book(row)
    connection.execute(
        """
        UPDATE cost_price_books
        SET name=%s, currency=%s, effective_at=%s, updated_at=%s,
            assumptions_json=%s, storage_rates_json=%s, restore_rates_json=%s
        WHERE id=%s
        """,
        (
            book.name,
            book.currency,
            book.effective_at,
            updated_at,
            json.dumps(book.assumptions),
            json.dumps(book.storage_rates),
            json.dumps(book.restore_rates),
            book.id,
        ),
    )
    row = connection.execute(
        "SELECT * FROM cost_price_books WHERE id=%s", (book.id,)
    ).fetchone()
    if not row:
        raise LookupError("price_book_not_found")
    return _row_to_book(row)


def activate_price_book(connection: Any, price_book_id: int) -> PriceBook:
    exists = connection.execute(
        "SELECT id FROM cost_price_books WHERE id=%s", (price_book_id,)
    ).fetchone()
    if not exists:
        raise LookupError("price_book_not_found")
    connection.execute("UPDATE cost_price_books SET is_active=FALSE")
    connection.execute(
        "UPDATE cost_price_books SET is_active=TRUE, updated_at=%s WHERE id=%s",
        (datetime.now(timezone.utc).isoformat(), price_book_id),
    )
    return get_active_price_book(connection)


def estimate_storage_month(
    book: PriceBook,
    *,
    size_bytes: int,
    storage_class: str,
) -> CostEstimate:
    class_key = (storage_class or "STANDARD").upper()
    gib = max(0, int(size_bytes)) / (1024**3)
    rate = float(book.storage_rates.get(class_key, 0.0))
    return CostEstimate(
        kind="storage_month",
        size_bytes=max(0, int(size_bytes)),
        storage_class=class_key,
        estimated_cost_eur=round(gib * rate, 6),
        pricing_effective_at=book.effective_at,
        assumptions=dict(book.assumptions),
        currency=book.currency,
    )


def estimate_restore_cost(
    book: PriceBook,
    *,
    size_bytes: int,
    storage_class: str,
    tier: str = "Bulk",
    days: int = 3,
) -> CostEstimate:
    class_key = (storage_class or "").upper()
    resolved_tier = normalize_restore_tier(tier, storage_class=class_key)
    gib = max(0, int(size_bytes)) / (1024**3)
    rate = float(book.restore_rates.get(class_key, {}).get(resolved_tier, 0.0))
    hours = float(DEFAULT_RESTORE_HOURS.get(class_key, {}).get(resolved_tier, 0.0))
    return CostEstimate(
        kind="restore",
        size_bytes=max(0, int(size_bytes)),
        storage_class=class_key,
        tier=resolved_tier,
        estimated_cost_eur=round(gib * rate, 6),
        estimated_hours=hours,
        pricing_effective_at=book.effective_at,
        assumptions={
            **dict(book.assumptions),
            "restore_days": max(1, int(days)),
            "restore_object_irreversible": True,
        },
        currency=book.currency,
    )
