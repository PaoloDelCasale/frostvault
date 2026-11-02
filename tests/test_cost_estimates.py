"""Cost estimates from bundled AWS list prices (#12, #413).

Seams under test:
- ``app.services.cost_estimates`` — per-region bundled prices, storage and
  restore estimates that expose region, assumptions and the pricing timestamp.
"""

from __future__ import annotations

import unittest

from app.services.cost_estimates import (
    FALLBACK_REGION,
    CostEstimate,
    available_regions,
    estimate_restore_cost,
    estimate_storage_month,
    list_prices,
)
from app.services.storage_classes import MANUAL_STORAGE_CLASSES, list_storage_class_options

REQUIRED_STORAGE_CLASSES = set(MANUAL_STORAGE_CLASSES)


class BundledListPriceTests(unittest.TestCase):
    def test_every_bundled_region_prices_every_class_and_restore_tier(self) -> None:
        self.assertIn("eu-south-1", available_regions())
        self.assertIn(FALLBACK_REGION, available_regions())
        for region in available_regions():
            with self.subTest(region=region):
                prices = list_prices(region)
                self.assertEqual(prices.region, region)
                self.assertTrue(REQUIRED_STORAGE_CLASSES <= set(prices.storage_rates))
                self.assertEqual(
                    set(prices.restore_rates["GLACIER"]),
                    {"Expedited", "Standard", "Bulk"},
                )
                self.assertEqual(
                    set(prices.restore_rates["DEEP_ARCHIVE"]), {"Standard", "Bulk"}
                )
                for storage_class in ("STANDARD_IA", "ONEZONE_IA", "GLACIER_IR"):
                    self.assertIn("Instant", prices.restore_rates[storage_class])
                rates = [
                    *prices.storage_rates.values(),
                    *(
                        rate
                        for tiers in prices.restore_rates.values()
                        for rate in tiers.values()
                    ),
                ]
                self.assertTrue(all(rate >= 0 for rate in rates))
                # Colder classes are never priced above Standard storage.
                self.assertLess(
                    prices.storage_rates["DEEP_ARCHIVE"], prices.storage_rates["STANDARD"]
                )

    def test_regions_have_their_own_list_prices(self) -> None:
        milan = list_prices("eu-south-1")
        virginia = list_prices("us-east-1")
        self.assertEqual(milan.storage_rates["STANDARD"], 0.024)
        self.assertEqual(virginia.storage_rates["STANDARD"], 0.023)
        self.assertEqual(milan.assumptions["region"], "eu-south-1")
        self.assertIn("not an AWS Billing quote", milan.assumptions["disclaimer"])
        self.assertTrue(milan.effective_at)

    def test_unknown_region_falls_back_and_says_so(self) -> None:
        prices = list_prices("xx-nowhere-9")
        self.assertEqual(prices.region, FALLBACK_REGION)
        self.assertEqual(prices.assumptions["region"], FALLBACK_REGION)
        self.assertEqual(prices.assumptions["requested_region"], "xx-nowhere-9")
        self.assertNotIn("requested_region", list_prices("eu-south-1").assumptions)


class StorageEstimateTests(unittest.TestCase):
    def test_monthly_estimate_uses_the_region_rate(self) -> None:
        prices = list_prices("eu-south-1")
        estimate = estimate_storage_month(
            prices, size_bytes=2 * 1024**3, storage_class="standard"
        )
        self.assertIsInstance(estimate, CostEstimate)
        self.assertEqual(estimate.storage_class, "STANDARD")
        self.assertEqual(estimate.estimated_cost_eur, 0.048)
        self.assertEqual(estimate.region, "eu-south-1")
        self.assertEqual(estimate.pricing_effective_at, prices.effective_at)
        self.assertEqual(estimate.assumptions, prices.assumptions)
        payload = estimate.as_dict()
        self.assertEqual(payload["region"], "eu-south-1")
        self.assertNotIn("price_book_id", payload)


class RestoreEstimateTests(unittest.TestCase):
    def test_restore_estimate_exposes_rate_hours_and_assumptions(self) -> None:
        prices = list_prices("eu-south-1")
        estimate = estimate_restore_cost(
            prices,
            size_bytes=1024**3,
            storage_class="DEEP_ARCHIVE",
            tier="Standard",
            days=5,
        )
        self.assertEqual(estimate.estimated_cost_eur, 0.024)
        self.assertEqual(estimate.estimated_hours, 12.0)
        self.assertEqual(estimate.tier, "Standard")
        self.assertEqual(estimate.assumptions["restore_days"], 5)
        self.assertTrue(estimate.assumptions["restore_object_irreversible"])

    def test_unsupported_tier_falls_back_to_bulk(self) -> None:
        estimate = estimate_restore_cost(
            list_prices("eu-south-1"),
            size_bytes=1024**3,
            storage_class="DEEP_ARCHIVE",
            tier="Expedited",
        )
        self.assertEqual(estimate.tier, "Bulk")
        self.assertEqual(estimate.estimated_cost_eur, 0.005)


class StorageClassPickerTests(unittest.TestCase):
    def test_picker_rates_come_from_the_region_list_prices(self) -> None:
        options = list_storage_class_options(list_prices("eu-south-1"))
        by_id = {item["id"]: item for item in options["items"]}
        self.assertEqual(by_id["STANDARD"]["storage_rate_eur_per_gib_month"], 0.024)
        self.assertEqual(by_id["GLACIER_IR"]["retrieval_rate_eur_per_gib"], 0.03)
        self.assertEqual(by_id["DEEP_ARCHIVE"]["restore_rate_eur_per_gib_standard"], 0.024)
        self.assertEqual(options["assumptions"]["region"], "eu-south-1")


if __name__ == "__main__":
    unittest.main()
