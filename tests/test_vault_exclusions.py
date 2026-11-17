"""Exclusions in the Vault browser: own rules, one-glob edits, disk reads (#754)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import vault_exclusions as exclusions
from app.services.operation_policies import OperationPolicy
from app.services.source_layout import VaultLocalAccess
from app.services.vault_relocation import ROOT_IDENTITY_VERSION, root_identity

ONLINE = VaultLocalAccess(True, True, "photos", "ok")
OFFLINE = VaultLocalAccess(False, True, "photos", "unavailable")


class OwnRuleTests(unittest.TestCase):
    def test_a_folder_gets_its_excluded_folder_rule_and_a_file_its_path(self):
        self.assertEqual(exclusions.own_rule("upload", is_directory=True), "upload/**")
        self.assertEqual(exclusions.own_rule("a/b/c.jpg", is_directory=False), "a/b/c.jpg")

    def test_a_path_with_glob_characters_has_no_own_rule(self):
        for path in ("a*b", "a?b", "a[1]", "x/a]b", "a\\b"):
            with self.subTest(path=path):
                self.assertIsNone(exclusions.own_rule(path, is_directory=False))
                self.assertIsNone(exclusions.own_rule(path, is_directory=True))


class EditGlobTests(unittest.TestCase):
    def test_adds_once_and_keeps_the_other_rules_and_settings(self):
        policy = OperationPolicy(stability_seconds=900, exclude_globs=("*.tmp",), include_globs=("**/*.jpg",))
        edited = exclusions.edit_glob(policy, "exclude_globs", "add", " upload/** ")
        self.assertEqual(edited.exclude_globs, ("*.tmp", "upload/**"))
        self.assertEqual(edited.include_globs, ("**/*.jpg",))
        self.assertEqual(edited.stability_seconds, 900)
        self.assertEqual(exclusions.edit_glob(edited, "exclude_globs", "add", "upload/**"), edited)

    def test_removes_only_that_rule_and_refuses_a_rule_no_longer_present(self):
        policy = OperationPolicy(exclude_globs=("a/**", "b/**"), include_globs=("**/*.jpg",))
        self.assertEqual(exclusions.edit_glob(policy, "exclude_globs", "remove", "a/**").exclude_globs, ("b/**",))
        self.assertEqual(exclusions.edit_glob(policy, "include_globs", "remove", "**/*.jpg").include_globs, ())
        with self.assertRaises(exclusions.RuleNotFound):
            exclusions.edit_glob(policy, "exclude_globs", "remove", "c/**")
        with self.assertRaises(ValueError):
            exclusions.edit_glob(policy, "exclude_globs", "add", "  ")


class ExclusionReasonTests(unittest.TestCase):
    def test_folder_rules_cover_a_folder_and_other_patterns_do_not(self):
        policy = OperationPolicy(exclude_globs=("upload/**", "**/node_modules/**", "*.tmp"))
        self.assertEqual(exclusions.exclusion_reasons(policy, "upload", is_directory=True)["rules"], ["upload/**"])
        self.assertEqual(exclusions.exclusion_reasons(policy, "a/node_modules/x", is_directory=True)["rules"],
                         ["**/node_modules/**"])
        self.assertFalse(exclusions.exclusion_reasons(policy, "library", is_directory=True)["excluded"])

    def test_a_file_reports_globs_fixed_noise_and_includes(self):
        policy = OperationPolicy(exclude_globs=("*.tmp", "upload/**"), include_globs=("**/*.jpg",))
        reasons = exclusions.exclusion_reasons(policy, "upload/a.tmp", is_directory=False)
        self.assertEqual(reasons["rules"], ["*.tmp", "upload/**"])
        self.assertTrue(reasons["not_included"])
        self.assertTrue(reasons["excluded"])
        noise = exclusions.exclusion_reasons(OperationPolicy(), "a/._x.jpg", is_directory=False)
        self.assertEqual(noise["fixed"], ["._*"])
        self.assertTrue(noise["excluded"])


class DiskReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "immich"
        for folder in ("library/2024", "upload/u1/aa", "thumbs", "backups"):
            (self.root / folder).mkdir(parents=True)
        (self.root / "library/2024/a.jpg").write_bytes(b"1" * 10)
        (self.root / "upload/u1/aa/b.jpg").write_bytes(b"2" * 20)
        (self.root / "upload/u1/c.jpg").write_bytes(b"3" * 30)
        (self.root / "upload/u1/c.tmp").write_bytes(b"4" * 40)
        (self.root / "upload/u1/._c.jpg").write_bytes(b"5")
        (self.root / "upload/readme.txt").write_bytes(b"hello")
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"s")
        os.symlink(outside, self.root / "upload/link")
        os.symlink(outside, self.root / "linked")
        self.vault = {"id": 1, "source_root": str(self.root), "root_identity_version": ROOT_IDENTITY_VERSION,
                      "root_identity": root_identity(str(self.root))}
        patcher = patch("app.services.vault_exclusions.vault_local_access", return_value=ONLINE)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.policy = OperationPolicy(exclude_globs=("upload/**", "thumbs/**", "backups/**", "*.tmp"))

    def test_the_root_shows_the_excluded_folders_with_their_rule(self):
        listing = exclusions.directory_listing(self.vault, self.policy, "")
        self.assertIsNone(listing["excluded_by"])
        self.assertEqual(listing["folders"], [
            {"name": "backups", "path": "backups", "rule": "backups/**"},
            {"name": "thumbs", "path": "thumbs", "rule": "thumbs/**"},
            {"name": "upload", "path": "upload", "rule": "upload/**"},
        ])
        self.assertEqual(listing["entries"], [])

    def test_without_folder_rules_the_disk_is_not_read(self):
        with patch("app.services.vault_exclusions._open_directory") as opened:
            listing = exclusions.directory_listing(self.vault, OperationPolicy(exclude_globs=("*.tmp",)), "")
        opened.assert_not_called()
        self.assertEqual(listing["folders"], [])

    def test_an_excluded_folder_lists_its_level_from_disk_without_symlinks(self):
        listing = exclusions.directory_listing(self.vault, self.policy, "upload")
        self.assertEqual(listing["excluded_by"], "upload/**")
        self.assertEqual([(e["name"], e["type"]) for e in listing["entries"]],
                         [("u1", "directory"), ("readme.txt", "file")])
        self.assertEqual(listing["entries"][1]["size"], 5)
        deeper = exclusions.directory_listing(self.vault, self.policy, "upload/u1")
        self.assertEqual([e["path"] for e in deeper["entries"]],
                         ["upload/u1/aa", "upload/u1/._c.jpg", "upload/u1/c.jpg", "upload/u1/c.tmp"])

    def test_a_listing_never_leaves_the_vault_through_a_symlink(self):
        policy = OperationPolicy(exclude_globs=("upload/**", "linked/**"))
        with self.assertRaises(exclusions.DiskReadError) as raised:
            exclusions.directory_listing(self.vault, policy, "upload/link")
        self.assertEqual(raised.exception.reason, "missing")
        with self.assertRaises(exclusions.DiskReadError):
            exclusions.directory_listing(self.vault, policy, "linked")
        self.assertNotIn("linked", [f["name"] for f in exclusions.directory_listing(self.vault, policy, "")["folders"]])

    def test_an_unavailable_or_replaced_root_is_not_read(self):
        with patch("app.services.vault_exclusions.vault_local_access", return_value=OFFLINE):
            with self.assertRaises(exclusions.DiskReadError) as raised:
                exclusions.directory_listing(self.vault, self.policy, "")
        self.assertEqual(raised.exception.reason, "unavailable")
        replaced = {**self.vault, "root_identity": "{}"}
        with self.assertRaises(exclusions.DiskReadError):
            exclusions.directory_listing(replaced, self.policy, "")

    def test_a_long_listing_is_truncated_and_counted(self):
        for index in range(5):
            (self.root / f"thumbs/t{index}.webp").write_bytes(b"")
        with patch.object(exclusions, "MAX_LISTING_ENTRIES", 3):
            listing = exclusions.directory_listing(self.vault, self.policy, "thumbs")
        self.assertEqual((len(listing["entries"]), listing["total_entries"], listing["truncated"]), (3, 5, True))

    def test_measure_counts_what_the_policy_would_upload(self):
        after = OperationPolicy(exclude_globs=("thumbs/**", "*.tmp"))
        measured = exclusions.measure_upload(self.vault, after, "upload", is_directory=True)
        # b.jpg, c.jpg, readme.txt: not c.tmp, not ._c.jpg, never the symlink.
        self.assertEqual(measured, {"files": 3, "bytes": 55, "complete": True})
        single = exclusions.measure_upload(self.vault, after, "upload/u1/c.jpg", is_directory=False)
        self.assertEqual(single, {"files": 1, "bytes": 30, "complete": True})

    def test_measure_skips_folders_still_excluded(self):
        after = OperationPolicy(exclude_globs=("upload/u1/aa/**",))
        measured = exclusions.measure_upload(self.vault, after, "upload", is_directory=True)
        self.assertEqual(measured, {"files": 3, "bytes": 75, "complete": True})

    def test_measure_past_its_budget_is_a_minimum(self):
        ticks = iter(range(100))
        measured = exclusions.measure_upload(self.vault, OperationPolicy(), "upload", is_directory=True,
                                             budget_seconds=2, clock=lambda: next(ticks))
        self.assertFalse(measured["complete"])
        self.assertLess(measured["files"], 5)


if __name__ == "__main__":
    unittest.main()
