"""Source confirmation follows registered coverage, not just the desired plan."""
import unittest

from app.services import watch_plan


class SourceConfirmationCoverageTests(unittest.TestCase):
    vault_id = 506

    def setUp(self):
        watch_plan.forget(self.vault_id)
        self.addCleanup(watch_plan.forget, self.vault_id)

    def gaps(self, *directories):
        paths = frozenset(directories)
        return watch_plan.UnobservedPaths(unreadable=paths, directories=paths)

    def ready(self):
        return watch_plan.source_confirmation_ready(self.vault_id)

    def test_scans_without_a_watcher_keep_their_existing_semantics(self):
        watch_plan.record_full_scan(self.vault_id, self.gaps("denied"))
        watch_plan.record_full_scan(self.vault_id, self.gaps())
        self.assertTrue(self.ready())

    def test_a_repaired_or_removed_directory_needs_registered_coverage(self):
        gaps = self.gaps("denied")
        watch_plan.record_full_scan(self.vault_id, gaps)
        watch_plan.record_registration(self.vault_id, gaps)
        self.assertTrue(self.ready())  # The incomplete walk still blocks health.
        watch_plan.record_full_scan(self.vault_id, self.gaps())
        self.assertFalse(self.ready())
        watch_plan.record_registration(self.vault_id, self.gaps())
        self.assertTrue(self.ready())

    def test_partial_repair_requires_coverage_even_with_other_gaps_remaining(self):
        gaps = self.gaps("a", "b")
        watch_plan.record_full_scan(self.vault_id, gaps)
        watch_plan.record_registration(self.vault_id, gaps)
        watch_plan.record_subtree_scan(self.vault_id, "a", self.gaps())
        self.assertFalse(self.ready())
        watch_plan.record_registration(self.vault_id, self.gaps("b"))
        self.assertTrue(self.ready())
        self.assertEqual(watch_plan.unobserved(self.vault_id).directories, {"b"})

    def test_repair_revealing_a_nested_gap_still_needs_a_handoff(self):
        watch_plan.record_registration(self.vault_id, self.gaps("parent"))
        watch_plan.record_full_scan(self.vault_id, self.gaps("parent/child"))
        self.assertFalse(self.ready())
        watch_plan.record_registration(self.vault_id, self.gaps("parent/child"))
        self.assertTrue(self.ready())

    def test_a_scan_started_before_the_handoff_cannot_confirm_its_coverage(self):
        watch_plan.record_registration(self.vault_id, self.gaps("repaired"))
        before_scan = watch_plan.scan_coverage_fence(self.vault_id)
        watch_plan.record_full_scan(self.vault_id, self.gaps())
        watch_plan.record_registration(self.vault_id, self.gaps())
        self.assertFalse(watch_plan.source_confirmation_ready(self.vault_id, before_scan))
        after_registration = watch_plan.scan_coverage_fence(self.vault_id)
        self.assertTrue(watch_plan.source_confirmation_ready(self.vault_id, after_registration))

    def test_manual_scan_cannot_erase_an_unconsumed_gap(self):
        watch_plan.record_registration(self.vault_id, self.gaps())
        watch_plan.record_full_scan(self.vault_id, self.gaps("denied"))
        before_scan = watch_plan.scan_coverage_fence(self.vault_id)
        watch_plan.record_full_scan(self.vault_id, self.gaps())
        self.assertFalse(self.ready())
        self.assertEqual(watch_plan.coverage_debt(self.vault_id), {"denied"})
        plan, debt = watch_plan.registration_plan(self.vault_id)
        self.assertEqual(plan.directories, set())
        self.assertEqual(watch_plan.record_registration(self.vault_id, plan, debt=debt), {"denied"})
        self.assertFalse(watch_plan.source_confirmation_ready(self.vault_id, before_scan))
        self.assertTrue(self.ready())  # A fresh paired scan may now confirm.
        self.assertEqual(watch_plan.coverage_debt(self.vault_id), set())

    def test_scan_started_before_debt_discovery_cannot_confirm_later_handoff(self):
        watch_plan.record_registration(self.vault_id, self.gaps())
        before_scan = watch_plan.scan_coverage_fence(self.vault_id)
        watch_plan.record_subtree_scan(self.vault_id, "denied", self.gaps("denied"))
        watch_plan.record_subtree_scan(self.vault_id, "denied", self.gaps())
        plan, debt = watch_plan.registration_plan(self.vault_id)
        watch_plan.record_registration(self.vault_id, plan, debt=debt)
        self.assertFalse(watch_plan.source_confirmation_ready(self.vault_id, before_scan))
        self.assertTrue(watch_plan.source_confirmation_ready(
            self.vault_id, watch_plan.scan_coverage_fence(self.vault_id),
        ))

    def test_subtree_repair_cannot_erase_an_unconsumed_gap(self):
        watch_plan.record_registration(self.vault_id, self.gaps())
        watch_plan.record_subtree_scan(self.vault_id, "parent", self.gaps("parent/denied"))
        watch_plan.record_subtree_scan(self.vault_id, "parent", self.gaps())
        self.assertFalse(self.ready())
        self.assertEqual(watch_plan.coverage_debt(self.vault_id), {"parent/denied"})

    def test_registration_preserves_debt_discovered_during_its_construction(self):
        watch_plan.record_registration(self.vault_id, self.gaps())
        watch_plan.record_full_scan(self.vault_id, self.gaps("first"))
        plan, debt = watch_plan.registration_plan(self.vault_id)
        watch_plan.record_subtree_scan(self.vault_id, "new", self.gaps("new/denied"))
        settled = watch_plan.record_registration(self.vault_id, plan, debt=debt)
        self.assertEqual(settled, {"first"})
        self.assertEqual(watch_plan.coverage_debt(self.vault_id), {"new/denied"})
        # An ordinary tick of the same observer must not acknowledge newer debt.
        self.assertEqual(watch_plan.record_registration(self.vault_id, plan, debt=debt), set())
        self.assertEqual(watch_plan.coverage_debt(self.vault_id), {"new/denied"})

    def test_registration_cannot_clear_a_new_generation_of_the_same_path(self):
        watch_plan.record_registration(self.vault_id, self.gaps())
        watch_plan.record_full_scan(self.vault_id, self.gaps("denied"))
        watch_plan.record_full_scan(self.vault_id, self.gaps())
        plan, debt = watch_plan.registration_plan(self.vault_id)
        watch_plan.record_full_scan(self.vault_id, self.gaps("denied"))
        watch_plan.record_full_scan(self.vault_id, self.gaps())
        self.assertEqual(watch_plan.record_registration(self.vault_id, plan, debt=debt), set())
        self.assertEqual(watch_plan.coverage_debt(self.vault_id), {"denied"})
        self.assertFalse(self.ready())
        successor_plan, successor_debt = watch_plan.registration_plan(self.vault_id)
        self.assertEqual(watch_plan.record_registration(
            self.vault_id, successor_plan, debt=successor_debt,
        ), {"denied"})
        self.assertTrue(self.ready())

    def test_unchanged_gap_does_not_create_permanent_debt(self):
        watch_plan.record_registration(self.vault_id, self.gaps())
        watch_plan.note_unreadable_directories(self.vault_id, {"denied"})
        plan, debt = watch_plan.registration_plan(self.vault_id)
        watch_plan.record_registration(self.vault_id, plan, debt=debt)
        watch_plan.record_full_scan(self.vault_id, self.gaps("denied"))
        self.assertEqual(watch_plan.coverage_debt(self.vault_id), set())
        self.assertTrue(self.ready())  # Incomplete scan, not coverage, blocks health.

    def test_file_only_gaps_do_not_need_directory_registration(self):
        found = watch_plan.UnobservedPaths(unsupported=frozenset({"bad-\udcff"}))
        watch_plan.record_registration(self.vault_id, found)
        watch_plan.record_full_scan(self.vault_id, watch_plan.UnobservedPaths())
        self.assertTrue(self.ready())

    def test_stopping_old_task_cannot_remove_its_replacements_coverage_fence(self):
        old, successor = object(), object()
        watch_plan.record_registration(self.vault_id, self.gaps("old"), owner=old)
        watch_plan.record_registration(self.vault_id, self.gaps("still-denied"), owner=successor)
        watch_plan.forget_registration(self.vault_id, owner=old)
        self.assertFalse(self.ready())
        self.assertEqual(watch_plan.registered_directories(self.vault_id), {"still-denied"})
        watch_plan.forget_registration(self.vault_id, owner=successor)
        self.assertTrue(self.ready())

    def test_stopped_watcher_releases_its_confirmation_gate(self):
        watch_plan.record_registration(self.vault_id, self.gaps("denied"))
        self.assertFalse(self.ready())
        watch_plan.forget_registration(self.vault_id)
        self.assertTrue(self.ready())
