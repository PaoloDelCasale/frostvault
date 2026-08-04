"""Configurable, timestamped cost estimates (issue #12).

Seams under test:
- ``app.services.cost_estimates`` — active price book CRUD, storage estimates,
  and restore estimates that expose assumptions plus the pricing timestamp.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.database import SQLiteConnection
from app.services.cost_estimates import (
    CostEstimate,
    PriceBook,
    activate_price_book,
    estimate_storage_month,
    get_active_price_book,
    list_price_books,
    upsert_price_book,
)
from tests.test_database import run_alembic


class PriceBookDefaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "pricing.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_active_book_uses_builtin_defaults_with_timestamp(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            book = get_active_price_book(connection)
        self.assertEqual(book.currency, "EUR")
        self.assertTrue(book.effective_at)
        self.assertIn("not an AWS Billing quote", book.assumptions["disclaimer"])
        estimate = estimate_storage_month(
            book,
            size_bytes=2 * (1024**3),
            storage_class="STANDARD",
        )
        self.assertIsInstance(estimate, CostEstimate)
        self.assertEqual(estimate.estimated_cost_eur, 0.046)
        self.assertEqual(estimate.pricing_effective_at, book.effective_at)
        self.assertIsNone(estimate.price_book_id)
        self.assertEqual(estimate.price_book_name, "builtin-defaults")
        self.assertEqual(estimate.assumptions, book.assumptions)


class PriceBookPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "pricing.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_activated_book_is_used_for_estimates(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            created = upsert_price_book(
                connection,
                PriceBook(
                    name="eu-south-1 2026-07",
                    currency="EUR",
                    effective_at="2026-07-01T00:00:00+00:00",
                    assumptions={
                        "region": "eu-south-1",
                        "disclaimer": "Internal estimate from configured price data.",
                    },
                    storage_rates={"STANDARD": 0.01, "GLACIER": 0.004},
                    restore_rates={
                        "GLACIER": {"Bulk": 0.0025, "Standard": 0.01},
                    },
                ),
            )
            activate_price_book(connection, created.id)
            active = get_active_price_book(connection)
            books = list_price_books(connection)
        self.assertEqual(active.id, created.id)
        self.assertEqual(active.name, "eu-south-1 2026-07")
        self.assertTrue(any(item.is_active for item in books))
        estimate = estimate_storage_month(
            active,
            size_bytes=1024**3,
            storage_class="STANDARD",
        )
        self.assertEqual(estimate.estimated_cost_eur, 0.01)
        self.assertEqual(estimate.price_book_id, created.id)
        self.assertEqual(estimate.price_book_name, "eu-south-1 2026-07")
        self.assertEqual(estimate.pricing_effective_at, "2026-07-01T00:00:00+00:00")


class RestoreEstimateFromPriceBookTests(unittest.TestCase):
    def test_restore_estimate_exposes_assumptions_and_timestamp(self) -> None:
        from app.services.cost_estimates import estimate_restore_cost

        book = PriceBook(
            name="builtin",
            currency="EUR",
            effective_at="2026-06-01T00:00:00+00:00",
            assumptions={"disclaimer": "Internal estimate."},
            storage_rates={},
            restore_rates={
                "GLACIER": {"Bulk": 0.0025, "Standard": 0.01, "Expedited": 0.03},
            },
        )
        estimate = estimate_restore_cost(
            book,
            size_bytes=1024**3,
            storage_class="GLACIER",
            tier="Bulk",
        )
        self.assertEqual(estimate.estimated_cost_eur, 0.0025)
        self.assertIsNone(estimate.price_book_id)
        self.assertEqual(estimate.price_book_name, "builtin")
        self.assertEqual(estimate.pricing_effective_at, "2026-06-01T00:00:00+00:00")
        self.assertEqual(estimate.assumptions["disclaimer"], "Internal estimate.")


if __name__ == "__main__":
    unittest.main()
