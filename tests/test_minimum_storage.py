"""Minimum Storage Duration and Class Entry (#731), without a database.

The Class Entry is the recorded ``storage_class_since`` or, when none is
recorded, an estimate from ``created_at`` and the Storage Plan (transition days
+ 1), marked as estimated. Objects under 128 KiB never transition. Costs are
list-price estimates of the remaining minimum days.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.services import minimum_storage as ms
from app.services.cost_estimates import list_prices
from app.services.lifecycle_profiles import LifecycleProfile, LifecycleTransition
from app.services.vault_moves import MINIMUM_DAYS

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
UPLOAD = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
DEEP_NOW = LifecycleProfile((LifecycleTransition(0, "DEEP_ARCHIVE"),))
TIERED = LifecycleProfile((LifecycleTransition(30, "STANDARD_IA"), LifecycleTransition(90, "GLACIER_IR"),
                           LifecycleTransition(365, "DEEP_ARCHIVE")))
MIB = 1024 ** 2
GIB = 1024 ** 3


def entry(storage_class: str | None, since: datetime | None = None, *, created: datetime = UPLOAD,
          size: int = MIB, plan: LifecycleProfile = DEEP_NOW, now: datetime = NOW,
          project: bool = True) -> ms.ClassEntry:
    return ms.class_entry(storage_class=storage_class, since=since, created_at=created, size=size,
                          plan=plan, now=now, project=project)


class ClassEntryTests(unittest.TestCase):
    def test_minimum_days_come_from_the_single_source(self) -> None:
        self.assertIs(ms.MINIMUM_DAYS, MINIMUM_DAYS)
        self.assertEqual(entry("DEEP_ARCHIVE").minimum_days, 180)
        self.assertEqual(entry("STANDARD", plan=LifecycleProfile()).minimum_days, 0)

    def test_a_recorded_class_entry_is_used_as_is(self) -> None:
        observed = datetime(2026, 11, 16, 3, 0, tzinfo=timezone.utc)
        result = entry("DEEP_ARCHIVE", observed)
        self.assertEqual((result.storage_class, result.since, result.estimated, result.expected),
                         ("DEEP_ARCHIVE", observed, False, False))
        self.assertEqual(result.minimum_until, observed + timedelta(days=180))

    def test_a_missing_entry_is_estimated_from_the_plan_transition_plus_one_day(self) -> None:
        result = entry("DEEP_ARCHIVE")
        self.assertEqual((result.since, result.estimated), (UPLOAD + timedelta(days=1), True))
        tiered = entry("GLACIER_IR", created=UPLOAD - timedelta(days=200), plan=TIERED)
        self.assertEqual(tiered.since, UPLOAD - timedelta(days=200) + timedelta(days=91))
        self.assertTrue(tiered.estimated)

    def test_a_class_outside_the_plan_was_entered_when_written(self) -> None:
        # A direct PUT or copy (a Vault move) into a class the plan never reaches.
        result = entry("GLACIER", plan=LifecycleProfile())
        self.assertEqual((result.since, result.estimated), (UPLOAD, True))
        # A plan transition still in the future cannot explain an observed class.
        early = entry("DEEP_ARCHIVE", created=NOW - timedelta(days=2), plan=TIERED)
        self.assertEqual(early.since, NOW - timedelta(days=2))

    def test_a_due_transition_not_observed_yet_is_expected_and_estimated(self) -> None:
        result = entry("STANDARD", UPLOAD)
        self.assertEqual((result.storage_class, result.since, result.estimated, result.expected),
                         ("DEEP_ARCHIVE", UPLOAD + timedelta(days=1), True, True))
        # Not yet due: still Standard, recorded.
        fresh = entry("STANDARD", NOW - timedelta(hours=3), created=NOW - timedelta(hours=3))
        self.assertEqual((fresh.storage_class, fresh.estimated), ("STANDARD", False))

    def test_objects_under_128_kib_stay_standard(self) -> None:
        small = entry("STANDARD", UPLOAD, size=ms.SMALL_OBJECT_BYTES - 1)
        self.assertEqual((small.storage_class, small.minimum_days, small.estimated), ("STANDARD", 0, False))
        large = entry("STANDARD", UPLOAD, size=ms.SMALL_OBJECT_BYTES)
        self.assertEqual(large.storage_class, "DEEP_ARCHIVE")

    def test_a_listing_observed_now_is_never_projected(self) -> None:
        self.assertEqual(entry("STANDARD", project=False).storage_class, "STANDARD")

    def test_the_tiered_plan_expects_the_last_due_step_only(self) -> None:
        created = NOW - timedelta(days=100)
        result = entry("STANDARD", created, created=created, plan=TIERED)
        self.assertEqual((result.storage_class, result.since), ("GLACIER_IR", created + timedelta(days=91)))


class CostTests(unittest.TestCase):
    def test_remaining_cost_is_remaining_days_times_size_times_monthly_rate(self) -> None:
        prices = list_prices("eu-south-1")
        result = entry("DEEP_ARCHIVE", NOW - timedelta(days=60))
        rate = prices.storage_rates["DEEP_ARCHIVE"]
        self.assertAlmostEqual(ms.remaining_cost(result, 10 * GIB, NOW, prices), 10 * rate * 120 / 30)
        self.assertEqual(ms.remaining_cost(result, 10 * GIB, NOW + timedelta(days=200), prices), 0)

    def test_deletion_summary_counts_only_versions_still_within_the_minimum(self) -> None:
        prices = list_prices("eu-south-1")
        rows = [
            {"storage_class": "DEEP_ARCHIVE", "storage_class_since": None, "created_at": UPLOAD, "size": 2 * GIB},
            {"storage_class": "DEEP_ARCHIVE", "storage_class_since": NOW - timedelta(days=400),
             "created_at": NOW - timedelta(days=401), "size": GIB},
            {"storage_class": "STANDARD", "storage_class_since": NOW, "created_at": NOW, "size": GIB},
        ]
        at = NOW + timedelta(days=7)
        summary = ms.deletion_summary(rows, plan=DEEP_NOW, at=at, now=NOW, prices=prices)
        self.assertEqual((summary["count"], summary["bytes"], summary["estimated_count"]), (1, 2 * GIB, 1))
        until = UPLOAD + timedelta(days=181)
        self.assertEqual(summary["savings_from"], until.isoformat())
        expected = 2 * prices.storage_rates["DEEP_ARCHIVE"] * (until - at).total_seconds() / 86400 / 30
        self.assertAlmostEqual(summary["remaining_cost"], round(expected, 6))
        self.assertEqual([c["storage_class"] for c in summary["by_storage_class"]], ["DEEP_ARCHIVE"])
        self.assertEqual((summary["currency"], summary["region"]), (prices.currency, prices.region))
        self.assertTrue(summary["disclaimer"])

    def test_beyond_minimum_is_judged_at_the_execution(self) -> None:
        row = {"storage_class": "DEEP_ARCHIVE", "storage_class_since": NOW - timedelta(days=175),
               "created_at": NOW - timedelta(days=176), "size": GIB}
        self.assertFalse(ms.beyond_minimum(row, plan=DEEP_NOW, at=NOW, now=NOW))
        self.assertTrue(ms.beyond_minimum(row, plan=DEEP_NOW, at=NOW + timedelta(days=7), now=NOW))
        standard = {"storage_class": "STANDARD", "storage_class_since": NOW, "created_at": NOW, "size": 10}
        self.assertTrue(ms.beyond_minimum(standard, plan=DEEP_NOW, at=NOW, now=NOW))


class VaultSummaryTests(unittest.TestCase):
    def test_per_class_within_and_beyond_cost_and_month_by_month_release(self) -> None:
        prices = list_prices("eu-south-1")
        rows = [
            # 55,033 versions uploaded on 15/11, in Deep Archive, no recorded entry.
            {"storage_class": "DEEP_ARCHIVE", "storage_class_since": None, "created_at": UPLOAD,
             "object_size": ms.SMALL_OBJECT_BYTES, "count": 55033, "size": 223 * GIB},
            # Older ones beyond the minimum.
            {"storage_class": "DEEP_ARCHIVE", "storage_class_since": NOW - timedelta(days=200),
             "created_at": NOW - timedelta(days=201), "object_size": ms.SMALL_OBJECT_BYTES,
             "count": 10, "size": GIB},
            # Small objects stay Standard; a Standard version just written too.
            {"storage_class": "STANDARD", "storage_class_since": UPLOAD, "created_at": UPLOAD,
             "object_size": 0, "count": 5, "size": 5 * 1024},
            {"storage_class": "STANDARD", "storage_class_since": NOW, "created_at": NOW,
             "object_size": ms.SMALL_OBJECT_BYTES, "count": 2, "size": 2 * MIB},
        ]
        summary = ms.vault_summary(rows, plan=DEEP_NOW, now=NOW, prices=prices)
        classes = {c["storage_class"]: c for c in summary["by_storage_class"]}
        self.assertEqual(set(classes), {"DEEP_ARCHIVE", "STANDARD"})
        deep = classes["DEEP_ARCHIVE"]
        self.assertEqual((deep["within"], deep["beyond"], deep["minimum_days"], deep["estimated_count"]),
                         ({"count": 55033, "bytes": 223 * GIB}, {"count": 10, "bytes": GIB}, 180, 55033))
        self.assertEqual(classes["STANDARD"]["beyond"], {"count": 7, "bytes": 5 * 1024 + 2 * MIB})
        until = UPLOAD + timedelta(days=181)
        expected = 223 * prices.storage_rates["DEEP_ARCHIVE"] * (until - NOW).total_seconds() / 86400 / 30
        self.assertAlmostEqual(summary["remaining_cost"], round(expected, 6))
        self.assertEqual(summary["releases"], [{"month": "2027-05", "count": 55033, "bytes": 223 * GIB}])
        self.assertEqual(summary["estimated_count"], 55033)

    def test_the_observation_sql_moves_the_entry_only_on_a_class_change(self) -> None:
        sql = ms.observed_since_sql("av.storage_class", "%s", "av.storage_class_since", "now()")
        self.assertIn("av.storage_class IS NOT NULL", sql)
        self.assertIn("upper(av.storage_class) <> upper(%s)", sql)
        self.assertTrue(sql.endswith("ELSE av.storage_class_since END"))


if __name__ == "__main__":
    unittest.main()
