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
        before_scan = watch_plan.registered_directories(self.vault_id)
        watch_plan.record_full_scan(self.vault_id, self.gaps())
        watch_plan.record_registration(self.vault_id, self.gaps())
        self.assertFalse(watch_plan.source_confirmation_ready(self.vault_id, before_scan))
        after_registration = watch_plan.registered_directories(self.vault_id)
        self.assertTrue(watch_plan.source_confirmation_ready(self.vault_id, after_registration))

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
