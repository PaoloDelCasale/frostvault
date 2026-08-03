from __future__ import annotations

import unittest

from app.services.lifecycle_profiles import (
    GUIDED_PROFILES,
    LifecycleProfile,
    LifecycleTransition,
    validate_lifecycle_profile,
)


class LifecycleProfileValidationTests(unittest.TestCase):
    def test_rejects_non_increasing_transition_days(self) -> None:
        profile = LifecycleProfile(
            transitions=(
                LifecycleTransition(days=90, storage_class="GLACIER_IR"),
                LifecycleTransition(days=60, storage_class="GLACIER"),
            )
        )
        result = validate_lifecycle_profile(profile)
        self.assertFalse(result.ok)
        self.assertIn("increase", result.errors[0].lower())

    def test_rejects_transition_before_minimum_days(self) -> None:
        profile = LifecycleProfile(
            transitions=(LifecycleTransition(days=7, storage_class="STANDARD_IA"),)
        )
        result = validate_lifecycle_profile(profile)
        self.assertFalse(result.ok)
        self.assertIn("30", result.errors[0])

    def test_warns_about_glacier_retrieval_costs(self) -> None:
        profile = LifecycleProfile(
            transitions=(LifecycleTransition(days=90, storage_class="GLACIER"),)
        )
        result = validate_lifecycle_profile(profile)
        self.assertTrue(result.ok)
        self.assertTrue(result.warnings)
        self.assertIn("retrieval", result.warnings[0].lower())

    def test_rejects_same_depth_band_and_accepts_skipped_tiers(self) -> None:
        same_band = LifecycleProfile(
            transitions=(
                LifecycleTransition(days=30, storage_class="STANDARD_IA"),
                LifecycleTransition(days=60, storage_class="ONEZONE_IA"),
            )
        )
        self.assertFalse(validate_lifecycle_profile(same_band).ok)

        skipped = LifecycleProfile(
            transitions=(
                LifecycleTransition(days=30, storage_class="STANDARD_IA"),
                LifecycleTransition(days=180, storage_class="DEEP_ARCHIVE"),
            )
        )
        self.assertTrue(validate_lifecycle_profile(skipped).ok)

    def test_noncurrent_rules_enforce_days_minimum_and_depth(self) -> None:
        profile = LifecycleProfile(
            noncurrent_transitions=(
                LifecycleTransition(days=90, storage_class="GLACIER"),
                LifecycleTransition(days=180, storage_class="GLACIER_IR"),
                LifecycleTransition(days=30, storage_class="STANDARD_IA"),
            )
        )
        result = validate_lifecycle_profile(profile)
        self.assertFalse(result.ok)
        self.assertTrue(any("deepen" in error for error in result.errors))
        self.assertTrue(any("increase" in error for error in result.errors))

    def test_valid_tiered_profile_passes(self) -> None:
        result = validate_lifecycle_profile(GUIDED_PROFILES["archive_tiered"])
        self.assertTrue(result.ok)
