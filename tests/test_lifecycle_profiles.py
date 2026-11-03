import unittest
from app.services.lifecycle_profiles import (
    GUIDED_PROFILES, LifecycleProfile, LifecycleTransition,
    profile_from_json, profile_to_json, validate_lifecycle_profile,
)


def plan(*steps):
    return LifecycleProfile(tuple(LifecycleTransition(days, cls) for days, cls in steps))


class StoragePlanValidationTests(unittest.TestCase):
    def test_presets_and_roundtrip(self):
        self.assertEqual(set(GUIDED_PROFILES), {"standard_only", "economical", "archive_tiered", "deep_archive_early"})
        for profile in GUIDED_PROFILES.values():
            with self.subTest(profile=profile):
                self.assertTrue(validate_lifecycle_profile(profile).ok)
                self.assertEqual(profile_from_json(profile_to_json(profile)), profile)
        self.assertEqual([(t.days, t.storage_class) for t in GUIDED_PROFILES["economical"].transitions], [(30, "STANDARD_IA"), (90, "GLACIER_IR")])

    def test_standard_can_transition_directly_to_archive_early(self):
        for cls in ["GLACIER_IR", "GLACIER", "DEEP_ARCHIVE"]:
            self.assertTrue(validate_lifecycle_profile(plan((1, cls))).ok)

    def test_source_residence_boundaries(self):
        for cls, start, minimum, target in [
            ("STANDARD_IA", 30, 30, "GLACIER_IR"),
            ("ONEZONE_IA", 30, 30, "DEEP_ARCHIVE"),
            ("GLACIER_IR", 4, 90, "DEEP_ARCHIVE"),
            ("GLACIER", 1, 90, "DEEP_ARCHIVE"),
        ]:
            with self.subTest(cls=cls):
                self.assertFalse(validate_lifecycle_profile(plan((start, cls), (start + minimum - 1, target))).ok)
                self.assertTrue(validate_lifecycle_profile(plan((start, cls), (start + minimum, target))).ok)

    def test_invalid_days_classes_order_and_routes(self):
        cases = [((0, "DEEP_ARCHIVE"),), ((-1, "GLACIER"),), ((1.5, "GLACIER"),), ((True, "GLACIER"),),
                 ((29, "STANDARD_IA"),), ((29, "ONEZONE_IA"),), ((1, "STANDARD"),), ((1, "UNKNOWN"),),
                 ((30, "STANDARD_IA"), (30, "GLACIER_IR")), ((1, "DEEP_ARCHIVE"), (200, "GLACIER")),
                 ((30, "ONEZONE_IA"), (90, "GLACIER_IR")), ((30, "STANDARD_IA"), (60, "STANDARD_IA"))]
        for steps in cases:
            with self.subTest(steps=steps):
                self.assertFalse(validate_lifecycle_profile(plan(*steps)).ok)
        self.assertTrue(validate_lifecycle_profile(plan((30, "STANDARD_IA"), (60, "ONEZONE_IA"), (90, "GLACIER"), (180, "DEEP_ARCHIVE"))).ok)

    def test_no_expiration_or_noncurrent_contract(self):
        for field in ["expiration_days", "noncurrent_expiration_days", "noncurrent_transitions"]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                profile_from_json('{"transitions": [], "' + field + '": []}')
