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

    def test_valid_tiered_profile_passes(self) -> None:
        result = validate_lifecycle_profile(GUIDED_PROFILES["archive_tiered"])
        self.assertTrue(result.ok)
